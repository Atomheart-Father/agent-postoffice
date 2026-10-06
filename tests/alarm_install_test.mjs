// OpenCode alarm plugin: the REAL INSTALL LAYOUT and the REAL tool-arg schema.
//
// tests/alarm_plugin_test.mjs imports ../opencode/postoffice.ts, i.e. the plugin still sitting in
// its repo checkout next to ./postoffice. That is the one layout in which the plugin's own CLI
// lookup happens to work, and it is NOT the layout any installer produces: `postoffice install
// opencode` copies only the .ts file into $XDG_CONFIG_HOME/opencode/plugins/ and leaves the
// executable behind in the checkout, reachable only through ~/.local/bin/postoffice.
//
// So this file builds that documented layout in a temp dir and enters the plugin from the
// installed copy, with a stand-in OpenCode session DB and its own post office home. Nothing here
// touches the real ~/.config/opencode, the real ~/agent-postoffice, or any other mailbox.
//
// Deliberately NOT set: POSTOFFICE_CLI. It is the plugin's only escape hatch today; a fix that
// keeps relying on it would pass this suite by asking the operator to do the installer's job.
//
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, cp, chmod, symlink, realpath, access } from 'node:fs/promises'
import { constants } from 'node:fs'
import { tmpdir, homedir } from 'node:os'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// ---------------------------------------------------------------- 找 OpenCode 的依赖树
// The plugin must resolve `@opencode-ai/plugin` and `zod` from the same node_modules OpenCode
// itself uses. Under the install layout those sit at $XDG_CONFIG_HOME/opencode/node_modules, one
// level above the plugin file — so the fixture mirrors that and the test's own zod comes from
// there too, through a bare-specifier shim (never a hardcoded path inside the zod package).
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
                `  用 OPENCODE_NODE_MODULES=<path> 指定。没有它就无法验证 args 的 schema 形状。`)
  process.exit(1)
}

// ---------------------------------------------------------------- 搭出真实安装布局
const T = await mkdtemp(join(tmpdir(), 'postoffice-alarm-install-'))
// RED 阶段也会跑到这里，所以清理挂在 exit 上：失败时不留下半截布局
process.on('exit', () => { try { createRequire(import.meta.url)('node:fs').rmSync(T, { recursive: true, force: true }) } catch {} })
const FAKE_HOME = join(T, 'fakehome')
const POHOME = join(FAKE_HOME, 'agent-postoffice')            // POSTOFFICE_HOME
const PLUGIN_DIR = join(FAKE_HOME, '.config/opencode/plugins')
const INSTALLED = join(PLUGIN_DIR, 'postoffice.ts')
const CHECKOUT = join(T, 'checkout')                            // the repo checkout the CLI stays in
const LOCAL_BIN = join(FAKE_HOME, '.local/bin/postoffice')
const SID = 'ses_a1_installed'
const BOX = 'a1lab'                                            // 独立信箱：本文件不碰投递，信箱也专用
const DIR_OK = join(T, 'proj')

await mkdir(PLUGIN_DIR, { recursive: true })
await mkdir(join(CHECKOUT, 'logs'), { recursive: true })
await mkdir(POHOME, { recursive: true })
await mkdir(LOCAL_BIN.replace(/\/postoffice$/, ''), { recursive: true })
await mkdir(join(DIR_OK, '.opencode'), { recursive: true })

// the executable CLI, left behind in the checkout
await cp(join(REPO, 'postoffice'), join(CHECKOUT, 'postoffice'))
await chmod(join(CHECKOUT, 'postoffice'), 0o755)
// install.sh's symlink: the only handle on that executable outside the checkout
await symlink(join(CHECKOUT, 'postoffice'), LOCAL_BIN)
// what `postoffice install opencode` writes, and all it writes: the .ts file, nothing else
await cp(join(REPO, 'opencode/postoffice.ts'), INSTALLED)
// the checkout's own copy of the plugin is deleted, so nothing can quietly resolve through it
await rm(join(CHECKOUT, 'opencode'), { recursive: true, force: true })
// node_modules where OpenCode keeps them: above the plugin file, resolvable by bare specifier
await symlink(OPENCODE_NM, join(FAKE_HOME, '.config/opencode/node_modules'))
// shim so this test file (living in the repo, with no node_modules) imports the SAME zod module
// instance the plugin will import
const SHIM = join(PLUGIN_DIR, '_zod_shim_for_test.mjs')
await writeFile(SHIM, 'export { z } from "zod"\n')

