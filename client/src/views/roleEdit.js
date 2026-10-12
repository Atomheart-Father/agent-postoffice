// The edit tab. One screen per phase of flows/roleEdit.js; the words say what happened and what did not.
import { esc, fmt } from "../util.js";
import { t } from "../i18n.js";
import { note, proto } from "./ui.js";
import { startEdit, patchOf, isDirty, movedUpstream, errorKind } from "../flows/roleEdit.js";

const FIELD = { summary: "ed.summary", duties: "ed.duties", resources: "ed.resources", qualification: "ed.qual" };

function diffList(changes) {
  return `<ul class="diff">${changes.map((c) => `<li data-op="${esc(c.op)}"><span class="op">${esc(t("df." + c.op))}</span><span class="fld">${esc(t(FIELD[c.field] || c.field))}${c.prototype ? " " + proto() : ""}</span>
    <span class="val">${c.op === "change" ? `<s>${esc(c.before)}</s> → ${esc(c.after)}` : esc(c.after != null ? c.after : c.before)}</span></li>`).join("")}</ul>`;
}

function form(es, role, locked) {
  const dis = locked ? "disabled" : "";
  return `<form class="edform" data-form="role" novalidate>
    <label class="f"><span>${esc(t("ed.summary"))}</span><input type="text" data-edit="summary" data-fk="ed:summary" value="${esc(es.text.summary)}" ${dis}></label>
    <label class="f"><span>${esc(t("ed.duties"))}<small>${esc(t("ed.onePerLine"))}</small></span><textarea rows="5" data-edit="duties" data-fk="ed:duties" ${dis}>${esc(es.text.duties)}</textarea></label>
    <label class="f"><span>${esc(t("ed.resources"))}<small>${esc(t("ed.resHint"))}</small></span><textarea rows="4" data-edit="resources" data-fk="ed:resources" ${dis}>${esc(es.text.resources)}</textarea></label>
    <label class="f"><span>${esc(t("ed.qual"))} ${proto(t("ed.qualProto"))}<small>${esc(t("ed.qualHint"))}</small></span>
      <select data-edit="level" data-fk="ed:level" ${dis}><option value="">—</option>${[1, 2, 3, 4, 5].map((n) => `<option value="${n}" ${es.level === String(n) ? "selected" : ""}>${n}</option>`).join("")}</select></label>
    <div class="actions"><button type="button" class="btn primary" data-act="ed-preview" data-fk="ed-preview" ${locked || !isDirty(es) ? "disabled" : ""}>${esc(t("ed.preview"))}</button>
      <button type="button" class="btn ghost" data-act="ed-discard" data-fk="ed-discard" ${locked || !isDirty(es) ? "disabled" : ""}>${esc(t("ed.discard"))}</button></div>
    <p class="fine">${esc(t("ed.flowHint"))}</p></form>`;
}

function errorBlock(es, role) {
  const e = es.error, k = errorKind(e);
  const msg = e.message || "";
  if (k === "stale") {
    const cur = e.current || {};
    const added = (cur.duties || []).filter((d) => !es.base.duties.includes(d)), gone = es.base.duties.filter((d) => !(cur.duties || []).includes(d));
    return `${note("stale", fmt(t("ed.stale"), { rev: cur.rev ?? "?" }))}
      ${added.length || gone.length ? `<ul class="diff">${added.map((d) => `<li data-op="add"><span class="op">${esc(t("df.add"))}</span><span class="fld">${esc(t("ed.duties"))}</span><span class="val">${esc(d)}</span></li>`).join("")}${gone.map((d) => `<li data-op="remove"><span class="op">${esc(t("df.remove"))}</span><span class="fld">${esc(t("ed.duties"))}</span><span class="val">${esc(d)}</span></li>`).join("")}</ul>` : ""}
      <div class="actions"><button type="button" class="btn primary" data-act="ed-rebase" data-fk="ed-rebase">${esc(t("ed.rebase"))}</button>
        <button type="button" class="btn" data-act="ed-reset" data-fk="ed-reset">${esc(t("ed.startOver"))}</button>
        <button type="button" class="btn ghost" data-act="ed-back" data-fk="ed-back">${esc(t("ed.keepEditing"))}</button></div>`;
  }
  const kind = k === "denied" ? "denied" : k === "capability_missing" ? "missing" : "error";
  return `${note(kind, msg || t("nk." + kind))}<div class="actions">${k === "network" ? `<button type="button" class="btn primary" data-act="ed-retry" data-fk="ed-retry">${esc(t("retry"))}</button>` : ""}
    <button type="button" class="btn ghost" data-act="ed-back" data-fk="ed-back">${esc(t("ed.keepEditing"))}</button></div>`;
}

