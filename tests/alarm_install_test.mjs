// The alarm plugin under a real install, driven by the real installer.
//
// `install.sh --no-postman` is the only installer this file runs. --no-postman skips the resident
// postman, which is the one part that would touch launchd; everything else it does (init, the
// ~/.local/bin symlink, the Claude hook, the plugin, the skill) lands inside the fake HOME.
//
// What is asserted here, and nothing else:
//   ① a temp HOME install puts the plugin where OpenCode loads it, byte for byte
//   ② the installed plugin drives the CLI of the very checkout that was installed
//   ③ installing from a second checkout moves the plugin and the CLI together, never apart
//   ④ a plugin still sitting in a checkout works in dev mode, with no symlink involved
//   ⑤ the alarm schema takes 1 and 1440 and turns down 0 / 1441 / 1.5 / "30"
//   ⑥ a missing @opencode-ai/plugin costs the alarm tools, never delivery
//   ⑦ no CLI at all is a diagnosis the model can read, not a crash
//
// PRECONDITION — one OpenCode installation's dependency tree has to be reachable, because the
// plugin's args are declared with the official `@opencode-ai/plugin` tool.schema and Node resolves
// that specifier from the plugin file's own directory (that is the whole resolution story; the
// plugin has no fallback of its own). A real install satisfies this for free. A bare repo checkout
// does not, so this file symlinks one into the fake install layout, exactly where OpenCode keeps
// it. It looks at $OPENCODE_NODE_MODULES first, then ~/.config/opencode/node_modules, then
// ~/.opencode/node_modules, and exits with the list of what it tried if none has zod +
// @opencode-ai/plugin. Nothing outside the temp dirs is written.
import assert from 'node:assert/strict'
import {
  mkdtemp, mkdir, writeFile, readFile, readdir, rm, cp, chmod, symlink, realpath, access, lstat,
} from 'node:fs/promises'
import { existsSync, constants, rmSync } from 'node:fs'
import { tmpdir, homedir } from 'node:os'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFileSync } from 'node:child_process'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const REAL_CLI = join(REPO, 'postoffice')

// ---- precondition: an OpenCode dependency tree -------------------------------------------
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

// ---- temp roots ----------------------------------------------------------------------------
const T = await mkdtemp(join(tmpdir(), 'postoffice-alarm-install-'))
process.on('exit', () => { try { rmSync(T, { recursive: true, force: true }) } catch {} })

// A checkout is what an operator has on disk: the single-file CLI next to the plugin source, plus
// the two trees `postoffice init` and `postoffice install skill` read. `marker` distinguishes two
// checkouts so "the plugin came from B" is an observation, not an assumption.
const mkCheckout = async (name, marker) => {
  const dir = join(T, name, 'checkout')
  await mkdir(join(dir, 'opencode'), { recursive: true })
  await cp(REAL_CLI, join(dir, 'postoffice'))
  await chmod(join(dir, 'postoffice'), 0o755)
  await cp(join(REPO, 'opencode/postoffice.ts'), join(dir, 'opencode/postoffice.ts'))
  await cp(join(REPO, 'install.sh'), join(dir, 'install.sh'))
  await chmod(join(dir, 'install.sh'), 0o755)
  await cp(join(REPO, 'docs'), join(dir, 'docs'), { recursive: true })
  await cp(join(REPO, 'skill'), join(dir, 'skill'), { recursive: true })
  if (marker) await writeFile(join(dir, 'opencode/postoffice.ts'),
    `// ${marker}\n` + await readFile(join(dir, 'opencode/postoffice.ts'), 'utf8'))
  // A checkout that can actually load the plugin needs @opencode-ai/plugin resolvable from
  // opencode/postoffice.ts, which — same rule as the install layout — means a node_modules above it.
  await symlink(OPENCODE_NM, join(dir, 'node_modules'))
  return dir
}

// A real OpenCode install keeps its dependency tree in ~/.config/opencode/node_modules, i.e. one
// level above plugins/. The plugin declares its args with the official @opencode-ai/plugin, which
// Node resolves from the plugin file's own directory, so a faithful fixture has to put it there.
// withDeps:false models the one case where that is missing (category ⑥).
const mkHome = async (name, { withDeps = true } = {}) => {
  const home = join(T, name, 'home')
  await mkdir(join(home, '.config/opencode'), { recursive: true })
  await mkdir(join(home, '.local/bin'), { recursive: true })
  if (withDeps) await symlink(OPENCODE_NM, join(home, '.config/opencode/node_modules'))
  return home
}

