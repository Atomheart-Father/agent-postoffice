// The data contract between the client and any backend. DRAFT: this is the shape the client is built against;
// the local backend's versioned read_model replaces it when the interface pack is delivered (see docs/CLIENT.md).
// Rules the whole client follows:
//   * every enum has an explicit "unknown" / "missing" member -- an unrecognised value is shown as unknown,
//     never guessed and never coerced to a friendly default;
//   * numbers are number | null; null means "no reliable value" and is never drawn as 0;
//   * the client holds no authority: it shows what the snapshot says and submits requests; the backend re-checks.

import { isNum } from "./util.js";

export const CONTRACT_VERSION = "office-readmodel/0-draft";

// Presence of a role's incumbent. "unconnected" = a person/session is named but no Harness is attached to it;
// "offline" = it was attached and is not now; "power_save" = held idle on purpose; "unknown" = no fresh signal.
export const PRESENCE = ["connected", "busy", "power_save", "offline", "unconnected", "unknown"];
// What the client may do with a thread. Observation is not control: only "controllable" enables input, and
// only because the backend said so for this thread right now.
export const CONTROL = ["controllable", "observe_only", "pending_approval", "denied", "capability_missing", "stale", "unknown"];
// Mail delivery. "accepted" means the mailbox took it -- it says nothing about the work.
export const DELIVERY = ["sent", "accepted", "consumed", "failed", "retracted", "unknown"];
// Work is one generic state model; it is not specific to any industry.
export const WORK_STATES = ["open", "submitted", "verified", "accepted", "returned", "released"];
export const KINDS = ["team", "position"];                 // a team groups roles; a position is held by an incumbent
export const OCCUPANT = ["human", "agent", "unspecified"];
export const CAP_STATUS = ["live", "partial", "mock", "missing"];
export const CONSISTENCY = ["consistent", "degraded"];

// Error codes every adapter call may return. The UI has one honest state per code.
export const ERR = {
  DENIED: "denied",                 // the backend refused: the actor lacks the capability
  STALE: "stale",                   // the thing changed since the client last read it (revision mismatch)
  MISSING: "capability_missing",    // this backend does not offer the operation at all
  PENDING: "pending_approval",      // accepted but waits for an approver; not done
  DEGRADED: "degraded",             // part of the read model could not be produced
  NETWORK: "network",               // could not reach the backend
  INVALID: "invalid",               // the request or the reply was malformed
};

export const ok = (data, extra = {}) => ({ ok: true, data, ...extra });
export const fail = (code, message = "", extra = {}) => ({ ok: false, error: { code, message, retryable: code === ERR.NETWORK || code === ERR.STALE, ...extra } });

const pick = (v, allowed, dflt = "unknown") => (allowed.includes(v) ? v : dflt);
const arr = (v) => (Array.isArray(v) ? v : []);
const str = (v, d = "") => (typeof v === "string" ? v : d);
const numOrNull = (v) => (isNum(v) ? v : null);

export function normalizeUsage(u) {
  if (!u || typeof u !== "object") return null;
  return {
    input: numOrNull(u.input), output: numOrNull(u.output),
    cacheRead: numOrNull(u.cacheRead), cacheWrite: numOrNull(u.cacheWrite),
    total: numOrNull(u.total),                             // as reported by the official source; never summed here
    model: str(u.model) || null, updatedAt: numOrNull(u.updatedAt), source: str(u.source) || null,
    coverage: pick(u.coverage, ["complete", "partial", "none"], "none"),
    context: u.context && isNum(u.context.tokens) && isNum(u.context.window) && u.context.window > 0
      ? { tokens: u.context.tokens, window: u.context.window } : null,   // occupancy is a separate fact from cumulative use
  };
}

export function normalizeOccupant(o) {
  if (!o || typeof o !== "object") return null;
  return {
    id: str(o.id), label: str(o.label || o.id), kind: pick(o.kind, OCCUPANT, "unspecified"),
    harness: str(o.harness) || null, model: str(o.model) || null,
    presence: pick(o.presence, PRESENCE), presenceAt: numOrNull(o.presenceAt),
    note: str(o.note),
  };
}

