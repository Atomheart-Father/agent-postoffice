// Threads: sessions that belong to roles. Observing a session is not controlling it. The control block says, in words,
// what Office can do with this thread right now, and input appears only when the backend said "controllable".
import { esc, fmt, ago } from "../util.js";
import { t, getLang } from "../i18n.js";
import { note, seg, controlBadge, deliveryBadge, roleTitle, mockChip } from "./ui.js";
import { hudCard } from "./hud.js";

const capOn = (snap, k) => { const c = snap.capabilities[k]; return !!c && c.status !== "missing"; };

export function filteredThreads(snap, f) {
  return snap.threads.filter((x) => (!f.role || x.roleId === f.role) && (!f.source || x.source === f.source))
    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}

function listPane(snap, S, now) {
  const L = getLang();
  const f = S.threadFilter, list = filteredThreads(snap, f);
  const roles = snap.roles.filter((r) => r.kind === "position" && snap.threads.some((x) => x.roleId === r.id));
  const filters = `<div class="filters">${seg([["", t("all")], ["office", t("src.office")], ["observed", t("src.observed")]], f.source, "data-tsrc", t("th.source"))}
    <label class="sel"><span class="sr">${esc(t("th.byRole"))}</span><select data-tfilter="role" data-fk="tf-role"><option value="">${esc(t("th.allRoles"))}</option>
      ${roles.map((r) => `<option value="${esc(r.id)}" ${f.role === r.id ? "selected" : ""}>${esc(r.title)}</option>`).join("")}</select></label></div>`;
  const rows = list.length ? `<ul class="plist">${list.map((x) => `<li><button type="button" class="pl" data-act="thread" data-thread="${esc(x.id)}" data-fk="thr:${esc(x.id)}" aria-selected="${S.threadId === x.id ? "true" : "false"}">
      <span class="who">${esc(x.title)}</span><span class="tm">${esc(ago(x.updatedAt, now, L) || t("unknown"))}</span>
      <span class="meta"><span class="sj">${esc(roleTitle(snap, x.roleId))} · ${esc(x.harness || t("noHarness"))} · ${esc(t("src." + x.source))}</span><span class="nd">${controlBadge(x.control)}</span></span></button></li>`).join("")}</ul>`
    : `<div class="pad">${!capOn(snap, "thread.list") ? note("missing", t("th.missing")) : note("empty", snap.threads.length ? t("th.noMatch") : t("th.empty"))}</div>`;
  return `<div class="tl-list"><div class="tl-head"><h1>${esc(t("tab.threads"))}</h1>${snap.capabilities["thread.list"] && snap.capabilities["thread.list"].status === "mock" ? mockChip() : ""}
      ${capOn(snap, "thread.control") ? `<button type="button" class="btn" data-act="newthread" data-role="${esc(S.roleId || "")}" data-fk="newthread">${esc(t("th.new"))}</button>` : ""}</div>${filters}<div class="sb" data-keep="tl-list">${rows}</div></div>`;
}

function controlBlock(snap, x) {
  const why = x.controlReason;
  const base = { controllable: t("ctl.controllable"), observe_only: t("ctl.observe_only"), pending_approval: t("ctl.pending_approval"), denied: t("ctl.denied"), capability_missing: t("ctl.capability_missing"), stale: t("ctl.stale"), unknown: t("ctl.unknown") }[x.control];
  const kind = { controllable: "unknown", observe_only: "unknown", pending_approval: "pending", denied: "denied", capability_missing: "missing", stale: "stale", unknown: "unknown" }[x.control];
  const link = x.control === "pending_approval" ? `<button type="button" class="btn" data-act="go" data-view="work" data-fk="goapp">${esc(t("th.openApprovals"))}</button>` : "";
  return `<div class="ctl" data-c="${esc(x.control)}"><div class="ctl-h"><b>${esc(t("th.control"))}</b>${controlBadge(x.control)}</div>
    <p>${esc(base)}</p>${why ? `<p class="fine">${esc(why)}</p>` : ""}${link}</div>`;
}

function events(x, now) {
  const L = getLang();
  const dropped = x.droppedBefore > 0 ? `<li class="gap" role="note">${esc(fmt(t("th.dropped"), { n: x.droppedBefore, m: x.events.length }))}</li>` : "";
  const items = x.events.map((e) => `<li class="ev" data-k="${esc(e.kind)}"><span class="at">${esc(ago(e.at, now, L) || "")}</span>
    <span class="who">${esc(e.who || t("ev.system"))}</span><span class="kd">${esc(t("ev." + e.kind))}</span><span class="tx">${esc(e.text)}</span>${e.delivery ? `<span class="dl">${deliveryBadge(e.delivery)}</span>` : ""}</li>`).join("");
  return `<ol class="events" aria-label="${esc(t("th.events"))}">${dropped}${items || `<li class="gap">${esc(t("th.noEvents"))}</li>`}</ol>`;
}

function detail(snap, S, now) {
  const x = snap.threads.find((y) => y.id === S.threadId);
  if (!x) return `<div class="tl-detail empty-d"><div class="pad">${note("empty", t("th.pick"))}</div></div>`;
  const send = x.control === "controllable"
    ? `<form class="compose" data-form="send"><label class="sr" for="send-text">${esc(t("th.instruction"))}</label>
        <textarea id="send-text" rows="2" data-fk="send-text" data-draft="send:${esc(x.id)}" placeholder="${esc(t("th.instructionPh"))}">${esc((S.drafts && S.drafts["send:" + x.id]) || "")}</textarea>
        <div class="actions"><button type="button" class="btn primary" data-act="send" data-thread="${esc(x.id)}" data-fk="send">${esc(t("th.send"))}</button><span class="fine">${esc(t("th.sendHint"))}</span></div></form>` : "";
  return `<div class="tl-detail"><header class="td-h"><button type="button" class="back" data-act="close-thread" data-fk="close-thread">← ${esc(t("tab.threads"))}</button>
      <div class="tt"><span class="k"><button type="button" class="lk" data-act="role" data-role="${esc(x.roleId)}" data-fk="lk-role">${esc(roleTitle(snap, x.roleId))}</button> · ${esc(x.harness || t("noHarness"))}${x.model ? " · " + esc(x.model) : ""} · ${esc(t("src." + x.source))}</span>
      <h2>${esc(x.title)}</h2></div></header>
    <div class="sb pad" data-keep="td-body">${controlBlock(snap, x)}
      <section><h3>${esc(t("hud.title"))}</h3>${hudCard(x.usage, now)}</section>
      <section><h3>${esc(t("th.events"))}${x.bufferLimit ? `<small>${esc(fmt(t("th.buffer"), { n: x.bufferLimit }))}</small>` : ""}</h3>${events(x, now)}</section>${send}</div></div>`;
}

export function threadsView(snap, S, now) {
  return `<section class="threads${S.threadId ? " has-detail" : ""}">${listPane(snap, S, now)}${detail(snap, S, now)}</section>`;
}