process.env.POSTOFFICE_HOME = POHOME
process.env.POSTOFFICE_NO_NOTIFY = '1'
delete process.env.POSTOFFICE_CLI
// HOME must be the fake one too, or any fix that resolves ~/.local/bin/postoffice would find the
// *real* operator symlink and pass while pointing at the wrong executable. Set before the import:
// the plugin reads homedir() at module-evaluation time.
process.env.HOME = FAKE_HOME

// stand-in OpenCode session DB for the Python-side identity re-check (postoffice: opencode_db)
const dbPath = join(T, 'opencode.db')
await writeFile(dbPath, '')
execFileSync('sqlite3', [dbPath, [
  'create table session (id text, title text, directory text, parent_id text, time_updated integer);',
  `insert into session values ('${SID}','installed','${DIR_OK}',null,0);`,
].join('')])
process.env.OPENCODE_DB = dbPath

// a registered, online, plugin-routed mailbox for that session — what `postoffice add` leaves behind
await mkdir(join(POHOME, BOX, 'inbox'), { recursive: true })
await mkdir(join(POHOME, 'logs'), { recursive: true })
await writeFile(join(POHOME, 'routes.json'), JSON.stringify({
  [BOX]: { methods: ['opencode_plugin'], session_id: SID, status: 'online' },
}, null, 2) + '\n')

const { z } = await import(pathToFileURL(SHIM).href)
const { PostofficePlugin } = await import(pathToFileURL(INSTALLED).href)

// Every plugin instance owns a setInterval. A failing case would otherwise leave one running and
// node would never exit, so the suite hangs instead of reporting red.
const live = new Set()
const track = (p) => { live.add(p); return p }
const disposeAll = async () => { for (const p of live) { try { await p.dispose() } catch {} } live.clear() }

const client = {
  session: {
    get: async () => ({ data: { directory: DIR_OK } }),
    status: async () => ({ data: {} }),          // no session is busy
    promptAsync: async () => { throw new Error('本用例不做投递') },
  },
}
const plugin = track(await PostofficePlugin({ client, directory: DIR_OK }))
const ctx = () => ({
  sessionID: SID, messageID: 'msg_a1', agent: 'build', directory: DIR_OK,
  worktree: DIR_OK, abort: new AbortController().signal,
  metadata() {}, async ask() {},
})
const out = async (r) => (typeof r === 'string' ? r : r.output)
const alarmFiles = async () =>
  (await readdir(join(POHOME, 'alarms')).catch(() => [])).filter((f) => f.endsWith('.json')).sort()
const clearAlarm = async () => {
  for (const f of await alarmFiles()) await rm(join(POHOME, 'alarms', f), { force: true })
}


// 前置条件（不算用例）：布局本身必须是真的。installed 副本在 plugins/ 下，checkout 里已无 postoffice.ts；
// 可执行文件只能通过 ~/.local/bin 的 symlink 够到，且它确实存在、可执行。
{
  assert.equal(await readFile(INSTALLED, 'utf8'), await readFile(join(REPO, 'opencode/postoffice.ts'), 'utf8'),
    '前置条件：装出来的插件就是仓库里那一份')
  await assert.rejects(() => access(join(CHECKOUT, 'opencode/postoffice.ts')), '前置条件：checkout 里不该再有插件副本')
  const viaBin = await realpath(LOCAL_BIN)
  await access(viaBin, constants.X_OK)
  assert.equal(viaBin, await realpath(join(CHECKOUT, 'postoffice')),
    '前置条件：~/.local/bin/postoffice 指向 checkout 里那个可执行文件')
}

let n = 0
const t = async (name, fn) => {
  await clearAlarm()
  try { await fn(); console.log('ok   -', name); n++ } catch (e) {
    console.log('FAIL -', name, '::', e.message); process.exitCode = 1
  }
}

