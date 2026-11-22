// What the model reads for one Office tool result (01_DESIGN §5.4). Presentation only: the machine code
// decides the guidance; the human message from Core is shown as-is; nothing is re-derived from text.
// One language per session. The one normalisation lives in `office_tool_spec.mjs`: an unresolved
// language is the company default (`DEFAULT_LOCALE`), never a hard-coded Chinese fallback.
import { DEFAULT_LOCALE, normLocale } from "./office_tool_spec.mjs";
// The refusal sentences for both languages live beside this module, keyed by the stable code.
// Which sentence is *decided* here; only where the words live moved. Same load-time import as
// `office_tool_text.json`: no loader, no fs read.
import RESULT_TEXT from "./office_result_text.json" with { type: "json" };

// ★ W229 R3 / W436：`outcome_unknown` 不在表里（唯一产出方 `office_tools.callTool` 自带按 op 分开的
//   message）；`view_required` / `view_ambiguous` 的话由 `office_tools.mjs::say()` 给。空白码不是覆盖。
const GUIDE = Object.fromEntries(
	Object.entries(RESULT_TEXT.guide).map(([k, e]) => [k, [e.zh, e.en]]));
;

/** ★ W194：`no_controlled_endpoint` 的**可证实原因** → 一句话（与宿主无关的口径；宿主只给状态码）。
 *  one language per session. */
const NO_ENDPOINT_STATE = Object.fromEntries(
	Object.entries(RESULT_TEXT.no_endpoint_state).map(([k, e]) => [k, [e.zh, e.en]]));
;
export const NO_ENDPOINT_STATES = Object.keys(NO_ENDPOINT_STATE);

/**
 * One refusal code -> one sentence in the session's language, with the adapter's detail appended.
 *
 * Adapters use the same language normalisation and fallback for missing text.
 *
 * @param {Record<string, () => [string, string]>} table code -> [zh, en]
 * @param {string} reason stable code; an unknown one falls back to `detail`, else to the code itself
 * @param {string} locale the session's one language (mailbox > company > default, resolved by the caller)
 * @param {*} [detail] short, language-neutral fact (e.g. a mailbox name)
 */
export function sayReason(table, reason, locale, detail) {
	const entry = table?.[reason];
	if (!entry) return detail ? String(detail) : String(reason);
	const en = normLocale(locale) === "en";
	const text = entry()[en ? 1 : 0];
	if (!detail) return text;
	return `${text}${en ? ` (${detail})` : `（${detail}）`}`;
}

// Fields the model never acts on: the adapter's concurrency baseline (view, the integer Work version), digests,
// binding/source bookkeeping and the navigation text already injected as the Office section.
// ★ R3-W181.1: letter ids and timestamps are NOT bookkeeping. The old list also hid
// `letter_id`, `created_at` and `recorded_at`; with the one-JSON rule that made letter.send's
// exact id and work_get's timestamps disappear from the only copy. They stay: business ids and
// times the employee has to act on, on every host (Pi/OC/MCP share this renderer).
const INTERNAL = new Set(["ok", "view", "sub_digest", "binding",
	"sources", "navigation", "epoch_at_submit", "body_sha256"]);
const QUIET_FALSE = new Set(["has_more", "truncated", "waiting"]);
// an empty list that answers a question ("nothing", "no role here", "nothing you can do") stays visible
const KEEP_EMPTY = new Set(["items", "your_roles", "available_actions", "work_participants"]);

/** The success payload as the model reads it: same keys and values, minus internal and empty fields.
 *
 *  One argument: the three filters below are decided **by key** (`INTERNAL` / `QUIET_FALSE` /
 *  `KEEP_EMPTY`), and each one is applied where the key is in hand, inside this loop. There used to
 *  be a second parameter `key` that every recursive call passed and nothing ever read -- the third
 *  time a signature in this wave claimed something the body did not do (after the daemon client's
 *  and `runEntry`'s `timeoutMs`, and `writeActivity`'s `sessionID`).
 */
export function compact(v, knowledge = false) {
	if (Array.isArray(v)) return v.map((x) => compact(x, knowledge)).filter((x) => x !== undefined);
	if (v && typeof v === "object") {
		knowledge ||= typeof v.position === "string" && (Array.isArray(v.entries) && Array.isArray(v.snapshots)
			|| ["add", "replace", "remove", "restore"].includes(v.op) && "entry_version" in v);
		const out = {};
		for (const [k, x] of Object.entries(v)) {
			if (INTERNAL.has(k) || (k === "acted_at_ts" && v.acted_at)) continue;
			if (k === "version" && Number.isInteger(x) && !knowledge) continue;   // Work CAS version
			if (x === false && QUIET_FALSE.has(k)) continue;
			const c = compact(x, knowledge);
			if (c === undefined) continue;
			const rosterPositions = k === "positions" && Array.isArray(v.work_participants);
			if (!KEEP_EMPTY.has(k) && !rosterPositions
				&& ((Array.isArray(c) && !c.length) || (isPlain(c) && !Object.keys(c).length))) continue;
			out[k] = c;
		}
		return out;
	}
	if (v === null || v === undefined || v === "") return undefined;
	return v;
}
const isPlain = (v) => !!v && typeof v === "object" && !Array.isArray(v);

