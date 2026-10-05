// OpenCode plugin side of the session self-set one-shot alarm.
//
// Two things are exercised here, both without a real model or a real mailbox:
//   1. the two tools the model may call — postoffice_alarm_schedule / postoffice_alarm_cancel —
//      which must bind to the *calling* session only, refuse uncertain identities, and never
//      touch another session's record;
//   2. delivery of the reminder letter the postman wrote — a new short round, no letter head,
//      no path, no body — reusing the existing idle channel, ledger and claims.
//
// Everything runs in a temp POSTOFFICE_HOME with a mocked OpenCode client.
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, readFile, readdir, rename, chmod, utimes } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const root = await mkdtemp(join(tmpdir(), 'postoffice-alarm-plugin-'))
process.env.POSTOFFICE_HOME = root
process.env.POSTOFFICE_NO_NOTIFY = '1'
const { PostofficePlugin } = await import('../opencode/postoffice.ts')
const FIXED = '你设的闹钟到了，请检查刚才安排的任务。'
const LIVE = 'ses_live_alarm'
const LIVE2 = 'ses_live_alarm_2'
const OTHER = 'ses_someone_else'
const DIR_OK = join(root, 'proj')

const logText = async () => readFile(join(root, 'logs/opencode_plugin.log'), 'utf8').catch(() => '')
const settle = async () => {
  let last = -1, stable = 0
  for (let i = 0; i < 80; i++) {
    await new Promise((r) => setTimeout(r, 50))
    const size = (await logText()).length
    if (size === last) { if (++stable >= 2) return } else { stable = 0; last = size }
  }
}
const alarmFile = (sid) => join(root, 'alarms', `${sid}.json`)
const readAlarm = async (sid) => JSON.parse(await readFile(alarmFile(sid), 'utf8'))
const alarmExists = (sid) => existsSync(alarmFile(sid))
const alarmFiles = async () => (await readdir(join(root, 'alarms')).catch(() => [])).sort()
const inbox = async (box) => (await readdir(join(root, box, 'inbox')).catch(() => [])).sort()

