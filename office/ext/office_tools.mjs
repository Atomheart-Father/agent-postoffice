// Office 1.1 tool adapter: tool call -> controlled socket request. Host-neutral (Pi and OpenCode share it).
//
// What this layer does, and nothing more (01_DESIGN §5, §5.3, §5.4):
//   * builds the wire body from the tool's own business fields; identity, timestamps, keys, digests and
//     underscore/internal fields never reach the wire from the model;
//   * remembers the latest observed Work version for later writes. A review uses the submission named by
//     the model and is checked directly by Core. The observed version is a concurrency baseline;
//   * one idempotency key per logical action: `busy` is retried a bounded number of times with the SAME key;
//     an outcome that is unknown (connection lost after sending) is reported as such, and an identical retry
//     re-uses the original key, so it can never execute twice.
// Authority is never decided here: Core checks identity, responsibility and versions in its transaction.

import { createHash, randomBytes } from "node:crypto";
import { DEFAULT_LOCALE, normLocale, OFFICE_TOOLS } from "./office_tool_spec.mjs";
import { outcomeOf, OUTCOMES } from "./office_result.mjs";
// The adapter messages live beside this module, keyed by the stable message name. The
// *interpolation* stays here (which value goes into which sentence is calling-path knowledge);
// only where the words live moved. Same load-time import as the other text resources.
import TOOLS_TEXT from "./office_tools_text.json" with { type: "json" };

// ★ W229（审计 6d563dd #6）：进程指纹。操作键原来只有 `actor:op:毫秒:序号`，序号又每进程从 1 起，
//   于是**两个进程在同一毫秒**各发一次会撞出同一个键：Core 的 (actor, op, act_key) 幂等域会把
//   两笔不同动作当成一笔（同参回首次结果、异参报 conflict），端点据键派生的 letter id 也会重合。
//   加一个每次进程启动随机的片段：同进程内的重试照旧复用（重试走内存里的未决键，不重新铸），
//   不同进程不会再靠"恰好不同毫秒"来躲碰撞。
const PROC_NONCE = randomBytes(4).toString("hex");
//: 同一进程里也可能有多个工具实例（一个身份一个；两个会话同一个信箱时也会各一份），
//: 所以键里再带一个实例序号：同实例的重试复用，实例之间、进程之间都不靠毫秒去躲碰撞。
let INSTANCE_SEQ = 0;
const BY_NAME = Object.fromEntries(OFFICE_TOOLS.map((t) => [t.name, t]));

const STRIP = new Set([
	"actor", "binding_id", "bindingId", "generation", "instance", "trusted_ctx", "trustedCtx",
	"channel_lifetime_ref", "channelLifetimeRef", "host_session_ref", "hostSessionRef", "session_token",
	"endpoint", "socket", "socket_path", "socketPath",
	"act_key", "now_ts", "created_at", "acted_at", "timestamp",
	"expected_version", "change_set", "digest", "sha256", "grants", "grant_ref", "grant_version",
]);

const BUSY_RETRIES = 3;
const BUSY_BACKOFF_MS = 200;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ★ W229（审计 6d563dd #6）：同一笔业务请求的身份不能因为**字段顺序**不同就变。
//   JSON.stringify 按插入顺序输出，而对象顺序是模型/宿主随手给的：同一封信换个键序就得到另一个
//   act_key、另一个 letter id，重试于是变成第二封。这里**递归按对象键排序**再摘要；
//   数组顺序有语义（cc 列表、references 列表），保持原样不动。
//   注意：这不改变“主动再发一次同内容仍是新动作”——只有一次未决（outcome_unknown）的重试才复用键。
function canonical(v) {
	if (Array.isArray(v)) return v.map(canonical);
	if (v && typeof v === "object") {
		const out = {};
		for (const k of Object.keys(v).sort()) out[k] = canonical(v[k]);
		return out;
	}
	return v;
}

function digest(v) {
	return createHash("sha256").update(JSON.stringify(canonical(v))).digest("hex").slice(0, 16);
}

