// OpenCode 插件侧的 message-flow 行为测试：批量唤醒（B）、presented 账（C）、原生工具（A/C）。
//
// 范围（全部在 mktemp 的 POSTOFFICE_HOME + mock OpenCode client 里跑，不碰真实邮局）：
//   B) 批量：空闲时把 ≤20 封 eligible 的普通正式信一次 promptAsync；一批只算一次限流；
//      每封分别有 DELIVERED 台账行；被抢走认领的信不出现、其余照常一次唤醒；>20 分轮；
//      闹钟信不混进普通 batch；过久回执仍是尾部合并块（正文永不注入）。
//      单封形态已被 tests/receipt_plugin_test.mjs 钉住，这里不重复。
//   C) presented：<box>/.presented.json 是裸 id 的 union，只有 promptAsync 被接受后才写；
//      postoffice_archive_current 只归档 presented 里的 id，尊重 keep_unarchived，
//      信箱身份只来自 ToolContext session。
//   A) postoffice_message_edit：ref = <recipient>/<id>；身份来自会话；原地更新；
//      content === null 表示 REVOKE（冻结签名是「对象或 null」：对象=UPDATE 字段，null=无需
//      内容的撤回）。CLI 侧行为在 tests/message_flow_test.py。
//   FAILURE) prompt 被接受但记账失败 → 不许自动重投（沿用现有插件语义，这里是批的版本）。
//
// 本文件没有为了可测性而改动任何生产代码；唯一刻意不钉的是 .presented.json 的落盘原子性
// （rename 原子重写属于实现细节，不是可观察契约）。
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, chmod, utimes, cp, symlink, access } from 'node:fs/promises'
import { constants } from 'node:fs'
import { tmpdir, homedir } from 'node:os'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const root = await mkdtemp(join(tmpdir(), 'postoffice-mflow-plugin-'))
process.env.POSTOFFICE_HOME = root
process.env.POSTOFFICE_NO_NOTIFY = '1'

// 插件用官方 @opencode-ai/plugin 的 tool.schema 声明原生工具的 args，Node 从插件文件自己的目录
// 解析这个 specifier。仓库树本身没有 node_modules，所以把插件 stage 成 checkout 形状的临时副本
// （上一层是 postoffice CLI，再上一层是 OpenCode 的依赖树），与 tests/alarm_plugin_test.mjs 同一手法。
// 前置条件：$OPENCODE_NODE_MODULES，否则 ~/.config/opencode/node_modules，否则 ~/.opencode/node_modules。
const nmCandidates = [
  process.env.OPENCODE_NODE_MODULES,
  join(homedir(), '.config/opencode/node_modules'),
  join(homedir(), '.opencode/node_modules'),
].filter(Boolean)
let OPENCODE_NM = null
for (const c of nmCandidates) {
  try {
    await access(join(c, 'zod/package.json'), constants.R_OK)
    await access(join(c, '@opencode-ai/plugin/package.json'), constants.R_OK)
    OPENCODE_NM = c
    break
  } catch {}
}
if (!OPENCODE_NM) {
  console.error(`FAIL - 找不到带 zod 与 @opencode-ai/plugin 的 node_modules，试过：\n  ${nmCandidates.join('\n  ')}\n` +
                `  用 OPENCODE_NODE_MODULES=<path> 指定。`)
  process.exit(1)
}
const staged = join(root, 'checkout')
await mkdir(join(staged, 'opencode'), { recursive: true })
await cp(join(REPO, 'opencode/postoffice.ts'), join(staged, 'opencode/postoffice.ts'))
await cp(join(REPO, 'postoffice'), join(staged, 'postoffice'))
await chmod(join(staged, 'postoffice'), 0o755)
await symlink(OPENCODE_NM, join(staged, 'node_modules'))
process.env.POSTOFFICE_HOME = root
const { PostofficePlugin } = await import(pathToFileURL(join(staged, 'opencode/postoffice.ts')).href)

const FIXED = '你设的闹钟到了，请检查刚才安排的任务。'
const SENTINEL = 'PLUGIN-MFLOW-BODY-71f0'
const LEDGER = join(root, 'opencode_delivered.jsonl')
const DIR_OK = join(root, 'proj')
await mkdir(DIR_OK, { recursive: true })
await writeFile(join(root, 'routes.json'), '{}')

