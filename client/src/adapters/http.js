// HTTP adapter for the GitHub master backend (v1.14.1, the panel API served by `postoffice panel`).
// It maps what master really returns into the client's read model and says plainly what it cannot map:
//   GET /api/state   -> company, the organisation tree (panel.organization) as MAILBOX nodes, per-mailbox facts
//                       (delivery switch, session binding, last activity), and the pending letters of each mailbox
// Everything else (role detail, threads, work, approvals, usage, role edit, thread control) is reported as missing and
// answers capability_missing instead of guessing a payload. The local candidate's role-diff / role-apply / role_cards
// are NOT called yet: they are unpublished and their reply shapes are only known from a written description, so the
// next step is one mapping pass against the anonymous fixture the local team provides (docs/CLIENT.zh-CN.md §5).
//
// What this mapping refuses to do, on purpose:
//   * treat a mailbox as a position: tree nodes are mailbox nodes (id `box:<name>`, origin "mailbox"), not roles;
//   * turn the delivery switch into "connected": presence comes only from a fresh activity signal, else unknown;
//   * flatten waiting / reminded / failed letters into one "accepted";
//   * hide a mailbox the (valid) tree forgot: it is listed under an "unassigned" node;
//   * say which role a letter was for: the backend does not record it, so mail is attributed to its mailbox only.
// Auth and CORS are not decided (docs/CLIENT.zh-CN.md §4): this adapter sends no credentials and is meant for a
// backend on the same origin as the client.

import { ERR, ok, fail, normalizeSnapshot, CONTRACT_VERSION } from "../contract.js";
import { capabilityMap, FEATURES } from "../capabilities.js";

const MAIL = { waiting: "waiting", reminded: "reminded", failed: "failed" };
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

// One mailbox as the holder of its node. Presence comes from the activity signal only (it is fresh and bound, or it
// is absent): working -> busy, idle -> connected, anything else -> unknown. The delivery switch and the binding stay
// separate facts, because a switch that is on proves delivery is allowed, not that a Harness is attached and healthy,
// and a switch that is off may be the owner's choice, not a dead session.
export function occupantOf(b) {
  const prof = (b.identity && b.identity.profile) || {};
  const act = b.activity && typeof b.activity === "object" ? b.activity : {};
  const live = act.state === "working" || act.state === "idle";
  const since = num(act.since);
  return {
    id: b.name, label: prof.display_name || b.who || b.name, kind: prof.kind === "human" || prof.kind === "agent" ? prof.kind : "unspecified",
    harness: b.app || null, model: null,
    presence: act.state === "working" ? "busy" : act.state === "idle" ? "connected" : "unknown", presenceAt: live ? since : null,
    switch: b.online === true ? "on" : b.online === false ? "off" : "unknown",
    binding: (b.identity && b.identity.binding && b.identity.binding.kind) || null,
    activity: live ? { text: act.state, at: since, source: "activity hook" } : null,
  };
}

