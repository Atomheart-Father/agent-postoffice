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
// Mail delivery, kept as the backend states it. Four different facts, never merged into one "ok":
//   waiting   = the letter is on disk in a mailbox and nothing has surfaced it yet
//   reminded  = a reminder was shown for it; still not read by the session
//   accepted  = a channel (e.g. an Office thread) took it; the session has not consumed it
//   consumed  = the receiving session took it
// None of them says the work is done. An unrecognised source state stays "unknown", never "accepted".
export const DELIVERY = ["sent", "waiting", "reminded", "accepted", "consumed", "failed", "retracted", "unknown"];
// Work is one generic state model; it is not specific to any industry.
export const WORK_STATES = ["open", "submitted", "verified", "accepted", "returned", "released"];
export const KINDS = ["team", "position"];                 // a team groups roles; a position is held by an incumbent
export const OCCUPANT = ["human", "agent", "unspecified"];
// live = wired and checked against a running backend; fixture = wired in code, checked against the backend's written
// fixtures only (no running backend of that kind was available); partial = only part of it exists; mock = sample data; missing = none.
export const CAP_STATUS = ["live", "fixture", "partial", "mock", "missing"];
export const CONSISTENCY = ["consistent", "degraded"];

// Error codes every adapter call may return. The UI has one honest state per code.
export const ERR = {
  DENIED: "denied",                 // the backend refused: the actor lacks the capability
  STALE: "stale",                   // the thing changed since the client last read it (revision mismatch)
  MISSING: "capability_missing",    // this backend does not offer the operation at all
  PENDING: "pending_approval",      // accepted but waits for an approver; not done
  DEGRADED: "degraded",             // part of the read model could not be produced
  NETWORK: "network",               // could not reach the backend
  INVALID: "invalid",               // the request or the reply was malformed (HTTP 400: nothing was saved)
  NOT_FOUND: "not_found",           // the named object does not exist (HTTP 404)
  SERVER: "server",                 // the backend failed before it could save (HTTP 500, e.g. backup failed)
  REFUSED: "refused",               // the backend understood and said no, with its own reason (a rule, not a fault)
  CONFLICT: "conflict",             // the target is in a state that blocks the move (e.g. the same id already filed)
};
// Resource kinds the role-edit contract accepts today, and its limits. Checked in the form only to save a round
// trip; the backend validates again and its verdict wins.
export const RESOURCE_KINDS = ["doc", "dir", "skill", "url"];
export const LIMITS = { duties: 5, dutyChars: 80, resources: 8, refChars: 200, noteChars: 60 };

export const ok = (data, extra = {}) => ({ ok: true, data, ...extra });
export const fail = (code, message = "", extra = {}) => ({ ok: false, error: { code, message, retryable: code === ERR.NETWORK || code === ERR.STALE, ...extra } });
// A write whose reply never arrived: the request may or may not have been applied. Never retried automatically; the UI
// says "outcome unknown" and tells the user where to look (a fresh read), instead of "failed".
export const unknownOutcome = (message) => fail(ERR.NETWORK, message, { outcomeUnknown: true, retryable: false });

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
    // Separate facts, never folded into presence: the delivery switch (on = letters may be delivered; off may be a
    // manual choice, not a broken Harness), the session binding (kind only; null = none known) and what it last did.
    switch: pick(o.switch, ["on", "off", "unknown"]), binding: str(o.binding) || null, activity: normalizeActivity(o.activity),
    // the backend's own state code for this person as a candidate (e.g. online_notify, unregistered), shown as written;
    // registered=false means the name is not a known mailbox (null = not said)
    state: str(o.state) || null, registered: o.registered === true ? true : o.registered === false ? false : null,
    note: str(o.note),
  };
}

