// Office 1.1 employee tools: ONE definition, consumed by every host adapter (Pi, OpenCode).
//
// Each entry: name, op (the controlled-socket op it calls), write (needs an idempotency key), the
// model-facing text in both languages, and a plain JSON-Schema `parameters` object. Hosts render one
// language per session (01_DESIGN §9: single language). Descriptions say what the tool does, the
// business fields it needs and what comes back -- nothing about grants, epochs, act keys or SQL.
//
// The model chooses targets, people, material, deliverables and conclusions. The system resolves
// identity, existing authority, snapshots, versions (CAS) and notifications.

import OFFICE_TOOL_TEXT from "./office_tool_text.json" with { type: "json" };

// The model-facing words live in `office_tool_text.json`, one entry per tool and per field. This
// module keeps what a host must act on -- name, op, type, required, additionalProperties, write and
// capability flags -- and reads the prose from that file at load time. A second copy in here would
// be the copy that drifts: the two languages of one field must move together or not at all.
const TEXT = OFFICE_TOOL_TEXT._tools;
const SHARED = OFFICE_TOOL_TEXT._shared;
const textOf = (key) => {
	const [tool, field] = key.split("/");
	const e = tool === "_shared" ? SHARED[field]
		: (TEXT[tool]?.[field] ?? TEXT[tool]?.parameters?.[field]);
	if (!e) throw new Error(`office_tool_text.json 缺少 ${key}`);
	return e;
};
//: A tool's label and description are a `[zh, en]` pair, so they share one reader with the fields.
const textPair = (key) => { const e = textOf(key); return [e.zh, e.en]; };
//: The same entry as an object's own `description_zh` / `description_en` pair (schema nodes).
const textFields = (key) => { const e = textOf(key); return { description_zh: e.zh, description_en: e.en }; };
const S = (key, extra = {}) => ({ type: "string", description_zh: textOf(key).zh, description_en: textOf(key).en, ...extra });
const I = (key, extra = {}) => ({ type: "integer", description_zh: textOf(key).zh, description_en: textOf(key).en, ...extra });
const B = (key) => ({ type: "boolean", description_zh: textOf(key).zh, description_en: textOf(key).en });

const WORK = S("_shared/WORK");
// Review is one plain field; the adapter turns it into the wire's {mode, reviewer}. "none" is the explicit
// no-review choice (never inferred from a missing reviewer).
const REVIEWER = S("_shared/REVIEWER");
// A list item that is either a plain locator string or an object with details.
const STR_OR_OBJ = { anyOf: [{ type: "string" }, { type: "object" }] };
const REFERENCE = { description_zh: textOf("_shared/REFERENCE").zh,
	description_en: textOf("_shared/REFERENCE").en };
const RELATION = {
	type: "object",
	description_zh: textOf("_shared/RELATION").zh,
	description_en: textOf("_shared/RELATION").en,
	properties: { work: S("_shared/RELATION_WORK"),
		relation: S("_shared/RELATION_KIND", { enum: ["belongs_to", "modifies", "waits_for", "based_on", "references"] }),
		note: S("_shared/RELATION_NOTE") },
	required: ["work"],
	additionalProperties: false,
};

const RESOURCE_TARGET = {
	target_kind: S("_shared/RESOURCE_TARGET_KIND", { enum: ["position", "work"] }),
	target_ref: S("_shared/RESOURCE_TARGET_REF"),
};
const RESOURCE_LINK = { ...RESOURCE_TARGET, resource_ref: S("_shared/RESOURCE_REF") };
const RESOURCE_TOOLS = [
	["register", {
		kind: S("_shared/RESOURCE_KIND", { enum: ["skill", "mcp", "material"] }),
		locator: S("_shared/RESOURCE_LOCATOR"), digest: S("_shared/RESOURCE_DIGEST"),
	}, ["kind", "locator"]],
	["assign", RESOURCE_LINK, Object.keys(RESOURCE_LINK)],
	["unassign", RESOURCE_LINK, Object.keys(RESOURCE_LINK)],
	["status", RESOURCE_TARGET, Object.keys(RESOURCE_TARGET)],
].map(([action, properties, required]) => {
	const name = `office_resource_${action}`;
	return {
		name, op: `resource.${action}`, write: action !== "status",
		business_fields: action === "register" ? ["digest"] : [],
		label: textPair(`${name}/label`), description: textPair(`${name}/description`),
		parameters: { type: "object", properties, required, additionalProperties: false },
	};
});