// One stand-in OpenCode session DB for every layout; the Python side re-checks session identity
// against it, so it needs the rows the layouts below register.
const SID = {}
let sidSeq = 0
const DB = join(T, 'opencode.db')
const DIRS = {}
const dbRows = [['create table session (id text, title text, directory text, parent_id text, time_updated integer);']]
const newSession = (name) => {
  const id = `ses_${name}_${++sidSeq}`
  SID[name] = id
  DIRS[name] = join(T, name, 'proj')
  dbRows.push(`insert into session values ('${id}','${name}','${DIRS[name]}',null,0);`)
  return id
}
for (const name of ['instA', 'instB', 'dev', 'nocli', 'nozod', 'foreign', 'sibling', 'sw']) newSession(name)
await writeFile(DB, '')
execFileSync('sqlite3', [DB, dbRows.join('')])

// Give a layout its mailbox + session registration, i.e. what `postoffice add --opencode` leaves.
const register = async (name, poHome) => {
  const box = `${name}box`
  await mkdir(join(poHome, box, 'inbox'), { recursive: true })
  await mkdir(join(poHome, 'logs'), { recursive: true })
  await writeFile(join(poHome, 'routes.json'), JSON.stringify({
    [box]: { methods: ['opencode_plugin'], session_id: SID[name], status: 'online' },
  }, null, 2) + '\n')
  return box
}

const runInstallRaw = (checkout, home) => {
  try {
    return { rc: 0, out: execFileSync(join(checkout, 'install.sh'), ['--no-postman'],
      { env: { ...process.env, HOME: home, XDG_CONFIG_HOME: join(home, '.config') },
        encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }) }
  } catch (e) {
    return { rc: e.status ?? -1, out: `${e.stdout ?? ''}${e.stderr ?? ''}` }
  }
}
const runInstall = (checkout, home) => {
  const r = runInstallRaw(checkout, home)
  if (r.rc !== 0) throw new Error(`install.sh 失败（rc=${r.rc}）：${r.out}`)
  return r.out
}

// Load one plugin copy under one layout. ROOT, PO_CLI and the schema are all frozen when the module
// is evaluated, so every layout needs its own copy of the file and its own env at import time.
const mounted = new Set()
const mount = async (pluginAt, { name, home, poHome, withDeps = true }) => {
  process.env.HOME = home
  process.env.POSTOFFICE_HOME = poHome
  process.env.OPENCODE_DB = DB
  process.env.POSTOFFICE_NO_NOTIFY = '1'
  delete process.env.POSTOFFICE_CLI
  const mod = await import(pathToFileURL(pluginAt).href)
  const prompts = []
  const plugin = await mod.PostofficePlugin({
    client: {
      session: {
        get: async () => ({ data: { directory: DIRS[name] } }),
        status: async () => ({ data: {} }),
        promptAsync: async (a) => { prompts.push(a) },
      },
    },
    directory: DIRS[name],
  })
  mounted.add(plugin)
  return {
    plugin, prompts,
    ctx: () => ({
      sessionID: SID[name], messageID: `msg_${name}`, agent: 'build', directory: DIRS[name],
      worktree: DIRS[name], abort: new AbortController().signal, metadata() {}, async ask() {},
    }),
    out: (r) => (typeof r === 'string' ? r : r.output),
    records: async () => (await readdir(join(poHome, 'alarms')).catch(() => [])).filter((f) => f.endsWith('.json')).sort(),
    logText: () => readFile(join(poHome, 'logs/opencode_plugin.log'), 'utf8').catch(() => ''),
    settle: async (want) => { for (let i = 0; i < 60 && prompts.length < want; i++) await new Promise((r) => setTimeout(r, 50)) },
  }
}
const disposeAll = async () => {
  for (const p of mounted) { try { await p.dispose() } catch {} }
  mounted.clear()
}

let n = 0
const t = async (label, fn) => {
  try { await fn(); console.log('ok   -', label); n++ }
  catch (e) { console.log('FAIL -', label, '::', e.message); process.exitCode = 1 }
}

const PLUGIN_TEXT = (checkout) => readFile(join(checkout, 'opencode/postoffice.ts'), 'utf8')

// =============================================================== ① temp HOME install
const coA = await mkCheckout('A', null)
const homeA = await mkHome('A')
runInstall(coA, homeA)
const installedAt = (home) => join(home, '.config/opencode/plugins/postoffice.ts')
const cliLink = (home) => join(home, '.local/bin/postoffice')

