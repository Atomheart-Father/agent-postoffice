import {closeSync, fstatSync, openSync, readSync} from 'node:fs';
import {basename} from 'node:path';
import {createHash} from 'node:crypto';

// W4 Claude Code usage: the hook event hands us the session's own transcript. Only what the host already
// persisted is used: one assistant `message.id` (stable, repeated on every line of that message, identical
// usage on each line) = one `usage.record` entry_id. Same rule as Pi's turn_end: the message's own
// office_work_publish / office_work_review receipts give `ref` (exactly one, else none); no cost (Claude Code
// writes none that the provider reported); a message whose tool results are not yet on disk is skipped until
// a later event, because Core keeps the first row it sees. The receipt is whatever renderToolResult leaves in
// the model-side text (compact JSON, `ok` stripped); a refusal renders as prose and simply is not JSON.
// Called from SessionStart and UserPromptSubmit only: by the next prompt every earlier tool result is on disk.
const WINDOW = 4 * 1024 * 1024;
const RECEIPT = {office_work_publish: 'sub_ref', office_work_review: 'review_ref'};

export function tail(path) {
  let fd; try { fd = openSync(path, 'r'); } catch { return []; }
  try {
    const size = fstatSync(fd).size, start = Math.max(0, size - WINDOW), buf = Buffer.alloc(size - start);
    readSync(fd, buf, 0, buf.length, start);
    return buf.toString('utf8').split('\n');     // a line cut by the window fails to parse and is skipped
  } finally { closeSync(fd); }
}

export function claudeUsageEntries(path, session) {
  if (typeof path !== 'string' || basename(path) !== `${session}.jsonl`) return [];
  const msgs = new Map(), results = new Map();
  for (const line of tail(path)) {
    let r; try { r = JSON.parse(line); } catch { continue; }
    const m = r?.message;
    if (r?.type === 'assistant' && m?.id && Array.isArray(m.content)) {
      const e = msgs.get(m.id) || {m, at: r.timestamp, uses: []};
      for (const c of m.content) if (c.type === 'tool_use') e.uses.push([c.id, String(c.name).split('__').pop()]);
      msgs.set(m.id, e);
    } else if (r?.type === 'user' && Array.isArray(m?.content)) {
      for (const c of m.content) if (c.type === 'tool_result') results.set(c.tool_use_id, c);
    }
  }
  const out = [];
  for (const [id, {m, at, uses}] of msgs) {
    const u = m.usage || {};
    const written = u.cache_creation_input_tokens;
    const input = Number.isSafeInteger(u.input_tokens)
      ? u.input_tokens + (Number.isSafeInteger(written) && written >= 0 ? written : 0) : undefined;
    const tokens = {in: input, out: u.output_tokens, cache: u.cache_read_input_tokens};
    if (!m.stop_reason || !Object.values(tokens).some(Boolean)) continue;
    if (!uses.every(([uid]) => results.has(uid))) continue;
    const refs = new Set();
    for (const [uid, name] of uses) {
      const c = results.get(uid), key = RECEIPT[name];
      if (!key) continue;
      let body = c.content; if (Array.isArray(body)) body = body.map(x => x?.text || '').join('');
      try { const ref = JSON.parse(body)?.[key]; if (typeof ref === 'string' && ref) refs.add(ref); } catch { /* refusals are prose, not a receipt */ }
    }
    const entry = {entry_id: id, tokens};
    if (refs.size === 1) entry.ref = [...refs][0];
    if (typeof m.model === 'string' && m.model) entry.model = m.model;
    if (Date.parse(at)) entry.at = Date.parse(at) / 1000;
    out.push(entry);
  }
  return out.slice(-200);
}

export async function reportClaudeUsage(client, event) {
  try {
    const entries = claudeUsageEntries(event?.transcript_path, event?.session_id);
    if (!entries.length) return;
    // Same entries ⇒ same key (Core replays); new entries ⇒ new key; known rows are ignored by entry_id.
    await client.usageRecord(entries, 'usage:claude:' + createHash('sha256').update(JSON.stringify(entries)).digest('hex').slice(0, 32));
  } catch { /* observation never changes the hook's outcome */ }
}
