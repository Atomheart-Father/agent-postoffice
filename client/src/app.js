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
import { ERR } from "./contract.js";
import { parseHash, formatHash } from "./router.js";
import { drawWires, hoverPath } from "./fx.js";
import { startEdit, buildRequest, isDirty, movedUpstream, validateDraft, reduce as editReduce } from "./flows/roleEdit.js";
import { issuesHtml } from "./views/roleEdit.js";
import { saveTheme, paintTheme, cleanTheme, saveLang } from "./theme.js";
import { roleById } from "./views/ui.js";

export function createApp({ root, toastRoot = null, adapter, now = () => Date.now() / 1000, win = globalThis, doc = globalThis.document, host = null, theme = "paper", lang = "en" }) {
  const r0 = parseHash(win.location && win.location.hash);
  const S = {
    lang, theme, view: r0.view, roleId: r0.roleId, roleTab: r0.roleTab, threadId: r0.threadId,
    snap: null, load: "loading", err: null,
    threadFilter: { role: "", source: "" }, workFilter: { role: r0.workRole }, workPane: "work", workOpen: null,
    edits: {}, drafts: {}, hud: false, hudPin: false, dialog: null,
    newThread: { roleId: "", harness: "", title: "", instruction: "", busy: false, result: null },
    toast: null, composing: false,
  };
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
    if (res.ok) { S.snap = res.data; S.load = "ready"; S.err = null; reconcile(); }
    else { S.err = res.error; if (!S.snap) S.load = "error"; }
    render();
  }
  function load() {
    if (inflight) return inflight;
    inflight = Promise.resolve(adapter.getSnapshot()).then(apply, (e) => apply({ ok: false, error: { code: ERR.NETWORK, message: String(e && e.message || e) } })).finally(() => { inflight = null; });
    return inflight;
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
    const res = await adapter.applyRoleChange(buildRequest(es), es.preview.baseline);
    if (res.ok) { setEdit(role.id, { type: "apply:ok", data: res.data }); await load(); }
    else { setEdit(role.id, { type: "apply:fail", error: res.error }); await load(); }   // the snapshot is refetched either way: a stale or lost reply is judged against the latest
    render();
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
    "close-dialog"() { S.dialog = null; render(); },
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
    const el = target && target.closest ? target.closest("[data-act],[data-lang],[data-theme-choice],[data-roletab],[data-tsrc],[data-wpane]") : null;
    if (!el) { if (S.hud && !(target && target.closest && target.closest("[data-hud]"))) { S.hud = false; S.hudPin = false; render(); } return; }
    const d = el.dataset || {};
    if (d.lang) { S.lang = setLang(d.lang); saveLang(S.lang); render(); return; }
    if (d.themeChoice) { S.theme = cleanTheme(d.themeChoice); saveTheme(S.theme); paintTheme(S.theme); render(); try { win.requestAnimationFrame && win.requestAnimationFrame(() => import("./fx.js").then((m) => m.redrawGround())); } catch { /* optional */ } return; }
    if (d.roletab) { S.roleTab = d.roletab; syncHash(); render(); return; }
    if (d.tsrc !== undefined) { S.threadFilter.source = d.tsrc; render(); return; }
    if (d.wpane) { S.workPane = d.wpane; render(); return; }
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
      const sub = root.querySelector('[data-fk="nt-submit"]');
      if (sub) sub.disabled = S.newThread.busy || !S.newThread.roleId || !S.newThread.title.trim();
    } catch { /* stub */ }
  }
  function input(target, isChange) {
    const el = target && target.closest ? target.closest("[data-edit],[data-nt],[data-draft],[data-tfilter],[data-wfilter],[data-scenario]") : null;
    if (!el) return;
    const d = el.dataset || {}, v = el.value;
    if (d.edit !== undefined) {
      const role = roleById(S.snap, S.roleId); if (!role) return;
      const es = editOf(), before = es.phase;
      S.edits[role.id] = editReduce(es, { type: "input", field: d.edit, value: v });
      if (S.edits[role.id].phase !== before) render(); else syncButtons();
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
      if (S.dialog) { S.dialog = null; render(); return true; }
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
  function hudOut(related, active) { if (S.hud && !S.hudPin && !inHud(related) && !inHud(active)) { S.hud = false; render(); } }
  function hover(target, on) { try { hoverPath(on && target && target.closest ? (target.closest(".seat") || { dataset: {} }).dataset.id || "" : ""); } catch { /* stub */ } }

  return { S, render, redraw: () => { after(); }, load, act: (name, d = {}) => A[name](d), click, input, key, hudIn, hudOut, hover, onHash, syncHash, toast, setComposing(on) { S.composing = on; if (!on && pendingRender) render(); }, setLang(l) { S.lang = setLang(l); render(); } };
}
