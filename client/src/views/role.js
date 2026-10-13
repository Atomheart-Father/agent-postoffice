// Role detail: who holds the role (and who only stands by), what it is for, what it may use, its threads,
// its mail and work, and the guarded edit flow. A docked pane on wide screens, a full-screen sheet on narrow ones.
import { esc, fmt, ago } from "../util.js";
import { t, getLang, has } from "../i18n.js";
import { lamp, presenceWord, proto, note, seg, deliveryBadge, workBadge, controlBadge, roleById, mockChip, mailOf, mailMeta } from "./ui.js";
import { treeIndex } from "./office.js";
import { standbys } from "../contract.js";
import { editTab } from "./roleEdit.js";

export const ROLE_TABS = ["overview", "threads", "work", "edit"];

function pathOf(snap, r) {
  const idx = treeIndex(snap), byId = new Map(snap.roles.map((x) => [x.id, x]));
  const out = []; let c = byId.get(r.parentId);
  while (c) { out.unshift(c.title); c = c.parentId ? byId.get(c.parentId) : null; }
  void idx; return out.join(" › ");
}

// Delivery switch, session binding and activity: three facts shown beside presence, never merged into it.
function facts(o, now) {
  const parts = [o.switch !== "unknown" ? `<span>${esc(t("sw." + o.switch))}</span>` : "", o.binding ? `<span>${esc(t("bind"))} ${esc(o.binding)}</span>` : "", o.activity ? activity(o.activity, now) : ""].filter(Boolean);
  return parts.length ? `<div class="l3 facts-l">${parts.join(" · ")}</div>` : "";
}

function occ(o, tag, now) {
  const L = getLang();
  const upd = o.presenceAt != null ? ago(o.presenceAt, now, L) : null;
  return `<div class="occ" data-st="${esc(o.presence)}">${lamp(o.kind === "human" ? "human" : "agent", o.presence)}
    <div class="tx"><div class="l1"><b>${esc(o.label)}</b><span class="tag">${esc(tag)}</span></div>
      <div class="l2"><span class="pw">${esc(presenceWord(o.presence))}</span>${o.harness ? `<span>${esc(o.harness)}</span>` : `<span>${esc(t("noHarness"))}</span>`}${o.model ? `<span>${esc(o.model)}</span>` : ""}${upd ? `<span>${esc(fmt(t("updatedAgo"), { s: upd }))}</span>` : `<span>${esc(t("noSignal"))}</span>`}</div>
      ${o.note ? `<div class="l3">${esc(o.note)}</div>` : ""}${facts(o, now)}</div></div>`;
}

// What the holder was last said to be doing, with when and from where. Absent = unknown; never drawn as idle.
function activity(a, now) {
  const L = getLang();
  const word = has("act." + a.text) ? t("act." + a.text) : a.text;
  return `<span class="act"><b>${esc(t("rp.activity"))}</b> ${esc(word)} · ${esc(a.at != null ? ago(a.at, now, L) : t("unknown"))}${a.source ? ` · ${esc(a.source)}` : ""}</span>`;
}
// Who takes over when this position cannot: the named position and whoever holds it now. Unknown stays unknown.
function escalation(snap, r) {
  if (r.escalationRole == null) return `<p class="fine">${esc(t("unknown"))}</p>`;
  if (r.escalationRole === "") return `<p class="fine">${esc(t("ed.none"))}</p>`;
  const up = roleById(snap, r.escalationRole);
  return `<p><b>${esc(up ? up.title : r.escalationRole)}</b>${r.escalationAcceptor ? ` <span class="dim">${esc(t("rp.acceptor"))} ${esc(r.escalationAcceptor)}</span>` : ""}</p>`;
}