// 一个可控的 mock OpenCode：身份、忙碌状态、投递目标、promptAsync 失败都可改
const state = {
  busy: new Set(),
  prompts: [],
  failNext: false,
  dirs: new Map(),
}
const routes = {}
const saveRoutes = () => writeFile(join(root, 'routes.json'), JSON.stringify(routes))
const addBox = async (name) => {
  const sid = `ses_${name}`
  routes[name] = { methods: ['opencode_plugin'], session_id: sid, status: 'online' }
  state.dirs.set(sid, DIR_OK)
  await mkdir(join(root, name, 'inbox'), { recursive: true })
  await mkdir(join(root, name, 'done'), { recursive: true })
  await saveRoutes()
  return sid
}
const client = {
  session: {
    get: async ({ path }) => {
      const d = state.dirs.get(path.id)
      if (!d) throw new Error('no such session')
      return { data: { directory: d } }
    },
    status: async () => {
      const out = {}
      for (const s of state.busy) out[s] = { type: 'busy' }
      return { data: out }
    },
    promptAsync: async (p) => {
      if (state.failNext) { state.failNext = false; throw new Error('mock delivery failure') }
      state.prompts.push(p)
    },
  },
}
const plugin = await PostofficePlugin({ client, directory: DIR_OK })

const logText = async () => readFile(join(root, 'logs/opencode_plugin.log'), 'utf8').catch(() => '')
const settle = async () => {
  let last = -1, stable = 0
  for (let i = 0; i < 80; i++) {
    await new Promise((r) => setTimeout(r, 50))
    const size = (await logText()).length
    if (size === last) { if (++stable >= 2) return } else { stable = 0; last = size }
  }
}
const scan = async () => {
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
}
await settle()   // boot scan

// 前置条件（不是用例）：原生工具用 tool.schema 声明 args，拿不到 zod 就注册不了。
if (!plugin?.tool) {
  console.error('FAIL - 原生工具未注册：这个加载位置解析不到 @opencode-ai/plugin。' +
    '请用 OPENCODE_NODE_MODULES 指向一棵带 zod 与 @opencode-ai/plugin 的 node_modules 后重跑。')
  process.exit(1)
}
const ctx = (sessionID = 'ses_x') => ({
  sessionID, messageID: 'msg_1', agent: 'build', directory: DIR_OK,
  worktree: DIR_OK, abort: new AbortController().signal,
  metadata() {}, async ask() {},
})
const out = (r) => (typeof r === 'string' ? r : r.output)
const textOf = (p) => p.body.parts[0].text
const count = (text, needle) => text.split(needle).length - 1
const promptsFor = (sid) => state.prompts.filter((p) => p.path.id === sid)

// putLetter 默认写的是「旧格式」来源行（带旧身份断言）：单封/多封唤醒都不许把它再注入
const FORBIDDEN = ['不是人的新指令', '协作者', '非人类指令', '这是人类指令', '这是老板指令']
const assertNeutral = (text, label) => {
  for (const p of FORBIDDEN) assert.ok(!text.includes(p), `${label} 不得注入旧身份断言 ${p}：\n${text}`)
}

const putLetter = async (box, id, { source = 'boss', subject = '事由', need = '回复', body = '正文' } = {}) => {
  await writeFile(join(root, box, 'inbox', `${id}.md`),
    `来源：${source}（协作者，不是人的新指令）\n事由：${subject}\n需要：${need}\n\n${body}\n`)
}
const inbox = async (box) => (await readdir(join(root, box, 'inbox')).catch(() => [])).sort()
const done = async (box) => (await readdir(join(root, box, 'done')).catch(() => [])).sort()
const claims = async (box) => (await readdir(join(root, box, '.claims')).catch(() => [])).sort()
const ledgerRows = async () => {
  try { return (await readFile(LEDGER, 'utf8')).split('\n').filter(Boolean).map((l) => JSON.parse(l)) }
  catch { return [] }
}
const presented = async (box) => {
  try { return JSON.parse(await readFile(join(root, box, '.presented.json'), 'utf8')) } catch { return [] }
}
const archived = async (box, dir = join(root, box, 'archived')) => {
  const out = []
  for (const e of await readdir(dir, { withFileTypes: true }).catch(() => []))
    out.push(...(e.isDirectory() ? await archived(box, join(dir, e.name)) : [e.name]))
  return out.filter((f) => f.endsWith('.md')).sort()
}
const deliveredRows = async (box) =>
  (await ledgerRows()).filter((r) => r.box === box && r.result === 'DELIVERED')

