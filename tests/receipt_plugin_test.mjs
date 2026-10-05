// Exercise the exported plugin's event seam without an actual model invocation.
// v1.4: a receipt reminder must be metadata-only (lookup ID + command), never the receipt body.
// v1.6: formal letters go first; a receipt that has waited long enough rides along as one merged block.
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, rm, utimes } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
const root = await mkdtemp(join(tmpdir(), 'postoffice-receipt-plugin-'))
process.env.POSTOFFICE_HOME = root
const { PostofficePlugin } = await import('../opencode/postoffice.ts')
const SENTINEL = 'PLUGIN-SENTINEL-BODY-4c1d'
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
  const prompts = []
  plugin = await PostofficePlugin({ directory: root, client: { session: {
    get: async () => ({ data: { directory: root } }),
    status: async () => ({ data: busy ? { 'session-1': { type: 'busy' } } : {} }),
    promptAsync: async (p) => prompts.push(p),
  } } })
  const scan = async () => {
    await plugin.event({ event: { type: 'session.idle' } })
    // Event launches its async scan; allow local fs IO to settle.
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  await scan()
  assert.equal(prompts.length, 0, 'offline: no prompt')
  routes.alice.status = 'online'
  await writeFile(join(root, 'routes.json'), JSON.stringify(routes))
  await scan()
  assert.equal(prompts.length, 0, 'busy: no prompt')
  busy = false
  await scan()
  assert.equal(prompts.length, 1, 'idle: receipt prompt')
  const text = prompts[0].body.parts[0].text
  assert.doesNotMatch(text, new RegExp(SENTINEL), 'receipt body must not be injected')
  assert.match(text, /【联络总站回执｜alice】/)
  assert.match(text, /查询：postoffice receipt 'alice' 'original'/, 'command args are POSIX-quoted')
  assert.match(text, /默认不答复/)
  assert.equal((text.match(/original/g) || []).length, 1, 'the lookup id appears exactly once')
  assert.doesNotMatch(text, /归档/, 'archive command is not inlined in the reminder')
  await scan()
  assert.equal(prompts.length, 1, 'same receipt never redelivered')

  // v1.6: a pending formal letter goes first and a receipt that has waited too long rides along
  await writeFile(join(root, 'alice/inbox/20260101-000010_carol_letter.md'),
    '来源：carol\n事由：正式信\n需要：回复\n\n正文\n')
  const lateReceipt = join(root, 'alice/inbox/20260101-000011_dave_notice.md')
  await writeFile(lateReceipt,
    '来源：dave\n事由：回执：另一件\n需要：回执（默认不答复）\n回执：orig2\n原事由：另一件\n\n正文\n')
  const old = new Date(Date.now() - 20 * 60 * 1000)
  await utimes(lateReceipt, old, old)
  await scan()
  assert.equal(prompts.length, 2, 'formal letter delivered')
  const merged = prompts[1].body.parts[0].text
  assert.match(merged, /【联络总站新信｜alice】/, 'formal letter first')
  assert.match(merged, /另有 1 条回执（默认不答复，需要时按 ID 查询）：/, 'receipt merged into one trailing block')
  assert.match(merged, /postoffice receipt 'alice' 'orig2'/, 'merged block carries the lookup command')
  assert.equal((merged.match(/orig2/g) || []).length, 1, 'merged lookup id appears once')
  await scan()
  assert.equal(prompts.length, 2, 'nothing redelivered')
  console.log('PASS: offline, busy, idle metadata-only receipt, lookup command, dedup, merged trailing receipt')
} finally {
  if (plugin) await plugin.dispose()
  await rm(root, { recursive: true, force: true })
}