// ------------------------------------------------------------------ 另一套布局的挂载器
// ROOT, PO_CLI and the zod seam are all frozen when the module is evaluated, so every layout needs
// its own copy of the plugin file — and its own HOME / POSTOFFICE_HOME / session, set before that
// copy is imported.
let sessionSeq = 0
const mount = async (name, { layout, symlinkedCLI = true, checkoutCLI = 'real', nodeModules = 'seam' }) => {
  const base = join(T, name)
  const home = join(base, 'home')
  const poHome = join(home, 'agent-postoffice')
  const checkout = join(base, 'checkout')
  const sid = `ses_${name}_${++sessionSeq}`
  const box = `${name}box`
  const dirOk = join(base, 'proj')
  await mkdir(join(checkout, 'opencode'), { recursive: true })
  await mkdir(poHome, { recursive: true })
  await mkdir(join(poHome, box, 'inbox'), { recursive: true })
  await mkdir(join(poHome, 'logs'), { recursive: true })
  await mkdir(dirOk, { recursive: true })

  // the executable, left in the checkout
  if (checkoutCLI === 'real') {
    await cp(join(REPO, 'postoffice'), join(checkout, 'postoffice'))
    await chmod(join(checkout, 'postoffice'), 0o755)
  } else if (checkoutCLI === 'stub') {
    // a stand-in that fails loudly, so "which candidate won" is observable in the tool's answer
    await writeFile(join(checkout, 'postoffice'), '#!/bin/sh\necho "STUB-CHECKOUT-CLI $*"\nexit 7\n')
    await chmod(join(checkout, 'postoffice'), 0o755)
  }
  if (symlinkedCLI) {
    await mkdir(join(home, '.local/bin'), { recursive: true })
    const real = join(base, 'symlink-target')
    await cp(join(REPO, 'postoffice'), real)
    await chmod(real, 0o755)
    await symlink(real, join(home, '.local/bin/postoffice'))
  }

  // the plugin file, at whatever place `layout` says
  const pluginAt = layout === 'installed'
    ? join(home, '.config/opencode/plugins/postoffice.ts')
    : layout === 'installed-nomodules'
      ? join(home, '.config/opencode/plugins/postoffice.ts')
      : join(checkout, 'opencode/postoffice.ts')
  await mkdir(dirname(pluginAt), { recursive: true })
  await cp(join(REPO, 'opencode/postoffice.ts'), pluginAt)

  // zod: 'inplace' mirrors a real install (node_modules right above the plugin, so the plugin's
  // own Node resolution finds it with no configuration); 'seam' forces the explicit override.
  if (nodeModules === 'inplace') {
    await symlink(OPENCODE_NM, join(home, '.config/opencode/node_modules'))
  } else {
    process.env.POSTOFFICE_PLUGIN_MODULES = OPENCODE_NM
  }

  const db = join(base, 'opencode.db')
  await writeFile(db, '')
  execFileSync('sqlite3', [db, [
    'create table session (id text, title text, directory text, parent_id text, time_updated integer);',
    `insert into session values ('${sid}','${name}','${dirOk}',null,0);`,
  ].join('')])
  await writeFile(join(poHome, 'routes.json'), JSON.stringify({
    [box]: { methods: ['opencode_plugin'], session_id: sid, status: 'online' },
  }, null, 2) + '\n')

  process.env.HOME = home
  process.env.POSTOFFICE_HOME = poHome
  process.env.OPENCODE_DB = db
  delete process.env.POSTOFFICE_CLI
  const prompts = []
  const mod = await import(pathToFileURL(pluginAt).href)
  const p = track(await mod.PostofficePlugin({
    client: {
      session: {
        get: async () => ({ data: { directory: dirOk } }),
        status: async () => ({ data: {} }),
        promptAsync: async (args) => { prompts.push(args) },
      },
    },
    directory: dirOk,
  }))
  const c = () => ({
    sessionID: sid, messageID: `msg_${name}`, agent: 'build', directory: dirOk,
    worktree: dirOk, abort: new AbortController().signal,
    metadata() {}, async ask() {},
  })
  const settle = async (want) => {
    for (let i = 0; i < 60 && prompts.length < want; i++) await new Promise((r) => setTimeout(r, 50))
  }
  return {
    plugin: p, ctx: c, home, poHome, sid, box, checkout, pluginAt, prompts, settle,
    out: async (r) => (typeof r === 'string' ? r : r.output),
    records: async () => (await readdir(join(poHome, 'alarms')).catch(() => [])).filter((f) => f.endsWith('.json')),
    dispose: () => p.dispose(),
  }
}

