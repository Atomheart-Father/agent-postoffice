// The controller. One mutable state object S, one render(), and a table of actions. The DOM events in main.js are
// translated to `act(name, dataset)`, so every behaviour can be driven (and tested) without a browser.
//
// Rules this file keeps:
//   * the backend is the only authority: actions send requests and show what comes back; nothing is granted locally;
//   * a failed or stale read keeps the last snapshot on screen with a banner; it never blanks the page;
//   * drafts (role edits, thread instructions, new-thread form) survive polls, navigation and failures;
//   * text boxes are never repainted while an IME composition is in progress.

import { shell } from "./views/shell.js";
import { paint } from "./paint.js";
import { t, setLang, getLang } from "./i18n.js";
import { ERR, unknownOutcome } from "./contract.js";
import { parseHash, formatHash } from "./router.js";
import { drawWires, hoverPath } from "./fx.js";
import { startEdit, buildRequest, isDirty, movedUpstream, validateDraft, reduce as editReduce } from "./flows/roleEdit.js";
import { issuesHtml } from "./views/roleEdit.js";
import { saveTheme, paintTheme, cleanTheme, saveLang } from "./theme.js";
import { roleById } from "./views/ui.js";
import { openLetter, letterReduce, editRequest, newCompose, composeReduce, canSend, sendRequest, hasContent } from "./flows/mail.js";
import { composeIssuesHtml } from "./views/mail.js";
import { normalizeLetter } from "./contract.js";
import { newRequestId, newClientRequestId, writesReduce, ledgerFull, LIMIT } from "./flows/writes.js";
import { createLedgerStore, defaultStorage } from "./ledgerStore.js";
import { fmt } from "./util.js";
import { newDelivery, deliveryReduce, verifyAgainst, newClear, clearReduce, canClear, lettersIn, mailboxOf, mailboxesOf } from "./flows/mailbox.js";

// The display id of a log record only needs to differ from its neighbours; the CSPRNG is used when there is one.
const cryptoRand = () => { try { const a = new Uint32Array(1); globalThis.crypto.getRandomValues(a); return a[0] / 2 ** 32; } catch { return Math.random(); } };