const REQUEST_REF = S("_shared/REQUEST_REF");
const QUOTES = { type: "array", items: { type: "object" }, ...textFields("_shared/REQUEST_QUOTES") };
const REQUEST_TOOLS = [
	["request.admin_set", { enabled: B("office_request_admin_set/enabled") }, ["enabled"]],
	["request.create", {
		kind: S("office_request_create/kind", { enum: ["grant", "holder", "revoke", "project", "deactivate", "retire", "unbind", "broadcast", "read_mail", "join"] }),
		target: S("office_request_create/target"), reason: S("office_request_create/reason"),
		position: S("_shared/APPOINTMENT_POSITION"), quotes: QUOTES,
		params: { type: "object", ...textFields("office_request_create/params") },
		create: { type: "object", ...textFields("office_request_create/create") },
	}, ["kind", "reason"]],
	["request.decide", {
		request_ref: REQUEST_REF, quotes: QUOTES, reason: S("office_request_decide/reason"),
		decision: S("office_request_decide/decision", { enum: ["approve", "refuse", "to_user"] }),
		expected_version: I("office_request_decide/expected_version"),
	}, ["request_ref", "decision", "expected_version"]],
	["request.withdraw", { request_ref: REQUEST_REF }, ["request_ref"]],
	["request.revoke", { request_ref: REQUEST_REF }, ["request_ref"]],
	["handover.return", { position: S("_shared/APPOINTMENT_POSITION") }, ["position"]],
	["identity.deactivate", {}, []],
	["position.retire", { position: S("_shared/APPOINTMENT_POSITION"),
		cancel_work: B("office_position_retire/cancel_work") }, ["position"]],
].map(([op, properties, required]) => {
	const name = `office_${op.replace(".", "_")}`;
	return { name, op, write: true, label: textPair(`${name}/label`), description: textPair(`${name}/description`),
		business_fields: op === "request.decide" ? ["expected_version"] : [],
		parameters: { type: "object", properties, required, additionalProperties: false } };
});