const schedule = (args) => plugin.tool.postoffice_alarm_schedule.execute(args, ctx())
const cancel = () => plugin.tool.postoffice_alarm_cancel.execute({}, ctx())
const boundShape = (name) => {
  const t = plugin.tool?.[name]
  assert.ok(t, `工具 ${name} 必须注册`)
  return t.args
}
// “真 Zod schema” 的判据：每个值都是 Zod schema（能 parse、有 _zod 定义）。
// JSON Schema 风格的 {type:'number',minimum:1} 两个特征都没有，所以这条不是恒真断言。
const isZodRawShape = (shape) => {
  assert.equal(typeof shape, 'object', 'args 必须是对象')
  for (const [k, v] of Object.entries(shape))
    assert.ok(typeof v?.parse === 'function' && v?._zod !== undefined,
      `args.${k} 必须是真正的 Zod schema，现在拿到的是 ${JSON.stringify(v)}`)
}
// 框架解析 args 的那一步。解析不了就是 RED 本身，所以把 zod 抛的原文带出来，别吞掉。
const parses = (shape, value) => {
  try { return z.object(shape).safeParse({ delay_minutes: value }) }
  catch (e) { throw new Error(`框架无法解析 args（${e.message}）`) }
}

// ================================================================ A-1 真实安装布局
// The two alarm tools are the only consumers of PO_CLI. Delivery (scan) never spawns anything, so
// a broken CLI lookup cannot break 投信 — it breaks exactly the two tools the model calls, and it
// breaks them by throwing instead of answering, because spawn("") rejects inside the Promise
// executor of runCLI and nothing catches it.

await t('安装布局下 CLI 可解析：schedule 落地一条真记录（不抛）', async () => {
  const r = await schedule({ delay_minutes: 30 })
  const text = await out(r)
  assert.ok(text.includes('已设'), `应设成功，实际：${text}`)
  const files = await alarmFiles()
  assert.equal(files.length, 1, `记录应由可执行的 CLI 落盘，实际有：${files}`)
  const rec = JSON.parse(await readFile(join(POHOME, 'alarms', files[0]), 'utf8'))
  assert.equal(rec.box, BOX)
  assert.equal(rec.session, SID)
  assert.equal(rec.state, 'pending')
})

await t('安装布局下 CLI 可解析：cancel 不抛且如实报告', async () => {
  await schedule({ delay_minutes: 30 })
  const text = await out(await cancel())
  assert.match(text, /已取消/, `取消应成功：${text}`)
  assert.deepEqual(await alarmFiles(), [], '取消后不该留下记录')
})

await t('安装布局下 cancel 幂等：没有活动闹钟时如实说明（仍不抛）', async () => {
  const text = await out(await cancel())
  assert.match(text, /没有活动闹钟/, `应如实说明：${text}`)
})

// ================================================================ A-2 真 Zod schema
// The registered args must be a zod raw shape, because that is what the framework types demand
// (@opencode-ai/plugin ToolDefinition = ReturnType<typeof tool>, args: z.ZodRawShape) and what the
// framework can therefore validate. A JSON-Schema-shaped args compiles only through two `as`
// casts, and then the framework has nothing it can parse.

await t('schedule 的 args 逐个是 Zod schema（框架能拿它校验）', async () => {
  isZodRawShape(boundShape('postoffice_alarm_schedule'))
})

await t('schedule 的 schema：合法值能解析', async () => {
  const r = parses(boundShape('postoffice_alarm_schedule'), 30)
  assert.equal(r.success, true, `30 分钟应当通过 schema：${JSON.stringify(r.error?.issues)}`)
  assert.equal(r.data.delay_minutes, 30)
})

