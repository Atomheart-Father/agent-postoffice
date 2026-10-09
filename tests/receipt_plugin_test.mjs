// Exercise the exported plugin's event seam without an actual model invocation.
// v1.4: a receipt reminder must be metadata-only (lookup ID + command), never the receipt body.
// v1.6: formal letters go first; a receipt that has waited long enough rides along as one merged block.
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, readFile, appendFile, chmod, rm, utimes } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
const root = await mkdtemp(join(tmpdir(), 'postoffice-receipt-plugin-'))
process.env.POSTOFFICE_HOME = root
const { PostofficePlugin } = await import('../opencode/postoffice.ts')

// 这个文件只测投递通道，不碰那两个闹钟工具，所以不依赖 zod 接缝（闹钟工具的 args schema 由
// tests/alarm_install_test.mjs 负责）。找不到 zod 时投递照旧，只是工具不注册。
const SENTINEL = 'PLUGIN-SENTINEL-BODY-4c1d'
const LEDGER = join(root, 'opencode_delivered.jsonl')
process.env.POSTOFFICE_NO_NOTIFY = '1' // 只留日志里的“通知人”记录，测试不弹系统通知
const rows = async () => (await readFile(LEDGER, 'utf8')).split('\n').filter(Boolean).map((l) => JSON.parse(l))
const rowFor = async (file) => (await rows()).filter((r) => r.file === file).pop()
const logText = async () => readFile(join(root, 'logs/opencode_plugin.log'), 'utf8').catch(() => '')
// 事件处理是异步起 scan 的：轮询插件日志，连续两次不再增长即视为这一轮结束
const settle = async () => {
  let last = -1, stable = 0
  for (let i = 0; i < 80; i++) {
    await new Promise((r) => setTimeout(r, 50))
    const size = (await logText()).length
    if (size === last) { if (++stable >= 2) return } else { stable = 0; last = size }
  }
}
const mkInstance = (arr, fail) => PostofficePlugin({ directory: root, client: { session: {
  get: async () => ({ data: { directory: root } }),
  status: async () => ({ data: {} }),
  promptAsync: async (p) => { if (fail()) throw new Error('mock delivery failure'); arr.push(p) },
} } })
const scanner = (pl) => async () => {
  await pl.event({ event: { type: 'session.idle' } })
  await settle()
}
let plugin
try {
  await mkdir(join(root, 'alice/inbox'), { recursive: true })
  const routes = { alice: { methods: ['opencode_plugin'], session_id: 'session-1', status: 'offline' } }
  await writeFile(join(root, 'routes.json'), JSON.stringify(routes))
  // legacy-style receipt notification: it still contains the body, which must never be injected
  await writeFile(join(root, 'alice/inbox/receipt.md'),
    '来源：bob\n事由：回执：核查结果\n需要：回执（默认不答复）\n回执：original\n原事由：核查结果\n' +
    `回执内容：${SENTINEL}\n`)
  let busy = true
  let boom = false // 模拟投递失败（promptAsync 抛错）
  const prompts = []
  plugin = await PostofficePlugin({ directory: root, client: { session: {
    get: async () => ({ data: { directory: root } }),
    status: async () => ({ data: busy ? { 'session-1': { type: 'busy' } } : {} }),
    promptAsync: async (p) => { if (boom) throw new Error('mock delivery failure'); prompts.push(p) },
  } } })
  const ageFile = async (fp, ms = 20 * 60 * 1000) => {
  const old = new Date(Date.now() - ms)
  await utimes(fp, old, old)
}
const scan = async () => {
    await plugin.event({ event: { type: 'session.idle' } })
    await settle() // 事件处理异步起 scan；等它真的做完
  }
  await scan()
  assert.equal(prompts.length, 0, 'offline: no prompt')
  routes.alice.status = 'online'
  await writeFile(join(root, 'routes.json'), JSON.stringify(routes))
  await scan()
  assert.equal(prompts.length, 0, 'busy: no prompt')
  busy = false
  await scan()
  // wake-coalesce 合同：普通回执永不单独唤醒——留 inbox、不认领、不标展示
  assert.equal(prompts.length, 0, 'lone receipt: no wake, letter stays')
  assert.ok(existsSync(join(root, 'alice/inbox/receipt.md')), 'the receipt is kept for the next ride')

  // 一封「会话离开期间到达」的正式信出发时，回执搭车：一个尾部合并块，只有元数据
  const carol = join(root, 'alice/inbox/20260101-000010_carol_letter.md')
  await writeFile(carol, '来源：carol\n事由：正式信\n需要：回复\n\n正文\n')
  await ageFile(carol)
  const lateReceipt = join(root, 'alice/inbox/20260101-000011_dave_notice.md')
  await writeFile(lateReceipt,
    '来源：dave\n事由：回执：另一件\n需要：回执（默认不答复）\n回执：orig2\n原事由：另一件\n\n正文\n')
  await ageFile(lateReceipt)
  await scan()
  assert.equal(prompts.length, 1, 'one wake: formal letter departs, receipts ride along')
  const merged = prompts[0].body.parts[0].text
  assert.match(merged, /【联络总站新信｜alice】/, 'formal letter first')
  assert.match(merged, /另有 2 条回执（默认不答复，需要时按 ID 查询）：/, 'receipts merged into one trailing block')
  assert.match(merged, /postoffice receipt 'alice' 'original'/, 'command args are POSIX-quoted')
  assert.match(merged, /postoffice receipt 'alice' 'orig2'/, 'merged block carries the lookup command')
  assert.doesNotMatch(merged, new RegExp(SENTINEL), 'receipt body must not be injected')
  assert.match(merged, /默认不答复/)
  assert.equal((merged.match(/original/g) || []).length, 1, 'the lookup id appears exactly once')
  assert.doesNotMatch(merged, /postoffice archive/, 'no archive command is inlined in the reminder')
  await scan()
  assert.equal(prompts.length, 1, 'nothing redelivered')

  // round 2: a retry must re-claim, otherwise two live instances each redeliver
  // 注意：每个实例启动时都会自己 scan 一轮（boot），所以先摆好文件/账本再造实例，
  // 再 settle() 把 boot scan 跑完，之后每次投递都由本测试显式触发，断言才稳定。
  let plugin2 = null
  try {
    const prompts2 = []
    plugin2 = await mkInstance(prompts2, () => boom)
    const scan2 = scanner(plugin2)
    await settle() // 两个实例的 boot scan 先跑完（此时没有待投文件）
    const frank = join(root, 'alice/inbox/20260101-000020_frank_letter.md')
    await writeFile(frank, '来源：frank\n事由：需要重试的信\n需要：回复\n\n正文\n')
    await ageFile(frank)
    boom = true
    await scan()
    assert.equal(prompts.length, 1, 'a failed delivery pushes no prompt')
    const delivered = (await rows()).filter((r) => r.result === 'DELIVERED').length
    boom = false
    await Promise.all([scan(), scan2()])
    assert.equal(prompts.length + prompts2.length, 2, 'after a failure exactly one instance retries')
    assert.equal((await rows()).filter((r) => r.result === 'DELIVERED').length, delivered + 1, 'exactly one DELIVERED row for the retried letter')
    await scan(); await scan2()
    assert.equal(prompts.length + prompts2.length, 2, 'the retried letter is never delivered twice')
    assert.equal((await rows()).filter((r) => r.result === 'DELIVERED').length, delivered + 1, 'no extra DELIVERED row on a second scan')
  } finally {
    if (plugin2) await plugin2.dispose()
  }

  // round 2: one prompt per batch, but each file counts its own attempts — an already-failed
  // receipt ends at FAILED_FINAL while the fresh letter in the same batch keeps one retry
  const prompts3 = []
  let boom3 = true
  let plugin3 = null
  try {
    const oldReceipt = join(root, 'alice/inbox/20260101-000030_gina_receipt.md')
    await writeFile(oldReceipt, '来源：gina\n事由：回执：旧事\n需要：回执（默认不答复）\n回执：orig-old\n原事由：旧事\n\n正文\n')
    const stale = new Date(Date.now() - 20 * 60 * 1000)
    await utimes(oldReceipt, stale, stale) // 等过回执等待时限，才会随正式信一起投
    await appendFile(LEDGER, JSON.stringify({ time: new Date().toISOString(), box: 'alice',
      file: '20260101-000030_gina_receipt.md', session: 'session-1', result: 'FAILED_RETRYABLE', attempt: 1 }) + '\n')
    const hank = join(root, 'alice/inbox/20260101-000031_hank_letter.md')
    await writeFile(hank, '来源：hank\n事由：新鲜正式信\n需要：回复\n\n正文\n')
    await ageFile(hank)
    plugin3 = await mkInstance(prompts3, () => boom3)
    const scan3 = scanner(plugin3)
    await settle() // boot scan 触发这一批失败
    assert.equal(prompts3.length, 0, 'a failing batch pushes nothing')
    const rOld = await rowFor('20260101-000030_gina_receipt.md')
    const rNew = await rowFor('20260101-000031_hank_letter.md')
    assert.equal(rOld.result, 'FAILED_FINAL', 'the already-failed receipt ends at FAILED_FINAL')
    assert.equal(rOld.attempt, 2, 'and it is counted as its own second attempt')
    assert.equal(rNew.result, 'FAILED_RETRYABLE', 'the fresh letter keeps one retry')
    assert.equal(rNew.attempt, 1, "the fresh letter is on attempt 1, not the batch's")
    assert.match(await logText(), /通知人：.*已达上限/, 'the exhausted file is reported to the human')
    boom3 = false
    await scan3()
    assert.equal(prompts3.length, 1, 'the fresh letter is retried once and lands')
    assert.equal((await rowFor('20260101-000031_hank_letter.md')).result, 'DELIVERED')
    await scan3()
    assert.equal(prompts3.length, 1, 'the retried letter is not sent again')
  } finally {
    if (plugin3) await plugin3.dispose()
  }

  // round 2: prompt accepted but the ledger write fails — never resend, keep the claim, tell the human
  const prompts4 = []
  let plugin4 = null
  try {
    const ivy = join(root, 'alice/inbox/20260101-000040_ivy_letter.md')
    await writeFile(ivy, '来源：ivy\n事由：记账失败测试\n需要：回复\n\n正文\n')
    await ageFile(ivy)
    await chmod(LEDGER, 0o444) // 账本只读 → append 失败
    plugin4 = await mkInstance(prompts4, () => false)
    const scan4 = scanner(plugin4)
    await settle() // boot scan：投递成功、记账失败
    assert.equal(prompts4.length, 1, 'the prompt was accepted once')
    assert.equal(await rowFor('20260101-000040_ivy_letter.md'), undefined, 'no DELIVERED row was written')
    assert.ok(existsSync(join(root, 'alice/.claims/20260101-000040_ivy_letter.md')), 'the claim is kept')
    await scan4()
    assert.equal(prompts4.length, 1, 'a ledger failure must not resend the letter')
    assert.match(await logText(), /记账失败/, 'the failure is logged')
    assert.match(await logText(), /通知人：.*记账失败/, 'the human is asked to check')
  } finally {
    await chmod(LEDGER, 0o644)
    if (plugin4) await plugin4.dispose()
  }

  console.log('PASS: offline/busy/lone-receipt-no-wake, ride-along metadata-only merged block, lookup command, dedup, single-instance retry, per-file attempt counts, no resend when the ledger write fails')
} finally {
  if (plugin) await plugin.dispose()
  await rm(root, { recursive: true, force: true })
}
