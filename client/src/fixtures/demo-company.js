// Anonymous sample company. Invented names, no real mailboxes, configs or sessions. Times are relative to `now`
// so the page always reads as "a few minutes ago". Every value here is sample data and the mock adapter says so.

import { CONTRACT_VERSION } from "../contract.js";

const M = 60, H = 3600, D = 86400;

export function demoSnapshot(now = Date.now() / 1000) {
  const ago = (s) => Math.round(now - s);
  // The sample holds the facts the real backend keeps apart: presence (from a fresh signal), the delivery switch,
// the session binding and the last activity. A holder can be "unknown" with the switch on: switch on is not connected.
  const occ = (id, label, kind, harness, model, presence, at, note = "") => ({ id, label, kind, harness, model, presence, presenceAt: at == null ? null : ago(at), note,
    switch: presence === "offline" ? "off" : presence === "unconnected" ? "unknown" : "on", binding: harness ? "session" : null,
    activity: presence === "busy" ? { text: "working", at: ago(at), source: "activity hook" } : presence === "connected" && at != null ? { text: "idle", at: ago(at), source: "activity hook" } : null });
  const eng = (seq, at, kind, who, text) => ({ seq, at: ago(at), kind, who, text });

  const roles = [
    { id: "owner", parentId: null, kind: "position", title: "Owner", order: 0,
      summary: "Sets direction, approves what leaves the company.",
      duties: ["Approve releases and external messages", "Set quarterly priorities", "Decide who holds each role"],
      resources: [{ kind: "doc", ref: "budgets/monthly.md", note: "Monthly token budget" }, { kind: "doc", ref: "operator.md", note: "Operator mailbox" }],
      incumbent: occ("you", "You (operator)", "human", null, null, "connected", 20),
      candidates: [], waiting: 2, qualification: { level: 5, note: "Final approver" },
      can: { edit: { allowed: false, reason: "The operator role is defined in the company config file, not from the client." } } },

    { id: "research", parentId: "owner", kind: "team", title: "Research", order: 1, summary: "Finds and checks facts before anything is built.", duties: [], resources: [], incumbent: null, candidates: [], waiting: 0, can: {} },
    { id: "research-lead", parentId: "research", kind: "position", title: "Research Lead", order: 1,
      summary: "Plans the research queue and signs off summaries.",
      duties: ["Break a question into checkable parts", "Assign sources to the Analyst", "Sign off the weekly summary"],
      resources: [{ kind: "dir", ref: "notes/", note: "read, write" }, { kind: "skill", ref: "web-search", note: "" }, { kind: "doc", ref: "budgets/research.md", note: "40% of research budget" }],
      incumbent: occ("rl-1", "Lead agent", "agent", "Claude Code", "opus-class", "connected", 2 * M), candidates: [occ("rl-2", "Backup lead", "agent", "Codex", "codex-default", "offline", 3 * H)],
      waiting: 1, qualification: { level: 4, note: "May sign off summaries" }, can: { edit: { allowed: true, reason: "" } } },
    { id: "analyst", parentId: "research-lead", kind: "position", title: "Analyst", order: 1,
      summary: "Collects and tabulates sources.", duties: ["Collect sources for a question", "Tabulate figures with their origin", "Flag figures that cannot be traced"],
      resources: [{ kind: "skill", ref: "web-search", note: "" }, { kind: "dir", ref: "notes/", note: "read" }],
      incumbent: occ("an-1", "Analyst agent", "agent", "OpenCode", "oss-coder", "busy", 20, "Working on a pricing table"), candidates: [],
      waiting: 0, qualification: { level: 2, note: "" }, can: { edit: { allowed: true, reason: "" } } },
    { id: "fact-checker", parentId: "research-lead", kind: "position", title: "Fact-checker", order: 2,
      summary: "Re-derives every figure from its source.", duties: ["Re-derive figures from sources", "Mark unverifiable claims"], resources: [{ kind: "skill", ref: "web-search", note: "" }],
      incumbent: occ("fc-1", "Checker agent", "agent", "Claude Code", "sonnet-class", "power_save", 41 * M, "Idle on purpose until a summary arrives"), candidates: [],
      waiting: 0, qualification: { level: 3, note: "" }, can: { edit: { allowed: true, reason: "" } } },

    { id: "engineering", parentId: "owner", kind: "team", title: "Engineering", order: 2, summary: "Builds and ships.", duties: [], resources: [], incumbent: null, candidates: [], waiting: 0, can: {} },
    { id: "eng-lead", parentId: "engineering", kind: "position", title: "Engineering Lead", order: 1,
      summary: "Owns the build plan and the release checklist.", duties: ["Turn requests into build tasks", "Own the release checklist", "Escalate blocked work to the Owner"],
      resources: [{ kind: "dir", ref: "repo/", note: "read, write on branches" }, { kind: "doc", ref: "budgets/build.md", note: "35% of build budget" }],
      incumbent: occ("el-1", "Build lead agent", "agent", "Claude Code", "opus-class", "connected", 90), candidates: [occ("el-2", "Spare lead", "agent", "OpenCode", "oss-coder", "unconnected", null, "Named, no Harness attached")],
      waiting: 1, qualification: { level: 4, note: "" }, can: { edit: { allowed: true, reason: "" } } },
    { id: "builder", parentId: "eng-lead", kind: "position", title: "Builder", order: 1,
      summary: "Writes code on branches.", duties: ["Implement tasks on a branch", "Open a change for review", "Never merge own change"],
      resources: [{ kind: "dir", ref: "repo/", note: "branches only" }, { kind: "skill", ref: "test-runner", note: "" }],
      incumbent: occ("bd-1", "Builder agent", "agent", "Codex", "codex-default", "busy", 15, "Running tests"), candidates: [], waiting: 0, qualification: { level: 2, note: "" }, can: { edit: { allowed: true, reason: "" } } },
    { id: "reviewer", parentId: "eng-lead", kind: "position", title: "Reviewer", order: 2,
      summary: "Reads changes and returns or accepts them.", duties: ["Review every change before release", "Return a change with reasons, or accept it"],
      resources: [{ kind: "dir", ref: "repo/", note: "read" }],
      incumbent: occ("rv-1", "Review agent", "agent", "Codex", "codex-default", "unknown", null, "No recent signal from the Harness"), candidates: [], waiting: 0, qualification: { level: 3, note: "" },
      can: { edit: { allowed: false, reason: "Changing a reviewer's resources needs the capability role.edit on Engineering, which you have not been granted." } } },
    { id: "release", parentId: "eng-lead", kind: "position", title: "Release Manager", order: 3,
      summary: "Cuts releases once changes are accepted.", duties: ["Cut a release from accepted changes", "Write release notes"], resources: [{ kind: "dir", ref: "repo/", note: "tags" }],
      incumbent: occ("rm-1", "Release agent", "agent", null, null, "unconnected", null, "Named, no Harness attached"), candidates: [], waiting: 0, qualification: { level: 3, note: "" }, can: { edit: { allowed: true, reason: "" } } },

    { id: "operations", parentId: "owner", kind: "team", title: "Operations", order: 3, summary: "Keeps records and the day-to-day.", duties: [], resources: [], incumbent: null, candidates: [], waiting: 0, can: {} },
    { id: "ops-coordinator", parentId: "operations", kind: "position", title: "Ops Coordinator", order: 1,
      summary: "Routes requests and keeps the calendar.", duties: ["Route incoming requests to a role", "Keep the shared calendar"],
      resources: [{ kind: "doc", ref: "shared-inbox.md", note: "Shared inbox" }],
      incumbent: occ("oc-1", "Coordinator agent", "agent", "OpenCode", "oss-coder", "offline", 5 * H), candidates: [], waiting: 0, qualification: { level: 2, note: "" }, can: { edit: { allowed: true, reason: "" } } },
    { id: "archivist", parentId: "operations", kind: "position", title: "Archivist", order: 2,
      summary: "Files finished work so it can be found again.", duties: ["File accepted work with its sources"], resources: [{ kind: "dir", ref: "archive/", note: "write" }],
      incumbent: null, candidates: [occ("ar-1", "Archive agent A", "agent", "Claude Code", "sonnet-class", "connected", 4 * M), occ("ar-2", "Archive agent B", "agent", null, null, "unconnected", null)],
      waiting: 0, qualification: { level: 1, note: "" }, can: { edit: { allowed: true, reason: "" } } },
  ];

  // Candidates are the ordered list of who may hold a position; the incumbent is normally one of them (a candidate is
  // not the holder). Escalation goes to the nearest position above; its current holder is the acceptor.
  const byId = new Map(roles.map((r) => [r.id, r]));
  for (const r of roles) {
    if (r.kind !== "position") continue;
    if (r.incumbent && !r.candidates.some((c) => c.id === r.incumbent.id)) r.candidates = [r.incumbent, ...r.candidates];
    let up = r.parentId ? byId.get(r.parentId) : null;
    while (up && up.kind !== "position") up = up.parentId ? byId.get(up.parentId) : null;
    r.escalationRole = up ? up.id : "";
    r.escalationAcceptor = up && up.incumbent ? up.incumbent.id : null;
  }

  const full = { input: 1284000, output: 212000, cacheRead: 9400000, cacheWrite: 310000, total: 11206000, model: "opus-class", updatedAt: ago(40), source: "harness usage report", coverage: "complete", context: { tokens: 84000, window: 200000 } };
  const partial = { input: 402000, output: 88000, cacheRead: null, cacheWrite: null, total: null, model: "oss-coder", updatedAt: ago(14 * M), source: "harness usage report", coverage: "partial", context: null };

  const threads = [
    { id: "t-scan", roleId: "research-lead", title: "Q3 market scan", harness: "Claude Code", model: "opus-class", source: "office", control: "controllable", status: "active", updatedAt: ago(2 * M),
      bufferLimit: 6, droppedBefore: 118, usage: full,
      events: [
        eng(119, 9 * M, "text", "Lead agent", "Splitting the scan into three questions: size, price bands, new entrants."),
        eng(120, 8 * M, "tool", "Lead agent", "search: market size 2026 small-business tools"),
        eng(121, 6 * M, "status", "", "Waiting for Analyst's table"),
        eng(122, 4 * M, "text", "Analyst agent", "Table v1 is in notes/pricing.md; two figures have no source yet."),
        eng(123, 3 * M, "text", "Lead agent", "Ask the Fact-checker to re-derive the two unsourced figures before the summary."),
        eng(124, 2 * M, "tool", "Lead agent", "write: notes/summary-draft.md (+31 lines)")] },
    { id: "t-pricing", roleId: "analyst", title: "Competitor pricing table", harness: "OpenCode", model: "oss-coder", source: "observed", control: "observe_only",
      controlReason: "Source: an OpenCode session started outside Office; no Office binding exists.", status: "active", updatedAt: ago(20),
      bufferLimit: 40, droppedBefore: 0, usage: partial,
      events: [eng(1, 30 * M, "text", "Analyst agent", "Collecting list prices for six competitors."), eng(2, 22 * M, "tool", "Analyst agent", "fetch: three pricing pages"), eng(3, 20, "status", "", "Tabulating")] },
    { id: "t-import", roleId: "builder", title: "Fix import pipeline", harness: "Codex", model: "codex-default", source: "office", control: "pending_approval",
      controlReason: "Binding requested by Engineering Lead; waits for your approval.", status: "active", updatedAt: ago(15),
      bufferLimit: 40, droppedBefore: 0, usage: { ...partial, model: "codex-default" },
      events: [eng(1, 25 * M, "text", "Builder agent", "Reproduced the failure with the sample file."), eng(2, 15, "tool", "Builder agent", "run: test suite (214 tests)")] },
    { id: "t-pr41", roleId: "reviewer", title: "Review change 41", harness: "Codex", model: "codex-default", source: "observed", control: "capability_missing",
      controlReason: "This Harness adapter has no write channel, so Office can only watch.", status: "waiting", updatedAt: ago(2 * H),
      bufferLimit: 40, droppedBefore: 0, usage: null, events: [eng(1, 2 * H, "text", "Review agent", "Waiting for the Builder to push the last commit.")] },
    { id: "t-release", roleId: "eng-lead", title: "Release checklist", harness: "Claude Code", model: "opus-class", source: "office", control: "controllable", status: "idle", updatedAt: ago(40 * M),
      bufferLimit: 40, droppedBefore: 0, usage: { ...full, total: 3480000, input: 410000, output: 60000, cacheRead: 3000000, cacheWrite: 10000, context: { tokens: 31000, window: 200000 } },
      events: [eng(1, 3 * H, "text", "Build lead agent", "Checklist drafted: tests, changelog, tag, announce."), eng(2, 40 * M, "status", "", "Idle until a change is accepted")] },
    { id: "t-vendor", roleId: "ops-coordinator", title: "Vendor invoices", harness: "OpenCode", model: "oss-coder", source: "observed", control: "denied",
      controlReason: "You have not been granted thread.control on Operations.", status: "idle", updatedAt: ago(5 * H),
      bufferLimit: 40, droppedBefore: 0, usage: null, events: [eng(1, 5 * H, "text", "Coordinator agent", "Three invoices filed, one waiting for a purchase order.")] },
    { id: "t-reading", roleId: "research-lead", title: "Reading list", harness: "Claude Code", model: "opus-class", source: "office", control: "stale",
      controlReason: "The Office binding for this thread expired; it must be re-bound before it accepts input.", status: "ended", updatedAt: ago(3 * D),
      bufferLimit: 40, droppedBefore: 0, usage: null, events: [eng(1, 3 * D, "text", "Lead agent", "Reading list finalised.")] },
  ];

  const mail = [
    { id: "m1", roleId: "owner", threadId: "t-scan", workId: "w-summary", dir: "in", from: "Research Lead", to: "Owner", subject: "Weekly research summary", at: ago(12 * M), delivery: "accepted" },
    { id: "m2", roleId: "builder", threadId: "t-import", workId: "w-import", dir: "out", from: "Owner", to: "Builder", subject: "Please fix the import pipeline", at: ago(35 * M), delivery: "consumed" },
    { id: "m3", roleId: "reviewer", threadId: "t-pr41", workId: null, dir: "out", from: "Owner", to: "Reviewer", subject: "Review request: change 41", at: ago(2 * H), delivery: "failed" },
    { id: "m4", roleId: "owner", threadId: "t-release", workId: "w-release", dir: "in", from: "Engineering Lead", to: "Owner", subject: "Release candidate ready for sign-off", at: ago(50 * M), delivery: "accepted" },
    { id: "m5", roleId: "analyst", threadId: "t-pricing", workId: "w-pricing", dir: "out", from: "Research Lead", to: "Analyst", subject: "Pricing table for six competitors", at: ago(4 * H), delivery: "consumed" },
    { id: "m6", roleId: "research-lead", threadId: null, workId: null, dir: "in", from: "Owner", to: "Research Lead", subject: "Priorities for the quarter", at: ago(1 * D), delivery: "consumed" },
    { id: "m7", roleId: "ops-coordinator", threadId: "t-vendor", workId: null, dir: "out", from: "Owner", to: "Ops Coordinator", subject: "Chase the missing purchase order", at: ago(6 * H), delivery: "sent" },
    { id: "m8", roleId: "eng-lead", threadId: "t-release", workId: null, dir: "in", from: "Owner", to: "Engineering Lead", subject: "Hold the release until the summary lands", at: ago(8 * M), delivery: "accepted" },
    { id: "m9", roleId: "archivist", threadId: null, workId: null, dir: "out", from: "Owner", to: "Archivist", subject: "File the Q2 report", at: ago(2 * D), delivery: "retracted" },
  ];

  // each sample letter sits in the mailbox of the role it is attributed to; the sender is the role whose title the
  // `from` carries (a canonical mailbox name, as the real letter header has)
  const idOfTitle = (title) => (roles.find((r) => r.title === title) || { id: "" }).id;
  mail.forEach((m) => { m.box = m.roleId; m.letterId = `20261013-${m.id}`; m.sender = idOfTitle(m.from); });

  const work = [
    { id: "w-summary", roleId: "research-lead", title: "Weekly research summary", state: "submitted", difficulty: 2, updatedAt: ago(12 * M), note: "The mail was accepted by the Owner's mailbox. Nobody has verified the figures yet.", history: [{ state: "open", at: ago(2 * D), by: "Owner" }, { state: "submitted", at: ago(12 * M), by: "Research Lead" }] },
    { id: "w-import", roleId: "builder", title: "Fix the import pipeline", state: "open", difficulty: 3, updatedAt: ago(35 * M), note: "The Builder consumed the mail and is working. That is not completion.", history: [{ state: "open", at: ago(35 * M), by: "Owner" }] },
    { id: "w-pricing", roleId: "analyst", title: "Competitor pricing table", state: "verified", difficulty: 2, updatedAt: ago(3 * H), note: "Two figures verified; two have no source.", history: [{ state: "open", at: ago(1 * D), by: "Research Lead" }, { state: "submitted", at: ago(5 * H), by: "Analyst" }, { state: "verified", at: ago(3 * H), by: "Fact-checker" }] },
    { id: "w-release", roleId: "eng-lead", title: "Release candidate 0.4", state: "accepted", difficulty: 4, updatedAt: ago(50 * M), note: "Reviewer accepted the change; waits for the Owner to release.", history: [{ state: "open", at: ago(3 * D), by: "Owner" }, { state: "submitted", at: ago(1 * D), by: "Builder" }, { state: "verified", at: ago(6 * H), by: "Reviewer" }, { state: "accepted", at: ago(50 * M), by: "Eng Lead" }] },
    { id: "w-copy", roleId: "ops-coordinator", title: "Landing page copy", state: "returned", difficulty: 1, updatedAt: ago(1 * D), note: "Returned: tone does not match the brand guide; section 2 is missing.", history: [{ state: "open", at: ago(3 * D), by: "Owner" }, { state: "submitted", at: ago(2 * D), by: "Ops Coordinator" }, { state: "returned", at: ago(1 * D), by: "Owner" }] },
    { id: "w-notes", roleId: "release", title: "Release notes 0.3", state: "released", difficulty: 1, updatedAt: ago(6 * D), note: "", history: [{ state: "open", at: ago(9 * D), by: "Eng Lead" }, { state: "submitted", at: ago(8 * D), by: "Release Manager" }, { state: "accepted", at: ago(7 * D), by: "Owner" }, { state: "released", at: ago(6 * D), by: "Release Manager" }] },
  ];

  const approvals = [
    { id: "ap-1", kind: "thread_bind", roleId: "builder", title: "Bind the Builder's thread \"Fix import pipeline\" to Office control", at: ago(15 * M), state: "pending" },
    { id: "ap-2", kind: "role_change", roleId: "reviewer", title: "Add repository \"infra\" (read) to the Reviewer's resources", at: ago(1 * H), state: "pending" },
  ];

  return {
    contract: CONTRACT_VERSION, asOf: Math.round(now), consistency: "consistent", degraded: [],
    company: { name: "Northwind Studio", operatorRoleId: "owner", operatorBox: "owner" },
    // sample "To" suggestions: the mock treats a position id as its mailbox name and offers two logical addresses
    addresses: [
      ...roles.filter((r) => r.kind === "position").map((r) => ({ kind: "box", name: r.id, label: r.title, target: null, online: r.incumbent ? r.incumbent.presence === "connected" || r.incumbent.presence === "busy" : null, error: "" })),
      { kind: "alias", name: "@researcher", label: "", target: "analyst", online: true, error: "" },
      { kind: "alias", name: "@builders", label: "", target: "builder", online: true, error: "" },
    ],
    capabilities: {},   // filled by the adapter
    roles, threads, mail, work, approvals,
    usage: { ...full, total: 14686000, input: 1696000, output: 300000, cacheRead: 12400000, cacheWrite: 320000, context: null, source: "sum reported by each Harness", coverage: "partial" },
  };
}
