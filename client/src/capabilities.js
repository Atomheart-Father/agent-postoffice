// One registry of what the client can do and which source serves it. The in-app "Capabilities" dialog and
// docs/CLIENT.zh-CN.md are both generated from this list, so the UI never claims more than the table does.
//
// Three sources are told apart, because they are three different things:
//   master     GitHub master (v1.14.1, 1ab0f54): the published backend. Only what its /api/state really returns.
//   candidate  the local candidate e819f0e: NOT deployed and not on GitHub master. The client is wired to its contract pack
//              (Atomheart-Father/agent-postoffice@9cd53d5, docs/cloud-client-api-e819) and checked against the pack's
//              anonymous fixtures only, so nothing there is "live": the best status is `fixture`.
//   mock       this client's own sample data: every screen can be shown, nothing reaches a backend.
// Statuses: live (wired and run against a real backend), fixture (wired, checked against written fixtures only),
// partial, mock, missing (the source does not offer it), unverified (not checked), planned (later).
// A missing capability stays non-actionable in the UI; it is never filled with made-up data.

export const SOURCES = {
  master: { version: "v1.14.1", commit: "1ab0f54", zh: "GitHub master", en: "GitHub master" },
  candidate: { version: "", commit: "e819f0e", zh: "本地候选", en: "Local candidate" },
  mock: { version: "", commit: "", zh: "客户端 mock", en: "Client mock" },
};
export const sourceLabel = (k, L) => { const s = SOURCES[k]; return [s[L], s.version || s.commit].filter(Boolean).join(" "); };

