// Mock backend: the same adapter interface as the HTTP one, served from the anonymous fixture. It exists so the
// whole client can be reviewed (and tested) before the real read model is delivered. It is also where every
// failure state can be provoked on purpose, with `scenario`:
//   default | empty | degraded | network | denied | missing | conflict | projection | lostreply
//   (conflict = the config moves once between your preview and your apply; projection = saved, derived cards still pending;
//    lostreply = every WRITE is applied but its reply is lost, so the client must say "outcome unknown")
// Nothing here talks to a Harness: a "created thread" or "sent instruction" is a sample result, and says so.

import { ERR, ok, fail, unknownOutcome, normalizeSnapshot, normalizeDiff, normalizeApplied, normalizeLetter, normalizeOutboxRow, normalizeMailResult } from "../contract.js";
import { capabilityMap } from "../capabilities.js";
import { demoSnapshot } from "../fixtures/demo-company.js";
import { clone, deepEqual } from "../util.js";
import { baseOf, validateDraft } from "../flows/roleEdit.js";

export const SCENARIOS = ["default", "empty", "degraded", "network", "denied", "missing", "conflict", "projection", "lostreply"];

// A stand-in for the backend's whole-config SHA-256: any change to any role changes it, which is exactly the
// property the client has to cope with (a baseline goes stale when ANYTHING in the config moved, not only this role).
// Not a cryptographic hash; the client treats the value as opaque and only hands it back.
export function mockBaseline(roles) {
  const str = JSON.stringify(roles.filter((r) => r.kind === "position").map((r) => [r.id, baseOf(r)]));
  let out = "";
  for (let k = 0; k < 8; k++) {
    let h = (0x811c9dc5 ^ (k * 0x9e3779b1)) >>> 0;
    for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    out += h.toString(16).padStart(8, "0");
  }
  return out;
}

