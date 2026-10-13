// Delivery switches, whole-mailbox clear, and the ledger of write requests. Pure (state) -> HTML.
//
// Rules for every sentence here:
//   * a switch button shows the SERVER's state (the last snapshot), never what we last asked for;
//   * "clear the whole mailbox" and "file one letter" share no button, no word and no dialog;
//   * a write with no usable reply is "possibly sent", never "failed", and nothing here offers to send it again.
import { esc, fmt, ago } from "../util.js";
import { t, getLang } from "../i18n.js";
import { note, rawCode, mockChip } from "./ui.js";
import { mailboxesOf, mailboxOf, lettersIn, canClear } from "../flows/mailbox.js";
import { unconfirmed } from "../flows/writes.js";

const capOn = (snap, k) => { const c = snap && snap.capabilities[k]; return !!c && c.status !== "missing"; };
export const deliveryOn = (snap) => capOn(snap, "mailbox.status") && mailboxesOf(snap).length > 0;
export const clearOn = (snap) => capOn(snap, "mailbox.clear");
const swWord = (sw) => t(`dl.sw.${sw}`);
const wantWord = (w) => (w === "on" ? t("dl.sw.on") : t("dl.sw.off"));

// ---------- result of the last switch request: asked vs what the server now says ----------
export function deliveryResult(snap, d, only = null) {
  if (!d.req || (d.phase === "idle")) return "";
  if (only) {
    const box = d.req.group ? ((snap.groups || []).find((g) => g.name === d.req.group) || { members: [] }).members : d.req.names;
    if (!box.includes(only)) return "";
  }
  if (d.phase === "busy") return note("loading", t("dl.busy"));
  const rid = d.reqId || (d.error && d.error.reqId) || "";
  const idLine = rid ? `<p class="fine">${esc(t("wr.id"))} ${rawCode(rid)}</p>` : "";
  let head;
  if (d.phase === "done") head = note("ok", t("dl.accepted"));
  else if (d.phase === "unknown") head = note("warn", t("dl.unknown"));
  else head = note(d.error && d.error.code === "denied" ? "denied" : "error", (d.error && d.error.message) || t("nk.error"));
  const part = d.error && d.error.applied ? `<p class="fine">${esc(fmt(t("dl.partial"), { done: d.error.applied.join(", ") || "—", rest: (d.error.notTried || []).join(", ") || "—" }))}</p>` : "";
  let ver = "";
  if (d.verify) {
    const rows = only ? d.verify.filter((v) => v.box === only) : d.verify;
    ver = `<p class="fine">${esc(fmt(t("dl.verify"), { want: wantWord(d.verify[0] ? d.verify[0].want : d.req.status === "online" ? "on" : "off") }))}</p>
      <ul class="bul facts">${rows.map((v) => `<li><b>${esc(v.box)}</b> ${esc(fmt(t(v.now === v.want ? "dl.v.match" : "dl.v.differ"), { now: swWord(v.now) }))}</li>`).join("")}</ul>`;
  } else if (d.phase !== "busy") ver = `<p class="fine">${esc(t("dl.noVerify"))}</p>`;
  return `<div class="dl-res">${head}${part}${ver}${idLine}</div>`;
}

