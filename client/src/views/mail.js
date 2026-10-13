// Mail screens: a letter, writing/replying, and the operator's sent list. One rule for every sentence here: say what the
// backend did, not what we hope happened. Filed is not received, a receipt is not finished work, a lost reply is
// "outcome unknown". The backend decides what is allowed; buttons appear only where it offers the operation, and every
// write shows the backend's own reason when it refuses.
import { esc, fmt, ago } from "../util.js";
import { t, getLang } from "../i18n.js";
import { note, deliveryBadge, badge, mockChip, proto, rawCode, needWord } from "./ui.js";
import { NEEDS, composeIssues, canSend, canEdit, editRequest, hasContent } from "../flows/mail.js";

const capOn = (snap, key) => { const c = snap && snap.capabilities[key]; return !!c && c.status !== "missing"; };
export const mailCaps = (snap) => ({ read: capOn(snap, "mail.read"), send: capOn(snap, "mail.send"), ack: capOn(snap, "mail.ack"), archive: capOn(snap, "mail.archive"), outbox: capOn(snap, "mail.outbox") });

// ---------- one row of mail, in the work page and in a role's mail tab ----------
export function mailRow(m, meta, extra = "") {
  const open = m.letterId && m.box;
  const kind = m.kind ? `<span class="t3" title="${esc(t("mail.kindHint"))}">${esc(t("mail.kind"))} ${rawCode(m.kind)}</span>` : "";
  const inner = `<span class="t1"><b>${esc(m.subject || t("noSubject"))}</b>${deliveryBadge(m.delivery, m.sourceStatus)}</span><span class="t2">${esc(meta)}</span>${kind}`;
  return open
    ? `<li class="row wrow"><button type="button" class="t1b" data-act="letter" data-box="${esc(m.box)}" data-id="${esc(m.letterId)}" data-mail="${esc(m.id)}" data-fk="ml:${esc(m.id)}">${inner}</button>${extra}</li>`
    : `<li class="row static">${inner}${extra}</li>`;
}

// ---------- sentences ----------
const errKind = (e) => (e && e.outcomeUnknown ? "unknown" : e && e.code) || "server";
function errBlock(e, what) {
  const k = errKind(e), msg = (e && e.message) || "";
  const body = k === "unknown" ? t("ml.unknown") : k === "not_found" ? t("ml.gone") : k === "conflict" ? (what === "archive" ? t("ml.conflict") : msg) : msg || t("nk.error");
  const kind = k === "unknown" ? "warn" : k === "denied" ? "denied" : k === "capability_missing" ? "missing" : k === "not_found" ? "stale" : "error";
  const extra = k === "unknown" || k === "not_found"
    ? `<div class="actions"><button type="button" class="btn" data-act="mail-refresh" data-fk="mail-refresh">${esc(t("ml.refresh"))}</button></div>` : "";
  return note(kind, body, extra) + ((k !== "unknown" || (e && e.status)) && msg && body !== msg ? `<p class="fine">${esc(msg)}</p>` : "");
}
// the sentence for a write that the backend accepted
function doneText(kind, r, l) {
  const k = `ml.done.${kind}.${r.state}`;
  const s = t(k);
  return s === k ? t("ml.done.other") : fmt(s, { to: r.to || (l && l.box) || "", ref: r.ref, box: (l && l.box) || "" });
}

// ---------- letter dialog ----------
function actionBlock(l, sent) {
  const a = l.act;
  if (!a) return "";
  const kind = a.kind, tx = (k) => t(`ml.ask.${kind}`) === `ml.ask.${kind}` ? "" : fmt(t(`ml.ask.${kind}`), { box: l.box, to: l.box });
  if (a.phase === "confirm") {
    return `<div class="confirm" role="group" aria-label="${esc(t("ml.confirm"))}"><p>${esc(tx())}</p>
      ${kind === "ack" ? `<label class="f"><span>${esc(t("ml.ackNote"))}<small>${esc(t("ml.ackNoteHint"))}</small></span><input type="text" data-letter-note data-fk="ml-note" value="${esc(a.note || "")}" autocomplete="off"></label>` : ""}
      <div class="actions"><button type="button" class="btn primary" data-act="letter-do" data-kind="${esc(kind)}" data-fk="ml-do">${esc(t(`ml.do.${kind}`))}</button>
        <button type="button" class="btn ghost" data-act="letter-cancel" data-fk="ml-cancel">${esc(t("cancel"))}</button></div></div>`;
  }
  if (a.phase === "busy") return note("loading", t("ml.working"));
  if (a.phase === "done") return note("ok", doneText(kind, a.result, l)) + `<p class="fine">${esc(t(`ml.after.${kind}`))}</p>`;
  return errBlock(a.error, kind) + (a.phase === "error" && a.error && a.error.code !== "not_found" ? `<div class="actions"><button type="button" class="btn" data-act="letter-ask" data-kind="${esc(kind)}" data-fk="ml-again">${esc(t("ml.again"))}</button></div>` : "");
}

