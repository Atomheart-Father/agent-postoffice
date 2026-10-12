// Mock backend: the same adapter interface as the HTTP one, served from the anonymous fixture. It exists so the
// whole client can be reviewed (and tested) before the real read model is delivered. It is also where every
// failure state can be provoked on purpose, with `scenario`:
//   default | empty | degraded | network | denied | missing | conflict | partial
// Nothing here talks to a Harness: a "created thread" or "sent instruction" is a sample result, and says so.

import { ERR, ok, fail, normalizeSnapshot } from "../contract.js";
import { capabilityMap } from "../capabilities.js";
import { demoSnapshot } from "../fixtures/demo-company.js";
import { clone, deepEqual } from "../util.js";

export const SCENARIOS = ["default", "empty", "degraded", "network", "denied", "missing", "conflict", "partial"];

// What a preview would change. The real backend computes this; the mock does it so the diff step is reviewable.
export function diffRole(role, patch) {
  const changes = [];
  if ("summary" in patch && patch.summary !== role.summary) changes.push({ field: "summary", op: "change", before: role.summary, after: patch.summary });
  if (patch.duties) {
    for (const d of role.duties) if (!patch.duties.includes(d)) changes.push({ field: "duties", op: "remove", before: d });
    for (const d of patch.duties) if (!role.duties.includes(d)) changes.push({ field: "duties", op: "add", after: d });
  }
  if (patch.resources) {
    const key = (r) => `${r.kind}:${r.label}`;
    const old = new Set(role.resources.map(key)), now = new Set(patch.resources.map(key));
    for (const r of role.resources) if (!now.has(key(r))) changes.push({ field: "resources", op: "remove", before: key(r) });
    for (const r of patch.resources) if (!old.has(key(r))) changes.push({ field: "resources", op: "add", after: key(r) });
  }
  if (patch.qualification && patch.qualification.level !== (role.qualification && role.qualification.level))
    changes.push({ field: "qualification", op: "change", before: role.qualification ? String(role.qualification.level) : "—", after: String(patch.qualification.level), prototype: true });
  return changes;
}

const PROJECTIONS = ["Organization tree", "Contact cards", "Identity hints"];