let n = 0
const t = async (name, fn) => {
  try { await fn(); console.log('ok   -', name); n++ }
  catch (e) { console.log('FAIL -', name, '::', e.message); process.exitCode = 1 }
}

// ---------------------------------------------------------------- B：批量
await t('批量：3 封普通信一批一次 prompt，batch 形态，逐封台账/认领', async () => {
  const box = 'flow_a'
  const sid = await addBox(box)
  const ids = ['20260101-100001_boss_甲', '20260101-100002_boss_乙', '20260101-100003_boss_丙']
  await putLetter(box, ids[0], { subject: '甲事', body: '正文甲 ' + SENTINEL })
  await putLetter(box, ids[1], { subject: '乙事', body: '正文乙 ' + SENTINEL })
  await putLetter(box, ids[2], { subject: '丙事', body: '正文丙 ' + SENTINEL })
  const before = promptsFor(sid).length
  await scan()
  const got = promptsFor(sid).slice(before)
  assert.equal(got.length, 1, '一批只发一次 prompt')
  assert.equal(got[0].path.id, sid, '投给本信箱绑定的那个会话')
  const text = textOf(got[0])
  assert.ok(text.includes('【联络总站｜3 封新信】'), '≥2 封用 batch 形态：\n' + text)
  assert.ok(!text.includes('【联络总站新信｜'), '≥2 封不得退回单封形态：\n' + text)
  for (const id of ids) {
    assert.ok(text.includes(`${box}/${id}`), 'batch 行必须带稳定引用：' + id)
    assert.ok(text.includes(join(root, box, 'inbox', `${id}.md`)), 'batch 行必须带该信绝对路径：' + id)
    assert.ok(text.includes('来源：boss'), 'batch 行必须带来源')
  }
  assert.ok(text.includes('事由：甲事') && text.includes('事由：乙事') && text.includes('事由：丙事'),
    'batch 行必须带事由：\n' + text)
  assert.ok(!text.includes(SENTINEL), '正文永不注入')
  assertNeutral(text, '多封唤醒')
  assert.equal(count(text, '按各信'), 1, '公共 tail 只出现一次')
  assert.equal(count(text, 'postoffice skill'), 1)
  assert.deepEqual((await deliveredRows(box)).map((r) => r.file).sort(),
    ids.map((i) => i + '.md').sort(), '每封分别一条 DELIVERED')
  assert.deepEqual(await claims(box), ids.map((i) => i + '.md').sort(), '每封各自认领')
  assert.deepEqual(await inbox(box), ids.map((i) => i + '.md').sort(), '每封仍各自留在 inbox')
})

await t('批量：一批只算一次限流（6 封一批后，第 7 封仍能投）', async () => {
  const box = 'flow_b'
  const sid = await addBox(box)
  const first = Array.from({ length: 6 }, (_, i) => `20260101-11000${i}_boss_批${i}`)
  for (const id of first) await putLetter(box, id, { subject: '批量' })
  const before = promptsFor(sid).length
  await scan()
  assert.equal(promptsFor(sid).length - before, 1, '6 封应一批发出')
  assert.ok(textOf(promptsFor(sid)[before]).includes('【联络总站｜6 封新信】'))
  const last = '20260101-119999_boss_第七封'
  await putLetter(box, last, { subject: '第七封' })
  await scan()
  assert.equal(promptsFor(sid).length - before, 2,
    '一批只该计一次限流；若按封计数，第 7 封会被 6 次窗口挡住')
  assert.ok(textOf(promptsFor(sid)[before + 1]).includes(join(root, box, 'inbox', `${last}.md`)),
    '第 7 封要真的投出去（单封形态给的是绝对路径）')
  assertNeutral(textOf(promptsFor(sid)[before + 1]), '单封唤醒（旧格式来源）')
})