function editForm(l) {
  const e = l.edit, req = editRequest(l);
  const busy = l.act && l.act.phase === "busy";
  return `<form class="edform" data-form="mailedit" novalidate>
    <label class="f"><span>${esc(t("cp.subject"))}</span><input type="text" data-le="subject" data-fk="le:subject" value="${esc(e.subject)}" autocomplete="off"></label>
    <label class="f"><span>${esc(t("cp.need"))}<small>${esc(t("cp.needHint"))}</small></span><input type="text" data-le="need" data-fk="le:need" value="${esc(e.need)}" list="need-list" autocomplete="off"></label>
    <label class="f"><span>${esc(t("cp.body"))}</span><textarea rows="8" data-le="body" data-fk="le:body">${esc(e.body)}</textarea></label>
    <p class="fine">${esc(t("ml.editHint"))}</p>
    <div class="actions"><button type="button" class="btn primary" data-act="letter-save" data-fk="le-save" ${!req || busy ? "disabled" : ""}>${esc(t("ml.save"))}</button>
      <button type="button" class="btn ghost" data-act="letter-edit-close" data-fk="le-close">${esc(t("cancel"))}</button></div></form>${needList()}`;
}
const needList = () => `<datalist id="need-list">${NEEDS.map((n) => `<option value="${esc(n)}" label="${esc(needWord(n))}">`).join("")}</datalist>`;

export function letterDialog(snap, S, now) {
  const l = S.letter, L = getLang(), caps = mailCaps(snap), sent = l.src === "sent";
  const d = l.data, op = snap.company.operatorBox;
  const subject = (d && d.subject) || l.subject || t("noSubject");
  const row = sent ? (S.outbox.rows || []).find((r) => r.to === l.box && r.id === l.id) : null;
  const acted = !!(l.act && (l.act.phase === "done" || l.act.phase === "unknown"));   // the state below was read before the action; say so
  const meta = d ? `<dl class="meta">
      <dt>${esc(t("ml.from"))}</dt><dd>${esc(d.sender || l.sender || "—")}</dd>
      <dt>${esc(t("ml.in"))}</dt><dd>${esc(l.box)}${row && row.alias ? ` <span class="fine">(${esc(row.alias)})</span>` : ""}</dd>
      <dt>${esc(t("cp.need"))}</dt><dd>${esc(needWord(d.need) || "—")}</dd>
      <dt>${esc(t("ml.state"))}</dt><dd>${sent && row ? sentBadge(row.status) : l.delivery ? deliveryBadge(l.delivery, l.rawStatus) : esc(t("unknown"))}${acted ? ` <span class="fine">${esc(t("ml.before"))}</span>` : ""}</dd>
      <dt>${esc(t("ml.ref"))}</dt><dd><code>${esc(l.box)}/${esc(l.id)}</code></dd></dl>` : "";
  const body = l.phase === "loading" ? note("loading", t("loading"))
    : l.phase === "error" ? errBlock(l.error, "read") + `<div class="actions"><button type="button" class="btn" data-act="letter-reload" data-fk="ml-reload">${esc(t("retry"))}</button></div>`
    : `${meta}<pre class="letter-body" data-fk="ml-body" tabindex="0">${esc(d.body || "")}</pre>`;
  const finished = l.act && (l.act.phase === "done" || l.act.phase === "unknown");
  const btn = (act, label, extra = "") => `<button type="button" class="btn${extra}" data-act="${act}" data-fk="${act}">${esc(label)}</button>`;
  const ask = (kind) => `<button type="button" class="btn" data-act="letter-ask" data-kind="${kind}" data-fk="ml-${kind}">${esc(t(`ml.btn.${kind}`))}</button>`;
  const busy = l.act && (l.act.phase === "busy" || l.act.phase === "confirm");
  const buttons = [];
  if (d && !finished && !l.edit) {
    if (!sent) {
      if (caps.send && op) buttons.push(btn("letter-reply", t("ml.btn.reply"), " primary"));
      if (caps.ack && !busy) buttons.push(ask("ack"));
      if (caps.archive && !busy) buttons.push(ask("archive"));
    } else if (row && canEdit(row) && caps.outbox && !busy) {
      buttons.push(btn("letter-edit", t("ml.btn.edit"), " primary"), ask("retract"));
    }
  }
  const why = [];
  if (d && !sent && caps.send && !op) why.push(t("cp.noOperator"));
  if (d && !sent && !caps.send) why.push(t("ml.noSend"));
  if (d && sent && row && !canEdit(row)) why.push(row.reason || t(`sn.status.${row.status}.why`));
  const act = (l.edit ? editForm(l) : "") + actionBlock(l, sent);
  return `<div class="layer" data-layer="letter"><div class="scrim" data-act="close-dialog"></div>
    <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><header class="dh"><h2 id="dlg-t">${esc(subject)}</h2>${snap.capabilities["mail.read"] && snap.capabilities["mail.read"].status === "mock" ? mockChip() : ""}
      <button type="button" class="close-x" data-act="close-dialog" data-fk="close-dialog">${esc(t("close"))}</button></header>
    <div class="sb pad" data-keep="dlg">${body}${act}${why.map((w) => `<p class="fine">${esc(w)}</p>`).join("")}
      ${d ? `<p class="fine">${esc(t("work.mailNotWork"))}</p>` : ""}</div>
    <footer class="df">${buttons.join("")}${btn("close-dialog", t("close"), " ghost")}</footer></div></div>`;
}