// ---------- the dialog: many mailboxes, or a group ----------
export function deliveryDialog(snap, S) {
  const d = S.delivery, boxes = mailboxesOf(snap), busy = d.phase === "busy";
  const mock = snap.capabilities["mailbox.status"] && snap.capabilities["mailbox.status"].status === "mock";
  const rows = boxes.map((m) => `<tr><td><label class="chk"><input type="checkbox" data-act="dl-pick" data-box="${esc(m.box)}" data-fk="dl-pick:${esc(m.box)}" ${d.sel.includes(m.box) ? "checked" : ""} ${busy ? "disabled" : ""}>
      <span><b>${esc(m.box)}</b>${m.label ? `<small>${esc(m.label)}</small>` : ""}</span></label></td>
      <td data-l="${esc(t("dl.cols.state"))}"><span class="sw" data-sw="${esc(m.sw)}">${esc(swWord(m.sw))}</span>${m.state ? ` ${rawCode(m.state)}` : ""}</td></tr>`).join("");
  const groups = (snap.groups || []).length ? `<ul class="rows">${snap.groups.map((g) => `<li class="row static ap"><span class="t1"><b>@${esc(g.name)}</b></span>
      <span class="t2">${esc(g.total != null ? fmt(t("dl.groupLine"), { total: g.total, online: g.online != null ? g.online : "—" }) : t("dl.groupUnknown"))}</span>
      <span class="actions"><button type="button" class="btn ghost" data-act="dl-group-pick" data-group="${esc(g.name)}" data-fk="dl-gp:${esc(g.name)}" ${busy ? "disabled" : ""}>${esc(t("dl.pickGroup"))}</button>
        <button type="button" class="btn" data-act="dl-group" data-group="${esc(g.name)}" data-status="online" data-fk="dl-gon:${esc(g.name)}" ${busy ? "disabled" : ""}>${esc(t("dl.groupOn"))}</button>
        <button type="button" class="btn" data-act="dl-group" data-group="${esc(g.name)}" data-status="offline" data-fk="dl-goff:${esc(g.name)}" ${busy ? "disabled" : ""}>${esc(t("dl.groupOff"))}</button></span></li>`).join("")}</ul>` : `<p class="fine">${esc(t("dl.noGroups"))}</p>`;
  return `<div class="layer" data-layer="delivery"><div class="scrim" data-act="close-dialog"></div>
    <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><header class="dh"><h2 id="dlg-t">${esc(t("dl.title"))}</h2>${mock ? mockChip() : ""}
      <button type="button" class="close-x" data-act="close-dialog" data-fk="close-dialog">${esc(t("close"))}</button></header>
    <div class="sb pad" data-keep="dlg"><p class="lead">${esc(t("dl.lead"))}</p>${deliveryResult(snap, d)}
      <section><div class="sec-h"><h3>${esc(t("dl.cols.box"))}</h3><span class="actions"><button type="button" class="btn ghost" data-act="dl-all" data-fk="dl-all" ${busy ? "disabled" : ""}>${esc(t("dl.all"))}</button>
        <button type="button" class="btn ghost" data-act="dl-none" data-fk="dl-none" ${busy || !d.sel.length ? "disabled" : ""}>${esc(t("dl.clearSel"))}</button></span></div>
        <table class="captbl"><thead><tr><th>${esc(t("dl.cols.box"))}</th><th>${esc(t("dl.cols.state"))}</th></tr></thead><tbody>${rows}</tbody></table></section>
      <section><h3>${esc(t("dl.groups"))}</h3>${groups}</section>
      <p class="fine">${esc(t("dl.hint.off"))}</p><p class="fine">${esc(t("dl.hint.on"))}</p></div>
    <footer class="df"><span class="fine" data-dl-count>${esc(fmt(t("dl.selected"), { n: d.sel.length }))}</span>
      <button type="button" class="btn primary" data-act="dl-sel" data-status="online" data-fk="dl-on" ${busy || !d.sel.length ? "disabled" : ""}>${esc(t("dl.selOn"))}</button>
      <button type="button" class="btn" data-act="dl-sel" data-status="offline" data-fk="dl-off" ${busy || !d.sel.length ? "disabled" : ""}>${esc(t("dl.selOff"))}</button>
      <button type="button" class="btn ghost" data-act="close-dialog" data-fk="close-dialog2">${esc(t("close"))}</button></footer></div></div>`;
}