export function normalizeRole(r) {
  const can = {};
  for (const [k, v] of Object.entries(r.can || {})) can[k] = { allowed: !!(v && v.allowed), reason: str(v && v.reason) };
  return {
    id: str(r.id), parentId: r.parentId == null ? null : str(r.parentId), order: numOrNull(r.order) ?? 0,
    kind: pick(r.kind, KINDS, "position"), title: str(r.title || r.id), summary: str(r.summary),
    duties: arr(r.duties).map(String),
    // `exists` = the backend checked whether the referenced path is there (true/false); null = it did not say
    resources: arr(r.resources).map((x) => ({ kind: str(x && x.kind, "resource"), ref: str(x && (x.ref != null ? x.ref : x.label)), note: str(x && x.note), exists: x && x.exists === true ? true : x && x.exists === false ? false : null })).filter((x) => x.ref),
    qualification: r.qualification && typeof r.qualification === "object" ? { level: numOrNull(r.qualification.level), note: str(r.qualification.note) } : null,
    // incumbent = who holds it now; candidates = the ordered list of who may hold it (the incumbent is usually one of them)
    incumbent: normalizeOccupant(r.incumbent), candidates: arr(r.candidates).map(normalizeOccupant).filter(Boolean),
    // the position this one escalates to: "" = none, null = the backend did not say (never treated as "none")
    escalationRole: typeof r.escalationRole === "string" ? r.escalationRole : null,
    escalationAcceptor: typeof r.escalationAcceptor === "string" ? r.escalationAcceptor : null,
    // as the backend wrote them, never interpreted: the title of the escalation role, the mailbox it currently resolves to,
    // the holder's state code, the backend's `confirmed` field, its config check and the status file's freshness
    escalationTitle: typeof r.escalationTitle === "string" ? r.escalationTitle : null,
    escalationTarget: typeof r.escalationTarget === "string" ? r.escalationTarget : null,
    holderState: str(r.holderState) || null, roleKind: str(r.roleKind) || null, project: str(r.project),
    confirmed: r.confirmed === undefined ? undefined : r.confirmed,
    configOk: r.configOk === true ? true : r.configOk === false ? false : null,
    statusFile: r.statusFile && typeof r.statusFile === "object" ? { pathExists: r.statusFile.pathExists === true ? true : r.statusFile.pathExists === false ? false : null, updated: str(r.statusFile.updated) || null } : null,
    // origin "mailbox" = a legacy node standing for one mailbox, not a position: its id is not a stable role id and
    // it cannot be edited as a role. `box` is that mailbox.
    origin: pick(r.origin, ["role", "mailbox"], "role"), box: str(r.box),
    waiting: numOrNull(r.waiting) ?? 0,
    // `can` is filled only by a backend that sends per-role rights. A backend that does not (the local candidate) leaves it
    // empty: that means "the read model says nothing", not "allowed" and not "denied"; the backend decides at write time.
    can,
  };
}

// What an occupant is doing, with when and from where it was said. Missing = unknown, never "idle".
export function normalizeActivity(a) {
  if (!a || typeof a !== "object") return null;
  const text = str(a.text || a.state);
  if (!text) return null;
  return { text, at: numOrNull(a.at), source: str(a.source) || null };
}

// Candidates other than the one holding the role.
export const standbys = (r) => r.candidates.filter((c) => !r.incumbent || c.id !== r.incumbent.id);