export const OFFICE_TOOLS = [
	...["revise", "retract"].map((action) => {
		const name = `office_letter_${action}`;
		return { name, op: `letter.${action}`, write: true,
			label: textPair(`${name}/label`), description: textPair(`${name}/description`),
			parameters: { type: "object", required: ["letter_id", "to", "content_version"], additionalProperties: false,
				properties: { letter_id: S("office_letter_read/letter_id"), to: S("office_letter_send/to"),
					content_version: I("office_letter_revise/content_version", { minimum: 1 }),
					...(action === "revise" ? { subject: S("office_letter_send/subject"),
						need: S("office_letter_send/need"), body: S("office_letter_send/body") } : {}) } } };
	}),
	{
		name: "office_request_list", op: "request.list", write: false,
		label: textPair("office_request_list/label"), description: textPair("office_request_list/description"),
		parameters: { type: "object", additionalProperties: false, properties: {
			state: S("office_request_list/state", { enum: ["all", "pending", "done", "refused", "withdrawn", "revoked"] }),
			limit: I("office_request_list/limit", { minimum: 1, maximum: 100 }),
			cursor: I("office_request_list/cursor", { minimum: 1 }),
		} },
	},
	...RESOURCE_TOOLS,
	...REQUEST_TOOLS,
	{
		name: "office_binding_revoke", op: "binding.revoke", write: true,
		label: textPair("office_binding_revoke/label"), description: textPair("office_binding_revoke/description"),
		parameters: { type: "object", additionalProperties: false, properties: {
			target: S("office_binding_revoke/target"),
			expect_generation: I("office_binding_revoke/expect_generation"),
		} },
	},
	{
		name: "office_coord_respond", op: "coord.respond", write: true,
		label: textPair("office_coord_respond/label"),
		description: textPair("office_coord_respond/description"),
		parameters: { type: "object", additionalProperties: false, required: ["item", "choice"],
			properties: {
				item: S("office_coord_respond/item"),
				choice: S("office_coord_respond/choice", { enum: ["handle", "delegate", "wait"] }),
				reason: S("office_coord_respond/reason"),
				deadline: I("office_coord_respond/deadline"),
			},
		},
	},
	...[["office_usage_read", "usage.read"], ["office_release_status", "release.status"]]
		.map(([name, op]) => ({
			name, op, write: false, label: textPair(`${name}/label`),
			description: textPair(`${name}/description`),
			parameters: { type: "object", additionalProperties: false,
				properties: { project_ref: S(`${name}/project_ref`), ...(op === "usage.read" ? { group_by: S(`${name}/group_by`, { enum: ["binding"] }) } : {}) } },
		})),
	{
		name: "office_whoami", op: "office.context", write: false,
		label: textPair("office_whoami/label"),
		description: textPair("office_whoami/description"),
		parameters: { type: "object", properties: {}, additionalProperties: false },
	},
	{
		name: "office_lookup", op: "office.lookup", write: false,
		label: textPair("office_lookup/label"), description: textPair("office_lookup/description"),
		parameters: { type: "object", additionalProperties: false,
			properties: { query: S("office_lookup/query"), project_ref: S("office_lookup/project_ref") } },
	},
	{
		name: "office_work_create", op: "work.create", write: true,
		label: textPair("office_work_create/label"),
		description: textPair("office_work_create/description"),
		parameters: {
			type: "object",
			properties: {
				project_ref: S("office_work_create/project_ref"),
				objective: S("office_work_create/objective"),
				assignee: S("office_work_create/assignee"),
				reviewer: REVIEWER,
				acceptance: S("office_work_create/acceptance"),
				references: { type: "array", items: STR_OR_OBJ, ...REFERENCE },
				relations: { type: "array", items: RELATION, ...textFields("office_work_create/relations") },
				scope: S("office_work_create/scope"),
			},
			required: ["project_ref", "objective", "assignee", "reviewer"],
			additionalProperties: false,
		},
	},
	{
		name: "office_work_update", op: "work.update", write: true,
		label: textPair("office_work_update/label"),
		description: textPair("office_work_update/description"),
		parameters: {
			type: "object",
			properties: {
				work_ref: WORK,
				action: S("office_work_update/action",
					{ enum: ["assign", "reviewer", "edit", "references", "wait", "continue", "cancel"] }),
				assignee: S("office_work_update/assignee"),
				scope: S("office_work_update/scope"),
				reviewer: S("office_work_update/reviewer"),
				objective: S("office_work_update/objective"),
				acceptance: S("office_work_update/acceptance"),
				add: { type: "array", items: STR_OR_OBJ, ...textFields("office_work_update/add") },
				remove: { type: "array", items: { type: "object" },
					...textFields("office_work_update/remove") },
				reason: S("office_work_update/reason"),
				on: S("office_work_update/on"),
				note: S("office_work_update/note"),
			},
			required: ["work_ref", "action"],
			additionalProperties: false,
		},
	},
	{
		name: "office_work_withdraw", op: "work.withdraw", write: true,
		label: textPair("office_work_withdraw/label"),
		description: textPair("office_work_withdraw/description"),
		parameters: {
			type: "object",
			properties: { work_ref: WORK, sub_ref: S("office_work_withdraw/sub_ref") },
			required: ["work_ref", "sub_ref"],
			additionalProperties: false,
		},
	},
	{
		name: "office_work_publish", op: "work.publish", write: true,
		label: textPair("office_work_publish/label"),
		description: textPair("office_work_publish/description"),
		parameters: {
			type: "object",
			properties: {
				work_ref: WORK,
				files: { type: "array", items: STR_OR_OBJ, ...textFields("office_work_publish/files") },
				note: S("office_work_publish/note"),
				replaces: S("office_work_publish/replaces"),
				final: B("office_work_publish/final"),
			},
			required: ["work_ref", "files"],
			additionalProperties: false,
		},
	},
	{
		name: "office_work_review", op: "work.review", write: true,
		label: textPair("office_work_review/label"),
		description: textPair("office_work_review/description"),
		parameters: {
			type: "object",
			properties: {
				work_ref: WORK,
				sub_ref: S("office_work_review/sub_ref"),
				decision: S("office_work_review/decision", { enum: ["accept", "return"] }),
				reason: S("office_work_review/reason"),
				evidence: { type: "array", items: { type: "object" }, ...textFields("office_work_review/evidence") },
			},
			required: ["work_ref", "sub_ref", "decision"],
			additionalProperties: false,
		},
	},
	{
		name: "office_position_knowledge_read", op: "knowledge.read", write: false,
		label: textPair("office_position_knowledge_read/label"),
		description: textPair("office_position_knowledge_read/description"),
		parameters: {
			type: "object",
			properties: {
				position: S("office_position_knowledge_read/position"),
				version: I("office_position_knowledge_read/version", { minimum: 1 }),
			},
			required: ["position"],
			additionalProperties: false,
		},
	},
	{
		// `expected_version` is this tool's business field (the CAS token the model read and sends back), so
		// the adapter's STRIP list must not drop it -- same protection as grant_ref above.
		name: "office_position_knowledge_update", op: "knowledge.update", write: true,
		business_fields: ["expected_version"],
		label: textPair("office_position_knowledge_update/label"),
		description: textPair("office_position_knowledge_update/description"),
		parameters: {
			type: "object",
			properties: {
				position: S("office_position_knowledge_update/position"),
				action: S("office_position_knowledge_update/action", { enum: ["add", "replace", "remove", "restore"] }),
				id: S("office_position_knowledge_update/id"),
				text: S("office_position_knowledge_update/text"),
				section: S("office_position_knowledge_update/section"),
				why: S("office_position_knowledge_update/why"),
				when: { type: "array", items: { type: "string" }, maxItems: 8,
					...textFields("office_position_knowledge_update/when") },
				expected_version: I("office_position_knowledge_update/expected_version", { minimum: 0 }),
				to_version: I("office_position_knowledge_update/to_version", { minimum: 1 }),
			},
			required: ["position", "action"],
			additionalProperties: false,
		},
	},
	{
		name: "office_project_create", op: "project.create", write: true,
		label: textPair("office_project_create/label"),
		description: textPair("office_project_create/description"),
		parameters: {
			type: "object",
			properties: {
				name: S("office_project_create/name"),
				// ★ W361：岗位负责人按**明确任职人（holder）**派生，不是"在岗候选"；也不要求候选非空。
				owner_ref: S("office_project_create/owner_ref"),
				workspace_refs: { type: "array", items: { type: "string" },
					...textFields("office_project_create/workspace_refs") },
				description: S("office_project_create/description"),
				visibility: S("office_project_create/visibility", { enum: ["shared", "private"] }),
			},
			required: ["name", "owner_ref", "workspace_refs"],
			additionalProperties: false,
		},
	},
	{
		name: "office_grant_create", op: "grant.create", write: true,
		label: textPair("office_grant_create/label"),
		description: textPair("office_grant_create/description"),
		parameters: {
			type: "object",
			properties: {
				subject_ref: S("office_grant_create/subject_ref"),
				scope: { type: "array", items: { type: "string" }, ...textFields("office_grant_create/scope") },
				actions: { type: "array", items: { type: "string" }, ...textFields("office_grant_create/actions") },
				project_ref: S("office_grant_create/project_ref"),
				expires_at: S("office_grant_create/expires_at"),
			},
			required: ["subject_ref", "scope", "actions"],
			additionalProperties: false,
		},
	},
	{
		name: "office_grant_revoke", op: "grant.revoke", write: true,
		business_fields: ["grant_ref"],   // see office_grant_show: the tool acts on this ref
		label: textPair("office_grant_revoke/label"),
		description: textPair("office_grant_revoke/description"),
		parameters: {
			type: "object",
			properties: { grant_ref: S("office_grant_revoke/grant_ref") },
			required: ["grant_ref"],
			additionalProperties: false,
		},
	},
	{
		name: "office_work_list", op: "work.list", write: false,
		label: textPair("office_work_list/label"),
		description: textPair("office_work_list/description"),
		parameters: {
			type: "object",
			properties: {
				role: S("office_work_list/role", { enum: ["assigned", "review", "managed", "all"] }),
				project_ref: S("office_work_list/project_ref"),
				top_level: B("office_work_list/top_level"),
				state: S("office_work_list/state", { enum: ["Active", "Submitted", "Done", "Cancelled"] }),
				cursor: S("office_work_list/cursor"),
				limit: I("office_work_list/limit",
					{ minimum: 1, maximum: 200 }),
			},
			additionalProperties: false,
		},
	},
	{
		name: "office_work_get", op: "work.get", write: false,
		label: textPair("office_work_get/label"),
		description: textPair("office_work_get/description"),
		parameters: {
			type: "object",
			properties: {
				work_ref: WORK,
				history: B("office_work_get/history"),
				todo: B("office_work_get/todo"),
			},
			required: ["work_ref"],
			additionalProperties: false,
		},
	},
	{
		name: "office_work_runnable", op: "work.runnable", write: false,
		label: textPair("office_work_runnable/label"),
		description: textPair("office_work_runnable/description"),
		parameters: {
			type: "object",
			properties: {
				unit: S("office_work_runnable/unit"),
			},
			additionalProperties: false,
		},
	},
	{
		name: "office_work_read", op: "work.read", write: false,
		label: textPair("office_work_read/label"),
		description: textPair("office_work_read/description"),
		parameters: {
			type: "object",
			properties: {
				work_ref: S("office_work_read/work_ref"),
				sub_ref: S("office_work_read/sub_ref"),
				path: S("office_work_read/path"),
				// Same shape and same defaults as r/artifact_read's own check_view: cursor is the
				// line to continue from, limit the page size — both optional integers >= 1, so a
				// call without them is byte-for-byte what it always was. One op, one view, no
				// second page protocol.
				cursor: I("office_work_read/cursor"),
				limit: I("office_work_read/limit"),
			},
			required: [],
			additionalProperties: false,
		},
	},

	{
		// ★ W111（#12 P0-2）：**普通员工发信的工具化入口**。
		//
		//   现状（W110 核实）：普通信的底层写路径早就是唯一的 `delivery/letters.py::write_letter`，
		//   但普通员工的**入口**是导航里那句 shell 教学
		//     printf '正文' | postoffice send <对方> <自己> "<事由>" "<需要>"
		//   —— 模型要自己学参数序、背信箱名、回信还要把 `--reply-to` 的编号抄全。
		//   而同一层级的"干活"（Work）有整套结构化工具：两套范式。
		//
		//   这两个工具**零新业务逻辑**：只把字段映射到 `write_letter`（与 CLI / 页面相同），
		//   权限/批窗/抄送的判据一条都不在这里写（★ W383：紧急已取消）。
		name: "office_letter_send", op: "letter.send", write: true,
		label: textPair("office_letter_send/label"),
		description: textPair("office_letter_send/description"),
		parameters: {
			type: "object",
			properties: {
				to: S("office_letter_send/to"),
				subject: S("office_letter_send/subject"),
				need: S("office_letter_send/need"),
				body: S("office_letter_send/body"),
				reply_to: S("office_letter_send/reply_to"),
				keep_open: B("office_letter_send/keep_open"),
				// ★ W116：抄送 —— 与 CLI `send --cc` **同一份实现**，不是新机制。抄送副本只可见
				//   可归档、**不唤醒**，也不承担回复义务。
				cc: {
					type: "array", items: { type: "string" },
					...textFields("office_letter_send/cc"),
				},
				// ★ W229 R3：结果未知时的续接回执。只有上一回答里给出的那个值可以原样带回来；
				//   它不是权限凭证、不改变身份，只是让适配器把这**同一次**发信还原成同一个
				//   act_key（Core 与端点据它回放/同一封信）。新发一封就不填。
				retry_ref: S("office_letter_send/retry_ref"),
			},
			required: ["subject", "need", "body"],
			additionalProperties: false,
		},
	},
	{
		name: "office_letter_list", op: "letter.read", write: false,
		label: textPair("office_letter_list/label"),
		description: textPair("office_letter_list/description"),
		parameters: {
			type: "object",
			properties: {
				include_archived: B("office_letter_list/include_archived"),
			},
			additionalProperties: false,
		},
	},
	{
		name: "office_letter_read", op: "letter.read", write: false,
		label: textPair("office_letter_read/label"),
		description: textPair("office_letter_read/description"),
		parameters: {
			type: "object",
			properties: {
				letter_id: S("office_letter_read/letter_id"),
				target: S("office_letter_read/target"),
				include_archived: B("office_letter_read/include_archived"),
				cursor: I("office_letter_read/cursor"),
				limit: I("office_letter_read/limit"),
			},
			required: ["letter_id"],
			additionalProperties: false,
		},
	},
	{
		name: "office_letter_archive", op: "letter.archive", write: true,
		label: textPair("office_letter_archive/label"),
		description: textPair("office_letter_archive/description"),
		parameters: {
			type: "object",
			properties: {
				letter_id: S("office_letter_archive/letter_id"),
				unit: S("office_letter_archive/unit"),
			},
			required: ["letter_id"],
			additionalProperties: false,
		},
	},
	{
		// `grant_ref` is this tool's business object, not a bookkeeping name: without this entry the
		// adapter's STRIP list would drop it before the wire (the same protection office_grant_revoke
		// needs). One comment, both tools, one rule.
		name: "office_grant_show", op: "grant.show", write: false,
		business_fields: ["grant_ref"],
		label: textPair("office_grant_show/label"),
		description: textPair("office_grant_show/description"),
		parameters: {
			type: "object",
			properties: {
				grant_ref: S("office_grant_show/grant_ref"),
			},
			required: ["grant_ref"],
			additionalProperties: false,
		},
	},
	{
		// 修复批 2 D：授权列表。只读；"能看全部还是只看与自己相关的"由 Core 判（visible 字段说清）。
		name: "office_grant_list", op: "grant.list", write: false,
		label: textPair("office_grant_list/label"),
		description: textPair("office_grant_list/description"),
		parameters: {
			type: "object",
			properties: {
				project_ref: S("office_grant_list/project_ref"),
				subject_ref: S("office_grant_list/subject_ref"),
				include_revoked: B("office_grant_list/include_revoked"),
			},
			additionalProperties: false,
		},
	},
];