await t('① 装到 temp HOME 的 OpenCode 插件目录，且与那份 checkout 一字不差', async () => {
  assert.equal(await readFile(installedAt(homeA), 'utf8'), await PLUGIN_TEXT(coA),
    '装出来的必须就是这份 checkout 的插件')
})

await t('① 同一个 install 同时把 ~/.local/bin/postoffice 指回这份 checkout', async () => {
  assert.equal(await realpath(cliLink(homeA)), await realpath(join(coA, 'postoffice')),
    'CLI 必须与插件同源')
  await access(await realpath(cliLink(homeA)), constants.X_OK)
})

// =============================================================== ② installed plugin drives that CLI
const poHomeA = join(homeA, 'agent-postoffice')
await register('instA', poHomeA)
await mkdir(join(DIRS.instA, '.opencode'), { recursive: true })
// The only CLI this plugin layout can reach is the symlink: plugins/postoffice.ts has no script
// above it, and the post office home does not ship one.
await assert.rejects(() => access(join(homeA, '.config/opencode/postoffice')))
await assert.rejects(() => access(join(poHomeA, 'postoffice')))
const mA = await mount(installedAt(homeA), { name: 'instA', home: homeA, poHome: poHomeA })

await t('② 装好的插件能通过 symlink 用上同源 CLI，落一条真记录', async () => {
  const text = mA.out(await mA.plugin.tool.postoffice_alarm_schedule.execute({ delay_minutes: 30 }, mA.ctx()))
  assert.match(text, /已设/, `应设成功：${text}`)
  const files = await mA.records()
  assert.equal(files.length, 1, `记录应由可执行的 CLI 落盘：${files}`)
  const rec = JSON.parse(await readFile(join(poHomeA, 'alarms', files[0]), 'utf8'))
  assert.equal(rec.session, SID.instA)
  assert.equal(rec.state, 'pending')
  const cancelled = mA.out(await mA.plugin.tool.postoffice_alarm_cancel.execute({}, mA.ctx()))
  assert.match(cancelled, /已取消/, `取消应成功：${cancelled}`)
  assert.deepEqual(await mA.records(), [], '取消后不留记录')
})

// =============================================================== ③ second checkout moves both
const coB = await mkCheckout('B', 'MARKER-B')
const homeB = join(T, 'B', 'home')
await mkdir(join(homeB, '.config/opencode'), { recursive: true })
runInstall(coB, homeB)
await t('③ 从第二份 checkout 重装：插件与 CLI 一起切过去，不错配', async () => {
  assert.equal(await readFile(installedAt(homeB), 'utf8'), await PLUGIN_TEXT(coB),
    '插件应当换成 B 那份')
  assert.equal(await realpath(cliLink(homeB)), await realpath(join(coB, 'postoffice')),
    'CLI 也应当换成 B 那份')
  assert.notEqual(await realpath(cliLink(homeB)), await realpath(join(coA, 'postoffice')),
    '不该还指着 A 的 CLI')
  assert.notEqual(await readFile(installedAt(homeB), 'utf8'), await readFile(installedAt(homeA), 'utf8'),
    '两份 checkout 的插件内容本就该不同（marker），否则这条断言没有分辨力')
})