// 一个可控的 mock OpenCode：身份、忙碌状态、投递目标都可改
const state = {
  routes: {
    lab: { methods: ['opencode_plugin'], session_id: LIVE, status: 'online' },
    // 取消用例专用：独立信箱 = 独立的限流窗口，避免被前面用例的投递次数卡住
    lab2: { methods: ['opencode_plugin'], session_id: LIVE2, status: 'online' },
    other: { methods: ['opencode_plugin'], session_id: OTHER, status: 'online' },
    quiet: { methods: ['notify'], session_id: 'ses_notify_only', status: 'online' },
  },
  busy: new Set(),
  prompts: [],
  failNext: false,
  sessionDirs: { [LIVE]: DIR_OK, [LIVE2]: DIR_OK, [OTHER]: DIR_OK, ses_notify_only: DIR_OK },
  getFails: false,
}
const saveRoutes = async () => writeFile(join(root, 'routes.json'), JSON.stringify(state.routes))
const client = {
  session: {
    get: async ({ path }) => {
      if (state.getFails) throw new Error('mock session.get failure')
      const d = state.sessionDirs[path.id]
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
await mkdir(join(root, 'lab/inbox'), { recursive: true })
await mkdir(join(root, 'lab2/inbox'), { recursive: true })
await mkdir(join(root, 'other/inbox'), { recursive: true })
await mkdir(join(root, 'alarms'), { recursive: true })
await saveRoutes()

const plugin = await PostofficePlugin({ client, directory: DIR_OK })
const ctx = (sessionID = LIVE) => ({
  sessionID, messageID: 'msg_1', agent: 'build', directory: DIR_OK,
  worktree: DIR_OK, abort: new AbortController().signal,
  metadata() {}, async ask() {},
})
const schedule = (args, c = ctx()) => plugin.tool.postoffice_alarm_schedule.execute(args, c)
const cancel = (c = ctx()) => plugin.tool.postoffice_alarm_cancel.execute({}, c)
const out = async (r) => (typeof r === 'string' ? r : r.output)
let n = 0
const t = async (name, fn) => {
  try { await fn(); console.log('ok   -', name); n++ } catch (e) { console.log('FAIL -', name, '::', e.message); process.exitCode = 1 }
}

// ---------------------------------------------------------------- 工具：入口
await t('schedule 建一条只属于本会话的 pending', async () => {
  const r = await schedule({ delay_minutes: 25 }, ctx(LIVE))
  assert.ok(alarmExists(LIVE), '本会话应有自己的闹钟记录')
  assert.ok(!alarmExists(OTHER), '绝不能替别的会话建闹钟')
  assert.equal((await alarmFiles()).length, 1, '只应有一条记录')
  const a = await readAlarm(LIVE)
  assert.equal(a.box, 'lab', '绑定到本会话自己的物理信箱')
  assert.equal(a.session, LIVE)
  assert.equal(a.state, 'pending')
  assert.match(a.id, /^A\d{8}-\d{6}_lab$/, '闹钟 id 要能当信的文件名用')
  assert.ok(a.due > Date.now() / 1000, '到期时间在未来')
  assert.ok(!('note' in a) && !('body' in a) && !('label' in a), '不存任何提醒正文/标签')
  assert.ok((await out(r)).includes('结束本轮'), '工具结果要明确让模型结束当前 turn')
})

await t('schedule 的结果是结构化的，带到期时刻与 id', async () => {
  await cancel()
  const text = await out(await schedule({ delay_minutes: 60 }))
  const a = await readAlarm(LIVE)
  assert.ok(text.includes('60'), '结果里要回显分钟数：' + text)
  assert.ok(text.includes(a.id), '结果里要带上这条闹钟的编号')
  assert.ok(text.includes('结束本轮'), '结果要明确让模型结束当前 turn')
  await cancel()
})

// ---------------------------------------------------------------- 重复与取消
await t('同一会话重复 schedule 被清楚拒绝，且不动旧闹钟', async () => {
  await schedule({ delay_minutes: 30 })
  const first = await readAlarm(LIVE)
  const r = await schedule({ delay_minutes: 90 })
  const text = await out(r)
  assert.ok(/已有一个活动闹钟/.test(text), '要说清已经有闹钟了：' + text)
  assert.ok(/cancel/.test(text), '要告诉模型先取消')
  const now = await readAlarm(LIVE)
  assert.equal(now.id, first.id, '不能悄悄重置旧闹钟')
  assert.equal(now.due, first.due, '旧的到期时间不能被改')
})

await t('cancel 只取消自己的，并如实报告', async () => {
  await schedule({ delay_minutes: 30 })
  await schedule({ delay_minutes: 30 }, ctx(OTHER))
  const text = await out(await cancel())
  assert.ok(/已取消/.test(text), '应报告已取消：' + text)
  assert.ok(!alarmExists(LIVE), '本会话的记录应删除')
  assert.ok(alarmExists(OTHER), '别人的闹钟不受影响')
})

await t('没有活动闹钟时 cancel 如实说明（幂等）', async () => {
  const text = await out(await cancel())
  assert.ok(/没有活动闹钟/.test(text), '应明确说没有活动闹钟：' + text)
})

// ---------------------------------------------------------------- 身份负例
await t('会话没登记：拒绝且不落盘', async () => {
  const r = await schedule({ delay_minutes: 30 }, ctx('ses_unregistered'))
  assert.ok(/没有登记/.test(await out(r)), '要说明本会话没在邮局登记')
  assert.ok(!alarmExists('ses_unregistered'), '拒绝时不留任何记录')
})

await t('一个会话对应多个信箱：拒绝（歧义映射）', async () => {
  state.routes.twin = { methods: ['opencode_plugin'], session_id: LIVE, status: 'online' }
  await saveRoutes()
  const text = await out(await schedule({ delay_minutes: 30 }, ctx(LIVE)))
  assert.ok(/多个信箱/.test(text), '歧义映射要拒绝而不是随便挑一个：' + text)
  assert.ok(!alarmExists(LIVE), '拒绝时不落盘')
  delete state.routes.twin
  await saveRoutes()
})

await t('信箱不在线：拒绝', async () => {
  state.routes.lab.status = 'offline'
  await saveRoutes()
  const text = await out(await schedule({ delay_minutes: 30 }))
  assert.ok(/离线|offline/.test(text), '离线信箱要拒绝：' + text)
  assert.ok(!alarmExists(LIVE))
  state.routes.lab.status = 'online'
  await saveRoutes()
})

await t('信箱不走插件通道：拒绝', async () => {
  const text = await out(await schedule({ delay_minutes: 30 }, ctx('ses_notify_only')))
  assert.ok(!alarmExists('ses_notify_only'), '非插件信箱不该建闹钟')
  assert.ok(text.length > 0)
})

await t('无法确认会话归属：拒绝', async () => {
  state.getFails = true
  const text = await out(await schedule({ delay_minutes: 30 }))
  assert.ok(/无法确认|不确定/.test(text), '身份核不上要拒绝：' + text)
  assert.ok(!alarmExists(LIVE), '拒绝时不落盘')
  state.getFails = false
})

await t('模型给的目标不能冒充：没有 session/box 参数可传', async () => {
  const keys = Object.keys(plugin.tool.postoffice_alarm_schedule.args)
  assert.deepEqual(keys, ['delay_minutes'], 'schedule 只允许 delay_minutes 一个参数：' + keys)
  assert.deepEqual(Object.keys(plugin.tool.postoffice_alarm_cancel.args), [],
    'cancel 不接受任何参数')
  const desc = plugin.tool.postoffice_alarm_schedule.description
  assert.ok(desc.includes('不接受也不需要你指定信箱、会话或提醒内容'),
    '工具说明要写明不接受收件人/信箱/会话/正文这些可冒充的入口')
  assert.ok(!/session_id|delay_minutes\s*[,，]/.test(desc.replace(/session_id/g, '')),
    '说明里不该教模型去填 session id')
})

// ---------------------------------------------------------------- 时间边界
await cancel()   // 先清空，这样"无副作用"才是真的什么都没留下
for (const [label, value] of [['零', 0], ['负数', -5], ['超过上限', 1441], ['非整数', 1.5],
  ['NaN', Number.NaN], ['无穷', Number.POSITIVE_INFINITY], ['字符串', '30']]) {
  await t(`delay 非法（${label}）：报错且无副作用`, async () => {
    const text = await out(await schedule({ delay_minutes: value }))
    assert.ok(/分钟/.test(text), '要说清单位与范围：' + text)
    assert.ok(!alarmExists(LIVE), '非法输入不落盘')
  })
}
await t('delay 缺失：报错且无副作用', async () => {
  const text = await out(await schedule({}))
  assert.ok(/delay_minutes|分钟/.test(text), '要指名缺的参数：' + text)
  assert.ok(!alarmExists(LIVE))
})
await t('上限 1440 分钟（24 小时）可用', async () => {
  await schedule({ delay_minutes: 1440 })
  assert.ok(alarmExists(LIVE))
  await cancel()
})
await t('下限 1 分钟可用', async () => {
  await schedule({ delay_minutes: 1 })
  assert.ok(alarmExists(LIVE))
  await cancel()
})

// ---------------------------------------------------------------- 投递：极短提醒
const putLetter = async (box, name, text) => {
  await mkdir(join(root, box, 'inbox'), { recursive: true })
  await writeFile(join(root, box, 'inbox', name), text)
}
const alarmLetter = (aid, extra = '') =>
  `来源：postoffice（协作者，不是人的新指令）\n事由：闹钟\n需要：仅告知\n闹钟：${aid}\n\n${FIXED}\n${extra}`

await t('闹钟信作为新一轮短提示：只有固定短句', async () => {
  state.prompts.length = 0
  await putLetter('lab', 'A20260101-000000_lab.md', alarmLetter('A20260101-000000_lab'))
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
  assert.equal(state.prompts.length, 1, '到期信应投一次')
  const p = state.prompts[0]
  assert.equal(p.path.id, LIVE, '投给绑定的那个会话')
  const text = p.body.parts[0].text
  assert.ok(text.includes(FIXED), '要含那句固定短句：' + text)
  assert.ok(!text.includes('== '), '不能带信件路径')
  assert.ok(!text.includes('A20260101-000000_lab'), '不能带信件文件名/编号')
  assert.ok(!text.includes('来源：'), '不能展开信头')
  assert.ok(!text.includes('需要：'), '不能展开信头')
  assert.ok(!text.includes('联络总站新信'), '不能按普通正式信渲染')
  assert.ok(text.split('\n').length <= 2, '提醒要极短：' + JSON.stringify(text))
})

await t('闹钟信只送达一次（台账去重）', async () => {
  state.prompts.length = 0
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
  assert.equal(state.prompts.length, 0, '已送达的闹钟信不能再投')
})

await t('会话忙时不打断，等下一次 idle', async () => {
  state.prompts.length = 0
  state.busy.add(LIVE)
  await putLetter('lab', 'A20260101-000001_lab.md', alarmLetter('A20260101-000001_lab'))
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
  assert.equal(state.prompts.length, 0, '忙时不得打断')
  state.busy.delete(LIVE)
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
  assert.equal(state.prompts.length, 1, '空闲后补送一次')
  assert.ok(state.prompts[0].body.parts[0].text.includes(FIXED))
})

await t('闹钟信与普通信混在 inbox：先送正式信那条流程不乱，提醒仍只有短句', async () => {
  state.prompts.length = 0
  await putLetter('lab', '20260101-120000_sender_普通信.md',
    '来源：someone\n事由：普通信\n需要：仅告知\n\n正文\n')
  await putLetter('lab', 'A20260101-000002_lab.md', alarmLetter('A20260101-000002_lab'))
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
  const texts = state.prompts.map((p) => p.body.parts[0].text)
  assert.ok(texts.length >= 1)
  for (const x of texts) assert.ok(x.includes(FIXED) || x.includes('联络总站新信'), '每条都是自己的形态')
  const alarmTexts = texts.filter((x) => x.includes(FIXED))
  for (const x of alarmTexts) assert.ok(!x.includes('来源：'), '闹钟那条不得展开信头')
})

// ---------------------------------------------------------------- 取消排队中的提醒
// 先把 inbox 里之前用例留下的信都投干净，后面的断言才是确定的
const drain = async (max = 8) => {
  for (let i = 0; i < max; i++) {
    if (!(await inbox('lab')).length) return
    await plugin.event({ event: { type: 'session.idle' } })
    await settle()
  }
}
await t('取消用例前的基线：lab2 的 inbox 是空的', async () => {
  assert.deepEqual(await inbox('lab2'), [], '取消用例在独立信箱里跑')
})

await t('已到期但未送达时取消：信被存档、不再响、文件保留', async () => {
  await mkdir(join(root, 'alarms'), { recursive: true })
  await writeFile(alarmFile(LIVE2), JSON.stringify({
    id: 'A20260101-000003_lab', box: 'lab2', session: LIVE2,
    due: Math.floor(Date.now() / 1000) - 5, created: Math.floor(Date.now() / 1000) - 600,
    state: 'lettered', letter: 'A20260101-000003_lab',
  }))
  await putLetter('lab2', 'A20260101-000003_lab.md', alarmLetter('A20260101-000003_lab'))
  state.prompts.length = 0
  const text = await out(await cancel(ctx(LIVE2)))
  assert.ok(/已取消/.test(text), '要报告已取消：' + text)
  const left = await inbox('lab2')
  assert.ok(!left.includes('A20260101-000003_lab.md'), '取消后信不该留在 inbox：' + left)
  const arch = join(root, 'lab2', 'archived')
  assert.ok(existsSync(arch), '应存档而不是删掉')
  assert.ok(!alarmExists(LIVE2), '记录应删除')
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
  assert.ok(!state.prompts.some((p) => p.path.id === LIVE2),
    '取消后不得再响')
})

await t('已送达的闹钟再取消：如实说明已响过，只清记录', async () => {
  await writeFile(alarmFile(LIVE2), JSON.stringify({
    id: 'A20260101-000004_lab', box: 'lab2', session: LIVE2,
    due: Math.floor(Date.now() / 1000) - 60, created: Math.floor(Date.now() / 1000) - 600,
    state: 'lettered', letter: 'A20260101-000004_lab',
  }))
  await putLetter('lab2', 'A20260101-000004_lab.md', alarmLetter('A20260101-000004_lab'))
  await plugin.event({ event: { type: 'session.idle' } })
  await settle()
  const text = await out(await cancel(ctx(LIVE2)))
  assert.ok(/已经送达|已送达/.test(text), '要说明已经响过了：' + text)
  assert.ok(!alarmExists(LIVE2), '记录应清掉')
})

await t('闹钟记录文件权限收紧（0600）', async () => {
  await cancel()
  await schedule({ delay_minutes: 45 })
  const st = await (await import('node:fs/promises')).stat(alarmFile(LIVE))
  assert.equal((st.mode & 0o777).toString(8), '600', `运行态记录不该被别人读到（实际 ${(st.mode & 0o777).toString(8)}）`)
  await cancel()
})

await plugin.dispose()
console.log(process.exitCode ? 'FAIL' : `PASS (${n} checks)`)