function normalizeDescriptor(d) {
	if (!d || typeof d !== "object") return undefined;
	return {
		actor: d.actor,
		// ★ W436/v9（A 2026-11-16）：`binding_id`/`socket_path`/`token_file` 三个下划线**旧别名已删**。
		//   现行写入器（`officed._write_descriptor`，候选 252dc36a 里 `office/officed.py:248`）逐字写：
		//   socketPath / tokenFile / actor / bindingId / generation / instance / channel_lifetime_ref /
		//   exportedAt / note（+ 可选 source）。所以只有 `channel_lifetime_ref` 这一处是下划线 —— 保留；
		//   其余再接受旧拼法，等于替旧写入器保兼容。
		bindingId: d.bindingId,
		generation: d.generation,
		instance: d.instance,
		socketPath: d.socketPath,
		tokenFile: d.tokenFile,
		channelLifetimeRef: d.channelLifetimeRef ?? d.channel_lifetime_ref,
	};
}

function trustedFrom(endpoint) {
	const d = normalizeDescriptor(endpoint);
	if (!d || !d.actor || !d.bindingId || !Number.isInteger(d.generation) || !d.instance) return undefined;
	const ctx = { actor: d.actor, binding_id: d.bindingId, generation: d.generation, instance: d.instance };
	if (d.channelLifetimeRef) ctx.channel_lifetime_ref = d.channelLifetimeRef;
	return ctx;
}

/**
 * ★ W390：**参数校验只在这里，规则只有 spec 一份**。
 *
 * 每个工具在自己的 `parameters` 里已经声明了字段与 `required`（还有 `additionalProperties:false`），
 * 但以前执行层只是**把不认识的字段悄悄丢掉**再发给 Core：
 * 现场（11-13 08:32 rev_director）把审核意见写进 `comment`（正名是 `reason`），
 * 工具回了 ok、Core 里却没有这条意见 —— 静默吞掉比报错难查得多。
 *
 * 这里按 spec 判两件事，都在**发往 Core 之前**：
 *   · 出现未声明的字段 ⇒ 拒绝，点名这个字段并列出该工具允许的字段；
 *   · 缺少 required ⇒ 同样拒绝并指名。
 * 不加别名（`comment` 不会被当成 `reason`），也不在单个工具里写特例。
 *
 * 返回 null 表示通过；否则返回可直接交给模型的错误结果。
 */
// Execute the structural keywords used by the catalog on the same arguments we forward.
function invalidField(schema, value, path) {
	if (schema.anyOf && !schema.anyOf.some(item => !invalidField(item, value, path))) return path;
	if (schema.enum && !schema.enum.includes(value)) return path;
	const type = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
	if (schema.type === "integer" ? !Number.isSafeInteger(value) : schema.type && schema.type !== type) return path;
	switch (type) {
	case "object":
		for (const key of schema.required ?? [])
			if (!Object.hasOwn(value, key)) return `${path}.${key}`;
		for (const [key, child] of Object.entries(value)) {
			const field = `${path}.${key}`;
			if (Object.hasOwn(schema.properties ?? {}, key)) {
				const invalid = invalidField(schema.properties[key], child, field);
				if (invalid) return invalid;
			} else if (schema.additionalProperties === false) return field;
		}
		return null;
	case "array":
		if (value.length < (schema.minItems ?? 0)
			|| value.length > (schema.maxItems ?? Infinity)) return path;
		for (let i = 0; schema.items && i < value.length; i++) {
			const invalid = invalidField(schema.items, value[i], `${path}[${i}]`);
			if (invalid) return invalid;
		}
		return null;
	case "number":
		return Number.isFinite(value)
			&& value >= (schema.minimum ?? -Infinity) && value <= (schema.maximum ?? Infinity) ? null : path;
	default:
		return null;
	}
}

export function validateArgs(tool, args = {}) {
	const spec = tool.parameters ?? {};
	const props = spec.properties ?? {};
	const allowed = Object.keys(props);
	const required = tool.op === "letter.send" && args?.intent == null
		? spec.required.map(k => k === "intent" ? "need" : k) : spec.required ?? [];
	const given = Object.keys(args ?? {});
	const unknown = given.filter((k) => !Object.hasOwn(props, k));
	const missing = required.filter((k) => {
		const v = args?.[k];
		return v === undefined || v === null || (typeof v === "string" && v.trim() === "");
	});
	const invalid = !unknown.length && !missing.length
		? invalidField({ ...spec, required }, args, "arguments") : null;
	if (!unknown.length && !missing.length && !invalid) return null;
	const error = unknown.length ? "unknown_arguments" : missing.length ? "missing_required_arguments" : "invalid_arguments";
	const V = TOOLS_TEXT.validate;
	return {
		ok: false,
		error,
		unknown: unknown.length ? unknown : undefined,
		missing: missing.length ? missing : undefined,
		allowed,
		// The sentences come from `office_tools_text.json` (`validate`); this function only fills
		// in the lists, because which list goes where is calling-path knowledge, not wording.
		message_zh: fillText(V.zh, {
			tool: tool.name,
			problem: invalid ? `字段 ${invalid} 不符合工具定义` :
				unknown.length ? `不认识的字段 ${unknown.join("、")}` : `缺少必填字段 ${missing.join("、")}`,
			allowed: `${allowed.join("、") || "（无）"}${required.length ? `；必填：${required.join("、")}` : ""}`,
		}),
		message_en: fillText(V.en, {
			tool: tool.name,
			problem: invalid ? `field ${invalid} does not match the tool schema` :
				unknown.length ? `unknown field(s) ${unknown.join(", ")}` : `missing required field(s) ${missing.join(", ")}`,
			allowed: `${allowed.join(", ") || "(none)"}${required.length ? `; required: ${required.join(", ")}` : ""}`,
		}),
	};
}