for (const [label, value] of [['0', 0], ['1441', 1441], ['小数 1.5', 1.5], ['字符串 "30"', '30']]) {
  await t(`schedule 的 schema 拒绝越界的 ${label}`, async () => {
    const r = parses(boundShape('postoffice_alarm_schedule'), value)
    assert.equal(r.success, false, `${JSON.stringify(value)} 必须被 schema 拒绝（框架据此拒绝，不该流进 execute）`)
  })
}

await t('schedule 的 schema 边界是闭区间 1 与 1440', async () => {
  const shape = boundShape('postoffice_alarm_schedule')
  for (const v of [1, 1440]) assert.equal(parses(shape, v).success, true, `${v} 分钟应当合法`)
})

await t('cancel 正常注册、无参数', async () => {
  assert.equal(typeof plugin.tool?.postoffice_alarm_cancel?.execute, 'function', 'cancel 必须注册')
  const shape = boundShape('postoffice_alarm_cancel')
  assert.deepEqual(Object.keys(shape), [], 'cancel 不收任何参数')
  assert.equal(z.object(shape).safeParse({}).success, true, 'cancel 的空 schema 必须能解析空参数')
})

await plugin.dispose()

// ================================================================ GREEN：同一组用例双跑两种布局
// The installed layout above and the repo checkout layout must both work, with no configuration.
// Same three cases, same expectations — only the place the plugin file lives differs.
const co = await mount('co', { layout: 'checkout', symlinkedCLI: false, nodeModules: 'seam' })
const runAlarmTrio = async (m, label) => {
  await t(`${label}：schedule 落地一条真记录`, async () => {
    const text = await m.out(await m.plugin.tool.postoffice_alarm_schedule.execute({ delay_minutes: 45 }, m.ctx()))
    assert.ok(text.includes('已设'), `应设成功：${text}`)
    const files = await m.records()
    assert.equal(files.length, 1, `记录应由可执行的 CLI 落盘：${files}`)
    const rec = JSON.parse(await readFile(join(m.poHome, 'alarms', files[0]), 'utf8'))
    assert.equal(rec.box, m.box)
    assert.equal(rec.session, m.sid)
    assert.equal(rec.state, 'pending')
  })
  await t(`${label}：cancel 清掉记录`, async () => {
    const text = await m.out(await m.plugin.tool.postoffice_alarm_cancel.execute({}, m.ctx()))
    assert.match(text, /已取消/, `取消应成功：${text}`)
    assert.deepEqual(await m.records(), [], '取消后不该留下记录')
  })
  await t(`${label}：cancel 幂等，如实说明没有活动闹钟`, async () => {
    const text = await m.out(await m.plugin.tool.postoffice_alarm_cancel.execute({}, m.ctx()))
    assert.match(text, /没有活动闹钟/, `应如实说明：${text}`)
  })
}
await runAlarmTrio(co, 'checkout 布局')
await co.dispose()

// ================================================================ GREEN：候选优先级
// Both candidates exist: a stub CLI one level above the plugin, and a real one behind
// ~/.local/bin. The stub answers, so which one ran is visible in the tool's own reply — and the
// layout is built so the wrong choice would *succeed* silently, which is exactly what must not
// happen: a developer running from a checkout must never have their alarms written by some other
// checkout's CLI.
const pri = await mount('pri', { layout: 'checkout', checkoutCLI: 'stub', nodeModules: 'seam' })
await t('checkout 里的脚本优先于 ~/.local/bin 的 symlink', async () => {
  const text = await pri.out(await pri.plugin.tool.postoffice_alarm_schedule.execute({ delay_minutes: 45 }, pri.ctx()))
  assert.match(text, /STUB-CHECKOUT-CLI/, `应当跑 checkout 里那个脚本，实际：${text}`)
  assert.deepEqual(await pri.records(), [], '桩脚本没写记录，记录不该出现')
})
await pri.dispose()