export function normalizeAddress(a) {
  const x = a && typeof a === "object" ? a : {};
  const kind = pick(x.kind, ["box", "alias"], "box");
  return { kind, name: str(x.name), label: str(x.label), target: str(x.target) || null, online: x.online === true ? true : x.online === false ? false : null, error: str(x.error) };
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
    contract: str(s.contract, CONTRACT_VERSION), source: pick(s.source, ["master", "candidate", "mock"], "mock"), asOf: numOrNull(s.asOf), rev: numOrNull(s.rev) ?? 0,
    consistency: pick(s.consistency, CONSISTENCY, "degraded"), degraded: arr(s.degraded).map(String),
    // operatorBox = the mailbox the backend says the human operator writes from ("" = not configured, so nothing may be sent as them)
    company: { name: str(s.company && s.company.name), operatorRoleId: str(s.company && s.company.operatorRoleId), operatorBox: str(s.company && s.company.operatorBox) },
    // Places a letter can be addressed to, as the backend lists them: mailboxes and logical addresses (@name). A logical
    // address is not a role card and a mailbox is not a role; the list only feeds the "To" suggestions.
    addresses: arr(s.addresses).map(normalizeAddress),
    capabilities: caps,
    roles: arr(s.roles).map(normalizeRole),
    threads: arr(s.threads).map(normalizeThread),
    // roleId is empty when the backend does not record which role a letter was for; `box` is the mailbox it sits in.
    mail: arr(s.mail).map((m) => ({ id: str(m.id), letterId: str(m.letterId), roleId: str(m.roleId), box: str(m.box), threadId: str(m.threadId) || null, workId: str(m.workId) || null, dir: pick(m.dir, ["in", "out"], "in"),
      from: str(m.from), to: str(m.to), subject: str(m.subject), at: numOrNull(m.at), delivery: pick(m.delivery, DELIVERY), sourceStatus: str(m.sourceStatus) || null,
      kind: str(m.kind) || null })),   // the backend's own word for the letter's kind (formal, notice, receipt, alarm ...), shown as written, never interpreted
    // Delivery groups as the backend lists them (name without "@"). counts are the backend's; nothing is summed here.
    groups: arr(s.groups).filter((g) => g && typeof g.name === "string" && g.name).map((g) => ({ name: g.name, members: arr(g.members).map(String), total: numOrNull(g.total), online: numOrNull(g.online) })),
    work: arr(s.work).map((w) => ({ id: str(w.id), roleId: str(w.roleId), title: str(w.title), state: pick(w.state, WORK_STATES, "open"), difficulty: numOrNull(w.difficulty),
      updatedAt: numOrNull(w.updatedAt), note: str(w.note), history: arr(w.history).map((h) => ({ state: pick(h.state, WORK_STATES, "open"), at: numOrNull(h.at), by: str(h.by) })) })),
    approvals: arr(s.approvals).map((a) => ({ id: str(a.id), kind: str(a.kind), roleId: str(a.roleId), title: str(a.title), at: numOrNull(a.at), state: pick(a.state, ["pending", "approved", "denied"], "pending") })),
    usage: normalizeUsage(s.usage),
  };
}