await t('批量：超过 20 封分轮（第一轮 20，下一轮剩下的）', async () => {
  const box = 'flow_c'
  const sid = await addBox(box)
  const ids = Array.from({ length: 21 }, (_, i) => `20260101-12${String(i).padStart(4, '0')}_boss_多${i}`)
  for (const id of ids) await putLetter(box, id, { subject: '多封' })
  const before = promptsFor(sid).length
  await scan()
  await scan()
  await scan()
  const got = promptsFor(sid).slice(before)
  assert.equal(got.length, 2, '21 封应分两轮')
  const t1 = textOf(got[0])
  const t2 = textOf(got[1])
  assert.equal((t1.match(/^\d+\. /gm) || []).length, 20, '第一轮恰好 20 封的编号行：\n' + t1)
  assert.ok(!t1.includes(ids[20]), '第一轮不该带上第 21 封')
  assert.equal((t2.match(/^\d+\. /gm) || []).length, 0, '只剩 1 封时回到单封形态')
  assert.ok(t2.includes(`【联络总站新信｜${box}】`), '单封保持现有形态：\n' + t2)
  assertNeutral(t2, '单封唤醒（旧格式来源）')
  assert.ok(t2.includes(ids[20]))
})

await t('批量：被抢走的信不出现、其余一批；下一轮不重复已投', async () => {
  const box = 'flow_d'
  const sid = await addBox(box)
  const [a, b, c] = ['20260101-130001_boss_留A', '20260101-130002_boss_抢B', '20260101-130003_boss_留C']
  for (const id of [a, b, c]) await putLetter(box, id, { subject: id.slice(-2) })
  await mkdir(join(root, box, '.claims'), { recursive: true })
  await writeFile(join(root, box, '.claims', `${b}.md`), '99999\n')
  const before = promptsFor(sid).length
  await scan()
  assert.equal(promptsFor(sid).length - before, 1, '抢不到的那封不投，其余一次唤醒')
  const text = textOf(promptsFor(sid)[before])
  assert.ok(text.includes(`${box}/${a}`) && text.includes(`${box}/${c}`))
  assert.ok(!text.includes(`${box}/${b}`), '被抢走的信不得出现在这一批里')
  assert.deepEqual((await deliveredRows(box)).map((r) => r.file).sort(), [a + '.md', c + '.md'])
  assert.equal(await readFile(join(root, box, '.claims', `${b}.md`), 'utf8'), '99999\n',
    '别人的认领不许被动')
  await rm(join(root, box, '.claims', `${b}.md`), { force: true })
  await scan()
  assert.equal(promptsFor(sid).length - before, 2, '下一轮补上抢不到的那封')
  const text2 = textOf(promptsFor(sid)[before + 1])
  assert.ok(text2.includes(join(root, box, 'inbox', `${b}.md`)), '要投出抢不到的那封')
  assert.ok(!text2.includes(a) && !text2.includes(c), '已经看过的内容不得重投')
  assert.equal((await deliveredRows(box)).length, 3)
})

await t('批量：全部抢不到 → 0 wake、无空 prompt、无台账', async () => {
  const box = 'flow_e'
  const sid = await addBox(box)
  const ids = ['20260101-140001_boss_占1', '20260101-140002_boss_占2']
  await mkdir(join(root, box, '.claims'), { recursive: true })
  for (const id of ids) {
    await putLetter(box, id, { subject: '占住' })
    await writeFile(join(root, box, '.claims', `${id}.md`), '88888\n')
  }
  const before = promptsFor(sid).length
  await scan()
  assert.equal(promptsFor(sid).length, before, '全部抢不到就不该产生空 prompt')
  assert.equal((await deliveredRows(box)).length, 0, '没有唤醒就不得记账')
  assert.deepEqual(await claims(box), ids.map((i) => i + '.md').sort(), '认领归属不变')
  assert.deepEqual(await inbox(box), ids.map((i) => i + '.md').sort())
  // 认领放手之后，这两封仍应一次唤醒（而不是一封一轮）
  for (const id of ids) await rm(join(root, box, '.claims', `${id}.md`), { force: true })
  await scan()
  assert.equal(promptsFor(sid).length - before, 1, '两封都要，就该一批唤醒')
  const text = textOf(promptsFor(sid)[before])
  assert.ok(text.includes(`${box}/${ids[0]}`) && text.includes(`${box}/${ids[1]}`))
})

