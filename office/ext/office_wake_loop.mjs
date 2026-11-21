import {createHash, randomUUID} from "node:crypto";
// ★ W439 首切片：本树只保留 Pi 原生片段，`office_daemon.mjs` 不在保留集里。这里改用
//   **既有 socket 模块**之上的 Pi 侧投影（`office_pi_round.mjs`）：同一条受控 socket、一份方法表，
//   不造第二入口、不轮询。客户端仍由调用方注入（夹具可以传自己的）。
import {ready, orderUnits, isCreated, hasCreatedFact, hostEventFrame} from "./office_pi_round.mjs";

/**
 * 岗位信唤醒 → 精确 Pi custom batch message 的**唯一条生命周期**（W431/W436）。
 * 发送前必须通过 Core payload 硬门；sendMessage 本身不是宿主受理回执。
 *
 * A 2026-11-16 的六条要求，逐条对应实现：
 *
 *  ① **长期空闲后新信到达能传递选中的 batch text**
 *     会话级持续等待（`start()` 起、`stop()` 收），不是"安全点等 1.5 秒就完"。
 *     Core `round.begin` 选定并呈现一份 batch text；发送前记录 anchor payload；精确 before-start
 *     记录 admission，Pi 顺序对应的 agent_start 记录 run-begin/accepted，settled 才消费。
 *
 *  ② **忙时不抢跑**
 *     本模块从 Pi 的 `agent_start` / `agent_settled` 事实维护忙闲；忙时不起轮、不投递，
 *     只把"有货"记下来等下一次原生空闲事件。
 *
 *  ③ **关闭可退出**
 *     `stop()` 幂等；等待循环用 **await 间隔 + 可中断睡眠**，不 `setInterval` 裸奔；
 *     停止时关闭受控 socket 打断 `round.await`，然后在 `finally` 撤销 owner。
 *
 *  ④ **失败/unknown 有界退避**
 *     失败按指数退避（有上限），`units_unknown` 不当成"没岗位"（照样去问 Core）。
 *
 *  ⑤ **重复唤醒不自激、不重复投递**
 *     `wake` 到"起轮"之间有 **in-flight 闸**，且同一批 `due_units` 不重复起轮
 *     （起轮前按服务端 `due_units` 逐个调用 Core `round.begin`；`created:false` 就记一次 empty，不空转）。
 *
 *  ⑥ **只用隔离会话与夹具，不触碰生产原窗、不部署**
 *     本模块不读环境里的"当前会话"任何全局状态，所有输入都由调用方注入
 *     （`client` / `box` / `host`），所以可以在夹具里整条跑。
 *
 *  ⑦ **会话空闲时续做现有 Work**
 *     只在 Pi 原生 `agent_settled` 安全点询问 Core `work.todo`；仅当 Core 当前返回可执行/待审项时，
 *     通过同一个原生 `triggerTurn` 发通用续工提示。客户端不挑 Work、不读邮箱、不维护监测器或工作账本；
 *     Core 判为 waiting/无项/未知时不触发。
 *
 * ★ 不复制选信规则：这一层**只**决定"要不要起一轮""要不要现在去问 Core"，
 *   送哪几封永远由 Core 的 `round.begin`（`client.begin`）决定。
 */

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

