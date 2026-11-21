import {spawn, spawnSync} from 'node:child_process';
import {createInterface} from 'node:readline';
import {readFileSync} from 'node:fs';
import {handleClaudeHook} from './office_claude_hook.mjs';
import {nativeDescriptor, openSession} from './office_socket.mjs';
import {reportCodexUsage} from './office_codex_usage.mjs';

const [mode, session, dueKey] = process.argv.slice(2);
try {
  const event = mode === 'send' ? {session_id: session, hook_event_name: 'SessionStart'} : JSON.parse(readFileSync(0, 'utf8'));
  const ref = event.session_id, descriptorPath = nativeDescriptor(null, 'codex', ref);
  const bound = openSession({nativeSession: {surface: 'codex', ref}});
  if (!bound.ok) process.exit(0);
  try {
    if (event.hook_event_name === 'PostModelRequest') await reportCodexUsage(bound.core, event);
  } catch { /* incomplete or unavailable host facts remain uncovered */ }
  finally { await bound.core.close(); }
  const options = {event: {...event, prompt_id: event.turn_id}, descriptorPath, collectUsage: false,
    promptMatches: (prompt, expected) => typeof prompt === 'string' && prompt === expected,
    nativeStart: event.hook_event_name === 'PostModelRequest' && event.status === 'completed',
    stopCanStart: false, waitMs: 1, dueKey, codexNative: true};
  if (mode === 'send') {
    let result = await handleClaudeHook({...options, mode: 'prepare'});
    const retry = result?.reason === 'pending' && typeof result.text === 'string';
    if (result?.status !== 'handoff' && !retry) {
      if (result?.reason !== 'pending') console.error('office codex delivery: prepare', result?.reason || result?.status);
      process.exit(0);
    }
    const child = spawn('codex', ['app-server'], {stdio: ['pipe', 'pipe', 'ignore']});
    const rl = createInterface({input: child.stdout});
    let finished = false, timedOut = false;
    child.on('error', e => { console.error('office codex delivery: app-server', e.message); rl.close(); });
    child.stdin.on('error', e => console.error('office codex delivery: input', e.message));
    const timer = setTimeout(() => { timedOut = true; child.kill(); rl.close(); }, 10000);
    const call = (id, method, params) => child.stdin.write(JSON.stringify({id, method, params}) + '\n');
    let marker = result.text.split('\n', 1)[0];
    call(1, 'initialize', {clientInfo: {name: 'office', version: '1'}, capabilities: {experimentalApi: true}});
    for await (const line of rl) {
      const msg = JSON.parse(line);
      if (msg.id === 1 && msg.result) {
        if (retry) call(5, 'thread/queue/list', {threadId: ref, limit: 100});
        else call(2, 'thread/queue/add', {threadId: ref,
          clientUserMessageId: marker, input: [{type: 'text', text: result.text}]});
      }
      if (msg.error) {
        console.error('office codex delivery: RPC', JSON.stringify(msg.error));
        if (msg.id === 2) finished = (await handleClaudeHook({...options, mode: 'reject'}))?.status === 'rejected';
        break;
      }
      if (msg.id === 2 && msg.result?.queuedSubmission?.clientUserMessageId === marker)
        call(3, 'thread/turns/list', {threadId: ref, limit: 1, sortDirection: 'desc', itemsView: 'notLoaded'});
      if (msg.id === 3) {
        if (msg.result?.data?.[0]?.status !== 'completed') {
          console.error('office codex delivery: no nudge', msg.result?.data?.[0]?.status || 'unknown');
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
        if (msg.result.data.some(x => x.clientUserMessageId === marker)) { finished = true; break; }
        if (msg.result.nextCursor) { call(5, 'thread/queue/list', {threadId: ref, limit: 100, cursor:msg.result.nextCursor}); continue; }
        result = await handleClaudeHook({...options, mode:'prepare', absentMarker:marker});
        if (result?.status !== 'handoff') { finished = true; break; }
        marker = result.text.split('\n', 1)[0];
        call(2, 'thread/queue/add', {threadId:ref, clientUserMessageId:marker, input:[{type:'text',text:result.text}]});
      }
    }
    clearTimeout(timer); child.kill();
    if (!finished) console.error('office codex delivery:', timedOut ? 'app-server timed out' : 'queue outcome unknown');
  } else if (['UserPromptSubmit', 'PostModelRequest', 'Stop'].includes(event.hook_event_name)) {
    const result = await handleClaudeHook({...options, mode: 'observe'});
    if (result?.status === 'obsolete') {
      const text = JSON.parse(readFileSync(new URL('../delivery/messages.json', import.meta.url), 'utf8'));
      process.stderr.write(text.idle_obsolete + '\n'); process.exitCode = 2;
    }
  }
} catch (e) { console.error('office codex delivery: unknown', e.message); }
