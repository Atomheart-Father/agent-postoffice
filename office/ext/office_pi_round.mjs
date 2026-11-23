// Pi 宿主侧的受控 round 客户端 + 等待/顺序判据（W439 首切片）。
//
// 为什么是这个文件：本树只保留了 Pi 原生片段，原来的 `office_daemon.mjs` 不在保留集里，
// 而 `office_wake_loop.mjs` 需要它的客户端与受控请求（awaitFrames /
// created 形状）。这里**不另造入口**：传输仍是既有的 `office_socket.mjs`（一条受控 socket、
// 一份方法表），本文件只提供该 socket 之上的业务投影。
//
// ## 首切片公共接口（已定）
//
//   · 请求：同一受控 JSON op，**顶层** `op` + `act_key` + 业务字段。
//   · 身份：**不在 payload 里自报**。连接的受控 binding（actor + binding_id + generation +
//     instance）由传输侧定；本层只做一件事 —— `round.*` 的 `box` 恒等于本箱（身份**回指**），
//     投递单元走 `unit`。
//   · 结果三态，彼此不混：
//       ① 成功：**把 Core 的 result 本体交回**（本树既有调用点就是按本体读的：`created`/`token`/`text`…）；
//       ② `{ok:false, code, message}`    —— **明确拒绝**（业务/授权/参数），`code` 是机器可判的那个；
//       ③ `{ok:false, unknown:true, code, message, retry_ref}` —— **传输未知**（没送到 / 回复前断线），
//          原样带回**同一个** act_key 与 `retry_ref`，由调用方按"结果未知"处理，绝不猜成成功或拒绝。
//
//   `host.event`（kind/ts/…）是 runtime 的路由；本层只负责把 op 发上去、把三态如实交回。