/** Wire body for one tool call. Model fields are business fields only. */
export function buildBody(tool, args) {
	const allowed = new Set(Object.keys(tool.parameters.properties ?? {}));
	const body = {};
	for (const [k, v] of Object.entries(args ?? {})) {
		if (k.startsWith("_")) continue;
		if (!allowed.has(k)) continue;          // the schema is the contract; extras never ride along
		// identity / bookkeeping fields never come from the model -- unless the tool's own schema names it as
		// its business field (only office_grant_revoke: grant_ref is the thing it acts on)
		if (STRIP.has(k) && !tool.business_fields?.includes(k)) continue;
		if (v === null || v === undefined) continue;
		body[k] = v;
	}
	// The tool field is `reviewer`; Core's parameter is `review`. It is the same value -- a mailbox or
	// "none" -- and only the name differs, so this is a rename and nothing else.
	//
	// It used to be rewritten into `{mode, reviewer}`, which is the shape `work_business` *returns*.
	// As an input that dict reached `_review_mode()` and died in the INSERT ("type 'dict' is not
	// supported"), so the call could never succeed: the wire shape of a result is not its input.
	if (typeof body.reviewer === "string") {
		body.review = body.reviewer;
		delete body.reviewer;
	}
	// ★ W317：改信/撤回的**目标收件人**在工具面叫 `to`（业务对象），线上仍是既有 `box` 字段。
	//   只做字段名映射：请求体不出现第二个名字，业务判据（发信人本人 / 主送项 / 未受理 / 版本）
	//   全在 Core 与 endpoint 的投递仲裁里。
	if ((tool.op === "letter.revise" || tool.op === "letter.retract") && typeof body.to === "string") {
		body.box = body.to;
		delete body.to;
	}
	body.op = tool.op;
	return body;
}

// ★ W229 R3（R3-W229.1 第 2 条）：结果未知时的续接回执。
//   作用域只有普通发信/回信这两条既有工具：结果未知时把**这次**操作的身份（actor/op/业务参数摘要/
//   act_key/签发时间）编成一个不透明回执交给模型；续接时原样带回来 ⇒ 适配器还原成**同一个**
//   act_key ⇒ Core 的 (actor, op, act_key) 幂等域回放首次结果、端点按它派生同一个 letter id。
//   它不是权限凭证：身份仍来自受控 ctx，回执里的字段逐项与本次调用比对，不一致就明确拒绝，
//   既不猜新动作也不按正文去重；新发一封不带回执 ⇒ 照常铸新动作（内容相同也是两封）。
const RETRY_REF_PREFIX = "r1.";
const RETRY_OPS = new Set(["letter.send"]);
// ★ W229 R3.2（R3-W229.2 第 ② 条）：**不设客户端时间门**。回执里带 `t` 只是给人看，
//   不参与判定：多久之后能续接，取决于 Core 里那条动作事实还在不在（Core 不删 actions，
//   所以同键重放就一直回放首次结果）。过期后要求"当新信重发"反而会制造重复。

function makeRetryRef({ actor, op, paramsDigest, actKey, at }) {
	const payload = { v: 1, a: actor, o: op, d: paramsDigest, k: actKey, t: at };
	return RETRY_REF_PREFIX + Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
}

function parseRetryRef(ref) {
	if (typeof ref !== "string" || !ref.startsWith(RETRY_REF_PREFIX)) return null;
	try {
		const o = JSON.parse(Buffer.from(ref.slice(RETRY_REF_PREFIX.length), "base64url").toString("utf8"));
		if (!o || o.v !== 1 || typeof o.a !== "string" || typeof o.o !== "string"
			|| typeof o.d !== "string" || typeof o.k !== "string" || !Number.isFinite(o.t)) return null;
		return o;
	} catch {
		return null;
	}
}