// ---------- the role pane of a mailbox node: its own switch, and (separately) the whole-mailbox clear ----------
export function mailboxSection(snap, r, S, now) {
  if (r.origin !== "mailbox" || r.kind !== "position" || !r.box) return "";
  const m = mailboxOf(snap, r.box), d = S.delivery, L = getLang();
  const sw = m ? m.sw : "unknown", busy = d.phase === "busy";
  const sOn = capOn(snap, "mailbox.status"), cOn = clearOn(snap);
  const btn = (status, label) => `<button type="button" class="btn${status === "offline" ? "" : " primary"}" data-act="dl-one" data-box="${esc(r.box)}" data-status="${status}" data-fk="dl-one:${status}" ${busy ? "disabled" : ""}>${esc(busy ? t("dl.applying") : label)}</button>`;
  const sec1 = !sOn ? "" : `<section><h3>${esc(t("dl.sw"))}</h3>
      <p><b class="sw" data-sw="${esc(sw)}">${esc(swWord(sw))}</b>${m && m.state ? ` ${rawCode(m.state)}` : ""}</p>
      <div class="actions">${sw === "on" ? "" : btn("online", t("dl.turnOn"))}${sw === "off" ? "" : btn("offline", t("dl.turnOff"))}</div>
      ${deliveryResult(snap, d, r.box)}<p class="fine">${esc(t("dl.paneHint"))}</p></section>`;
  const n = lettersIn(snap, r.box);
  const sec2 = !cOn ? "" : `<section class="danger"><h3>${esc(t("cl.section"))}</h3>
      <p>${esc(n ? fmt(t("cl.paneText"), { n: n.n, when: snap.asOf != null ? ago(snap.asOf, now, L) || t("unknown") : t("unknown") }) : t("cl.paneUnknown"))}</p>
      <div class="actions"><button type="button" class="btn danger" data-act="clear-ask" data-box="${esc(r.box)}" data-fk="clear-ask">${esc(t("cl.open"))}</button></div>
      <p class="fine">${esc(t("cl.paneFine"))}</p></section>`;
  return sec1 + sec2;
}

// ---------- clear the whole mailbox: ask -> last check -> result ----------
export function clearDialog(snap, S, now) {
  const c = S.clear, L = getLang();
  const live = lettersIn(snap, c.box), n = live ? live.n : null, was = c.count;
  const vars = { box: c.box, n: n != null ? n : "" };
  const by = live && Object.keys(live.by).length ? `<p class="fine">${esc(fmt(t("cl.by"), { list: "" }))} ${Object.entries(live.by).map(([k, v]) => `${rawCode(k)}&nbsp;${v}`).join(" · ")}</p>` : "";
  const changed = c.step === "confirm" && c.phase === "idle" && was != null && n != null && was !== n ? note("warn", fmt(t("cl.changed"), { a: was, b: n })) : "";
  let body, foot;
  if (c.phase === "busy") { body = note("loading", t("cl.busy")); foot = ""; }
  else if (c.phase === "done" || c.phase === "unknown" || c.phase === "error") {
    const rid = c.reqId ? `<p class="fine">${esc(t("wr.id"))} ${rawCode(c.reqId)}</p>` : "";
    const left = c.after === undefined ? "" : c.after === null ? `<p class="fine">${esc(t("cl.afterNone"))}</p>`
      : `<p>${esc(fmt(t("cl.after"), { box: c.box, n: c.after, when: snap.asOf != null ? ago(snap.asOf, now, L) || t("unknown") : t("unknown") }))}</p>${c.after > 0 && c.phase === "done" ? `<p class="fine">${esc(t("cl.afterLeft"))}</p>` : ""}`;
    body = (c.phase === "done" ? note("ok", t("cl.done")) : c.phase === "unknown" ? note("warn", t("cl.unknown")) : note(c.error && c.error.code === "denied" ? "denied" : "error", (c.error && c.error.message) || t("nk.error"))) + left + rid;
    foot = c.phase === "unknown" ? `<button type="button" class="btn" data-act="writes-check" data-fk="cl-check">${esc(t("wr.check"))}</button>` : "";
  } else if (c.step === "ask") {
    body = `<p>${esc(fmt(t(n != null ? "cl.ask" : "cl.askUnknown"), vars))}</p>${by}<p class="fine">${esc(t("cl.notArchive"))}</p>`;
    foot = `<button type="button" class="btn danger" data-act="clear-next" data-fk="clear-next">${esc(t("cl.next"))}</button>`;
  } else {
    body = `<h3>${esc(t("cl.last"))}</h3>${changed}<p>${esc(fmt(t(n != null ? "cl.ask" : "cl.askUnknown"), vars))}</p>${by}
      <label class="chk"><input type="checkbox" data-act="clear-ack" data-fk="clear-ack" ${c.ack ? "checked" : ""}><span>${esc(fmt(t(n != null ? "cl.tick" : "cl.tickUnknown"), vars))}</span></label>`;
    foot = `<button type="button" class="btn danger" data-act="clear-do" data-fk="clear-do" ${canClear(c) ? "" : "disabled"}>${esc(fmt(t(n != null ? "cl.do" : "cl.doUnknown"), vars))}</button>
      <button type="button" class="btn ghost" data-act="clear-back" data-fk="clear-back">${esc(t("cl.back"))}</button>`;
  }
  const mock = snap.capabilities["mailbox.clear"] && snap.capabilities["mailbox.clear"].status === "mock";
  return `<div class="layer" data-layer="clear"><div class="scrim" data-act="close-dialog"></div>
    <div class="dlg narrow" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><header class="dh"><h2 id="dlg-t">${esc(fmt(t("cl.title"), { box: c.box }))}</h2>${mock ? mockChip() : ""}
      <button type="button" class="close-x" data-act="close-dialog" data-fk="close-dialog">${esc(t("close"))}</button></header>
    <div class="sb pad" data-keep="dlg">${body}</div>
    <footer class="df">${foot}<button type="button" class="btn ghost" data-act="close-dialog" data-fk="close-dialog2">${esc(c.phase === "idle" ? t("cancel") : t("close"))}</button></footer></div></div>`;
}

