// v1.13 runtime activity（OpenCode 插件侧，纯presentation）行为测试。
//
// 冻结契约（全部在 mktemp 的 POSTOFFICE_HOME + mock OpenCode client 里跑）：
//   - plugin.event({event:{type:"session.status", properties:{sessionID:<own>, status:{type:"busy"}}}})
//     → <home>/runtime/activity/<box>.json 出现，state=="working"，source=="opencode_plugin"，
//       binding == routes 里该箱的 session_id。
//   - 紧接着 status:{type:"idle"} → state=="idle" 且 since 重置（更大）。
//   - 同状态连发两次 → since 不变（observed 可前进）。
//   - 属于其他会话的 event → 不给任何别的信箱写 activity 文件。
//   - plugin.event({event:{type:"session.idle", properties:{sessionID:<own>}}}) → idle。
//
// 本文件写于实现之前，预期 RED（功能缺失）；只钉冻结的可观察行为，不钉实现。
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, cp, symlink, access } from 'node:fs/promises'
import { constants } from 'node:fs'
import { tmpdir, homedir } from 'node:os'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const root = await mkdtemp(join(tmpdir(), 'postoffice-activity-plugin-'))
process.env.POSTOFFICE_HOME = root
process.env.POSTOFFICE_NO_NOTIFY = '1'

// 与 tests/message_flow_plugin_test.mjs 同一 staging 手法：把 opencode/postoffice.ts + postoffice
// stage 成 checkout 形状，再 symlink 一棵带 zod 与 @opencode-ai/plugin 的 node_modules。
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
await symlink(OPENCODE_NM, join(staged, 'node_modules'))
const { PostofficePlugin } = await import(pathToFileURL(join(staged, 'opencode/postoffice.ts')).href)

const DIR_OK = join(root, 'proj')
await mkdir(DIR_OK, { recursive: true })
const BOX = 'actbox'
const OWN_SID = 'ses_act'
const SECOND_SID = 'ses_act2'
const THIRD_SID = 'ses_act3'
const FOREIGN_SID = 'ses_foreign'
await mkdir(join(root, BOX, 'inbox'), { recursive: true })
await mkdir(join(root, BOX, 'done'), { recursive: true })
await writeFile(join(root, 'routes.json'), JSON.stringify({
  [BOX]: { methods: ['opencode_plugin'], session_id: OWN_SID, status: 'online' },
}))

const state = { prompts: [], ctxPrompts: [] }
const client = {
  session: {
    get: async ({ path }) => {
      if (path.id === OWN_SID || path.id === SECOND_SID || path.id === THIRD_SID) return { data: { directory: DIR_OK } }
      throw new Error('no such session')
    },
    status: async () => ({ data: {} }),
    promptAsync: async (p) => { state.prompts.push(p); return { data: {} } },
    prompt: async (p) => { state.ctxPrompts.push(p); return { data: {} } },
  },
}

const plugin = await PostofficePlugin({ client, directory: DIR_OK })

const activityPath = (box = BOX) => join(root, 'runtime', 'activity', `${box}.json`)
const readActivity = async (box = BOX) => {
  try { return JSON.parse(await readFile(activityPath(box), 'utf8')) } catch { return null }
}
const activityBoxes = async () => {
  try { return (await readdir(join(root, 'runtime', 'activity'))).filter((f) => f.endsWith('.json')).sort() }
  catch { return [] }
}
const settle = async (ms = 150) => new Promise((r) => setTimeout(r, ms))
const statusEvent = (sid, kind) => ({
  event: { type: 'session.status', properties: { sessionID: sid, status: { type: kind } } },
})
const idleEvent = (sid) => ({ event: { type: 'session.idle', properties: { sessionID: sid } } })

let n = 0
const t = async (name, fn) => {
  try { await fn(); console.log('ok   -', name); n++ }
  catch (e) { console.log('FAIL -', name, '::', e.message); process.exitCode = 1 }
}

await t('票1 atomic-write：真入口并发 busy/idle 不自撞 tmp，终态=最后事件，无残留', async () => {
  // 修前红（RED_EVIDENCE_ATOMIC_WRITE.md RED-C）：同名 tmp-{pid} 自撞 → 19/20 写入失败 ENOENT。
  // 修后：per-box 队列按收到顺序串行落盘；busy/idle 交替绕开 60s 同状态节流。
  const evs = []
  for (let i = 0; i < 10; i++) {
    evs.push(plugin.event(statusEvent(OWN_SID, 'busy')))
    evs.push(plugin.event(idleEvent(OWN_SID)))
  }
  await Promise.allSettled(evs)
  await settle(300)
  const logText = await readFile(join(root, 'logs/opencode_plugin.log'), 'utf8').catch(() => '')
  assert.doesNotMatch(logText, /activity 写入失败/, '并发下不得再有 activity 写入失败')
  const rec = await readActivity()
  assert.equal(rec.state, 'idle', `终态=最后入队事件（idle）：${JSON.stringify(rec)}`)
  assert.equal(rec.binding, OWN_SID)
  const files = await readdir(join(root, 'runtime', 'activity'))
  assert.ok(!files.some((f) => f.includes('.tmp-')), `不留 tmp：${files}`)
})

