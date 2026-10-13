// The edit tab. One screen per phase of flows/roleEdit.js; the words say what happened and what did not.
// Two facts are kept apart on purpose: "the configuration is saved" and "the derived cards are fresh".
import { esc, fmt, deepEqual } from "../util.js";
import { t } from "../i18n.js";
import { RESOURCE_KINDS } from "../contract.js";
import { note, proto } from "./ui.js";
import { startEdit, isDirty, movedUpstream, errorKind, validateDraft, threeWay, editBlocker, resourceText, baseOf, draftOf } from "../flows/roleEdit.js";

const FIELD = { candidates: "ed.candidates", escalation: "ed.escalation", escalation_role: "ed.escalation", duties: "ed.duties", resources: "ed.resources" };
const label = (f) => (FIELD[f] ? t(FIELD[f]) : f);

// one list item / scalar as text
const itemText = (f, x) => (f === "resources" && x && typeof x === "object" ? resourceText([x]) : typeof x === "string" ? x : x == null ? "" : JSON.stringify(x));
const scalar = (v) => (v === undefined || v === null ? "—" : v === "" ? t("ed.none") : String(v));
const key = (f) => (f === "escalation_role" ? "escalation" : f);

// rows for one change: added / removed items for lists, from → to for the rest
function rows(field, from, to, cls = "") {
  const f = key(field);
  if (Array.isArray(from) && Array.isArray(to)) {
    const a = from.map((x) => itemText(f, x)), b = to.map((x) => itemText(f, x));
    const out = [];
    for (const x of a) if (!b.includes(x)) out.push(row("remove", f, x, cls));
    for (const x of b) if (!a.includes(x)) out.push(row("add", f, x, cls));
    if (!out.length && !deepEqual(a, b)) out.push(row("change", f, `${a.join(" · ")} → ${b.join(" · ")}`, cls, true));
    return out.join("");
  }
  return row("change", f, `<s>${esc(scalar(from))}</s> → ${esc(scalar(to))}`, cls, true);
}
const row = (op, f, val, cls, html) => `<li data-op="${esc(op)}" ${cls ? `class="${cls}"` : ""}><span class="op">${esc(t("df." + op))}</span><span class="fld">${esc(label(f))}</span><span class="val">${html ? val : esc(val)}</span></li>`;

function diffList(p) {
  const body = p.changes.map((c) => (c.raw ? row("change", c.field, `${itemText(c.field, c.to)}`, "") : rows(c.field, c.from, c.to))).join("");
  return `<ul class="diff">${body}</ul>`;
}
const textList = (title, xs) => (xs.length ? `<section class="sub"><h4>${esc(title)}</h4><ul class="bul">${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></section>` : "");

function issueText(i) {
  return fmt(t("iss." + i.key), { n: i.n ?? "", at: i.at ?? "", kinds: i.kinds ?? "" });
}
// The list of problems with the draft against the contract's limits; re-rendered live while typing (see app.js).
export const issuesHtml = (es) => {
  const issues = validateDraft(es);
  return issues.length ? `<ul class="issues">${issues.map((i) => `<li><b>${esc(label(i.field))}</b> ${esc(issueText(i))}</li>`).join("")}</ul>` : "";
};