// ---------- compose / reply ----------
export const composeIssueText = (i) => t(`cp.iss.${i.key}`);
export const composeIssuesHtml = (c, op) => {
  const iss = composeIssues(c, op);
  return iss.length ? `<ul class="issues">${iss.map((i) => `<li>${esc(composeIssueText(i))}</li>`).join("")}</ul>` : "";
};

function addressOptions(snap) {
  return snap.addresses.map((a) => {
    const lab = a.kind === "alias"
      ? `${a.target ? `→ ${a.target}${a.online === false ? ` (${t("p.offline")})` : ""}` : a.error || t("cp.noTarget")}`
      : [a.label, a.online === false ? t("p.offline") : ""].filter(Boolean).join(" · ");
    return `<option value="${esc(a.name)}" label="${esc(lab)}">`;
  }).join("");
}

export function composeDialog(snap, S) {
  const c = S.compose, op = snap.company.operatorBox;
  const sending = c.phase === "sending", sentOk = c.phase === "sent", lock = sending || sentOk;
  let status = "";
  if (c.phase === "sent") {
    const r = c.result, a = c.archive;
    status = (r.deduped ? note("warn", fmt(t("ml.done.send.deduped"), { ref: r.ref || r.id || "—" })) : note("ok", fmt(t("ml.done.send.sent"), { to: r.to, ref: r.ref }))) + `<p class="fine">${esc(t("ml.after.send"))}</p>`;
    if (c.replyOf && c.fileOriginal && a) {
      status += a.phase === "busy" || a.phase === "pending" ? note("loading", t("cp.filing"))
        : a.phase === "done" ? note("ok", t(`ml.done.archive.${a.state}`) === `ml.done.archive.${a.state}` ? t("ml.done.other") : t(`ml.done.archive.${a.state}`))
        : errBlock(a.error, "archive") + `<p class="fine">${esc(t("cp.replyOut"))}</p><div class="actions"><button type="button" class="btn" data-act="compose-refile" data-fk="cp-refile">${esc(t("cp.refile"))}</button></div>`;
    }
  } else if (c.phase === "unknown") status = errBlock(c.error, "send") + `<p class="fine">${esc(t("cp.newLetterWarn"))}</p><div class="actions"><button type="button" class="btn" data-act="compose-check" data-fk="cp-check">${esc(t("cp.checkSent"))}</button></div>`;
  else if (c.phase === "error") status = errBlock(c.error, "send");
  const opNote = op ? `<span class="fine">${esc(t("cp.operatorNote"))}</span>` : "";
  return `<div class="layer" data-layer="compose"><div class="scrim" data-act="close-dialog"></div>
    <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><header class="dh"><h2 id="dlg-t">${esc(t(c.mode === "reply" ? "cp.reply" : "cp.new"))}</h2>${snap.capabilities["mail.send"] && snap.capabilities["mail.send"].status === "mock" ? mockChip() : ""}
      <button type="button" class="close-x" data-act="close-dialog" data-fk="close-dialog">${esc(t("close"))}</button></header>
    <div class="sb pad" data-keep="dlg">
      ${op ? "" : note("missing", t("cp.noOperator"))}
      <form class="edform" data-form="compose" novalidate>
        <div class="f"><span>${esc(t("cp.from"))}</span><b class="fixed">${esc(op || "—")}</b>${opNote}</div>
        <label class="f"><span>${esc(t("cp.to"))}<small>${esc(t("cp.toHint"))}</small></span><input type="text" data-cp="to" data-fk="cp:to" value="${esc(c.to)}" list="addr-list" autocomplete="off" autocapitalize="off" spellcheck="false" ${lock ? "disabled" : ""}></label>
        <label class="f"><span>${esc(t("cp.subject"))}</span><input type="text" data-cp="subject" data-fk="cp:subject" value="${esc(c.subject)}" autocomplete="off" ${lock ? "disabled" : ""}></label>
        <label class="f"><span>${esc(t("cp.need"))}<small>${esc(t("cp.needHint"))}</small></span><input type="text" data-cp="need" data-fk="cp:need" value="${esc(c.need)}" list="need-list" autocomplete="off" ${lock ? "disabled" : ""}></label>
        <label class="f"><span>${esc(t("cp.body"))}</span><textarea rows="9" data-cp="body" data-fk="cp:body" ${lock ? "disabled" : ""}>${esc(c.body)}</textarea></label>
        ${c.replyOf ? `<label class="chk"><input type="checkbox" data-cp="fileOriginal" data-fk="cp:file" ${c.fileOriginal ? "checked" : ""} ${lock ? "disabled" : ""}><span>${esc(t("cp.fileOriginal"))}<small>${esc(fmt(t("cp.fileOriginalHint"), { box: c.replyOf.box }))}</small></span></label>` : ""}
        <div data-issues role="status">${composeIssuesHtml(c, op)}</div>
      </form>${status}<datalist id="addr-list">${addressOptions(snap)}</datalist>${needList()}
      <p class="fine">${esc(t("cp.fine"))}</p></div>
    <footer class="df">${sentOk ? "" : `<button type="button" class="btn primary" data-act="compose-send" data-fk="compose-send" ${canSend(c, op) && !sending ? "" : "disabled"}>${esc(sending ? t("cp.sending") : c.phase === "unknown" ? t("cp.sendAnyway") : t("cp.send"))}</button>`}
      <button type="button" class="btn ghost" data-act="close-dialog" data-fk="close-dialog2">${esc(sentOk ? t("close") : t("cp.keep"))}</button>
      ${!sentOk && !sending && hasContent(c) ? `<button type="button" class="btn ghost" data-act="compose-discard" data-fk="compose-discard">${esc(t("cp.discard"))}</button>` : ""}</footer></div></div>`;
}