import {createOfficeSocket, openControlledEndpoint} from "./office_socket.mjs";
import {readFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";

/** 短名 → 线方法。一个动作一个短名：要新动作就在这里加一行，不加通用口。 */
const OPS = {
  // ★ W439：whoami 走**工具 spec 的统一 op**（`office_tool_spec.mjs` 里 `office_whoami` 就是这个 op），
  //   本层不再自留一条 `identity.whoami` 的路。
  context: "office.context",
  begin: "round.begin",
  recover: "round.recover",
  recordPayload: "round.record_payload",
  recordAdmission: "round.record_admission",
  runBegin: "round.run.begin",
  settle: "round.settle",
  consume: "round.consume",
  endNative: "round.run.end",
  roundState: "round.payload",
  awaitRound: "round.await",
  wakeAck: "wake.ack",
  ownerRegister: "round.owner.register",
  ownerShow: "round.owner.show",
  ownerRevoke: "round.owner.revoke",
  hostEvent: "host.event",
  usageRecord: "usage.record",
  workRunnable: "work.runnable",
};

/** 传输层自己产出的错误码：没有"业务答案"这回事，一律是**结果未知**（不是拒绝）。 */
const TRANSPORT_UNKNOWN = new Set(["socket_error", "socket_closed", "no_socket_path", "transport_unknown"]);

/**
 * 端点描述符的**唯一入口**（与 runtime 侧写出的那一份同名同形）：`{ socketPath, tokenFile, ... }`。
 *
 * 读不懂 / 缺字段 / 不是对象 ⇒ `null`（fail closed，不猜路径、不合成凭据）。
 * 身份四个字段（`actor`/`binding_id`/`generation`/`instance`）在**写入侧**固定；本层只如实读出哪一份
 * 描述符，不用它当授权（真正的判权在受控线路那一侧）。
 */
export function readDescriptor(file) {
  return openControlledEndpoint({descriptor: String(file || ""), envDescriptor: ""}).endpoint ?? null;
}

/** 这条错误码是不是"结果未知"。放在这里而不是散在调用点：三态的分界只有一处。 */
const isTransportUnknown = (code) => TRANSPORT_UNKNOWN.has(String(code || ""));

/** `round.begin` 的 `created` 契约：true/1 = 有货，false/0 = 这一拍没货，其它一律畸形（≠没货）。 */
export const isCreated = (value) => value === true || value === 1;
export const hasCreatedFact = (value) => isCreated(value) || value === false || value === 0;

/**
 * 受控 round 客户端。`socketPath` / `tokenFile` 来自本箱自己的端点描述符（由调用方给，不猜路径）。
 *
 * 返回值就是 `office_wake_loop.mjs` 需要的那组短名；每个方法都返回上面那个三态信封
 * （成功/拒绝/未知各是各的），只有 `setConnBox`/`close` 是本地动作。
 */
export function createPiRoundClient({ socketPath, tokenFile } = {}) {
  const sock = createOfficeSocket({ socketPath, tokenFile });
  /** The local mailbox used to omit redundant unit fields and correlate host events. */
  let connBox = "";

  /** 同一个动作重试要带**同一个** act_key（幂等回读原记录）；没有业务线索时退化为 op 名。 */
  function actKeyFor(op, fields) {
    // `host.event` 是**多条**事实共用一个 op：键必须由这条事实自己的原生标识组成。否则第二条起
    // 就是"同键异参" —— Core 对同键异参回 `conflict`，观测会被静默丢掉（第一条看起来还成功）。
    // 键由帧本身决定 ⇒ 同一次观测的重试仍复用同一个键；新的增量（新 version）天然是新的一笔。
    if (op === "host.event") {
      // `ts` 也要进键（或重试时原样复用 ts）。同一件事换时间重发会
      // 变成"同键异参" ⇒ `conflict` ⇒ 观测被丢；把 ts 纳进来，重发就是新的一笔，不会被误判成冲突。
      const disc = [fields && fields.kind, fields && fields.attempt_id, fields && fields.native_id, fields && fields.turn_id,
        fields && fields.version, fields && fields.ts]
        .filter((v) => v !== undefined && v !== null && v !== "").join(":");
      return `pi:${connBox || "-"}:host.event:${disc || "-"}`;
    }
    const scope = fields && (fields.token || fields.owner_token || fields.ref || fields.unit || "");
    return `pi:${connBox || "-"}:${op}:${scope || "-"}`;
  }

  async function call(name, fields = {}) {
    const op = OPS[name];
    if (!op) throw new Error(`unknown_op: ${name}`);
    const retryRef = fields && fields.retry_ref;
    // 每个 op 只发它**自己**的字段。实测（真 daemon，`b0d55a32`）：`round.owner.register` 的请求
    // 带了 `act_key`/`box` 就被判"未知字段"拒绝，于是每 0.5–4s 重试、永远没有 owner、轮起不来。
    // 裁决口径：`act_key` 是 `host.event` 的幂等键（只有它要）；`box` 不是任何 round.* 的字段 ——
    // 连接身份由**服务端**从 socket 的受信 ctx 定，客户端自报它本来就与"不从线路自报身份"相悖。
    const wire = { ...fields, op };
    delete wire.retry_ref;
    if (op === "host.event") wire.act_key = fields.act_key || actKeyFor(op, fields);
    if (op.startsWith("round.") && name !== "ownerRegister" && name !== "roundState") {
      const unit = wire.unit;
      if (!unit || unit === connBox) delete wire.unit;   // 个人箱的单元不发（缺省 = 本箱）
    }
    let got;
    try {
      got = await sock.handle(wire);
    } catch (e) {
      return { ok: false, unknown: true, code: "transport_unknown",
        message: String((e && e.message) || e), retry_ref: retryRef };
    }
    if (got && got.ok === false) {
      const code = String(got.error || got.code || "internal");
      const out = { ok: false, code, message: String(got.message || "") };
      if (got.reason_code) out.reason_code = got.reason_code;
      if (got.detail) out.detail = got.detail;
      if (got.credential_changed) out.credential_changed = true;
      if (isTransportUnknown(code)) { out.unknown = true; out.retry_ref = retryRef; }
      return out;
    }
    // 成功：传输把 Core 的 result 本体交回；op 没有返回体时就是 null。
    // （拒绝/未知是上面那两种 `{ok:false,…}` 信封 —— 调用方按 `ok === false` 分开处理。）
    return got === undefined ? null : got;
  }

  return {
    /** 告诉客户端"我是哪个箱"：`round.*` 的 `box` 恒用它。 */
    setConnBox(box) { connBox = String(box || ""); return this; },
    acquireSerial: () => sock.acquireSerial(),
    releaseSerial: () => sock.releaseSerial(),
    async serialState(op, state) {
      const out = await sock.serialRequest({op: `host.serial.${op}`, ...(state ? {state} : {})});
      if (!out || out.ok === false) {
        const error = Error('serial state unknown');
        error.serialReadRetryable = op === 'read' && (['socket_error', 'socket_closed', 'internal', 'busy'].includes(out?.error)
          || (out?.error === 'unauthorized_attempt' && out.credential_changed));
        error.serialWriteDisconnected = op === 'write' && ['socket_error', 'socket_closed'].includes(out?.error);
        throw error;
      }
      return out;
    },

    context: () => call("context"),

    /**
     * Core `round.begin`：这一拍这个单元有没有货。入参是**投递单元**（个人箱时它等于本箱）与
     * `channel` —— 实测 `round_begin(actor, *, channel, unit=None, …)` 里 `channel` 是**必填的非空
     * 字符串**（缺了直接 `invalid_request: 请求缺少必填字段 ['channel']`）。它只是被记下的"这一轮
     * 是经哪条通道起的"，不是政策；值由调用点给（帧消费给帧的 kind，长轮询给 "poll"）。
     */
    begin: (unit, channel = "wake", frame) => call("begin", { unit, channel,
      ...(frame ? {expected_refs:frame.refs, due_version:frame.due_version} : {}) }),
    recover: (unit) => call("recover", {unit}),

    /** 发送前的硬门：把精确 batch 正文记成 anchor payload。 */
    recordPayload: (unit, roundToken, anchorRef, attemptKey, text) =>
      call("recordPayload", { unit, token: roundToken, ref: anchorRef, attempt_key: attemptKey, text }),

    /**
     * **原生受理**回执：只认调用方给的原生尝试标识（`harnessRef`，Pi 自己的关联标识），
     * 持有绑定由传输侧的受控 ctx 证明 —— 这里**不自报** binding/identity。
     */
    recordAdmission: (unit, roundToken, harnessRef, fallback, beforeEnqueue) =>
      call("recordAdmission", { unit, token: roundToken, harness_ref: harnessRef, fallback,
        ...(beforeEnqueue ? {before_enqueue:true} : {}) }),

    /** **真正开始**：宿主的原生 start 已发生（同一尝试标识 + 持有绑定），再落 run-begin。 */
    runBegin: (unit, roundToken, ownerToken, harnessRef, nativeText) =>
      call("runBegin", { unit, token: roundToken, owner_token: ownerToken, harness_ref: harnessRef, native_text: nativeText }),

    /** 结算：accepted / rejected / unknown（未知走这里时不带 accepted 语义）。 */
    settle: (unit, roundToken, status = "accepted", harnessRef) =>
      call("settle", { unit, token: roundToken, status, harness_ref: harnessRef }),

    /** 宿主原生回合结束事实（原样上报，不改写宿主给的字段）。 */
    consume: (unit, event, ownerToken, roundToken) =>
      call("consume", { unit, event, owner_token: ownerToken, round_token: roundToken }),

    endNative: (nativeRef) => call("endNative", {native_ref: nativeRef}),

    roundState: (unit, token) => call("roundState", { unit, token }),

    /** Wait for the current binding's wake frames. `skip` belongs to the caller's in-flight work. */
    awaitFrames: ({waitMs, unit, skip, toolMail} = {}) => {
      const fields = {wait_ms: waitMs};
      if (unit) fields.unit = unit;
      if (toolMail) fields.tool_mail = true;
      if (Array.isArray(skip) && skip.length) fields.skip = skip.filter((k) => typeof k === "string" && k);
      return call("awaitRound", fields);
    },

    /**
     * 回执：**只有真的受理/起轮之后**才发（`wake.ack` → `clear_batch` / `record_attempt` 就是结清
     * 到期项的那一步）。拒帧绝不发 —— 发了就等于把那封信从队列里抹掉而轮没人跑。
     */
    wakeAck: (unit, dueKey, attemptKey) => call("wakeAck", {unit, due_key: dueKey, attempt_key: attemptKey}),

    /** 活动：只在状态变化时发，只发这一个键（哪些相位、什么含义由线方法自己定）。 */


    ownerRegister: (unit, expected_owner_gen) => call("ownerRegister", { unit, ...(expected_owner_gen === undefined ? {} : {expected_owner_gen}) }),
    ownerShow: (unit) => call("ownerShow", { unit }),
    ownerRevoke: (unit, owner) => call("ownerRevoke", { unit, owner_token: owner.owner_token, owner_gen: owner.owner_gen }),

    /** `host.event`：kind/ts 与业务字段由调用方给（路由与字段表是 runtime 的）。 */
    hostEvent: (kind, ts, fields = {}) => call("hostEvent", { kind, ts, ...fields }),
    workRunnable: (unit) => call("workRunnable", unit ? {unit} : {}),

    /**
     * `usage.record`：只报**原生**用量。每一条的 `entry_id` 是宿主持久消息的 id（Core 按它去重），
     * `act_key` 也由它组成 ⇒ 重发是“回读原记录”，**不需要本地再存一份用量缓存**。
     * `harness` 不在这里送：它是部署侧钉在绑定上的 `surface`，宿主自报就等于给正文开了改身份的扣。
     */
    usageRecord: (entries, actKey) => call("usageRecord", { entries, act_key: actKey }),

    close: (options) => sock.close(options),
  };
}

/**
 * 这一份**实际加载**的发布产物自报的版本 —— `host.event{kind:"loaded"}` 的唯一来源。
 *
 * 读的是发布侧导出时写在自己目录旁边的 `<release>/DEPLOY_SOURCE.json`：它给出不可变的 `commit`，
 * 而且同一份 sha 又出现在 `export = git archive <sha> office` 里；**两处必须一致**，否则这份记录
 * 自相矛盾，宁可不报。
 *
 * 为什么**不**读 `office/VERSION.json`（实测，真机）：8 个在册发布目录里那份文件**逐字相同**
 * （`release=work-1.1-consolidate-20261105`、`source_commit=ebfd5083…`、manifest 10 件且不含 `ext/`）。
 * 它是 2026-11-05 那个候选随件带过来的，不是这个目录的身份 —— 拿它上报会让 A、B 两版看起来一模一样。
 *
 * 找不到 / 读不懂 / 不自洽 ⇒ `null`（当前产物的版本未知）。
 *
 * @param {string} moduleUrl  加载方自己的模块 URL（`import.meta.url`）
 */
export function releaseVersion(moduleUrl) {
  let file = "";
  try { file = fileURLToPath(moduleUrl); } catch { return null; }
  // `<release>/office/ext/<file>` ⇒ 发布根就在上面三层（不扫目录、不猜兄弟目录）。
  const root = dirname(dirname(dirname(file)));
  let src;
  try { src = JSON.parse(readFileSync(join(root, "DEPLOY_SOURCE.json"), "utf8")); } catch { return null; }
  if (!src || typeof src !== "object") return null;
  const commit = String(src.commit || "");
  if (!/^[0-9a-f]{40}$/.test(commit)) return null;
  // 导出记录里那条 `git archive <sha> office` 用的是**短 sha**（实测：`git archive 9165ca8 office`）⇒
  // 它必须是这个 commit 的前缀；换一个 sha 手写的记录就在这里被拒。
  const exported = /^git archive ([0-9a-f]{7,40}) /i.exec(String(src.export || ""));
  if (!exported || !commit.startsWith(exported[1])) return null;
  return { version: commit, root };
}

/**
 * 额度窗口归一：五小时窗 / 周窗 → `five_hour` / `weekly`。
 *
 * ★ W439：**只有这两个窗名**（`watchdog.quota_decision` 里 `429` 只认它们；
 * `402` 不看窗）。别名表就是"同一件事的第二种叫法" —— 没人会发的名字留着，只会让一个认不出的窗
 * 看起来被认出来了。认不出 ⇒ 返回 `""`：调用方据此**不发**这条额度事实（未知保留），既不猜成某个窗，
 * 也不替 Core 说"没阻塞"。本层也**不自行切模型**。
 */
function quotaWindow(raw) {
  const s = String(raw || "").trim().toLowerCase().replace(/[-\s]+/g, "_");
  if (!s) return "";
  if (s === "five_hour") return "five_hour";
  if (s === "weekly") return "weekly";
  return "";
}

/**
 * `host.event` 的 kind 闭集（规格 §3.2）—— 与 Core 的 `HOST_EVENT_KINDS` **逐字同集**：两边不同集，
 * 就会有一边把另一边的合法事实当畸形。
 */
export const HOST_EVENT_KINDS = new Set([
  "tool_start", "tool_progress", "tool_end", "turn_end", "quota", "loaded",
  "model_progress", "round.started", "round.accepted", "resource",
]);

/**
 * 一条原生观测 → `host.event` 的线上帧。**字段表的唯一实现**：观测口与 wake loop 的尝试类事实
 * 都走这里，不各写一份。
 *
 * 关联口径照 Core 的判据分两种，谁也不多要一件：`tool_*` 是「回合里的某次原生动作」⇒
 * `turn_id` + `native_id`；`model_progress`/`round.started`/`round.accepted` 是「某一次原生尝试」
 * ⇒ `attempt_id`（真 Pi 取证 `5e05476a`：增量本身**不带任何 id**）；`turn_end`/`quota`/`loaded`
 * 是合法的无回合事件。身份字段一个都不进业务帧；`fields` 覆盖不了关联/`kind`/`ts`。
 *
 * @returns {{ok:true,ts:number,frame:object}|{ok:false,code:string,message:string}}
 */
export function hostEventFrame(kind, ev = {}) {
  const k = String(kind || "");
  if (!HOST_EVENT_KINDS.has(k)) {
    return { ok: false, code: "event_kind_unknown", message: `host.event kind 不在规格 §3.2 闭集里: ${kind}` };
  }
  const needsTool = k.startsWith("tool_");
  const needsAttempt = k === "model_progress" || k === "round.started" || k === "round.accepted";
  const missing = [];
  if (needsTool) {
    if (typeof ev.turnId !== "string" || !ev.turnId) missing.push("turn_id");
    if (typeof ev.nativeId !== "string" || !ev.nativeId) missing.push("native_id");
  }
  if (needsAttempt && !(typeof ev.attemptId === "string" && ev.attemptId)) missing.push("attempt_id");
  if (missing.length) {
    // 未知保留：关联不全的一条不发（回读只接受同一组关联完全相同）。
    return { ok: false, code: "event_correlation_incomplete",
      message: `原生关联不全，不发这条 host.event: ${missing.join(", ")}` };
  }
  const ts = Number.isFinite(ev.ts) ? ev.ts : Math.floor(Date.now() / 1000);
  const fields = { ...(ev.fields || {}) };
  for (const k2 of ["bindingId", "generation", "actor", "instance", "kind", "ts",
    "turn_id", "native_id", "attempt_id"]) delete fields[k2];
  const frame = { ...fields, kind: k, ts };
  if (typeof ev.turnId === "string" && ev.turnId) frame.turn_id = ev.turnId;
  if (typeof ev.nativeId === "string" && ev.nativeId) frame.native_id = ev.nativeId;
  if (typeof ev.attemptId === "string" && ev.attemptId) frame.attempt_id = ev.attemptId;
  return { ok: true, ts, frame };
}

/**
 * Pi 原生观测 → `host.event` 的**关联组**。
 *
 * D2 定的口径：回读只接受**同一** `binding + generation + turn_id + native_id`；
 * 只有真实增量推进 `progress_revision`，重复输出不推进，未知保留。
 *
 * ★ W439 在途核对（用户 2026-11-19）后的口径，本函数按这四条改：
 *   ① `binding_id`/`generation` 由**传输（socket）解析的可信 ctx** 决定 —— 业务帧**不送**这两个字段，
 *      否则就等于给了"payload 里覆盖身份"的口子。本层只保留**本地 held 校对**：调用方若自己带了
 *      绑定线索，必须与我们持有的那一份一致，否则不发。
 *   ② **只有 `tool_*` 要 `turn_id` + `native_id` 双关联** —— 它们是"某个回合里的某次原生动作"。
 *   ③ `turn_end`/`loaded`/`quota` 是**合法的无回合事件**，不能因为它们没有回合/原生命名就被误拒。
 *   ④ `ev.fields` 不得覆盖关联、`kind`、`ts`；身份字段一个都不进业务帧。
 *
 * 字段表本身在 `hostEventFrame()`（唯一实现）；本函数只管「这条事实能不能从本端口发出去」。
 * 本层不做判定、不维护 revision、不选模型：它发的是事实。
 */
export function createPiHostEvents({ client, binding } = {}) {
  /** The binding supplied by the caller for local event correlation. */
  let held = binding || null;

  return {
    /** 换绑/换代之后由调用方更新；没更新就说明这一帧还是那条绑定。 */
    setBinding(next) { held = next || null; return this; },

    /**
     * 发一条原生观测事实。
     *
     * @param {string} kind   `HOST_EVENT_KINDS` 之一
     * @param {object} ev     `{turnId, nativeId, ts, binding_id?, generation?, fields?}`
     */
    async observe(kind, ev = {}) {
      const k = String(kind || "");
      const shaped = hostEventFrame(k, ev);
      if (shaped.ok === false) return shaped;
      if (typeof client?.hostEvent !== "function") {
        return { ok: false, code: "host_event_unavailable", message: "socket 客户端没有 host.event 口" };
      }
      // 本地 held 校对（身份不在业务帧里送，只在这里对）：带了线索就必须对得上。
      if (ev.bindingId !== undefined || ev.generation !== undefined) {
        const same = held && ev.bindingId === held.bindingId && ev.generation === held.generation;
        if (!same) {
          return { ok: false, code: "event_binding_mismatch",
            message: "事件自带的绑定与本端持有的不一致（或本端尚未持有），不发这条事实" };
        }
      }
      return await client.hostEvent(k, shaped.ts, shaped.frame);
    },

    /**
     * 额度事实：窗名先归一（`five_hour` / `weekly` 两种；认不出就不发这条事实，也不替 Core 判"没阻塞"），
     * 且**不自行切模型**。宿主自报的进展版本字段名是 `version`（不是 `progress_version`）——
     * 它随 `fields` 原样上行，Core 落到 due 行的 `payload.progress_revision`。
     */
    async quota(ev = {}) {
      const window = quotaWindow(ev.window);
      if (!window) {
        return { ok: false, code: "quota_window_unknown",
          message: `额度窗名认不出（只认 five_hour / weekly）: ${ev.window}` };
      }
      return await this.observe("quota", { ...ev, fields: { ...(ev.fields || {}), window } });
    },
  };
}