export function normalizeRole(r) {
  const can = {};
  for (const [k, v] of Object.entries(r.can || {})) can[k] = { allowed: !!(v && v.allowed), reason: str(v && v.reason) };
  return {
    id: str(r.id), parentId: r.parentId == null ? null : str(r.parentId), order: numOrNull(r.order) ?? 0,
    kind: pick(r.kind, KINDS, "position"), title: str(r.title || r.id), summary: str(r.summary),
    duties: arr(r.duties).map(String), resources: arr(r.resources).map((x) => ({ kind: str(x && x.kind, "resource"), label: str(x && x.label) })).filter((x) => x.label),
    qualification: r.qualification && typeof r.qualification === "object" ? { level: numOrNull(r.qualification.level), note: str(r.qualification.note) } : null,
    incumbent: normalizeOccupant(r.incumbent), candidates: arr(r.candidates).map(normalizeOccupant).filter(Boolean),
    waiting: numOrNull(r.waiting) ?? 0, rev: numOrNull(r.rev), can,
  };
}

export function normalizeThread(t) {
  return {
    id: str(t.id), roleId: str(t.roleId), title: str(t.title || t.id), harness: str(t.harness) || null, model: str(t.model) || null,
    source: pick(t.source, ["office", "observed"], "observed"),
    control: pick(t.control, CONTROL), controlReason: str(t.controlReason),
    status: pick(t.status, ["active", "idle", "waiting", "ended"], "idle"), updatedAt: numOrNull(t.updatedAt),
    events: arr(t.events).map((e) => ({ seq: numOrNull(e.seq) ?? 0, at: numOrNull(e.at), kind: pick(e.kind, ["text", "tool", "status"], "text"), who: str(e.who), text: str(e.text),
      delivery: e.delivery ? pick(e.delivery, DELIVERY) : null })),
    bufferLimit: numOrNull(t.bufferLimit), droppedBefore: numOrNull(t.droppedBefore) ?? 0,
    usage: normalizeUsage(t.usage),
  };
}

export function normalizeSnapshot(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  const caps = {};
  for (const [k, v] of Object.entries(s.capabilities || {})) caps[k] = { status: pick(v && v.status, CAP_STATUS, "missing"), note: str(v && v.note) };
  return {
    contract: str(s.contract, CONTRACT_VERSION), asOf: numOrNull(s.asOf), rev: numOrNull(s.rev) ?? 0,
    consistency: pick(s.consistency, CONSISTENCY, "degraded"), degraded: arr(s.degraded).map(String),
    company: { name: str(s.company && s.company.name), operatorRoleId: str(s.company && s.company.operatorRoleId) },
    capabilities: caps,
    roles: arr(s.roles).map(normalizeRole),
    threads: arr(s.threads).map(normalizeThread),
    mail: arr(s.mail).map((m) => ({ id: str(m.id), roleId: str(m.roleId), threadId: str(m.threadId) || null, workId: str(m.workId) || null, dir: pick(m.dir, ["in", "out"], "in"),
      from: str(m.from), to: str(m.to), subject: str(m.subject), at: numOrNull(m.at), delivery: pick(m.delivery, DELIVERY) })),
    work: arr(s.work).map((w) => ({ id: str(w.id), roleId: str(w.roleId), title: str(w.title), state: pick(w.state, WORK_STATES, "open"), difficulty: numOrNull(w.difficulty),
      updatedAt: numOrNull(w.updatedAt), note: str(w.note), history: arr(w.history).map((h) => ({ state: pick(h.state, WORK_STATES, "open"), at: numOrNull(h.at), by: str(h.by) })) })),
    approvals: arr(s.approvals).map((a) => ({ id: str(a.id), kind: str(a.kind), roleId: str(a.roleId), title: str(a.title), at: numOrNull(a.at), state: pick(a.state, ["pending", "approved", "denied"], "pending") })),
    usage: normalizeUsage(s.usage),
  };
}

// For tests and the dev console: structural problems in a snapshot that the UI cannot render honestly.
export function validateSnapshot(s) {
  const p = [];
  const ids = new Set();
  for (const r of s.roles) {
    if (!r.id) p.push("role without id");
    if (ids.has(r.id)) p.push(`duplicate role id ${r.id}`);
    ids.add(r.id);
  }
  for (const r of s.roles) if (r.parentId && !ids.has(r.parentId)) p.push(`role ${r.id}: parent ${r.parentId} not found`);
  if (s.roles.filter((r) => !r.parentId).length !== 1 && s.roles.length) p.push("expected exactly one root role");
  for (const t of s.threads) if (t.roleId && !ids.has(t.roleId)) p.push(`thread ${t.id}: role ${t.roleId} not found`);
  for (const w of s.work) if (w.roleId && !ids.has(w.roleId)) p.push(`work ${w.id}: role ${w.roleId} not found`);
  return p;
}
