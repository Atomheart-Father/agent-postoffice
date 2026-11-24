import {createHash, randomUUID} from 'node:crypto';
import {appendFileSync, createReadStream, mkdirSync, readFileSync, realpathSync, lstatSync, unlinkSync, writeFileSync} from 'node:fs';
import {basename, dirname, join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';
import {setTimeout as sleep} from 'node:timers/promises';
import {createPiRoundClient, readDescriptor, isCreated} from './office_pi_round.mjs';
import {nativeDescriptor} from './office_socket.mjs';
import {reportClaudeUsage} from './office_claude_usage.mjs';
import messages from '../delivery/messages.json' with {type: 'json'};

const good = x => x && typeof x === 'object' && x.ok !== false && !x.refused;
const nonempty = x => typeof x === 'string' && x.length > 0;
const retryableHookFailure = x => x?.unknown === true || ['internal', 'busy'].includes(x?.code) ||
  (x?.code === 'unauthorized_attempt' && x.credential_changed === true);

async function reconnect(windowOpen, call, disconnected) {
  for (let delay = 250; !windowOpen || windowOpen(); delay = Math.min(delay * 2, 5000)) {
    const result = await call();
    if (!windowOpen || !disconnected(result)) return result;
    await sleep(delay);
  }
  return null;
}
async function locked(client, fn, windowOpen, retryAuth = false) {
  for (let delay = 250; ; delay = Math.min(delay * 2, 5000)) {
    const acquired = await reconnect(windowOpen, () => client.acquireSerial(), result =>
      ['busy', 'internal', 'socket_error', 'socket_closed'].includes(result?.error) ||
      (result?.error === 'unauthorized_attempt' && (retryAuth || result.credential_changed)));
    if (windowOpen && acquired?.error) throw Error('serial acquire: ' + acquired.error);
    if (acquired !== true) return null;
    try { return await fn(); }
    catch (error) {
      if (!windowOpen || !error.serialReadRetryable) throw error;
    } finally { await client.releaseSerial(); }
    if (!windowOpen()) return null;
    await sleep(delay);
  }
}
async function writeCodexHandoff(client, state) {
  state.offer = randomUUID();
  try {
    if (!(await client.serialState('write', state))?.written) return {status:'unknown', reason:'serial'};
    return {status:'handoff', text:state.text, state};
  } catch (error) {
    if (!error.serialWriteDisconnected) throw error;
    return {status:'recheck_handoff', state};
  }
}

function codexUnsentPath(descriptorPath, descriptor, state) {
  const dir = join(dirname(descriptorPath), '.claude-wake'), st = lstatSync(dir);
  if (!st.isDirectory() || st.uid !== process.getuid() || (st.mode & 0o077)) throw Error('unsafe handoff directory');
  const key = JSON.stringify([descriptor.bindingId, descriptor.generation, descriptor.instance, descriptor.hostSessionRef, state]);
  return join(dir, createHash('sha256').update(key).digest('hex') + '.unsent');
}

export function recordCodexUnsent(descriptorPath, state) {
  const d = readDescriptor(descriptorPath);
  if (!d || !state?.offer || state.session !== d.hostSessionRef || state.phase !== 'handed') return;
  try { writeFileSync(codexUnsentPath(descriptorPath, d, state), '', {flag:'wx', mode:0o600}); }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
}

async function recheckCodexHandoff(client, descriptorPath, result) {
  const until = Date.now() + 10000;
  const checked = await locked(client, async () => {
    const current = (await client.serialState('read')).state;
    if (JSON.stringify(current) !== JSON.stringify(result.state)) return {status:'unknown', reason:'handoff_changed'};
    return {status:'handoff', text:current.text, state:current};
  }, () => Date.now() < until, true);
  if (checked) return checked;
  recordCodexUnsent(descriptorPath, result.state);
  return {status:'unknown', reason:'busy'};
}

async function authorizeBatch(client, state, sendId, previousSendId) {
  const result = await client.authorizeSend(state.unit, state.token, state.attempt, sendId,
    {previousSendId, ownerToken:state.owner});
  if (result?.unknown && !previousSendId && sendId === state.token)
    await client.settle(state.unit, state.token, 'rejected');
  return result;
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

async function receiveLateClaudeWake(client, event, promptMatches) {
  if (event.hook_event_name !== 'UserPromptSubmit' || !nonempty(event.prompt_id)) return false;
  const token = typeof event.prompt === 'string' && event.prompt.match(/\[Office wake ([a-f0-9]{32})\]/)?.[1];
  if (!token) return false;
  const round = await client.roundState(undefined, token);
  if (round?.channel !== 'batch_round' || !round.send_authorized || !['begun', 'unknown'].includes(round.state)
      || round.run_state != null) return false;
  const payload = round.payloads?.find(part => promptMatches(event.prompt, `[Office wake ${token}]\n${part.text}`));
  if (!payload || !(await client.recordAdmission(round.box, token, event.prompt_id))?.recorded) return false;
  return (await client.runBegin(round.box, token, undefined, event.prompt_id, payload.text))?.recorded === true;
}

async function captureBatch(client, descriptor, frame, channel, textFor, ownerToken, stage) {
  let owner = ownerToken || channel === 'native_delivery' ? await client.ownerShow(frame.unit) : null;
  const owns = row => good(row) && row.binding_id === descriptor.bindingId
    && row.binding_gen === descriptor.generation && row.instance === descriptor.instance;
  if (!ownerToken && (!owns(owner) || owner.revoked)) owner = await client.ownerRegister(frame.unit);
  if (!owns(owner) || owner.revoked) return {status: 'unknown', reason: 'owner'};
  const batch = await client.begin(frame.unit, channel, frame);
  if (batch?.reason === 'stale_frame') return {status: 'stale_frame'};
  if (!good(batch) || !isCreated(batch.created) || !nonempty(batch.token)
      || !Array.isArray(batch.deliver) || !batch.deliver.length || !Array.isArray(batch.carry))
    return {status: 'unknown', reason: 'begin'};
  const refs = [...batch.deliver, ...batch.carry];
  if (new Set(refs).size !== refs.length || refs.length !== frame.refs.length
      || !refs.every(ref => frame.refs.includes(ref) && /^[a-f0-9]{64}$/i.test(batch.content_sha256?.[ref] || '')))
    return {status: 'unknown', reason: 'refs'};
  const attempt = `${descriptor.bindingId}|${descriptor.generation}|${descriptor.instance}|claude-${batch.token}`;
  const text = textFor ? textFor(batch.token, refs) : frame.text;
  if (stage && !(await stage({token:batch.token, owner:ownerToken || owner.owner_token, attempt, anchor:batch.deliver[0]})))
    return {status:'unknown', reason:'serial'};
  const captured = await client.recordPayload(frame.unit, batch.token, batch.deliver[0], attempt, text);
  if (!good(captured) || captured.recorded !== true || captured.text_available !== true
      || captured.text_matches !== true || captured.conflict !== false)
    return {status: 'unknown', reason: 'payload'};
  return {token: batch.token, owner: ownerToken || owner.owner_token, attempt, text};
}

const mailMarker = /^\[Office mail ([a-f0-9]{32}) ([A-Za-z0-9_-]+) ([A-Za-z0-9_-]+)\]\n/;

async function confirmToolMail(client, event) {
  const path = event.transcript_path, session = event.session_id;
  if (typeof path !== 'string' || basename(path) !== `${session}.jsonl`) return;
  const due = await client.awaitFrames({waitMs: 0, toolMail: true});
  if (!good(due)) return;
  const pending = new Map((due.tool_mail || []).map(receipt => [receipt.text, receipt]));
  if (pending.size) {
    const stream = createReadStream(path);
    const lines = createInterface({input: stream, crlfDelay: Infinity});
    try {
      for await (const line of lines) {
        if (!line.includes('[Office mail ')) continue;
        let row;
        try { row = JSON.parse(line); } catch { continue; }
        const native = row.attachment;
        if (row.type !== 'attachment' || row.sessionId !== session || native?.type !== 'hook_additional_context'
            || native.hookEvent !== 'PostToolBatch' || !Array.isArray(native.content)) continue;
        for (const text of native.content) {
          const receipt = pending.get(text), match = receipt && mailMarker.exec(text);
          if (!match || receipt.token !== match[1]) continue;
          const {box: unit, token} = receipt, originalTurn = match[3];
          const admitted = await client.recordAdmission(unit, token, originalTurn);
          if (admitted?.recorded && (await client.settle(unit, token, 'accepted', originalTurn))?.state === 'accepted')
            pending.delete(text);
        }
        if (!pending.size) break;
      }
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    } finally { lines.close(); stream.destroy(); }
  }
  for (const frame of due.frames || [])
    if (frame.kind === 'batch_round') await client.wakeAck(frame.unit, frame.due_key);
}

async function toolMail(client, descriptor, event) {
  if (!nonempty(event.prompt_id) || !/^[A-Za-z0-9_-]+$/.test(event.prompt_id)) return;
  const due = await client.awaitFrames({waitMs: 0});
  const frame = due?.frames?.find(item => item.kind === 'batch_round' && due.units?.includes(item.unit));
  if (!frame?.refs?.length) return;
  const unit = Buffer.from(frame.unit).toString('base64url');
  const batch = await captureBatch(client, descriptor, frame, 'native_delivery', (token, refs) =>
    `[Office mail ${token} ${unit} ${event.prompt_id}]\n${messages.tool_batch} ${JSON.stringify(refs)}\n${messages.read_instruction}`);
  if (!batch.token) return;
  const permission = await authorizeBatch(client, {...batch, unit:frame.unit}, batch.token);
  if (permission?.allowed === true) return batch.text;
}

function codexPayloads(state) {
  const payloads = [state.text];
  for (const part of state.appends || []) payloads.push(payloads.at(-1) + '\n\n' + (part.text ?? `[Office wake ${part.token}]\n${part.frame.text}`));
  return payloads;
}

async function receiveCodexBatch(client, state, event) {
  const delivered = codexPayloads(state).indexOf(event.prompt);
  if (delivered < 0 || !nonempty(event.prompt_id)) return {status: 'unknown', reason: 'prompt'};
  const parts = [state, ...(state.appends || [])];
  const received = [];
  let pending = false;
  for (const [index, part] of parts.entries()) {
    const {unit, token} = part;
    const prior = await client.roundState(unit, token);
    if (prior?.reason_code === 'unit_not_carried') continue;
    if (!good(prior) || !prior.state) { pending = true; continue; }
    if (index > delivered) {
      if ((await client.settle(unit, token, 'rejected'))?.state !== 'rejected') pending = true;
      continue;
    }
    if (!(prior.state === 'accepted' && prior.run_state === 'settled')
        && !(await client.runBegin(unit, token, undefined, event.prompt_id, part.frame.text))?.recorded) { pending = true; continue; }
    received.push(part);
  }
  for (const part of received) {
    const ack = await client.wakeAck(part.unit, part.frame.due_key);
    if (!good(ack) && ack?.code !== 'not_found' && ack?.reason_code !== 'unit_not_carried') pending = true;
  }
  if (Number.isFinite(event.terminal_ts)) {
    if (!good(await client.endNative(event.prompt_id))) return {status:'unknown', reason:'native_end'};
  }
  if (pending) return {status:'unknown', reason:'part'};
  await client.serialState('clear');
  return {status: Number.isFinite(event.terminal_ts) ? 'finished' : 'running', tokens:received.map(part => part.token)};
}

async function receiveCodexHistory(client, descriptor, event) {
  if (!nonempty(event.prompt_id) || !nonempty(event.prompt) || !nonempty(event.client_id)) return;
  const pieces = event.prompt.split(/\n\n(?=\[Office wake [a-f0-9]{32}\]\n)/);
  const parsed = pieces.map(text => /^\[Office wake ([a-f0-9]{32})\]\n([\s\S]*)$/.exec(text));
  if (parsed.some(match => !match) || `[Office wake ${parsed[0][1]}]` !== event.client_id) return;
  const matches = [];
  for (const [, token, text] of parsed) {
    const row = await client.roundState(undefined, token);
    if (row?.reason_code === 'unit_not_carried') continue;
    if (!good(row) || !nonempty(row.box) || row.channel !== 'codex' || row.binding_id !== descriptor.bindingId
        || row.binding_gen !== descriptor.generation || !row.payloads?.some(p => p.text === text)) continue;
    matches.push({unit:row.box, token, text});
  }
  const received = [];
  for (const part of matches)
    if ((await client.runBegin(part.unit, part.token, undefined, event.prompt_id, part.text))?.recorded) received.push(part.token);
  if (Number.isFinite(event.terminal_ts) && !good(await client.endNative(event.prompt_id))) return {tokens:[]};
  return {tokens:received};
}

export async function handleClaudeHook({event, mode, descriptorPath, clientFactory = createPiRoundClient, waitMs = 60000, promptMatches = exactPrompt, nativeStart = false, stopCanStart = true, dueKey = null, collectUsage = true, absentMarker = null, codexNative = false, expectedOffer, windowOpen = () => true, reportWaitError = () => {}}) {
  const d = readDescriptor(descriptorPath);
  const session = event?.session_id;
  if (!d || !nonempty(session) || d.hostSessionRef !== session) return {status: 'unknown', reason: 'session_or_descriptor'};
  const client = clientFactory({socketPath: d.socketPath, tokenFile: d.tokenFile}).setConnBox(d.actor);
  try {
    if (codexNative && mode === 'authorize') return await locked(client, async () => {
      const s = (await client.serialState('read')).state;
      if (s?.session !== session || s.frame.kind !== 'batch_round' || s.phase !== 'handed'
          || !expectedOffer || s.offer !== expectedOffer) return {allowed:false, reason:'handoff_changed'};
      const result = await authorizeBatch(client, s, s.offer, s.previousSendId);
      if (result?.rejected) await client.serialState('clear');
      return result;
    });
    if (codexNative && mode === 'attempted') return await locked(client, async () => {
      const s = (await client.serialState('read')).state;
      if (s?.session !== session || !expectedOffer || s.offer !== expectedOffer || s.phase !== 'handed') return;
      s.queueAttempted = true;
      return client.serialState('write', s);
    });
    if (collectUsage && ['SessionStart', 'UserPromptSubmit'].includes(event.hook_event_name)) await reportClaudeUsage(client, event);
    if (codexNative && mode === 'seen') return await locked(client, async () => {
      const s = (await client.serialState('read')).state;
      if (s?.session !== session || !s.offer || s.offer !== expectedOffer || s.text?.split('\n', 1)[0] !== event.client_id) return;
      try { unlinkSync(codexUnsentPath(descriptorPath, d, s)); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    });
    if (mode === 'observe' && !codexNative) {
      await confirmToolMail(client, event);
      if (event.hook_event_name === 'PostToolBatch') {
        const text = await locked(client, () => toolMail(client, d, event));
        if (text) return {status: 'context', text};
      }
    }
    if (codexNative && mode === 'history') return await locked(client, () => receiveCodexHistory(client, d, event));
    if (codexNative && mode === 'receipt') return await locked(client, async () => {
      const s = (await client.serialState('read')).state;
      if (s?.session !== session || s.frame.kind !== 'batch_round' || s.offer !== expectedOffer) return;
      return receiveCodexBatch(client, s, event);
    });
    if (mode === 'wait' || mode === 'prepare') {
      if (!['SessionStart', 'Stop'].includes(event.hook_event_name)) return {status: 'unknown', reason: 'event'};
      const waitWindow = mode === 'wait' ? windowOpen : undefined;
      let lastWaitError;
      const awaitFrames = options => reconnect(waitWindow, () => client.awaitFrames(options), got => {
        // Waiting only reads delivery frames. A failed daemon read must not end the native waiter.
        if (got?.code === 'internal' || got?.code === 'busy') {
          const error = [got.code, got.message].filter(Boolean).join(': ').slice(0, 300);
          if (error !== lastWaitError) reportWaitError(`await_retry: ${error}`);
          lastWaitError = error;
          return true;
        }
        if (good(got) && lastWaitError) { reportWaitError('await_resumed'); lastWaitError = undefined; }
        return retryableHookFailure(got);
      });
      while (true) {
        if (!windowOpen()) return {status: 'window_closed'};
        const pending = await locked(client, async () => {
          const s = (await client.serialState('read')).state;
          if (!s) return {cleared: true};
          if (codexNative && absentMarker && s.offer !== expectedOffer) return s;
          if (s.frame?.kind === 'idle_started') {
            // This only frees the local slot. It makes no assertion about native history.
            if ((await client.serialState('clear'))?.cleared) return {cleared:true};
            return s;
          }
          if (s.frame.kind === 'batch_round' && s.phase === 'running' && !s.ackUnknown) {
            await client.serialState('clear'); return {cleared: true};
          }
          if (s.frame.kind === 'batch_round' && nonempty(s.token) && typeof client.roundState === 'function') {
            const prior = await client.roundState(s.unit, s.token);
            if (prior?.reason_code === 'unit_not_carried') {
              const rest = await Promise.all((s.appends || []).map(part => client.roundState(part.unit, part.token)));
              if (rest.every(row => row?.reason_code === 'unit_not_carried' || (good(row)
                  && row.binding_id === d.bindingId && row.binding_gen === d.generation
                  && (row.state === 'rejected' || row.state === 'accepted' && row.run_state === 'settled')))) {
                if ((await client.serialState('clear'))?.cleared) return {cleared:true};
              }
              if (mode !== 'prepare') return s;
            }
          }
          if (mode === 'prepare') {
            if (codexNative && s.frame.kind === 'batch_round' && s.phase === 'staged') {
              const prior = await client.roundState(s.unit, s.token);
              if (!good(prior) || prior.binding_id !== d.bindingId || prior.binding_gen !== d.generation || !Array.isArray(prior.payloads)) return s;
              if (!prior.payloads.some(p => p.ref === s.anchor && p.attempt_key === s.attempt && p.text === s.frame.text)) {
                if (prior.state !== 'begun') { await client.serialState('clear'); return {cleared:true}; }
                const saved = await client.recordPayload(s.unit, s.token, s.anchor, s.attempt, s.frame.text);
                if (!good(saved) || !saved.text_available || !saved.text_matches || saved.conflict) return s;
              }
              s.phase = 'handed';
              return writeCodexHandoff(client, {...s, previousSendId:`staged:${s.token}`});
            }
            if (codexNative && s.phase === 'handed' && s.offer && nonempty(absentMarker)
                && s.text?.split('\n', 1)[0] === absentMarker) {
              let proof;
              try { proof = codexUnsentPath(descriptorPath, d, s); if (!lstatSync(proof).isFile()) proof = null; }
              catch (error) { if (error.code !== 'ENOENT') throw error; proof = null; }
              if (proof) {
                const round = await client.roundState(s.unit, s.token);
                if (!good(round) || !['begun', 'unknown'].includes(round.state) || round.run_state != null) return s;
                if (round.send_id && ![s.offer, s.previousSendId].includes(round.send_id)) return s;
                const next = await writeCodexHandoff(client, {...s, previousSendId:round.send_id || s.offer});
                if (next.status === 'handoff') unlinkSync(proof);
                return next;
              }
            }
            if (s.frame.kind === 'batch_round' && s.text?.split('\n', 1)[0] === absentMarker) {
              const parts = [s, ...(s.appends || [])];
              const ours = row => good(row) && row.binding_id === d.bindingId && row.binding_gen === d.generation;
              let rounds = await Promise.all(parts.map(part => client.roundState(part.unit, part.token)));
              if (s.queueAttempted || rounds[0]?.send_authorized) {
                for (const [i, part] of parts.entries()) {
                  const row = rounds[i];
                  if (row?.reason_code === 'unit_not_carried') continue;
                  if (!ours(row) || !Array.isArray(row.payloads)) return s;
                  if (row.state === 'begun') {
                    const status = i > 0 && row.payloads.length === 0 ? 'rejected' : 'unknown';
                    if ((await client.settle(part.unit, part.token, status))?.state !== status) return s;
                  } else if (!['unknown', 'rejected', 'accepted'].includes(row.state)) return s;
                }
                await client.serialState('clear'); return {cleared:true};
              }
              const expired = new Set(parts.filter((part, i) => ours(rounds[i]) && rounds[i].state === 'begun'
                && Number.isFinite(rounds[i].deadline_ts) && rounds[i].deadline_ts <= Date.now() / 1000).map(part => part.unit));
              if (expired.size) {
                for (const unit of expired) if (!good(await client.recover(unit))) return s;
                rounds = await Promise.all(parts.map(part => client.roundState(part.unit, part.token)));
              }
              if (rounds.every(row => row?.reason_code === 'unit_not_carried'
                  || (ours(row) && (row.state === 'unknown' || row.state === 'rejected')))) {
                await client.serialState('clear'); return {cleared:true};
              }
            }
            return s;
          }
          if (s.frame.kind !== 'batch_round' || !nonempty(s.token)) return s;
          // A different real batch can proceed while this round awaits a native receipt.
          const frames = (await client.awaitFrames({waitMs:0}))?.frames;
          if (frames?.some(f => f.kind === 'batch_round'
              && (f.unit !== s.unit || f.refs?.some(ref => !s.frame.refs.includes(ref))))) {
            const old = await client.settle(s.unit, s.token, 'unknown');
            if (old?.state === 'unknown') {
              await client.serialState('clear');
              return {cleared:true};
            }
          }
          const recovered = await client.recover(s.unit);
          if (!good(recovered) || !Array.isArray(recovered.tokens)) return {...s, recoveryFailed: true};
          const state = await client.roundState(s.unit, s.token);
          if (!good(state) || state.binding_id !== d.bindingId || state.binding_gen !== d.generation)
            return {...s, recoveryFailed: true};
          if (state.state === 'rejected' || state.state === 'unknown'
              || (state.state === 'accepted' && state.run_state === 'settled')) {
            await client.serialState('clear'); return {cleared: true};
          }
          return s;
        }, waitWindow);
        if (!pending) return {status: 'unknown', reason: 'busy'};
        if (pending.status === 'recheck_handoff') return await recheckCodexHandoff(client, descriptorPath, pending);
        if (pending.cleared) break;
        if (pending.status) return pending;
        if (mode === 'prepare' && pending.frame.kind !== 'batch_round') return {status:'unknown', reason:'retired_wake'};
        if (mode === 'prepare') return {status: 'unknown', reason: 'pending', text: pending.text, offer:pending.offer,
          ...(codexNative && pending.frame.kind === 'batch_round' ? {batch: true} : {})};
        if (pending.recoveryFailed) return {status: 'unknown', reason: 'recover'};
        const {due_key, due_version} = pending.frame;
        const skip = [Number.isInteger(due_version) ? `${due_key}#${due_version}` : due_key];
        const waited = await awaitFrames({waitMs, skip});
        if (!windowOpen()) return {status: 'window_closed'};
        if (!good(waited) || !Array.isArray(waited.frames)) return {status: 'unknown', reason: 'await', error:waited?.code};
      }
      while (true) {
      let frame;
      do {
        const received = await awaitFrames({waitMs});
        if (!windowOpen()) return {status: 'window_closed'};
        if (!good(received) || !Array.isArray(received.frames)) return {status: 'unknown', reason: 'await', error:received?.code};
        const units = Array.isArray(received.units) ? received.units : [];
        frame = received.frames.find(f => f?.kind === 'batch_round' && units.includes(f.unit) && (!dueKey || f.due_key === dueKey));
        if (mode === 'prepare' && !frame) return {status: 'unknown', reason: 'frame'};
        if (received.frames.length && !frame) return {status: 'unknown', reason: 'unsupported_frame'};
      } while (!frame);

      const result = await locked(client, async () => {
        if (!windowOpen()) return {status: 'window_closed'};
        if ((await client.serialState('read')).state) return {status: 'unknown', reason: 'pending'};
        if (!nonempty(frame.due_key) || !nonempty(frame.text) || (frame.kind === 'batch_round' && (!Array.isArray(frame.refs) || !frame.refs.length)))
          return {status: 'unknown', reason: 'frame'};
        const batch = await captureBatch(client, d, frame, codexNative ? 'codex' : 'batch_round', undefined, undefined,
          codexNative ? async staged => (await client.serialState('write', {session, unit:frame.unit, frame, ...staged,
            text:`[Office wake ${staged.token}]\n${frame.text}`, phase:'staged'}))?.written === true : undefined);
        if (!batch.token) return batch;
        const text = `[Office wake ${batch.token}]\n${frame.text}`;
        const handed = {session, unit: frame.unit, frame, ...batch, text, phase: 'handed'};
        if (codexNative) return writeCodexHandoff(client, handed);
        if (!(await client.serialState('write', handed))?.written) return {status:'unknown', reason:'serial'};
        const permission = await authorizeBatch(client, handed, batch.token);
        if (permission?.allowed !== true || !windowOpen()) {
          if (permission?.allowed === true) await client.settle(handed.unit, handed.token, 'rejected');
          await client.serialState('clear');
          return {status:permission?.rejected ? 'stale_frame' : 'unknown', reason:permission?.reason || 'authorize'};
        }
        return {status: 'handoff', text};
      }, waitWindow);
      if (result?.status === 'stale_frame') continue;
      if (result?.status === 'recheck_handoff') return await recheckCodexHandoff(client, descriptorPath, result);
      return result || {status: 'unknown', reason: 'busy'};
      }
    }
    return await locked(client, async () => {
      const s = (await client.serialState('read')).state;
      if (codexNative && mode === 'reject' && s?.offer !== expectedOffer) return {status:'unknown', reason:'handoff_changed'};
      if ((!s || s.prompt !== event.prompt_id) && event.hook_event_name === 'Stop' && nonempty(event.prompt_id)) {
        await client.endNative(event.prompt_id);
        if (codexNative && event.historical) return {status:'finished'};
        const ended = await client.hostEvent('turn_end', event.terminal_ts ?? Math.floor(Date.now()/1000),
          {turn_id: event.prompt_id, act_key: `${codexNative ? 'codex' : 'claude'}:turn_end:${event.prompt_id}`});
        return good(ended) ? {status: codexNative ? 'turn_end_reported' : 'finished'} :
          {status: 'unknown', reason: 'turn_end', retryStop: !codexNative && retryableHookFailure(ended)};
      }
      if (!codexNative && event.hook_event_name === 'UserPromptSubmit' && (!s || !promptMatches(event.prompt, s.text))
          && await receiveLateClaudeWake(client, event, promptMatches)) return {status:'running'};
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
        if (s.frame.kind === 'batch_round') {
          const out = await client.recordAdmission(s.unit, s.token, event.prompt_id);
          if (!good(out) || !out.recorded) return {status: 'unknown', reason: 'admission'};
        }
        s.prompt = event.prompt_id; s.phase = 'admitted'; await client.serialState('write', s);
        if (!nativeStart) return {status: 'admitted'};
      }
      if (!nonempty(event.prompt_id) || event.prompt_id !== s.prompt) return {status: 'unknown', reason: 'prompt_id'};
      if (event.hook_event_name === 'StopFailure' && s.phase === 'admitted') {
        const outcome = await client.settle(s.unit, s.token, 'unknown', s.prompt);
        if (!good(outcome) || outcome.state !== 'unknown') return {status:'unknown', reason:'settle'};
        await client.serialState('clear');
        return {status:'finished'};
      }
      if ((nativeStart || ['MessageDisplay', 'PostToolUse'].includes(event.hook_event_name)
           || (stopCanStart && event.hook_event_name === 'Stop')) && s.phase === 'admitted') {
        if (s.frame.kind === 'batch_round') {
          const run = await client.runBegin(s.unit, s.token, s.owner, s.prompt);
          if (!good(run) || !run.recorded) return {status: 'unknown', reason: 'run'};
        }
        const fields = {attempt_id:s.attempt};
        if (codexNative) {
          fields.turn_id = s.prompt;
          if (Number.isFinite(event.started_ts) && Number.isFinite(event.terminal_ts) && event.terminal_ts >= event.started_ts) {
            // A recovered terminal turn must never briefly appear as an expired live attempt.
            fields.terminal_ts = event.terminal_ts;
            fields.act_key = `codex:recovered_attempt:${s.attempt}`;
          }
        }
        const started = await client.hostEvent('round.started', event.started_ts ?? Math.floor(Date.now()/1000), fields);
        if (!good(started)) return {status: 'unknown', reason: 'started'};
        if (s.frame.kind === 'batch_round') {
          const settled = await client.settle(s.unit, s.token, 'accepted', s.prompt);
          if (!good(settled) || settled.state !== 'accepted') return {status: 'unknown', reason: 'settle'};
        }
        s.phase = 'running'; s.ackUnknown = true; await client.serialState('write', s);
        const ack = await client.wakeAck(s.unit, s.frame.due_key, undefined);
        s.ackUnknown = !(good(ack) && ack.taken); await client.serialState('write', s);
        if (event.hook_event_name !== 'Stop') {
          if (s.frame.kind === 'batch_round' && !s.ackUnknown) await client.serialState('clear');
          return {status: 'running', ack: s.ackUnknown ? 'unknown' : 'taken'};
        }
      }
      if (['Stop', 'StopFailure'].includes(event.hook_event_name) && s.phase === 'running') {
        if (s.frame.kind === 'batch_round') {
          const out = await client.consume(s.unit, {type: 'agent_settled', aborted: event.hook_event_name === 'StopFailure'}, s.owner, s.token);
          if (!good(out) || !out.recorded) return {status: 'unknown', reason: 'consume'};
        }
        if (!s.stopTs) { s.stopTs = event.terminal_ts ?? Math.floor(Date.now()/1000); await client.serialState('write', s); }
        if (codexNative && !good(await client.hostEvent('turn_end', s.stopTs,
          {attempt_id:s.attempt, attempt_only:true, act_key:`codex:attempt_end:${s.attempt}`})))
          return {status: 'unknown', reason: 'turn_end'};
        if (!codexNative || !event.historical) {
          const fields = codexNative ? {turn_id:s.prompt, act_key:`codex:turn_end:${s.prompt}`} : {attempt_id:s.attempt};
          const ended = await client.hostEvent('turn_end', s.stopTs, fields);
          if (!good(ended)) return {status: 'unknown', reason: 'turn_end', retryStop: !codexNative && retryableHookFailure(ended)};
        }
        await client.serialState('clear');
        return {status: 'finished'};
      }
      return {status: 'unknown', reason: 'event'};
    }, event.hook_event_name === 'Stop' && !codexNative ? windowOpen : undefined) || {status: 'unknown', reason: 'busy'};
  } finally { client.close(); }
}

if (import.meta.main ?? (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href)) {
  const mode = process.argv[2];
  let descriptorPath;
  const logWait = reason => {
    if (!descriptorPath || !['wait', 'stop'].includes(mode)) return;
    const dir = join(dirname(descriptorPath), '..', 'logs');
    try {
      mkdirSync(dir, {recursive:true, mode:0o700});
      appendFileSync(join(dir, 'claude-hook.log'),
        `${new Date().toISOString()} ${basename(descriptorPath, '.endpoint.json')} ${process.pid} ${mode} ${JSON.stringify(reason)}\n`, {mode:0o600});
    } catch { /* Diagnostics must not change native hook output. */ }
  };
  try {
    const input = JSON.parse(readFileSync(0, 'utf8'));
    descriptorPath = nativeDescriptor(null, 'claude', input.session_id);
    const parent = process.ppid;
    const args = {event: input, descriptorPath, reportWaitError: logWait,
      windowOpen: () => parent > 1 && process.ppid === parent};
    if (['wait', 'stop'].includes(mode))
      process.once('SIGTERM', () => { logWait('SIGTERM'); process.exit(143); });
    let result;
    if (mode === 'stop') {
      // The same native Stop must keep the same action parameters across daemon recovery.
      input.terminal_ts ??= Math.floor(Date.now()/1000);
      let retrying = false;
      const done = await reconnect(args.windowOpen, () => handleClaudeHook({...args, mode: 'observe'}), outcome => {
        if (!outcome?.retryStop) return false;
        if (!retrying) logWait('stop_retry: turn_end');
        retrying = true;
        return true;
      });
      if (!args.windowOpen()) result = {status:'window_closed'};
      else if (done.status !== 'finished' && done.reason !== 'no_pending')
        throw Error('stop remains unknown: ' + done.reason);
    }
    result ??= await handleClaudeHook({...args, mode: mode === 'stop' ? 'wait' : mode});
    logWait(args.windowOpen() ? [result.reason || result.status, result.error].filter(Boolean).join(':') : 'window_closed');
    if (result.status === 'handoff') { process.stderr.write(result.text + '\n'); process.exitCode = 2; }
    if (result.status === 'context') console.log(JSON.stringify({hookSpecificOutput:
      {hookEventName: input.hook_event_name, additionalContext: result.text}}));
  } catch (e) { logWait(e.message); process.stderr.write('Office hook unknown: ' + e.message + '\n'); process.exitCode = 1; }
}