// ---------- the operator's sent list ----------
export const sentBadge = (s) => badge("sent", s, t(`sn.status.${s}`));
export function sentPane(snap, S, now) {
  const o = S.outbox, L = getLang(), caps = mailCaps(snap), op = snap.company.operatorBox;
  if (!caps.outbox) return note("missing", t("sn.missing"));
  if (!op) return note("missing", t("cp.noOperator"));
  if (o.phase === "loading" && !o.rows.length) return note("loading", t("loading"));
  const head = `<div class="actions"><button type="button" class="btn ghost" data-act="outbox-refresh" data-fk="outbox-refresh">${esc(t("sn.refresh"))}</button>
    ${o.at ? `<span class="fine">${esc(fmt(t("sn.readAt"), { s: ago(o.at, now, L) || t("unknown") }))}</span>` : ""}</div>`;
  const err = o.error ? errBlock(o.error, "outbox") : "";
  const rows = o.rows.length ? `<ul class="rows">${o.rows.map((r) => `<li class="row wrow"><button type="button" class="t1b" data-act="sent-open" data-to="${esc(r.to)}" data-id="${esc(r.id)}" data-fk="sn:${esc(r.ref)}">
      <span class="t1"><b>${esc(r.subject || t("noSubject"))}</b>${sentBadge(r.status)}</span>
      <span class="t2">${esc(r.to)}${r.alias ? ` (${esc(r.alias)})` : ""} · ${esc(ago(r.at, now, L) || t("unknown"))}</span>
      ${r.status !== "pending" && r.reason ? `<span class="t3">${esc(r.reason)}</span>` : ""}</button></li>`).join("")}</ul>` : o.phase === "ok" ? note("empty", t("sn.empty")) : "";
  return `${head}${err}${rows}<p class="fine">${esc(t("sn.fine"))}</p>`;
}
