// The local request log: one record per client OPERATION (one explicit click that sends a write), not a company ledger and
// not an audit trail. It exists so a request whose reply was lost does not vanish and so the user is pointed at the place
// where the truth can be read (the mailbox state, the sent list).
//
// States (result_state):
//   pending       the request has left; there is no reliable result yet
//   acknowledged  the backend said, in a readable reply, what happened to THIS request (for a send: filed; says nothing about
//                 the receiver having read it, and nothing about the work being done)
//   rejected      the backend refused, and the reply itself proves nothing was applied (see the adapter's classification);
//                 an HTTP status alone never proves that - a proxy can answer 502 after the backend already saved
//   unknown       timeout, dropped connection, unreadable reply or an error that does not prove "not applied": it MAY have run
// `dismissed` is a separate flag: "I no longer want this reminder". It never changes the state and is not a verdict.
//
// A send also carries a one-off `client_request_id` (a UUID made before the request leaves). The id used here for display
// (`w-...`) is not that key and is never sent. Whether the backend de-duplicates by the key is NOT assumed anywhere in this
// file: no "safe retry" exists until the pinned backend contract says so (see SEND_DEDUPE below).
//
// Nothing in this file sends anything. There is no retry here.

export const PHASES = ["pending", "acknowledged", "rejected", "unknown"];
export const LIMIT = 30;

// The pinned contract version that defines send de-duplication (capability flag, retention window, error replies). Until a
// fixed, reviewed contract names it, the answer is "not supported" whatever a backend claims in /api/state: the candidate flag
// `features.send_request_id` is only a candidate and a flag nobody has pinned is not a promise.
export const SEND_DEDUPE = { contract: null };
export const sendDedupe = () => ({ supported: false, reason: SEND_DEDUPE.contract ? "unpinned-flag" : "contract-pending" });
// Whether a send MAY go out without a request key. Today it may (no de-duplication is claimed, so nothing is promised that a
// missing key could break). The change that pins the formal de-duplication contract must set `required: true` in the same
// commit: from then on a send whose key cannot be made is refused before anything leaves, never sent bare under a promise of
// protection.
export const SEND_KEY = { required: false };

