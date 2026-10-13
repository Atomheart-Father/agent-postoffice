// Role editing as an explicit state machine: draft -> preview (diff) -> confirm -> result.
// Pure functions only, so every state can be tested without a browser.
//
// The write contract is the backend's, not the client's (office local candidate e819f0e: POST role-diff / role-apply):
//   request  { name, candidates[], escalation_role, duties?, resources?[{kind, ref, note?}] }
//            `name` is the logical role name. candidates is required, ordered, non-empty, without duplicates.
//            duties / resources: key ABSENT = leave as is, [] = clear, a list = replace the whole list.
//   preview  -> { diff: { name, change, expected, facts, baseline } }       baseline = SHA-256 of the whole config
//   apply    = the same request + baseline -> { saved, projection: fresh | pending | not_applicable, ... }
//   a stale baseline is refused (409) WITHOUT returning the current role; the client refetches and compares itself.
// Nothing else goes into the payload: qualification, policy and capability assignment are design, not contract.
//
// The client never grants itself the right to edit and never overwrites silently: a draft survives navigation,
// polls, conflicts and failures; the preview shows what would change before anything is sent.
//
// phase: idle -> previewing -> preview -> applying -> done        (happy path)
//        any request phase can end in `error` (denied / stale / capability_missing / network / invalid / ...)
//        done.result: saved is always true there; result.projection says whether the derived cards are fresh.

import { ERR, RESOURCE_KINDS, LIMITS } from "../contract.js";
import { deepEqual } from "../util.js";

const chars = (s) => [...String(s)].length;
const lines = (text) => String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
export const parseCandidates = lines;
export const parseDuties = lines;
// "kind: ref | note"  (the note is optional; the first colon ends the kind, so "url: https://x" works)
export const parseResources = (text) => lines(text).map((line) => {
  const i = line.indexOf(":");
  const kind = i > 0 ? line.slice(0, i).trim().toLowerCase() : "";
  const rest = i > 0 ? line.slice(i + 1).trim() : line;
  const j = rest.indexOf(" | ");
  const ref = (j >= 0 ? rest.slice(0, j) : rest).trim(), note = j >= 0 ? rest.slice(j + 3).trim() : "";
  return note ? { kind, ref, note } : { kind, ref };
});
export const resourceText = (rs) => rs.map((r) => `${r.kind}: ${r.ref}${r.note ? ` | ${r.note}` : ""}`).join("\n");

// The editable part of a role, in the contract's own terms.
export const baseOf = (role) => ({
  candidates: role.candidates.map((c) => c.id),
  escalation: role.escalationRole == null ? null : role.escalationRole,
  duties: [...role.duties],
  resources: role.resources.map((r) => (r.note ? { kind: r.kind, ref: r.ref, note: r.note } : { kind: r.kind, ref: r.ref })),
});

// Why this role cannot be edited from what the read model gave. "" = it can.
export function editBlocker(role) {
  if (role.kind !== "position") return "team";
  if (role.origin === "mailbox") return "mailbox";   // a legacy mailbox node: its id is not a role name, so no valid request can be built
  if (!role.candidates.length) return "candidates";
  if (role.escalationRole == null) return "escalation";
  return "";
}

const textOf = (b) => ({ candidates: b.candidates.join("\n"), escalation: b.escalation == null ? "" : b.escalation, duties: b.duties.join("\n"), resources: resourceText(b.resources) });

export function startEdit(role) {
  const b = baseOf(role);
  return { roleId: role.id, base: b, text: textOf(b), phase: "idle", preview: null, result: null, error: null };
}

export const draftOf = (es) => ({ candidates: parseCandidates(es.text.candidates), escalation: es.text.escalation.trim(), duties: parseDuties(es.text.duties), resources: parseResources(es.text.resources) });
const FIELDS = ["candidates", "escalation", "duties", "resources"];
export const changedFields = (es) => { const d = draftOf(es); return FIELDS.filter((f) => !deepEqual(d[f], es.base[f])); };
export const isDirty = (es) => changedFields(es).length > 0;

// The request body. Candidates and the escalation role are always part of it; duties / resources only when the
// draft changed them, so an untouched list is never sent as "clear". An emptied list is sent as [] on purpose.
export function buildRequest(es) {
  const d = draftOf(es), req = { name: es.roleId, candidates: d.candidates, escalation_role: d.escalation };
  if (!deepEqual(d.duties, es.base.duties)) req.duties = d.duties;
  if (!deepEqual(d.resources, es.base.resources)) req.resources = d.resources;
  return req;
}