export function createApp({ root, toastRoot = null, adapter, now = () => Date.now() / 1000, win = globalThis, doc = globalThis.document, host = null, theme = "paper", lang = "en", storage, ledgerBase, newKey = newClientRequestId }) {
  const r0 = parseHash(win.location && win.location.hash);
  const S = {
    lang, theme, view: r0.view, roleId: r0.roleId, roleTab: r0.roleTab, threadId: r0.threadId,
    snap: null, load: "loading", err: null,
    threadFilter: { role: "", source: "" }, workFilter: { role: r0.workRole }, workPane: "work", workOpen: null,
    edits: {}, drafts: {}, hud: false, hudPin: false, dialog: null,
    newThread: { roleId: "", harness: "", title: "", instruction: "", busy: false, result: null },
    toast: null, composing: false,
    // mail: the open letter, compose drafts (one per target, kept when the dialog is closed), the operator's sent list
    mailPane: "in", letter: null, compose: null, composeKey: "", composes: {}, editDrafts: {},
    outbox: { phase: "idle", rows: [], error: null, at: null },
    // writes: the local ledger of every POST (id, kind, phase) - see flows/writes.js; delivery: switch dialog + last result; clear: whole-mailbox clear flow
    writes: [], delivery: newDelivery(), clear: null,
    // ledger: where the request log is kept. store = "unset" until the first snapshot names the operator, then "ok" | "empty" |
    // "unusable" (stored data not read) | "off" (storage missing or failing: the log lives on this page only)
    ledger: { scope: null, store: "unset", seen: new Set() },
  };
  const ledgerStore = createLedgerStore({
    storage: storage === undefined ? defaultStorage(win) : storage,
    base: ledgerBase !== undefined ? ledgerBase : (win.location && win.location.origin) || "",
  });
  let wseq = 0;
  let pendingRender = false, inflight = null, ret = null, lastLayer = null, toastTimer = null, hashSelf = "";
  const narrow = () => (win.innerWidth || 1440) <= 900;
  const $ = (id) => (doc && doc.getElementById ? doc.getElementById(id) : null);

  // ---------- render ----------
  function render() {
    if (S.composing) { pendingRender = true; return; }
    pendingRender = false;
    const n = now();
    const html = shell(S.snap, S, adapter, n);
    const changed = paint(root, html);
    if (toastRoot) toastRoot.textContent = S.toast ? S.toast.text : "";
    if (changed) after();
    syncLayers();
  }
  function after() {
    try { if (doc) { doc.title = `${S.snap && S.snap.company.name ? S.snap.company.name + " · " : ""}BOXZ OFFICE`; doc.documentElement.lang = getLang() === "zh" ? "zh-CN" : "en"; } } catch { /* stub */ }
    if (S.view === "office" && S.snap) {
      const dead = (nd) => { const r = roleById(S.snap, nd.role); return !!r && (!r.incumbent || ["offline", "unconnected"].includes(r.incumbent.presence)); };
      drawWires(dead);
    }
  }
  const toast = (text, kind = "info") => {
    S.toast = { text, kind };
    if (toastRoot) toastRoot.textContent = text;
    if (toastTimer && win.clearTimeout) win.clearTimeout(toastTimer);
    if (win.setTimeout) toastTimer = win.setTimeout(() => { S.toast = null; if (toastRoot) toastRoot.textContent = ""; }, 7000);
  };
  const errText = (e) => `${t("err." + e.code) === "err." + e.code ? "" : t("err." + e.code) + ": "}${e.message || ""}`;

  // ---------- layers: a dialog (or, on narrow screens, the role / thread sheet) makes the rest inert ----------
  function currentLayer() {
    if (S.dialog) return "dialog";
    if (narrow() && S.view === "office" && S.roleId) return "role";
    if (narrow() && S.view === "threads" && S.threadId) return "thread";
    return null;
  }
  function syncLayers() {
    const layer = currentLayer();
    const set = (el, on) => { if (!el) return; try { on ? el.setAttribute("inert", "") : el.removeAttribute("inert"); } catch { /* stub */ } };
    set($("bar"), !!layer);
    try {
      const main = $("main");
      set(main, layer === "dialog");
      if (main && main.querySelector) { set(main.querySelector(".org"), layer === "role"); set(main.querySelector(".tl-list"), layer === "thread"); }
      if (doc && doc.body && doc.body.classList) doc.body.classList.toggle("has-layer", !!layer);
    } catch { /* stub */ }
    if (layer && layer !== lastLayer) {
      try { const el = root.querySelector(layer === "dialog" ? ".dlg .close-x, .dlg input, .dlg select" : layer === "role" ? "#rolepane .close-x" : ".tl-detail .back"); if (el && el.focus) el.focus(); } catch { /* stub */ }
    } else if (!layer && lastLayer && ret) {
      try { const el = root.querySelector(`[data-fk="${ret}"]`); if (el && el.focus) el.focus(); } catch { /* stub */ }
      ret = null;
    }
    lastLayer = layer;
  }
  const remember = (el) => { try { const fk = el && el.getAttribute && el.getAttribute("data-fk"); if (fk) ret = fk; } catch { /* stub */ } };

  // ---------- data ----------
  function reconcile() {
    for (const id of Object.keys(S.edits)) {
      const role = roleById(S.snap, id);
      if (!role) { delete S.edits[id]; continue; }
      const es = S.edits[id];
      if (es.phase === "idle" && !isDirty(es) && movedUpstream(es, role)) S.edits[id] = startEdit(role);   // nothing to lose: follow the role
    }
  }
  function apply(res) {
    if (res.ok) { S.snap = res.data; S.load = "ready"; S.err = null; adoptScope(); reconcile(); }
    else { S.err = res.error; if (!S.snap) S.load = "error"; }
    render();
  }
  function load() {
    if (inflight) return inflight;
    inflight = Promise.resolve(adapter.getSnapshot()).then(apply, (e) => apply({ ok: false, error: { code: ERR.NETWORK, message: String(e && e.message || e) } })).finally(() => { inflight = null; });
    return inflight.then(() => { if (S.view === "work" && S.mailPane === "sent") return loadOutbox(); });
  }

  // A read that STARTS after now. load() joins a read already in flight, which began before the write that just finished and so
  // can still show the old state; anything that compares "what we asked" with "what the server says" must not use that one.
  async function loadFresh() { if (inflight) { try { await inflight; } catch { /* the next read decides */ } } return load(); }

  // ---------- the request log ----------
  // The records of one (backend, operator) scope are restored from storage when the first snapshot says who the operator is.
  // Restoring only fills the list: it sends nothing, and a record that was pending is shown as unknown (its reply is gone).
  function adoptScope() {
    if (!S.snap) return;
    const sc = ledgerStore.scopeOf(S.snap.company.operatorBox), key = ledgerStore.keyOf(sc);
    if (S.ledger.scope === key) return;
    const got = ledgerStore.load(S.snap.company.operatorBox);
    // Records made on this page before the scope was known (none today) stay on top; stored ones follow, without duplicates.
    const here = S.ledger.scope === null ? S.writes : [];
    const ids = new Set(here.map((w) => w.id));
    S.writes = [...here, ...got.items.filter((w) => !ids.has(w.id))];
    S.ledger = { scope: key, store: got.state, seen: new Set(S.writes.map((w) => w.id)) };
  }
  function persist() {
    if (S.ledger.scope === null || !S.snap) return;
    for (const w of S.writes) S.ledger.seen.add(w.id);
    const r = ledgerStore.save(S.snap.company.operatorBox, S.writes, S.ledger.seen);
    S.ledger = { ...S.ledger, store: r };
  }
  const logEv = (ev) => { S.writes = writesReduce(S.writes, ev); persist(); };

  // Every write goes through here: it gets a record (pending) before it is sent, and the record is closed by the reply. A throw
  // or an unreadable reply is "unknown", never "failed". Nothing is retried. `opts.key` = a send operation: it gets ONE fresh
  // client_request_id here, passed to `run(id, key)`; the key belongs to this operation and to no other.
  // A full log (LIMIT records, none of them resolvable by the client) refuses new writes before anything is sent.
  async function tracked(kind, target, run, opts = {}) {
    if (ledgerFull(S.writes)) {
      return { ok: false, error: { code: ERR.REFUSED, message: fmt(t("wr.fullMsg"), { n: LIMIT }), retryable: false, blockedLocally: true } };
    }
    const id = newRequestId(++wseq, cryptoRand);
    const key = opts.key ? newKey() : null;
    logEv({ type: "start", id, kind, target, at: now(), clientRequestId: key });
    render();   // the top bar shows "awaiting confirmation" from the moment the request leaves
    let res;
    try { res = await run(id, key); } catch (e) { res = unknownOutcome(`The request ended without a usable answer (${String((e && e.message) || e)}). It may have been sent.`); }
    const ref = res.ok && res.data ? (typeof res.data.ref === "string" && res.data.ref ? res.data.ref : typeof res.data.id === "string" ? res.data.id : "") : "";
    logEv(res.ok ? { type: "ok", id, at: now(), ref: kind === "send" ? ref : "", deduped: !!(res.data && res.data.deduped === true) } : { type: "fail", id, error: res.error, at: now() });
    return res.ok ? { ...res, data: { ...(res.data && typeof res.data === "object" ? res.data : {}), reqId: id } } : { ...res, error: { ...res.error, reqId: id } };
  }

  // ---------- hash ----------
  function syncHash() {
    try { const h = formatHash(S); if (win.location && win.location.hash !== h) { hashSelf = h; win.location.hash = h; } } catch { /* stub */ }
  }
  function onHash() {
    const h = win.location && win.location.hash;
    if (h === hashSelf) return;
    const r = parseHash(h);
    S.view = r.view; S.roleId = r.roleId; S.roleTab = r.roleTab; S.threadId = r.threadId; S.workFilter.role = r.workRole;
    render();
  }

  // ---------- role edit ----------
  const editOf = () => { const role = roleById(S.snap, S.roleId); return role ? (S.edits[role.id] || (S.edits[role.id] = startEdit(role))) : null; };
  const setEdit = (id, ev) => { S.edits[id] = editReduce(S.edits[id], ev); };
  async function edPreview() {
    const role = roleById(S.snap, S.roleId), es = editOf(); if (!role || !es || es.phase === "previewing" || es.phase === "applying") return;
    if (!isDirty(es) || validateDraft(es).length) return;
    setEdit(role.id, { type: "preview:start" }); render();
    const res = await adapter.previewRoleChange(buildRequest(es));
    setEdit(role.id, res.ok ? { type: "preview:ok", data: res.data.diff } : { type: "preview:fail", error: res.error }); render();
  }
  // Apply sends the request that was previewed plus the baseline the preview returned, untouched. It is never
  // retried automatically: a lost reply can mean the config WAS saved, so the only follow-up is to look again.
  async function edApply() {
    const role = roleById(S.snap, S.roleId), es = editOf(); if (!role || !es || es.phase !== "preview" || !es.preview.baseline) return;
    setEdit(role.id, { type: "apply:start" }); render();
    const res = await tracked("role-apply", role.id, () => adapter.applyRoleChange(buildRequest(es), es.preview.baseline));
    if (res.ok) { setEdit(role.id, { type: "apply:ok", data: res.data }); await loadFresh(); }
    else { setEdit(role.id, { type: "apply:fail", error: res.error }); await loadFresh(); }   // the snapshot is refetched either way: a stale or lost reply is judged against the latest
    render();
  }

  // ---------- mail ----------
  const mailCapOn = (k) => { const c = S.snap && S.snap.capabilities[k]; return !!c && c.status !== "missing"; };
  const operatorBox = () => (S.snap ? S.snap.company.operatorBox : "");
  async function loadOutbox() {
    if (!S.snap || !mailCapOn("mail.outbox") || !operatorBox() || S.outbox.phase === "loading") return;
    S.outbox = { ...S.outbox, phase: "loading" }; render();
    const res = await adapter.listOutbox();
    S.outbox = res.ok ? { phase: "ok", rows: res.data.rows, error: null, at: now() } : { ...S.outbox, phase: "ok", error: res.error };   // a failed read keeps the last rows on screen, with the error beside them
    render();
  }
  const setLetter = (ev) => { if (S.letter) { S.letter = letterReduce(S.letter, ev); } };
  const sameLetter = (box, id) => S.letter && S.letter.box === box && S.letter.id === id;
  async function readLetter() {
    const l = S.letter; if (!l) return;
    const { box, id } = l; render();
    const res = await adapter.readLetter({ box, id });
    if (!sameLetter(box, id)) return;   // the user moved on; this reply belongs to a letter no longer on screen
    setLetter(res.ok ? { type: "read:ok", data: res.data } : { type: "read:fail", error: res.error }); render();
  }
  function openInbox(d, el) {
    remember(el);
    const m = S.snap && S.snap.mail.find((x) => x.id === d.mail);
    S.letter = openLetter({ src: "inbox", box: d.box, id: d.id, subject: m ? m.subject : "", sender: m ? m.from : "", delivery: m ? m.delivery : null, rawStatus: m ? m.sourceStatus : null, mailId: d.mail || "" });
    S.dialog = "letter"; return readLetter();
  }
  function openSent(d, el) {
    remember(el);
    const row = S.outbox.rows.find((r) => r.to === d.to && r.id === d.id); if (!row) return;
    S.letter = { ...openLetter({ src: "sent", box: row.to, id: row.id, subject: row.subject, sender: operatorBox() }), phase: "ok", data: normalizeLetter({ id: row.id, box: row.to, sender: operatorBox(), subject: row.subject, need: row.need, body: row.body }) };
    S.dialog = "letter"; render();
  }
  // One write from the letter dialog. The reply is shown as the backend gave it; the snapshot (and the sent list when it
  // is the sent list's letter) is re-read either way, because a lost reply or a refusal can only be judged against that.
  async function letterWrite(kind, run) {
    const l = S.letter; if (!l || (l.act && l.act.phase === "busy")) return;
    const { box, id } = l;
    setLetter({ type: "act:start", kind }); render();
    const res = await tracked(kind, `${box}/${id}`, run);
    if (sameLetter(box, id)) setLetter(res.ok ? { type: "act:ok", result: res.data } : { type: "act:fail", error: res.error });
    render();
    await loadFresh(); if (l.src === "sent" || S.outbox.phase === "ok") await loadOutbox();
  }
  const doLetter = (kind) => {
    const l = S.letter; if (!l) return;
    if (kind === "ack") return letterWrite("ack", () => adapter.ackMail({ box: l.box, id: l.id, note: (l.act && l.act.note) || "" }));
    if (kind === "archive") return letterWrite("archive", () => adapter.archiveMail({ box: l.box, id: l.id }));
    if (kind === "retract") return letterWrite("retract", () => adapter.retractMail({ to: l.box, id: l.id }));
  };
  const stashEdit = () => { const l = S.letter; if (l && l.edit && l.data && editRequest(l)) S.editDrafts[`${l.box}/${l.id}`] = l.edit; };
  function closeDialog() {
    if (S.dialog === "letter") { stashEdit(); S.letter = null; }
    if (S.dialog === "compose") { if (S.compose && S.compose.phase === "sent") delete S.composes[S.composeKey]; S.compose = null; S.composeKey = ""; }
    if (S.dialog === "clear") S.clear = null;
    S.dialog = null; render();
  }
  function openCompose(key, make, el) {
    remember(el);
    const have = S.composes[key];
    S.composeKey = key; S.composes[key] = have && have.phase !== "sent" ? have : make(); S.compose = S.composes[key];
    S.dialog = "compose"; render();
  }
  const setCompose = (ev) => { S.composes[S.composeKey] = composeReduce(S.composes[S.composeKey], ev); S.compose = S.composes[S.composeKey]; };
  async function fileOriginal() {
    const c = S.compose; if (!c || !c.replyOf) return;
    const key = S.composeKey, { box, id } = c.replyOf;
    setCompose({ type: "archive:start" }); render();
    const res = await tracked("archive", `${box}/${id}`, () => adapter.archiveMail({ box, id }));
    if (S.composeKey === key && S.composes[key]) setCompose(res.ok ? { type: "archive:ok", result: res.data } : { type: "archive:fail", error: res.error });
    render(); await loadFresh();
  }
  async function sendCompose() {
    const c = S.compose; if (!c || !canSend(c, operatorBox())) return;
    const key = S.composeKey;
    setCompose({ type: "send:start" }); render();
    const req = sendRequest(c);
    // One click = one operation = one key. The body is built once, here, and is the same object for the whole operation. Clicking
    // again while it is in flight does nothing (phase "sending"); a send after the draft changed, or after an unknown result, is a
    // new operation with a new key - the earlier record stays in the log either way.
    const res = await tracked("send", req.to, (_, key) => adapter.sendMail(key ? { ...req, client_request_id: key } : req), { key: true });
    if (!S.composes[key]) { await loadFresh(); return; }   // discarded while the request was out: the letter was still sent, so only refresh
    S.composeKey = key; setCompose(res.ok ? { type: "send:ok", result: res.data } : { type: "send:fail", error: res.error }); render();
    if (res.ok) { await loadFresh(); if (S.compose && S.compose.mode === "reply" && S.compose.fileOriginal) await fileOriginal(); else if (S.outbox.phase === "ok") await loadOutbox(); }
    else if (res.error.outcomeUnknown) await loadFresh();   // look at the mailboxes now: the letter may be there
  }

  // ---------- delivery switches ----------
  // The buttons never flip by themselves: a request is sent, the snapshot is read again, and the result is shown as "asked vs
  // what the server now says". `req` is { status, names } or { status, group } (group without "@").
  const deliveryCapOn = () => mailCapOn("mailbox.status");
  async function doDelivery(req) {
    if (!deliveryCapOn() || S.delivery.phase === "busy") return;
    if (req.group ? !req.group : !(req.names && req.names.length)) return;
    S.delivery = deliveryReduce(S.delivery, { type: "start", req }); render();
    const target = req.group ? `@${req.group}` : req.names.join(", ");
    const res = await tracked("status", `${req.status} · ${target}`, () => adapter.setDelivery(req.group ? { group: req.group, status: req.status } : { names: req.names, status: req.status }));
    S.delivery = deliveryReduce(S.delivery, res.ok ? { type: "ok", reqId: res.data.reqId } : { type: "fail", error: res.error }); render();
    await loadFresh();
    S.delivery = deliveryReduce(S.delivery, { type: "verify", verify: S.err || !S.snap ? null : verifyAgainst(S.snap, req) }); render();
  }

  // ---------- clear the whole mailbox ----------
  function openClear(box, el) {
    if (!mailCapOn("mailbox.clear") || !S.snap) return;
    const n = lettersIn(S.snap, box);
    remember(el);
    S.clear = newClear(box, n ? n.n : null); S.dialog = "clear"; render();
  }
  async function doClear() {
    const c = S.clear; if (!c || !canClear(c)) return;
    const box = c.box;
    S.clear = clearReduce(c, { type: "start" }); render();
    const res = await tracked("clear", box, () => adapter.clearMailbox({ name: box }));
    if (S.clear && S.clear.box === box) S.clear = clearReduce(S.clear, res.ok ? { type: "ok", reqId: res.data.reqId } : { type: "fail", error: res.error });
    render();
    await loadFresh();
    if (S.clear && S.clear.box === box) { const n = S.err ? null : lettersIn(S.snap, box); S.clear = clearReduce(S.clear, { type: "after", count: n ? n.n : null }); render(); }
  }

  // ---------- other requests ----------
  async function submitNewThread() {
    const f = S.newThread; if (f.busy || !f.roleId || !f.title.trim()) return;
    f.busy = true; f.result = null; render();
    f.result = await adapter.createThread({ roleId: f.roleId, harness: f.harness, title: f.title.trim(), instruction: f.instruction });
    f.busy = false; if (f.result.ok) load(); render();
  }
  async function sendThread(id) {
    const key = "send:" + id, text = (S.drafts[key] || "").trim(); if (!text) return;
    const res = await adapter.sendToThread(id, text);
    if (res.ok) { delete S.drafts[key]; toast(res.data.note || t("th.accepted"), "ok"); load(); } else toast(errText(res.error), "error");   // the draft stays on failure
    render();
  }
  async function decide(id, decision) {
    const res = await adapter.decideApproval(id, decision);
    if (!res.ok) toast(errText(res.error), "error"); else toast(t(decision === "approved" ? "ap.approved" : "ap.denied"), "ok");
    await load();
  }

  // ---------- actions (data-act) ----------
  const A = {
    go(d) {
      S.view = d.view; if (d.role !== undefined && d.view === "work") S.workFilter.role = d.role;
      syncHash(); render();
    },
    role(d, el) { remember(el); if (S.roleId !== d.role) S.roleTab = "overview"; S.roleId = d.role; S.view = "office"; syncHash(); render(); },
    "close-role"() { S.roleId = null; syncHash(); render(); },
    thread(d, el) { remember(el); S.threadId = d.thread; S.view = "threads"; syncHash(); render(); },
    "close-thread"() { S.threadId = null; syncHash(); render(); },
    hud() { if (S.hud && S.hudPin) { S.hud = false; S.hudPin = false; } else { S.hud = true; S.hudPin = true; } render(); },
    caps(d, el) { remember(el); S.dialog = "caps"; render(); },
    "close-dialog"() { closeDialog(); },
    letter(d, el) { return openInbox(d, el); },
    "sent-open"(d, el) { return openSent(d, el); },
    "letter-reload"() { if (S.letter) { setLetter({ type: "read:start" }); return readLetter(); } },
    "letter-ask"(d) { if (S.letter) { setLetter({ type: "act:ask", kind: d.kind }); render(); } },
    "letter-cancel"() { setLetter({ type: "act:cancel" }); render(); },
    "letter-do"(d) { return doLetter(d.kind); },
    "letter-reply"(d, el) {
      const l = S.letter; if (!l || !l.data) return;
      const replyOf = { box: l.box, id: l.id, sender: l.data.sender, subject: l.data.subject };
      stashEdit(); S.letter = null;
      return openCompose(`reply:${l.box}/${l.id}`, () => newCompose({ replyOf, operatorBox: operatorBox() }), el);
    },
    "letter-edit"() { const l = S.letter; if (!l) return; setLetter({ type: "edit:open", draft: S.editDrafts[`${l.box}/${l.id}`] }); delete S.editDrafts[`${l.box}/${l.id}`]; render(); },
    "letter-edit-close"() { stashEdit(); setLetter({ type: "edit:close" }); render(); },
    "letter-save"() { const l = S.letter, req = l && editRequest(l); if (req) return letterWrite("edit", () => adapter.editMail(req)); },
    compose(d, el) { return openCompose("new", () => newCompose({ operatorBox: operatorBox() }), el); },
    "compose-send"() { return sendCompose(); },
    "compose-refile"() { return fileOriginal(); },
    "compose-check"() { S.dialog = null; S.compose = null; S.composeKey = ""; S.view = "work"; S.mailPane = "sent"; S.workPane = "mail"; syncHash(); render(); return loadOutbox(); },
    "compose-discard"() { delete S.composes[S.composeKey]; S.compose = null; S.composeKey = ""; S.dialog = null; render(); },
    "mail-refresh"() { return load().then(() => (S.outbox.phase === "ok" ? loadOutbox() : null)); },
    "outbox-refresh"() { return loadOutbox(); },
    writes(d, el) { remember(el); S.dialog = "writes"; render(); },
    "writes-check"() {
      return loadFresh().then(() => (S.outbox.phase === "ok" ? loadOutbox() : null)).then(() => { if (!S.err) { logEv({ type: "checked", at: now() }); render(); } });
    },
    "writes-dismiss"(d) { logEv({ type: "dismiss", id: d.write }); render(); },
    delivery(d, el) { remember(el); S.delivery = deliveryReduce(S.delivery, { type: "reset" }); S.dialog = "delivery"; render(); },
    "dl-pick"(d, el) { S.delivery = deliveryReduce(S.delivery, { type: "select", box: d.box, on: !!(el && el.checked) }); render(); },
    "dl-all"() { S.delivery = deliveryReduce(S.delivery, { type: "select:set", boxes: mailboxesOf(S.snap).map((m) => m.box) }); render(); },
    "dl-none"() { S.delivery = deliveryReduce(S.delivery, { type: "select:set", boxes: [] }); render(); },
    "dl-group-pick"(d) { const g = (S.snap.groups || []).find((x) => x.name === d.group); S.delivery = deliveryReduce(S.delivery, { type: "select:set", boxes: g ? g.members : [] }); render(); },
    "dl-sel"(d) { return doDelivery({ status: d.status, names: S.delivery.sel.slice() }); },
    "dl-group"(d) { return doDelivery({ status: d.status, group: d.group }); },
    "dl-one"(d) { return doDelivery({ status: d.status, names: [d.box] }); },
    "clear-ask"(d, el) { return openClear(d.box, el); },
    "clear-next"() { if (S.clear) { S.clear = clearReduce(S.clear, { type: "next" }); render(); } },
    "clear-back"() { if (S.clear) { S.clear = clearReduce(S.clear, { type: "back" }); render(); } },
    "clear-ack"(d, el) { if (S.clear) { S.clear = clearReduce(S.clear, { type: "ack", on: !!(el && el.checked) }); render(); } },
    "clear-do"() { return doClear(); },
    newthread(d, el) { remember(el); S.newThread = { roleId: d.role || S.roleId || "", harness: "", title: "", instruction: "", busy: false, result: null }; S.dialog = "newthread"; render(); },
    "nt-submit"() { return submitNewThread(); },
    send(d) { return sendThread(d.thread); },
    approve(d) { return decide(d.ap, "approved"); },
    deny(d) { return decide(d.ap, "denied"); },
    "work-open"(d) { S.workOpen = S.workOpen === d.work ? null : d.work; render(); },
    reload() { S.err = null; render(); return load(); },
    "ed-preview"() { return edPreview(); },
    "ed-apply"() { return edApply(); },
    "ed-back"() { const r = roleById(S.snap, S.roleId); if (r) { setEdit(r.id, { type: "back" }); render(); } },
    "ed-discard"() { const r = roleById(S.snap, S.roleId); if (r) { S.edits[r.id] = startEdit(r); render(); } },
    "ed-reset"() { const r = roleById(S.snap, S.roleId); if (r) { S.edits[r.id] = startEdit(r); render(); } },
    async "ed-rebase"() { const r = roleById(S.snap, S.roleId); if (!r) return; await load(); const latest = roleById(S.snap, r.id); if (!latest) return; setEdit(r.id, { type: "rebase", role: latest }); render(); return edPreview(); },
    "ed-retry"() { return edPreview(); },
    async "ed-refresh"() { await load(); render(); },
  };

  function click(target) {
    const el = target && target.closest ? target.closest("[data-act],[data-lang],[data-theme-choice],[data-roletab],[data-tsrc],[data-wpane],[data-mbox]") : null;
    if (!el) { if (S.hud && !(target && target.closest && target.closest("[data-hud]"))) { S.hud = false; S.hudPin = false; render(); } return; }
    const d = el.dataset || {};
    if (d.lang) { S.lang = setLang(d.lang); saveLang(S.lang); render(); return; }
    if (d.themeChoice) { S.theme = cleanTheme(d.themeChoice); saveTheme(S.theme); paintTheme(S.theme); render(); try { win.requestAnimationFrame && win.requestAnimationFrame(() => import("./fx.js").then((m) => m.redrawGround())); } catch { /* optional */ } return; }
    if (d.roletab) { S.roleTab = d.roletab; syncHash(); render(); return; }
    if (d.tsrc !== undefined) { S.threadFilter.source = d.tsrc; render(); return; }
    if (d.wpane) { S.workPane = d.wpane; render(); return; }
    if (d.mbox) { S.mailPane = d.mbox; render(); if (d.mbox === "sent") return loadOutbox(); return; }
    if (d.act && A[d.act]) { const out = A[d.act](d, el); if (out && out.catch) out.catch((e) => { toast(String(e && e.message || e), "error"); render(); }); }
  }
  // text typed into a field must not repaint the page (IME safety); only dependent buttons are updated
  function syncButtons() {
    try {
      const es = S.snap && roleById(S.snap, S.roleId) ? S.edits[S.roleId] : null;
      const pv = root.querySelector('[data-fk="ed-preview"]'), ds = root.querySelector('[data-fk="ed-discard"]');
      if (es && pv) {
        const bad = validateDraft(es).length > 0;
        pv.disabled = !isDirty(es) || bad; if (ds) ds.disabled = !isDirty(es);
        const box = root.querySelector("[data-issues]"); if (box) box.innerHTML = issuesHtml(es);   // a sibling of the field, so typing and IME are untouched
      }
      const cs = root.querySelector('[data-fk="compose-send"]');
      if (cs && S.compose) { cs.disabled = !canSend(S.compose, operatorBox()); const box = root.querySelector("[data-issues]"); if (box) box.innerHTML = composeIssuesHtml(S.compose, operatorBox()); }
      const sv = root.querySelector('[data-fk="le-save"]');
      if (sv && S.letter) sv.disabled = !editRequest(S.letter) || !!(S.letter.act && S.letter.act.phase === "busy");
      const sub = root.querySelector('[data-fk="nt-submit"]');
      if (sub) sub.disabled = S.newThread.busy || !S.newThread.roleId || !S.newThread.title.trim();
    } catch { /* stub */ }
  }
  function input(target, isChange) {
    const el = target && target.closest ? target.closest("[data-edit],[data-nt],[data-draft],[data-tfilter],[data-wfilter],[data-scenario],[data-cp],[data-le],[data-letter-note]") : null;
    if (!el) return;
    const d = el.dataset || {}, v = el.value;
    if (d.edit !== undefined) {
      const role = roleById(S.snap, S.roleId); if (!role) return;
      const es = editOf(), before = es.phase;
      S.edits[role.id] = editReduce(es, { type: "input", field: d.edit, value: v });
      if (S.edits[role.id].phase !== before) render(); else syncButtons();
    } else if (d.cp !== undefined) {
      if (!S.compose) return;
      const before = S.compose.phase;
      setCompose({ type: "input", field: d.cp, value: d.cp === "fileOriginal" ? el.checked : v });
      if (S.compose.phase !== before) render(); else syncButtons();   // typing never repaints the form (IME); only the send button and the issue list follow
    } else if (d.le !== undefined) {
      if (!S.letter || !S.letter.edit) return;
      setLetter({ type: "edit:input", field: d.le, value: v }); syncButtons();
    } else if (d.letterNote !== undefined) {
      if (S.letter && S.letter.act && S.letter.act.phase === "confirm") setLetter({ type: "act:note", value: v });
    } else if (d.nt !== undefined) {
      S.newThread[d.nt] = v; if (d.nt === "roleId") { S.newThread.harness = ""; render(); } else syncButtons();
    } else if (d.draft !== undefined) S.drafts[d.draft] = v;
    else if (isChange && d.tfilter) { S.threadFilter[d.tfilter] = v; render(); }
    else if (isChange && d.wfilter) { S.workFilter[d.wfilter] = v; syncHash(); render(); }
    else if (isChange && d.scenario !== undefined && adapter.setScenario) { adapter.setScenario(v); load(); }
  }
  // ---------- keyboard and hover ----------
  function key(e) {
    if (e.key === "Escape") {
      if (S.hud) { S.hud = false; S.hudPin = false; render(); return true; }
      if (S.dialog) { closeDialog(); return true; }
      if (S.view === "threads" && S.threadId && narrow()) { S.threadId = null; syncHash(); render(); return true; }
      if (S.view === "office" && S.roleId && (narrow() || (doc && doc.activeElement && doc.activeElement.closest && doc.activeElement.closest("#rolepane")))) { S.roleId = null; syncHash(); render(); return true; }
    }
    if (e.key === "Tab") {
      const box = root.querySelector && (S.dialog ? root.querySelector(".dlg") : narrow() && S.view === "office" && S.roleId ? root.querySelector("#rolepane") : narrow() && S.view === "threads" && S.threadId ? root.querySelector(".tl-detail") : null);
      if (box) {
        const f = [...box.querySelectorAll('button:not([disabled]),a[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')].filter((x) => !x.hidden);
        if (f.length) {
          const a = doc.activeElement, i = f.indexOf(a);
          if (e.shiftKey && (i <= 0)) { f[f.length - 1].focus(); return true; }
          if (!e.shiftKey && (i === f.length - 1)) { f[0].focus(); return true; }
          if (i < 0) { f[0].focus(); return true; }
        }
      }
    }
    return false;
  }
  const inHud = (el) => !!(el && el.closest && el.closest("[data-hud]"));
  function hudIn(target) { if (inHud(target) && !S.hud) { S.hud = true; S.hudPin = false; render(); } }
  // Leaving the chip with Tab: the next element receives focus only after this handler returns, so repainting right now
  // would replace it and drop focus to <body> (a keyboard trap at the top bar). Let the move finish, then repaint; paint()
  // restores focus by data-fk. Pointer leaves (no related element) repaint at once.
  function hudOut(related, active) {
    if (S.hud && !S.hudPin && !inHud(related) && !inHud(active)) { S.hud = false; if (related && win.setTimeout) win.setTimeout(render, 0); else render(); }
  }
  function hover(target, on) { try { hoverPath(on && target && target.closest ? (target.closest(".seat") || { dataset: {} }).dataset.id || "" : ""); } catch { /* stub */ } }

  return { S, render, redraw: () => { after(); }, load, act: (name, d = {}) => A[name](d), click, input, key, hudIn, hudOut, hover, onHash, syncHash, toast, setComposing(on) { S.composing = on; if (!on && pendingRender) render(); }, setLang(l) { S.lang = setLang(l); render(); } };
}