export const TOOL_NAMES = OFFICE_TOOLS.map((t) => t.name);

/** 这个工具在给定能力下要不要摆到桌面上（修复批 2 C）。
 *
 * `requires` 是 Core 的动作名（或 `governance`）。判据只有这一处：Pi 与 OC2 都调 `toolSpecs`。
 * **默认（没给能力）一律不给管理类工具** —— 会话启动时读不到"能不能"就不猜，宁可少给一个入口，
 * 也不给普通员工多一扇门。真正的授权永远在 Core 的动作事务里复核，这里只管工具表长短。
 */
export function toolAllowed(tool, capabilities) {
	const need = tool.requires;
	if (!need) return true;
	const c = capabilities || {};
	if (c[need] === true) return true;
	return (c.actions || {})[need] === true;
}

/** Localized, host-neutral JSON Schema (description_* collapsed to one `description`). */
function localizeSchema(node, idx) {
	if (Array.isArray(node)) return node.map((n) => localizeSchema(n, idx));
	if (!node || typeof node !== "object") return node;
	const out = {};
	for (const [k, v] of Object.entries(node)) {
		if (k === "description_zh" || k === "description_en") continue;
		out[k] = localizeSchema(v, idx);
	}
	const d = idx === 1 ? node.description_en : node.description_zh;
	if (d) out.description = d;
	return out;
}

