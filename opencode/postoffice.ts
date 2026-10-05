// agent-postoffice × OpenCode 投递插件
// 盯着 routes.json 里 methods 含 "opencode_plugin" 的信箱；有新信且目标会话空闲时，
// 用 OpenCode 自带 client 给该会话发一条提醒（普通信给路径 + 开头三行；回执只给来源/原事由 +
// 查询命令 + “默认不答复”，不附正文；归档用 postoffice archive-receipt，见 skill）。
// 同一轮正式信在前；没有正式信时把积压回执合并成一条“另有 N 条回执”清单，超过等待时限的回执
// 也会随下一次正式投递带上，绝不被正式信永远挤掉。
// - 只投给属于本 OpenCode 实例目录的会话；多个实例同时运行时用认领文件保证一封信只投一次
// - 离线（status: "offline"）的信箱不投，信留在 inbox，上线后补送
// - 每封只投一次（opencode_delivered.jsonl 账本），重启不重投；每信箱每 10 分钟最多 6 次
// - 失败重试一次，再失败弹通知给人；任何异常都吞掉，绝不影响 OpenCode 本身
// - 不改模型/工具/权限，不新建会话，不批准权限请求
import type { Plugin } from "@opencode-ai/plugin"
import { homedir, platform } from "node:os"
import { readdir, readFile, appendFile, mkdir, open, rm, stat } from "node:fs/promises"
import { existsSync } from "node:fs"
import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import { realpathSync } from "node:fs"

const ROOT = process.env.POSTOFFICE_HOME || `${homedir()}/agent-postoffice`
const LEDGER = `${ROOT}/opencode_delivered.jsonl`
const LOG = `${ROOT}/logs/opencode_plugin.log`
const POLL_MS = 10_000
const RATE_N = 6
const RATE_WIN_MS = 600_000
const MAX_ATTEMPTS = 2
const RECEIPT_WAIT_MS = Number(process.env.POSTOFFICE_RECEIPT_WAIT || 600) * 1000

type Route = { methods?: string[]; session_id?: string; status?: string }

const log = async (msg: string) => {
  try {
    await mkdir(`${ROOT}/logs`, { recursive: true })
    await appendFile(LOG, `${new Date().toISOString()} ${msg}\n`)
  } catch {}
}

const notifyHuman = (text: string) => {
  try {
    const t = text.replace(/"/g, "'").slice(0, 180)
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

// 认领：多个 OpenCode 实例同时运行时，只有抢到认领文件的那个投递
const claim = async (box: string, file: string) => {
  try {
    await mkdir(`${ROOT}/${box}/.claims`, { recursive: true })
    const fh = await open(`${ROOT}/${box}/.claims/${file}`, "wx")
    await fh.close()
    return true
  } catch {
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
        // OpenCode 的 status 表只列非空闲会话：缺席 = 空闲
        let st = "idle"
        try {
          const res = await client.session.status()
          st = (res.data as Record<string, { type?: string }>)?.[sessionID]?.type ?? "idle"
        } catch {
          continue
        }
        if (st !== "idle") continue
        // 读开头块分出正式信与回执通知；正文永不注入
        type Item = { file: string; src: string; subj: string; id: string; mtime: number }
        const formal: string[] = []
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
          if (id) receipts.push({ file, src: fieldOf("来源：").split("（")[0].trim(), subj: receiptSubject(fieldOf), id, mtime })
          else formal.push(file)
        }
        // 有正式信：先送一封；没有正式信：把回执合并成一条。
        // 回执超过 10 分钟就随下一次投递带上，绝不被正式信永远挤掉。
        const letter: string | null = formal.length ? formal[0] : null
        const due = letter === null
          ? receipts
          : receipts.filter((r) => Date.now() - r.mtime > RECEIPT_WAIT_MS)
        if (!letter && !due.length) continue
        const now = Date.now()
        const win = (recent.get(box) ?? []).filter((t) => now - t < RATE_WIN_MS)
        if (win.length >= RATE_N) continue
        // 每次投递前都认领：失败时已释放认领，所以重试照样能拿到；
        // 抢不到说明别的实例正在投这封，跳过（否则多实例会各投一次）
        const group: { file: string; isReceipt: boolean }[] =
          (letter ? [{ file: letter, isReceipt: false }] : [])
            .concat(due.map((r) => ({ file: r.file, isReceipt: true })))
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
        if (claimedFormal.length === 0 && claimedReceipts.length === 1) {
          const r = claimedReceipts[0]
          text = `【联络总站回执｜${box}】来自 ${r.src} 的回执` + (r.subj ? `，原事由：${r.subj}` : "") + `\n` +
                 `查询：postoffice receipt ${shq(box)} ${shq(r.id)}\n默认不答复`
        } else {
          const parts: string[] = []
          for (const f of claimedFormal) {
            parts.push(`【联络总站新信｜${box}】\n== ${ROOT}/${box}/inbox/${f}\n${await head3(`${ROOT}/${box}/inbox/${f}`)}\n` +
              `按信件“需要”字段处理；回信/回执/归档规则见 postoffice skill。`)
          }
          if (claimedReceipts.length) {
            parts.push(`另有 ${claimedReceipts.length} 条回执（默认不答复，需要时按 ID 查询）：\n` +
              claimedReceipts.map((r) => `- ${r.src}：${r.subj}  查询：postoffice receipt ${shq(box)} ${shq(r.id)}`).join("\n"))
          }
          text = parts.join("\n")
        }
        const files2 = claimed.map((g) => g.file).join(",")
        const attempt = (ledger.failed.get(`${box}::${claimed[0].file}`) ?? 0) + 1
        try {
          await client.session.promptAsync({ path: { id: sessionID }, body: { parts: [{ type: "text", text }] } })
          for (const g of claimed) {
            await appendFile(LEDGER, JSON.stringify({ time: new Date().toISOString(), box, file: g.file, session: sessionID, result: "DELIVERED", attempt }) + "\n")
          }
          win.push(now)
          recent.set(box, win)
          await log(`DELIVERED ${box}/${files2} → ${sessionID}（${reason}，第 ${attempt} 次）`)
        } catch (e) {
          const final = attempt >= MAX_ATTEMPTS
          for (const g of claimed) {
            await appendFile(LEDGER, JSON.stringify({ time: new Date().toISOString(), box, file: g.file, session: sessionID, result: final ? "FAILED_FINAL" : "FAILED_RETRYABLE", attempt, detail: String(e).slice(0, 200) }) + "\n")
            // release the claim so a retry (or another instance) can pick the batch up
            try { await rm(`${ROOT}/${box}/.claims/${g.file}`, { force: true }) } catch {}
          }
          await log(`FAILED ${box}/${files2}（第 ${attempt} 次）: ${e}`)
          if (final) notifyHuman(`${box}/${files2} 两次投递失败，请手动转交`)
        }
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
  return {
    event: async ({ event }) => {
      try {
        if ((event as { type?: string })?.type === "session.idle") void scan("session.idle")
      } catch {}
    },
    dispose: async () => clearInterval(timer),
  }
}
