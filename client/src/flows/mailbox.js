// Delivery switches and whole-mailbox clear, as small pure helpers and reducers.
//
// Facts this file keeps straight:
//   * the delivery switch is one fact among several (binding, activity, backend state code). Turning it off does not
//     disconnect anything and turning it on does not connect anything;
//   * a button's state is the SERVER's: it comes from the last snapshot, never from what we just asked for. After a
//     write the snapshot is re-read and the result is shown as a comparison between what was asked and what the
//     server now says;
//   * "clear" moves every letter of one inbox into archived/. It is not "file one letter" (archive-one -> done/) and
//     shares no button, word or reducer with it.

// The mailboxes of a snapshot (mailbox nodes of the tree), never roles.
export const mailboxesOf = (snap) => (snap ? snap.roles.filter((r) => r.origin === "mailbox" && r.kind === "position" && r.box)
  .map((r) => ({ box: r.box, label: r.title === r.box ? "" : r.title, sw: r.incumbent ? r.incumbent.switch : "unknown", known: !!r.incumbent, state: r.incumbent && r.incumbent.state || null })) : []);
export const mailboxOf = (snap, box) => mailboxesOf(snap).find((m) => m.box === box) || null;

// Letters the last snapshot lists in one inbox. null = the snapshot does not know this mailbox, so the count is unknown (never 0).
export function lettersIn(snap, box) {
  const m = mailboxOf(snap, box);
  if (!m || !m.known) return null;
  const rows = snap.mail.filter((x) => x.box === box);
  const by = {};
  for (const x of rows) { const k = x.sourceStatus || x.delivery || "unknown"; by[k] = (by[k] || 0) + 1; }
  return { n: rows.length, by };
}

// ---------- delivery switch ----------
export const newDelivery = () => ({ sel: [], phase: "idle", req: null, error: null, verify: null, reqId: "" });
// req = { status, names?, group? }.  verify = [{ box, want, now }] filled after the snapshot is re-read.
export function deliveryReduce(d, ev) {
  switch (ev.type) {
    case "select": return d.phase === "busy" ? d : { ...d, sel: ev.on ? [...new Set([...d.sel, ev.box])] : d.sel.filter((b) => b !== ev.box), ...(d.phase !== "idle" ? { phase: "idle", error: null, verify: null, req: null } : {}) };
    case "select:set": return d.phase === "busy" ? d : { ...d, sel: [...new Set(ev.boxes)], ...(d.phase !== "idle" ? { phase: "idle", error: null, verify: null, req: null } : {}) };
    case "start": return { ...d, phase: "busy", req: ev.req, error: null, verify: null, reqId: "" };
    case "ok": return { ...d, phase: "done", error: null, reqId: ev.reqId || "" };
    case "fail": return { ...d, phase: ev.error && ev.error.outcomeUnknown ? "unknown" : "error", error: ev.error, reqId: (ev.error && ev.error.reqId) || "" };
    case "verify": return { ...d, verify: ev.verify };
    case "reset": return { ...newDelivery(), sel: d.sel };
    default: return d;
  }
}
// The boxes a request touches, for the comparison afterwards.
export function targetBoxes(snap, req) {
  if (req.group) { const g = (snap.groups || []).find((x) => x.name === req.group); return g ? g.members : []; }
  return req.names || [];
}
export function verifyAgainst(snap, req) {
  const want = req.status === "online" ? "on" : "off";
  return targetBoxes(snap, req).map((box) => { const m = mailboxOf(snap, box); return { box, want, now: m ? m.sw : "unknown" }; });
}

// ---------- clear the whole mailbox ----------
// step "ask" -> "confirm" (a second screen with a tick box) -> busy -> done | error | unknown
export const newClear = (box, count) => ({ box, step: "ask", count, ack: false, phase: "idle", error: null, after: null, reqId: "" });
export function clearReduce(c, ev) {
  switch (ev.type) {
    case "next": return c.step === "ask" && c.phase === "idle" ? { ...c, step: "confirm" } : c;
    case "back": return c.step === "confirm" && c.phase === "idle" ? { ...c, step: "ask", ack: false } : c;
    case "ack": return c.step === "confirm" && c.phase === "idle" ? { ...c, ack: !!ev.on } : c;
    case "start": return { ...c, phase: "busy", error: null };
    case "ok": return { ...c, phase: "done", error: null, reqId: ev.reqId || "" };
    case "fail": return { ...c, phase: ev.error && ev.error.outcomeUnknown ? "unknown" : "error", error: ev.error, reqId: (ev.error && ev.error.reqId) || "" };
    case "after": return { ...c, after: ev.count };
    default: return c;
  }
}
export const canClear = (c) => c.step === "confirm" && c.ack && c.phase === "idle";
