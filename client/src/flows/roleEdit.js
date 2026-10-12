// Role editing as an explicit state machine: draft -> preview (diff) -> confirm -> result.
// Pure functions only, so every state can be tested without a browser. The client never grants itself the right to
// edit (the role's `can.edit` decides, and the backend re-checks) and never overwrites silently: a draft survives
// navigation, polls, conflicts and failures; the preview shows exactly what would change before anything is sent.
//
// phase: idle -> previewing -> preview -> applying -> done        (happy path)
//        any request phase can end in `error` (denied / stale / capability_missing / network / invalid)
//        done.result.state is "applied" or "partial" (some projections failed; retry or leave)

import { ERR } from "../contract.js";

export const parseDuties = (text) => String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
export const parseResources = (text) => String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean).map((line) => {
  const i = line.indexOf(":");
  return i > 0 ? { kind: line.slice(0, i).trim() || "resource", label: line.slice(i + 1).trim() } : { kind: "resource", label: line };
}).filter((r) => r.label);
const resText = (rs) => rs.map((r) => `${r.kind}: ${r.label}`).join("\n");

const baseOf = (role) => ({ summary: role.summary || "", duties: [...role.duties], resources: role.resources.map((r) => ({ ...r })), level: role.qualification && role.qualification.level != null ? role.qualification.level : null });

export function startEdit(role) {
  const b = baseOf(role);
  return { roleId: role.id, baseRev: role.rev, base: b,
    text: { summary: b.summary, duties: b.duties.join("\n"), resources: resText(b.resources) }, level: b.level == null ? "" : String(b.level),
    phase: "idle", preview: null, result: null, error: null };
}

// Only the fields the draft actually changes, relative to the version the draft started from.
export function patchOf(es) {
  const p = {};
  const duties = parseDuties(es.text.duties), res = parseResources(es.text.resources);
  if (es.text.summary.trim() !== es.base.summary) p.summary = es.text.summary.trim();
  if (JSON.stringify(duties) !== JSON.stringify(es.base.duties)) p.duties = duties;
  if (JSON.stringify(res) !== JSON.stringify(es.base.resources)) p.resources = res;
  const lv = es.level === "" ? null : Number(es.level);
  if (lv !== es.base.level && lv != null && Number.isFinite(lv)) p.qualification = { level: lv };
  return p;
}
export const isDirty = (es) => Object.keys(patchOf(es)).length > 0;
// The role moved on after this draft started: say so, never merge silently.
export const movedUpstream = (es, role) => !!role && role.rev != null && es.baseRev != null && role.rev !== es.baseRev;

export function reduce(es, ev) {
  switch (ev.type) {
    case "input": {
      if (es.phase === "previewing" || es.phase === "applying") return es;   // the request in flight belongs to the text it was made from
      if (ev.field === "level") return { ...es, level: ev.value, phase: es.phase === "preview" || es.phase === "error" ? "idle" : es.phase, preview: null };
      return { ...es, text: { ...es.text, [ev.field]: ev.value }, phase: es.phase === "preview" || es.phase === "error" ? "idle" : es.phase, preview: null, error: es.phase === "error" ? null : es.error };
    }
    case "preview:start": return { ...es, phase: "previewing", error: null };
    case "preview:ok": return { ...es, phase: "preview", preview: ev.data, error: null };
    case "preview:fail": return { ...es, phase: "error", error: ev.error, preview: null };
    case "back": return { ...es, phase: "idle", preview: null, error: null };
    case "apply:start": return { ...es, phase: "applying", error: null };
    case "apply:ok": return { ...es, phase: "done", result: ev.data, error: null };
    case "apply:fail": return { ...es, phase: "error", error: ev.error };
    // keep my draft, move its base to the version that appeared upstream; the next preview shows the true diff
    case "rebase": {
      const cur = es.error && es.error.current; if (!cur) return es;
      return { ...es, baseRev: cur.rev, base: { ...es.base, summary: cur.summary ?? es.base.summary, duties: cur.duties || es.base.duties, resources: cur.resources || es.base.resources },
        phase: "idle", preview: null, error: null };
    }
    case "reset": return startEdit(ev.role);
    case "retry:ok": return { ...es, result: { ...es.result, state: "applied", projections: ev.projections } };
    default: return es;
  }
}

// Which honest state a failed call maps to; the view picks its words from this, never from raw error text alone.
export const errorKind = (e) => (e && Object.values(ERR).includes(e.code) ? e.code : "unknown");
