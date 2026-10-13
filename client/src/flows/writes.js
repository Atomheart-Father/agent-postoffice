// The local ledger of write requests. Every POST the client makes gets a one-off request id here, BEFORE it is sent, and
// stays on screen as "awaiting confirmation" until the backend's reply is in hand.
//
// The id is local. The backend has no idempotency key, so it cannot de-duplicate anything with it, and the client sends
// it nowhere: it exists so a user (and a test) can tell requests apart and so a request whose reply was lost does not
// vanish. A browser or proxy may resend a POST it already sent when a keep-alive connection drops; JavaScript cannot see
// that, so a lost reply is always "may have been sent" and the user looks at the mailbox state / sent list first.
//
// Nothing in this file sends anything. There is no retry here and no "send again" for a lost reply.

export const PHASES = ["pending", "accepted", "refused", "unknown", "checked"];
// accepted  the backend said yes (it still does not say anything about the receiver)
// refused   the backend answered with an error that says it did not apply the request
// unknown   no usable answer: the request may or may not have been applied
// checked   the user looked at the state / sent list and dismissed an unknown or pending entry

export function newRequestId(seq, rand = Math.random) {
  return `w-${Number(seq).toString(36)}-${Math.floor(rand() * 36 ** 4).toString(36).padStart(4, "0")}`;
}

const LIMIT = 30;
export function writesReduce(list, ev) {
  switch (ev.type) {
    case "start": return [{ id: ev.id, kind: ev.kind, target: ev.target || "", at: ev.at, phase: "pending", message: "", closedAt: null }, ...list].slice(0, LIMIT);
    case "ok": return list.map((w) => (w.id === ev.id ? { ...w, phase: "accepted", closedAt: ev.at } : w));
    case "fail": return list.map((w) => (w.id === ev.id ? { ...w, phase: ev.error && ev.error.outcomeUnknown ? "unknown" : "refused", message: (ev.error && ev.error.message) || "", closedAt: ev.at } : w));
    case "dismiss": return list.map((w) => (w.id === ev.id && (w.phase === "unknown" || w.phase === "pending") ? { ...w, phase: "checked" } : w));
    default: return list;
  }
}
// What still needs a human look: not yet answered, or answered with nothing usable.
export const unconfirmed = (list) => list.filter((w) => w.phase === "pending" || w.phase === "unknown");