//: ★ W112：**语言的默认值只有一个定义处** —— 与 `office_core.DEFAULT_LOCALE` 同值。
//
//  为什么这里也要有：适配层在 `office.context` 回来之前要先把工具表注册出来（Pi 的
//  `session_start` / OC2 的 `setup` 都是同步点），所以必须有一个"首帧语言"。以前这个首帧
//  取自环境变量 `OFFICE_LOCALE`（生产里没人设 ⇒ 恒为 zh ⇒ **首轮工具描述是中文**，
//  第二轮才被 `office.context.locale` 纠正）。用户 20261104 澄清：未设置默认英文。
//
//  纪律：这个常量**只用于首帧**。任何"以它为准"的判断都不许有 —— 真值一律来自
//  `office.context.locale`（`office_core.resolve_locale`：箱 > 公司 > DEFAULT_LOCALE）。
//  `test/office_locale_test.mjs` 与 `tests/fix_w112_locale_single_source_test.py` 钉住这一条。
export const DEFAULT_LOCALE = "en";

/** The one language normalisation, the same rule `office_core.norm_locale` applies.
 *
 *  Callers still resolve the real value (mailbox > company > default) before asking; this is the
 *  normalisation those resolutions end with, not a second resolution.
 */
export function isLocale(loc) {
	return loc === "zh" || loc === "en";
}

