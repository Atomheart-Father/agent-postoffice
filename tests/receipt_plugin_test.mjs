// Exercise the exported plugin's event seam without an actual model invocation.
// v1.4: a receipt reminder must be metadata-only (lookup ID + command), never the receipt body.
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
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
  assert.match(text, /postoffice receipt 'alice' 'original'/, 'command args are POSIX-quoted')
  assert.match(text, /归档：mv '/, 'archive command is POSIX-quoted')
  assert.match(text, /原事由：核查结果/)
  assert.match(text, /默认不答复/)
  await scan()
  assert.equal(prompts.length, 1, 'same receipt never redelivered')
  console.log('PASS: offline, busy, idle metadata-only receipt, lookup command, dedup')
} finally {
  if (plugin) await plugin.dispose()
  await rm(root, { recursive: true, force: true })
}
