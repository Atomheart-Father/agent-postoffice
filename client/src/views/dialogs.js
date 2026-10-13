// Dialogs: Capabilities (what is live, mock or missing) and New thread. head + scrolling body + foot, like the panel.
import { esc } from "../util.js";
import { t, getLang } from "../i18n.js";
import { FEATURES, GROUPS, sourceLabel } from "../capabilities.js";
import { SCENARIOS } from "../adapters/mock.js";
import { note, proto } from "./ui.js";

const SRC = ["master", "candidate", "mock"];
const chipS = (s) => `<span class="cap" data-s="${esc(s)}">${esc(t("cap." + s))}</span>`;

export function capsDialog(snap, S, adapter) {
  const L = getLang();
  const body = GROUPS.map(([g, label]) => {
    const fs = FEATURES.filter((f) => f.group === g);
    return `<section><h3>${esc(label[L])}</h3><table class="captbl"><thead><tr><th>${esc(t("cap.feature"))}</th><th>${esc(t("cap.now"))}</th>${SRC.map((k) => `<th>${esc(sourceLabel(k, L))}</th>`).join("")}</tr></thead><tbody>
      ${fs.map((f) => { const c = snap && snap.capabilities[f.key]; return `<tr><td><b>${esc(f[L])}</b><span class="fine">${esc(f.note[L])}</span>${f.needs ? `<span class="fine needs">${esc(t("cap.needs"))}: ${esc(f.needs)}</span>` : ""}</td>
        <td data-l="${esc(t("cap.now"))}">${chipS(g === "planned" ? "planned" : c ? c.status : "missing")}</td>${SRC.map((k) => `<td data-l="${esc(sourceLabel(k, L))}">${chipS(f.src[k])}</td>`).join("")}</tr>`; }).join("")}</tbody></table></section>`;
  }).join("");
  const scen = adapter && adapter.kind === "mock"
    ? `<div class="scen"><label for="scen"><b>${esc(t("cap.scenario"))}</b><span class="fine">${esc(t("cap.scenarioHint"))}</span></label>
        <select id="scen" data-scenario data-fk="scenario">${SCENARIOS.map((s) => `<option value="${s}" ${adapter.scenario() === s ? "selected" : ""}>${s}</option>`).join("")}</select></div>` : "";
  return `<div class="layer" data-layer="caps"><div class="scrim" data-act="close-dialog"></div>
    <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><header class="dh"><h2 id="dlg-t">${esc(t("cap.title"))}</h2>
      <button type="button" class="close-x" data-act="close-dialog" data-fk="close-dialog">${esc(t("close"))}</button></header>
    <div class="sb pad" data-keep="dlg"><p class="lead">${esc(t("cap.lead"))}</p>${adapter ? `<p class="fine">${esc(t("cap.adapter"))}: <b>${esc(adapter.kind)}</b> · ${esc(adapter.label)}</p>` : ""}${scen}${body}</div>
    <footer class="df"><span class="fine">${esc(t("cap.foot"))}</span></footer></div></div>`;
}

export function newThreadDialog(snap, S) {
  const f = S.newThread;
  const roles = snap.roles.filter((r) => r.kind === "position");
  const role = roles.find((r) => r.id === f.roleId);
  const harnesses = role ? [...new Set([role.incumbent && role.incumbent.harness, ...role.candidates.map((c) => c.harness)].filter(Boolean))] : [];
  const res = f.result;
  const resKind = !res ? null : res.ok ? (res.data.state === "pending_approval" ? "pending" : "unknown") : ({ denied: "denied", capability_missing: "missing", network: "error", stale: "stale" }[res.error.code] || "error");
  const out = !res ? "" : res.ok
    ? note(resKind, t("nt.pending")) + `<p class="fine">${esc(res.data.note || "")}</p><div class="actions"><button type="button" class="btn primary" data-act="go" data-view="work" data-fk="nt-gowork">${esc(t("th.openApprovals"))}</button></div>`
    : note(resKind, res.error.message || t("nk." + resKind));
  return `<div class="layer" data-layer="newthread"><div class="scrim" data-act="close-dialog"></div>
    <div class="dlg narrow" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><header class="dh"><h2 id="dlg-t">${esc(t("nt.title"))}</h2>${proto(t("nt.protoTip"))}
      <button type="button" class="close-x" data-act="close-dialog" data-fk="close-dialog">${esc(t("close"))}</button></header>
    <div class="sb pad" data-keep="dlg"><p class="fine">${esc(t("nt.lead"))}</p>
      <form class="edform" data-form="newthread" novalidate>
        <label class="f"><span>${esc(t("nt.role"))}</span><select data-nt="roleId" data-fk="nt-role"><option value="">—</option>${roles.map((r) => `<option value="${esc(r.id)}" ${f.roleId === r.id ? "selected" : ""}>${esc(r.title)}</option>`).join("")}</select></label>
        <label class="f"><span>${esc(t("nt.harness"))}<small>${esc(t("nt.harnessHint"))}</small></span><select data-nt="harness" data-fk="nt-harness"><option value="">${esc(t("nt.anyHarness"))}</option>${harnesses.map((h) => `<option ${f.harness === h ? "selected" : ""}>${esc(h)}</option>`).join("")}</select></label>
        <label class="f"><span>${esc(t("nt.name"))}</span><input type="text" data-nt="title" data-fk="nt-title" value="${esc(f.title)}"></label>
        <label class="f"><span>${esc(t("nt.first"))}</span><textarea rows="3" data-nt="instruction" data-fk="nt-first">${esc(f.instruction)}</textarea></label>
      </form>${out}</div>
    <footer class="df"><button type="button" class="btn primary" data-act="nt-submit" data-fk="nt-submit" ${f.busy || !f.roleId || !f.title.trim() ? "disabled" : ""}>${esc(f.busy ? t("nt.sending") : t("nt.submit"))}</button>
      <button type="button" class="btn ghost" data-act="close-dialog" data-fk="close-dialog2">${esc(t("cancel"))}</button></footer></div></div>`;
}
