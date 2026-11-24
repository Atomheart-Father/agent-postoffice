import {createHash, randomUUID} from "node:crypto";
import {setTimeout as delay} from "node:timers/promises";
// ★ W439 首切片：本树只保留 Pi 原生片段，`office_daemon.mjs` 不在保留集里。这里改用
//   **既有 socket 模块**之上的 Pi 侧投影（`office_pi_round.mjs`）：同一条受控 socket、一份方法表，
//   不造第二入口、不轮询。客户端仍由调用方注入（夹具可以传自己的）。
import {isCreated, hasCreatedFact, hostEventFrame} from "./office_pi_round.mjs";

// Core selects and owns each batch. The native adapter only chooses a safe
// point to hand it over, reports receipt, and separately reports execution end.

/** 默认参数（都可由调用方覆盖，便于夹具里把时间压到毫秒级）。 */
const DEFAULTS = {
  /** 单次 `round.await` 的有界等待；到点无 wake 就再挂一次（持续等待靠这个循环）。 */
  waitMs: 30_000,
  /** 失败退避：从 base 起翻倍，封顶 max。 */
  backoffBaseMs: 500,
  backoffMaxMs: 30_000,
  /** `model_progress` 的传输节流（每回合第一条不受限）。它是流量旋钮，不进静默判据。 */
  modelProgressMinMs: 1000,
};

async function sleep(ms, signal) {
  try { await delay(ms, undefined, {signal}); }
  catch (error) { if (!signal.aborted) throw error; }
}

/**
 * 建一条会话级等待（**未启动**）。
 *
 * @param {object} opts
 * @param {object} opts.client       `createDaemonClient(...)` 的产物
 * @param {string} opts.box          本箱（身份由连接决定，这里只做定位）
 * @param {object} opts.host         宿主端口：`{sessionRef(), instanceId(), triggerTurn(batchText, details)}`
 * @param {(s:string, ctx?:any)=>void} [opts.diag]
 * @param {(s:string, ctx?:any)=>void} [opts.diagFailure]
 * @param {object} [opts.tuning]     覆盖 `DEFAULTS`
 */