// ================================================================ GREEN：CLI 找不到时不许 reject
// Nothing resolvable: no script above the plugin, no ~/.local/bin, nothing in POSTOFFICE_HOME.
// The tools must still answer in Chinese — the model must never see a spawn() TypeError, and the
// delivery channel (which never spawns anything) must be unaffected.
const nocli = await mount('no', { layout: 'installed-nomodules', symlinkedCLI: false, nodeModules: 'seam' })
await t('CLI 不可解析时 schedule 返回中文说明而不是抛异常', async () => {
  const r = await nocli.plugin.tool.postoffice_alarm_schedule.execute({ delay_minutes: 45 }, nocli.ctx())
  const text = await nocli.out(r)
  assert.match(text, /没设闹钟/, `应当是工具自己的说明：${text}`)
  assert.match(text, /postoffice 可执行文件/, `说明要指出可执行文件找不到：${text}`)
  assert.match(text, /POSTOFFICE_CLI/, `说明要给出可操作的出路：${text}`)
  assert.deepEqual(await nocli.records(), [], '没有 CLI 就不该有记录')
})
await t('CLI 不可解析时 cancel 同样返回中文说明', async () => {
  const text = await nocli.out(await nocli.plugin.tool.postoffice_alarm_cancel.execute({}, nocli.ctx()))
  assert.match(text, /没取消/, `应当是工具自己的说明：${text}`)
  assert.match(text, /postoffice 可执行文件/, `说明要指出可执行文件找不到：${text}`)
})
await t('CLI 不可解析时投递通道照旧：同一份插件仍把信投出去', async () => {
  // A broken CLI lookup must not take 投信 down with it: scan() never spawns anything. The letter
  // goes through the ordinary idle channel and must actually arrive — asserted on the prompt the
  // mock client received, not on a counter nobody increments.
  const letter = '20260101-000001_nocli.md'
  await writeFile(join(nocli.poHome, nocli.box, 'inbox', letter),
    `来源：boss\n事由：试投\n需要：仅告知\n\n这封信不该被工具坏了影响\n`)
  await nocli.plugin.event({ event: { type: 'session.idle' } })
  await nocli.settle(1)
  assert.equal(nocli.prompts.length, 1, 'CLI 找不到也照样投信')
  const body = nocli.prompts[0].body.parts[0].text
  assert.ok(body.includes(nocli.box), `提醒要带上信箱：${body}`)
  assert.ok(body.includes(letter), `提醒要带上信件路径：${body}`)
  assert.deepEqual(await nocli.records(), [], '投递不该顺带造出闹钟记录')
})
await nocli.dispose()

// ================================================================ GREEN：XDG_CONFIG_HOME + 真 installer
// Run the real `postoffice install opencode` with a fake HOME and a non-default XDG_CONFIG_HOME,
// then load the plugin from exactly where the installer put it. The zod seam here cannot be
// Node's own resolution (nothing above that fake XDG dir has a node_modules), so this case is
// also the one that exercises POSTOFFICE_PLUGIN_MODULES as the explicit, reproducible override.
const xdgBase = join(T, 'xdg')
const xdgHome = join(xdgBase, 'home')
const xdgConf = join(xdgBase, 'xdgconfig')
const xdgCheckout = join(xdgBase, 'checkout')
const xdgPoHome = join(xdgHome, 'agent-postoffice')
await mkdir(join(xdgCheckout, 'opencode'), { recursive: true })
await mkdir(join(xdgPoHome, 'logs'), { recursive: true })
await cp(join(REPO, 'postoffice'), join(xdgCheckout, 'postoffice'))
await chmod(join(xdgCheckout, 'postoffice'), 0o755)
await cp(join(REPO, 'opencode/postoffice.ts'), join(xdgCheckout, 'opencode/postoffice.ts'))
const xdgEnv = {
  ...process.env,
  HOME: xdgHome,
  XDG_CONFIG_HOME: xdgConf,
  POSTOFFICE_HOME: xdgPoHome,
  POSTOFFICE_NO_NOTIFY: '1',
}
const runInstaller = (...args) =>
  execFileSync(join(xdgCheckout, 'postoffice'), args,
    { env: xdgEnv, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })

const xdgInstalled = join(xdgConf, 'opencode/plugins/postoffice.ts')
runInstaller('install', 'opencode')
const xdgFirst = await readFile(xdgInstalled, 'utf8')

