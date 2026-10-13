// Mail operations as small state machines, pure so every state can be tested without a browser.
//
// What the backend does for each write, and therefore what the words may claim (master v1.14.1):
//   send     the letter is FILED in the recipient's mailbox. Not received, not read, not done.
//   ack      a receipt is recorded, the original is filed, the sender is told when idle. "Seen", not "done".
//   archive  the letter moves inbox -> done/. No receipt. The sender is told nothing.
//   edit     the letter is rewritten in place; only while no channel has accepted it.
//   retract  the original is archived; only while no channel has accepted it. The recipient is not told.
// The backend decides whether each is allowed. The client shows the verdict; it never pre-decides it. The one local
// check is the shape of the form (a recipient and a subject), because the backend answers an empty one with a bare
// "bad_request".
//
// A write whose reply is lost is `unknown`: the draft stays, nothing is resent by itself, and the user is pointed at the
// place where the truth can be read (the sent list, a fresh snapshot).

export const NEEDS = ["回复", "审核", "仅告知"];   // the backend's own vocabulary for 需要：

// ---------- compose (new letter / reply) ----------
export function newCompose({ replyOf = null, operatorBox = "" } = {}) {
  const subject = replyOf ? (/^re:/i.test(replyOf.subject) ? replyOf.subject : `Re: ${replyOf.subject}`) : "";
  return {
    mode: replyOf ? "reply" : "new", replyOf: replyOf ? { box: replyOf.box, id: replyOf.id, sender: replyOf.sender, subject: replyOf.subject } : null,
    from: operatorBox, to: replyOf ? replyOf.sender : "", subject, need: replyOf ? "仅告知" : "", body: "",
    // filing the original after the reply is the panel's etiquette for a letter that asked for an answer; it is a
    // separate write the user can decline, and it is on by default only when the letter sits in the operator's own mailbox
    fileOriginal: !!replyOf && replyOf.box === operatorBox,
    phase: "edit", result: null, error: null, archive: null,
  };
}

// Why this form cannot be sent yet. [] = it can. Each issue is { field, key }.
export function composeIssues(c, operatorBox = c.from) {
  const out = [];
  if (!operatorBox) out.push({ field: "from", key: "operator" });
  if (!c.to.trim()) out.push({ field: "to", key: "to" });
  if (!c.subject.trim()) out.push({ field: "subject", key: "subject" });
  return out;
}
export const canSend = (c, operatorBox) => (c.phase === "edit" || c.phase === "error" || c.phase === "unknown") && composeIssues(c, operatorBox).length === 0;
export const sendRequest = (c) => ({ from: c.from, to: c.to.trim(), subject: c.subject, need: c.need, body: c.body });

export function composeReduce(c, ev) {
  switch (ev.type) {
    case "input":
      if (c.phase === "sending" || c.phase === "sent") return c;   // the request in flight belongs to the text it was made from
      return { ...c, [ev.field]: ev.field === "fileOriginal" ? !!ev.value : ev.value, phase: c.phase === "error" ? "edit" : c.phase, error: c.phase === "error" ? null : c.error };
    case "send:start": return { ...c, phase: "sending", error: null };
    case "send:ok": return { ...c, phase: "sent", result: ev.result, error: null, archive: c.mode === "reply" && c.fileOriginal ? { phase: "pending" } : null };
    // A lost reply is not a failure: the letter may be filed. The draft stays and nothing is resent by itself.
    case "send:fail": return { ...c, phase: ev.error && ev.error.outcomeUnknown ? "unknown" : "error", error: ev.error };
    case "archive:start": return { ...c, archive: { phase: "busy" } };
    case "archive:ok": return { ...c, archive: { phase: "done", state: ev.result.state } };
    case "archive:fail": return { ...c, archive: { phase: "error", error: ev.error } };
    default: return c;
  }
}

// ---------- one letter dialog (an inbox letter, or a sent one with edit / retract) ----------
// src "inbox": a pending letter in a mailbox.  src "sent": a row of the operator's outbox.
export function openLetter({ src = "inbox", box, id, subject = "", sender = "", delivery = null, rawStatus = null, mailId = "" }) {
  return { src, box, id, subject, sender, delivery, rawStatus, mailId, phase: "loading", data: null, error: null, act: null, edit: null };
}
export function letterReduce(l, ev) {
  switch (ev.type) {
    case "read:start": return { ...l, phase: "loading", error: null };
    case "read:ok": return { ...l, phase: "ok", data: ev.data, error: null };
    case "read:fail": return { ...l, phase: "error", error: ev.error };
    // an action needs an explicit confirm: it says what will happen, and only then does it write
    case "act:ask": return { ...l, act: { kind: ev.kind, phase: "confirm", result: null, error: null, note: "" } };
    case "act:note": return l.act && l.act.phase === "confirm" ? { ...l, act: { ...l.act, note: ev.value } } : l;
    case "act:cancel": return l.act && l.act.phase === "confirm" ? { ...l, act: null } : l;
    case "act:start": return { ...l, act: { ...(l.act || { kind: ev.kind, note: "" }), kind: ev.kind, phase: "busy", error: null } };
    case "act:ok": return { ...l, act: { ...l.act, phase: "done", result: ev.result, error: null }, edit: l.act && l.act.kind === "edit" ? null : l.edit };
    case "act:fail": return { ...l, act: { ...l.act, phase: ev.error && ev.error.outcomeUnknown ? "unknown" : "error", error: ev.error } };
    case "edit:open": return { ...l, edit: ev.draft || { subject: l.data ? l.data.subject : l.subject, need: l.data ? l.data.need : "", body: l.data ? l.data.body : "" }, act: null };
    case "edit:input": return l.edit ? { ...l, edit: { ...l.edit, [ev.field]: ev.value }, act: l.act && l.act.phase === "error" ? null : l.act } : l;
    case "edit:close": return { ...l, edit: null };
    default: return l;
  }
}
// Only fields that differ from what the letter says now are sent; an unchanged letter is not a valid edit.
export function editRequest(l) {
  if (!l.edit || !l.data) return null;
  const out = { to: l.box, id: l.id };
  for (const f of ["subject", "need", "body"]) if (l.edit[f] !== l.data[f]) out[f] = l.edit[f];
  return Object.keys(out).length > 2 ? out : null;
}

// A compose form that holds something worth keeping (more than the defaults a reply starts with).
export const hasContent = (c) => c.phase !== "sent" && (c.mode === "reply" ? c.body.trim() !== "" || c.to !== (c.replyOf && c.replyOf.sender) : c.to.trim() !== "" || c.subject.trim() !== "" || c.body.trim() !== "" || c.need.trim() !== "");

// ---------- sent list ----------
export const canEdit = (row) => !!row && row.status === "pending";