// ---- role-diff / role-apply replies ----
// The reply shapes are the backend's. Inner shapes the client has no fixture for yet (what exactly `expected` and
// `facts` contain) are shown as the backend wrote them: strings stay strings, anything else is printed compactly.
// Nothing is derived from them and nothing is invented to fill a gap.
const text = (v) => (v == null ? "" : typeof v === "string" ? v : typeof v === "number" || typeof v === "boolean" ? String(v) : JSON.stringify(v));
export const lineList = (v) => (Array.isArray(v) ? v.map(text).filter(Boolean)
  : v && typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k}: ${text(x)}`)
  : v == null || v === "" ? [] : [text(v)]);

// role-diff (e819f0e, fixture role_diff_ok_response.json): change{ <field>_from, <field>_to ... } always lists every field,
// changed or not; the client compares the pairs itself. A key that is neither a pair nor a known annotation is kept and
// shown raw, so a newer backend cannot be silently dropped.
const FIELD_NAMES = ["candidates", "escalation", "duties", "resources"];
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
// expected / facts: objects of scalars (and one list of {name,state}). Kept key by key, in the backend's words.
const kv = (v) => {
  if (v == null) return [];
  if (typeof v !== "object" || Array.isArray(v)) return [{ k: "", v: lineList(v).join(" · ") }];
  return Object.entries(v).map(([k, x]) => ({ k, v: k === "candidates" && Array.isArray(x) ? x.map((c) => (c && typeof c === "object" ? `${text(c.name)} (${text(c.state)})` : text(c))).join(", ") : x === null || typeof x === "boolean" ? x : text(x) }));
};
export function normalizeDiff(raw) {
  const d = raw && typeof raw === "object" ? raw : {};
  const ch = d.change && typeof d.change === "object" ? d.change : {};   // an array or any other shape is not the pack shape: its entries are kept raw
  const changes = [], used = new Set();
  const prefixes = Object.keys(ch).filter((k) => k.endsWith("_from") && (k.slice(0, -5) + "_to") in ch).map((k) => k.slice(0, -5));
  for (const f of [...FIELD_NAMES.filter((x) => prefixes.includes(x)), ...prefixes.filter((x) => !FIELD_NAMES.includes(x))]) {
    used.add(f + "_from"); used.add(f + "_to");
    const extra = Object.keys(ch).filter((k) => k.startsWith(f + "_to_"));
    extra.forEach((k) => used.add(k));
    if (!same(ch[f + "_from"], ch[f + "_to"])) changes.push({ field: f, from: ch[f + "_from"], to: ch[f + "_to"], notes: extra.map((k) => ({ k: k.slice(f.length + 4), v: text(ch[k]) })).filter((n) => n.v) });
  }
  for (const [k, v] of Object.entries(ch)) if (!used.has(k)) changes.push({ field: k, from: undefined, to: v, raw: true });
  // `baseline` is opaque: the client hands it back to apply unchanged and never makes one up. Without it nothing can be applied.
  return { name: str(d.name), baseline: typeof d.baseline === "string" ? d.baseline : "", changes, expected: kv(d.expected), facts: kv(d.facts) };
}

export const PROJECTION = ["fresh", "pending", "not_applicable"];
// A successful apply. `saved` is the authoritative config; `projection` is the derived cards, a separate fact.
export function normalizeApplied(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  return { saved: r.saved === true, projection: pick(r.projection, PROJECTION), projectionRaw: typeof r.projection === "string" ? r.projection : null, message: str(r.message), backup: str(r.backup) || null,
    activeNow: lineList(r.active_now != null ? r.active_now : r.activeNow), acceptorNow: lineList(r.acceptor_now != null ? r.acceptor_now : r.acceptorNow) };
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

// ---- mail operations (master /api/letter, /api/outbox, /api/send, /api/ack-one, /api/archive-one, /api/edit-one, /api/retract-one) ----
// One letter, as read from the recipient's inbox. `sender` is the canonical mailbox name from the letter header.
export function normalizeLetter(raw) {
  const d = raw && typeof raw === "object" ? raw : {};
  return { id: str(d.id), box: str(d.box), sender: str(d.sender), subject: str(d.subject), need: str(d.need), body: str(d.body) };
}
// The operator's still-observable sent mail. status is the backend's own judgement of whether edit/retract can still
// work: pending = nothing took it yet, locked = a channel accepted it or a delivery is in flight, unknown = it cannot
// tell. Even "pending" can lose a race; the POST decides.
export const OUTBOX_STATUS = ["pending", "locked", "unknown"];
export function normalizeOutboxRow(raw) {
  const d = raw && typeof raw === "object" ? raw : {};
  return { to: str(d.to), id: str(d.id), ref: str(d.ref) || `${str(d.to)}/${str(d.id)}`, subject: str(d.subject), need: str(d.need), body: str(d.body), alias: str(d.alias),
    at: numOrNull(d.mtime != null ? d.mtime : d.at), status: pick(d.status, OUTBOX_STATUS), reason: str(d.reason) };
}
// What each write answered. `state` is the backend's word and the view picks the sentence from it; an unrecognised
// state stays unrecognised, it is not turned into success wording.
export const MAIL_STATES = {
  send: ["sent"], ack: ["recorded", "already", "receipt_notice"], archive: ["moved", "already"], edit: ["updated"], retract: ["retracted"],
};
export function normalizeMailResult(kind, raw) {
  const d = raw && typeof raw === "object" ? raw : {};
  const state = kind === "send" ? "sent" : pick(d.state, MAIL_STATES[kind] || []);
  const ref = str(d.ref);
  // deduped: the backend says this request repeated an earlier one with the same key, and id / ref then name the ORIGINAL letter.
  // Only a literal true counts; nothing is inferred.
  return { kind, state, id: str(d.id), to: str(d.to) || (ref.includes("/") ? ref.split("/")[0] : ""), ref, deduped: d.deduped === true };
}