function form(snap, es, role, locked) {
  const dis = locked ? "disabled" : "";
  const issues = validateDraft(es);
  const esc_opts = snap.roles.filter((r) => r.kind === "position" && r.id !== role.id);
  const cur = es.text.escalation;
  const known = esc_opts.some((r) => r.id === cur);
  return `<form class="edform" data-form="role" novalidate>
    <label class="f"><span>${esc(t("ed.candidates"))}<small>${esc(t("ed.candHint"))}</small></span><textarea rows="3" data-edit="candidates" data-fk="ed:candidates" ${dis}>${esc(es.text.candidates)}</textarea></label>
    <label class="f"><span>${esc(t("ed.escalation"))}<small>${esc(t("ed.escHint"))}</small></span>
      <select data-edit="escalation" data-fk="ed:escalation" ${dis}><option value="" ${cur === "" ? "selected" : ""}>${esc(t("ed.none"))}</option>${cur && !known ? `<option value="${esc(cur)}" selected>${esc(cur)}</option>` : ""}${esc_opts.map((r) => `<option value="${esc(r.id)}" ${cur === r.id ? "selected" : ""}>${esc(r.title)}</option>`).join("")}</select></label>
    <label class="f"><span>${esc(t("ed.duties"))}<small>${esc(t("ed.dutyHint"))}</small></span><textarea rows="5" data-edit="duties" data-fk="ed:duties" ${dis}>${esc(es.text.duties)}</textarea></label>
    <label class="f"><span>${esc(t("ed.resources"))}<small>${esc(fmt(t("ed.resHint"), { kinds: RESOURCE_KINDS.join(" / ") }))}</small></span><textarea rows="4" data-edit="resources" data-fk="ed:resources" ${dis}>${esc(es.text.resources)}</textarea></label>
    <div data-issues role="status">${issuesHtml(es)}</div>
    <div class="actions"><button type="button" class="btn primary" data-act="ed-preview" data-fk="ed-preview" ${locked || !isDirty(es) || issues.length ? "disabled" : ""}>${esc(t("ed.preview"))}</button>
      <button type="button" class="btn ghost" data-act="ed-discard" data-fk="ed-discard" ${locked || !isDirty(es) ? "disabled" : ""}>${esc(t("ed.discard"))}</button></div>
    <p class="fine">${esc(t("ed.flowHint"))}</p>
    <p class="fine">${proto(t("ed.designProto"))} ${esc(t("ed.design"))}</p></form>`;
}

// The latest role against this draft: what changed upstream, what the whole-list replace would drop.
function conflictBlock(es, role) {
  const tw = threeWay(es, role).filter((x) => x.upstream);
  if (!tw.length) return note("stale", t("ed.staleElsewhere"));
  const items = tw.map((x) => `${rows(x.field, x.base, x.theirs)}${x.conflict ? `<li class="warn" data-op="change"><span class="op">${esc(t("ed.conflict"))}</span><span class="fld">${esc(label(x.field))}</span><span class="val">${esc(t("ed.conflictText"))}</span></li>` : ""}`).join("");
  const lost = tw.filter((x) => x.lost.length).map((x) => note("warn", fmt(t("ed.lost"), { field: label(x.field), items: x.lost.map((i) => itemText(x.field, i)).join(" · ") }))).join("");
  return `<h4 class="subh">${esc(t("ed.upstream"))}</h4><ul class="diff">${items}</ul>${lost}`;
}

function errorBlock(es, role) {
  const e = es.error, k = errorKind(e), msg = e.message || "";
  if (k === "stale") {
    return `${note("stale", t("ed.stale"))}${conflictBlock(es, role)}
      <div class="actions"><button type="button" class="btn primary" data-act="ed-rebase" data-fk="ed-rebase">${esc(t("ed.rebase"))}</button>
        <button type="button" class="btn" data-act="ed-reset" data-fk="ed-reset">${esc(t("ed.startOver"))}</button>
        <button type="button" class="btn ghost" data-act="ed-back" data-fk="ed-back">${esc(t("ed.keepEditing"))}</button></div>`;
  }
  // a lost reply to an apply: the config may or may not have been saved, so look again instead of sending it twice
  if (k === "network" && e.during === "apply") {
    const same = deepEqual(baseOf(role), draftOf(es));
    return `${note("error", t("ed.unknownOutcome"))}${same ? note("unknown", t("ed.looksSaved")) : ""}
      <div class="actions"><button type="button" class="btn primary" data-act="ed-refresh" data-fk="ed-refresh">${esc(t("ed.refresh"))}</button>
        <button type="button" class="btn ghost" data-act="ed-back" data-fk="ed-back">${esc(t("ed.keepEditing"))}</button></div>`;
  }
  const kind = k === "denied" ? "denied" : k === "capability_missing" ? "missing" : "error";
  const unsaved = e.during === "apply" && e.saved === false ? `<p class="fine">${esc(t("ed.notSaved"))}</p>` : "";
  return `${note(kind, msg || t("nk." + kind))}${unsaved}<div class="actions">${k === "network" ? `<button type="button" class="btn primary" data-act="ed-retry" data-fk="ed-retry">${esc(t("retry"))}</button>` : ""}
    <button type="button" class="btn ghost" data-act="ed-back" data-fk="ed-back">${esc(t("ed.keepEditing"))}</button></div>`;
}

