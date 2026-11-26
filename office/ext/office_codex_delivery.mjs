import {spawn, spawnSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {createInterface} from 'node:readline';
import {handleClaudeHook, recordCodexUnsent} from './office_claude_hook.mjs';
import {nativeDescriptor, openSession} from './office_socket.mjs';
import {reportCodexUsage} from './office_codex_usage.mjs';

async function reportTurn(core, turn) {
  const report = (kind, ts, fields = {}) => core.handle({op:'host.event', kind, ts, turn_id:turn.id,
    act_key:'codex:observation:' + kind + ':' + turn.id, ...fields});
  const outcome = {completed:'completed', failed:'failed', interrupted:'interrupted'}[turn.status];
  if (!outcome || !Number.isFinite(turn.completedAt)) return;
  const failure = turn.error?.codexErrorInfo;
  const category = typeof failure === 'string' ? failure : Object.keys(failure || {})[0];
  const status = failure?.[category]?.httpStatusCode;
  if (outcome === 'failed' && failure === 'usageLimitExceeded')
    await report('quota', turn.completedAt, {reason:'quota_exhausted', status:429});
  await report('turn_end', turn.completedAt, {...(outcome ? {outcome} : {}),
    ...(outcome === 'failed' && category ? {reason:category} : {}),
    ...(Number.isInteger(status) ? {status} : {})});
}

const [mode, session, arg, monitoredTurn] = process.argv.slice(2);
const dueKey = mode === 'send' ? arg : undefined;
const candidates = mode === 'poll' && arg ? JSON.parse(arg) : [];
const wanted = new Set(candidates.map(row => row.token));
const nativeRefs = new Set(candidates.map(row => row.native_ref).filter(ref => typeof ref === 'string' && ref));
const earliest = Math.min(...candidates.map(row => row.begun_ts).filter(Number.isFinite));
try {
  if (mode === 'monitor') {
    const event = JSON.parse(readFileSync(0, 'utf8'));
    await handleClaudeHook({event, mode, descriptorPath:nativeDescriptor(null, 'codex', event.session_id),
      hostPid:process.ppid, codexNative:true});
    process.exit(0);
  }
  if (!['send', 'poll'].includes(mode)) process.exit(0);
  const ref = session, descriptorPath = nativeDescriptor(null, 'codex', ref);
  const nativeSession = {surface: 'codex', ref};
  const options = {event: {session_id: session, hook_event_name:'SessionStart'}, descriptorPath, collectUsage: false,
    promptMatches: (prompt, expected) => typeof prompt === 'string' && prompt === expected,
    stopCanStart: false, waitMs: 1, dueKey, codexNative: true};
  {
    let result = await handleClaudeHook({...options, mode: 'prepare'});
    const retry = result?.reason === 'pending' && typeof result.text === 'string';
    const handed = result?.status === 'handoff';
    if (!handed && !retry && mode === 'send') process.exit(0);
    let unsent = handed && result.state, nativeWrite = false;
    try {
    const child = spawn('codex', ['app-server'], {stdio: ['pipe', 'pipe', 'ignore']});
    const rl = createInterface({input: child.stdout});
    let finished = false, timedOut = false;
    child.on('error', e => { console.error('office codex delivery: app-server', e.message); rl.close(); });
    child.stdin.on('error', e => console.error('office codex delivery: input', e.message));
    const timer = setTimeout(() => { timedOut = true; child.kill(); rl.close(); }, 10000);
    const call = (id, method, params) => child.stdin.write(JSON.stringify({id, method, params}) + '\n');
    async function enqueue() {
      const permission = await handleClaudeHook({...options, mode:'authorize', expectedOffer:result.state?.offer});
      if (permission?.allowed !== true || timedOut || child.exitCode !== null) {
        if (permission && !permission.allowed && !permission.unknown) unsent = false;
        finished = true;
        return;
      }
      nativeWrite = true;
      call(2, 'thread/queue/add', {threadId:ref, clientUserMessageId:marker, input:[{type:'text', text:result.text}]});
      if (result.state?.frame?.kind === 'batch_round')
        await handleClaudeHook({...options, mode:'attempted', expectedOffer:result.state.offer});
    }
    let usageTurn, markerFound = false, firstPage = true, currentTurn;
    let marker = result.text?.split('\n', 1)[0];
    call(1, 'initialize', {clientInfo: {name: 'office', version: '1'}, capabilities: {experimentalApi: true}});
    const stream = rl[Symbol.asyncIterator]();
    for (let next; !(next = await stream.next()).done;) {
      const msg = JSON.parse(next.value);
      if (msg.id === 1 && msg.result) {
        if (retry) call(5, 'thread/queue/list', {threadId: ref, limit: 100});
        else if (handed) await enqueue();
        else call(6, 'thread/turns/list', {threadId: ref, limit: wanted.size ? 20 : 1, sortDirection: 'desc', itemsView: wanted.size ? 'full' : 'notLoaded'});
      }
      if (msg.error) {
        console.error('office codex delivery: RPC', JSON.stringify(msg.error));
        // A native error reply does not universally prove the queue write rolled back.
        if (msg.id === 2) finished = true;
        break;
      }
      if (msg.id === 2 && msg.result?.queuedSubmission?.clientUserMessageId === marker)
        call(3, 'thread/turns/list', {threadId: ref, limit: 1, sortDirection: 'desc', itemsView: 'notLoaded'});
      if (msg.id === 3) {
        const turns = msg.result?.data, latest = turns?.[0];
        if (!Array.isArray(turns) || (turns.length && (!['completed', 'interrupted', 'failed'].includes(latest?.status) || !Number.isFinite(latest.completedAt)))) {
          finished = true; break;
        }
        call(4, 'thread/queue/list', {threadId: ref, limit: 100});
      }
      if (msg.id === 4) {
        const pending = msg.result?.data?.some(x => x.clientUserMessageId === marker);
        if (pending && process.platform === 'darwin') {
          const processes = spawnSync('/bin/ps', ['-axo', 'comm='], {encoding: 'utf8', timeout: 1000});
          if (processes.stdout?.split('\n').some(path => path.trim() === '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT')) {
            const opened = spawnSync('open', ['-g', `codex://threads/${ref}`], {timeout: 10000});
            if (opened.status !== 0) console.error('office codex delivery: open failed', opened.error?.message || opened.status);
          }
        }
        finished = true; break;
      }
      if (msg.id === 5) {
        if (!Array.isArray(msg.result?.data)) break;
        if (msg.result.data.some(x => x.clientUserMessageId === marker)) {
          await handleClaudeHook({...options, mode:'seen', expectedOffer:result.offer, event:{...options.event, client_id:marker}});
          call(3, 'thread/turns/list', {threadId: ref, limit: 1, sortDirection: 'desc', itemsView: 'notLoaded'});
          continue;
        }
        if (msg.result.nextCursor) { call(5, 'thread/queue/list', {threadId: ref, limit: 100, cursor:msg.result.nextCursor}); continue; }
        call(6, 'thread/turns/list', {threadId: ref, limit: 20, sortDirection: 'desc', itemsView: 'full'});
      }
      if (msg.id === 6 && Array.isArray(msg.result?.data)) {
        const terminal = turn => ['completed', 'interrupted', 'failed'].includes(turn?.status) && Boolean(turn.id);
        const turns = msg.result.data;
        const initial = firstPage; if (initial) { currentTurn = turns[0]; firstPage = false; }
        if (initial && monitoredTurn && currentTurn?.id === monitoredTurn && terminal(currentTurn)) {
          const bound = openSession({nativeSession});
          if (bound?.ok) {
            try { await reportTurn(bound.core, currentTurn); }
            finally { await bound.core.close(); }
          }
        }
        for (const turn of turns) for (const item of turn.items || []) {
          if (item.type !== 'userMessage' || item.clientId === marker || item.content?.length !== 1
              || typeof item.content[0]?.text !== 'string' || !item.clientId?.startsWith('[Office wake ')
              || ![...item.content[0].text.matchAll(/\[Office wake ([a-f0-9]{32})\]/g)].some(match => wanted.has(match[1]))) continue;
          const seen = await handleClaudeHook({...options, mode:'history', event:{...options.event, prompt_id:turn.id,
            client_id:item.clientId, prompt:item.content[0].text,
            terminal_ts:terminal(turn) && Number.isFinite(turn.completedAt) ? turn.completedAt : undefined}});
          for (const token of seen?.tokens || []) wanted.delete(token);
          if (seen?.tokens?.length && terminal(turn) && Number.isFinite(turn.completedAt)) nativeRefs.delete(turn.id);
        }
        for (const turn of turns) if (terminal(turn) && Number.isFinite(turn.completedAt) && nativeRefs.has(turn.id)) {
          const bound = openSession({nativeSession});
          if (bound?.ok) {
            try {
              const ended = await bound.core.handle({op:'round.run.end', native_ref:turn.id});
              if (Array.isArray(ended?.recorded)) { nativeRefs.delete(turn.id); for (const token of ended.recorded) wanted.delete(token); }
            }
            finally { await bound.core.close(); }
          }
        }
        const found = marker && !markerFound && turns.find(turn => Boolean(turn.id) && turn.items?.some(item => item.type === 'userMessage'
          && item.clientId === marker && item.content?.length === 1
          && (result.batch || item.content[0]?.text === result.text)));
        if (found) {
          await handleClaudeHook({...options, mode:'seen', expectedOffer:result.offer, event:{...options.event, client_id:marker}});
          markerFound = true;
          const prompt = found.items.find(item => item.type === 'userMessage' && item.clientId === marker).content[0].text;
          const evidence = {...options.event, prompt_id:found.id, prompt, started_ts:found.startedAt,
            terminal_ts:terminal(found) && Number.isFinite(found.completedAt) ? found.completedAt : undefined,
            historical:found.id !== currentTurn?.id, aborted: found.status !== 'completed'};
          if (result.batch) {
            const receipt = await handleClaudeHook({...options, mode:'receipt', expectedOffer:result.offer, event:evidence});
            for (const token of receipt?.tokens || []) wanted.delete(token);
          } else if (!result.batch) {
            const observe = (name, nativeStart = false) => handleClaudeHook({...options, mode:'observe', nativeStart,
              event: {...evidence, hook_event_name:name}});
            const opened = await observe('UserPromptSubmit', true);
            if (opened?.reason === 'prompt') await observe('PostModelRequest', true);
            if (terminal(found) && Number.isFinite(found.completedAt)) await observe('Stop');
          }
          if (terminal(found) && Number.isFinite(found.completedAt)) usageTurn = found.id;
        }
        if (initial && currentTurn && terminal(currentTurn) && Number.isFinite(currentTurn.completedAt)
            && !(found?.id === currentTurn.id && !result.batch)) {
          await handleClaudeHook({...options, mode:'observe', event:{...options.event, prompt_id:currentTurn.id,
            terminal_ts:currentTurn.completedAt, hook_event_name:'Stop'}});
          usageTurn = currentTurn.id;
        }
        if (!found && marker && !markerFound) {
          if (msg.result.nextCursor) {
            call(6, 'thread/turns/list', {threadId:ref, limit:20, sortDirection:'desc', itemsView:'full',
              cursor:msg.result.nextCursor});
            continue;
          }
          result = await handleClaudeHook({...options, mode:'prepare', absentMarker:marker, expectedOffer:result.offer});
          if (result?.status === 'handoff') {
            unsent = result.state;
            nativeWrite = false;
            marker = result.text.split('\n', 1)[0];
            await enqueue();
            continue;
          }
        }
        const times = turns.map(turn => turn.startedAt).filter(Number.isFinite);
        if (wanted.size && msg.result.nextCursor && (!times.length || Math.min(...times) >= Math.floor(earliest))) {
          call(6, 'thread/turns/list', {threadId:ref, limit:20, sortDirection:'desc', itemsView:'full', cursor:msg.result.nextCursor});
          continue;
        }
        if (usageTurn) { call(7, 'thread/read', {threadId:ref, includeTurns:false}); continue; }
        finished = true;
        break;
      }
      if (msg.id === 7) {
        const path = msg.result?.thread?.id === ref && msg.result.thread.path;
        const bound = path && openSession({nativeSession});
        if (bound?.ok) {
          try { await reportCodexUsage(bound.core, {session_id:ref, turn_id:usageTurn, transcript_path:path}); }
          finally { await bound.core.close(); }
        }
        finished = true;
        break;
      }
    }
    clearTimeout(timer); child.kill();
    if (!finished) console.error('office codex delivery:', timedOut ? 'app-server timed out' : 'queue outcome unknown');
    } finally { if (unsent && !nativeWrite) recordCodexUnsent(descriptorPath, unsent); }
  }
} catch (e) { console.error('office codex delivery: unknown', e.message); }
