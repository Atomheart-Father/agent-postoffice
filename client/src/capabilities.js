// One registry of what the client can do, how each backend serves it, and what is still needed.
// The in-app "Capabilities" dialog and docs/CLIENT.md both come from this list, so the UI never claims more than
// the table does. Statuses: live (served by the real backend), partial, mock (sample data only), missing, planned.
//
// `r0` is how the *published* R0 backend (GET /api/state, /api/letter, /api/outbox, POST /api/send ...) serves it.
// Routes that exist only in the unpublished local candidate (role-diff / role-apply) are NOT counted as live here:
// their payloads are not part of the handoff, so the HTTP adapter does not call them (see adapters/http.js).

export const FEATURES = [
  { key: "office.tree", group: "office", r0: "partial", zh: "公司 / 岗位树与在线状态", en: "Company and role tree with presence",
    note: { zh: "R0：树来自 config 的 panel.organization，状态只有「在线」开关；没有 busy / 省电 / 未接通的区分。", en: "R0: the tree comes from panel.organization in config; presence is only the online flag, with no busy / power-save / unconnected." },
    needs: "read_model.roles, read_model.presence" },
  { key: "role.detail", group: "office", r0: "missing", zh: "岗位职责 / 资源 / 资格", en: "Role duties, resources, qualification",
    note: { zh: "R0 的 /api/state 不含岗位职责与资源；每个信箱的 identity 有 memberships / active_roles，映射待接口包确认。", en: "R0 /api/state has no role duties or resources; each mailbox identity carries memberships / active_roles, mapping to be confirmed from the interface pack." },
    needs: "read_model.roles[].duties, resources, qualification" },
  { key: "role.incumbent", group: "office", r0: "partial", zh: "现任与候选的区分", en: "Incumbent versus candidates",
    note: { zh: "R0 的 identity.active_roles / candidate_only_roles 与此对应，HTTP 适配器暂未映射。", en: "identity.active_roles / candidate_only_roles in R0 correspond to this; the HTTP adapter does not map them yet." },
    needs: "read_model.roles[].incumbent, candidates" },
  { key: "role.edit", group: "office", r0: "missing", zh: "岗位编辑：草稿 → 差异 → 确认", en: "Role edit: draft, diff, confirm",
    note: { zh: "职责 / 资源的 preview / apply 在本地候选版有路由，未发布到 GitHub，载荷不在交接包内。资格 / 策略字段尚未接线（原型）。", en: "Preview / apply for duties and resources exist as routes in the unpublished local candidate; payloads are not in the handoff. Qualification and policy fields are not wired (prototype)." },
    needs: "POST role-diff / role-apply payload + error samples; qualification and policy fields" },
  { key: "thread.list", group: "threads", r0: "missing", zh: "线程列表与归属岗位", en: "Thread list and owning role",
    note: { zh: "R0 没有会话列表接口。", en: "R0 has no session list." }, needs: "read_model.threads" },
  { key: "thread.events", group: "threads", r0: "missing", zh: "线程文本 / 工具事件（有界缓冲）", en: "Thread text and tool events (bounded buffer)",
    note: { zh: "客户端只保留最近 N 条，可从快照恢复；不复制 Harness 的完整历史。", en: "The client keeps the last N events and can resume from a snapshot; it does not copy a Harness's full history." },
    needs: "event stream or cursor API, snapshot resume" },
  { key: "thread.control", group: "threads", r0: "missing", zh: "线程控制（观察 ≠ 控制）", en: "Thread control (observe is not control)",
    note: { zh: "只有后端确认的 Office 绑定可输入。创建 / 发送在 mock 里只返回样例结果，没有连接任何 Harness。", en: "Only a binding the backend confirmed accepts input. Create and send return sample results in mock and never reach a Harness." },
    needs: "thread create / send API, control state, approval flow" },
  { key: "mail.list", group: "work", r0: "partial", zh: "邮件收执状态", en: "Mail delivery state",
    note: { zh: "R0：pending[] 映射为「已受理」，失败映射为「失败」；没有「已消费」与发件箱以外的关联。", en: "R0: pending[] maps to accepted and failures to failed; no consumed state and no link from mail to threads or work." },
    needs: "read_model.mail with delivery + threadId / workId" },
  { key: "work.state", group: "work", r0: "missing", zh: "工作提交 / 验证 / 接受 / 退修 / 发布", en: "Work: submit, verify, accept, return, release",
    note: { zh: "一套通用状态，不是某个行业专用的状态机。后端未接线。", en: "One generic state model, not an industry-specific state machine. Not wired in the backend." },
    needs: "work module read model + transitions" },
  { key: "approvals", group: "work", r0: "missing", zh: "待批准事项", en: "Pending approvals", note: { zh: "批准决定由后端执行。", en: "The decision is made and enforced by the backend." }, needs: "approvals read model + decide API" },
  { key: "usage.hud", group: "hud", r0: "missing", zh: "Token HUD（官方累计用量）", en: "Token HUD (official cumulative usage)",
    note: { zh: "用量账模块已验收，但真实 reader / HUD 接口未交付；缺失值显示「未知」，不当作 0。", en: "The usage ledger module passed review but the real reader / HUD API is not delivered; missing values show as unknown, never 0." },
    needs: "usage reader API: input / output / cache / total / model / updatedAt / source / coverage; context occupancy" },
  { key: "live.events", group: "hud", r0: "missing", zh: "实时推送与快照恢复", en: "Live push and snapshot resume", note: { zh: "R0 为 5 秒轮询。", en: "R0 is polled every 5 seconds." }, needs: "stream with resume token" },
  { key: "secretary", group: "planned", r0: "planned", zh: "秘书（受控工具 / 批准条件）", en: "Secretary (controlled tools, approval conditions)", note: { zh: "后续方向，等后端能力包。", en: "Later; waits for the backend capability pack." }, needs: "capability pack" },
  { key: "meetings", group: "planned", r0: "planned", zh: "会议", en: "Meetings", note: { zh: "后续方向。", en: "Later." }, needs: "capability pack" },
  { key: "skills", group: "planned", r0: "planned", zh: "技能与插件", en: "Skills and plugins", note: { zh: "后续方向。", en: "Later." }, needs: "capability pack" },
  { key: "multi-office", group: "planned", r0: "planned", zh: "跨 Office", en: "Cross-office", note: { zh: "后续方向。", en: "Later." }, needs: "capability pack" },
];

export const GROUPS = [
  ["office", { zh: "公司与岗位", en: "Company and roles" }],
  ["threads", { zh: "线程", en: "Threads" }],
  ["work", { zh: "工作与邮件", en: "Work and mail" }],
  ["hud", { zh: "用量与实时", en: "Usage and live" }],
  ["planned", { zh: "后续", en: "Later" }],
];

// Every adapter reports its status per feature in snapshot.capabilities; this builds that map for a given backend.
export const capabilityMap = (statusOf) => Object.fromEntries(FEATURES.filter((f) => f.group !== "planned").map((f) => [f.key, statusOf(f)]));