export function renderToolResult(result, locale = DEFAULT_LOCALE) {
	const idx = normLocale(locale) === "en" ? 1 : 0;
	if (result && result.ok === false) {
		const code = result.error ?? "error";
		const head = idx ? `Not done: ${code}` : `未完成：${code}`;
		const lines = [head];
		// the wire message is "code: text"; the head line already names the code
		const msg = result.message ? String(result.message).replace(new RegExp(`^${code}: `), "") : "";
		if (msg) lines.push(msg);
		// 原因可证实时用具体那句（业务口径在本共享层，不在宿主里）；否则用兜底那句。
		const g = (code === "no_controlled_endpoint" && NO_ENDPOINT_STATE[result.state]?.[idx])
			|| GUIDE[code]?.[idx];
		if (g) lines.push(g);
		const d = result.detail;
		// detail values the message already names are not repeated
		const extra = d && typeof d === "object"
			? Object.fromEntries(Object.entries(d).filter(([k, x]) => k !== "reason_code" && !msg.includes(String(x))))
			: {};
		if (result.retry_ref) lines.push(`retry_ref: ${result.retry_ref}`);
		if (result.retry_hint) lines.push(String(result.retry_hint));
		if (Object.keys(extra).length) lines.push(JSON.stringify(extra));
		if (result.reason_code) lines.push(`reason_code=${result.reason_code}`);
		return { content: [{ type: "text", text: lines.join("\n") }], details: result, isError: true };
	}
	const shown = compact(result ?? null);
	// No indentation: the layout carries no information for the model and is paid on every call.
	// Field set and order are unchanged -- this is the one place the text shape is decided, and both
	// entries (CLI and MCP) print exactly this.
	return { content: [{ type: "text", text: JSON.stringify(shown ?? null) }], details: result };
}

/**
 * The three outcomes one call can end in, and the only place they are decided.
 *
 * The CLI, the MCP server and the hosts all need the same answer to "what happened", and the two
 * wrong answers are expensive in opposite directions: reading an unknown as a refusal means the
 * letter is sent again (a duplicate), reading it as success means it is never sent (a loss).
 *
 *   ok       — Core answered; `result` is the business payload.
 *   refused  — Core explicitly refused: `code` + `message` say why and what to do next.
 *   unknown  — the transport cannot say whether the write happened; `retry_ref` (when the op has
 *              one) is the only thing that may be carried back, unchanged.
 *
 * `sent === false` is the transport's own fact, not a judgement: it means no frame ever left this
 * process, so "refused because nothing was reachable" is provable and is NOT an empty result.
 */
export const OUTCOMES = { ok: "ok", refused: "refused", unknown: "unknown" };

//: Transport failures. WHICH state they belong to depends on `sent`: before a frame is written the
//: line simply was not reachable (nothing happened), after it is written the outcome is unknown.
const TRANSPORT_UNKNOWN = new Set(["socket_error", "socket_closed"]);
//: Refusals that are really "there is no line": nothing was sent because there was nowhere to send.
const UNREACHABLE = new Set(["no_controlled_endpoint", "no_socket_path", "no_core_handler"]);

/** Which of the three outcomes one result is. Unknown codes are refusals, never silently "ok". */
export function outcomeOf(result) {
	if (!result || result.ok !== false) return OUTCOMES.ok;
	const code = result.error ?? result.code ?? "";
	if (code === "outcome_unknown") return OUTCOMES.unknown;
	// A connect failure and a post-write failure are the same code but opposite facts: only the
	// second one can have created the letter, so only the second one is `unknown`.
	if (TRANSPORT_UNKNOWN.has(code)) return result.sent === false ? OUTCOMES.refused : OUTCOMES.unknown;
	return OUTCOMES.refused;
}

/**
 * Exit code for one result: 0 ok / 2 refused / 3 nothing was sent / 4 unknown.
 *
 * 3 and 2 are separate because "the line is not reachable" is not "the request was refused", and 4
 * forbids an automatic retry (the caller must re-send with the same act_key / retry_ref, or verify).
 */
export function exitCodeOf(result) {
	const state = outcomeOf(result);
	if (state === OUTCOMES.ok) return 0;
	if (state === OUTCOMES.unknown) return 4;
	const code = (result && (result.error ?? result.code)) ?? "";
	// 3 is "the line is not there", not "we refused something before sending it": a validation or
	// view refusal is also `sent: false`, and calling that "unavailable" would hide a real answer.
	if (result && result.sent === false && (UNREACHABLE.has(code) || TRANSPORT_UNKNOWN.has(code))) return 3;
	return 2;
}
