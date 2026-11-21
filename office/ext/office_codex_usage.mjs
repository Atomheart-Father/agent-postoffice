import {basename} from 'node:path';
import {createHash} from 'node:crypto';
import {tail} from './office_claude_usage.mjs';

export async function reportCodexUsage(core, event) {
  const {session_id: session, turn_id: turn, transcript_path: path} = event;
  if (event.hook_event_name !== 'PostModelRequest' || typeof session !== 'string' ||
      typeof turn !== 'string' || !turn || typeof path !== 'string') return;
  const rollout = basename(path).replace(/_[\da-f-]{36}\.jsonl$/i, '.jsonl');
  if (!rollout.endsWith(`-${session}.jsonl`)) return;
  const entries = [];
  let model;
  for (const line of tail(path)) {
    let row; try { row = JSON.parse(line); } catch { continue; }
    const p = row?.payload, u = p?.usage;
    if (row?.type === 'turn_context' && p?.turn_id === turn) model = p.model;
    if (row?.type !== 'token_usage_record' || p?.session_id !== session || p?.thread_id !== session ||
        p?.turn_id !== turn || typeof p?.response_id !== 'string' || !p.response_id || !u) continue;
    const values = [u.input_tokens, u.output_tokens, u.cached_input_tokens];
    if (!values.every(n => Number.isSafeInteger(n) && n >= 0)) continue;
    const at = Date.parse(row.timestamp); if (!Number.isFinite(at)) continue;
    entries.push({entry_id: p.response_id, at: at / 1000, ...(typeof model === 'string' && model ? {model} : {}),
      tokens: {in: values[0], out: values[1], cache: values[2]}});
  }
  if (entries.length) {
    const recent = entries.slice(-200);
    await core.handle({op: 'usage.record', entries: recent,
      act_key: 'usage:codex:' + createHash('sha256').update(JSON.stringify(recent)).digest('hex')});
  }
}
