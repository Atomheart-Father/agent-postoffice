import {randomUUID} from 'node:crypto';
import {readFileSync, realpathSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {setTimeout as sleep} from 'node:timers/promises';
import {createPiRoundClient, readDescriptor, isCreated} from './office_pi_round.mjs';
import {nativeDescriptor} from './office_socket.mjs';
import {reportClaudeUsage} from './office_claude_usage.mjs';

const good = x => x && typeof x === 'object' && x.ok !== false && !x.refused;
const nonempty = x => typeof x === 'string' && x.length > 0;

async function reconnect(windowOpen, call, disconnected) {
  for (let delay = 250; !windowOpen || windowOpen(); delay = Math.min(delay * 2, 5000)) {
    const result = await call();
    if (!windowOpen || !disconnected(result)) return result;
    await sleep(delay);
  }
  return null;
}
async function locked(client, fn, windowOpen) {
  while (true) {
    const acquired = await reconnect(windowOpen, () => client.acquireSerial(), result =>
      result?.error === 'socket_error' || result?.error === 'socket_closed');
    if (acquired !== true) return null;
    try { return await fn(); }
    catch (error) {
      if (!windowOpen || !error.serialReadDisconnected) throw error;
    } finally { await client.releaseSerial(); }
    if (!windowOpen()) return null;
    await sleep(250);
  }
}
function exactPrompt(prompt, text) {
  if (!nonempty(prompt)) return false;
  const prefix = '<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\n';
  const suffix = '\n\n</system-reminder>';
  if (!prompt.startsWith(prefix) || !prompt.endsWith(suffix)) return false;
  const body = prompt.slice(prefix.length, -suffix.length);
  return /^Stop hook blocking error from command "[^"\n]+": /.test(body)
    && body.slice(body.indexOf('": ') + 3) === text;
}