// /api/state -> snapshot. Pure so it can be tested against fixtures.
export function mapMasterState(st, now = Date.now() / 1000) {
  const boxes = Array.isArray(st && st.boxes) ? st.boxes : [];
  const box = (n) => boxes.find((b) => b.name === n);
  const panel = (st && st.panel) || {};
  const roles = [], degraded = [], seen = new Set();
  const blank = (id, parentId, order, title, extra = {}) => ({ id, parentId, order, kind: "team", title, summary: "", duties: [], resources: [], incumbent: null, candidates: [], origin: "mailbox", box: "", waiting: 0, can: {}, ...extra });
  const seat = (name, label, parentId, order) => {
    const b = box(name); seen.add(name);
    const c = (b && b.counts) || {};
    roles.push(blank(`box:${name}`, parentId, order, label || name, { kind: "position", box: name, incumbent: b ? occupantOf(b) : null, waiting: (num(c.waiting) || 0) + (num(c.reminded) || 0) }));
  };
  const walk = (n, parentId, order) => {
    if (!n) return null;
    const me = n.mailbox ? `box:${n.mailbox}` : `team:${parentId || "root"}:${order}`;
    if (n.mailbox) seat(n.mailbox, n.label, parentId, order); else roles.push(blank(me, parentId, order, n.label || ""));
    (n.members || []).forEach((m, i) => seat(m, "", me, i));
    (n.children || []).forEach((c, i) => walk(c, me, (n.members || []).length + i));
    return me;
  };
  if (panel.ok && panel.organization) {
    const root = walk(panel.organization, null, 0);
    const lost = boxes.filter((b) => !seen.has(b.name));
    if (lost.length) {   // a valid tree can still forget a mailbox; it must not vanish from the page
      const id = "team:unassigned";
      roles.push(blank(id, root, 1000, "Unassigned mailboxes", { summary: "Registered mailboxes that the organisation tree does not place." }));
      lost.forEach((b, i) => seat(b.name, "", id, i));
    }
  } else {
    degraded.push(panel.error ? `Organization config: ${panel.error}` : "No organization in config.json; mailboxes are listed under one root.");
    roles.push(blank("root", null, 0, panel.label || "Office"));
    boxes.forEach((b, i) => seat(b.name, "", "root", i));
  }
  const mail = [];
  for (const b of boxes) for (const p of b.pending || []) {
    mail.push({ id: `${b.name}/${p.file}`, roleId: "", box: b.name, threadId: null, workId: null, dir: "in", from: p.from || "", to: b.name, subject: p.subject || "",
      at: num(p.mtime), delivery: MAIL[p.status] || "unknown", sourceStatus: typeof p.status === "string" ? p.status : null });
  }
  return normalizeSnapshot({
    contract: CONTRACT_VERSION, asOf: now, rev: 0, consistency: degraded.length ? "degraded" : "consistent", degraded,
    company: { name: panel.label || "", operatorRoleId: panel.operator ? `box:${panel.operator}` : "" },
    capabilities: capabilityMap((f) => ({ status: f.src.master === "live" || f.src.master === "partial" ? f.src.master : "missing", note: f.note ? f.note.en : "" })),
    roles, threads: [], mail, work: [], approvals: [], usage: null,
  });
}

export function createHttpAdapter({ baseUrl = "", fetchImpl = (typeof fetch === "function" ? fetch : null), timeoutMs = 8000, pollMs = 5000 } = {}) {
  const base = String(baseUrl || "").replace(/\/+$/, "");
  const listeners = new Set();
  let timer = null;
  async function getJSON(path) {
    if (!fetchImpl) return fail(ERR.NETWORK, "fetch is not available in this environment.");
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const to = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
    try {
      const res = await fetchImpl(base + path, { signal: ctl ? ctl.signal : undefined });
      if (res.status === 401 || res.status === 403) return fail(ERR.DENIED, `The backend refused the request (${res.status}).`);
      if (!res.ok) return fail(ERR.NETWORK, `The backend answered ${res.status}.`);
      return ok(await res.json());
    } catch (e) {
      return fail(ERR.NETWORK, e && e.name === "AbortError" ? "The backend did not answer in time." : "Cannot reach the backend. Is `postoffice panel` running, and is this page served from the same origin?");
    } finally { if (to) clearTimeout(to); }
  }
  const missing = (what) => async () => fail(ERR.MISSING, `${what} is not offered by the master backend.`);
  return {
    kind: "http", label: base || "this origin", scenario: () => null,
    async getSnapshot() { const r = await getJSON("/api/state"); return r.ok ? ok(mapMasterState(r.data)) : r; },
    subscribe(cb) {
      listeners.add(cb);
      if (!timer && typeof setInterval === "function") timer = setInterval(() => listeners.forEach((f) => f()), pollMs);
      return () => { listeners.delete(cb); if (!listeners.size && timer) { clearInterval(timer); timer = null; } };
    },
    previewRoleChange: missing("Role editing"), applyRoleChange: missing("Role editing"),
    createThread: missing("Starting a thread"), sendToThread: missing("Sending to a thread"), decideApproval: missing("Approvals"),
    stop() { if (timer) clearInterval(timer); timer = null; listeners.clear(); },
  };
}
export const HTTP_FEATURES = FEATURES;