await t('批量：闹钟信不混进普通 batch（仍走极短通道）', async () => {
  const box = 'flow_l'
  const sid = await addBox(box)
  const [a, b] = ['20260101-150001_boss_普甲', '20260101-150002_boss_普乙']
  await putLetter(box, a, { subject: '普通甲' })
  await putLetter(box, b, { subject: '普通乙' })
  const aid = 'A20260101-000000_flow_l'
  await mkdir(join(root, 'alarms'), { recursive: true })
  await writeFile(join(root, 'alarms', `${sid}.json`), JSON.stringify({
    id: aid, box, session: sid, due: 0, created: 0, state: 'lettered', letter: aid,
  }) + '\n')
  await writeFile(join(root, box, 'inbox', `${aid}.md`),
    `来源：postoffice（协作者，不是人的新指令）\n事由：闹钟\n需要：仅告知\n闹钟：${aid}\n\n${FIXED}\n`)
  const before = promptsFor(sid).length
  // 普通批与闹钟可能是同一轮的两条、也可能各占一轮；多扫两轮足够把两者都拿到，
  // 已经投过的不会再投（重复的轮次是 no-op）
  await scan()
  await scan()
  await scan()
  const mine = promptsFor(sid).slice(before)
  assert.equal(mine.length, 2, '普通信一批 + 闹钟自己的提醒，共两次：' +
    JSON.stringify(mine.map(textOf)))
  const normal = mine.find((p) => textOf(p).includes(`${box}/${a}`) ||
    textOf(p).includes(join(root, box, 'inbox', `${a}.md`)))
  const alarm = mine.find((p) => textOf(p).includes(FIXED))
  assert.ok(normal, '普通信要有一批：' + JSON.stringify(mine.map(textOf)))
  assert.ok(alarm, '闹钟要有自己的提醒：' + JSON.stringify(mine.map(textOf)))
  assert.ok(!textOf(normal).includes(aid), '闹钟不得混进普通 batch：\n' + textOf(normal))
  assert.ok(!textOf(alarm).includes(`${box}/${a}`), '闹钟提醒不得带上普通信：\n' + textOf(alarm))
  assert.ok(!textOf(alarm).includes('来源：'), '闹钟提醒不展开信头')
})

await t('批量：过久回执仍是尾部合并块，正文不注入', async () => {
  const box = 'flow_m'
  const sid = await addBox(box)
  const [a, b] = ['20260101-160001_boss_回甲', '20260101-160002_boss_回乙']
  await putLetter(box, a, { subject: '回执尾甲' })
  await putLetter(box, b, { subject: '回执尾乙' })
  const rid = '20260101-160003_ghost_回执'
  const rp = join(root, box, 'inbox', `${rid}.md`)
  await writeFile(rp, `来源：ghost\n事由：回执：旧事\n需要：回执（默认不答复）\n` +
    `回执：orig-77\n原事由：旧事\n\n${SENTINEL}\n`)
  const old = new Date(Date.now() - 20 * 60 * 1000)
  await utimes(rp, old, old)
  const before = promptsFor(sid).length
  await scan()
  assert.equal(promptsFor(sid).length - before, 1)
  const text = textOf(promptsFor(sid)[before])
  assert.ok(text.includes('【联络总站｜2 封新信】'))
  assert.ok(text.includes('另有 1 条回执'), '回执仍是尾部合并块：\n' + text)
  assert.ok(!text.includes(SENTINEL), '回执正文永不注入')
  assert.ok(text.includes(`postoffice receipt '${box}' 'orig-77'`), '合并块带查询命令')
})

// ---------------------------------------------------------------- C：presented
await t('presented：被接受后才写、union、只存裸 id', async () => {
  const box = 'flow_f'
  await addBox(box)
  await writeFile(join(root, box, '.presented.json'), JSON.stringify(['stale-old']))
  const ids = ['20260101-170001_boss_展甲', '20260101-170002_boss_展乙']
  for (const id of ids) await putLetter(box, id, { subject: '已展示' })
  await scan()
  const p = await presented(box)
  assert.ok(Array.isArray(p), 'presented 应是 id 数组：' + JSON.stringify(p))
  for (const id of ids) assert.ok(p.includes(id), '被接受的 wake 必须记 presented：' + id)
  assert.ok(p.includes('stale-old'), 'union：不覆盖仍保留的旧事项')
  for (const x of p)
    assert.ok(typeof x === 'string' && !x.includes('/') && !x.endsWith('.md'), '只存裸 id：' + x)
  const d = '20260101-170003_boss_失败那封'
  await putLetter(box, d, { subject: '失败' })
  state.failNext = true
  await scan()
  assert.ok(!(await presented(box)).includes(d), 'promptAsync 没被接受就不算展示过')
  await scan()
  assert.ok((await presented(box)).includes(d), '重试成功后才记 presented')
})