// ---------- ids ----------
// Display / log id for the local record. Not a server key.
export function newRequestId(seq, rand = Math.random) {
  return `w-${Number(seq).toString(36)}-${Math.floor(rand() * 36 ** 4).toString(36).padStart(4, "0")}`;
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const isUuid = (v) => typeof v === "string" && UUID.test(v);
// A cryptographically random UUID v4 for ONE send operation. Math.random is never used for this: with no CSPRNG the result is
// null and the request goes out without a key (which is exactly what an old backend gets anyway), not with a guessable one.
export function newClientRequestId(c = globalThis.crypto) {
  try {
    if (c && typeof c.randomUUID === "function") { const u = c.randomUUID(); if (isUuid(u)) return u; }
    if (c && typeof c.getRandomValues === "function") {
      const b = c.getRandomValues(new Uint8Array(16));
      b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
      const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
      const u = `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
      if (isUuid(u)) return u;
    }
  } catch { /* fall through: no key */ }
  return null;
}

// ---------- the log ----------
// A record can be dropped to make room when nobody has to look at it any more: the backend answered (acknowledged / rejected),
// or the user said they stopped watching it. pending / unknown that are not dismissed are never dropped by the client.
export const evictable = (w) => w.phase === "acknowledged" || w.phase === "rejected" || w.dismissed === true;
export const ledgerFull = (list) => list.length >= LIMIT && !list.some(evictable);

export function writesReduce(list, ev) {
  switch (ev.type) {
    case "start": {
      let rest = list;
      if (rest.length >= LIMIT) {   // make room: the oldest record that may go, never an unresolved one
        const i = [...rest].reverse().findIndex(evictable);
        if (i < 0) return list;   // full of unresolved records: the caller refuses the write before it gets here
        const at = rest.length - 1 - i;
        rest = rest.filter((_, k) => k !== at);
      }
      return [{ id: ev.id, kind: ev.kind, target: ev.target || "", at: ev.at, phase: "pending", message: "", closedAt: null, clientRequestId: ev.clientRequestId || null, ref: "", dismissed: false, checkedAt: null, restored: false, deduped: false }, ...rest];
    }
    case "ok": return list.map((w) => (w.id === ev.id ? { ...w, phase: "acknowledged", closedAt: ev.at, ref: clean(ev.ref), deduped: ev.deduped === true } : w));
    case "fail": return list.map((w) => (w.id === ev.id ? { ...w, phase: ev.error && ev.error.outcomeUnknown ? "unknown" : "rejected", message: (ev.error && ev.error.message) || "", closedAt: ev.at } : w));
    // The user stops watching an unresolved request. The state stays what it was: this is not "it worked".
    case "dismiss": return list.map((w) => (w.id === ev.id && w.phase === "unknown" ? { ...w, dismissed: true } : w));
    // The user looked (state / sent list were read again). Only a record that is still unresolved records the time.
    case "checked": return list.map((w) => (w.phase === "unknown" || w.phase === "pending" ? { ...w, checkedAt: ev.at } : w));
    default: return list;
  }
}
const clean = (s) => (typeof s === "string" ? s.slice(0, 200) : "");
// What still needs a human look: not yet answered, or answered with nothing usable, and not dismissed.
export const unconfirmed = (list) => list.filter((w) => (w.phase === "pending" || w.phase === "unknown") && !w.dismissed);

// ---------- what survives a reload ----------
// Only this minimal summary is ever stored. Never a body, a subject, a draft, a full request, a recipient, a letter reference
// or anything that is a credential. (A letter reference is returned by the backend and may be shown on THIS page for
// navigation, but it is built from the sender and a shortened subject, so it is not part of the stored summary.)
export const STORED_FIELDS = ["local_id", "client_request_id", "operation", "started_at", "result_state", "dismissed", "last_checked_at"];
export const toStored = (w) => ({
  local_id: w.id, client_request_id: w.clientRequestId || null, operation: w.kind, started_at: w.at, result_state: w.phase,
  dismissed: !!w.dismissed, last_checked_at: typeof w.checkedAt === "number" ? w.checkedAt : null,
});
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
// One stored summary back to a record, or null when it is not usable. A state nobody recognises becomes `unknown`, never a
// success. Fields that are not in the summary (a `result_ref` written by an older client, anything else) are not read.
export function fromStored(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (typeof raw.local_id !== "string" || !raw.local_id || raw.local_id.length > 80) return null;
  const at = num(raw.started_at); if (at == null) return null;
  const op = typeof raw.operation === "string" && /^[a-z][a-z0-9-]{0,30}$/.test(raw.operation) ? raw.operation : "unknown";
  let phase = PHASES.includes(raw.result_state) ? raw.result_state : "unknown";
  if (phase === "pending") phase = "unknown";   // the page that sent it is gone: nothing can bring its reply back
  return {
    id: raw.local_id, kind: op, target: "", at, phase, message: "", closedAt: null,
    clientRequestId: isUuid(raw.client_request_id) ? raw.client_request_id : null,
    ref: "", dismissed: raw.dismissed === true, checkedAt: num(raw.last_checked_at), restored: true, deduped: false,
  };
}
// At most LIMIT records, newest first. Only the single writer ever saves, so a stored list never legitimately exceeds the limit;
// a payload that does (damaged, edited by hand) is cut: records that could be dropped by the rules go first, then the oldest.
export function capList(list) {
  const sorted = [...list].sort((a, b) => b.at - a.at);
  if (sorted.length <= LIMIT) return { list: sorted, dropped: 0 };
  const keep = sorted.filter((w) => !evictable(w)).slice(0, LIMIT);
  const spare = sorted.filter(evictable).slice(0, LIMIT - keep.length);
  const out = [...keep, ...spare].sort((a, b) => b.at - a.at);
  return { list: out, dropped: sorted.length - out.length };
}