// Same guarantee when the subcommand is run on its own, which is the contract T0 names. Running
// it from B over an install from A is the mismatch test: only the installer can move the symlink.
await t('③ 单独跑 postoffice install opencode：第二份 checkout 装完，plugin 与 CLI 一起过去', async () => {
  const homeC = await mkHome('C')
  runInstall(coA, homeC)
  assert.equal(await realpath(cliLink(homeC)), await realpath(join(coA, 'postoffice')), '先由 A 当值')
  execFileSync(join(coB, 'postoffice'), ['install', 'opencode'],
    { env: { ...process.env, HOME: homeC, XDG_CONFIG_HOME: join(homeC, '.config') },
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  assert.equal(await readFile(installedAt(homeC), 'utf8'), await PLUGIN_TEXT(coB), '插件应当换成 B 那份')
  assert.equal(await realpath(cliLink(homeC)), await realpath(join(coB, 'postoffice')),
    'CLI 也必须跟着换，否则就是插件用着别的 checkout 的 CLI')
})

// =============================================================== ④ checkout dev mode
const coDev = await mkCheckout('dev', null)
const homeDev = await mkHome('dev')
const poHomeDev = join(homeDev, 'agent-postoffice')
await register('dev', poHomeDev)
await mkdir(join(DIRS.dev, '.opencode'), { recursive: true })
const mDev = await mount(join(coDev, 'opencode/postoffice.ts'), { name: 'dev', home: homeDev, poHome: poHomeDev })

await t('④ checkout 开发模式：没有 symlink 也能设/取消，用的是同目录那份脚本', async () => {
  await assert.rejects(() => access(cliLink(homeDev)), '这个 HOME 刻意没有 symlink')
  const text = mDev.out(await mDev.plugin.tool.postoffice_alarm_schedule.execute({ delay_minutes: 45 }, mDev.ctx()))
  assert.match(text, /已设/, `应设成功：${text}`)
  assert.equal((await mDev.records()).length, 1)
  const cancelled = mDev.out(await mDev.plugin.tool.postoffice_alarm_cancel.execute({}, mDev.ctx()))
  assert.match(cancelled, /已取消/, `取消应成功：${cancelled}`)
  assert.deepEqual(await mDev.records(), [])
})

// =============================================================== ⑤ the schema itself
const scheduleOf = (m) => m.plugin.tool.postoffice_alarm_schedule
const parseDelay = (m, value) => {
  const field = scheduleOf(m).args.delay_minutes
  // Validate the way the framework does: through the standard-schema interface the schema carries
  // itself. No zod instance is invented here, and the shape must be a real Zod schema all the same.
  assert.equal(typeof field?.parse, 'function', `args.delay_minutes 必须能 parse：${JSON.stringify(field)}`)
  assert.notEqual(field?._zod, undefined, 'args.delay_minutes 必须真的是 Zod schema')
  return field['~standard'].validate(value)
}

await t('⑤ schema 接受边界值 1 与 1440', async () => {
  for (const v of [1, 1440]) {
    const r = parseDelay(mA, v)
    assert.equal(r.value, v, `${v} 分钟应当合法：${JSON.stringify(r.issues)}`)
  }
})

for (const [label, v] of [['0', 0], ['1441', 1441], ['小数 1.5', 1.5], ['字符串 "30"', '30']]) {
  await t(`⑤ schema 拒绝 ${label}`, async () => {
    const r = parseDelay(mA, v)
    assert.ok(r.issues && r.issues.length, `${JSON.stringify(v)} 必须被 schema 拒绝，不该流进 execute`)
  })
}

// =============================================================== ⑥ no schema dependency -> delivery
// Same installed layout, but no node_modules anywhere above the plugin file, so the official
// `@opencode-ai/plugin` cannot be resolved. The alarm tools must go; 投信 must not.
const homeNoZod = await mkHome('nozod', { withDeps: false })
const installedNoZod = installedAt(homeNoZod)
await mkdir(dirname(installedNoZod), { recursive: true })
await cp(join(REPO, 'opencode/postoffice.ts'), installedNoZod)
const poHomeNoZod = join(homeNoZod, 'agent-postoffice')
await register('nozod', poHomeNoZod)
await mkdir(join(DIRS.nozod, '.opencode'), { recursive: true })
const mNoZod = await mount(installedNoZod, { name: 'nozod', home: homeNoZod, poHome: poHomeNoZod, withDeps: false })

await t('⑥ 拿不到 @opencode-ai/plugin 时闹钟工具不注册，投信照旧', async () => {
  assert.equal(mNoZod.plugin.tool, undefined, '不该注册一个框架无法校验的工具')
  const letter = '20260101-000001_nozod.md'
  await writeFile(join(poHomeNoZod, 'nozodbox', 'inbox', letter),
    '来源：boss\n事由：试投\n需要：仅告知\n\n这封信不该被工具缺席影响\n')
  await mNoZod.plugin.event({ event: { type: 'session.idle' } })
  await mNoZod.settle(1)
  // wake-coalesce 合同：投递选择唯一归 wake-plan CLI；CLI 不在（本夹具没装）→ fail-closed：
  // 不投递、信保留、留可诊断日志，插件本体照常存活。旧「投信照旧」的回退规则已按票删除。
  assert.equal(mNoZod.prompts.length, 0, 'CLI 不可用就不得投递（fail-closed，不退回旧规则）')
  assert.match(await mNoZod.logText(), /闹钟工具未注册/, '日志里要写明为什么没注册')
  assert.match(await mNoZod.logText(), /wake-plan/, '日志里要写明投递选择器不可用')
})

// =============================================================== ⑦ no CLI -> a diagnosis, not a crash
// Tools registered (dependencies present) but nothing to run: no symlink, no script above the
// plugin, nothing in the post office home.
const homeNoCli = await mkHome('nocli')
const installedNoCli = installedAt(homeNoCli)
await mkdir(dirname(installedNoCli), { recursive: true })
await cp(join(REPO, 'opencode/postoffice.ts'), installedNoCli)
const poHomeNoCli = join(homeNoCli, 'agent-postoffice')
await register('nocli', poHomeNoCli)
await mkdir(join(DIRS.nocli, '.opencode'), { recursive: true })
const mNoCli = await mount(installedNoCli, { name: 'nocli', home: homeNoCli, poHome: poHomeNoCli })

await t('⑦ 找不到 CLI 时返回可读的诊断，而不是抛异常，也不打挂插件', async () => {
  assert.ok(mNoCli.plugin.tool, '有依赖时工具应当注册')
  const r = await mNoCli.plugin.tool.postoffice_alarm_schedule.execute({ delay_minutes: 30 }, mNoCli.ctx())
  const text = mNoCli.out(r)
  assert.match(text, /没设闹钟/, `应当是工具自己的话：${text}`)
  assert.match(text, /可执行文件/, `要指出可执行文件找不到：${text}`)
  assert.match(text, /POSTOFFICE_CLI/, `要给出可操作的出路：${text}`)
  assert.deepEqual(await mNoCli.records(), [], '没有 CLI 就不该有记录')
  // the plugin itself is still healthy: the delivery hook still runs and dispose still works
  const letter = '20260101-000002_nocli.md'
  await writeFile(join(poHomeNoCli, 'noclibox', 'inbox', letter), '来源：boss\n事由：试投\n需要：仅告知\n\n正文\n')
  await mNoCli.plugin.event({ event: { type: 'session.idle' } })
  await mNoCli.settle(1)
  // fail-closed：没有 CLI 就没有唤醒选择，信留在 inbox 并留诊断；插件存活（dispose 可用）
  assert.equal(mNoCli.prompts.length, 0, 'CLI 缺席→不投递（fail-closed），信保留')
  assert.match(await mNoCli.logText(), /wake-plan/, '诊断要写明 wake-plan 不可用')
})

// =============================================================== ⑦ (negative) a foreign CLI
// Same as ⑦, except something *is* sitting where a careless resolver would look: a `postoffice`
// under the post office home. The post office home is per-operator state rather than a checkout, so
// that file could be any checkout's, and using it is exactly the "plugin and CLI from different
// checkouts" this must never do. The stub announces itself and leaves a marker, so "it was not
// called" is an observation rather than an inference from the absence of a record.
const homeForeign = await mkHome('foreign')
const installedForeign = installedAt(homeForeign)
await mkdir(dirname(installedForeign), { recursive: true })
await cp(join(REPO, 'opencode/postoffice.ts'), installedForeign)
const poHomeForeign = join(homeForeign, 'agent-postoffice')
const boxForeign = await register('foreign', poHomeForeign)
await mkdir(join(DIRS.foreign, '.opencode'), { recursive: true })
const markerForeign = join(poHomeForeign, 'FOREIGN-CLI-WAS-CALLED')
const stubForeign = join(poHomeForeign, 'postoffice')
// exit 0 on purpose: were this stub called, the tool would report a *successful* alarm and leave no
// error text at all, which is precisely the failure this negative case is about.
await writeFile(stubForeign, `#!/bin/sh\ntouch "${markerForeign}"\necho "STUB-FOREIGN-CHECKOUT-CLI $*"\nexit 0\n`)
await chmod(stubForeign, 0o755)
const mForeign = await mount(installedForeign, { name: 'foreign', home: homeForeign, poHome: poHomeForeign })

await t('⑦ 负例：邮局目录里那份外来 CLI 不会被调用，只得到可诊断的报错', async () => {
  const text = mForeign.out(await mForeign.plugin.tool.postoffice_alarm_schedule.execute({ delay_minutes: 30 }, mForeign.ctx()))
  assert.ok(!existsSync(markerForeign), '外来 checkout 的 CLI 绝不能被调用（桩一旦被跑就会留下这个文件）')
  assert.doesNotMatch(text, /STUB-FOREIGN-CHECKOUT-CLI/, `不能出现外来 CLI 的自报身份：${text}`)
  assert.match(text, /没设闹钟/, `应当是工具自己的话：${text}`)
  assert.match(text, /可执行文件/, `要指出可执行文件找不到：${text}`)
  // 从安装位置加载时只该找 ~/.local/bin/postoffice —— 同级那份不是安装 checkout
  assert.match(text, /从安装位置加载/, `要说清自己是在哪种布局下找的：${text}`)
  assert.match(text, /只找过 ~\/\.local\/bin\/postoffice/, `要说清只找过哪条：${text}`)
  assert.doesNotMatch(text, /\.\.\/postoffice/, `安装位置不该提同级那份：${text}`)
  assert.match(text, /POSTOFFICE_CLI/, `要给出可操作的出路：${text}`)
  assert.deepEqual(await mForeign.records(), [], '不该有任何记录')
  // and the plugin is still alive: delivery works
  const letter = '20260101-000003_foreign.md'
  await writeFile(join(poHomeForeign, boxForeign, 'inbox', letter), '来源：boss\n事由：试投\n需要：仅告知\n\n正文\n')
  await mForeign.plugin.event({ event: { type: 'session.idle' } })
  await mForeign.settle(1)
  // fail-closed：不退回任何同级/外来 CLI，唤醒选择器缺席就留信＋诊断；桩必须仍未被调用
  assert.ok(!existsSync(markerForeign), '投递路径同样绝不能碰外来 CLI 桩')
  assert.equal(mForeign.prompts.length, 0, '无合法 wake-plan→不投递（fail-closed），信保留')
  assert.match(await mForeign.logText(), /wake-plan/, '诊断要写明 wake-plan 不可用')
})

// =============================================================== ① (fail-closed) link failure
// `postoffice install opencode` promises the plugin and the CLI come from the same checkout. When it
// cannot keep that promise the command must fail: reporting success would leave a plugin wired to a
// foreign CLI and nothing would ever notice. Reproducible injection: ~/.local/bin/postoffice is
// occupied by a directory, so the link cannot be created (and does not depend on file permissions,
// so it fails the same way for any user).
const homeFail = await mkHome('fail')
const failCheckout = join(T, 'fail', 'checkout')
await mkdir(join(failCheckout, 'opencode'), { recursive: true })
await cp(REAL_CLI, join(failCheckout, 'postoffice'))
await chmod(join(failCheckout, 'postoffice'), 0o755)
await cp(join(REPO, 'opencode/postoffice.ts'), join(failCheckout, 'opencode/postoffice.ts'))
await mkdir(join(homeFail, '.local/bin/postoffice'), { recursive: true })   // squatting the link path

let failRc = 0, failOut = ''
try {
  failOut = execFileSync(join(failCheckout, 'postoffice'), ['install', 'opencode'],
    { env: { ...process.env, HOME: homeFail, XDG_CONFIG_HOME: join(homeFail, '.config') },
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
} catch (e) {
  failRc = e.status ?? -1
  failOut = `${e.stdout ?? ''}${e.stderr ?? ''}`
}

await t('① 链接更新不了时 install opencode 非零退出，且不说“装好了”', async () => {
  assert.notEqual(failRc, 0, `链接失败必须以非零退出，实际 rc=${failRc}，输出：${failOut}`)
  assert.match(failOut, /没有安装/, `诊断要明说这次没装：${failOut}`)
  assert.match(failOut, /CLI 链接没能更新成同源/, `诊断要说清链接没跟上：${failOut}`)
  assert.match(failOut, /插件一个字节都没动/, `诊断要明说插件没被动过：${failOut}`)
  assert.match(failOut, /POSTOFFICE_CLI=/, `诊断要给出路：${failOut}`)
  assert.doesNotMatch(failOut, /已装入/, `不许出现宣称安装成功的措辞：${failOut}`)
  assert.doesNotMatch(failOut, /重启 OpenCode 生效/, `不许出现宣称安装成功的措辞：${failOut}`)
  // Nothing landed: the link is fixed before the plugin is touched, so a link failure leaves the
  // install untouched rather than half-applied.
  await assert.rejects(() => access(installedAt(homeFail)), '插件不该被复制进来')
  const linkStat = await lstat(join(homeFail, '.local/bin/postoffice'))
  assert.ok(linkStat.isDirectory(),
    `链接路径仍应是那个占位目录，实际是 ${linkStat.isSymbolicLink() ? 'symlink' : '别的东西'}`)
  assert.deepEqual((await readdir(join(homeFail, '.local/bin'))).sort(), ['postoffice'],
    '不该留下半截的临时链接')
})

// =============================================================== ② (negative) the adjacent decoy
// Same category as ②, and the decoy is the one that actually sits next to an installed plugin:
// $XDG_CONFIG_HOME/opencode/postoffice — one level above plugins/, exactly where `../postoffice`
// points from the installed copy. The correct ~/.local/bin/postoffice symlink is also in place and
// does work. So whichever candidate is picked is visible: the symlink's CLI announces itself and
// then delegates to the real CLI (so the alarm is really written), while the decoy only announces
// itself and does nothing.
const homeSibling = await mkHome('sibling')
const installedSibling = installedAt(homeSibling)
await mkdir(dirname(installedSibling), { recursive: true })
await cp(join(REPO, 'opencode/postoffice.ts'), installedSibling)
const coSibling = await mkCheckout('sibling', null)
await mkdir(join(homeSibling, '.local/bin'), { recursive: true })
const markerSibling = join(T, 'SIBLING-STUB-CALLED')
const markerSymlink = join(T, 'SYMLINK-CLI-CALLED')
const decoySibling = join(homeSibling, '.config/opencode/postoffice')
await writeFile(decoySibling, `#!/bin/sh\ntouch "${markerSibling}"\necho "STUB-ADJACENT-DECOY-CLI $*"\nexit 0\n`)
await chmod(decoySibling, 0o755)
// ~/.local/bin/postoffice: the correct handle, made self-identifying, and it delegates to the real
// CLI of that checkout so the alarm really lands when (and only when) this path is taken.
const symlinkCli = join(T, 'symlink-cli.sh')
await writeFile(symlinkCli, `#!/bin/sh\ntouch "${markerSymlink}"\nexec "${join(coSibling, 'postoffice')}" "$@"\n`)
await chmod(symlinkCli, 0o755)
await symlink(symlinkCli, cliLink(homeSibling))
const poHomeSibling = join(homeSibling, 'agent-postoffice')
const boxSibling = await register('sibling', poHomeSibling)
await mkdir(join(DIRS.sibling, '.opencode'), { recursive: true })
const mSibling = await mount(installedSibling, { name: 'sibling', home: homeSibling, poHome: poHomeSibling })

await t('② 负例：安装位置同级那份外来可执行文件不会被调用，用的是同源 symlink', async () => {
  const text = mSibling.out(await mSibling.plugin.tool.postoffice_alarm_schedule.execute({ delay_minutes: 30 }, mSibling.ctx()))
  assert.ok(!existsSync(markerSibling), '同级那份外来可执行文件绝不能被调用（桩一旦被跑就会留下这个文件）')
  assert.ok(existsSync(markerSymlink), '应当走 ~/.local/bin/postoffice 这条同源 symlink')
  assert.doesNotMatch(text, /STUB-ADJACENT-DECOY-CLI/, `不能出现同级桩的自报身份：${text}`)
  assert.match(text, /已设/, `应当真的设成功（symlink 那条链是真 CLI）：${text}`)
  assert.equal((await mSibling.records()).length, 1, '记录应当由 symlink 指向的 CLI 落盘')
})

// =============================================================== ③ (negative) a failed switch
// A is installed and matched (plugin A + link A). Now B tries to install and its link update fails.
// Either outcome is acceptable as long as it is not a mismatch; this asserts the one this
// implementation aims for — nothing moved, so A's pair is still A's pair.
const homePair = await mkHome('pair')
const coPairA = await mkCheckout('pairA', 'MARKER-PAIR-A')
const coPairB = await mkCheckout('pairB', 'MARKER-PAIR-B')
const envFor = (home) => ({
  ...process.env, HOME: home, XDG_CONFIG_HOME: join(home, '.config'), POSTOFFICE_NO_NOTIFY: '1',
})
const runSub = (checkout, home) =>
  execFileSync(join(checkout, 'postoffice'), ['install', 'opencode'],
    { env: envFor(home), encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })

runSub(coPairA, homePair)
const binDir = join(homePair, '.local/bin')
await chmod(binDir, 0o555)                       // B's link cannot be created: EACCES, A's link survives
let pairRc = 0, pairOut = ''
try { runSub(coPairB, homePair) } catch (e) { pairRc = e.status ?? -1; pairOut = `${e.stdout ?? ''}${e.stderr ?? ''}` }
await chmod(binDir, 0o755)                       // restore so the recovery step can run

await t('③ 负例：换 checkout 时链接失败，A 的配对原封不动（插件=A、link=A），不是错配', async () => {
  assert.notEqual(pairRc, 0, `B 的安装必须失败，实际 rc=${pairRc}，输出：${pairOut}`)
  assert.match(pairOut, /没有安装/, `诊断要明说没装：${pairOut}`)
  assert.doesNotMatch(pairOut, /已装入/, `不许宣称成功：${pairOut}`)
  // the invariant: plugin content and link target are still A's, together
  const pluginText = await readFile(installedAt(homePair), 'utf8')
  assert.equal(pluginText, await PLUGIN_TEXT(coPairA), '插件必须还是 A 那份，不能是 B 的')
  assert.ok(!pluginText.includes('MARKER-PAIR-B'), '绝不能把 B 的插件留在这儿')
  assert.equal(await realpath(cliLink(homePair)), await realpath(join(coPairA, 'postoffice')),
    '链接必须还指着 A，不能是 B')
})

await t('③ 负例：故障排除后重跑 B，插件与链接一起切到 B', async () => {
  runSub(coPairB, homePair)
  assert.equal(await readFile(installedAt(homePair), 'utf8'), await PLUGIN_TEXT(coPairB),
    '这次应当真的装成 B')
  assert.equal(await realpath(cliLink(homePair)), await realpath(join(coPairB, 'postoffice')),
    '链接也应当一起切到 B')
})

// =============================================================== ③ (negative) install.sh must not switch early
// The whole point of the transaction inside `postoffice install opencode` is that a failed plugin copy
// leaves the link where it was. Running the installer end to end is what proves nothing above it
// gets there first: install.sh used to do its own `ln -sfn` before calling the subcommand, which
// moved the link to B up front, so B failing to copy left link=B next to plugin=A — the transaction
// could not undo a switch that happened before it started.
//
// The failure is injected at the copy step, uid-independently: checkout B's
// opencode/postoffice.ts is made a directory. shutil.copy2 opens the source for reading, and
// reading a directory fails with EISDIR for every user, root included — unlike a permission bit.
// A's installed plugin file is never touched by the injection, so "plugin is still A" stays a real
// assertion rather than a tautology.
const homeSw = await mkHome('sw')
const swA = await mkCheckout('swA', 'MARKER-SW-A')
const swB = await mkCheckout('swB', 'MARKER-SW-B')
const swBPlugin = join(swB, 'opencode/postoffice.ts')
const swBSource = await readFile(swBPlugin)          // keep B's real plugin to restore later

runInstall(swA, homeSw)
await t('③ 前置：A 用 install.sh 装好，plugin 与 link 都是 A', async () => {
  assert.ok((await readFile(installedAt(homeSw), 'utf8')).includes('MARKER-SW-A'), 'plugin=A')
  assert.equal(await realpath(cliLink(homeSw)), await realpath(join(swA, 'postoffice')), 'link=A')
})

await rm(swBPlugin, { recursive: true, force: true })
await mkdir(join(swBPlugin, 'blocker'), { recursive: true })     // a directory where a file must be
const swFail = runInstallRaw(swB, homeSw)

await t('③ 负例：B 的 install.sh 插件复制失败时，plugin 与 link 都还是 A（不错配）', async () => {
  assert.notEqual(swFail.rc, 0, `install.sh 必须非零退出，实际 rc=${swFail.rc}：${swFail.out}`)
  assert.doesNotMatch(swFail.out, /安装完成/, `失败时不许打出成功收尾：${swFail.out}`)
  const pluginText = await readFile(installedAt(homeSw), 'utf8')
  assert.ok(pluginText.includes('MARKER-SW-A'), '插件必须还是 A 那份')
  assert.ok(!pluginText.includes('MARKER-SW-B'), '绝不能把 B 的插件留在这儿')
  assert.equal(await realpath(cliLink(homeSw)), await realpath(join(swA, 'postoffice')),
    '链接必须还指着 A —— 提前切换正是这条要防的')
  assert.deepEqual((await readdir(join(homeSw, '.local/bin'))).sort(), ['postoffice'],
    '不该留下半截的临时链接')
})

await t('③ 负例：排除故障后重跑 B 的 install.sh，plugin 与 link 一起切到 B', async () => {
  await rm(swBPlugin, { recursive: true, force: true })
  await writeFile(swBPlugin, swBSource)                          // B 恢复正常
  runInstall(swB, homeSw)
  assert.ok((await readFile(installedAt(homeSw), 'utf8')).includes('MARKER-SW-B'), 'plugin=B')
  assert.equal(await realpath(cliLink(homeSw)), await realpath(join(swB, 'postoffice')), 'link=B')
})

await disposeAll()
console.log(process.exitCode ? 'FAIL' : `PASS (${n} checks)`)
// A plugin that failed in an unexpected way may leave a setInterval no dispose can clear. Report the
// result and leave, rather than hanging the suite with no output.
await new Promise((r) => process.stdout.write('', r))
process.exit(process.exitCode ? 1 : 0)