await t('archive 工具：只归档 presented、keep 保留、身份来自会话', async () => {
  const box = 'flow_g'
  const sid = await addBox(box)
  const ids = ['20260101-180001_boss_存甲', '20260101-180002_boss_存乙', '20260101-180003_boss_存丙']
  for (const id of ids) await putLetter(box, id, { subject: '待归档' })
  await scan()
  const pre = await presented(box)
  for (const id of ids) assert.ok(pre.includes(id), '前置：presented 已写（见上一条）')
  const d = '20260101-180004_boss_后到'
  await putLetter(box, d, { subject: '没展示过' })

  const tool = plugin.tool.postoffice_archive_current
  assert.ok(tool, 'postoffice_archive_current 应注册')
  assert.deepEqual(Object.keys(tool.args), ['keep_unarchived'], 'args 只能是 keep_unarchived')
  const r = await out(await tool.execute({ keep_unarchived: [ids[1]] }, ctx(sid)))
  assert.ok(typeof r === 'string' && r.length > 0, '工具要有结果说明：' + JSON.stringify(r))
  assert.deepEqual(await inbox(box), [ids[1] + '.md', d + '.md'].sort(),
    'keep 的与没展示过的必须留在 inbox')
  assert.deepEqual(await done(box), [ids[0] + '.md', ids[2] + '.md'].sort(), '只有 presented 进 done')
  const p = await presented(box)
  assert.ok(p.includes(ids[1]), 'keep 的仍在 presented')
  assert.ok(!p.includes(ids[0]) && !p.includes(ids[2]), '成功归档的移出 presented')
  assert.ok(!p.includes(d), '没展示过的不得进 presented')

  await out(await tool.execute({ keep_unarchived: [ids[1]] }, ctx(sid)))
  assert.deepEqual(await inbox(box), [ids[1] + '.md', d + '.md'].sort(), '重复调用幂等')

  const sid2 = await addBox('flow_g2')
  await out(await tool.execute({}, ctx(sid2)))
  assert.ok((await inbox(box)).includes(ids[1] + '.md'), '别的会话的工具调用不得动本箱')
  assert.ok((await inbox('flow_g2')).length === 0)
})

await t('archive 工具：按精确编号归档本箱信、幂等、拒绝路径/glob/跨箱', async () => {
  const box = 'flow_arch'
  const sid = await addBox(box)
  const ids = ['20260101-190001_boss_精甲', '20260101-190002_boss_精乙']
  for (const id of ids) await putLetter(box, id, { subject: '精确归档' })

  const tool = plugin.tool.postoffice_archive
  assert.ok(tool, 'postoffice_archive 应注册')
  assert.deepEqual(Object.keys(tool.args), ['letter_id'], 'args 只能是 letter_id')
  const r = await out(await tool.execute({ letter_id: ids[0] }, ctx(sid)))
  assert.ok(typeof r === 'string' && r.length > 0, '工具要有结果说明：' + JSON.stringify(r))
  assert.deepEqual(await done(box), [ids[0] + '.md'], '精确编号进 done')
  assert.ok((await inbox(box)).includes(ids[1] + '.md'), '同箱别的信不动')

  const r2 = await out(await tool.execute({ letter_id: ids[0] }, ctx(sid)))
  assert.ok(typeof r2 === 'string' && r2.length > 0, '重复调用有结果（幂等）')
  assert.deepEqual(await done(box), [ids[0] + '.md'], '重复调用不产生第二份')

  await out(await tool.execute({ letter_id: '../evil' }, ctx(sid)))
  await out(await tool.execute({ letter_id: '*.md' }, ctx(sid)))
  assert.ok((await inbox(box)).includes(ids[1] + '.md'), '路径/glob 不得归档，原信仍在')

  const sid2 = await addBox('flow_arch2')
  await out(await tool.execute({ letter_id: ids[1] }, ctx(sid2)))
  assert.ok((await inbox(box)).includes(ids[1] + '.md'), '别的会话不得按本箱编号归档')
})