// ---------- the ledger ----------
export const writesChip = (S) => {
  if (!S.writes.length) return "";
  const n = unconfirmed(S.writes).length;
  return `<button type="button" class="act${n ? " warn" : ""}" data-act="writes" data-fk="writes">${esc(t("wr.open"))}${n ? ` <span class="counter" aria-label="${esc(fmt(t("wr.chip"), { n }))}">${n}</span>` : ""}</button>`;
};
export function writesDialog(S, now) {
  const L = getLang(), un = unconfirmed(S.writes);
  const rows = S.writes.map((w) => `<li class="row static wr" data-phase="${esc(w.phase)}"><span class="t1"><b>${esc(t(`wr.kind.${w.kind}`) === `wr.kind.${w.kind}` ? w.kind : t(`wr.kind.${w.kind}`))}</b>
      <span class="badge" data-b="write:${esc(w.phase)}">${esc(t(`wr.phase.${w.phase}`))}</span></span>
      <span class="t2">${esc(w.target || "—")} · ${esc(ago(w.at, now, L) || t("unknown"))} · ${esc(t("wr.id"))} ${rawCode(w.id)}</span>
      ${w.message ? `<span class="t3">${esc(w.message)}</span>` : ""}
      ${w.phase === "unknown" || w.phase === "pending" ? `<span class="actions"><button type="button" class="btn ghost" data-act="writes-dismiss" data-write="${esc(w.id)}" data-fk="wr-d:${esc(w.id)}">${esc(t("wr.dismiss"))}</button></span>` : ""}</li>`).join("");
  return `<div class="layer" data-layer="writes"><div class="scrim" data-act="close-dialog"></div>
    <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlg-t"><header class="dh"><h2 id="dlg-t">${esc(t("wr.title"))}</h2>
      <button type="button" class="close-x" data-act="close-dialog" data-fk="close-dialog">${esc(t("close"))}</button></header>
    <div class="sb pad" data-keep="dlg"><p class="lead">${esc(t("wr.lead"))}</p>
      ${un.length ? note("warn", fmt(t("wr.warn"), { n: un.length })) : ""}
      ${rows ? `<ul class="rows">${rows}</ul>` : note("empty", t("wr.none"))}</div>
    <footer class="df"><button type="button" class="btn primary" data-act="writes-check" data-fk="wr-check">${esc(t("wr.check"))}</button>
      <button type="button" class="btn ghost" data-act="close-dialog" data-fk="close-dialog2">${esc(t("close"))}</button></footer></div></div>`;
}
