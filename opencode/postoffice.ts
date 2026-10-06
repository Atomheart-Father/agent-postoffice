// agent-postoffice × OpenCode 投递插件
// 盯着 routes.json 里 methods 含 "opencode_plugin" 的信箱；有新信且目标会话空闲时，
// 用 OpenCode 自带 client 给该会话发一条提醒（普通信给路径 + 开头三行；回执只给来源/原事由 +
// 查询命令 + “默认不答复”，不附正文；归档用 postoffice archive-receipt，见 skill）。
// 同一轮正式信在前；没有正式信时把积压回执合并成一条“另有 N 条回执”清单，超过等待时限的回执
// 也会随下一次投递带上，绝不被正式信永远挤掉。
// - 只投给属于本 OpenCode 实例目录的会话；多个实例同时运行时用认领文件保证一封信只投一次
// - 离线（status: "offline"）的信箱不投，信留在 inbox，上线后补送
// - 每封只投一次（opencode_delivered.jsonl 账本），重启不重投；每信箱每 10 分钟最多 6 次
// - 失败重试一次（按每个文件各自计数，到上限的单独记 FAILED_FINAL 并通知人）；任何异常都吞掉，绝不影响 OpenCode 本身
// - prompt 已被接受但本地记账失败：不当作“没投”、不自动重发；认领保留 + 日志 + 通知人核对一次。
//   恢复规则由人决定：删掉 <信箱>/.claims/<文件名> 才允许再投一次，或把信挪进 done 收账
// - 不改模型/工具/权限，不新建会话，不批准权限请求
//
// 另外提供四个原生工具，模型只能操作**当前会话自己**的信箱：
//   postoffice_alarm_schedule(delay_minutes) —— 只收一个分钟数，没有收件人/信箱/正文参数
//   postoffice_alarm_cancel()               —— 不收任何参数
//   postoffice_message_edit(message_ref, content) —— 原地改（或 content=null 撤回）自己发出、
//                                                   还没被对方通道接受的普通信；规矩同 CLI 的 edit/retract
//   postoffice_archive_current(keep_unarchived)   —— 归档本会话已展示过、还没归档的信（presented 集合）
// 身份只来自框架给的 ToolContext.sessionID，再唯一映射到已登记且启用插件的信箱；
// 无映射、多个映射、信箱离线、身份核不上时一律拒绝，模型无法指定别的信箱或会话。
// 改信/归档的真正改动都由 CLI 在既有的认领与锁下完成，插件自己不动信。
// 闹钟记录落在 <HOME>/alarms/<session>.json（运行态账本，0600），由常驻邮递员到期时写成
// 一封固定短句的信，再复用上面这条 idle 投递通道送达 —— 插件本身不实现计时器。
// 到期的提示是**常量**：只有与本会话自己的活动闹钟记录 id 完全一致的信才走闹钟通道，
// 渲染时直接用 ALARM_TEXT，一个字的信件正文都不读；伪造的信头或陈旧记录按普通信路径处理。
import type { Plugin } from "@opencode-ai/plugin"
import type { tool as opencodeTool } from "@opencode-ai/plugin/tool"
import { homedir, platform } from "node:os"
import { readdir, readFile, writeFile, appendFile, mkdir, open, rm, stat, chmod, rename } from "node:fs/promises"
import { accessSync, constants, existsSync } from "node:fs"
import { spawn } from "node:child_process"
import { createHash } from "node:crypto"
import { fileURLToPath } from "node:url"
import { realpathSync } from "node:fs"
import { join, dirname } from "node:path"

const ROOT = process.env.POSTOFFICE_HOME || `${homedir()}/agent-postoffice`
const LEDGER = `${ROOT}/opencode_delivered.jsonl`
const LOG = `${ROOT}/logs/opencode_plugin.log`
const ALARM_DIR = `${ROOT}/alarms`
const POLL_MS = 10_000
const RATE_N = 6
const RATE_WIN_MS = 600_000
const MAX_ATTEMPTS = 2
const MAX_FORMAL_BATCH = 20   // ordinary letters per single wake; the rest go out next round
const RECEIPT_WAIT_MS = Number(process.env.POSTOFFICE_RECEIPT_WAIT || 600) * 1000
const ALARM_MIN = 1
const ALARM_MAX = 1440
const ALARM_TEXT = "你设的闹钟到了，请检查刚才安排的任务。"

type Route = { methods?: string[]; session_id?: string; status?: string }
type Alarm = {
  id: string
  box: string
  session: string
  due: number
  created: number
  state: string
  letter?: string
  note?: string
}

const stamp = (d = new Date()) => {
  const p = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}

const wallClock = (epochSeconds: number) => {
  try {
    return new Date(epochSeconds * 1000).toLocaleString()
  } catch {
    return String(epochSeconds)
  }
}

type Zod = typeof opencodeTool.schema

const log = async (msg: string) => {
  try {
    await mkdir(`${ROOT}/logs`, { recursive: true })
    await appendFile(LOG, `${new Date().toISOString()} ${msg}\n`)
  } catch {}
}