function overview(snap, r, now) {
  const inc = r.incumbent;
  const holder = r.kind === "team" ? "" : `${r.origin === "mailbox" ? note("unknown", t("rp.mailboxNode")) : ""}<section><h3>${esc(t("rp.incumbent"))}</h3>
      ${inc ? occ(inc, t(inc.kind === "human" ? "tag.human" : "tag.holder"), now) : note("empty", t("rp.vacant"))}</section>
    <section><h3>${esc(t("rp.candidates"))}<small>${esc(t("rp.candidatesHint"))}</small></h3>
      ${standbys(r).length ? standbys(r).map((c) => occ(c, t("tag.candidate"), now)).join("") : `<p class="fine">${esc(t("rp.noCandidates"))}</p>`}</section>
    <section><h3>${esc(t("rp.escalation"))}</h3>${escalation(snap, r)}</section>`;
  const duties = r.duties.length ? `<ul class="bul">${r.duties.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : `<p class="fine">${esc(t(r.kind === "team" ? "rp.teamNoDuties" : "rp.noDuties"))}</p>`;
  const res = r.resources.length ? `<ul class="chips">${r.resources.map((x) => `<li><span class="rk">${esc(x.kind)}</span>${esc(x.ref)}${x.note ? `<small class="rn">${esc(x.note)}</small>` : ""}</li>`).join("")}</ul>` : `<p class="fine">${esc(t("rp.noResources"))}</p>`;
  const q = r.qualification
    ? `<p><b class="big">${r.qualification.level != null ? esc(r.qualification.level) : "—"}</b> <span class="dim">${esc(r.qualification.note || "")}</span></p>`
    : `<p class="fine">${esc(t("unknown"))}</p>`;
  return `${r.summary ? `<p class="lead">${esc(r.summary)}</p>` : ""}${holder}
    <section><h3>${esc(t("rp.duties"))}</h3>${duties}</section>
    <section><h3>${esc(t("rp.resources"))}</h3>${res}</section>
    ${r.kind === "team" ? "" : `<section><h3>${esc(t("rp.qual"))} ${proto()}</h3>${q}<p class="fine">${esc(t("rp.qualHint"))}</p></section>`}`;
}

function threadsTab(snap, r, now) {
  const L = getLang();
  const ts = snap.threads.filter((x) => x.roleId === r.id).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  return `<section><div class="sec-h"><h3>${esc(t("tab.threads"))}</h3>
      ${r.kind === "position" ? `<button type="button" class="btn" data-act="newthread" data-role="${esc(r.id)}" data-fk="newthread">${esc(t("th.new"))}</button>` : ""}</div>
    ${ts.length ? `<ul class="rows">${ts.map((x) => `<li><button type="button" class="row" data-act="thread" data-thread="${esc(x.id)}" data-fk="thr:${esc(x.id)}">
        <span class="t1"><b>${esc(x.title)}</b>${controlBadge(x.control)}</span>
        <span class="t2">${esc(x.harness || t("noHarness"))} · ${esc(t("src." + x.source))} · ${esc(ago(x.updatedAt, now, L) || t("unknown"))}</span></button></li>`).join("")}</ul>`
      : note("empty", t("rp.noThreads"))}</section>`;
}

function workTab(snap, r, now) {
  const L = getLang();
  const ws = snap.work.filter((x) => x.roleId === r.id), ms = mailOf(snap, r).sort((a, b) => (b.at || 0) - (a.at || 0));
  const wCap = snap.capabilities["work.state"];
  return `<section><div class="sec-h"><h3>${esc(t("tab.work"))} ${wCap && wCap.status === "mock" ? mockChip() : ""}</h3>
      <button type="button" class="btn ghost" data-act="go" data-view="work" data-role="${esc(r.id)}" data-fk="gowork">${esc(t("rp.openWork"))}</button></div>
    ${ws.length ? `<ul class="rows">${ws.map((w) => `<li class="row static"><span class="t1"><b>${esc(w.title)}</b>${workBadge(w.state)}</span><span class="t2">${esc(ago(w.updatedAt, now, L) || t("unknown"))}</span></li>`).join("")}</ul>` : note("empty", t("rp.noWork"))}</section>
    <section><h3>${esc(t("work.mail"))}</h3>
    ${ms.length ? `<ul class="rows">${ms.map((m) => `<li class="row static"><span class="t1"><b>${esc(m.subject)}</b>${deliveryBadge(m.delivery)}</span>
        <span class="t2">${esc(mailMeta(m, ago(m.at, now, L)))}</span></li>`).join("")}</ul>` : note("empty", t("rp.noMail"))}
    <p class="fine">${esc(t("work.mailNotWork"))}</p></section>`;
}

export function rolePane(snap, S, now) {
  const r = roleById(snap, S.roleId);
  if (!r) return `<aside class="rolepane" id="rolepane" aria-label="${esc(t("rp.label"))}"><header class="rp-h"><div class="tt"><h2>${esc(t("rp.gone"))}</h2></div>
    <button type="button" class="close-x" data-act="close-role" data-fk="close-role">${esc(t("close"))}</button></header><div class="sb pad">${note("missing", t("rp.goneText"))}</div></aside>`;
  const inc = r.incumbent;
  const tab = ROLE_TABS.includes(S.roleTab) ? S.roleTab : "overview";
  const tabs = seg(ROLE_TABS.filter((k) => r.kind === "position" || k === "overview" || k === "work").map((k) => [k, t("tab." + k)]), tab, "data-roletab", t("rp.tabs"));
  const body = tab === "threads" ? threadsTab(snap, r, now) : tab === "work" ? workTab(snap, r, now) : tab === "edit" ? editTab(snap, r, S) : overview(snap, r, now);
  return `<aside class="rolepane" id="rolepane" aria-label="${esc(t("rp.label"))}: ${esc(r.title)}">
    <header class="rp-h"><div class="tt"><span class="k">${esc(pathOf(snap, r) || t("rp.top"))}</span><h2>${esc(r.title)}</h2>
      <span class="st" data-st="${esc(inc ? inc.presence : "vacant")}">${lamp(inc && inc.kind === "human" ? "human" : "agent", inc ? inc.presence : "vacant")}${esc(inc ? presenceWord(inc.presence) : t("vacant"))}${inc ? ` · ${esc(inc.label)}` : ""}</span></div>
      <button type="button" class="close-x" data-act="close-role" data-fk="close-role">${esc(t("close"))}</button></header>
    <div class="rp-tabs">${tabs}</div>
    <div class="sb pad" data-keep="rolepane">${body}</div></aside>`;
}