await t('XDG_CONFIG_HOME 非默认时 installer 仍把插件装到 OpenCode 会加载的位置', async () => {
  assert.equal(xdgFirst, await readFile(join(REPO, 'opencode/postoffice.ts'), 'utf8'),
    '装出来的必须就是仓库里那一份')
  assert.deepEqual((await readdir(dirname(xdgInstalled))).sort(), ['postoffice.ts'],
    'plugins/ 下只应有插件本身（方案 ii 不持久化任何 CLI 路径）')
})

await t('重装幂等：不产生第二份、也不改动已装的那份', async () => {
  runInstaller('install', 'opencode')
  assert.deepEqual((await readdir(dirname(xdgInstalled))).sort(), ['postoffice.ts'], '重装不该多出文件')
  assert.equal(await readFile(xdgInstalled, 'utf8'), xdgFirst, '重装后内容必须一致')
})

await t('从 XDG 安装位置加载的插件能用（zod 走 POSTOFFICE_PLUGIN_MODULES 这条显式接缝）', async () => {
  const sid = 'ses_xdg', box = 'xdgbox', dirOk = join(xdgBase, 'proj')
  await mkdir(join(dirOk), { recursive: true })
  await mkdir(join(xdgPoHome, box, 'inbox'), { recursive: true })
  const db = join(xdgBase, 'opencode.db')
  await writeFile(db, '')
  execFileSync('sqlite3', [db, [
    'create table session (id text, title text, directory text, parent_id text, time_updated integer);',
    `insert into session values ('${sid}','xdg','${dirOk}',null,0);`,
  ].join('')])
  await writeFile(join(xdgPoHome, 'routes.json'), JSON.stringify({
    [box]: { methods: ['opencode_plugin'], session_id: sid, status: 'online' },
  }, null, 2) + '\n')
  // a checkout + symlink for the CLI, exactly like install.sh would leave behind
  await mkdir(join(xdgHome, '.local/bin'), { recursive: true })
  await symlink(join(xdgCheckout, 'postoffice'), join(xdgHome, '.local/bin/postoffice'))

  process.env.HOME = xdgHome
  process.env.POSTOFFICE_HOME = xdgPoHome
  process.env.OPENCODE_DB = db
  process.env.POSTOFFICE_PLUGIN_MODULES = OPENCODE_NM
  delete process.env.POSTOFFICE_CLI
  const mod = await import(pathToFileURL(xdgInstalled).href)
  const p = track(await mod.PostofficePlugin({
    client: {
      session: {
        get: async () => ({ data: { directory: dirOk } }),
        status: async () => ({ data: {} }),
        promptAsync: async () => { throw new Error('本用例不做投递') },
      },
    },
    directory: dirOk,
  }))
  const c = { sessionID: sid, messageID: 'm', agent: 'build', directory: dirOk, worktree: dirOk,
    abort: new AbortController().signal, metadata() {}, async ask() {} }
  const r = await p.tool.postoffice_alarm_schedule.execute({ delay_minutes: 45 }, c)
  assert.match(typeof r === 'string' ? r : r.output, /已设/, `XDG 布局下应当设成功：${r}`)
  assert.equal((await readdir(join(xdgPoHome, 'alarms')).catch(() => [])).filter((f) => f.endsWith('.json')).length, 1,
    'XDG 布局下也该有记录')
  await p.dispose()
})

await t('现状记录：uninstall 没有 opencode 这个选项（相邻问题，本轮只报不改）', async () => {
  // There is no `postoffice uninstall opencode`. Locking the *current* contract so this change is
  // visibly not quietly widening scope; whoever adds the real uninstall will flip this test.
  let code = 0, stderr = ''
  try { runInstaller('uninstall', 'opencode') } catch (e) { code = e.status ?? -1; stderr = e.stderr ?? '' }
  assert.notEqual(code, 0, 'uninstall opencode 现在应当失败')
  assert.match(stderr + runInstaller('--help'), /claude/, 'uninstall 的可选部件应包含 claude')
})

await t('现状记录：卸载/重装都不回收 CLI symlink 与邮局目录（残留是有意保留的现状）', async () => {
  await access(join(xdgHome, '.local/bin/postoffice'), constants.X_OK)
  await access(join(xdgPoHome, 'routes.json'), constants.R_OK)
})

await disposeAll()
console.log(process.exitCode ? 'FAIL' : `PASS (${n} checks)`)
