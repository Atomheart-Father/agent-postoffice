// Work and mail, side by side and never merged. A mailbox accepting or consuming a letter says nothing about whether
// the work was done; the work column shows the work's own state (generic: open, submitted, verified, accepted,
// returned, released), and a mail row that points at unfinished work says so.
import { esc, fmt, ago } from "../util.js";
import { t, getLang } from "../i18n.js";
import { WORK_STATES } from "../contract.js";
import { note, seg, workBadge, deliveryBadge, roleTitle, mockChip, proto, mailOf, mailMeta, roleById } from "./ui.js";

function approvals(snap, S) {
  const pend = snap.approvals.filter((a) => a.state === "pending");
  const cap = snap.capabilities["approvals"];
  return `<section class="need" aria-labelledby="need-h"><div class="sec-h"><h2 id="need-h">${esc(t("work.need"))}</h2>${cap && cap.status === "mock" ? mockChip() : ""}</div>
    ${pend.length ? `<ul class="rows">${pend.map((a) => `<li class="row static ap"><span class="t1"><b>${esc(a.title)}</b></span>
      <span class="t2">${esc(t("ap." + a.kind) === "ap." + a.kind ? a.kind : t("ap." + a.kind))} · ${esc(roleTitle(snap, a.roleId))}</span>
      <span class="actions"><button type="button" class="btn primary" data-act="approve" data-ap="${esc(a.id)}" data-fk="ap-y:${esc(a.id)}">${esc(t("ap.approve"))}</button>
        <button type="button" class="btn" data-act="deny" data-ap="${esc(a.id)}" data-fk="ap-n:${esc(a.id)}">${esc(t("ap.deny"))}</button></span></li>`).join("")}</ul>` : `<p class="fine">${esc(t("work.needNone"))}</p>`}</section>`;
}

function workCol(snap, S, now, role) {
  const L = getLang();
  const items = snap.work.filter((w) => !role || w.roleId === role);
  const cap = snap.capabilities["work.state"];
  const groups = WORK_STATES.map((s) => [s, items.filter((w) => w.state === s)]).filter(([, a]) => a.length);
  return `<div class="wk"><div class="sec-h"><h2>${esc(t("work.work"))}</h2>${cap && cap.status === "mock" ? mockChip() : ""}</div>
    ${groups.length ? groups.map(([s, a]) => `<section class="grp"><h3>${workBadge(s)}<span class="n">${a.length}</span></h3><ul class="rows">${a.map((w) => {
      const open = S.workOpen === w.id;
      return `<li class="row wrow"><button type="button" class="t1b" data-act="work-open" data-work="${esc(w.id)}" aria-expanded="${open ? "true" : "false"}" data-fk="wk:${esc(w.id)}">
        <b>${esc(w.title)}</b><span class="t2">${esc(roleTitle(snap, w.roleId))} · ${esc(ago(w.updatedAt, now, L) || t("unknown"))}${w.difficulty != null ? ` · ${esc(t("work.diff"))} ${esc(w.difficulty)}` : ""}</span></button>
        ${open ? `<div class="wd">${w.note ? `<p>${esc(w.note)}</p>` : ""}<ol class="hist">${w.history.map((h) => `<li>${workBadge(h.state)} <span>${esc(h.by)}${h.at != null ? " · " + esc(ago(h.at, now, L)) : ""}</span></li>`).join("")}</ol>
          <p class="fine">${esc(t("work.diffHint"))} ${proto()}</p></div>` : ""}</li>`;
    }).join("")}</ul></section>`).join("") : note("empty", t("work.noWork"))}</div>`;
}

function mailCol(snap, S, now, role) {
  const L = getLang();
  const ms = mailOf(snap, role ? roleById(snap, role) || { id: role } : null).sort((a, b) => (b.at || 0) - (a.at || 0));
  const w = (id) => snap.work.find((x) => x.id === id);
  return `<div class="ml"><div class="sec-h"><h2>${esc(t("work.mail"))}</h2></div>
    ${ms.length ? `<ul class="rows">${ms.map((m) => {
      const wk = m.workId ? w(m.workId) : null;
      const unfinished = wk && ["open", "submitted", "verified"].includes(wk.state) && (m.delivery === "accepted" || m.delivery === "consumed");
      return `<li class="row static"><span class="t1"><b>${esc(m.subject)}</b>${deliveryBadge(m.delivery)}</span>
        <span class="t2">${esc(mailMeta(m, ago(m.at, now, L)))}</span>
        ${unfinished ? `<span class="t3">${esc(fmt(t("work.stillOpen"), { s: t("w." + wk.state) }))}</span>` : ""}
        ${m.threadId ? `<span class="t3"><button type="button" class="lk" data-act="thread" data-thread="${esc(m.threadId)}" data-fk="ml-t:${esc(m.id)}">${esc(t("work.toThread"))}</button></span>` : ""}</li>`;
    }).join("")}</ul>` : note("empty", t("work.noMail"))}
    <p class="fine">${esc(t("work.mailNotWork"))}</p></div>`;
}

export function workView(snap, S, now) {
  const role = S.workFilter.role;
  const roles = snap.roles.filter((r) => r.kind === "position");
  const filter = `<label class="sel"><span class="sr">${esc(t("th.byRole"))}</span><select data-wfilter="role" data-fk="wf-role"><option value="">${esc(t("th.allRoles"))}</option>
    ${roles.map((r) => `<option value="${esc(r.id)}" ${role === r.id ? "selected" : ""}>${esc(r.title)}</option>`).join("")}</select></label>`;
  return `<section class="workpage"><div class="wp-h"><h1>${esc(t("tab.work2"))}</h1>${filter}
      <div class="only-narrow">${seg([["work", t("work.work")], ["mail", t("work.mail")]], S.workPane, "data-wpane", t("work.panes"))}</div></div>
    ${approvals(snap, S)}
    <div class="wp-cols" data-pane="${esc(S.workPane)}">${workCol(snap, S, now, role)}${mailCol(snap, S, now, role)}</div></section>`;
}