export function createMockAdapter({ scenario = "default", latency = 140, now = () => Date.now() / 1000, live = false } = {}) {
  let sc = SCENARIOS.includes(scenario) ? scenario : "default";
  let data = demoSnapshot(now());
  const listeners = new Set();
  let timer = null, seq = 0;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const emit = () => { for (const f of listeners) { try { f(); } catch { /* a listener must not break the store */ } } };
  const role = (id) => data.roles.find((r) => r.id === id);
  const bump = () => { data.rev += 1; data.asOf = Math.round(now()); };
  const net = () => (sc === "network" ? fail(ERR.NETWORK, "Cannot reach the backend (mock scenario: network).") : null);

  function view() {
    const d = clone(data);
    d.capabilities = capabilityMap((f) => ({ status: sc === "missing" && (f.key === "role.edit" || f.key === "thread.control") ? "missing" : "mock", note: "Sample data from the anonymous fixture." }));
    if (sc === "empty") { d.roles = []; d.threads = []; d.mail = []; d.work = []; d.approvals = []; d.usage = null; d.company.name = "Empty Office"; }
    if (sc === "degraded") {
      d.consistency = "degraded"; d.degraded = ["Presence feed is unavailable, so every status is unknown.", "Usage reader is unavailable."];
      for (const r of d.roles) { if (r.incumbent) r.incumbent.presence = "unknown"; for (const c of r.candidates) c.presence = "unknown"; }
      d.usage = null; for (const t of d.threads) t.usage = null;
    }
    return d;
  }

  const api = {
    kind: "mock", label: "Sample data", scenario: () => sc,
    setScenario(name) { if (SCENARIOS.includes(name)) { sc = name; emit(); } },
    async getSnapshot() { await sleep(latency); const e = net(); if (e) return e; return ok(normalizeSnapshot(view())); },
    subscribe(cb) {
      listeners.add(cb);
      if (live && !timer && typeof setInterval === "function") timer = setInterval(() => api.tick(), 6000);
      return () => { listeners.delete(cb); if (!listeners.size && timer) { clearInterval(timer); timer = null; } };
    },
    // advance the sample world by one event: a new line in the scan thread, dropping the oldest past the buffer limit
    tick() {
      const t = data.threads.find((x) => x.id === "t-scan"); if (!t) return;
      const last = t.events[t.events.length - 1].seq + 1;
      t.events.push({ seq: last, at: Math.round(now()), kind: last % 3 ? "tool" : "text", who: "Lead agent", text: last % 3 ? `search: sample query ${last}` : `Note ${last}: figures checked against notes/pricing.md.` });
      while (t.events.length > t.bufferLimit) { t.events.shift(); t.droppedBefore += 1; }
      t.updatedAt = Math.round(now()); bump(); emit();
    },

    async previewRoleChange(roleId, patch, baseRev) {
      await sleep(latency); const e = net(); if (e) return e;
      if (sc === "missing") return fail(ERR.MISSING, "This backend does not offer role editing.");
      const r = role(roleId); if (!r) return fail(ERR.INVALID, `No role ${roleId}.`);
      if (sc === "denied" || !(r.can && r.can.edit && r.can.edit.allowed)) return fail(ERR.DENIED, (r.can && r.can.edit && r.can.edit.reason) || "You lack the capability role.edit for this role.");
      if (baseRev !== r.rev) return fail(ERR.STALE, "The role changed after you opened it.", { current: { rev: r.rev, duties: r.duties, resources: r.resources, summary: r.summary } });
      const changes = diffRole(r, patch);
      const warnings = [];
      if (patch.duties && !patch.duties.length) warnings.push("The role would have no duties.");
      if (patch.resources && r.resources.length && !patch.resources.length) warnings.push("The role would lose every resource, so its incumbent could not act.");
      return ok({ roleId, baseRev, changes, warnings, projections: PROJECTIONS.map((name) => ({ name })), token: `mock:${roleId}:${baseRev}:${JSON.stringify(patch).length}:${changes.length}` });
    },

    async applyRoleChange(roleId, patch, baseRev, token) {
      await sleep(latency); const e = net(); if (e) return e;
      const r = role(roleId); if (!r) return fail(ERR.INVALID, `No role ${roleId}.`);
      if (sc === "denied") return fail(ERR.DENIED, "You lack the capability role.edit for this role.");
      if (sc === "conflict") {   // someone else changed the role between preview and confirm
        r.duties = [...r.duties, "Added by someone else while you were editing"]; r.rev += 1; bump(); emit();
        return fail(ERR.STALE, "The role changed after you previewed.", { current: { rev: r.rev, duties: r.duties, resources: r.resources, summary: r.summary } });
      }
      if (baseRev !== r.rev) return fail(ERR.STALE, "The role changed after you previewed.", { current: { rev: r.rev, duties: r.duties, resources: r.resources, summary: r.summary } });
      if (typeof token !== "string" || !token.startsWith(`mock:${roleId}:${baseRev}:`)) return fail(ERR.INVALID, "The preview does not match this change; preview again.");
      if (patch.summary != null) r.summary = patch.summary;
      if (patch.duties) r.duties = [...patch.duties];
      if (patch.resources) r.resources = patch.resources.map((x) => ({ ...x }));
      if (patch.qualification) r.qualification = { level: patch.qualification.level, note: (r.qualification && r.qualification.note) || "" };
      r.rev += 1; bump(); emit();
      const projections = PROJECTIONS.map((name) => ({ name, ok: !(sc === "partial" && name === "Contact cards"), error: sc === "partial" && name === "Contact cards" ? "contacts/ could not be written (disk is read-only)" : "" }));
      const failed = projections.some((p) => !p.ok);
      return ok({ roleId, state: failed ? "partial" : "applied", rev: r.rev, projections });
    },

    async retryProjections(roleId, names) {
      await sleep(latency); const e = net(); if (e) return e;
      sc = sc === "partial" ? "default" : sc;   // the sample problem is fixed once retried
      emit();
      return ok({ roleId, state: "applied", projections: names.map((name) => ({ name, ok: true, error: "" })) });
    },

    async createThread(req) {
      await sleep(latency); const e = net(); if (e) return e;
      if (sc === "missing") return fail(ERR.MISSING, "This backend cannot start threads.");
      if (sc === "denied") return fail(ERR.DENIED, "You lack the capability thread.create for this role.");
      const r = role(req.roleId); if (!r) return fail(ERR.INVALID, `No role ${req.roleId}.`);
      const id = `ap-new-${++seq}`;
      data.approvals.push({ id, kind: "thread_create", roleId: req.roleId, title: `Start thread "${req.title}" for ${r.title} on ${req.harness || "any Harness"}`, at: Math.round(now()), state: "pending" });
      bump(); emit();
      return ok({ state: ERR.PENDING, approvalId: id, mock: true, note: "A sample approval was added to Work. Nothing was started on any Harness." });
    },

    async sendToThread(threadId, text) {
      await sleep(latency); const e = net(); if (e) return e;
      const t = data.threads.find((x) => x.id === threadId); if (!t) return fail(ERR.INVALID, `No thread ${threadId}.`);
      if (t.control !== "controllable") return fail(t.control === "pending_approval" ? ERR.PENDING : t.control === "capability_missing" ? ERR.MISSING : t.control === "stale" ? ERR.STALE : ERR.DENIED, t.controlReason || "This thread does not accept input.");
      const last = t.events.length ? t.events[t.events.length - 1].seq : 0;
      t.events.push({ seq: last + 1, at: Math.round(now()), kind: "text", who: "You", text, delivery: "accepted" });
      while (t.events.length > t.bufferLimit) { t.events.shift(); t.droppedBefore += 1; }
      t.updatedAt = Math.round(now()); bump(); emit();
      return ok({ state: "accepted", mock: true, note: "Accepted by Office. The session has not consumed it; nothing reached a Harness." });
    },

    async decideApproval(id, decision) {
      await sleep(latency); const e = net(); if (e) return e;
      const a = data.approvals.find((x) => x.id === id); if (!a) return fail(ERR.INVALID, `No approval ${id}.`);
      if (sc === "denied") return fail(ERR.DENIED, "You are not an approver for this request.");
      if (a.state !== "pending") return fail(ERR.STALE, "This request was already decided.");
      a.state = decision === "approved" ? "approved" : "denied";
      if (a.kind === "thread_bind" && a.state === "approved") { const t = data.threads.find((x) => x.roleId === a.roleId && x.control === "pending_approval"); if (t) { t.control = "controllable"; t.controlReason = ""; } }
      bump(); emit();
      return ok({ id, state: a.state, mock: true });
    },

    sameData(other) { return deepEqual(data, other); },
    stop() { if (timer) clearInterval(timer); timer = null; listeners.clear(); },
  };
  return api;
}