export const FEATURES = [
  { key: "office.tree", group: "office", src: { master: "partial", candidate: "fixture", mock: "mock" }, zh: "公司 / 岗位树与在线状态", en: "Company and role tree with presence",
    note: { zh: "master：树来自 config 的 panel.organization，节点是信箱，不是岗位；树漏掉的信箱列在「未编入」下。候选：根下分「岗位」（来自 role_cards，按项目分组）和「信箱」两组，岗位不是信箱。", en: "master: the tree comes from panel.organization and its nodes are mailboxes, not positions; a mailbox the tree forgot is listed as unassigned. Candidate: under the root, \"Roles\" (from role_cards, grouped by project) and \"Mailboxes\" are separate groups; a role is not a mailbox." },
    needs: "read_model.roles, read_model.presence" },
  { key: "role.detail", group: "office", src: { master: "missing", candidate: "fixture", mock: "mock" }, zh: "岗位职责 / 资源 / 资格", en: "Role duties, resources, qualification",
    note: { zh: "master 的 /api/state 不含岗位职责与资源。候选的 role_cards 给出标题、职责、结构化资源（含路径是否存在）、升级岗位及其标题、状态码、配置检查、状态文件新鲜度；客户端原样显示，不显示资源的本机路径。", en: "master's /api/state has no duties or resources. The candidate's role_cards give title, duties, structured resources (including whether the path exists), the escalation role and its title, state codes, the config check and the status file's freshness; the client shows them as written and does not show a resource's local path." },
    needs: "read_model.roles[].duties, resources, qualification" },
  { key: "role.incumbent", group: "office", src: { master: "partial", candidate: "fixture", mock: "mock" }, zh: "现任与候选的区分", en: "Incumbent versus candidates",
    note: { zh: "master 只有每个信箱的事实：投递开关、会话绑定、最近活动。候选的岗位有 active（现任）与候选名单，现任的信箱事实按名字查，投递开关 / 绑定 / 活动 / 后端状态码并列显示，不合成「已连接」。活动「unknown」是后端的真实状态，保持未知，不当作空闲。", en: "master has per-mailbox facts only: delivery switch, session binding, last activity. The candidate's roles carry an active holder and a candidate list; the holder's mailbox facts are looked up by name and the delivery switch, binding, activity and the backend's state code are shown side by side, never merged into connected. Activity \"unknown\" is a real backend state and stays unknown, never idle." },
    needs: "read_model.roles[].incumbent, candidates" },
  { key: "role.edit", group: "office", src: { master: "missing", candidate: "fixture", mock: "mock" }, zh: "岗位编辑：草稿 → 差异 → 确认", en: "Role edit: draft, diff, confirm",
    note: { zh: "候选有 POST /api/role-diff（只预览，不写）与 /api/role-apply（需要预览给出的基线，确认后才发）。键不存在 = 不改，[] = 清空，整份替换；基线不符 409（没有 current，需重新预览）；saved:true + projection:pending = 已保存、名片待刷新，不是失败，也不重发；回复丢失按「结果未知」处理，不自动重发，只允许重新预览。没有 token 与 can.*，是否允许由后端在写入时判定。资格 / 策略 / 能力分配不在写入合同里。", en: "The candidate has POST /api/role-diff (preview only, writes nothing) and /api/role-apply (needs the baseline the preview returned and is sent only after confirmation). Absent key = unchanged, [] = clear, whole-list replace; a stale baseline answers 409 without a current role, so preview again; saved:true with projection pending means saved with the cards still to refresh, not a failure and never resent; a lost reply is outcome unknown and is never resent, only previewed again. There is no token and no can.*: the backend decides at write time. Qualification, policy and capability assignment are not in the write contract." },
    needs: "a deployed candidate (e819f0e is not) to verify role-diff / role-apply against; same-origin hosting" },
  { key: "thread.list", group: "threads", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "线程列表与归属岗位", en: "Thread list and owning role",
    note: { zh: "master 与候选都没有会话列表接口。", en: "Neither master nor the candidate has a session list." }, needs: "read_model.threads" },
  { key: "thread.events", group: "threads", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "线程文本 / 工具事件（有界缓冲）", en: "Thread text and tool events (bounded buffer)",
    note: { zh: "客户端只保留最近 N 条，可从快照恢复；不复制 Harness 的完整历史。", en: "The client keeps the last N events and can resume from a snapshot; it does not copy a Harness's full history." },
    needs: "event stream or cursor API, snapshot resume" },
  { key: "thread.control", group: "threads", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "线程控制（观察 ≠ 控制）", en: "Thread control (observe is not control)",
    note: { zh: "master 与候选都没有线程创建、发送、控制接口。真实后端下这些按钮不出现；只有 ?backend=mock 才能看到界面，且标「未接线」。", en: "Neither master nor the candidate has thread create, send or control. Against a real backend those buttons do not appear; only ?backend=mock shows the screens, marked Not wired." },
    needs: "thread create / send API, control state, approval flow" },
  { key: "mail.list", group: "work", src: { master: "partial", candidate: "fixture", mock: "mock" }, zh: "邮件收执状态", en: "Mail delivery state",
    note: { zh: "待处理邮件保留后端状态：待提醒 / 已提醒 / 失败；候选还有计数里的 ride，契约包没有说明它的含义，不并入任何状态。后端不记录信是发给哪个岗位的，所以只按信箱归属。", en: "Pending letters keep the backend state: waiting / reminded / failed. The candidate also counts `ride`; the pack does not say what it means, so it is not folded into any state. The backend does not record which role a letter was for, so letters are attributed to their mailbox only." },
    needs: "read_model.mail with delivery + threadId / workId" },
  { key: "mail.read", group: "work", src: { master: "live", candidate: "fixture", mock: "mock" }, zh: "查看信件", en: "Read a letter",
    note: { zh: "GET /api/letter：只读收件箱里的那一封，不写任何东西，也不叫醒任何人。已归档的信读不到。", en: "GET /api/letter: reads that one letter from the recipient's inbox and writes nothing. An archived letter can no longer be read." },
    needs: "" },
  { key: "mail.send", group: "work", src: { master: "live", candidate: "fixture", mock: "mock" }, zh: "发信与回复", en: "Write and reply",
    note: { zh: "POST /api/send：信写进收件人的信箱，发件人固定为后端配置的 operator。「已写入信箱」不等于对方收到、读过或办完。回复就是发信；可选「同时把原信归档」，回复发出后归档失败只重试归档，不会再发一次。", en: "POST /api/send: the letter is filed in the recipient's mailbox, sent as the operator the backend is configured with. Filed is not received, read or done. A reply is a send; the original can optionally be filed afterwards, and if that filing fails only the filing is retried, never the reply." },
    needs: "" },
  { key: "mail.ack", group: "work", src: { master: "live", candidate: "fixture", mock: "mock" }, zh: "收执（回执）", en: "Acknowledge (receipt)",
    note: { zh: "POST /api/ack-one：记一份回执、把原信归档，并在对方空闲时通知发信人。回执只表示「看到了」，不表示工作完成；重复调用不会产生第二份回执。", en: "POST /api/ack-one: records a receipt, files the original and notifies the sender when idle. A receipt says \"seen\", not \"done\"; repeating it does not produce a second receipt." },
    needs: "" },
  { key: "mail.archive", group: "work", src: { master: "live", candidate: "fixture", mock: "mock" }, zh: "归档（处理完成）", en: "File as processed",
    note: { zh: "POST /api/archive-one：把这一封从收件箱移到 done/，不发回执。同名文件已存在时后端拒绝并什么都不动。", en: "POST /api/archive-one: moves the letter from the inbox to done/ without a receipt. If a file with the same id is already there the backend refuses and moves nothing." },
    needs: "" },
  { key: "mail.outbox", group: "work", src: { master: "live", candidate: "fixture", mock: "mock" }, zh: "已发信：修改与撤回", en: "Sent mail: edit and retract",
    note: { zh: "GET /api/outbox 列出 operator 发出、仍在对方收件箱里的普通信，并给出后端对「还能不能改 / 撤」的判断；POST 时后端会重新检查，可能因竞争而拒绝。通道已受理的信改不了也撤不了。", en: "GET /api/outbox lists the operator's ordinary letters still in recipients' inboxes with the backend's own verdict on whether they can still be edited or retracted; the POST checks again and may refuse after losing a race. A letter a channel has accepted can be neither edited nor retracted." },
    needs: "" },
  { key: "mailbox.status", group: "work", src: { master: "live", candidate: "fixture", mock: "missing" }, zh: "上下线（投递开关）", en: "Delivery switch (online / offline)",
    note: { zh: "POST /api/status：单个、批量（候选的 names；master 没有，客户端逐个发、遇错即停）或按组（组名不带 @）。回复只说「后端接受了」，开关到底是什么以重新读到的状态为准；按钮永远按服务端状态显示。开关不等于连接，也不是老板的权限。", en: "POST /api/status: one mailbox, several (the candidate's names; master has none, so the client sends them one by one and stops at the first error) or a whole group (group name without \"@\"). The reply only says the backend accepted it; the switch is whatever the re-read state says, and buttons always follow the server's state. The switch is not a connection and not an authority." },
    needs: "" },
  { key: "mailbox.clear", group: "work", src: { master: "live", candidate: "fixture", mock: "missing" }, zh: "整箱清理（不是归档单封）", en: "Clear whole mailbox (not file one letter)",
    note: { zh: "POST /api/clear：把这个信箱 inbox 里的全部信收进 archived/<时间>/（留在磁盘上，不再投递）。和归档单封（archive-one → done/）是两回事：独立入口、写明「整箱」和封数、二次确认。回复不带封数，之后重新读状态。", en: "POST /api/clear: moves EVERY letter in this mailbox's inbox into archived/<time>/ (kept on disk, never delivered). It is not filing one letter (archive-one, to done/): separate entry, says \"whole mailbox\" and how many letters, two-step confirmation. The reply carries no count; the state is re-read afterwards." },
    needs: "" },
  { key: "work.state", group: "work", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "工作提交 / 验证 / 接受 / 退修 / 发布", en: "Work: submit, verify, accept, return, release",
    note: { zh: "一套通用状态，不是某个行业专用的状态机。后端未接线。", en: "One generic state model, not an industry-specific state machine. Not wired in the backend." },
    needs: "work module read model + transitions" },
  { key: "approvals", group: "work", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "待批准事项", en: "Pending approvals", note: { zh: "批准决定由后端执行。", en: "The decision is made and enforced by the backend." }, needs: "approvals read model + decide API" },
  { key: "usage.hud", group: "hud", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "Token HUD（官方累计用量）", en: "Token HUD (official cumulative usage)",
    note: { zh: "用量账模块已验收，但真实 reader / HUD 接口未交付；缺失值显示「未知」，不当作 0。", en: "The usage ledger module passed review but the real reader / HUD API is not delivered; missing values show as unknown, never 0." },
    needs: "usage reader API: input / output / cache / total / model / updatedAt / source / coverage; context occupancy" },
  { key: "live.events", group: "hud", src: { master: "missing", candidate: "missing", mock: "mock" }, zh: "实时推送与快照恢复", en: "Live push and snapshot resume", note: { zh: "master 与候选都是读取 /api/state 的 5 秒轮询，没有推送。", en: "Neither master nor the candidate pushes: the client polls /api/state every 5 seconds." }, needs: "stream with resume token" },
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
const WORD = { live: "已接通", fixture: "已接线·仅 fixture 核对", partial: "部分", mock: "样例·未接线", missing: "缺失", unverified: "未核对", planned: "后续" };
export function capsTableMarkdown() {
  const cell = (s) => String(s).replace(/\|/g, "\\|");
  const head = ["功能", sourceLabel("master", "zh"), sourceLabel("candidate", "zh"), sourceLabel("mock", "zh"), "说明", "接通需要"];
  const rows = FEATURES.map((f) => [f.zh, WORD[f.src.master], WORD[f.src.candidate], WORD[f.src.mock], f.note.zh, f.needs]);
  return [head, head.map(() => "---"), ...rows].map((r) => `| ${r.map(cell).join(" | ")} |`).join("\n");
}