// ---------------------------------------------------------------- A：message_edit
await t('message_edit：按会话身份原地改自己的信', async () => {
  const sender = 'flow_h'
  const target = 'flow_h2'
  const sid = await addBox(sender)
  await addBox(target)
  const id = '20260101-200001_flow_h_原始'
  await putLetter(target, id, { source: sender, subject: '原始事由', need: '回复', body: '旧正文' })
  const tool = plugin.tool.postoffice_message_edit
  assert.ok(tool, 'postoffice_message_edit 应注册')
  assert.deepEqual(Object.keys(tool.args).sort(), ['content', 'message_ref'], 'args 形状冻结')
  const r = await out(await tool.execute({
    message_ref: `${target}/${id}`,
    content: { subject: '新事由', need: '仅告知', body: '新正文' },
  }, ctx(sid)))
  assert.ok(r.includes('已更新'), '成功要报告已更新：' + r)
  assert.ok(r.includes('尚未收到'), '成功要说明收件方尚未收到旧版本：' + r)
  const text = await readFile(join(root, target, 'inbox', `${id}.md`), 'utf8')
  assert.ok(text.includes('事由：新事由') && text.includes('需要：仅告知') && text.includes('新正文'))
  assert.ok(text.includes(`来源：${sender}`), '来源不变')
  assert.ok(!text.includes('旧正文') && !text.includes('原始事由'))
})

await t('message_edit：拒绝冒名改别人的信，原信一字节不动', async () => {
  const sender = 'flow_i1'
  const target = 'flow_i2'
  const sid = await addBox(sender)
  await addBox(target)
  const id = '20260101-210001_other_别人的信'
  await putLetter(target, id, { source: 'other', subject: '别人的事', need: '回复', body: '不许动' })
  const tool = plugin.tool.postoffice_message_edit
  assert.ok(tool, 'postoffice_message_edit 应注册')
  const p = join(root, target, 'inbox', `${id}.md`)
  const before = await readFile(p, 'utf8')
  const r = await out(await tool.execute({
    message_ref: `${target}/${id}`, content: { subject: '冒名' },
  }, ctx(sid)))
  assert.ok(!r.includes('已更新'), '身份不符必须拒绝：' + r)
  assert.equal(await readFile(p, 'utf8'), before, '拒绝时一个字节不改')
  assert.ok((await inbox(target)).includes(`${id}.md`))
})

await t('message_edit：content 为 null 表示撤回（REVOKE）', async () => {
  const sender = 'flow_j1'
  const target = 'flow_j2'
  const sid = await addBox(sender)
  await addBox(target)
  const id = '20260101-220001_flow_j1_要撤回'
  await putLetter(target, id, { source: sender, subject: '要撤回', need: '回复', body: '撤回我' })
  const tool = plugin.tool.postoffice_message_edit
  assert.ok(tool, 'postoffice_message_edit 应注册')
  const r = await out(await tool.execute({ message_ref: `${target}/${id}`, content: null }, ctx(sid)))
  assert.ok(r.includes('已撤回'), 'REVOKE 要报告已撤回：' + r)
  assert.ok(r.includes('不会再'), 'REVOKE 要说不会再因这封信被唤醒：' + r)
  assert.ok(!(await inbox(target)).includes(`${id}.md`), '撤回后不在 inbox')
  assert.ok((await archived(target)).includes(`${id}.md`), '原信进 archived')
})

// ---------------------------------------------------------------- FAILURE
await t('FAILURE：prompt 被接受但记账失败，不许自动重投（批）', async () => {
  const box = 'flow_k'
  const sid = await addBox(box)
  const ids = ['20260101-230001_boss_记甲', '20260101-230002_boss_记乙']
  for (const id of ids) await putLetter(box, id, { subject: '记账失败' })
  await writeFile(LEDGER, await readFile(LEDGER, 'utf8').catch(() => ''))
  await chmod(LEDGER, 0o444)
  try {
    const before = promptsFor(sid).length
    await scan()
    assert.equal(promptsFor(sid).length - before, 1, 'prompt 应被接受一次')
    const text = textOf(promptsFor(sid)[before])
    assert.ok(text.includes(`${box}/${ids[0]}`) && text.includes(`${box}/${ids[1]}`),
      '两封应同批发出：\n' + text)
    assert.equal((await ledgerRows()).filter((r) => r.box === box).length, 0, '账本没写成')
    assert.deepEqual(await claims(box), ids.map((i) => i + '.md').sort(), '认领必须保留')
    await scan()
    assert.equal(promptsFor(sid).length - before, 1, '记账失败不得自动重投')
    assert.deepEqual(await claims(box), ids.map((i) => i + '.md').sort())
    assert.match(await logText(), /记账失败/)
  } finally {
    await chmod(LEDGER, 0o644)
  }
})

await plugin.dispose()
await rm(root, { recursive: true, force: true })
console.log(process.exitCode ? 'FAIL' : `PASS (${n} checks)`)