export async function handleClaudeHook({event, mode, descriptorPath, clientFactory = createPiRoundClient, waitMs = 60000, promptMatches = exactPrompt, nativeStart = false, stopCanStart = true, dueKey = null, collectUsage = true, absentMarker = null, codexNative = false, windowOpen = () => true}) {
  const d = readDescriptor(descriptorPath);
  const session = event?.session_id;
  if (!d || !nonempty(session) || d.hostSessionRef !== session) return {status: 'unknown', reason: 'session_or_descriptor'};
  const client = clientFactory({socketPath: d.socketPath, tokenFile: d.tokenFile}).setConnBox(d.actor);
  try {
    if (collectUsage && ['SessionStart', 'UserPromptSubmit'].includes(event.hook_event_name)) await reportClaudeUsage(client, event);
    if (mode === 'wait' || mode === 'prepare') {
      if (!['SessionStart', 'Stop'].includes(event.hook_event_name)) return {status: 'unknown', reason: 'event'};
      const waitWindow = mode === 'wait' ? windowOpen : undefined;
      const awaitFrames = options => reconnect(waitWindow, () => client.awaitFrames(options), got => got?.unknown === true);
      const skip = new Set();
      while (true) {
        if (!windowOpen()) return {status: 'window_closed'};
        const pending = await locked(client, async () => {
          const s = (await client.serialState('read')).state;
          if (!s) return {cleared: true};
          if (mode === 'prepare') {
            // A queued Codex item can outlive its deadline. Recover only after the host confirms its absence.
            if (s.frame.kind === 'batch_round' && s.text?.split('\n', 1)[0] === absentMarker) {
              let round = await client.roundState(s.unit, s.token);
              const ours = row => good(row) && row.binding_id === d.bindingId && row.binding_gen === d.generation;
              if (ours(round) && round.state === 'begun' && Number.isFinite(round.deadline_ts)
                  && round.deadline_ts <= Date.now() / 1000) {
                const recovered = await client.recover(s.unit);
                if (good(recovered)) round = await client.roundState(s.unit, s.token);
              }
              if (ours(round) && round.state === 'unknown') {
                await client.serialState('clear'); return {cleared:true};
              }
            }
            return s;
          }
          if (s.frame.kind === 'idle_started' && s.stopTs && s.ackUnknown) {
            const ack = await client.wakeAck(s.unit, s.frame.due_key, s.attempt);
            const ended = await client.hostEvent('turn_end', s.stopTs, {attempt_id: s.attempt});
            if (good(ack) && ack.taken && good(ended)) { await client.serialState('clear'); return {cleared: true}; }
            return s;
          }
          if (s.frame.kind !== 'batch_round' || !nonempty(s.token)) return s;
          const recovered = await client.recover(s.unit);
          if (!good(recovered) || !Array.isArray(recovered.tokens)) return {...s, recoveryFailed: true};
          const state = await client.roundState(s.unit, s.token);
          if (!good(state) || state.binding_id !== d.bindingId || state.binding_gen !== d.generation)
            return {...s, recoveryFailed: true};
          if (state.state === 'rejected' || (state.state === 'unknown' && state.result_src === 'recover')
              || (state.state === 'accepted' && state.run_state === 'settled')) {
            await client.serialState('clear'); return {cleared: true};
          }
          return s;
        }, waitWindow);
        if (!pending) return {status: 'unknown', reason: 'busy'};
        if (pending.cleared) break;
        if (mode === 'prepare') return {status: 'unknown', reason: 'pending', text: pending.text};
        if (pending.recoveryFailed) return {status: 'unknown', reason: 'recover'};
        skip.add(pending.frame.due_key);
        const waited = await awaitFrames({waitMs, skip: [...skip]});
        if (!windowOpen()) return {status: 'window_closed'};
        if (!good(waited) || !Array.isArray(waited.frames)) return {status: 'unknown', reason: 'await'};
        for (const f of waited.frames) if (nonempty(f?.due_key)) skip.add(f.due_key);
      }
      while (true) {
      let frame;
      do {
        const received = await awaitFrames({waitMs});
        if (!windowOpen()) return {status: 'window_closed'};
        if (!good(received) || !Array.isArray(received.frames)) return {status: 'unknown', reason: 'await'};
        const units = Array.isArray(received.units) ? received.units : [];
        frame = received.frames.find(f => ['batch_round', 'idle_started'].includes(f?.kind) && units.includes(f.unit) && (!dueKey || f.due_key === dueKey));
        if (mode === 'prepare' && !frame) return {status: 'unknown', reason: 'frame'};
        if (received.frames.length && !frame) return {status: 'unknown', reason: 'unsupported_frame'};
      } while (!frame);

      const result = await locked(client, async () => {
        if (!windowOpen()) return {status: 'window_closed'};
        if ((await client.serialState('read')).state) return {status: 'unknown', reason: 'pending'};
        if (!nonempty(frame.due_key) || !nonempty(frame.text) || (frame.kind === 'batch_round' && (!Array.isArray(frame.refs) || !frame.refs.length)))
          return {status: 'unknown', reason: 'frame'};
        if (frame.kind === 'idle_started') {
          if (!nonempty(frame.attempt_key)) return {status: 'unknown', reason: 'attempt'};
          const text = `[Office wake ${randomUUID()}]\n${frame.text}`;
          await client.serialState('write', {session, unit: frame.unit, frame, attempt: frame.attempt_key, text, phase: 'handed'});
          return {status: 'handoff', text};
        }
        const owner = await client.ownerRegister(frame.unit);
        if (!good(owner) || !nonempty(owner.owner_token) || owner.binding_id !== d.bindingId
            || owner.binding_gen !== d.generation || owner.instance !== d.instance) return {status: 'unknown', reason: 'owner'};
        const batch = await client.begin(frame.unit, codexNative ? 'codex' : 'batch_round', frame);
        if (batch?.reason === 'stale_frame') return {status:'stale_frame'};
        if (!good(batch) || !isCreated(batch.created) || !nonempty(batch.token)
            || !Array.isArray(batch.deliver) || !batch.deliver.length || !Array.isArray(batch.carry))
          return {status: 'unknown', reason: 'begin'};
        const refs = [...batch.deliver, ...batch.carry];
        if (new Set(refs).size !== refs.length || refs.length !== frame.refs.length
            || !refs.every(r => frame.refs.includes(r) && /^[a-f0-9]{64}$/i.test(batch.content_sha256?.[r] || '')))
          return {status: 'unknown', reason: 'refs'};
        const attempt = `${d.bindingId}|${d.generation}|${d.instance}|claude-${batch.token}`;
        const captured = await client.recordPayload(frame.unit, batch.token, batch.deliver[0], attempt, frame.text);
        if (!good(captured) || captured.recorded !== true || captured.text_available !== true
            || captured.text_matches !== true || captured.conflict !== false)
          return {status: 'unknown', reason: 'payload'};
        const text = `[Office wake ${codexNative ? batch.token : randomUUID()}]\n${frame.text}`;
        await client.serialState('write', {session, unit: frame.unit, frame, token: batch.token, owner: owner.owner_token, attempt, text, phase: 'handed'});
        return {status: 'handoff', text};
      }, waitWindow);
      if (result?.status === 'stale_frame') continue;
      return result || {status: 'unknown', reason: 'busy'};
      }
    }
    return await locked(client, async () => {
      const s = (await client.serialState('read')).state;
      if (!s && codexNative && event.hook_event_name === 'Stop' && nonempty(event.prompt_id)) {
        const ended = await client.hostEvent('turn_end', Math.floor(Date.now()/1000),
          {turn_id: event.prompt_id, act_key: `codex:turn_end:${event.prompt_id}`});
        return good(ended) ? {status: 'idle_reported'} : {status: 'unknown', reason: 'turn_end'};
      }
      if (!s || s.session !== session) return {status: 'unknown', reason: 'no_pending'};
      if (mode === 'reject' && s.phase === 'handed') {
        if (s.frame.kind === 'batch_round') {
          const rejected = await client.settle(s.unit, s.token, 'rejected');
          if (!good(rejected) || rejected.state !== 'rejected') return {status: 'unknown', reason: 'reject'};
        }
        await client.serialState('clear'); return {status: 'rejected'};
      }
      if (event.hook_event_name === 'UserPromptSubmit') {
        if (s.phase !== 'handed' || !nonempty(event.prompt_id) || !promptMatches(event.prompt, s.text))
          return {status: 'unknown', reason: 'prompt'};
        if (codexNative && s.frame.kind === 'idle_started') {
          const current = await client.workRunnable(s.unit);
          if (good(current) && current.count === 0) {
            await client.serialState('clear'); return {status: 'obsolete'};
          }
        }
        if (s.frame.kind === 'batch_round') {
          const out = await client.recordAdmission(s.unit, s.token, event.prompt_id);
          if (!good(out) || !out.recorded) return {status: 'unknown', reason: 'admission'};
        }
        s.prompt = event.prompt_id; s.phase = 'admitted'; await client.serialState('write', s);
        if (!nativeStart) return {status: 'admitted'};
      }
      if (!nonempty(event.prompt_id) || event.prompt_id !== s.prompt) return {status: 'unknown', reason: 'prompt_id'};
      if (event.hook_event_name === 'StopFailure' && s.phase === 'admitted') {
        if (s.frame.kind === 'batch_round') {
          const rejected = await client.settle(s.unit, s.token, 'rejected', s.prompt);
          if (!good(rejected) || rejected.state !== 'rejected') return {status: 'unknown', reason: 'reject'};
        }
        await client.serialState('clear');
        return {status: 'not_started'};
      }
      if ((nativeStart || ['MessageDisplay', 'PostToolUse'].includes(event.hook_event_name)
           || (stopCanStart && event.hook_event_name === 'Stop')) && s.phase === 'admitted') {
        if (s.frame.kind === 'batch_round') {
          const run = await client.runBegin(s.unit, s.token, s.owner, s.prompt);
          if (!good(run) || !run.recorded) return {status: 'unknown', reason: 'run'};
        }
        const started = await client.hostEvent('round.started', Math.floor(Date.now()/1000), {attempt_id: s.attempt});
        if (!good(started)) return {status: 'unknown', reason: 'started'};
        if (s.frame.kind === 'batch_round') {
          const settled = await client.settle(s.unit, s.token, 'accepted', s.prompt);
          if (!good(settled) || settled.state !== 'accepted') return {status: 'unknown', reason: 'settle'};
        }
        s.phase = 'running'; s.ackUnknown = true; await client.serialState('write', s);
        const ack = await client.wakeAck(s.unit, s.frame.due_key, s.frame.kind === 'idle_started' ? s.attempt : undefined);
        s.ackUnknown = !(good(ack) && ack.taken); await client.serialState('write', s);
        if (event.hook_event_name !== 'Stop')
          return {status: 'running', ack: s.ackUnknown ? 'unknown' : 'taken'};
      }
      if (['Stop', 'StopFailure'].includes(event.hook_event_name) && s.phase === 'running') {
        if (s.frame.kind === 'batch_round') {
          const out = await client.consume(s.unit, {type: 'agent_settled', aborted: event.hook_event_name === 'StopFailure'}, s.owner, s.token);
          if (!good(out) || !out.recorded) return {status: 'unknown', reason: 'consume'};
        }
        if (!s.stopTs) { s.stopTs = Math.floor(Date.now()/1000); await client.serialState('write', s); }
        const ended = await client.hostEvent('turn_end', s.stopTs, {attempt_id: s.attempt});
        if (!good(ended)) return {status: 'unknown', reason: 'turn_end'};
        if (s.frame.kind === 'idle_started' && s.ackUnknown) {
          const ack = await client.wakeAck(s.unit, s.frame.due_key, s.attempt);
          if (!good(ack) || !ack.taken) return {status: 'unknown', reason: 'idle_ack'};
        }
        await client.serialState('clear');
        return {status: 'finished'};
      }
      return {status: 'unknown', reason: 'event'};
    }, event.hook_event_name === 'Stop' && !codexNative ? windowOpen : undefined) || {status: 'unknown', reason: 'busy'};
  } finally { client.close(); }
}

if (import.meta.main ?? (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href)) {
  try {
    const input = JSON.parse(readFileSync(0, 'utf8'));
    const descriptorPath = nativeDescriptor(null, 'claude', input.session_id);
    const parent = process.ppid;
    const args = {event: input, descriptorPath, windowOpen: () => parent > 1 && process.ppid === parent};
    if (process.argv[2] === 'stop') {
      const done = await handleClaudeHook({...args, mode: 'observe'});
      if (!['finished', 'unknown'].includes(done.status) || (done.status === 'unknown' && done.reason !== 'no_pending'))
        throw Error('stop remains unknown: ' + done.reason);
    }
    const result = await handleClaudeHook({...args, mode: process.argv[2] === 'stop' ? 'wait' : process.argv[2]});
    if (result.status === 'handoff') { process.stderr.write(result.text + '\n'); process.exitCode = 2; }
  } catch (e) { process.stderr.write('Office hook unknown: ' + e.message + '\n'); process.exitCode = 1; }
}