const MSG_TEXT = TOOLS_TEXT.messages;
//: One template, one fill. The fill is this module's own calling-path knowledge; the sentence is not.
const fillText = (tpl, values) =>
	tpl.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
const fill = (key, values) => {
	const [zh, en] = [MSG_TEXT[key].zh, MSG_TEXT[key].en];
	const render = (tpl) => tpl.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
	return [render(zh), render(en)];
};

const MSG = {
	no_endpoint: () => fill("no_endpoint", {}),
	unknown_tool: (n) => fill("unknown_tool", { name: n }),
	retry_ref_invalid: () => fill("retry_ref_invalid", {}),
	retry_ref_mismatch: (why) => fill("retry_ref_mismatch", { why }),
	retry_hint: () => fill("retry_hint", {}),
	outcome_unknown_letter: () => fill("outcome_unknown_letter", {}),
	outcome_unknown_write: () => fill("outcome_unknown_write", {}),
};

function publicTodo(todo) {
	const items = todo.items.slice(0, 20).map(item => {
		const chars = Array.from(item.objective || "");
		return chars.length > 80 ? { ...item, objective: chars.slice(0, 80).join("") + "…" } : item;
	});
	return { ...todo, ...(todo.items.length > items.length ? { omitted: todo.items.length - items.length } : {}), items };
}