await t('session.status busy → working / opencode_plugin / binding=session_id', async () => {
  await plugin.event(statusEvent(OWN_SID, 'busy'))
  await settle()
  const act = await readActivity()
  assert.ok(act, '必须写出 runtime/activity/<box>.json')
  assert.deepEqual(Object.keys(act).sort(), ['binding', 'observed', 'since', 'source', 'state'].sort(),
    '文件键集合被冻结：' + JSON.stringify(act))
  assert.equal(act.state, 'working')
  assert.equal(act.source, 'opencode_plugin')
  assert.equal(act.binding, OWN_SID, 'binding 必须是本箱的 routes session_id')
  assert.equal(typeof act.since, 'number')
  assert.equal(typeof act.observed, 'number')
  return act.since
})

await t('session.status idle → idle 且 since 重置（更大）', async () => {
  await plugin.event(statusEvent(OWN_SID, 'busy'))
  await settle()
  const a = await readActivity()
  assert.ok(a, '前置：busy 已写文件')
  await settle(60)
  await plugin.event(statusEvent(OWN_SID, 'idle'))
  await settle()
  const b = await readActivity()
  assert.equal(b.state, 'idle', JSON.stringify(b))
  assert.ok(b.since > a.since, `状态改变 since 必须重置更大：${a.since} → ${b.since}`)
})

await t('同状态连发两次 → since 不变', async () => {
  await plugin.event(statusEvent(OWN_SID, 'busy'))
  await settle()
  const a = await readActivity()
  await settle(60)
  await plugin.event(statusEvent(OWN_SID, 'busy'))
  await settle()
  const b = await readActivity()
  assert.equal(b.state, 'working')
  assert.equal(b.since, a.since, '同状态：since 不许动')
  assert.ok(b.observed >= a.observed, 'observed 只许前进')
})

await t('别的会话的 event → 不给任何信箱写 activity', async () => {
  const before = await activityBoxes()
  await plugin.event(statusEvent(FOREIGN_SID, 'busy'))
  await plugin.event(idleEvent(FOREIGN_SID))
  await settle()
  const after = await activityBoxes()
  assert.deepEqual(after, before, '不认识的会话不得写出任何 activity 文件：' + JSON.stringify(after))
})

await t('session.idle（自己的会话）→ idle', async () => {
  await plugin.event(statusEvent(OWN_SID, 'busy'))
  await settle()
  await plugin.event(idleEvent(OWN_SID))
  await settle()
  const act = await readActivity()
  assert.ok(act, 'idle 事件后文件必须存在')
  assert.equal(act.state, 'idle', JSON.stringify(act))
})

await t('换绑定：同状态也立即写、since 重置、binding 更新（不被旧绑定的节流挡住）', async () => {
  await plugin.event(statusEvent(OWN_SID, 'busy'))
  await settle()
  const a = await readActivity()
  assert.equal(a.binding, OWN_SID, '前置：绑定是旧会话')
  // 信箱改绑到新会话：routes 的 session_id 换人
  await writeFile(join(root, 'routes.json'), JSON.stringify({
    [BOX]: { methods: ['opencode_plugin'], session_id: SECOND_SID, status: 'online' },
  }))
  await settle(60)
  await plugin.event(statusEvent(SECOND_SID, 'busy'))
  await settle()
  const b = await readActivity()
  assert.equal(b.state, 'working')
  assert.equal(b.binding, SECOND_SID, 'binding 必须换成新会话')
  assert.ok(b.since > a.since, `换绑定不许继承旧绑定的 since：${a.since} → ${b.since}`)
})

