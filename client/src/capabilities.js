// One registry of what the client can do and which source serves it. The in-app "Capabilities" dialog and
// docs/CLIENT.zh-CN.md are both generated from this list, so the UI never claims more than the table does.
//
// Three sources are told apart, because they are three different things:
//   master     GitHub master (v1.14.1, 1ab0f54): the published backend. Only what its /api/state really returns.
//   candidate  the local candidate e819f0e: NOT on GitHub, known to this client only from a written description, so
//              nothing counts as live there until a mapping pass has been done against its anonymous fixture.
//   mock       this client's own sample data: every screen can be shown, nothing reaches a backend.
// Statuses: live, partial, mock, missing (the source does not offer it), unverified (not checked), planned (later).
// A missing capability stays non-actionable in the UI; it is never filled with made-up data.

export const SOURCES = {
  master: { version: "v1.14.1", commit: "1ab0f54", zh: "GitHub master", en: "GitHub master" },
  candidate: { version: "", commit: "e819f0e", zh: "本地候选", en: "Local candidate" },
  mock: { version: "", commit: "", zh: "客户端 mock", en: "Client mock" },
};
export const sourceLabel = (k, L) => { const s = SOURCES[k]; return [s[L], s.version || s.commit].filter(Boolean).join(" "); };

export const FEATURES = [
  { key: "office.tree", group: "office", src: { master: "partial", candidate: "partial", mock: "mock" }, zh: "公司 / 岗位树与在线状态", en: "Company and role tree with presence",
    note: { zh: "master：树来自 config 的 panel.organization，节点是信箱，不是岗位。树漏掉的信箱会列在「未编入」下。", en: "master: the tree comes from panel.organization and its nodes are mailboxes, not positions. A mailbox the tree forgot is listed as unassigned." },
    needs: "read_model.roles, read_model.presence" },
  { key: "role.detail", group: "office", src: { master: "missing", candidate: "partial", mock: "mock" }, zh: "岗位职责 / 资源 / 资格", en: "Role duties, resources, qualification",
    note: { zh: "master 的 /api/state 不含岗位职责与资源。本地候选的 role_cards 有 name / title / active / candidates / duties / resources / kind，字段形状待匿名 fixture 核对后再映射。", en: "master /api/state has no duties or resources. The candidate's role_cards carry name / title / active / candidates / duties / resources / kind; their exact shapes are mapped only after the anonymous fixture is checked." },
    needs: "read_model.roles[].duties, resources, qualification" },
  { key: "role.incumbent", group: "office", src: { master: "partial", candidate: "partial", mock: "mock" }, zh: "现任与候选的区分", en: "Incumbent versus candidates",
    note: { zh: "master 只有每个信箱的事实：投递开关、会话绑定、最近活动。只有新鲜的活动信号才算「已接通 / 忙」，开关打开不等于已连接。", en: "master has per-mailbox facts only: delivery switch, session binding, last activity. Only a fresh activity signal counts as connected or busy; a switch that is on does not mean connected." },
    needs: "read_model.roles[].incumbent, candidates" },
  { key: "role.edit", group: "office", src: { master: "missing", candidate: "partial", mock: "mock" }, zh: "岗位编辑：草稿 → 差异 → 确认", en: "Role edit: draft, diff, confirm",
    note: { zh: "本地候选有 POST /api/role-diff 与 /api/role-apply，master 没有。客户端按其合同做了流程（整配置基线、已保存 / 派生名片分开），HTTP 适配器尚未调用。资格 / 策略 / 能力分配仍是设计，不在写入合同里。", en: "The local candidate has POST /api/role-diff and /api/role-apply; master does not. The client implements the flow to that contract (whole-config baseline, saved and derived cards kept apart) but the HTTP adapter does not call it yet. Qualification, policy and capability assignment are design and not part of the write contract." },
    needs: "anonymous role-diff / role-apply fixtures incl. 409 and projection-pending samples; same-origin hosting" },
  { key: "thread.list", group: "threads", src: { master: "missing", candidate: "unverified", mock: "mock" }, zh: "线程列表与归属岗位", en: "Thread list and owning role",
    note: { zh: "master 没有会话列表接口。", en: "master has no session list." }, needs: "read_model.threads" },
  { key: "thread.events", group: "threads", src: { master: "missing", candidate: "unverified", mock: "mock" }, zh: "线程文本 / 工具事件（有界缓冲）", en: "Thread text and tool events (bounded buffer)",
    note: { zh: "客户端只保留最近 N 条，可从快照恢复；不复制 Harness 的完整历史。", en: "The client keeps the last N events and can resume from a snapshot; it does not copy a Harness's full history." },
    needs: "event stream or cursor API, snapshot resume" },
  { key: "thread.control", group: "threads", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "线程控制（观察 ≠ 控制）", en: "Thread control (observe is not control)",
    note: { zh: "只有后端确认的 Office 绑定可输入。创建 / 发送在 mock 里只返回样例结果，没有连接任何 Harness。", en: "Only a binding the backend confirmed accepts input. Create and send return sample results in mock and never reach a Harness." },
    needs: "thread create / send API, control state, approval flow" },
  { key: "mail.list", group: "work", src: { master: "partial", candidate: "partial", mock: "mock" }, zh: "邮件收执状态", en: "Mail delivery state",
    note: { zh: "待处理邮件保留后端状态：在信箱待提醒 / 已提醒 / 失败，不并成「已受理」。后端不记录信是发给哪个岗位的，所以只按信箱归属。", en: "Pending letters keep the backend state: waiting / reminded / failed, never merged into accepted. The backend does not record which role a letter was for, so letters are attributed to their mailbox only." },
    needs: "read_model.mail with delivery + threadId / workId" },
  { key: "work.state", group: "work", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "工作提交 / 验证 / 接受 / 退修 / 发布", en: "Work: submit, verify, accept, return, release",
    note: { zh: "一套通用状态，不是某个行业专用的状态机。后端未接线。", en: "One generic state model, not an industry-specific state machine. Not wired in the backend." },
    needs: "work module read model + transitions" },
  { key: "approvals", group: "work", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "待批准事项", en: "Pending approvals", note: { zh: "批准决定由后端执行。", en: "The decision is made and enforced by the backend." }, needs: "approvals read model + decide API" },
  { key: "usage.hud", group: "hud", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "Token HUD（官方累计用量）", en: "Token HUD (official cumulative usage)",
    note: { zh: "用量账模块已验收，但真实 reader / HUD 接口未交付；缺失值显示「未知」，不当作 0。", en: "The usage ledger module passed review but the real reader / HUD API is not delivered; missing values show as unknown, never 0." },
    needs: "usage reader API: input / output / cache / total / model / updatedAt / source / coverage; context occupancy" },
  { key: "live.events", group: "hud", src: { master: "missing", candidate: "unverified", mock: "mock" }, zh: "实时推送与快照恢复", en: "Live push and snapshot resume", note: { zh: "master 为 5 秒轮询。", en: "master is polled every 5 seconds." }, needs: "stream with resume token" },
  { key: "secretary", group: "planned", src: { master: "planned", candidate: "planned", mock: "planned" }, zh: "秘书（受控工具 / 批准条件）", en: "Secretary (controlled tools, approval conditions)", note: { zh: "后续方向，等后端能力包。", en: "Later; waits for the backend capability pack." }, needs: "capability pack" },
  { key: "meetings", group: "planned", src: { master: "planned", candidate: "planned", mock: "planned" }, zh: "会议", en: "Meetings", note: { zh: "后续方向。", en: "Later." }, needs: "capability pack" },
  { key: "skills", group: "planned", src: { master: "planned", candidate: "planned", mock: "planned" }, zh: "技能与插件", en: "Skills and plugins", note: { zh: "后续方向。", en: "Later." }, needs: "capability pack" },
  { key: "multi-office", group: "planned", src: { master: "planned", candidate: "planned", mock: "planned" }, zh: "跨 Office", en: "Cross-office", note: { zh: "后续方向。", en: "Later." }, needs: "capability pack" },
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

// The Markdown table in docs/CLIENT.zh-CN.md between the caps markers is this function's output; a test keeps them equal.
const WORD = { live: "已接通", partial: "部分", mock: "样例", missing: "缺失", unverified: "未核对", planned: "后续" };
export function capsTableMarkdown() {
  const cell = (s) => String(s).replace(/\|/g, "\\|");
  const head = ["功能", sourceLabel("master", "zh"), sourceLabel("candidate", "zh"), sourceLabel("mock", "zh"), "说明", "接通需要"];
  const rows = FEATURES.map((f) => [f.zh, WORD[f.src.master], WORD[f.src.candidate], WORD[f.src.mock], f.note.zh, f.needs]);
  return [head, head.map(() => "---"), ...rows].map((r) => `| ${r.map(cell).join(" | ")} |`).join("\n");
}