export function normLocale(loc) {
	return isLocale(loc) ? loc : DEFAULT_LOCALE;
}

//: ★ W113：系统提示里那一段 Office 导航的**单处定义**（两个宿主适配器共用）。
//
//  以前只有 Pi 适配器（ext/office-tools.ts）自己写了 `const SECTION = "office_navigation"`，
//  OC2 那边完全没有这段 —— 同一个员工在 Pi 里读得到"你是谁、什么岗位、待办、怎么沟通"，
//  在 OC 里读不到。本票把章节名与"过期"补充语搬到共享 spec：**一处定义**，两个适配器引用；
//  正文本身仍然只有一份 —— `office.context.navigation`（Python `navigation_text()`）。
//  适配器只负责把它送进宿主的系统提示，**不重写、不裁剪、不另写模板**。
export const NAV_SECTION = "office_navigation";

//: 上下文过期的补充语（Pi 与 OC2 同一句）。这是把**已存在**的那句话挪到共享位置，
//  不是新增模板：两个宿主对"这次没取到上下文"必须说同一句话，否则模型会读到两种口径。
const NAV_STALE_SUFFIX = {
	zh: "（Office 上下文可能不是最新：受控连接这次没有应答。）",
	en: "(Office context may be outdated: the controlled connection did not answer.)",
};