export function createOfficeTools({ endpoint, core, now = () => Date.now(), locale = () => DEFAULT_LOCALE } = {}) {
	const trusted = trustedFrom(endpoint);
	const say = (key, ...a) => MSG[key](...a)[normLocale(locale()) === "en" ? 1 : 0];
	let seq = 0;
	// work_ref -> latest observed version; session-local and lost on restart.
	const versions = new Map();
	// ★ W229 R3.3（R3-W229.3）：**非信件**写动作保留原来的同进程续接 —— 结果未知时同一份业务参数
	//   再调一次复用同一个 act_key（Core 的 (actor, op, act_key) 幂等域会回放首次结果，不会建第二笔）。
	//   成功一次之后这条记录删掉，所以"做完了再调一次"仍是新动作；进程重启后认不出（如实说）。
	//   未决项保留原始请求；发信仍只通过 retry_ref 续接。
	const unresolved = new Map();
	INSTANCE_SEQ += 1;
	const instanceTag = `${PROC_NONCE}${INSTANCE_SEQ.toString(36)}`;

	function mintKey(op) {
		seq += 1;
		return `${trusted.actor}:${op}:${now().toString(36)}:${seq}:${instanceTag}`;
	}

	function observe(result) {
		if (result?.work_ref && Number.isInteger(result.version) && result.version >= (versions.get(result.work_ref) ?? -Infinity))
			versions.set(result.work_ref, result.version);
	}

	async function send(body) {
		// `sent: false` is a fact, not a guess: no frame was written, so the caller's exit code can say
		// "unavailable" instead of "refused", and nobody re-sends a letter that never existed.
		if (!trusted) return { ok: false, error: "no_controlled_endpoint", message: say("no_endpoint"), sent: false };
		if (!core || typeof core.handle !== "function") return { ok: false, error: "no_core_handler", sent: false };
		try {
			return await core.handle(body, trusted);
		} catch (e) {
			return { ok: false, error: e?.code ?? "core_error", message: e?.message ?? String(e), detail: e?.detail };
		}
	}

	async function callTool(name, args) {
		const tool = BY_NAME[name];
		if (!tool) return { ok: false, error: "unknown_tool", message: say("unknown_tool", name), sent: false };
		// ★ W390：唯一共享执行入口，参数按 spec 校验（不认识的字段 / 缺 required 都拒绝，不发往 Core）。
		const invalid = validateArgs(tool, args);
		if (invalid) {
			return {
				ok: false,
				error: invalid.error,
				unknown: invalid.unknown,
				missing: invalid.missing,
				allowed: invalid.allowed,
				message: normLocale(locale()) === "en" ? invalid.message_en : invalid.message_zh,
				sent: false,
			};
		}
		let body = buildBody(tool, args);
		if (name === "office_letter_list") body.limit ??= 50;
		if (name === "office_letter_read") body.accept_on_read = true;
		if (tool.write) {
			// ★ W229 R3：回执只在适配器里解释，绝不作为业务字段上线路。
			const retryArg = typeof body.retry_ref === "string" ? body.retry_ref.trim() : "";
			delete body.retry_ref;
			const usesReceipt = RETRY_OPS.has(tool.op), k = digest([name, body]);
			const pending = !usesReceipt && unresolved.get(k);
			if (pending) body = pending;
			else if (body.expected_version === undefined && tool.op !== "work.review" && versions.has(body.work_ref))
				body.expected_version = versions.get(body.work_ref);
			else if (trusted && body.expected_version === undefined && (tool.op === "work.update" || tool.op === "work.publish"))
				return {ok:false, error:"work_version_required", sent:false, message:normLocale(locale()) === "en"
					? "Read this Work in this session or supply expected_version from a prior read."
					: "先在当前会话读这件 Work，或提供此前读到的 expected_version。"};
			let actKey;
			if (retryArg) {
				// 续接：回执必须与**这次身份、这个动作、这份业务参数**一致（没有时间门）；
				// 任何一项不符就明确拒绝（不猜、不降级成新动作）。
				const parsed = parseRetryRef(retryArg);
				if (!parsed) return { ok: false, error: "invalid_retry_ref", message: say("retry_ref_invalid"), sent: false };
				if (parsed.a !== trusted?.actor) {
					return { ok: false, error: "retry_ref_actor_mismatch", message: say("retry_ref_mismatch", "actor"), sent: false };
				}
				if (parsed.o !== tool.op) {
					return { ok: false, error: "retry_ref_op_mismatch", message: say("retry_ref_mismatch", "op"), sent: false };
				}
				if (parsed.d !== k) {
					return { ok: false, error: "retry_ref_payload_mismatch", message: say("retry_ref_mismatch", "payload"), sent: false };
				}
				actKey = parsed.k;      // 回执自描述、无签名：它不是凭证/授权，只用来沿用原操作键；
				                        // 真正拦住越界的是上面三项一致性 + Core 自己的身份与冲突保护。
			} else if (usesReceipt) {
				actKey = mintKey(tool.op);      // 信件：不带回执就是新的一封（不替用户判"是不是同一次"）
			} else {
				actKey = pending?.act_key ?? mintKey(tool.op);
			}
			body.act_key = actKey;
			const sentBody = pending || (!usesReceipt && structuredClone(body));
			if (sentBody) body = sentBody;
			let res;
			for (let i = 0; ; i++) {
				res = await send(body);
				if (!(res && res.ok === false && res.error === "busy" && i < BUSY_RETRIES)) break;
				await sleep(BUSY_BACKOFF_MS * (i + 1));
			}
			const retryLostAuthority = (pending || retryArg) && res?.ok === false && res.error === "unauthorized_attempt";
			if (outcomeOf(res) === OUTCOMES.unknown || retryLostAuthority) {
				if (!usesReceipt) unresolved.set(k, sentBody);
				const out = { ...res, error: "outcome_unknown",
					message: say(usesReceipt ? "outcome_unknown_letter" : "outcome_unknown_write") };
				if (usesReceipt && trusted?.actor) {
					// 给**同一次**操作的续接回执；模型主动新发（不带它）不受影响。
					out.retry_ref = makeRetryRef({ actor: trusted.actor, op: tool.op, paramsDigest: k,
						actKey: body.act_key, at: now() });
					out.retry_hint = say("retry_hint");
				}
				return out;
			}
			// Busy and unsent retries do not resolve an earlier unknown write.
			if (!pending || res?.ok !== false || (res.error !== "busy" && res.sent !== false)) unresolved.delete(k);
			if (res?.ok !== false) observe(res);
			return res;
		}
		let res = await send(body);
		for (let i = 0; res && res.ok === false && res.error === "busy" && i < BUSY_RETRIES; i++) {
			await sleep(BUSY_BACKOFF_MS * (i + 1));
			res = await send(body);
		}
		if (tool.op === "work.get" && res && res.ok !== false) observe(res);
		if (tool.op === "work.list" && res?.ok !== false && Array.isArray(res?.items))
			for (const item of res.items) observe(item);
		if (tool.op === "work.runnable" && res?.ok !== false && Array.isArray(res?.items)) res = publicTodo(res);
		if (tool.op === "work.get" && res?.ok !== false && Array.isArray(res?.todo?.items)) res = { ...res, todo: publicTodo(res.todo) };
		return res;
	}

	return { callTool };
}
