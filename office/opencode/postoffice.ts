import { homedir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { setTimeout as sleep } from 'node:timers/promises';
import { join } from 'node:path';
import { openControlledEndpoint, createOfficeSocket } from '../ext/office_socket.mjs';
import { createOfficeTools, validateArgs } from '../ext/office_tools.mjs';
import { toolSpecs } from '../ext/office_tool_spec.mjs';
import { renderToolResult } from '../ext/office_result.mjs';
import { createPiRoundClient, releaseVersion } from '../ext/office_pi_round.mjs';
import { createWakeLoop } from '../ext/office_wake_loop.mjs';
import TOOL_TEXT from '../ext/office_tool_text.json' with { type: 'json' };

const hintPath = (home: string, name: any) => typeof name === 'string' && /^[A-Za-z0-9_.-]+\.endpoint\.json$/.test(name)
  ? join(home, 'sock', name) : '';
export async function receiptPages(storage: any, prefix: string) {
  const entries: any[] = []; let after: string | undefined;
  do { const page = await storage.scan({prefix, ...(after ? {after} : {})}); entries.push(...page.entries); after = page.next; } while (after);
  return entries;
}
async function bind(ctx: any, endpoint: any, expectedSession?: string, signal?: AbortSignal) {
    if (!endpoint.ok || !endpoint.endpoint.hostSessionRef) return;
    const sessionID = endpoint.endpoint.hostSessionRef;
    if (expectedSession && sessionID !== expectedSession) return;
    const directory = ctx.location?.directory;
    const ownsSession = async () => typeof directory === "string" && Boolean(directory)
      && (await ctx.session.get({sessionID}))?.location?.directory === directory;
    if (!await ownsSession() || signal?.aborted) return;
    const client = createPiRoundClient({socketPath: endpoint.socketPath, tokenFile: endpoint.tokenFile});
    let who: any;
    try { who = await client.context(); } finally { client.close(); }
    if (who?.unknown) throw new Error('endpoint temporarily unavailable');
    if (!who || who.ok === false || who.actor !== endpoint.endpoint.actor || signal?.aborted) return;
    const socket = createOfficeSocket({socketPath: endpoint.socketPath, tokenFile: endpoint.tokenFile});
    const office = createOfficeTools({endpoint: endpoint.endpoint, core: socket});
    let pending: any = null;
    let closed = false;
    let officeRunning = false, officeStep = '', officeStarted = 0, stepStarted = 0;
    const controller = new AbortController();
    const loop = createWakeLoop({client, box: endpoint.endpoint.actor,
      diagFailure: (message: string) => console.error('OpenCode Office:', message), host: {
      sessionRef: () => sessionID,
      receiptEntries: async () => (await receiptPages(ctx.storage, `office.receipt.${sessionID}.`))
        .map((e: any) => ({type:'custom', customType:'office.receipt', data:e.value})),
      saveReceipt: async (receipt: any) => {
        const key = `office.receipt.${sessionID}.${receipt.token}`;
        if (receipt.done) await ctx.storage.remove(key); else await ctx.storage.set(key, receipt);
      },
      receiptText: async (receipt: any) => {
        if (!await ownsSession()) return;
        const state = await client.roundState(receipt.unit, receipt.token);
        if (state?.ok === false) { if (state.reason_code === 'unit_not_carried') return; throw new Error('round payload unavailable'); }
        return state?.payloads?.find((p: any) => p.ref === receipt.anchorRef && p.attempt_key === receipt.attemptKey)?.text;
      },
      instanceId: () => `oc2:${endpoint.endpoint.instance}@${sessionID}`,
      acceptWhileBusy: true,
      conditionalOwner: true,
      beforeBegin: async () => {
        if (closed || !await ownsSession()) throw new Error('Office session is unavailable or belongs to another OpenCode location');
      },
      triggerTurn: async (text: string, details: any) => {
        const owned = await ownsSession().catch(() => false);
        if (closed || signal?.aborted || !owned) throw Object.assign(new Error('Office session is unavailable or belongs to another OpenCode location'), {notSent:true});
        const current = pending = {text, details, messageID: '', events: [], ready: false};
        let admitted: any;
        try { admitted = await ctx.session.prompt({sessionID, text, delivery: 'steer'}); }
        catch (error) { if (pending === current) pending = null; throw error; }
        if (!admitted?.id || admitted.id !== current.messageID || admitted.sessionID !== sessionID || admitted.payload?.text !== text)
          { pending = null; throw new Error('OpenCode prompt admission did not match the submitted batch'); }
        current.ready = true;
        void drain(current).catch(console.error);
      },
    }});
    const hook = await ctx.session.hook('prompt', async (event: any) => {
      if (pending && event.sessionID === sessionID && event.prompt?.text === pending.text) {
        if (!event.messageID || !await loop.submittedExact(pending.text, {}, event.messageID)) throw new Error('Office admission refused before enqueue');
        pending.messageID = event.messageID;
      }
    });
    if (signal?.aborted) { await hook.dispose(); await socket.close(); await client.close(); return; }
    const responseHook = await ctx.session.hook('http.response', async (event: any) => {
      if (event.sessionID !== sessionID || event.response?.status !== 402 || !await ownsSession()) return;
      try {
        const result = await client.hostEvent('quota', Date.now() / 1000, {
          act_key: `oc2:quota:http:${randomUUID()}`, status: 402,
        });
        if (result?.ok === false) console.error(`OpenCode quota: ${result.code || 'unknown'}`);
      } catch (error) { console.error('OpenCode quota:', error); }
    });
    if (signal?.aborted) { await hook.dispose(); await responseHook.dispose(); await socket.close(); await client.close(); return; }
    const terminal = (event: any) => ['session.execution.succeeded', 'session.execution.failed', 'session.execution.interrupted'].includes(event.type);
    async function reportEnd(event: any) {
      officeRunning = false; officeStep = '';
      const closed = loop.turnClosed('');
      await loop.agentSettled({aborted: event.type !== 'session.execution.succeeded'}, {});
      const result = await client.hostEvent('turn_end', event.created / 1000, {
        act_key: `oc2:turn_end:${event.id}`, ...(closed?.attempt_id ? {attempt_id: closed.attempt_id} : {}),
      });
      if (result?.ok === false) throw new Error(`OpenCode turn_end: ${result.code || 'unknown'}`);
    }
    const toolsInFlight = new Map<string, {revision: number; seen: Set<string>; skill?: string; name?: string}>();
    const toolNames = new Map<string, string>();
    const stepUsage = new Map<string, {refs: Set<string>, model?: string, started?: number, startId?: string, progress?: boolean}>();
    const fingerprint = (value: any): string => JSON.stringify(value, (_key, item) =>
      item && typeof item === 'object' && !Array.isArray(item)
        ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);
    async function reportTool(event: any) {
      const kinds: Record<string, string> = {'session.tool.called': 'tool_start',
        'session.tool.progress': 'tool_progress', 'session.tool.success': 'tool_end', 'session.tool.failed': 'tool_end'};
      const kind = kinds[event.type], data = event.data;
      if (event.type === 'session.tool.input.started' && data?.sessionID === sessionID
          && data.assistantMessageID && data.id && ['skill', 'office_work_publish', 'office_work_review'].includes(data.name) && await ownsSession()) {
        toolNames.set(JSON.stringify([data.assistantMessageID, data.id]), data.name);
      }
      if (!kind || data?.sessionID !== sessionID || !event.id || !Number.isFinite(event.created)
          || !data.assistantMessageID || !data.id || !await ownsSession()) return;
      const key = JSON.stringify([data.assistantMessageID, data.id]);
      let state = toolsInFlight.get(key);
      if (kind === 'tool_start') {
        if (state) return;
        const name = toolNames.get(key);
        const skill = name === 'skill' ? data.input?.id : undefined;
        toolNames.delete(key);
        state = {revision: 0, seen: new Set(), skill, name};
        toolsInFlight.set(key, state);
      } else if (!state) return;
      if (kind === 'tool_progress') {
        if (!data.metadata || typeof data.metadata !== 'object' || !Object.keys(data.metadata).length) return;
        const signature = fingerprint(data.metadata);
        if (state.seen.has(signature)) return;
        state.seen.add(signature);
        state.revision += 1;
      }
      const result = await client.hostEvent(kind, event.created / 1000, {
        act_key: `oc2:tool:${event.id}`, turn_id: data.assistantMessageID, native_id: data.id,
        ...(kind === 'tool_progress' ? {version: String(state.revision)} : {}),
        ...(kind === 'tool_end' ? {outcome: event.type === 'session.tool.success' ? 'ok' : 'error'} : {}),
      });
      if (result?.ok === false) throw new Error(`OpenCode ${kind}: ${result.code || 'unknown'}`);
      if (kind === 'tool_end') {
        toolsInFlight.delete(key);
        if (event.type === 'session.tool.success' && ['office_work_publish', 'office_work_review'].includes(state.name || '')) {
          try {
            const output = JSON.parse(data.content?.find((part: any) => part.type === 'text')?.text);
            const ref = state.name === 'office_work_publish' ? output.sub_ref : output.review_ref;
            if (typeof ref === 'string' && ref.trim()) {
              const usage = stepUsage.get(data.assistantMessageID) || {refs: new Set<string>()};
              usage.refs.add(ref); stepUsage.set(data.assistantMessageID, usage);
            }
          } catch { /* No exact committed reference: leave usage unassigned. */ }
        }
        if (event.type === 'session.tool.success' && state.skill === 'opencode'
            && data.metadata?.directory === 'skill://opencode') {
          const context = await client.context();
          const position = context?.positions?.find((p: any) => p.role === endpoint.endpoint.bindingId);
          for (const resource of position?.resources || []) {
            if (resource.kind !== 'skill' || resource.locator !== 'skill://opencode/SKILL.md') continue;
            const observed = await client.hostEvent('resource', Math.floor(event.created / 1000), {
              act_key: `oc2:resource:${event.id}:${resource.resource_ref}`, resource_ref: resource.resource_ref, loaded: true, usable: true,
            });
            if (observed?.ok === false) throw new Error(`OpenCode resource: ${observed.code || 'unknown'}`);
          }
        }
      }
    }
    async function reportUsage(event: any) {
      const data = event.data, id = data?.assistantMessageID;
      if (data?.sessionID !== sessionID || !id) return;
      if (event.type === 'session.step.started') {
        stepUsage.set(id, {refs: new Set(), model: typeof data.model?.id === 'string' ? data.model.id : undefined, started: event.created, startId: event.id});
      }
      if (event.type !== 'session.step.ended'
          || !event.id || !Number.isFinite(event.created) || !await ownsSession()) return;
      const usage = stepUsage.get(id); stepUsage.delete(id);
      const count = (n: any) => Number.isInteger(n) && n >= 0 ? n : undefined;
      const usageTokens = data.tokens, input = count(usageTokens?.input), output = count(usageTokens?.output);
      const tokens = {in: input === undefined ? undefined : input + (count(usageTokens?.cache?.write) ?? 0),
        out: output === undefined ? undefined : output + (count(usageTokens?.reasoning) ?? 0),
        cache: count(usageTokens?.cache?.read)};
      if (!Object.values(tokens).some(Boolean)) return;
      const entry = {entry_id: id, at: event.created / 1000, tokens,
        ...(usage?.model ? {model: usage.model} : {}),
        ...(usage?.refs.size === 1 ? {ref: [...usage.refs][0]} : {})};
      const result = await client.usageRecord([entry], `oc2:usage:${event.id}`);
      if (result?.ok === false) throw new Error(`OpenCode usage: ${result.code || 'unknown'}`);
    }
    async function reportProgress(event: any) {
      const data = event.data;
      if (data?.sessionID !== sessionID) return;
      if (event.type === 'session.inbox.delivered') { officeRunning = false; officeStep = ''; }
      if (event.type === 'session.step.started' && officeRunning && Number.isFinite(event.created) && event.created >= officeStarted
          && typeof data.assistantMessageID === 'string' && data.assistantMessageID) {
        officeStep = data.assistantMessageID; stepStarted = event.created;
      }
      if (event.type === 'session.step.ended' && data.assistantMessageID === officeStep) officeStep = '';
      if (event.type === 'session.text.delta' && officeRunning && officeStep && data.assistantMessageID === officeStep
          && typeof event.id === 'string' && event.id && Number.isFinite(event.created) && event.created >= stepStarted
          && typeof data.delta === 'string' && data.delta && await ownsSession())
        await loop.modelProgress({turnId:officeStep});
    }
    async function drain(current: any) {
      if (pending !== current || !current.ready || current.processing) return;
      current.processing = true;
      try {
        while (current.events.length) {
          const event = current.events.shift();
          if (event.type === 'session.inbox.delivered' && event.data?.inboxID === current.messageID) {
            if (await loop.agentStart({})) {
              officeRunning = Number.isFinite(event.created); officeStarted = event.created; officeStep = '';
              if (pending === current) pending = null;
            }
          } else if (terminal(event)) {
            const late = loop.lateNativeRunning();
            try { await reportEnd(event); }
            finally { if (!late && pending === current) pending = null; }
          } else await reportProgress(event);
        }
      } finally { current.processing = false; }
    }
    void (async () => {
      for await (const event of ctx.event.subscribe({signal: controller.signal})) {
        try {
          const error = event.data?.error;
          if (event.type === 'session.execution.failed' && event.data?.sessionID === sessionID
              && ((error?.type === 'provider.quota' && error.status === 402)
                || (error?.type === 'provider.go-limit' && error.status === 429))
              && typeof event.id === 'string' && event.id && Number.isFinite(event.created) && await ownsSession()) {
            const result = await client.hostEvent('quota', event.created / 1000, {
              act_key: `oc2:quota:${event.id}`, status: error.status,
              ...(error.type === 'provider.go-limit' ? {reason: 'quota_exhausted'} : {}),
            });
            if (result?.ok === false) throw new Error(`OpenCode quota: ${result.code || 'unknown'}`);
          }
          const step = stepUsage.get(event.data?.assistantMessageID);
          if (event.type === 'session.text.delta' && event.data?.sessionID === sessionID
              && event.id && step?.startId && !step.progress && Number.isFinite(step.started)
              && Number.isFinite(event.created) && event.created >= step.started!
              && typeof event.data.delta === 'string' && event.data.delta.trim() && await ownsSession()) {
            const result = await client.hostEvent('model_progress', step.started! / 1000, {
              act_key: `oc2:progress:${step.startId}`, native_id: step.startId, turn_id: event.data.assistantMessageID,
            });
            if (result?.ok !== false && !result?.unknown) step.progress = true;
          }
          await reportTool(event);
          await reportUsage(event);
          if (event.data?.sessionID === sessionID && terminal(event)) stepUsage.clear();
          if (event.type === 'session.inbox.delivered' && event.data?.sessionID === sessionID
              && event.data.inboxID && event.data.inboxID !== pending?.messageID
              && await loop.lateNativeStart(event.data.inboxID, {})) {
            officeRunning = Number.isFinite(event.created); officeStarted = event.created; officeStep = '';
            continue;
          }
          if (pending && event.data?.sessionID === sessionID) {
            pending.events.push(event);
            await drain(pending);
          } else if (event.data?.sessionID === sessionID && terminal(event)) {
            await reportEnd(event);
          } else await reportProgress(event);
        } catch (error) { console.error('OpenCode Office event:', error); }
      }
    })().catch(() => {});
    loop.start({});
    const version = releaseVersion(import.meta.url)?.version ?? null;
    void client.hostEvent('loaded', Math.floor(Date.now() / 1000), {version});
    return {office, close: async () => { closed = true; controller.abort(); loop.stop(); await hook.dispose(); await responseHook.dispose(); socket.close({final:true}); }};
}
export function createOfficePlugin(configuredHome?: string) { return {
  id: 'postoffice-delivery',
  async setup(ctx: any) {
    const home = configuredHome || process.env.POSTOFFICE_HOME || join(homedir(), 'office11');
    const sessions = new Map<string, Promise<Awaited<ReturnType<typeof bind>>>>();
    const configured = !configuredHome && process.env.OFFICE_ENDPOINT_FILE;
    let remembered = '';
    const recovery = new AbortController();
    const directory = ctx.location?.directory;
    const owns = async (id: string) => typeof directory === 'string' && Boolean(directory)
      && /^ses_[A-Za-z0-9]+$/.test(id)
      && (await ctx.session.get({sessionID: id}))?.location?.directory === directory;
    async function attached(id: string, supplied = '') {
      if (!sessions.has(id)) {
        const opening = (async () => {
          const native = hintPath(home, 'opencode-' + id + '.endpoint.json');
          let pending = false, refused = false;
          for (const file of new Set([supplied, native, configured, remembered])) {
            if (!file) continue;
            const observed = openControlledEndpoint({descriptor: file});
            try {
              const session = await bind(ctx, observed, id, recovery.signal);
              if (session) { if (recovery.signal.aborted) { await session.close(); return; } return session; }
              if (observed.ok && observed.endpoint.hostSessionRef === id || file === native && observed.error === 'endpoint_untrusted') refused = true;
            } catch { pending = true; }
          }
          if (pending && !refused) throw new Error('Office endpoint temporarily unavailable');
        })();
        sessions.set(id, opening);
        void opening.then(session => { if (!session) sessions.delete(id); }, () => sessions.delete(id));
      }
      return sessions.get(id);
    }
    const command = await ctx.command.transform((commands: any) => commands.add({
      name: 'office-connect', description: TOOL_TEXT._office_connect.description,
      execute: async ({sessionID, prompt}: any) => {
        const file = String(prompt?.text || '').trim();
        const name = file.split('/').pop();
        const selected = file && hintPath(home, name) === file && openControlledEndpoint({descriptor: file});
        if (!selected || !selected.ok || selected.endpoint.hostSessionRef !== sessionID
            || !await owns(sessionID) || !await attached(sessionID, file).catch(() => undefined))
          throw new Error(TOOL_TEXT._opencode_connect.wrong_session);
        await ctx.storage.set('office.endpoint', name);
        await ctx.session.synthetic({sessionID, text: TOOL_TEXT._office_connect.connected, resume: false});
      },
    }));
    const prompt = await ctx.session.hook('prompt', async ({sessionID}: any) => {
      if (await owns(sessionID)) await attached(sessionID).catch(() => undefined);
    });
    const locale = process.env.OFFICE_LOCALE === 'zh' ? 'zh' : 'en';
    const registration = await ctx.tool.transform((tools: any) => {
      for (const spec of toolSpecs(locale)) tools.add({
        name: spec.name, description: spec.description,
        input: spec.parameters, options: {codemode: false},
        execute: async (args: any, {sessionID}: any) => {
          if (!await owns(sessionID)) throw new Error(TOOL_TEXT._opencode_connect.wrong_session);
          const linked = await attached(sessionID).catch(() => undefined);
          let result;
          if (linked) result = await linked.office.callTool(spec.name, args);
          else if (spec.name !== 'office_request_create' || args?.kind !== 'join') {
            return {content: TOOL_TEXT._office_join.required[locale]};
          } else {
            result = validateArgs(spec, args);
            if (!result) {
              const socket = createOfficeSocket({socketPath: join(home, 'sock', 'officed.sock')});
              try { result = await socket.handle({op: 'request.create', kind: 'join', reason: args.reason,
                native_session: {surface: 'opencode', ref: sessionID}, act_key: 'join:' + randomUUID()}); }
              finally { socket.close(); }
            }
          }
          return {content: renderToolResult(result, locale).content.map((part: any) => part.text || '').join('\n')};
        },
      });
    });
    const retry = async (action: () => Promise<any>) => {
      let delay = 250;
      while (!recovery.signal.aborted) {
        try { return await action(); }
        catch (error) { console.error('OpenCode Office receipt restore:', error); }
        try { await sleep(delay, undefined, {signal: recovery.signal, ref: false}); }
        catch { return; }
        delay = Math.min(delay * 2, 4000);
      }
    };
    void (async () => {
      remembered = hintPath(home, await retry(() => ctx.storage.get('office.endpoint')));
      if (recovery.signal.aborted) return;
      const initial = openControlledEndpoint({descriptor: configured || remembered});
      if (initial.ok && initial.endpoint.hostSessionRef) void retry(() => attached(initial.endpoint.hostSessionRef));
      const entries = await retry(() => receiptPages(ctx.storage, 'office.receipt.'));
      if (!entries || recovery.signal.aborted) return;
      for (const id of new Set(entries.map((entry: any) => entry.value?.sessionFile).filter(Boolean)))
        void retry(async () => { if (!await owns(id as string) || recovery.signal.aborted) return; await attached(id as string); });
    })();
    return async () => {
      recovery.abort();
      await prompt.dispose(); await registration.dispose(); await command.dispose();
      for (const session of sessions.values()) void session.then(value => value?.close()).catch(() => {});
    };
  },
}; }
export default createOfficePlugin();