// The contract's validation, run on the mock's side exactly as the flow runs it on the client's.
function badRequest(r, req) {
  if (r.kind !== "position") return "Only a position can be edited.";
  if (!req || !Array.isArray(req.candidates) || !req.candidates.length) return "candidates must not be empty.";
  if (req.escalation_role !== undefined && typeof req.escalation_role !== "string") return "escalation_role must be a string (empty for none).";
  const es = { roleId: r.id, base: baseOf(r), text: { candidates: req.candidates.join("\n"), escalation: req.escalation_role === undefined ? baseOf(r).escalation || "" : req.escalation_role, duties: (req.duties || baseOf(r).duties).join("\n"),
    resources: (req.resources || baseOf(r).resources).map((x) => `${x.kind}: ${x.ref}${x.note ? " | " + x.note : ""}`).join("\n") } };
  const issues = validateDraft(es);
  return issues.length ? `Rejected by validation: ${issues.map((i) => i.field + " " + i.key).join(", ")}.` : "";
}

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

  // ---- sample mailboxes: the same rules the master backend applies, on invented letters ----
  // A letter lives in its recipient's inbox until it is acknowledged or filed. Edit / retract are allowed only for the
  // operator's own ordinary letters that no channel has accepted yet (the outbox says so, the write checks again).
  const operator = () => data.company.operatorBox;
  const boxNames = () => data.addresses.filter((a) => a.kind === "box").map((a) => a.name);
  const letters = new Map(), done = new Set(), acked = new Set();
  const locked = (m) => m.delivery === "accepted" || m.delivery === "consumed";
  const bodyFor = (m) => `Sample letter.\n\nSubject: ${m.subject}\nThis text exists only in the client's mock; nothing was written to a mailbox.`;
  for (const m of data.mail) letters.set(`${m.box}/${m.letterId}`, { box: m.box, id: m.letterId, sender: m.sender, subject: m.subject, need: m.dir === "in" ? "回复" : "仅告知", body: bodyFor(m), mailId: m.id, ordinary: true });
  const mailRow = (l) => data.mail.find((m) => m.id === l.mailId);
  let lseq = 0;
  const gate = (needs) => {
    if (sc === "denied") return fail(ERR.DENIED, "The backend refused the request (403). (mock scenario: denied)");
    if (sc === "missing" && needs) return fail(ERR.MISSING, "This backend does not offer that operation. (mock scenario: missing)");
    return null;
  };
  // a write that was applied but whose reply is lost; the caller has already changed the state
  const reply = (r) => (sc === "lostreply" ? unknownOutcome("The backend did not answer in time. The request may or may not have been applied; look before trying again. (mock scenario: lostreply)") : r);
  const outboxRows = () => [...letters.values()].filter((l) => l.sender === operator() && l.ordinary && mailRow(l) && mailRow(l).delivery !== "retracted").map((l) => {
    const m = mailRow(l);
    return { to: l.box, id: l.id, ref: `${l.box}/${l.id}`, subject: l.subject, need: l.need, body: l.body, alias: l.alias || "", mtime: m.at, status: locked(m) ? "locked" : "pending", reason: locked(m) ? "A channel already accepted this letter; it can no longer be edited or retracted." : "" };
  });

  function view() {
    const d = clone(data);
    d.capabilities = capabilityMap((f) => ({ status: sc === "missing" && ["role.edit", "thread.control", "mail.send", "mail.outbox"].includes(f.key) ? "missing" : "mock", note: "Sample data from the anonymous fixture." }));
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

    // Mirrors the local candidate's role-diff / role-apply (see flows/roleEdit.js). `req` is the request body as the
    // flow builds it; the mock refuses what the contract refuses so the form is exercised against real rules.
    async previewRoleChange(req) {
      await sleep(latency); const e = net(); if (e) return e;
      if (sc === "missing") return fail(ERR.MISSING, "This backend does not offer role editing.");
      const r = role(req && req.name); if (!r) return fail(ERR.NOT_FOUND, `No role ${req && req.name}.`, { saved: false });
      if (sc === "denied" || !(r.can && r.can.edit && r.can.edit.allowed)) return fail(ERR.DENIED, (r.can && r.can.edit && r.can.edit.reason) || "You lack the capability role.edit for this role.");
      const bad = badRequest(r, req); if (bad) return fail(ERR.INVALID, bad, { saved: false });
      // the reply has the candidate's shape: every field as a _from / _to pair (changed or not), expected / facts as objects
      const cur = baseOf(r);
      const next = { candidates: req.candidates, escalation: req.escalation_role === undefined ? cur.escalation : req.escalation_role, duties: req.duties || cur.duties, resources: req.resources || cur.resources };
      const change = {};
      for (const f of ["candidates", "escalation", "duties", "resources"]) { change[f + "_from"] = cur[f] == null ? "" : cur[f]; change[f + "_to"] = next[f] == null ? "" : next[f]; }
      change.escalation_to_title = (role(next.escalation) || {}).title || "";
      const inc = r.incumbent ? r.incumbent.id : "", after = inc && next.candidates.includes(inc) ? inc : "";
      const expected = { active_now: inc, active_after: after, active_will_change: inc !== after, acceptor_now: r.escalationAcceptor || "", acceptor_after: r.escalationAcceptor || "", note_key: "roleNoteTiming" };
      const facts = { active_now: inc, acceptor_now: r.escalationAcceptor || "", confirmed: null, candidates: r.candidates.map((c) => ({ name: c.id, state: c.presence })) };
      return ok({ diff: normalizeDiff({ name: r.id, change, expected, facts, baseline: mockBaseline(data.roles) }) });
    },

    async applyRoleChange(req, baseline) {
      await sleep(latency); const e = net(); if (e) return e;
      if (sc === "missing") return fail(ERR.MISSING, "This backend does not offer role editing.");
      const r = role(req && req.name); if (!r) return fail(ERR.NOT_FOUND, `No role ${req && req.name}.`, { saved: false });
      if (sc === "denied") return fail(ERR.DENIED, "You lack the capability role.edit for this role.", { saved: false });
      if (sc === "conflict") {   // someone else changes the config once, between your preview and your confirm
        r.duties = [...r.duties, "Added by someone else while you were editing"]; sc = "default"; bump(); emit();
      }
      // 409: the baseline no longer matches. The reply does NOT carry the current role; the client refetches.
      if (typeof baseline !== "string" || baseline !== mockBaseline(data.roles)) return fail(ERR.STALE, "The configuration changed after you previewed.", { saved: false, projection: "not_applicable" });
      const bad = badRequest(r, req); if (bad) return fail(ERR.INVALID, bad, { saved: false, projection: "not_applicable" });
      const known = new Map();
      for (const x of data.roles) { if (x.incumbent) known.set(x.incumbent.id, x.incumbent); for (const c of x.candidates) known.set(c.id, c); }
      r.candidates = req.candidates.map((id) => clone(known.get(id) || { id, label: id, kind: "unspecified", harness: null, model: null, presence: "unknown", presenceAt: null, note: "" }));
      if (r.incumbent && !req.candidates.includes(r.incumbent.id)) r.incumbent = null;
      if (req.escalation_role !== undefined) r.escalationRole = req.escalation_role;
      const up = role(r.escalationRole); r.escalationAcceptor = up && up.incumbent ? up.incumbent.id : null;
      if (req.duties) r.duties = [...req.duties];
      if (req.resources) r.resources = req.resources.map((x) => ({ kind: x.kind, ref: x.ref, note: x.note || "" }));
      bump(); emit();
      return ok(normalizeApplied({ saved: true, projection: sc === "projection" ? "pending" : "fresh", message: "Saved (sample: nothing was written anywhere).", backup: "sample-backup", active_now: r.incumbent ? [r.incumbent.id] : [], acceptor_now: r.escalationAcceptor }));
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

    // ---- mail (mirrors master's /api/letter, /api/outbox, /api/send, /api/ack-one, /api/archive-one, /api/edit-one, /api/retract-one) ----
    async readLetter({ box, id }) {
      await sleep(latency); const e = net() || gate(false); if (e) return e;
      const l = letters.get(`${box}/${id}`); if (!l || done.has(`${box}/${id}`)) return fail(ERR.NOT_FOUND, "No such letter in that inbox.");
      return ok(normalizeLetter(l));
    },
    async listOutbox() {
      await sleep(latency); const e = net() || gate(true); if (e) return e;
      return ok({ rows: outboxRows().map(normalizeOutboxRow) });
    },
    async sendMail({ from, to, subject, need, body }) {
      await sleep(latency); const e = net() || gate(true); if (e) return e;
      if (!from || !to || typeof subject !== "string" || typeof need !== "string" || typeof body !== "string") return fail(ERR.INVALID, "bad_request");
      if (from !== operator()) return fail(ERR.NOT_FOUND, "No such sender mailbox.");
      let target = to;
      if (to.startsWith("@")) {
        const a = data.addresses.find((x) => x.kind === "alias" && x.name === to);
        if (!a) return fail(ERR.REFUSED, `No logical address ${to}.`);
        if (!a.target || a.online === false) return fail(ERR.REFUSED, `发送失败：${to} 当前没有在线信箱。`);
        target = a.target;
      } else if (!boxNames().includes(to)) return fail(ERR.REFUSED, `没有信箱 ${to}。`);
      const id = `20261013-new${++lseq}`, at = Math.round(now());
      const m = { id: `n${lseq}`, letterId: id, roleId: target, box: target, sender: from, threadId: null, workId: null, dir: "out", from: "Owner", to: target, subject, at, delivery: "waiting" };
      data.mail.push(m); letters.set(`${target}/${id}`, { box: target, id, sender: from, subject, need, body, mailId: m.id, ordinary: true, alias: to.startsWith("@") ? to : "" });
      bump(); emit();
      return reply(ok(normalizeMailResult("send", { id, ref: `${target}/${id}` })));
    },
    async ackMail({ box, id, note = "" }) {
      await sleep(latency); const e = net() || gate(false); if (e) return e;
      const k = `${box}/${id}`, l = letters.get(k); if (!l) return fail(ERR.NOT_FOUND, "No such letter.");
      if (acked.has(k)) return reply(ok(normalizeMailResult("ack", { state: "already", id, to: l.sender })));
      if (done.has(k)) return fail(ERR.REFUSED, `找不到这封信：${id}。`);
      acked.add(k); done.add(k); data.mail = data.mail.filter((m) => m.id !== l.mailId); bump(); emit();
      return reply(ok(normalizeMailResult("ack", { state: "recorded", id, to: l.sender })));
    },
    async archiveMail({ box, id }) {
      await sleep(latency); const e = net() || gate(false); if (e) return e;
      const k = `${box}/${id}`, l = letters.get(k); if (!l) return fail(ERR.NOT_FOUND, "No such letter.");
      if (done.has(k)) return reply(ok(normalizeMailResult("archive", { state: "already" })));
      done.add(k); data.mail = data.mail.filter((m) => m.id !== l.mailId); bump(); emit();
      return reply(ok(normalizeMailResult("archive", { state: "moved" })));
    },
    async editMail({ to, id, subject, need, body }) {
      await sleep(latency); const e = net() || gate(true); if (e) return e;
      const k = `${to}/${id}`, l = letters.get(k); if (!l || done.has(k)) return fail(ERR.NOT_FOUND, "No such letter in that inbox.");
      const row = outboxRows().find((r) => r.ref === k);
      if (!row) return fail(ERR.REFUSED, "这不是 operator 发出的普通信，改不了。");
      if (row.status !== "pending") return fail(ERR.REFUSED, row.reason);
      if ([subject, need, body].every((v) => v == null)) return fail(ERR.INVALID, "bad_request");
      if (subject != null) { l.subject = subject; mailRow(l).subject = subject; }
      if (need != null) l.need = need;
      if (body != null) l.body = body;
      bump(); emit();
      return reply(ok(normalizeMailResult("edit", { state: "updated", id, to })));
    },
    async retractMail({ to, id }) {
      await sleep(latency); const e = net() || gate(true); if (e) return e;
      const k = `${to}/${id}`, l = letters.get(k); if (!l || done.has(k)) return fail(ERR.NOT_FOUND, "No such letter in that inbox.");
      const row = outboxRows().find((r) => r.ref === k);
      if (!row) return fail(ERR.REFUSED, "这不是 operator 发出的普通信，撤不了。");
      if (row.status !== "pending") return fail(ERR.REFUSED, row.reason);
      done.add(k); data.mail = data.mail.filter((m) => m.id !== l.mailId); bump(); emit();
      return reply(ok(normalizeMailResult("retract", { state: "retracted", id, to })));
    },

    sameData(other) { return deepEqual(data, other); },
    stop() { if (timer) clearInterval(timer); timer = null; listeners.clear(); },
  };
  return api;
}