const notifyHuman = (text: string) => {
  try {
    const t = text.replace(/"/g, "'").slice(0, 180)
    void log(`通知人：${t}`) // 系统通知本身不留痕，日志留一条，便于事后核对
    if (process.env.POSTOFFICE_NO_NOTIFY) return // 静默/测试场景：只记日志，不弹通知
    if (platform() === "darwin")
      spawn("osascript", ["-e", `display notification "${t}" with title "联络总站投递失败"`], { stdio: "ignore" })
    else spawn("notify-send", ["联络总站投递失败", t], { stdio: "ignore" })
  } catch {}
}

const readLedger = async () => {
  const done = new Set<string>()
  const failed = new Map<string, number>()
  try {
    if (!existsSync(LEDGER)) return { done, failed }
    for (const line of (await readFile(LEDGER, "utf8")).split("\n")) {
      if (!line) continue
      try {
        const e = JSON.parse(line)
        const k = `${e.box}::${e.file}`
        if (e.result === "DELIVERED" || e.result === "FAILED_FINAL") done.add(k)
        else if (e.result === "FAILED_RETRYABLE") failed.set(k, (failed.get(k) ?? 0) + 1)
      } catch {}
    }
  } catch {}
  return { done, failed }
}

// 认领：多个 OpenCode 实例同时运行时，只有抢到认领文件的那个投递。
// 抢到之后立刻确认信还在 inbox/ 里：`postoffice retract` 会把还没被接受的信原样挪进 archived/，
// 若它恰好在「列出待投」与「抢到认领」之间撤回，这里放掉认领并跳过，不投递一封已经不在的信。
const claim = async (box: string, file: string) => {
  try {
    await mkdir(`${ROOT}/${box}/.claims`, { recursive: true })
    const fh = await open(`${ROOT}/${box}/.claims/${file}`, "wx")
    await fh.close()
  } catch {
    return false
  }
  try {
    await stat(`${ROOT}/${box}/inbox/${file}`)
    return true
  } catch {
    await rm(`${ROOT}/${box}/.claims/${file}`, { force: true })
    return false
  }
}

const head3 = async (path: string) => {
  try {
    return (await readFile(path, "utf8")).split("\n").slice(0, 3).join("\n")
  } catch {
    return "（读取开头失败，请直接打开信件）"
  }
}

// the original subject of a receipt notification (metadata only)
const receiptSubject = (fieldOf: (k: string) => string) =>
  (fieldOf("原事由：") || fieldOf("事由：")).replace(/^(回执：|copy that：|copy that:)/, "").slice(0, 60)

// POSIX single-quote a value for a shell command shown to the model (paths/ids may hold spaces, ();')
const shq = (s: string) => "'" + s.replace(/'/g, "'\\''") + "'"

// A batch line must show the letter as it stands *after* the claim: a concurrent edit can
// rewrite it in the window between the scan snapshot and the claim. Python's reminder()
// re-reads after claiming too, so both sides deliver the version the claim froze.
const letterHead = async (box: string, file: string) => {
  try {
    const raw = await readFile(`${ROOT}/${box}/inbox/${file}`, "utf8")
    const head = raw.split("\n\n", 1)[0].split("\n")
    const fieldOf = (k: string) => {
      const l = head.find((x) => x.startsWith(k))
      return l ? l.slice(k.length).trim() : ""
    }
    return { src: fieldOf("来源：").split("（")[0].trim(), subj: fieldOf("事由：") }
  } catch {
    return { src: "", subj: "" }
  }
}

// presented set: which ids this box was actually shown. Only written after the channel accepted
// the wake, union-only, atomic rewrite. It is an archive hint, not a second delivery truth —
// delivery truth stays in the ledger / .seen / .woken.json / the claims.
const presentedPath = (box: string) => `${ROOT}/${box}/.presented.json`
const presentedAdd = async (box: string, ids: string[]) => {
  const clean = ids.filter(Boolean)
  if (!clean.length) return
  const p = presentedPath(box)
  let cur: string[] = []
  try {
    const v = JSON.parse(await readFile(p, "utf8"))
    if (Array.isArray(v)) cur = v.filter((x): x is string => typeof x === "string")
  } catch {}
  const merged = cur.slice()
  for (const id of clean) if (!merged.includes(id)) merged.push(id)
  const tmp = `${p}.tmp-${process.pid}`
  await writeFile(tmp, JSON.stringify(merged), "utf8")
  await rename(tmp, p)
}

// ---------------------------------------------------------------- 闹钟记录
// 一个会话一个文件：设/取消/到期清理都只碰自己那一个，两个写者（插件工具 / 邮递员）
// 不会互相覆盖别人的记录。
const alarmPath = (sessionID: string) => {
  const safe = /^[A-Za-z0-9_.-]{1,80}$/.test(sessionID)
    ? sessionID
    : createHash("sha256").update(sessionID).digest("hex").slice(0, 32)
  return `${ALARM_DIR}/${safe}.json`
}

// 只有「这条记录确实是本会话的、信箱对得上、而且已经进入 lettered（= 邮递员确认到期并落了信）」
// 才允许走闹钟短通道。记录还在 pending 时，inbox 里哪怕出现同编号的信也不认 ——
// 那样等于绕过了到期判定。正文依旧一个字节都不读。
const alarmLetterOwns = (rec: Alarm | null, box: string, sessionID: string): Alarm | null => {
  if (!rec) return null
  if (rec.session !== sessionID || rec.box !== box) return null
  if (rec.state !== "lettered") return null
  if (rec.letter !== rec.id) return null          // lettered 记录必须指着它自己那封信
  return rec
}

const readAlarm = async (sessionID: string): Promise<Alarm | null> => {
  try {
    const v = JSON.parse(await readFile(alarmPath(sessionID), "utf8"))
    return v && typeof v === "object" && typeof v.box === "string" ? (v as Alarm) : null
  } catch {
    return null
  }
}

// 锁不在这一侧。
//
// 同一会话的闹钟记录会被本插件（设/取消）和常驻邮递员（到期）同时改，早期版本两边各拿一把
// O_EXCL 文件锁，再配 mtime 看门狗 + pid 存活检查 + 回收锁去接「持有者崩溃后怎么接管」。
// 那套协议有两个躲不掉的问题：回收是 check-then-unlink，两个进程都判定过期时会互删对方的
// 新锁；硬超时又不得不无视 pid，于是暂停超过硬超时的旧持有者恢复后会按路径删掉新锁。
//
// 现在锁是内核 flock(2)，而 Node 没有 flock API —— 所以**写记录只由 Python 侧做**：
// 插件用 client 确认这个会话归本实例、解析出唯一信箱，然后调用
// `postoffice alarm-set` / `postoffice alarm-cancel`，由 Python 在 flock 内改记录。
// 内核在持有者崩溃/被 kill/重启时自动释放，锁文件永不删除或替换，因此不存在 ABA，
// 「旧持有者删掉新持有者的锁」在结构上不可能发生。
// The CLI is the only writer of alarm records, and it must come from the same checkout as this
// plugin. Which handle on it is legitimate depends on which layout this file was loaded from, so
// the two are told apart instead of trying everything everywhere:
//
//   checkout  the plugin sits in <checkout>/opencode/, so ../postoffice *is* that checkout's CLI
//             and nothing else can be. Candidate order: ../postoffice, then ~/.local/bin.
//   installed `postoffice install opencode` copies this .ts into one fixed place —
//             $XDG_CONFIG_HOME/opencode/plugins/postoffice.ts — and points ~/.local/bin/postoffice
//             back at the checkout it came from. Here ../postoffice is $XDG_CONFIG_HOME/opencode/
//             postoffice, which is NOT that checkout: anything executable sitting there would win
//             over the correct symlink. So: ~/.local/bin/postoffice only.
//
// The test is the directory the plugin was loaded from, compared against the installer's own
// destination (opencode_config_dir() on the Python side computes the same thing). That is a fact
// about where the file is, not a guess about who installed it, and both branches are exercised:
// tests/alarm_install_test.mjs runs the checkout layout with no symlink in HOME and the installed
// layout with a same-directory foreign executable that must stay untouched.
//
// POSTOFFICE_CLI stays an explicit override/debug switch, first, and is not something an install
// depends on. Deliberately not a candidate: $POSTOFFICE_HOME/postoffice — the post office home is
// per-operator state rather than a checkout, so a `postoffice` file there may belong to any
// checkout, which is exactly how an installed plugin ends up driving a foreign CLI.
const runnable = (p: string) => {
  // accessSync returns undefined on success, so it must not be the tail of the && chain —
  // `find` would read that undefined as falsy and skip a perfectly good candidate.
  try { return !!p && existsSync(p) && accessSync(p, constants.X_OK) === undefined } catch { return false }
}
const realpathIf = (p: string) => { try { return realpathSync(p) } catch { return p } }
const INSTALLED_PLUGIN_DIR =
  join(process.env.XDG_CONFIG_HOME || join(homedir(), ".config"), "opencode", "plugins")
const SELF_DIR = realpathIf(dirname(fileURLToPath(import.meta.url)))
const inCheckout = SELF_DIR !== realpathIf(INSTALLED_PLUGIN_DIR)
const PO_CLI =
  (process.env.POSTOFFICE_CLI || "").trim() ||
  (inCheckout
    ? [new URL("../postoffice", import.meta.url).pathname, join(homedir(), ".local/bin/postoffice")]
    : [join(homedir(), ".local/bin/postoffice")]
  ).map(realpathIf).find(runnable) || ""

// ---------------------------------------------------------------------- zod：只走官方 API
// OpenCode's ToolDefinition is ReturnType<typeof tool>, so `args` is a z.ZodRawShape and
// `@opencode-ai/plugin/tool` re-exports zod as `tool.schema`. That is the only thing used here: no
// direct zod import, no sidecar, no hand-rolled lookup — the bare specifier below is resolved by
// Node itself, from this file's own directory, exactly as a static `import` would be.
//
// In a real install that needs no configuration at all: the plugin is loaded out of
// $XDG_CONFIG_HOME/opencode/plugins/, i.e. from underneath the very node_modules OpenCode loads
// plugins with. A bare repo checkout has no node_modules above it, so the import fails there; that
// is handled by not registering the two alarm tools (see below), never by taking 投信 down too.
const loadZod = async (): Promise<Zod | null> => {
  try {
    const m = await import("@opencode-ai/plugin/tool")
    return m?.tool?.schema ?? null
  } catch {
    return null
  }
}

const runCLI = (args: string[], timeout = 20_000): Promise<{ code: number; text: string }> =>
  new Promise((resolve) => {
    // 找不到可执行文件是一种要如实回答的状态，不是抛给模型的异常：下面 execute() 会把它
    // 变成一句中文说明。spawn 对空/不可执行的 file 会同步抛，那会让整个工具 reject。
    if (!PO_CLI) {
      resolve({ code: -1, text: `找不到 postoffice 可执行文件。这个插件是从${
        inCheckout ? " checkout" : "安装位置"}加载的，只找过${
        inCheckout ? " ../postoffice（同 checkout 的脚本）和 ~/.local/bin/postoffice"
                   : " ~/.local/bin/postoffice（装插件的那份 checkout，`postoffice install opencode` 会把链接指回去）"
      }，都不可用。修法：重跑那一 checkout 的 postoffice install opencode，或显式设 ` +
        "POSTOFFICE_CLI=<那份 checkout 里的 postoffice>。" })
      return
    }
    try {
      const child = spawn(PO_CLI, args, {
        env: { ...process.env, POSTOFFICE_HOME: ROOT, POSTOFFICE_NO_NOTIFY: "1" },
      })
      let out = ""
      const timer = setTimeout(() => child.kill("SIGKILL"), timeout)
      child.stdout.on("data", (d) => (out += d))
      child.stderr.on("data", (d) => (out += d))
      child.on("error", (e) => { clearTimeout(timer); resolve({ code: -1, text: String(e) }) })
      child.on("close", (code) => { clearTimeout(timer); resolve({ code: code ?? -1, text: out.trim() }) })
    } catch (e) {
      resolve({ code: -1, text: String(e) })
    }
  })

export const PostofficePlugin: Plugin = async ({ client, directory }) => {
  const recent = new Map<string, number[]>()
  const mine = new Map<string, boolean>() // sessionID → 是否属于本实例目录
  let scanning = false

  const ownsSession = async (id: string) => {
    if (mine.has(id)) return mine.get(id)!
    try {
      const res = await client.session.get({ path: { id } })
      const ok = !!res.data && (res.data as { directory?: string }).directory === directory
      mine.set(id, ok)
      return ok
    } catch {
      return false
    }
  }

  // 核身份时现查一次，不用投递路径那个缓存：认人这件事不能拿旧答案
  const ownsSessionNow = async (id: string) => {
    try {
      const res = await client.session.get({ path: { id } })
      return !!res.data && (res.data as { directory?: string }).directory === directory
    } catch {
      return false
    }
  }

// 反查“这个会话自己的物理信箱”：唯一匹配 + 走插件通道 + 属于本实例目录。
// 任何一条不成立都拒绝 —— 宁可没有闹钟，也不能把提醒投到别人的信箱去。
// 路由离线单独标出来：离线时身份仍然是唯一的，只是当下没人收 —— 取消自己的定时器
// 可以照常做（连同那封还没人收的提醒一起收走），设新闹钟则拒绝。
const resolveOwnBox = async (sessionID: string): Promise<{ box?: string; why?: string; offline?: boolean }> => {
    let routes: Record<string, Route> = {}
    try {
      routes = JSON.parse(await readFile(`${ROOT}/routes.json`, "utf8"))
    } catch {
      return { why: "读不到邮局通讯录 routes.json" }
    }
    const hits = Object.entries(routes).filter(
      ([box, r]) =>
        !box.startsWith("_") &&
        r &&
        typeof r === "object" &&
        (r.methods ?? []).includes("opencode_plugin") &&
        r.session_id === sessionID,
    )
    if (!hits.length) return { why: "本会话没有登记为由 OpenCode 插件投递的信箱" }
    if (hits.length > 1)
      return { why: `本会话同时对应多个信箱（${hits.map((h) => h[0]).join("、")}），无法确定唯一目标` }
    const [box, route] = hits[0]
    if (!(await ownsSessionNow(sessionID))) return { why: "无法确认本会话属于当前 OpenCode 实例" }
    return route.status === "offline" ? { box, offline: true } : { box }
  }

  const ownBox = async (sessionID: string) => {
    const r = await resolveOwnBox(sessionID)
    return r.offline ? { why: `信箱 ${r.box} 当前离线` } : r
  }

  async function scan(reason: string) {
    if (scanning) return
    scanning = true
    try {
      let routes: Record<string, Route> = {}
      try {
        routes = JSON.parse(await readFile(`${ROOT}/routes.json`, "utf8"))
      } catch {
        return
      }
      const ledger = await readLedger()
      for (const [box, route] of Object.entries(routes)) {
        if (box.startsWith("_") || !route || typeof route !== "object") continue
        if (!(route.methods ?? []).includes("opencode_plugin") || !route.session_id) continue
        if (route.status === "offline") continue
        const sessionID = route.session_id
        let files: string[] = []
        try {
          files = (await readdir(`${ROOT}/${box}/inbox`)).filter((f) => f.endsWith(".md")).sort()
        } catch {
          continue
        }
        const todo = files.filter((f) => !ledger.done.has(`${box}::${f}`))
        if (!todo.length || !(await ownsSession(sessionID))) continue
        // 只有与本会话自己的活动闹钟记录 id 完全一致的信，才享受「闹钟」这条极短通道
        const ownAlarm = await alarmLetterOwns(await readAlarm(sessionID), box, sessionID)
        // OpenCode 的 status 表只列非空闲会话：缺席 = 空闲
        let st = "idle"
        try {
          const res = await client.session.status()
          st = (res.data as Record<string, { type?: string }>)?.[sessionID]?.type ?? "idle"
        } catch {
          continue
        }
        if (st !== "idle") continue
        // 读开头块分出正式信、闹钟提醒与回执通知；正文永不注入
        type Item = { file: string; src: string; subj: string; id: string; mtime: number }
        type Formal = Item & { alarm?: boolean }
        const formal: Formal[] = []   // ordinary letters: batchable
        const alarms: Formal[] = []   // this session's own alarm reminders: never batched
        const receipts: Item[] = []
        for (const file of todo) {
          const key = `${box}::${file}`
          if ((ledger.failed.get(key) ?? 0) >= MAX_ATTEMPTS) continue
          const path = `${ROOT}/${box}/inbox/${file}`
          let raw: string
          let mtime = 0
          try {
            mtime = (await stat(path)).mtimeMs
            raw = await readFile(path, "utf8")
          } catch {
            continue
          }
          const head = raw.split("\n\n", 1)[0].split("\n")
          const fieldOf = (k: string) => {
            const l = head.find((x) => x.startsWith(k))
            return l ? l.slice(k.length).trim() : ""
          }
          const id = fieldOf("回执：")
          const alarmId = fieldOf("闹钟：")
          if (id) receipts.push({ file, src: fieldOf("来源：").split("（")[0].trim(), subj: receiptSubject(fieldOf), id, mtime })
          else if (alarmId && ownAlarm && ownAlarm.id === alarmId && file === `${alarmId}.md`) {
            // 闹钟提醒：只渲染那句固定常量，正文一个字节都不读、不注入
            alarms.push({ file, src: "postoffice", subj: "闹钟", id: alarmId, mtime, alarm: true })
          } else formal.push({ file, src: fieldOf("来源：").split("（")[0].trim(), subj: fieldOf("事由："), id: "", mtime })
        }
        // 一批普通正式信（≤ MAX_FORMAL_BATCH）优先；没有普通信时，本会话自己的闹钟提醒独占一轮；
        // 再没有才轮到回执。抢不到认领的那几封这一轮不投，但不会挡住后面的信（跳过继续收集）。
        const quota = formal.slice(0, MAX_FORMAL_BATCH)
        const alarmRound = quota.length === 0 && alarms.length > 0
        const due = quota.length
          ? receipts.filter((r) => Date.now() - r.mtime > RECEIPT_WAIT_MS)
          : (alarmRound ? [] : receipts)
        const group: { file: string; isReceipt: boolean }[] = (quota.length
          ? quota.map((f) => ({ file: f.file, isReceipt: false }))
          : alarmRound ? [{ file: alarms[0].file, isReceipt: false }] : [])
          .concat(due.map((r) => ({ file: r.file, isReceipt: true })))
        if (!group.length) continue
        const now = Date.now()
        const win = (recent.get(box) ?? []).filter((t) => now - t < RATE_WIN_MS)
        if (win.length >= RATE_N) continue
        // 每次投递前都认领：失败时已释放认领，所以重试照样能拿到；
        // 抢不到说明别的实例正在投这封，跳过（否则多实例会各投一次）
        const claimed: { file: string; isReceipt: boolean }[] = []
        for (const g of group) {
          if (!(await claim(box, g.file))) continue
          claimed.push(g)
        }
        if (!claimed.length) continue
        const claimedFormal = claimed.filter((g) => !g.isReceipt).map((g) => g.file)
        const claimedReceipts = claimed.filter((g) => g.isReceipt)
          .map((g) => receipts.find((r) => r.file === g.file)!).filter(Boolean)
        let text: string
        const alarmFormal = claimedFormal.length === 1 && alarms.some((f) => f.file === claimedFormal[0])
        if (alarmFormal) {
          // 固定短句：常量，不来自信件
          text = `⏰ ${ALARM_TEXT}`
        } else if (claimedFormal.length === 0 && claimedReceipts.length === 1) {
          const r = claimedReceipts[0]
          text = `【联络总站回执｜${box}】来自 ${r.src} 的回执` + (r.subj ? `，原事由：${r.subj}` : "") + `\n` +
                 `查询：postoffice receipt ${shq(box)} ${shq(r.id)}\n默认不答复`
        } else {
          const parts: string[] = []
          if (claimedFormal.length >= 2) {
            // 多封：公共 header 与规则各只写一次，每封一行（稳定引用 + 来源 + 事由 + 路径）
            parts.push(`【联络总站｜${claimedFormal.length} 封新信】`)
            let i = 1
            for (const f of claimedFormal) {
              const m = await letterHead(box, f)
              parts.push(`${i}. ${box}/${f.replace(/\.md$/, "")}  来源：${m.src || "?"}  ` +
                `事由：${m.subj || "（无）"}  ${ROOT}/${box}/inbox/${f}`)
              i++
            }
            parts.push(`按各信“需要”字段处理；回信/回执/归档规则见 postoffice skill。`)
          } else {
            for (const f of claimedFormal) {
              parts.push(`【联络总站新信｜${box}】\n== ${ROOT}/${box}/inbox/${f}\n${await head3(`${ROOT}/${box}/inbox/${f}`)}\n` +
                `按信件“需要”字段处理；回信/回执/归档规则见 postoffice skill。`)
            }
          }
          if (claimedReceipts.length) {
            parts.push(`另有 ${claimedReceipts.length} 条回执（默认不答复，需要时按 ID 查询）：\n` +
              claimedReceipts.map((r) => `- ${r.src}：${r.subj}  查询：postoffice receipt ${shq(box)} ${shq(r.id)}`).join("\n"))
          }
          text = parts.join("\n")
        }
        const files2 = claimed.map((g) => g.file).join(",")
        // 一批只发一次 prompt，但每个文件按自己的失败次数递增、各自判断是否已到上限
        const tries = new Map<string, number>()
        for (const g of claimed) tries.set(g.file, (ledger.failed.get(`${box}::${g.file}`) ?? 0) + 1)
        try {
          await client.session.promptAsync({ path: { id: sessionID }, body: { parts: [{ type: "text", text }] } })
        } catch (e) {
          // 投递失败：逐个文件记账（到上限的写 FAILED_FINAL），释放认领以便重试或别的实例接手
          let finals = 0
          for (const g of claimed) {
            const attempt = tries.get(g.file)!
            const final = attempt >= MAX_ATTEMPTS
            if (final) finals++
            await appendFile(LEDGER, JSON.stringify({ time: new Date().toISOString(), box, file: g.file, session: sessionID, result: final ? "FAILED_FINAL" : "FAILED_RETRYABLE", attempt, detail: String(e).slice(0, 200) }) + "\n")
            try { await rm(`${ROOT}/${box}/.claims/${g.file}`, { force: true }) } catch {}
          }
          await log(`FAILED ${box}/${files2}: ${e}`)
          if (finals) notifyHuman(`${box}/${files2} 有 ${finals} 封投递失败已达上限，请手动转交`)
          continue
        }
        // prompt 已被接受：算一次唤醒（限流照旧），此后本地记账出错也不能当成“没投”
        win.push(now)
        recent.set(box, win)
        try {
          // 只有这时候才算「展示过」：后面 archive_current 只动这一集合
          await presentedAdd(box, claimed.map((g) => g.file.replace(/\.md$/, "")))
        } catch (e) {
          await log(`presented 记录失败（不影响投递，信仍留在 inbox）${box}/${files2}: ${e}`)
        }
        try {
          for (const g of claimed) {
            await appendFile(LEDGER, JSON.stringify({ time: new Date().toISOString(), box, file: g.file, session: sessionID, result: "DELIVERED", attempt: tries.get(g.file) }) + "\n")
          }
        } catch (e) {
          // 恢复规则：认领保留 → 本实例与其它实例都不会自动重投；账本缺行 → 下次启动看似待投
          // 但仍被认领挡住。要重投由人先删认领文件（或把信挪进 done 收账），不会静默重发。
          await log(`DELIVERED 但记账失败（不重发，认领保留待人工核对）${box}/${files2}: ${e}`)
          notifyHuman(`${box}/${files2} 已投递但记账失败，请核对 ${LEDGER}`)
          continue
        }
        await log(`DELIVERED ${box}/${files2} → ${sessionID}（${reason}）`)
      }
    } catch (e) {
      await log(`scan 异常（已忽略）: ${e}`)
    } finally {
      scanning = false
    }
  }

  // 旧版本把插件放在项目 .opencode/plugin(s)/ 里；若存在则让位，避免重复投递
  let self = ""
  try {
    self = realpathSync(fileURLToPath(import.meta.url))
  } catch {}
  for (const d of ["plugin", "plugins"]) {
    const local = `${directory}/.opencode/${d}/postoffice.ts`
    let localReal = ""
    try {
      localReal = realpathSync(local)
    } catch {}
    if (localReal && localReal !== self) {
      await log(`实例 ${directory} 内已有项目级 postoffice.ts，全局插件在此实例让位`)
      return {}
    }
  }

  await log(`投递插件上岗（实例 ${directory}）`)
  const timer = setInterval(() => void scan("poll"), POLL_MS)
  void scan("boot")
  const event = async ({ event }: { event: { type?: string } }) => {
    try {
      if (event?.type === "session.idle") void scan("session.idle")
    } catch {}
  }
  // args 按官方形态声明：ToolDefinition 就是 tool() 的返回类型，args 是 z.ZodRawShape。
  // （旧的 JSON-Schema 形态在 v1.18.x 有 legacy 兼容，不会必然注册失败；换成 tool.schema 是为了
  // 类型与校验都落在同一处，而不是为了绕开注册错误。）
  // 拿不到 zod 就不注册这两个工具：投递通道照旧，但模型不会拿到一个没有 schema 的 args。
  const zod = await loadZod()
  if (!zod) {
    const why = "闹钟工具未注册：这个 OpenCode 运行环境里解析不到 @opencode-ai/plugin，" +
      "拿不到它的 tool.schema 就没法声明工具参数；请确认 OpenCode 自己的依赖树完整。投递通道不受影响。"
    await log(why)
    notifyHuman(`联络总站：${why.slice(0, 120)}`)
    return { event, dispose: async () => clearInterval(timer) }
  }
  return {
    event,
    tool: {
      postoffice_alarm_schedule: {
        description:
          "给**当前这个会话自己**设一个一次性闹钟（单位分钟，1–1440）。等待期间不需要模型参与，" +
          "所以设完就结束本轮：不要 sleep、不要轮询、不要空转；到点邮局会发一条固定短提醒把你叫回来。" +
          "同一会话同时只能有一个闹钟，已经有的话必须先 cancel，不能直接再设一个。" +
          "收件人由本会话自动绑定，不接受也不需要你指定信箱、会话或提醒内容。",
        args: {
          delay_minutes: zod.number().int().min(ALARM_MIN).max(ALARM_MAX)
            .describe(`从现在起多少分钟后响一次（${ALARM_MIN}–${ALARM_MAX} 分钟的整数）。`),
        },
        async execute(args, ctx) {
          const raw = (args as { delay_minutes?: unknown }).delay_minutes
          if (typeof raw !== "number" || !Number.isFinite(raw))
            return `没设闹钟：delay_minutes 必须是数字（单位分钟，整数，${ALARM_MIN}–${ALARM_MAX}）。`
          if (!Number.isInteger(raw))
            return `没设闹钟：delay_minutes 要整数分钟（现在给的是 ${raw}）。`
          if (raw < ALARM_MIN || raw > ALARM_MAX)
            return `没设闹钟：delay_minutes 要在 ${ALARM_MIN}–${ALARM_MAX} 分钟之间（现在给的是 ${raw}）。`
          const who = await ownBox(ctx.sessionID)
          if (!who.box)
            return `没设闹钟：${who.why}。邮局不会把提醒投给身份不确定的信箱，` +
              `请让本会话在邮局登记（postoffice add …）后再设。`
          // 记录由 Python 在内核锁里写：这里只把「这个会话属于哪个信箱」这件只有插件能确认的
          // 事告诉它。box/session 由本进程推导，模型指定不了。
          const r = await runCLI(["alarm-set", "--box", who.box, "--session", ctx.sessionID,
                                  "--delay", String(raw)])
          await log(`ALARM SET ${ctx.sessionID} rc=${r.code}`)
          if (r.code !== 0) return `没设闹钟：${r.text}`
          const cur = await readAlarm(ctx.sessionID)
          if (!cur) return `已设闹钟（${raw} 分钟后），但运行态记录读不回来，请用 postoffice doctor 查一下。`
          return (
            `已设 ${raw} 分钟闹钟（编号 ${cur.id}，信箱 ${who.box}，到期 ${wallClock(cur.due)}）。\n` +
            `等待期间不需要模型参与：请现在结束本轮，不要 sleep、不要轮询、不要空转；` +
            `你安排的那个任务要能脱离本轮自己跑下去，到点邮局会发一条固定短提醒把你叫回来。\n` +
            `要提前取消就调用 postoffice_alarm_cancel。`
          )
        },
      },
      postoffice_alarm_cancel: {
        description:
          "取消**当前这个会话自己**的活动闹钟（不带任何参数）。没有活动闹钟时会明确告诉你“没有活动闹钟”。" +
          "如果提醒已经到期进了投递队列、但还没被投递方认领，会把它移进 archived/ 存档，" +
          "这样取消后不会再响；如果提醒已经进入投递流程（认领已被投递方抢到），则**无法保证取消**，" +
          "这时会明确告诉你，并且不改动活动记录与那封信。" +
          "本会话当前的信箱归属核不上（或已改绑给别人）时会拒绝，不会去动任何信箱的文件。",
        args: {},
        async execute(_args, ctx) {
          const who = await resolveOwnBox(ctx.sessionID)
          if (!who.box)
            return `没取消：${who.why}。归属核不上时邮局不会去动任何信箱的文件，` +
              `请先让本会话在通讯录里对应唯一信箱（postoffice add … / doctor 看绑定），再试一次。`
          // 同上：取消也由 Python 在锁内做，包括「与投递方争同一把认领、把还没送出的提醒
          // 存档」那一步；CLI 的文案已经如实覆盖离线与「投递已在路上」两种结局。
          const r = await runCLI(["alarm-cancel", "--box", who.box, "--session", ctx.sessionID])
          await log(`ALARM CANCEL ${ctx.sessionID} rc=${r.code}`)
          if (r.code !== 0) return `没取消：${r.text}`
          return `已取消本会话的活动闹钟。\n${r.text}`
        },
      },
      postoffice_message_edit: {
        description:
          "改一封**你自己发的、还没被收件方通道接受**的普通信：content 给 subject/need/body 里至少一个 → 原地更新，" +
          "编号、文件名、来源、收件人都不变，收件方只会看到新版；content 给 null → 撤回这封信（原样进 archived/，" +
          "收件方不会再因这封信被唤醒）。message_ref 写成 <收件信箱>/<信件编号>（send 会打印「引用：」）。" +
          "只对普通直发信有效：回执/广播/交接/闹钟等派生通知不能改；已经进入投递的会明确拒绝，" +
          "这时请另发一封更正消息，不要把已经送出的内容当成没送过。",
        args: {
          message_ref: zod.string().describe("<收件信箱>/<信件编号>，例如 worker/20260101-000000_boss_hi"),
          content: zod.object({
            subject: zod.string().optional().describe("新事由"),
            need: zod.string().optional().describe("新的「需要」"),
            body: zod.string().optional().describe("新正文（字面文本）"),
          }).nullable().describe("要改成的内容（至少一个字段）；null 表示撤回这封信"),
        },
        async execute(args, ctx) {
          const a = args as { message_ref?: unknown; content?: unknown }
          const ref = typeof a.message_ref === "string" ? a.message_ref.trim() : ""
          if (!ref) return "没改：message_ref 必须是 <收件信箱>/<信件编号>。"
          const who = await resolveOwnBox(ctx.sessionID)
          if (!who.box) {
            return `没改：${who.why}。邮局不会替身份不确定的会话改信，请先让本会话在通讯录里对应唯一信箱。`
          }
          if (a.content === null) {
            const m = ref.split("/")
            if (m.length !== 2 || !m[0] || !m[1]) return "没撤回：message_ref 必须写成 <收件信箱>/<信件编号>。"
            const r = await runCLI(["retract", who.box, m[0], m[1]])
            return r.text || "（撤回没有输出）"
          }
          if (typeof a.content !== "object" || Array.isArray(a.content)) {
            return "没改：content 要是一个对象（subject/need/body 至少一个），撤回请传 null。"
          }
          const c = a.content as { subject?: unknown; need?: unknown; body?: unknown }
          const argv = ["edit", "--box", who.box, ref]
          if (typeof c.subject === "string") argv.push("--subject", c.subject)
          if (typeof c.need === "string") argv.push("--need", c.need)
          if (typeof c.body === "string") argv.push("--body", c.body)
          if (argv.length === 4) return "没改：content 里至少要有一个字符串字段（subject / need / body）。"
          const r = await runCLI(argv)
          return r.text || "（更新没有输出）"
        },
      },
      postoffice_archive_current: {
        description:
          "把**本会话当前已经展示过、但还没归档**的消息一键归档到 done/（不读正文、不发信、不叫醒）。" +
          "只处理本会话实际看过的那些消息：之后新到的信不会被碰。keep_unarchived 里列出的编号留在 inbox，" +
          "当作还没完成的事项继续保留。不要在 shell 里自己 mv 信箱文件；工作完成后用这个工具收尾。",
        args: {
          keep_unarchived: zod.array(zod.string()).optional()
            .describe("这些信件编号继续留在 inbox（也不从已展示集合里删）；其余已展示过的归档"),
        },
        async execute(args, ctx) {
          const raw = (args as { keep_unarchived?: unknown }).keep_unarchived
          const keep = Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string") : []
          const who = await resolveOwnBox(ctx.sessionID)
          if (!who.box) {
            return `没归档：${who.why}。邮局只归档本会话自己信箱里已展示过的消息。`
          }
          const argv = ["archive-current", "--box", who.box]
          for (const k of keep) argv.push("--keep", k)
          const r = await runCLI(argv)
          return r.text || "（归档没有输出）"
        },
      },
    },
    dispose: async () => clearInterval(timer),
  }
}
