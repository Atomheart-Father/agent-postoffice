// HTTP adapter for the published R0 backend (the panel API served by `postoffice panel`).
// It maps what R0 really returns into the client's read model and says plainly what it cannot map:
//   GET /api/state   -> company, role tree (panel.organization), presence (online flag only), waiting counts, inbox mail
// Everything else (role detail, threads, work, approvals, usage, role edit, thread control) is reported as missing;
// calls for them return capability_missing instead of guessing a payload. The unpublished role-diff / role-apply
// routes are deliberately NOT called: their payloads are not in the handoff, so any request body would be a guess.
// Auth and CORS are not decided yet (docs/CLIENT.md): this adapter sends no credentials and works against a panel
// on the same origin, or one that already allows the client's origin.

import { ERR, ok, fail, normalizeSnapshot, CONTRACT_VERSION } from "../contract.js";
import { capabilityMap, FEATURES } from "../capabilities.js";

const LIVE = new Set(["office.tree", "role.incumbent", "mail.list"]);

// R0 /api/state -> snapshot. Pure so it can be tested against fixtures.
export function mapR0State(st, now = Date.now() / 1000) {
  const boxes = Array.isArray(st && st.boxes) ? st.boxes : [];
  const box = (n) => boxes.find((b) => b.name === n);
  const panel = (st && st.panel) || {};
  const roles = [];
  const degraded = [];
  const seat = (name, label, parentId, order) => {
    const b = box(name);
    roles.push({
      id: `mb:${name}`, parentId, order, kind: "position", title: label || name, summary: "", duties: [], resources: [],
      incumbent: b ? { id: name, label: (b.identity && b.identity.profile && b.identity.profile.display_name) || b.who || name, kind: b.method === "manual" || b.app === "Manual" ? "human" : "agent",
        harness: b.app || null, model: null, presence: b.online ? "connected" : "offline", presenceAt: null } : null,
      candidates: [], waiting: b && b.counts ? b.counts.waiting || 0 : 0, rev: null, can: {},
    });
  };
  const walk = (n, parentId, order) => {
    if (!n) return;
    if (n.mailbox) seat(n.mailbox, n.label, parentId, order);
    else roles.push({ id: `team:${parentId}:${order}`, parentId, order, kind: "team", title: n.label || "", summary: "", duties: [], resources: [], incumbent: null, candidates: [], waiting: 0, rev: null, can: {} });
    const me = n.mailbox ? `mb:${n.mailbox}` : `team:${parentId}:${order}`;
    (n.members || []).forEach((m, i) => seat(m, "", me, i));
    (n.children || []).forEach((c, i) => walk(c, me, (n.members || []).length + i));
  };
  if (panel.ok && panel.organization) walk(panel.organization, null, 0);
  else {
    degraded.push(panel.error ? `Organization config: ${panel.error}` : "No organization in config.json; mailboxes are listed under one root.");
    roles.push({ id: "root", parentId: null, order: 0, kind: "team", title: panel.label || "Office", summary: "", duties: [], resources: [], incumbent: null, candidates: [], waiting: 0, rev: null, can: {} });
    boxes.forEach((b, i) => seat(b.name, "", "root", i));
  }
  const mail = [];
  for (const b of boxes) for (const p of b.pending || []) {
    mail.push({ id: `${b.name}/${p.file}`, roleId: `mb:${b.name}`, threadId: null, workId: null, dir: "in", from: p.from || "", to: b.name, subject: p.subject || "",
      at: typeof p.mtime === "number" ? p.mtime : null, delivery: p.status === "failed" ? "failed" : "accepted" });
  }
  return normalizeSnapshot({
    contract: CONTRACT_VERSION, asOf: now, rev: 0, consistency: degraded.length ? "degraded" : "consistent", degraded,
    company: { name: panel.label || "", operatorRoleId: panel.operator ? `mb:${panel.operator}` : "" },
    capabilities: capabilityMap((f) => ({ status: LIVE.has(f.key) ? "partial" : "missing", note: f.note ? f.note.en : "" })),
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
      return fail(ERR.NETWORK, e && e.name === "AbortError" ? "The backend did not answer in time." : "Cannot reach the backend. Is `postoffice panel` running, and does it allow this origin?");
    } finally { if (to) clearTimeout(to); }
  }
  const missing = (what) => async () => fail(ERR.MISSING, `${what} is not offered by the R0 backend.`);
  return {
    kind: "http", label: base || "this origin", scenario: () => null,
    async getSnapshot() { const r = await getJSON("/api/state"); return r.ok ? ok(mapR0State(r.data)) : r; },
    subscribe(cb) {
      listeners.add(cb);
      if (!timer && typeof setInterval === "function") timer = setInterval(() => listeners.forEach((f) => f()), pollMs);
      return () => { listeners.delete(cb); if (!listeners.size && timer) { clearInterval(timer); timer = null; } };
    },
    previewRoleChange: missing("Role editing"), applyRoleChange: missing("Role editing"), retryProjections: missing("Role editing"),
    createThread: missing("Starting a thread"), sendToThread: missing("Sending to a thread"), decideApproval: missing("Approvals"),
    stop() { if (timer) clearInterval(timer); timer = null; listeners.clear(); },
  };
}
export const HTTP_FEATURES = FEATURES;