function resultBlock(r) {
  const facts = [r.message && [t("ed.rMessage"), r.message], r.backup && [t("ed.rBackup"), r.backup], r.activeNow.length && [t("ed.rActive"), r.activeNow.join(" · ")], r.acceptorNow.length && [t("ed.rAcceptor"), r.acceptorNow.join(" · ")]].filter(Boolean);
  const factsHtml = facts.length ? `<ul class="bul facts">${facts.map(([k, v]) => `<li><b>${esc(k)}</b> ${esc(v)}</li>`).join("")}</ul>` : "";
  const saved = `<div class="ok-b" role="status"><b>${esc(t("ed.saved"))}</b><span>${esc(t("ed.savedText"))}</span></div>`;
  const proj = { fresh: `<p class="fine">${esc(t("ed.projFresh"))}</p>`, not_applicable: "", pending: note("warn", t("ed.projPending")), unknown: note("warn", t("ed.projUnknown")) }[r.projection];
  const more = r.projection === "pending" || r.projection === "unknown" ? `<button type="button" class="btn" data-act="ed-refresh" data-fk="ed-refresh">${esc(t("ed.refresh"))}</button>` : "";
  return `${saved}${proj}${factsHtml}<div class="actions"><button type="button" class="btn primary" data-act="ed-reset" data-fk="ed-reset">${esc(t("ed.done"))}</button>${more}</div>`;
}

export function editTab(snap, role, S) {
  const cap = snap.capabilities["role.edit"];
  const can = role.can && role.can.edit;
  if (cap && cap.status === "missing" && !(S.edits && S.edits[role.id])) return `<section>${note("missing", t("ed.missing"))}</section>`;
  if (!can || !can.allowed) return `<section>${note("denied", (can && can.reason) || t("ed.noRight"))}<p class="fine">${esc(t("ed.denyHint"))}</p></section>`;
  const block = editBlocker(role);
  if (block) return `<section>${note("missing", t("ed.block." + block))}</section>`;
  const es = (S.edits && S.edits[role.id]) || startEdit(role);
  const moved = movedUpstream(es, role) && es.phase !== "done" && es.phase !== "error" ? note("stale", t("ed.movedUp")) : "";
  const head = `<div class="sec-h"><h3>${esc(t("tab.edit"))}</h3>${cap && cap.status === "mock" ? `<span class="chip proto" title="${esc(t("mock.tip"))}">${esc(t("mock"))}</span>` : ""}</div>`;
  const stepper = `<ol class="steps">${["draft", "preview", "confirm", "result"].map((k, i) => {
    const at = { idle: 0, previewing: 1, preview: 1, applying: 2, done: 3, error: es.preview || es.result ? 2 : 0 }[es.phase];
    return `<li ${i === at ? 'aria-current="step"' : ""} class="${i < at ? "done" : ""}">${esc(t("step." + k))}</li>`;
  }).join("")}</ol>`;
  let body;
  switch (es.phase) {
    case "previewing": body = `${form(snap, es, role, true)}${note("loading", t("ed.previewing"))}`; break;
    case "preview": {
      const p = es.preview, canApply = p.changes.length && p.baseline;
      body = `${p.changes.length ? diffList(p) : note("empty", t("ed.noChanges"))}
        ${p.baseline ? "" : note("error", t("ed.noBaseline"))}
        ${textList(t("ed.facts"), p.facts)}${textList(t("ed.expected"), p.expected)}
        <p class="fine">${esc(t("ed.baselineHint"))}</p>
        <div class="actions"><button type="button" class="btn primary" data-act="ed-apply" data-fk="ed-apply" ${canApply ? "" : "disabled"}>${esc(t("ed.apply"))}</button>
        <button type="button" class="btn ghost" data-act="ed-back" data-fk="ed-back">${esc(t("ed.back"))}</button></div>`;
      break;
    }
    case "applying": body = note("loading", t("ed.applying")); break;
    case "done": body = resultBlock(es.result); break;
    case "error": body = `${errorBlock(es, role)}${es.preview || errorKind(es.error) === "stale" || (es.error.during === "apply" && errorKind(es.error) === "network") ? "" : form(snap, es, role, false)}`; break;
    default: body = form(snap, es, role, false);
  }
  return `<section>${head}${stepper}${moved}${body}</section>`;
}
