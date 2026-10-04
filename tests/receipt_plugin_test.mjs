// Exercise the exported plugin's event seam without an actual model invocation.
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
const root = await mkdtemp(join(tmpdir(), 'postoffice-receipt-plugin-'))
process.env.POSTOFFICE_HOME = root
const { PostofficePlugin } = await import('../opencode/postoffice.ts')
let plugin
try {
  await mkdir(join(root, 'alice/inbox'), { recursive: true })
  const routes = { alice: { methods: ['opencode_plugin'], session_id: 'session-1', status: 'offline' } }
  await writeFile(join(root, 'routes.json'), JSON.stringify(routes))
  await writeFile(join(root, 'alice/inbox/receipt.md'), '来源：bob\n事由：回执：核查结果\n需要：回执（默认不答复）\n回执：original\n配置正常，测试未检查\n')
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
  assert.match(text, /配置正常，测试未检查/)
  assert.match(text, /默认不答复、不再 ack/)
  assert.doesNotMatch(text, /原信仍在等待答复/)
  await scan()
  assert.equal(prompts.length, 1, 'same receipt never redelivered')
  console.log('PASS: offline, busy, idle receipt content, no-reply instruction, dedup')
} finally {
  if (plugin) await plugin.dispose()
  await rm(root, { recursive: true, force: true })
}