/** 可中断睡眠（`stop()` 能立刻打断，不留悬空计时器）。 */
function sleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal && signal.aborted) return resolve();
    const t = setTimeout(finish, Math.max(0, ms | 0));
    function finish() {
      clearTimeout(t);
      if (signal) signal.removeEventListener("abort", finish);
      resolve();
    }
    if (signal) signal.addEventListener("abort", finish, { once: true });
  });
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
    /** 在飞的那次等待的取消器。 */
    abort: null,
    /** 主循环睡眠的关闭信号。 */
    stopController: new AbortController(),
    /** 连续 empty wake 次数（用于把间隔放长）。 */
    emptyWake: 0,
    awaitFailures: 0,
    /** 最近一次失败原因（诊断用，不参与判定）。 */
    lastError: "",
    /** ★ 忙时收到、还没处理的 wake（不丢：空闲后立刻处理，不等下一帧）。 */
    pending: null,
    /** ★ Core batch 已 begun；native wake 的 admission/consume 仍未知时保留此槽。 */
    inflightRound: null,
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
    /** 受控 Core owner token（只在本会话 wake loop 生命周期内持有）。 */
    ownerToken: null,
    /** round.owner.register 返回的 Core 权威 binding facts；不从 whoami/descriptor 猜。 */
    ownerFacts: null,
    /** 来自本 socket 的 identity.whoami，不从唤醒消息或扩展请求体推断。 */
    identity: null,
    /** triggerTurn 请求数（仅诊断/夹具，不是 Pi run 或 Core admission 证据）。 */
    turnRequests: 0,
    /** The module owns the current Pi busy/idle projection from native lifecycle events. */
    busy: false,
    /** 原生回合关门标识与本端在跑的尝试：模型增量靠这两件关联与节流（D2）。 */
    closedTurn: "",
    activeAttempt: "",
    modelProgress: {attemptId: "", turn: "", sent: 0, lastMs: 0, suppressed: 0},
    /** A host-settled edge must interrupt the post-wake wait when busy mail is pending. */
    wakeWaiter: null,
  };

  function requestPendingWake() {
    state.wakeWaiter?.();
  }

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
    state.inflightRound = {unit, token};
    return false;
  }

  async function handleWake(ctx, dueUnitsForThisWake = null, channel = "poll", carriedText = "", carriedRefs = null, dueKey = null, frame = null) {
    if (state.inflightRound) {
      diag("wake deferred: an earlier Core batch remains unresolved", ctx);
      return cfg.backoffMaxMs;
    }
    if (!state.ownerToken) {
      diagFailure("wake skipped: no Core round owner", ctx);
      return cfg.backoffMaxMs;
    }
    if (!state.ownerFacts) {
      diagFailure("wake skipped: Core owner returned no trusted binding facts", ctx);
      return cfg.backoffMaxMs;
    }
    state.emptyWake = 0;
    // ★ 按 wake 帧的 due_units 逐个调用 Core `round.begin`，个人箱优先。**这一层不选信**：
    //   送哪几封完全由 Core 决定。
    let batch = null;
    try {
      // ★ runtime 2026-11-16 定的用法：**按 wake 帧的 `due_units` 逐个 begin**。
      //   `due_units` 是"本次真正到期的单元"，服务端已经按 `carried_units` 扇出过；
      //   我再去问 `positionUnits` 拿全集就会**多问不该问的单元**（也更容易撞无谓的 unauthorized）。
      //   现行服务端保证 wake 帧里带 `due_units`；缺失说明对面不是现行服务端 ⇒ 如实报，不自造列表。
      //   客户端不再另问 positionUnits，避免复制服务端的单元扇出/选信规则。
      const list = Array.isArray(dueUnitsForThisWake) ? dueUnitsForThisWake.slice() : null;
      if (list === null) {
        diagFailure("wake frame carried no due_units: 对面不是现行服务端（不猜全集）", ctx);
        return cfg.backoffMaxMs;
      }
      if (!list.length) {
        // 空 due_units 是合法的（hold / 刚写过）：不当成"没有单元"，直接一轮 empty。
        state.emptyWake += 1;
        diag("wake with empty due_units (hold / just-written)", ctx);
        return Math.min(cfg.backoffMaxMs, cfg.backoffBaseMs * Math.pow(2, Math.min(state.emptyWake, 5)));
      }
      // ★ 逐个询问 Core；任何请求结果未知/畸形/拒绝时立即停，不能冒险继续选别的单元。
      const ordered = orderUnits(box, list);
      let last = null;
      for (const unit of ordered) {
        let got = null;
        try {
          got = await client.begin(unit, channel, frame && frame.unit === unit ? frame : null);
        } catch (e) {
          state.lastError = String((e && e.message) || e);
          state.inflightRound = {unit, token: null};
          diagFailure(`begin ${unit} outcome unknown; stop selecting and retain association: ${state.lastError}`, ctx);
          return cfg.backoffMaxMs;
        }
        if (!got || typeof got !== "object" || !hasCreatedFact(got.created) || got.refused === true) {
          const knownCreated = Boolean(got && isCreated(got.created));
          const token = knownCreated && typeof got.token === "string" && got.token ? got.token : null;
          state.inflightRound = {unit, token};
          diagFailure(`begin ${unit} returned malformed/refused facts; retain unknown`, ctx);
          if (token) await retainUnknown(state.inflightRound,
            "round.begin created a batch with malformed projection", ctx);
          return cfg.backoffMaxMs;
        }
        if (isCreated(got.created)) { batch = { ...got, unit }; break; }
        if (!last || unit === box) last = got;
      }
      if (!batch) batch = last;
    } catch (e) {
      state.lastError = String((e && e.message) || e);
      diagFailure(`wake fetch failed: ${state.lastError}`, ctx);
      return Math.min(cfg.backoffMaxMs, cfg.backoffBaseMs * Math.pow(2, state.emptyWake + 1));
    }
    if (!batch || !isCreated(batch.created)) {
      // 有 wake 但 Core 说没有可出发项：不空转（把它当一次 empty wake）
      state.emptyWake += 1;
      diag(`wake but Core created=false (${(batch && batch.reason) || "?"})`, ctx);
      // ★ Core 说没货 ⇒ 退避再问（不能立刻重问：空箱时以极高频率打服务端，实测 72 次）
      return Math.min(cfg.backoffMaxMs, cfg.backoffBaseMs * Math.pow(2, Math.min(state.emptyWake, 5)));
    }

    // ① 只把经过硬门记录的精确 batch.text 发给 Pi；sendMessage 仍不是宿主受理回执。
    //   before_agent_start 精确匹配后只记 admission；run/accepted 等同会话 agent_start。
    const unit = batch.unit || batch.box || box;
    const token = batch.token;
    if (typeof token !== "string" || !token) {
      state.inflightRound = {unit, token: null};
      diagFailure("round.begin created a batch without a token; retain unknown", ctx);
      return cfg.backoffMaxMs;
    }
    if (state.stopped) return 0;
    const deliver = batch.deliver;
    const carry = batch.carry;
    // 正文由**投递层写一次**（`delivery.round.batch_text`，用刚冻结的 refs），帧原样搬过来；
    //  宿主只携带原文。所以这里**只有帧那一份**：没有"回落到 begin 投影"，更没有按 refs 重渲染 ——
    //  那两条都会变成第二个"这批信说什么"的答案。帧没给正文 ⇒ 硬门在 onWakeFrame 处就停住。
    const text = typeof carriedText === "string" ? carriedText : "";
    const selectedRefs = Array.isArray(deliver) && Array.isArray(carry) ? [...deliver, ...carry] : [];
    const contentHashes = batch.content_sha256;
    const hashesComplete = contentHashes && typeof contentHashes === "object"
      && Object.keys(contentHashes).length === selectedRefs.length
      && selectedRefs.every((ref) => typeof ref === "string" && /^[a-f0-9]{64}$/i.test(contentHashes[ref] || ""));
    const refsUnique = new Set(selectedRefs).size === selectedRefs.length;
    // 帧带来的正文是**按帧自己的 refs** 生成的；而 `round.begin`
    //   会在同一拍重新冻结一份选择。两者必须是同一份，否则会出现"发送的是旧版本正文，受理的却是后选
    //   的对象"。只有在帧真的带了 refs 时才比（空的帧不冒充一致），比的是集合相等，不是包含。
    const frameRefs = Array.isArray(carriedRefs) ? carriedRefs.filter((r) => typeof r === "string" && r) : null;
    const refsSameSource = !frameRefs || !frameRefs.length
      || (frameRefs.length === selectedRefs.length
          && new Set(frameRefs).size === frameRefs.length
          && frameRefs.every((r) => selectedRefs.includes(r)));
    const identity = state.identity;
    const ownerFacts = state.ownerFacts;
    let sessionFile = "";
    let instanceId = "";
    try { sessionFile = typeof host.sessionRef === "function" ? String(host.sessionRef(ctx) || "") : ""; }
    catch { sessionFile = ""; }
    try { instanceId = typeof host.instanceId === "function" ? String(host.instanceId(ctx) || "") : ""; }
    catch { instanceId = ""; }
    const bindingId = ownerFacts && ownerFacts.binding_id;
    const bindingGen = ownerFacts && ownerFacts.binding_gen;
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
    if (!identity || identity.actor !== box) gaps.push("identity_actor");
    if (!ownerFacts) gaps.push("owner_facts");
    if (Number.isInteger(identity && identity.generation) && identity.generation !== bindingGen) gaps.push("identity_gen_vs_owner_gen");
    if (typeof bindingId !== "string" || !bindingId) gaps.push("owner_binding_id");
    if (!Number.isInteger(bindingGen) || bindingGen <= 0) gaps.push("owner_binding_gen");
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
      state.lastError = String((e && e.message) || e);
      diagFailure(`round.record_payload failed; no message sent: ${state.lastError}`, ctx);
      await settleUnsent(unit, token, "round.record_payload failed", ctx);
      return cfg.backoffMaxMs;
    }
    if (!payload || payload.recorded !== true || payload.text_available !== true
        || payload.text_matches !== true || payload.conflict !== false) {
      diagFailure("round.record_payload did not pass the hard pre-send gate; no message sent", ctx);
      await settleUnsent(unit, token, "payload capture gate refused", ctx);
      return cfg.backoffMaxMs;
    }
    if (hostUnavailable()) {
      await settleUnsent(unit, token, "the host cannot start a round now", ctx);
      return 0;
    }

    const associationId = randomUUID();
    const messageDetails = {
      officeWakeId: associationId, officeRoundToken: token, officeAnchorRef: anchorRef,
      officeAttemptKey: attemptKey, officeSelectedRefs: selectedRefs, officeBatchSha256: batchSha256,
    };
    state.inflightRound = {
      unit, token, anchorRef, selectedRefs, attemptKey, requestKey, dueKey,
      text, batchSha256, sessionFile, associationId, messageDetails,
      dispatchAttempted: true, beforeStartSeen: false, admissionRecorded: false,
      runBegan: false, accepted: false, sawStart: false, sequenceInvalidated: false,
      unknownSettleAttempted: false, consumeInFlight: false,
    };
    try {
      await host.triggerTurn(text, messageDetails);
      state.turnRequests += 1;
    } catch (e) {
      if (!state.inflightRound) return 0; // Core rejected the batch before native enqueue.
      state.lastError = String((e && e.message) || e);
      diagFailure(`Pi sendMessage outcome unknown; retain in-flight batch: ${state.lastError}`, ctx);
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
    const ac = new AbortController();
    state.abort = ac;
    let got = null;
    try {
      got = await client.awaitFrames({waitMs: cfg.waitMs, skip: [...state.awaitSkip]});
      state.lastError = "";
    } catch (e) {
      // ④ 有界退避：失败不清零、也不无限放大。
      state.lastError = String((e && e.message) || e);
      diagFailure(`wake wait failed: ${state.lastError}`, ctx);
      const wait = Math.min(cfg.backoffMaxMs, cfg.backoffBaseMs * Math.pow(2, Math.min(state.awaitFailures, 6)));
      state.awaitFailures += 1;
      return ac.signal.aborted ? 0 : wait;
    } finally {
      if (state.abort === ac) state.abort = null;
    }
    if (state.stopped) return 0;
    if (!got || got.ok === false) {
      // 拒绝（有 code）与未知（读不到）都不起轮。未知按未知保留，进同一有界退避。
      const wait = Math.min(cfg.backoffMaxMs, cfg.backoffBaseMs * Math.pow(2, Math.min(state.awaitFailures, 6)));
      state.awaitFailures += 1;
      diagFailure(`wake wait refused/unknown: ${(got && got.code) || "no_result"}`, ctx);
      return wait;
    }
    state.awaitFailures = 0;
    const frames = Array.isArray(got.frames) ? got.frames : [];
    // 到点没有帧是正常的一次等待（`round.await` 本身就阻塞），立刻再挂一次即可，不空转。
    if (!frames.length) return 0;
    for (const frame of frames) {
      if (state.stopped || state.inflightRound || state.pendingIdle) break;
      const dueKey = (frame && frame.due_key) || "";
      let verdict = "failed";
      try {
        verdict = await onWakeFrame(frame, ctx);
      } catch (e) {
        state.lastError = String((e && e.message) || e);
        diagFailure(`frame handling failed: ${state.lastError} due_key=${dueKey}`, ctx);
      }
      if (verdict !== "batch_handled" && verdict !== "turn_requested") {
        if (verdict === "work_check_unavailable") return cfg.backoffBaseMs;
        const skipped = verdict === "obsolete_work" ? frame.attempt_key : dueKey;
        if (skipped) state.awaitSkip.add(skipped);
        diag(`frame not acked (${verdict}) due_key=${dueKey}`, ctx);
      }
    }
    return 0;
  }

  /** Pi 的原生 pre-start 事件（形状严格；不匹配就什么都不记）。 */
  async function beforeAgentStart(event, ctx) {
    return admitExact({text: (event && event.message && event.message.content) || "", event, viaPiEvent: true}, ctx);
  }

  /**
   * 没有 pre-start 原生事件的宿主自报的那一次提交（Codex：`turn/start`/`turn/steer` 的答复之后）。
   * 判据只有"正文逐字相同 + 同一会话"。
   */
  async function submittedExact(text, ctx, nativeMessageId) {
    return admitExact({text, nativeMessageId}, ctx);
  }

  async function registerOwner(ctx, unit = box) {
    const key = String(unit || box);
    const known = state.ownerTokens.get(key);
    if (known) {
      state.ownerToken = known.token;
      state.ownerFacts = known.facts;
      return true;
    }
    if (state.ownerToken && key === box) return true;      // 兼容：启动时已按本箱登记过
    try {
      if (typeof client.ownerRegister !== "function") throw new Error("client.ownerRegister missing");
      const got = await client.ownerRegister(key);
      if (!got || typeof got.owner_token !== "string" || !got.owner_token) {
        throw new Error(`round.owner.register returned no owner_token: ${JSON.stringify(got)}`);
      }
      state.ownerToken = got.owner_token;
      const factsOk = typeof got.binding_id === "string" && Boolean(got.binding_id)
        && Number.isInteger(got.binding_gen) && got.binding_gen > 0
        && typeof got.instance === "string" && Boolean(got.instance);
      state.ownerFacts = factsOk ? {
        binding_id: got.binding_id, binding_gen: got.binding_gen, instance: got.instance,
      } : null;
      if (state.ownerFacts && state.identity && Number.isInteger(state.identity.generation)
          && state.identity.generation !== state.ownerFacts.binding_gen) {
        state.ownerFacts = null;
      }
      if (!state.ownerFacts) {
        diagFailure("round.owner.register lacks consistent trusted binding facts; sending stays disabled", ctx);
      }
      state.ownerTokens.set(key, {token: state.ownerToken, facts: state.ownerFacts});
      diag(`Core round owner registered for unit=${key}`, ctx);
      return true;
    } catch (e) {
      state.lastError = String((e && e.message) || e);
      diagFailure(`round owner registration failed: ${state.lastError}`, ctx);
      return false;
    }
  }

  /** 持续等待的主循环（**只在 start() 里跑**，stop() 立刻退）。 */
  async function run(ctx) {
    // ★ 先确认身份再等待：`ready()` 顺带把 connBox 定下来（`round.*` 的 box 恒为本箱）。
    //   不可达/未就绪时有界重试；没有身份快照绝不开始 round 或发送正文。
    let ownerRetryMs = cfg.backoffBaseMs;
    let readyRetryMs = cfg.backoffBaseMs;
    try {
      while (!state.stopped) {
        if (!state.identity) {
          try {
            state.identity = await ready(client, box);
            readyRetryMs = cfg.backoffBaseMs;
            diag("native Pi Core identity ready", ctx);
          } catch (e) {
            state.lastError = String((e && e.message) || e);
            diagFailure(`wake loop not ready: ${state.lastError}`, ctx);
            await sleep(readyRetryMs, state.stopController.signal);
            readyRetryMs = Math.min(cfg.backoffMaxMs, readyRetryMs * 2);
            continue;
          }
        }
        if (!state.ownerToken) {
          if (!(await registerOwner(ctx))) {
            await sleep(ownerRetryMs, state.stopController.signal);
            ownerRetryMs = Math.min(cfg.backoffMaxMs, ownerRetryMs * 2);
            continue;
          }
          ownerRetryMs = cfg.backoffBaseMs;
        }
        if (state.stopped) break;
        if (state.inflightRound || state.pendingIdle || hostUnavailable()) {
          await waitForNextTick(cfg.waitMs);
          continue;
        }
        let next = 0;
        try {
          next = await tick(ctx);
        } catch (e) {
          // 任何未预期异常都不许把循环打死（否则"关闭可退出"变成"静默死掉"）
          state.lastError = String((e && e.message) || e);
          diagFailure(`wake loop error: ${state.lastError}`, ctx);
          next = cfg.backoffMaxMs;
        }
        if (state.stopped) break;
        // ★ 等待本身是阻塞的（`round.await`）；这里只保证 `next` 不合法时不至于负值。
        const wait = next | 0;
        await waitForNextTick(wait);
      }
    } finally {
      const units = state.ownerTokens.size ? [...state.ownerTokens.keys()] : (state.ownerToken ? [box] : []);
      for (const unit of units) {
        try {
          await client.ownerRevoke(unit);
          diag(`Core round owner revoked for unit=${unit}`, ctx);
        } catch (e) {
          state.lastError = String((e && e.message) || e);
          diagFailure(`round owner revoke failed for unit=${unit}: ${state.lastError}`, ctx);
        }
      }
      state.ownerTokens.clear();
      state.ownerToken = null;
      state.ownerFacts = null;
      state.running = false;
    }
  }

  function sessionFileFor(ctx) {
    try { return typeof host.sessionRef === "function" ? String(host.sessionRef(ctx) || "") : ""; }
    catch { return ""; }
  }

  async function retainUnknown(cur, reason, ctx) {
    if (!cur) return false;
    cur.sequenceInvalidated = true;
    cur.unknownReason = reason;
    diagFailure(`${reason}; retain the Office batch as unknown`, ctx);
    if (cur.unknownSettleAttempted || cur.accepted) return false;
    cur.unknownSettleAttempted = true;
    try {
      const result = await client.settle(cur.unit, cur.token, "unknown", cur.harnessRef);
      cur.unknownSettleRecorded = Boolean(result && result.ok !== false && result.token === cur.token &&
        (result.state === "unknown" || result.result === "unknown"));
      if (!cur.unknownSettleRecorded) {
        diagFailure("round.settle(unknown) was not confirmed; retain local association", ctx);
      }
      return cur.unknownSettleRecorded;
    } catch (e) {
      state.lastError = String((e && e.message) || e);
      diagFailure(`round.settle(unknown) failed; retain local association: ${state.lastError}`, ctx);
      return false;
    }
  }

  function matchesBeforeStart(event, cur) {
    const message = event && event.message;
    const details = message && message.details;
    return Boolean(cur && cur.dispatchAttempted && event?.messageType === "custom"
      && message?.customType === "office-wake"
      && details?.officeWakeId === cur.associationId
      && details?.officeRoundToken === cur.token
      && details?.officeAnchorRef === cur.anchorRef
      && details?.officeAttemptKey === cur.attemptKey
      && details?.officeBatchSha256 === cur.batchSha256
      && sameStrings(details?.officeSelectedRefs, cur.selectedRefs)
      && typeof message.content === "string"
      && message.content === cur.text
      && sha256Text(message.content) === cur.batchSha256);
  }

  /**
   * 精确受理的唯一一个体：**正文逐字相同 + 同一会话**才记 admission。
   *
   * 两个入口进的是同一个体，只是"谁说自己提交了"不同：
   *  - `beforeAgentStart`：Pi 的原生 `before_agent_start`（自定义消息形状，`matchesBeforeStart` 严格校验）；
   *  - `submittedExact`：没有 pre-start 原生事件的宿主（Codex）自报"我刚把这一份原文交出去"，
   *    只认正文逐字相同 + 同一会话 —— 不伪造 Pi 的事件形状，也不多一个判据。
   */
  async function admitExact({text, event = null, viaPiEvent = false, nativeMessageId}, ctx) {
    const cur = state.inflightRound;
    if (!cur) return false;
    const matches = viaPiEvent
      ? matchesBeforeStart(event, cur)
      : (typeof text === "string" && text === cur.text && sha256Text(text) === cur.batchSha256);
    const sessionFile = sessionFileFor(ctx);
    const sameSession = Boolean(sessionFile && sessionFile === cur.sessionFile);
    if (cur.beforeStartSeen) {
      if (!matches || !sameSession) {
        // Pi's next before-start proves the prior prepareRun did not reach agent_start/settled.
        // Do not let a later agent_start or settled event be attributed to the old Office batch.
        await retainUnknown(cur, !sameSession
          ? "host session changed after the batch before-start"
          : "a nonmatching before-start followed the batch before-start", ctx);
      }
      return false; // duplicate exact hooks are not a second admission
    }
    if (!sameSession) {
      await retainUnknown(cur, "host session differs from the dispatched batch", ctx);
      return false;
    }
    if (!matches || cur.sequenceInvalidated) {
      await retainUnknown(cur, "a nonmatching before-start superseded the dispatched batch", ctx);
      return false;
    }

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
      const admission = await client.recordAdmission(cur.unit, cur.token, harnessRef, fallback, Boolean(nativeMessageId));
      if (admission?.reason === "stale_frame") {
        state.inflightRound = null;
        return false;
      }
      if (!admission || admission.refused || !hasRecorded(admission)) {
        throw new Error("round.record_admission returned no usable receipt");
      }
      cur.admissionRecorded = true;
      diag("exact batch matched the host submission; admission recorded; awaiting the native start", ctx);
      return true;
    } catch (e) {
      state.lastError = String((e && e.message) || e);
      await retainUnknown(cur, `matched batch admission remains unknown: ${state.lastError}`, ctx);
      return false;
    }
  }


  async function agentStart(ctx, info) {
    state.busy = true;
    // ★ 纯续工走**同一条**原生起点事件：宿主把它替我触发的那一次 attempt_key 交回来，我只认这一个
    //   （不认"下一个起点"——那会把人的 prompt 轮也算成我们的）。报事实 + 回执，然后不再重复。
    const idle = state.pendingIdle;
    if (idle && info && info.pendingAttemptKey && info.pendingAttemptKey === idle.attemptKey) {
      state.pendingIdle = null;
      // 这一次尝试从现在起就是本端在跑的那一次：后面的模型增量靠它关联（D2）。
      state.activeAttempt = idle.attemptKey;
      await reportAttemptStarted(idle.attemptKey, ctx);
      await ackFrame(idle, ctx);
      return true;
    }
    const cur = state.inflightRound;
    if (!cur || cur.sequenceInvalidated) return false;
    if (!cur.beforeStartSeen || !cur.admissionRecorded) {
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
    state.activeAttempt = cur.attemptKey;      // 同一次尝试：`round.started` 那个键不是唯一来源
    try {
      const run = await client.runBegin(cur.unit, cur.token, state.ownerToken, cur.harnessRef);
      if (!hasRecorded(run)) throw new Error("round.run_begin did not record the matched host run");
      cur.runBegan = true;
      const settled = await client.settle(cur.unit, cur.token, "accepted", cur.harnessRef);
      if (!acceptedSettle(settled)) throw new Error("round.settle did not confirm accepted");
      cur.accepted = true;
      // 产品 §9「模型无进展」也盖**普通业务轮**：复用这一次尝试已有的键（`record_payload` 那次），
      // 不新造第二次尝试、不开平行通知 —— 起轮/进展/结束仍是同一条链。
      await reportAttemptStarted(cur.attemptKey, ctx);
      diag("matched native start recorded run-begin and accepted settle", ctx);
      await ackFrame(cur, ctx);
      return true;
    } catch (e) {
      state.lastError = String((e && e.message) || e);
      await retainUnknown(cur, `matched Pi agent_start lifecycle remains unknown: ${state.lastError}`, ctx);
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
    if (shaped.ok === false || shaped.retired) return shaped;
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

  /** 报「这一次尝试真的起轮了」：续工与业务轮共用**这一处**（同一个键、同一条通知）。 */
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
   * 为什么需要：迟到的一条增量若照样上报，Core 会把 `turn_closed_at` 抹掉（`host_event(model_progress)`
   * 传的是 `None`），而 `_silence_extend` 对"不在 armed"的行是**重新挂** —— 一个已经结束的回合
   * 会变成下一次"模型无动静"的欠账。按回合标识比，不是个裸布尔：能说清是哪一次回合关了。
   */
  function turnClosed(turnId) {
    state.closedTurn = String(turnId || "");
    // 回报"关的是哪一次尝试"：`turn_end` 带上它才按**那一次**清心跳到期项（Core 的回退是
    // "这个代次恰好一条 armed"，两次尝试挨得近时会清错行）。
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
    const firstOfTurn = slot.attemptId !== attemptId || slot.turn !== turn || !slot.sent;
    if (!firstOfTurn && nowMs - slot.lastMs < cfg.modelProgressMinMs) {
      slot.suppressed += 1;
      return { ok: true, throttled: true, attempt_id: attemptId };
    }
    slot.attemptId = attemptId;
    slot.turn = turn;
    slot.sent += 1;
    slot.lastMs = nowMs;
    return sendAttemptEvent("model_progress", attemptId, Math.floor(nowMs / 1000));
  }

  async function agentSettled(event, ctx) {
    state.busy = false;
    state.activeAttempt = "";
    state.modelProgress = {attemptId: "", turn: "", sent: 0, lastMs: 0, suppressed: 0};
    state.pendingIdle = null;
    const cur = state.inflightRound;
    if (!cur) {
      state.awaitSkip.clear();
      requestPendingWake(); // An unrelated host run may have been the only reason mail was deferred.
      return false;
    }
    if (cur.sequenceInvalidated) {
      if (cur.sawStart && cur.unknownSettleRecorded && sessionFileFor(ctx) === cur.sessionFile) {
        state.inflightRound = null;
        state.awaitSkip.clear();
        requestPendingWake();
      }
      return false;
    }
    if (!cur.beforeStartSeen || !cur.sawStart || !cur.runBegan || !cur.accepted) {
      await retainUnknown(cur, "agent_settled is not tied to a valid admitted Office batch", ctx);
      return false;
    }
    if (cur.consumeInFlight) return false;
    const sessionFile = sessionFileFor(ctx);
    if (!sessionFile || sessionFile !== cur.sessionFile) {
      await retainUnknown(cur, "agent_settled session differs from the admitted batch", ctx);
      return false;
    }
    if (!event || typeof event !== "object" || typeof event.aborted !== "boolean") {
      await retainUnknown(cur, "agent_settled lacks Pi's boolean aborted fact", ctx);
      return false;
    }
    cur.consumeInFlight = true;
    cur.aborted = event.aborted;
    try {
      // Preserve Pi's event facts, including aborted, without inferring a run id.
      const exactEvent = {...event, type: "agent_settled"};
      const result = await client.consume(cur.unit, exactEvent, state.ownerToken, cur.token);
      if (!hasRecorded(result)) {
        await retainUnknown(cur, "round.consume did not record the matched agent_settled", ctx);
        return false;
      }
      state.inflightRound = null;
      state.awaitSkip.clear();
      requestPendingWake();
      diag("round.consume recorded for the matched owner/token/session", ctx);
      return true;
    } catch (e) {
      state.lastError = String((e && e.message) || e);
      await retainUnknown(cur, `round.consume failed; retain unknown: ${state.lastError}`, ctx);
      return false;
    } finally {
      if (state.inflightRound === cur) cur.consumeInFlight = false;
    }
  }

  async function lifecycleBoundary(name, ctx, action) {
    try {
      return await action();
    } catch (e) {
      state.lastError = String((e && e.message) || e);
      const detail = `${name} handler failed: ${state.lastError}`;
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

  /**
   * S2 文件帧入口（v1）。帧只带来**唤醒的事实**，不带来批次的正文：
   * `batch_round` 仍走既有 Core 取件（`round.begin`，正文由 Core 的 payload 给），`idle_started` 的
   * 正文本来就是投递自己的句子。`v`/`kind` 的形状检查在读帧那一层（`office_pi_wake_frames.mjs`）。
   *
   * 「已在飞一轮就不再起第二个」这条判据在宿主这一侧：Core 只会在同一次尝试上收到 already_attempted，
   * 但宿主多发一次 turn 是实打实的成本。
   */
  async function onWakeFrame(frame, ctx) {
    // ★ W439：在 A 之下"帧没正文/没 due_key"正常**永远不该**发生，
    //  所以它不能是一个静默兜底：停住的同时**出声**，并把回执键带上，才查得到是哪一条 due 出的帧。
    const due = (frame && typeof frame.due_key === "string") ? frame.due_key : "(no due_key)";
    const reject = (reason) => {
      diagFailure(`frame rejected: ${reason} due_key=${due}`, ctx);
      return "bad_frame";
    };
    if (!frame || typeof frame.kind !== "string" || !frame.kind) return reject("no kind");
    // ★ W439：硬门在**帧边界**上按帧自己的事实判 —— `kind` 认识、
    //  `due_key` 非空（回执键）、`text` 非空（两类帧都带正文）、`batch_round` 还要 `refs` 非空。
    //  空 text 只会在"投递侧真的没写"时出现，那是真缺陷：停住、不猜、不自己拼。
    if (typeof frame.due_key !== "string" || !frame.due_key) return reject("empty due_key");
    if (typeof frame.text !== "string" || !frame.text) return reject("empty frame text");
    if (frame.kind === "batch_round" && !(Array.isArray(frame.refs) && frame.refs.length)) return reject("empty refs");
    if (hostUnavailable()) return "host_busy";
    const unit = typeof frame.unit === "string" && frame.unit ? frame.unit : box;
    if (typeof host.beforeBegin === "function" && ["batch_round", "idle_started"].includes(frame.kind)) {
      await host.beforeBegin(ctx);
      if (state.stopped || !state.running || state.inflightRound) return "not_running";
      if (hostUnavailable()) return "host_busy";
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
      // 这一帧就是"这个单元有到期项"的事实；正文仍由 Core 在 `round.begin` 里给（不在这里渲染第二遍）。
      // ★ W439：批次须从 Core 领取。
      //   取不到就按既有失败路径记一行、帧保持"已取走" —— **绝不**在没有 `round.begin` 时自己渲染批次：
      //   那就是"这批信说什么"的第二个答案。
      await handleWake(ctx, [unit], frame.kind, typeof frame.text === "string" ? frame.text : "",
        Array.isArray(frame.refs) ? frame.refs : null, frame.due_key, {...frame, unit});
      return "batch_handled";
    }
    if (frame.kind === "idle_started") {
      if (state.inflightRound) return "already_inflight";
      if (typeof frame.text !== "string" || !frame.text) return reject("empty idle text");
      if (typeof host.triggerTurn !== "function") return "no_trigger_turn";
      const current = await client.workRunnable(unit);
      if (current?.ok === false || !Number.isInteger(current?.count)) return "work_check_unavailable";
      if (current.count === 0) return "obsolete_work";
      if (state.stopped || hostUnavailable()) return "host_busy";
      // ★ 纯续工**不造业务 round**（不 `round.begin`、不记 payload），但必须在调用原生**之前**登记
      //   "这一次尝试的 attempt_key、这一帧的 due_key"。否则原生真的起轮了、Core 却没有任何记账，
      //   帧会被反复拉起（独核反例 `32220768`：多于 2 个 agent_start）。
      state.pendingIdle = {unit, dueKey: String(frame.due_key || ""), attemptKey: String(frame.attempt_key || "")};
      state.turnRequests += 1;
      // `details` 带上原生尝试标识与到期键，便于和 Core 的轮次对账（来信第三节）。
      try {
        await host.triggerTurn(frame.text, {
          officeWorkContinuation: true,
          attempt_key: frame.attempt_key,
          due_key: frame.due_key,
        });
      } catch (e) {
        state.pendingIdle = null;
        throw e;
      }
      return "turn_requested";
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
    // ★ 第二参必须**原样传下去**：宿主在 `agent_start` 时交回它替我触发的那一次 attempt_key，
    //   wrapper 丢掉它 ⇒ 纯续工永远匹配不上、不记账、不回执，同一帧被反复拉起（真机实测几十轮）。
    agentStart: (ctx, info) => lifecycleBoundary(
      "agent_start", ctx, () => agentStart(ctx, info)),
    agentSettled: (event, ctx) => lifecycleBoundary(
      "agent_settled", ctx, () => agentSettled(event, ctx)),
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
      const longPollInFlight = Boolean(state.abort);
      try { state.stopController.abort(); } catch { /* 忽略 */ }
      try { if (state.abort) state.abort.abort(); } catch { /* 忽略 */ }
      if (longPollInFlight) {
        try {
          const closed = client.close?.();
          if (closed && typeof closed.catch === "function") closed.catch(() => {});
        } catch { /* close 失败不改变 unknown round 状态 */ }
      }
    },
    /** 诊断快照（夹具断言用；**不**作为产品判定）。★ W436：只留真的有人在读的字段 ——
     *  `stopped` / `emptyWake` / `lastError` 三个曾经也在里面，全仓零读者（它们本身在循环里
     *  照旧被读写，只是没有人从快照里看），删掉。 */
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