await t('恢复接缝：官方仅上下文入口注入一次身份提示，busy 不注入、不请求答复', async () => {
  await writeFile(join(root, 'routes.json'), JSON.stringify({
    [BOX]: { methods: ['opencode_plugin'], session_id: THIRD_SID, status: 'online' },
  }))
  await writeFile(join(root, 'config.json'), JSON.stringify({
    version: 2, groups: {}, aliases: {
      'a.owner': { candidates: [BOX], role: { scope: { kind: 'company', id: 'co' }, title: '主持' } } },
    organization: { companies: { co: { name: 'Co', rules_file: '/tmp/rules.md', members: [BOX] } }, projects: {} },
  }))
  const beforeCtx = state.ctxPrompts.length
  const beforeAsk = state.prompts.length
  // busy 事件是「正在忙」，绝不在这里叫起一轮新的模型回答
  await plugin.event(statusEvent(THIRD_SID, 'busy'))
  await settle(300)
  assert.equal(state.ctxPrompts.length, beforeCtx, 'busy 事件不注入身份提示')
  assert.equal(state.prompts.length, beforeAsk, 'busy 事件不请求模型答复')
  await plugin.event(statusEvent(THIRD_SID, 'idle'))
  await settle(300)
  const added = state.ctxPrompts.slice(beforeCtx)
  assert.equal(added.length, 1, '空闲接缝注入恰好一次提示：' + JSON.stringify(added))
  assert.ok(added[0].body.parts[0].text.includes(`身份（${BOX}）`), '提示含短身份行')
  assert.equal(added[0].body.noReply, true, '官方仅上下文入口：body.noReply=true，不请求模型答复')
  assert.equal(state.prompts.length, beforeAsk, '恢复提示绝不走会请求答复的 promptAsync')
  await plugin.event(statusEvent(THIRD_SID, 'busy'))
  await plugin.event(statusEvent(THIRD_SID, 'idle'))
  await settle(300)
  assert.equal(state.ctxPrompts.length, beforeCtx + 1, '同一会话不再重复注入')
})

await t('终检②：归属查询慢不翻转到达顺序（busy→idle 乱序复现，终态=idle）', async () => {
  // 真实公共 event 入口：busy 事件的归属查询慢 150ms、idle 事件 0ms——顺序必须按「到达」登记，
  // 终态=最后到达的事件（f0feb89 曾把终态翻转成 working）。先恢复 BOX→OWN_SID 绑定
  //（上一例把它改成了 THIRD_SID）。
  await writeFile(join(root, 'routes.json'), JSON.stringify({
    [BOX]: { methods: ['opencode_plugin'], session_id: OWN_SID, status: 'online' },
  }))
  await settle(60)
  let gets = 0
  const slow = {
    ...client,
    session: {
      ...client.session,
      get: async ({ path }) => {
        if (path.id !== OWN_SID) throw new Error('no such session')
        gets += 1
        if (gets === 1) await new Promise((r) => setTimeout(r, 150))   // 首查（busy）慢
        return { data: { directory: DIR_OK } }
      },
    },
  }
  const p2 = await PostofficePlugin({ client: slow, directory: DIR_OK })
  // 并发发射：busy 起跑但不等（它的归属查询要 150ms），idle 先处理完——
  // 只有真并发才暴露「先到后被查询延迟反超」的乱序；逐个 await 的话旧代码也顺序成立。
  const busyRun = p2.event(statusEvent(OWN_SID, 'busy'))
  await p2.event(idleEvent(OWN_SID))
  await busyRun
  await settle(400)
  const act = await readActivity()
  assert.ok(act, '活动文件必须存在')
  assert.equal(act.state, 'idle',
    `终态必须是最后到达的事件（busy 的慢归属查询不得翻转顺序）：${JSON.stringify(act)}`)
  assert.equal(act.binding, OWN_SID, JSON.stringify(act))
  await p2.dispose()
})

// 窄复检 RETURN（1335d27）：身份 hint 曾在活动链内 await prompt——挂起的 hint 拖住整条链，
// 后到事件（跨箱也一样）的 presence 被扣住。合同：链只串「归属核验+presence」，hint 在链外。
await t('窄复检：pending 的身份 hint 不挡后续活动更新', async () => {
  await writeFile(join(root, 'routes.json'), JSON.stringify({
    [BOX]: { methods: ['opencode_plugin'], session_id: OWN_SID, status: 'online' },
  }))
  await settle(60)
  let releaseHint
  const gate = new Promise((r) => { releaseHint = r })
  let hintStarted = false
  const stuck = {
    ...client,
    session: {
      ...client.session,
      prompt: async (p) => { hintStarted = true; state.ctxPrompts.push(p); await gate; return { data: {} } },
    },
  }
  const p3 = await PostofficePlugin({ client: stuck, directory: DIR_OK })
  void p3.event(idleEvent(OWN_SID))            // 首次 idle：触发 hint，prompt 挂起不返回
  await settle(150)
  assert.ok(hintStarted, '夹具：hint 已走到 prompt 并挂起')
  try {
    void p3.event(statusEvent(OWN_SID, 'busy')) // 后到事件：不 await——旧代码它会挂在链内 gate 上
    await settle(150)
    const act = await readActivity()
    assert.equal(act?.state, 'working',
      `挂起的 hint 不得挡住后续活动更新：${JSON.stringify(act)}`)
  } finally {
    releaseHint()                              // 旧代码会让 event() 挂在链内 prompt 上：必须放行才能收尾
    await settle(50)
    await p3.dispose()
  }
})

await plugin.dispose()
await rm(root, { recursive: true, force: true })
console.log(process.exitCode ? 'FAIL' : `PASS (${n} checks)`)