export function editTab(snap, role, S) {
  const cap = snap.capabilities["role.edit"];
  const can = role.can && role.can.edit;
  if (cap && cap.status === "missing" && !(S.edits && S.edits[role.id])) return `<section>${note("missing", t("ed.missing"))}</section>`;
  if (!can || !can.allowed) return `<section>${note("denied", (can && can.reason) || t("ed.noRight"))}<p class="fine">${esc(t("ed.denyHint"))}</p></section>`;
  const es = (S.edits && S.edits[role.id]) || startEdit(role);
  const moved = movedUpstream(es, role) && es.phase !== "done" ? note("stale", fmt(t("ed.movedUp"), { a: es.baseRev ?? "?", b: role.rev })) : "";
  const head = `<div class="sec-h"><h3>${esc(t("tab.edit"))}</h3>${cap && cap.status === "mock" ? `<span class="chip proto" title="${esc(t("mock.tip"))}">${esc(t("mock"))}</span>` : ""}</div>`;
  const stepper = `<ol class="steps">${["draft", "preview", "confirm", "result"].map((k, i) => {
    const at = { idle: 0, previewing: 1, preview: 1, applying: 2, done: 3, error: es.preview || es.result ? 2 : 0 }[es.phase];
    return `<li ${i === at ? 'aria-current="step"' : ""} class="${i < at ? "done" : ""}">${esc(t("step." + k))}</li>`;
  }).join("")}</ol>`;
  let body;
  switch (es.phase) {
    case "previewing": body = `${form(es, role, true)}${note("loading", t("ed.previewing"))}`; break;
    case "preview": {
      const p = es.preview;
      body = `${p.changes.length ? diffList(p.changes) : note("empty", t("ed.noChanges"))}
        ${p.warnings.length ? p.warnings.map((w) => note("degraded", w)).join("") : ""}
        <p class="fine">${esc(t("ed.willUpdate"))} ${p.projections.map((x) => esc(x.name)).join(" · ")}</p>
        <div class="actions"><button type="button" class="btn primary" data-act="ed-apply" data-fk="ed-apply" ${p.changes.length ? "" : "disabled"}>${esc(t("ed.apply"))}</button>
        <button type="button" class="btn ghost" data-act="ed-back" data-fk="ed-back">${esc(t("ed.back"))}</button></div>`;
      break;
    }
    case "applying": body = note("loading", t("ed.applying")); break;
    case "done": {
      const r = es.result, bad = (r.projections || []).filter((x) => !x.ok);
      body = r.state === "partial"
        ? `${note("degraded", fmt(t("ed.partial"), { rev: r.rev }))}<ul class="diff">${bad.map((x) => `<li data-op="remove"><span class="op">${esc(t("ed.failed"))}</span><span class="fld">${esc(x.name)}</span><span class="val">${esc(x.error)}</span></li>`).join("")}</ul>
          <div class="actions"><button type="button" class="btn primary" data-act="ed-retryproj" data-fk="ed-retryproj">${esc(t("ed.retryProj"))}</button><button type="button" class="btn ghost" data-act="ed-reset" data-fk="ed-reset">${esc(t("ed.done"))}</button></div>`
        : `<div class="ok-b" role="status"><b>${esc(t("ed.applied"))}</b><span>${esc(fmt(t("ed.appliedText"), { rev: r.rev }))}</span></div>
          <div class="actions"><button type="button" class="btn primary" data-act="ed-reset" data-fk="ed-reset">${esc(t("ed.done"))}</button></div>`;
      break;
    }
    case "error": body = `${errorBlock(es, role)}${es.preview || errorKind(es.error) === "stale" ? "" : form(es, role, false)}`; break;
    default: body = form(es, role, false);
  }
  return `<section>${head}${stepper}${moved}${body}</section>`;
}