/**
 * 把 `office.context.navigation` 的正文变成要写进系统提示的那段文字。
 * 两个适配器都走这里，避免各写一遍拼接（也避免一边加了"过期"提示、另一边忘了）。
 * @param {unknown} navigation 正文；不是非空字符串就返回 null（没有正文就不注入，不编空壳）
 * @param {string} locale 会话语言
 * @returns {{section: string, text: string, staleText: string} | null}
 */
export function navSectionText(navigation, locale) {
	if (typeof navigation !== "string" || navigation.length === 0) return null;
	const suffix = NAV_STALE_SUFFIX[normLocale(locale)];
	return { section: NAV_SECTION, text: navigation, staleText: navigation + "\n" + suffix };
}

/**
 * ★ W228（审计 #8）：`neutral: true` = **诚实的中性全集**：这套工具面本来就有哪些，全部如实声明。
 *
 * 为什么需要：能力**未知**时按 `toolAllowed` 过滤，会让管理类工具永远不出现（连"有这个入口"都
 * 看不到），而宿主又没实现真正按会话变化的列表（`listChanged:false`）⇒ 一旦早期请求算错，
 * 后面永远少三项。声明与授权是两件事：这里只决定"桌面上摆不摆这张牌"，
 * **能不能打由 Core 的动作事务复核**（`call()` 走的是同一条 Core op，拒就拒）。
 * 能力**已知**时仍按能力过滤（那是真实范围，不是猜）。
 */
export function toolSpecs(locale = DEFAULT_LOCALE, capabilities = null, opts = {}) {
	const idx = normLocale(locale) === "en" ? 1 : 0;
	const neutral = opts && opts.neutral === true;
	return (neutral ? OFFICE_TOOLS : OFFICE_TOOLS.filter((t) => toolAllowed(t, capabilities))).map((t) => ({
		name: t.name, op: t.op, write: t.write, requires: t.requires,
		label: t.label[idx], description: t.description[idx],
		parameters: localizeSchema(t.parameters, idx),
	}));
}