export function createWakeLoop({
  client,
  box,
  host,
  diag = () => {},
  diagFailure = () => {},
  tuning = {},
} = {}) {
  const cfg = { ...DEFAULTS, ...(tuning || {}) };
  const state = {
    running: false,
    stopped: false,
    awaitingFrames: false,
    /** 主循环睡眠的关闭信号。 */
    stopController: new AbortController(),
    /** 连续 empty wake 次数（用于把间隔放长）。 */
    awaitFailures: 0,
    /** Pi 正在执行时收到、尚未交给工具边界的邮件帧。 */
    pending: null,
    /** ★ Core batch 已 begun；native wake 的 admission/consume 仍未知时保留此槽。 */
    inflightRound: null,
    runningBatches: [],
    restoredReceipts: false,
    //: 本趟已看过、但**不能回执**的帧（在飞 / 拒帧）：它们仍在盘上，服务端每趟都会再给一遍。
    //  `skip`（服务端不存状态，由宿主给）就是为这个存在的；不清空它，循环会空转（实测 4 连发）。
    //  一轮落定（agentSettled）后清空：那时它们才该被重新看待。
    awaitSkip: new Set(),
    //: 已经交给原生、等**原生受理**（`agentStart` 里 run-begin + accepted settle 成功）之后才回执的帧。
    //  取到帧就 ack 是另一条会真丢信的路：Pi 那一步失败时 due 已 clear，投递永久消失。
    //: 每个**投递单元**各一份通道 ownership：`channel_owner` 是按 `box=unit` 记的，
    //  只给个人箱注册 ⇒ 岗位单元（人承岗、unit 是岗位）会在 `record_payload` 处被
    //  `unauthorized_attempt/owner_unregistered` 拒掉（真打实撞）。
    ownerTokens: new Map(),
    /** triggerTurn 请求数（仅诊断/夹具，不是 Pi run 或 Core admission 证据）。 */
    turnRequests: 0,
    /** The module owns the current Pi busy/idle projection from native lifecycle events. */
    busy: false,
    /** 原生回合关门标识与本端在跑的尝试：模型增量靠这两件关联与节流（D2）。 */
    closedTurn: "",
    activeAttempt: "",
    modelProgress: {attemptId: "", turn: "", lastMs: 0},
    /** A host-settled edge must interrupt the post-wake wait when busy mail is pending. */
    wakeWaiter: null,
  };

  async function persistReceipt(batch, ctx, done = false) {
    if (!host.saveReceipt) return;
    const owner = state.ownerTokens.get(batch.unit);
    if (!owner) return;
    const {unit, token, anchorRef, selectedRefs, attemptKey, dueKey, batchSha256, sessionFile, associationId, harnessRef, end} = batch;
    try {
      await host.saveReceipt({unit, token, anchorRef, selectedRefs, attemptKey, dueKey, batchSha256, sessionFile,
        associationId, harnessRef, actor: owner.actor, bindingId: owner.binding_id,
        generation: owner.binding_gen, instance: owner.instance,
        ...(end ? {end: {type: "agent_settled", aborted: end.aborted}} : {}), done}, ctx);
    } catch (e) {
      diagFailure(`session could not save the observed Office receipt: ${e.message}`, ctx);
    }
  }

  async function restoreReceipts(ctx) {
    if (state.restoredReceipts || !host.receiptEntries) return;
    const entries = await host.receiptEntries(ctx);
    if (!Array.isArray(entries)) return;
    const latest = new Map();
    for (const e of entries) if (e.type === "custom" && e.customType === "office.receipt" && e.data?.token) latest.set(e.data.token, e.data);
    const personal = state.ownerTokens.get(box);
    let retry = false;
    for (const saved of latest.values()) {
      if (saved.done || saved.sessionFile !== sessionFileFor(ctx) || !saved.harnessRef || !saved.unit || !saved.token) continue;
      if (saved.actor !== personal.actor || saved.bindingId !== personal.binding_id
          || saved.generation !== personal.binding_gen || saved.instance !== personal.instance) continue;
      if (state.runningBatches.some(batch => batch.token === saved.token)) continue;
      const entry = entries.findLast(e => e.type === "custom_message" && typeof e.content === "string"
        && matchesMessage(e, {...saved, text: e.content}));
      const text = host.receiptText ? await host.receiptText(saved, ctx) : entry?.content;
      if (typeof text !== "string" || sha256Text(text) !== saved.batchSha256) continue;
      if (!await registerOwner(ctx, saved.unit)) {
        const prior = await client.roundState(saved.unit, saved.token);
        if (prior?.reason_code !== "unit_not_carried") retry = true;
        continue;
      }
      const end = saved.end?.type === "agent_settled" && typeof saved.end.aborted === "boolean"
        ? {type: "agent_settled", aborted: saved.end.aborted} : undefined;
      state.runningBatches.push({...saved, text, ownerToken: state.ownerTokens.get(saved.unit).owner_token,
        end, accepted: false, restored: true});
    }
    state.restoredReceipts = !retry;
  }

  function requestPendingWake() {
    state.wakeWaiter?.();
  }

  let receiptFlush;

  const hostUnavailable = () => state.busy || host.canWake?.() === false;

  function waitForNextTick(ms) {
    let resume;
    const wake = new Promise((resolve) => { resume = state.wakeWaiter = resolve; });
    return Promise.race([sleep(ms, state.stopController.signal), wake]).finally(() => {
      if (state.wakeWaiter === resume) state.wakeWaiter = null;
    });
  }

  /** 处理一次 wake：问 Core 拿批次 → 请求 Pi custom-message。
   *  两条路径共用：① 新帧（tick 里）；② 忙时欠下的 pending（空闲后补做）。 */
  const sha256Text = (text) => createHash("sha256").update(text, "utf8").digest("hex");
  const hasRecorded = (result) => result &&
    (result.recorded === true || (typeof result.recorded === "number" && result.recorded > 0));
  // `created` 的形状与"个人箱先问"的顺序都只有一份，在 `office_daemon.mjs`（与 OC2 插件共用）；
  // 这一层不各写一遍，否则同一个 Core 契约要改两处。
  const acceptedSettle = (result) => result &&
    (result.state === "accepted" || result.result === "accepted" || result.accepted === true);
  const sameStrings = (left, right) => Array.isArray(left) && Array.isArray(right) &&
    left.length === right.length && left.every((value, i) => value === right[i]);

  async function settleUnsent(unit, token, detail, ctx) {
    try {
      const result = await client.settle(unit, token, "rejected", undefined);
      if (result && (result.state === "rejected" || result.result === "rejected")) {
        diag(`batch rejected before host dispatch: ${detail}`, ctx);
        return true;
      }
      diagFailure(`pre-send rejection was not confirmed: ${detail}`, ctx);
    } catch (e) {
      diagFailure(`pre-send rejection failed (${detail}): ${String((e && e.message) || e)}`, ctx);
    }
    return false;
  }

  async function handleWake(ctx, frame, appendAtBoundary) {
    const {unit, text, due_key: dueKey, refs: frameRefs} = frame;
    const owner = state.ownerTokens.get(unit);
    if (state.inflightRound || !owner) return;
    let batch;
    try {
      batch = await client.begin(unit, frame.kind, frame);
    } catch (e) {
      diagFailure(`round.begin outcome unknown: ${e.message}`, ctx);
      return cfg.backoffBaseMs;
    }
    if (!hasCreatedFact(batch?.created) || batch.refused) {
      diagFailure(`round.begin refused or did not confirm the batch for ${unit}`, ctx);
      return cfg.backoffBaseMs;
    }
    if (!isCreated(batch.created)) return;
    const token = batch.token;
    if (typeof token !== "string" || !token) {
      diagFailure("round.begin created a batch without a token; retain unknown", ctx);
      return cfg.backoffMaxMs;
    }
    if (state.stopped) { await settleUnsent(unit, token, "host stopped before payload", ctx); return 0; }
    const deliver = batch.deliver;
    const carry = batch.carry;
    const selectedRefs = Array.isArray(deliver) && Array.isArray(carry) ? [...deliver, ...carry] : [];
    const contentHashes = batch.content_sha256;
    const hashesComplete = contentHashes && typeof contentHashes === "object"
      && Object.keys(contentHashes).length === selectedRefs.length
      && selectedRefs.every((ref) => typeof ref === "string" && /^[a-f0-9]{64}$/i.test(contentHashes[ref] || ""));
    const refsUnique = new Set(selectedRefs).size === selectedRefs.length;
    const refsSameSource = frameRefs.length === selectedRefs.length
      && new Set(frameRefs).size === frameRefs.length && frameRefs.every(ref => selectedRefs.includes(ref));
    const sessionFile = sessionFileFor(ctx);
    let instanceId = "";
    try { instanceId = typeof host.instanceId === "function" ? String(host.instanceId(ctx) || "") : ""; }
    catch { instanceId = ""; }
    const {binding_id: bindingId, binding_gen: bindingGen} = owner;
    // ★ W439：这条硬门的原句只说"投影不全"，**不说是哪一项** —— 真机上就是这么盲了半小时。
    //   同一个判断改成逐项点名（行为不变，只是让失败可读）。
    const gaps = [];
    if (!Array.isArray(deliver) || !deliver.length || typeof deliver[0] !== "string" || !deliver[0]) gaps.push("deliver");
    if (!Array.isArray(carry)) gaps.push("carry");
    if (!selectedRefs.length) gaps.push("refs_empty");
    if (!refsUnique) gaps.push("refs_dup");
    if (!refsSameSource) gaps.push("frame_refs_vs_frozen");
    if (!hashesComplete) gaps.push("content_sha256");
    if (typeof text !== "string" || !text.length) gaps.push("text");
    if (!instanceId) gaps.push("instance_id");
    if (!sessionFile) gaps.push("session_file");
    if (gaps.length) {
      await settleUnsent(unit, token, `round.begin payload/identity projection incomplete: ${gaps.join(",")}`, ctx);
      return cfg.backoffMaxMs;
    }
    if (typeof client.recordPayload !== "function" || typeof host.triggerTurn !== "function") {
      await settleUnsent(unit, token, "payload recorder or Pi custom-message sender missing", ctx);
      return cfg.backoffMaxMs;
    }

    const anchorRef = deliver[0];
    const requestKey = `pi-token-${token}`;
    // Approved attempt-key order: Core binding id/gen, Pi's session instance_id, then request key.
    const attemptKey = `${bindingId}|${bindingGen}|${instanceId}|${requestKey}`;
    const batchSha256 = sha256Text(text);
    let payload;
    try {
      payload = await client.recordPayload(unit, token, anchorRef, attemptKey, text);
    } catch (e) {
      diagFailure(`round.record_payload failed; no message sent: ${String(e?.message || e)}`, ctx);
      await settleUnsent(unit, token, "round.record_payload failed", ctx);
      return cfg.backoffMaxMs;
    }
    if (!payload || payload.recorded !== true || payload.text_available !== true
        || payload.text_matches !== true || payload.conflict !== false) {
      diagFailure("round.record_payload did not pass the hard pre-send gate; no message sent", ctx);
      await settleUnsent(unit, token, "payload capture gate refused", ctx);
      return cfg.backoffMaxMs;
    }
    if (state.stopped) { await settleUnsent(unit, token, "host stopped before dispatch", ctx); return 0; }
    if (!appendAtBoundary && !host.acceptWhileBusy && hostUnavailable()) {
      await settleUnsent(unit, token, "the host cannot start a round now", ctx);
      return 0;
    }

    const associationId = randomUUID();
    const permission = await client.authorizeSend(unit, token, attemptKey, associationId,
      {ownerToken:owner.owner_token});
    if (permission?.allowed !== true) {
      // Even when the grant reply is lost, this process has never called the native host.
      if (permission?.unknown) await settleUnsent(unit, token, "send grant response lost before dispatch", ctx);
      return cfg.backoffBaseMs;
    }
    if (state.stopped || (!appendAtBoundary && !host.acceptWhileBusy && hostUnavailable())) {
      await settleUnsent(unit, token, "host stopped or became busy before dispatch", ctx);
      return 0;
    }
    const messageDetails = {
      officeWakeId: associationId, officeRoundToken: token, officeAnchorRef: anchorRef,
      officeAttemptKey: attemptKey, officeSelectedRefs: selectedRefs, officeBatchSha256: batchSha256,
    };
    const current = state.inflightRound = {
      unit, token, anchorRef, selectedRefs, attemptKey, dueKey,
      text, batchSha256, sessionFile, associationId, ownerToken: owner.owner_token,
      beforeStartSeen: false, admissionRecorded: false,
      accepted: false, sawStart: false, sequenceInvalidated: false,
      toolBoundary: Boolean(appendAtBoundary),
    };
    try {
      if (appendAtBoundary) {
        appendAtBoundary({type: "custom_message", customType: "office-wake", content: text, display: true, details: messageDetails});
        return 0;
      }
      await host.triggerTurn(text, messageDetails);
      state.turnRequests += 1;
    } catch (e) {
      if (state.inflightRound !== current) return 0;
      if (e?.notSent) { state.inflightRound = null; await settleUnsent(unit, token, "host stopped before dispatch", ctx); return 0; }
      await retainUnknown(current, `native submission outcome unknown: ${String(e?.message || e)}`, ctx);
      return cfg.backoffMaxMs;
    }
    diag("exact batch text submitted to Pi; awaiting matching host event", ctx);
    return 0;
  }

  /**
   * 一次处理：问受控通知入口要**本单元**的帧（单元由服务端自己算，不由宿主自报），逐帧判定，
   * 然后**只有真的受理/起轮之后才回执**。
   *
   * ★ W439两条会真丢信的时序规则，就是这段的全部理由：
   *   ① 拒帧 **绝不** ack：`wake.ack` 就是结清到期项的那一步；取到帧、发现不该发、却又 ack，
   *      等于让这封信既不在队列里、也不在任何人的轮里，只剩一条日志。拒帧 ⇒ 留痕 + 不 ack，
   *      行仍是 `armed`，daemon 下一拍重发。
   *   ② ack 在**原生受理之后**（`batch_handled` 内部已等到 `agent_start`），不是取到之后。
   *      否则 Pi 那一步失败时 due 已 clear，这次投递永久消失。
   */
  async function tick(ctx) {
    if (state.stopped) return 0;
    let got, failure;
    state.awaitingFrames = true;
    try {
      got = await client.awaitFrames({waitMs: cfg.waitMs, skip: [...state.awaitSkip]});
    } catch (e) {
      failure = String(e?.message || e);
    } finally {
      state.awaitingFrames = false;
    }
    if (state.stopped) return 0;
    if (!got || got.ok === false) {
      diagFailure(failure ? `wake wait failed: ${failure}` : `wake wait refused/unknown: ${got?.code || "no_result"}`, ctx);
      const wait = Math.min(cfg.backoffMaxMs, cfg.backoffBaseMs * 2 ** Math.min(state.awaitFailures, 6));
      state.awaitFailures += 1;
      return wait;
    }
    state.awaitFailures = 0;
    const frames = Array.isArray(got.frames) ? got.frames : [];
    // 到点没有帧是正常的一次等待（`round.await` 本身就阻塞），立刻再挂一次即可，不空转。
    if (!frames.length) return 0;
    let backoff = 0;
    for (const frame of frames) {
      if (state.stopped || state.inflightRound) break;
      const dueKey = (frame && frame.due_key) || "";
      if (host.toolBoundary && frame?.kind === "batch_round" && hostUnavailable()) {
        state.pending = frame;
        return 0;
      }
      let verdict = "failed";
      try {
        verdict = await onWakeFrame(frame, ctx);
      } catch (e) {
        diagFailure(`frame handling failed: ${String(e?.message || e)} due_key=${dueKey}`, ctx);
      }
      if (typeof verdict === "number" && verdict > 0) { backoff = Math.max(backoff, verdict); continue; }
      if (verdict !== "batch_handled" && verdict !== "turn_requested") {
        if (verdict === "work_check_unavailable") return cfg.backoffBaseMs;
        const skipped = verdict === "obsolete_work" ? frame.attempt_key : dueKey;
        if (skipped) state.awaitSkip.add(skipped);
        diag(`frame not acked (${verdict}) due_key=${dueKey}`, ctx);
      }
    }
    return backoff;
  }

  /** Pi 的原生 pre-start 事件（形状严格；不匹配就什么都不记）。 */
  async function beforeAgentStart(event, ctx) {
    const message = event?.message, details = message?.details;
    if (event?.messageType === "custom" && message?.customType === "office-wake"
        && details?.officeRoundToken && details.officeRoundToken !== state.inflightRound?.token) {
      if (state.runningBatches.some(batch => batch.token === details.officeRoundToken)) return false;
      const prior = await client.roundState(undefined, details.officeRoundToken);
      const owner = state.ownerTokens.get(box);
      const payload = prior?.payloads?.find(p => p.ref === details.officeAnchorRef
        && p.attempt_key === details.officeAttemptKey && p.text === message.content
        && p.payload_sha256 === details.officeBatchSha256 && p.binding_id === owner?.binding_id
        && p.binding_gen === owner?.binding_gen && p.owner_instance === owner?.instance);
      if (prior?.token !== details.officeRoundToken || prior.state !== "unknown" || prior.send_id !== details.officeWakeId
          || prior.binding_id !== owner?.binding_id || prior.binding_gen !== owner?.binding_gen
          || !payload || sha256Text(message.content) !== details.officeBatchSha256
          || !await registerOwner(ctx, prior.box)) return false;
      const cur = {unit: prior.box, token: prior.token, anchorRef: details.officeAnchorRef,
        selectedRefs: details.officeSelectedRefs, attemptKey: details.officeAttemptKey,
        text: message.content, batchSha256: details.officeBatchSha256,
        sessionFile: sessionFileFor(ctx), associationId: details.officeWakeId,
        ownerToken: state.ownerTokens.get(prior.box).owner_token, lateReceipt: true};
      if (!cur.sessionFile || !matchesMessage(message, cur) || !await admitExact({text:cur.text, event, viaPiEvent:true}, ctx, cur)) return false;
      state.runningBatches.push(cur);
      return true;
    }
    return admitExact({text: (event && event.message && event.message.content) || "", event, viaPiEvent: true}, ctx);
  }

  async function lateNativeStart(nativeId, ctx) {
    if (typeof nativeId !== "string" || !nativeId) return false;
    const prior = await client.roundState(undefined, undefined, nativeId);
    const owner = state.ownerTokens.get(box);
    if (prior?.state !== "unknown" || !prior.token || prior.token === state.inflightRound?.token
        || prior.binding_id !== owner?.binding_id || prior.binding_gen !== owner?.binding_gen
        || state.runningBatches.some(batch => batch.token === prior.token)) return false;
    const payload = prior.payloads?.find(p => p.binding_id === owner.binding_id && p.binding_gen === owner.binding_gen
      && p.owner_instance === owner.instance && typeof p.text === "string" && sha256Text(p.text) === p.payload_sha256);
    if (!payload || !await registerOwner(ctx, prior.box)) return false;
    const cur = {unit:prior.box, token:prior.token, anchorRef:payload.ref, attemptKey:payload.attempt_key,
      text:payload.text, batchSha256:payload.payload_sha256, sessionFile:sessionFileFor(ctx),
      associationId:prior.send_id, ownerToken:state.ownerTokens.get(prior.box).owner_token, lateReceipt:true};
    if (!cur.sessionFile || !await admitExact({text:cur.text, nativeMessageId:nativeId, beforeEnqueue:false}, ctx, cur)) return false;
    state.runningBatches.push(cur);
    await agentStart(ctx);
    return true;
  }

  /**
   * 没有 pre-start 原生事件的宿主自报的那一次提交（Codex：`turn/start`/`turn/steer` 的答复之后）。
   * 判据只有"正文逐字相同 + 同一会话"。
   */
  async function submittedExact(text, ctx, nativeMessageId) {
    return admitExact({text, nativeMessageId}, ctx);
  }

  async function registerOwner(ctx, unit = box) {
    if (state.ownerTokens.has(unit)) return true;
    try {
      const prior = host.conditionalOwner ? await client.ownerShow(unit) : null;
      if (state.stopped) return false;
      if (host.conditionalOwner && (!prior || prior.ok === false || (prior.registered !== false && !Number.isInteger(prior.owner_gen))))
        throw new Error("round.owner.show did not confirm the current generation");
      const owner = await client.ownerRegister(unit, host.conditionalOwner ? (prior.registered ? prior.owner_gen : 0) : undefined);
      const personal = state.ownerTokens.get(box);
      if (owner?.actor !== box || owner.box !== unit || !owner.owner_token || !owner.binding_id
          || !Number.isInteger(owner.binding_gen) || owner.binding_gen <= 0 || !owner.instance
          || (personal && (owner.binding_id !== personal.binding_id || owner.binding_gen !== personal.binding_gen
                           || owner.instance !== personal.instance))) {
        throw new Error("round.owner.register did not confirm this session's identity");
      }
      state.ownerTokens.set(unit, owner);
      diag(`Core round owner registered for unit=${unit}`, ctx);
      return true;
    } catch (e) {
      diagFailure(`round owner registration failed: ${String(e?.message || e)}`, ctx);
      return false;
    }
  }

  /** 持续等待的主循环（**只在 start() 里跑**，stop() 立刻退）。 */
  async function run(ctx) {
    let ownerRetryMs = cfg.backoffBaseMs;
    try {
      while (!state.stopped) {
        if (!await registerOwner(ctx)) {
          await sleep(ownerRetryMs, state.stopController.signal);
          ownerRetryMs = Math.min(cfg.backoffMaxMs, ownerRetryMs * 2);
          continue;
        }
        ownerRetryMs = cfg.backoffBaseMs;
        if (state.stopped) break;
        client.setConnBox?.(box);
        let next = cfg.backoffBaseMs;
        try {
          await restoreReceipts(ctx);
          await flushNativeReceipts(ctx);
          next = cfg.backoffMaxMs;
          const blocked = state.inflightRound || state.pending
            || (!host.toolBoundary && !host.acceptWhileBusy && hostUnavailable());
          next = blocked ? cfg.waitMs : await tick(ctx);
        } catch (e) {
          // 任何未预期异常都不许把循环打死（否则"关闭可退出"变成"静默死掉"）
          diagFailure(`wake loop error: ${String(e?.message || e)}`, ctx);
        }
        if (state.stopped) break;
        // ★ 等待本身是阻塞的（`round.await`）；这里只保证 `next` 不合法时不至于负值。
        const wait = next | 0;
        await waitForNextTick(wait);
      }
    } finally {
      for (const [unit, owner] of state.ownerTokens) {
        try {
          await client.ownerRevoke(unit, owner);
          diag(`Core round owner revoked for unit=${unit}`, ctx);
        } catch (e) {
          diagFailure(`round owner revoke failed for unit=${unit}: ${String(e?.message || e)}`, ctx);
        }
      }
      state.ownerTokens.clear();
      await client.close?.({final:true});
      state.running = false;
    }
  }

  function sessionFileFor(ctx) {
    try { return typeof host.sessionRef === "function" ? String(host.sessionRef(ctx) || "") : ""; }
    catch { return ""; }
  }

  async function retainUnknown(cur, reason, ctx) {
    if (!cur || cur.sequenceInvalidated) return false;
    cur.sequenceInvalidated = true;
    if (state.inflightRound === cur) {
      state.inflightRound = null;
      requestPendingWake();
    }
    diagFailure(`${reason}; retain the Office batch as unknown`, ctx);
    if (cur.accepted) return false;
    try {
      const result = await client.settle(cur.unit, cur.token, "unknown", cur.harnessRef);
      const recorded = Boolean(result && result.ok !== false && result.token === cur.token &&
        (result.state === "unknown" || result.result === "unknown"));
      if (!recorded) {
        diagFailure("round.settle(unknown) was not confirmed", ctx);
      }
      return recorded;
    } catch (e) {
      diagFailure(`round.settle(unknown) failed: ${String(e?.message || e)}`, ctx);
      return false;
    }
  }

  function matchesMessage(message, cur) {
    const details = message && message.details;
    return Boolean(cur && message?.customType === "office-wake"
      && details?.officeWakeId === cur.associationId
      && details?.officeRoundToken === cur.token
      && details?.officeAnchorRef === cur.anchorRef
      && details?.officeAttemptKey === cur.attemptKey
      && details?.officeBatchSha256 === cur.batchSha256
      && sameStrings(details?.officeSelectedRefs, cur.selectedRefs)
      && message.content === cur.text);
  }

  /**
   * 精确受理的唯一一个体：**正文逐字相同 + 同一会话**才记 admission。
   *
   * 两个入口进的是同一个体，只是"谁说自己提交了"不同：
   *  - `beforeAgentStart`：Pi 的原生 `before_agent_start`（自定义消息形状，`matchesBeforeStart` 严格校验）；
   *  - `submittedExact`：OpenCode 在原生入队前报出同一会话和正文，
   *    只认正文逐字相同 + 同一会话 —— 不伪造 Pi 的事件形状，也不多一个判据。
   */
  async function admitExact({text, event = null, viaPiEvent = false, nativeMessageId, beforeEnqueue = Boolean(nativeMessageId)}, ctx, cur = state.inflightRound) {
    if (!cur) return false;
    const matches = viaPiEvent
      ? event?.messageType === "custom" && matchesMessage(event.message, cur)
      : text === cur.text;
    const sessionFile = sessionFileFor(ctx);
    const sameSession = Boolean(sessionFile && sessionFile === cur.sessionFile);
    if (!sameSession || !matches) {
      await retainUnknown(cur, sameSession ? "a nonmatching before-start superseded the dispatched batch"
        : "host session differs from the dispatched batch", ctx);
      return false;
    }
    if (cur.beforeStartSeen) return false;

    cur.beforeStartSeen = true;
    const fallback = {
      session_file: cur.sessionFile,
      submitted_at: new Date().toISOString(),
      batch_sha256: cur.batchSha256,
    };
    // Pi exposes no native run id at before_agent_start; this exact content_time fallback is
    // explicitly marked as derived and is never described as a Pi run/session id.
    const harnessRef = nativeMessageId || `content_time:${fallback.session_file}|${fallback.submitted_at}|${fallback.batch_sha256}`;
    cur.harnessRef = harnessRef;
    try {
      const admission = await client.recordAdmission(cur.unit, cur.token, harnessRef, fallback, beforeEnqueue, cur.associationId);
      if (admission?.rejected || admission?.reason === "stale_frame") {
        if (state.inflightRound === cur) state.inflightRound = null;
        return false;
      }
      if (!admission || admission.refused || !hasRecorded(admission)) {
        throw new Error("round.record_admission returned no usable receipt");
      }
      cur.admissionRecorded = true;
      diag("exact batch matched the host submission; admission recorded; awaiting the native start", ctx);
      return true;
    } catch (e) {
      const detail = String(e?.message || e);
      if (beforeEnqueue) {
        state.inflightRound = null;
        await settleUnsent(cur.unit, cur.token, `native pre-enqueue admission failed: ${detail}`, ctx);
        return false;
      }
      diagFailure(`matched batch admission awaits native start: ${detail}`, ctx);
      return true;
    }
  }


  async function agentStart(ctx, info) {
    state.busy = true;
    return recordNativeStart(ctx);
  }

  async function recordNativeStart(ctx, modelStarting = true) {
    const cur = state.runningBatches.find(batch => batch.lateReceipt && batch.beforeStartSeen && !batch.sawStart) || state.inflightRound;
    if (!cur || cur.sequenceInvalidated) return false;
    if (!cur.beforeStartSeen) {
      await retainUnknown(cur, "Pi agent_start arrived without a matched preceding before_agent_start", ctx);
      return false;
    }
    if (cur.sawStart) return false;
    const sessionFile = sessionFileFor(ctx);
    if (!sessionFile || sessionFile !== cur.sessionFile) {
      await retainUnknown(cur, "Pi session changed before the matched agent_start", ctx);
      return false;
    }
    // By Pi 1.5's single-run ordering, this is the start immediately following the exact
    // before_agent_start. A later before-start would already have invalidated this association.
    cur.sawStart = true;
    if (modelStarting) state.activeAttempt = cur.attemptKey;
    if (!state.runningBatches.includes(cur)) state.runningBatches.push(cur);
    await persistReceipt(cur, ctx);
    const received = await recordNativeReceipt(cur, ctx, modelStarting);
    if (state.inflightRound === cur) state.inflightRound = null;
    state.awaitSkip.clear();
    requestPendingWake();
    return received;
  }

  async function recordNativeReceipt(cur, ctx, modelStarting = false) {
    try {
      if ((!cur.restored || host.restoreAdmission) && !cur.admissionRecorded && !hasRecorded(await client.recordAdmission(cur.unit, cur.token, cur.harnessRef)))
        throw new Error("round.record_admission did not confirm the native start");
      cur.admissionRecorded = true;
      const run = await client.runBegin(cur.unit, cur.token, cur.ownerToken, cur.harnessRef, cur.restored || cur.lateReceipt ? cur.text : undefined);
      if (!hasRecorded(run)) throw new Error("round.run_begin did not record the matched host run");
      if (!cur.restored && !cur.lateReceipt && !acceptedSettle(await client.settle(cur.unit, cur.token, "accepted", cur.harnessRef)))
        throw new Error("round.settle did not confirm accepted");
      cur.coreSettled = run.run_state === "settled";
      cur.accepted = true;
      if (modelStarting) await reportAttemptStarted(cur.attemptKey, ctx);
      diag("matched native start recorded run-begin and accepted settle", ctx);
      if (!cur.lateReceipt) await ackFrame(cur, ctx);
      return true;
    } catch (e) {
      diagFailure(`native receipt could not be recorded: ${String(e?.message || e)}`, ctx);
      return false;
    }
  }


  async function ackFrame({unit, dueKey}, ctx) {
    if (!dueKey) return;
    try {
      const ack = await client.wakeAck(unit, dueKey);
      if (ack?.ok === false || (ack?.result ?? ack)?.taken === false) {
        diagFailure(`wake.ack did not take the frame: ${ack.code || "pending"} due_key=${dueKey}`, ctx);
      }
    } catch (e) {
      diagFailure(`wake.ack outcome unknown: ${String((e && e.message) || e)} due_key=${dueKey}`, ctx);
    }
  }

  /**
   * 发一条「尝试类」事实（`round.started` / `model_progress`）：字段表走同一处门，缺失/退休就在
   * 本地给出理由，一个字节不发。
   */
  async function sendAttemptEvent(kind, attemptId, ts) {
    const shaped = hostEventFrame(kind, {attemptId, ts});
    if (shaped.ok === false) return shaped;
    return await client.hostEvent(kind, shaped.ts, shaped.frame);
  }

  /**
   * 报一次**原生用量**（`usage.record`）。`entry_id` 是宿主持久消息的 id，`act_key` 也由它组成
   * ⇒ 重发是同一条动作（回读原记录），同一个原生请求不会被算两次——**本端不留用量缓存**。
   * 没有真实用量就不调这里（调用方判）：记一条 0 不是"没有用量"，是伪造。
   */
  async function reportUsage(entries, actKey) {
    try {
      const out = await client.usageRecord(entries, actKey);
      if (out && out.ok === false) diagFailure(`usage.record refused: ${out.code || "?"}`);
      return out;
    } catch (e) {
      diagFailure(`usage.record outcome unknown: ${String((e && e.message) || e)}`);
      return { ok: false, unknown: true };
    }
  }

  /** 报「这一次业务尝试真的起轮了」。 */
  async function reportAttemptStarted(attemptKey, ctx) {
    try {
      const ev = await sendAttemptEvent("round.started", attemptKey, Math.floor(Date.now() / 1000));
      if (ev && ev.ok === false) diagFailure(`round.started refused: ${ev.code || "?"}`, ctx);
      else diag("native attempt started (round.started)", ctx);
    } catch (e) {
      diagFailure(`round.started outcome unknown: ${String((e && e.message) || e)}`, ctx);
    }
  }

  /**
   * 宿主回合关门（Pi `turn_end` / Codex `turn/completed`）：这个回合的增量到此为止。
   *
   * 关闭后的迟到增量不能用作额度恢复证据。
   */
  function turnClosed(turnId) {
    state.closedTurn = String(turnId || "");
    // `turn_end` 携带确切的原生尝试标识。
    return { ok: true, closed_turn: state.closedTurn, attempt_id: state.activeAttempt };
  }

  /**
   * 模型产出的原生事实（D2 心跳）：`text_delta` / `thinking_delta` 一次一条。
   *
   * ① 只在这两条上发：工具块（`toolcall_*`）走 `tool_start` 的既有通道，不算模型产出。
   * ② 关联不是"增量 id"（取证 `5e05476a`：增量只有 `{type, contentIndex, delta}`）：用本端手里
   *    **这次尝试**的 `attempt_id`，与 `round.started` 同键。没在跑的尝试就不发（Core 无处记它）。
   * ③ 节流是**传输策略**，不是静默判据：每回合第一条必发，之后至多 `modelProgressMinMs` 一条。
   * ④ 已 `turn_end` 的回合不再报（见 `turnClosed`）。
   */
  function modelProgress(ev = {}) {
    const attemptId = state.activeAttempt;
    if (!attemptId) return { ok: false, code: "no_active_attempt",
      message: "本端没有在跑的原生尝试：这条增量不属于任何尝试，不发" };
    const turn = typeof ev.turnId === "string" ? ev.turnId : "";
    if (turn && state.closedTurn === turn) {
      return { ok: true, skipped: "turn_closed", attempt_id: attemptId };
    }
    const nowMs = Number.isFinite(ev.nowMs) ? ev.nowMs : Date.now();
    const slot = state.modelProgress;
    const firstOfTurn = slot.attemptId !== attemptId || slot.turn !== turn;
    if (!firstOfTurn && nowMs - slot.lastMs < cfg.modelProgressMinMs) {
      return { ok: true, throttled: true, attempt_id: attemptId };
    }
    slot.attemptId = attemptId;
    slot.turn = turn;
    slot.lastMs = nowMs;
    return sendAttemptEvent("model_progress", attemptId, Math.floor(nowMs / 1000));
  }

  async function insertAtToolBoundary(ctx) {
    const pending = state.pending;
    if (!pending) return;
    let entry;
    try {
      const current = await client.awaitFrames({unit: pending.unit, waitMs: 0});
      const frame = current?.frames?.find(f => f.due_key === pending.due_key);
      if (frame) await onWakeFrame(frame, ctx, (draft) => { entry = draft; });
      return entry;
    } finally {
      state.pending = null;
    }
  }

  async function confirmToolBoundary(ctx, modelStarting = true) {
    const cur = state.inflightRound;
    if (!cur?.toolBoundary || cur.beforeStartSeen || cur.sequenceInvalidated) return;
    if (sessionFileFor(ctx) !== cur.sessionFile) return retainUnknown(cur, "Pi session changed at tool boundary", ctx);
    const entries = ctx?.sessionManager?.getEntries?.();
    if (!Array.isArray(entries)) return retainUnknown(cur, "Pi tool-boundary transcript unavailable", ctx);
    const entry = entries.findLast((item) => item.type === "custom_message" && matchesMessage(item, cur));
    if (!entry) {
      if (!modelStarting && await settleUnsent(cur.unit, cur.token, "Pi did not append the tool-boundary entry", ctx)) {
        state.inflightRound = null;
      }
      return;
    }
    if (await admitExact({text: entry.content, nativeMessageId: entry.id, beforeEnqueue: false}, ctx)) {
      await recordNativeStart(ctx, false);
    }
  }

  async function agentSettled(event, ctx) {
    await confirmToolBoundary(ctx, false);
    if (typeof event?.aborted === "boolean" && state.inflightRound
        && !state.runningBatches.some(batch => batch.lateReceipt && batch.sawStart && !batch.end))
      await retainUnknown(state.inflightRound, "native terminal without matching start", ctx);
    state.pending = null;
    state.busy = false;
    state.activeAttempt = "";
    state.modelProgress = {attemptId: "", turn: "", lastMs: 0};
    state.awaitSkip.clear();
    requestPendingWake();
    if (typeof event?.aborted !== "boolean") {
      diagFailure("agent_settled lacks a native aborted fact", ctx);
      return false;
    }
    for (const batch of state.runningBatches)
      if (!batch.end && !batch.restored && (!batch.lateReceipt || batch.sawStart) && batch.sessionFile === sessionFileFor(ctx)) {
        batch.end = {...event, type: "agent_settled"};
        await persistReceipt(batch, ctx);
      }
    return flushNativeReceipts(ctx);
  }

  function flushNativeReceipts(ctx) {
    const pending = state.runningBatches.filter(batch => (!batch.lateReceipt || batch.sawStart) && (!batch.accepted || batch.end) && batch !== state.inflightRound
      && batch.sessionFile === sessionFileFor(ctx));
    if (!pending.length) return false;
    return receiptFlush ??= (async () => {
      let recorded = false;
      for (const batch of pending) {
        try {
          if (!batch.accepted && !await recordNativeReceipt(batch, ctx, !batch.end && state.activeAttempt === batch.attemptKey)) continue;
          if (!batch.end && !batch.restored) continue;
          if (batch.end && !batch.coreSettled
              && !hasRecorded(await client.consume(batch.unit, batch.end, batch.ownerToken, batch.token))) {
            diagFailure("round.consume did not record the matched agent_settled", ctx);
            continue;
          }
          await persistReceipt(batch, ctx, true);
          state.runningBatches.splice(state.runningBatches.indexOf(batch), 1);
          recorded = true;
        } catch (e) {
          diagFailure(`round.consume failed: ${e.message}`, ctx);
        }
      }
      return recorded;
    })().finally(() => { receiptFlush = null; });
  }

  async function lifecycleBoundary(name, ctx, action) {
    try {
      return await action();
    } catch (e) {
      const detail = `${name} handler failed: ${String(e?.message || e)}`;
      if (state.inflightRound) {
        try { await retainUnknown(state.inflightRound, detail, ctx); }
        catch (retainError) {
          diagFailure(`${detail}; unknown retention also failed: ${String((retainError && retainError.message) || retainError)}`, ctx);
        }
      } else {
        diagFailure(detail, ctx);
      }
      return false;
    }
  }

  // Batch frames carry Core's frozen text.
  async function onWakeFrame(frame, ctx, appendAtBoundary = null) {
    const due = (frame && typeof frame.due_key === "string") ? frame.due_key : "(no due_key)";
    const reject = (reason) => {
      diagFailure(`frame rejected: ${reason} due_key=${due}`, ctx);
      return "bad_frame";
    };
    if (!frame || typeof frame.kind !== "string" || !frame.kind) return reject("no kind");
    if (typeof frame.due_key !== "string" || !frame.due_key) return reject("empty due_key");
    if (typeof frame.text !== "string" || !frame.text) return reject("empty frame text");
    if (frame.kind === "batch_round" && !(Array.isArray(frame.refs) && frame.refs.length)) return reject("empty refs");
    const waitForIdle = !appendAtBoundary && (frame.kind !== "batch_round" || !host.acceptWhileBusy);
    if (waitForIdle && hostUnavailable()) return "host_busy";
    const unit = typeof frame.unit === "string" && frame.unit ? frame.unit : box;
    if (typeof host.beforeBegin === "function" && frame.kind === "batch_round") {
      await host.beforeBegin(ctx, frame);
      if (state.stopped || !state.running || state.inflightRound) return "not_running";
      if (waitForIdle && hostUnavailable()) return "host_busy";
    }
    if (frame.kind === "batch_round") {
      if (state.stopped || !state.running) return "not_running";
      if (state.inflightRound) return "already_inflight";
      // ★ 先给**这一帧的单元**登记通道 ownership：`channel_owner` 按 `box=unit` 记，
      //   岗位单元没登记就会被 `record_payload` 以 `owner_unregistered` 拒掉（独核真例）。
      if (!(await registerOwner(ctx, unit))) {
        diagFailure(`no channel ownership for unit=${unit}; frame left unacked`, ctx);
        return "no_owner";
      }
      return await handleWake(ctx, {...frame, unit}, appendAtBoundary) || "batch_handled";
    }
    return "unknown_kind:" + frame.kind;
  }

  return {
    /** 起（幂等：已在跑就什么都不做）。返回一个可 await 的 promise（调用方**不该** await）。 */
    start(ctx) {
      if (state.running || state.stopped) return null;
      state.running = true;
      return run(ctx);
    },
    /** 宿主在"刚把这一份正文原样交出去"时调用（Codex 这类没有 pre-start 事件的宿主用）。 */
    submittedExact: (text, ctx, nativeMessageId) => lifecycleBoundary(
      "before_agent_start_submitted", ctx, () => submittedExact(text, ctx, nativeMessageId)),
    beforeAgentStart: (event, ctx) => lifecycleBoundary(
      "before_agent_start", ctx, () => beforeAgentStart(event, ctx)),
    lateNativeStart: (nativeId, ctx) => lifecycleBoundary(
      "late_native_start", ctx, () => lateNativeStart(nativeId, ctx)),
    lateNativeRunning: () => state.runningBatches.some(batch => batch.lateReceipt && batch.sawStart && !batch.end),
    agentStart: (ctx, info) => lifecycleBoundary(
      "agent_start", ctx, () => agentStart(ctx, info)),
    agentSettled: (event, ctx) => lifecycleBoundary(
      "agent_settled", ctx, () => agentSettled(event, ctx)),
    insertAtToolBoundary,
    confirmToolBoundary,
    /** 宿主回合结束（Pi `turn_end`）：这一回合的增量到此为止。 */
    turnClosed: (turnId) => turnClosed(turnId),
    /** 模型增量（`text_delta`/`thinking_delta`）：关联本端在跑的尝试，节流后上行。 */
    modelProgress: (ev) => modelProgress(ev),
    /** 原生用量（`usage.record`）：只发宿主从持久消息里读到的那一份，不推算、不留缓存。 */
    reportUsage: (entries, actKey) => reportUsage(entries, actKey),
    /** 受控通知入口（`round.await`）取回的一帧 —— 形状由投递层定，宿主不自造。 */
    onWakeFrame: (frame, ctx) => onWakeFrame(frame, ctx),
    /** 停（幂等）：中断本地等待并关掉长轮询 socket；run finally 随后用新连接撤销 owner。 */
    stop() {
      if (state.stopped) return;
      state.stopped = true;
      state.running = false;
      state.stopController.abort();
      if (state.awaitingFrames) {
        try {
          const closed = client.close?.();
          if (closed && typeof closed.catch === "function") closed.catch(() => {});
        } catch { /* close 失败不改变 unknown round 状态 */ }
      }
    },
    /** Diagnostic snapshot for host tests. */
    stats() {
      return {
        running: state.running,
        busy: state.busy,
        turnRequests: state.turnRequests,
        pendingBatch: Boolean(state.inflightRound),
      };
    },
  };
}