// Cheap local checks against the contract's limits, so the user sees the problem next to the field. The backend
// checks again. Each issue is { field, key, n? } and the view turns it into words.
export function validateDraft(es) {
  const d = draftOf(es), out = [];
  if (!d.candidates.length) out.push({ field: "candidates", key: "empty" });
  if (new Set(d.candidates).size !== d.candidates.length) out.push({ field: "candidates", key: "dup" });
  if (d.duties.length > LIMITS.duties) out.push({ field: "duties", key: "count", n: LIMITS.duties });
  d.duties.forEach((x, i) => { if (chars(x) > LIMITS.dutyChars) out.push({ field: "duties", key: "long", n: LIMITS.dutyChars, at: i + 1 }); });
  if (d.resources.length > LIMITS.resources) out.push({ field: "resources", key: "count", n: LIMITS.resources });
  d.resources.forEach((r, i) => {
    if (!RESOURCE_KINDS.includes(r.kind)) out.push({ field: "resources", key: "kind", at: i + 1, kinds: RESOURCE_KINDS.join(" / ") });
    else if (!r.ref) out.push({ field: "resources", key: "ref", at: i + 1 });
    else if (chars(r.ref) > LIMITS.refChars) out.push({ field: "resources", key: "refLong", n: LIMITS.refChars, at: i + 1 });
    if (r.note && chars(r.note) > LIMITS.noteChars) out.push({ field: "resources", key: "noteLong", n: LIMITS.noteChars, at: i + 1 });
  });
  return out;
}

// The role in the snapshot is no longer what this draft started from (someone, or a projection, changed it).
export const movedUpstream = (es, role) => !!role && !deepEqual(es.base, baseOf(role));

// Three-way view for a conflict: where the draft started (base), what the draft wants, what the snapshot says now.
// `lost` lists items that exist upstream now but are in neither the base nor the draft: because duties and
// resources are replaced as whole lists, applying the draft as it stands would silently remove them.
const itemKey = (f, x) => (f === "resources" ? `${x.kind}:${x.ref}` : String(x));
export function threeWay(es, latest) {
  const mine = draftOf(es), theirs = baseOf(latest);
  return FIELDS.map((f) => {
    const upstream = !deepEqual(theirs[f], es.base[f]), edited = !deepEqual(mine[f], es.base[f]);
    let lost = [];
    if (Array.isArray(mine[f]) && upstream) {
      const known = new Set([...es.base[f], ...mine[f]].map((x) => itemKey(f, x)));
      lost = theirs[f].filter((x) => !known.has(itemKey(f, x)));
    }
    return { field: f, base: es.base[f], mine: mine[f], theirs: theirs[f], upstream, edited, conflict: upstream && edited && !deepEqual(mine[f], theirs[f]), lost };
  }).filter((x) => x.upstream || x.edited);
}

export function reduce(es, ev) {
  const reopen = (e) => (e.phase === "preview" || e.phase === "error" ? "idle" : e.phase);
  switch (ev.type) {
    case "input":
      if (es.phase === "previewing" || es.phase === "applying") return es;   // the request in flight belongs to the text it was made from
      return { ...es, text: { ...es.text, [ev.field]: ev.value }, phase: reopen(es), preview: null, error: es.phase === "error" ? null : es.error };
    case "preview:start": return { ...es, phase: "previewing", error: null };
    case "preview:ok": return { ...es, phase: "preview", preview: ev.data, error: null };
    case "preview:fail": return { ...es, phase: "error", error: { ...ev.error, during: "preview" }, preview: null };
    case "back": return { ...es, phase: "idle", preview: null, error: null };
    case "apply:start": return { ...es, phase: "applying", error: null };
    case "apply:ok": return { ...es, phase: "done", result: ev.data, error: null };
    case "apply:fail": return { ...es, phase: "error", error: { ...ev.error, during: "apply" } };
    // Keep my draft, move its base to the role as the snapshot now has it. A field I did not touch follows upstream;
    // a field I did touch keeps my text (the next preview shows the true diff, and the conflict list warned first).
    case "rebase": {
      const latest = baseOf(ev.role), mine = draftOf(es), text = { ...es.text }, nb = textOf(latest);
      for (const f of FIELDS) if (deepEqual(mine[f], es.base[f])) text[f] = nb[f];
      return { ...es, base: latest, text, phase: "idle", preview: null, error: null };
    }
    case "reset": return startEdit(ev.role);
    default: return es;
  }
}

// Which honest state a failed call maps to; the view picks its words from this, never from raw error text alone.
export const errorKind = (e) => (e && Object.values(ERR).includes(e.code) ? e.code : "unknown");
