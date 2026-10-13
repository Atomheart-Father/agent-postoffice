// Client views and controller under Node (no browser): every state renders, every data-act is wired, both languages
// are complete, drafts survive, and a missing number is never drawn as 0.
import { createApp } from "../client/src/app.js";
import { createMemoryLocks } from "../client/src/flows/writer.js";
import { createMockAdapter, SCENARIOS } from "../client/src/adapters/mock.js";
import { setLang, missing, has } from "../client/src/i18n.js";
import { STR } from "../client/src/strings.js";
import { PRESENCE, DELIVERY, WORK_STATES, CONTROL } from "../client/src/contract.js";
import { hudCard } from "../client/src/views/hud.js";
import { mapMasterState } from "../client/src/adapters/http.js";
import { normalizeUsage } from "../client/src/contract.js";
import { readFileSync } from "node:fs";

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const is = (g, w, m) => (Object.is(g, w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));
const yes = (c, m) => (c ? ok(m) : bad(m));
const hasS = (h, n, m) => (String(h).includes(n) ? ok(m) : bad(m, `missing ${JSON.stringify(n)}`));
const hasNot = (h, n, m) => (String(h).includes(n) ? bad(m, `unexpected ${JSON.stringify(n)}`) : ok(m));

function boot({ hash = "", scenario = "default", width = 1440 } = {}) {
  const root = { innerHTML: "", querySelectorAll() { return []; }, querySelector() { return null; }, contains() { return false; } };
  const win = { location: { hash }, innerWidth: width, setTimeout: () => 0, clearTimeout() {} };
  const adapter = createMockAdapter({ scenario, latency: 0 });
  const app = createApp({ root, toastRoot: { textContent: "" }, adapter, win, doc: null, locks: createMemoryLocks().tab() });
  return { app, root, adapter, S: app.S };
}
const target = (dataset, extra = {}) => ({ closest: (sel) => (dataset ? { dataset, getAttribute: () => null, ...extra } : null), ...extra });

setLang("en");

// ---- both languages are complete ----
for (const [k, v] of Object.entries(STR)) if (!(Array.isArray(v) && v.length === 2 && v[0] !== "" && v[1] !== "")) bad(`string ${k} has both languages`);
ok("every string has a Chinese and an English text");
for (const p of PRESENCE) yes(has("p." + p), `presence word ${p}`);
for (const d of DELIVERY) yes(has("d." + d), `delivery word ${d}`);
for (const w of WORK_STATES) yes(has("w." + w), `work word ${w}`);
for (const c of CONTROL) yes(has("c." + c) && has("ctl." + c), `control words ${c}`);

// ---- walk every view in both languages; no key may be missing ----
for (const lang of ["en", "zh"]) {
  setLang(lang);
  const { app, root, S } = boot();
  await app.load();
  const tabs = ["overview", "threads", "work", "edit"];
  for (const id of ["research-lead", "analyst", "archivist", "reviewer", "owner", "research"]) for (const tab of tabs) { S.view = "office"; S.roleId = id; S.roleTab = tab; app.render(); }
  for (const th of ["t-scan", "t-pricing", "t-import", "t-pr41", "t-release", "t-vendor", "t-reading", ""]) { S.view = "threads"; S.threadId = th || null; app.render(); }
  S.view = "work"; for (const o of ["w-summary", "w-pricing", null]) { S.workOpen = o; for (const p of ["work", "mail"]) { S.workPane = p; app.render(); } }
  S.dialog = "caps"; app.render(); S.dialog = "newthread"; S.newThread.roleId = "builder"; app.render(); S.dialog = null; S.hud = true; S.view = "office"; app.render();
}
is([...missing].join(","), "", "no wording key is missing in any view or language");
setLang("en");

// ---- states ----
{
  const e = boot({ scenario: "empty" }); await e.app.load(); hasS(e.root.innerHTML, "No roles in this Office yet", "empty office says why");
  const n = boot({ scenario: "network" }); await n.app.load(); hasS(n.root.innerHTML, "Cannot reach the backend", "first-load failure shows the error"); hasS(n.root.innerHTML, 'data-act="reload"', "…with a retry");
  const g = boot({ scenario: "degraded" }); await g.app.load(); hasS(g.root.innerHTML, "Presence feed is unavailable", "degraded says what is unavailable"); hasNot(g.root.innerHTML, 'class="pw">Connected<', "degraded: no seat claims to be connected");
  // a failed refresh keeps the data on screen
  const k = boot(); await k.app.load(); k.adapter.setScenario("network"); await k.app.load();
  hasS(k.root.innerHTML, "still showing data", "a failed refresh keeps the last data with a banner"); hasS(k.root.innerHTML, "Northwind Studio", "…and the company is still drawn");
}

// ---- the office tree comes from the snapshot ----
{
  const { app, root, S } = boot(); await app.load();
  const h = root.innerHTML;
  is((h.match(/class="col"/g) || []).length, 3, "one column per branch of the root");
  for (const t of ["Research Lead", "Engineering Lead", "Archivist", "Ops Coordinator"]) hasS(h, t, `role ${t} is drawn`);
  hasS(h, "Vacant", "a role without an incumbent says vacant"); hasS(h, "2 candidates", "…and how many candidates stand by");
  for (const w of ["Connected", "Busy", "Power-save", "Offline", "Not connected", "Unknown"]) hasS(h, `>${w}<`, `status word ${w} is printed next to its mark`);
  hasS(h, 'data-p="unconnected"', "marks carry the state as an attribute, not only a colour");
  hasS(h, "need you", "the call to action counts what waits for the operator");
}

// ---- navigation, deep links, role pane ----
{
  const { app, root, S } = boot({ hash: "#/office/builder/edit" }); await app.load();
  is(S.roleId, "builder", "deep link opens the role"); is(S.roleTab, "edit", "…on the edit tab"); hasS(root.innerHTML, 'id="rolepane"', "the pane is drawn");
  app.click(target({ roletab: "threads" })); is(S.roleTab, "threads", "tab click"); is(app.S.view, "office", "stays in office");
  app.click(target({ act: "thread", thread: "t-import" })); is(S.view, "threads", "a thread link switches view"); is(S.threadId, "t-import", "…and selects it");
  app.click(target({ act: "go", view: "work", role: "builder" })); is(S.workFilter.role, "builder", "go to work with a role filter");
  app.click(target({ act: "close-role" })); is(S.roleId, null, "close role");
}

// ---- role edit end to end through the controller (the contract's own fields, nothing else) ----
const inpOf = (app) => (field, value) => app.input({ closest: () => ({ dataset: { edit: field }, value }) }, false);
const spyApply = (adapter) => { const calls = []; const f = adapter.applyRoleChange; adapter.applyRoleChange = (...a) => { calls.push(a); return f(...a); }; return calls; };
{
  const { app, root, S, adapter } = boot({ hash: "#/office/analyst/edit" }); await app.load();
  const inp = inpOf(app); const applies = spyApply(adapter);
  for (const f of ["candidates", "escalation", "duties", "resources"]) hasS(root.innerHTML, `data-edit="${f}"`, `the form has the ${f} field`);
  hasNot(root.innerHTML, 'data-edit="summary"', "no summary field (not in the write contract)"); hasNot(root.innerHTML, 'data-edit="level"', "no qualification field in the form");
  hasS(root.innerHTML, "Qualification, policy and capability assignment are still design", "the richer fields are said to be design, not wired");
  inp("duties", "Collect pricing data\nCite every figure");
  is(S.edits.analyst.phase, "idle", "typing does not leave the draft state");
  await app.act("ed-preview"); is(S.edits.analyst.phase, "preview", "preview phase"); hasS(root.innerHTML, "Added", "the diff is shown"); hasS(root.innerHTML, "Confirm and apply", "confirm is a separate step");
  hasS(root.innerHTML, "even outside this role", "the page says the baseline covers the whole configuration");
  await app.act("ed-apply"); is(S.edits.analyst.phase, "done", "done"); is(applies.length, 1, "apply was sent once");
  hasS(root.innerHTML, "authoritative configuration is written", "result says the config is saved"); hasS(root.innerHTML, "Derived cards are fresh", "…and, separately, that the cards are fresh");
  is(S.snap.roles.find((r) => r.id === "analyst").duties.join("|"), "Collect pricing data|Cite every figure", "the snapshot was reloaded from the backend");
  await app.act("ed-apply"); is(applies.length, 1, "pressing apply again after done sends nothing");
  // denied role: no form
  S.roleId = "reviewer"; S.roleTab = "edit"; app.render(); hasS(root.innerHTML, "role.edit on Engineering", "a role the actor may not edit shows the backend's reason"); hasNot(root.innerHTML, 'data-edit="duties"', "…and offers no form");
  // limits are shown next to the field and block the preview
  S.roleId = "analyst"; await app.act("ed-reset"); is(S.edits.analyst.phase, "idle", "Done starts a fresh draft"); inp("duties", "1\n2\n3\n4\n5\n6"); app.render(); hasS(root.innerHTML, "allows at most 5 entries", "a contract limit is shown next to the field");
  await app.act("ed-preview"); is(S.edits.analyst.phase, "idle", "…and the preview is not requested");
  inp("duties", "ok"); inp("resources", "tool: web"); app.render(); hasS(root.innerHTML, "needs a kind of doc / dir / skill / url", "an unknown resource kind is explained");
}
{
  // conflict: the backend refuses without sending the current role; the client refetches and compares
  const { app, root, S, adapter } = boot({ hash: "#/office/fact-checker/edit", scenario: "conflict" }); await app.load();
  const inp = inpOf(app);
  inp("duties", "Re-derive figures from sources\nMy own duty"); await app.act("ed-preview"); await app.act("ed-apply");
  const es = S.edits["fact-checker"];
  is(es.phase, "error", "conflict: error phase"); hasS(root.innerHTML, "nothing was saved", "conflict says nothing was saved"); hasS(root.innerHTML, "Your draft is still here", "…and that nothing was overwritten");
  is(es.text.duties.includes("My own duty"), true, "conflict: the draft is kept"); hasS(root.innerHTML, "Added by someone else", "conflict shows what changed upstream (from the refetched snapshot)");
  hasS(root.innerHTML, "would remove them", "…and warns that the whole-list draft would remove the upstream addition");
  hasNot(root.innerHTML, 'data-edit="duties"', "the form is not shown over the conflict");
  await app.act("ed-rebase"); is(S.edits["fact-checker"].phase, "preview", "rebase re-previews on the latest configuration");
  await app.act("ed-apply"); is(S.edits["fact-checker"].phase, "done", "and the second apply goes through on the fresh baseline");
}
{
  // saved, derived cards pending: saved, not failed, and nothing offers to apply again
  const { app, root, S, adapter } = boot({ hash: "#/office/analyst/edit", scenario: "projection" }); await app.load(); const applies = spyApply(adapter);
  inpOf(app)("duties", "Only duty"); await app.act("ed-preview"); await app.act("ed-apply");
  is(S.edits.analyst.phase, "done", "projection pending ends as done, not as an error"); hasS(root.innerHTML, "authoritative configuration is written", "the config is reported saved");
  hasS(root.innerHTML, "Derived cards are pending", "…and the cards are reported pending, separately"); hasS(root.innerHTML, "Do not apply again", "…with the instruction not to re-submit");
  hasNot(root.innerHTML, "ed-retryproj", "no fake retry-projection button"); hasNot(root.innerHTML, 'data-act="ed-apply"', "no apply button in the result");
  await app.act("ed-refresh"); await app.act("ed-apply"); is(applies.length, 1, "refresh and apply-after-done never send a second apply");
}
{
  // a lost reply to apply: unknown outcome, no automatic or one-click resend
  const { app, root, S, adapter } = boot({ hash: "#/office/analyst/edit" }); await app.load();
  const real = adapter.applyRoleChange; let n = 0;
  adapter.applyRoleChange = async (...a) => { n++; await real(...a); return { ok: false, error: { code: "network", message: "reply lost", retryable: true } }; };   // the backend saved, the reply never arrived
  inpOf(app)("duties", "Only duty"); await app.act("ed-preview"); await app.act("ed-apply");
  is(S.edits.analyst.phase, "error", "lost reply: error phase"); hasS(root.innerHTML, "not known whether the apply took effect", "…says the outcome is unknown");
  hasS(root.innerHTML, "will not send it again", "…and that the client will not send it again");
  hasNot(root.innerHTML, "most likely saved", "…and does not infer success from the state read (it has no whole-config baseline)");
  hasS(root.innerHTML, 'data-act="ed-preview"', "…the way to find out is a fresh preview");
  hasNot(root.innerHTML, 'data-act="ed-retry"', "no retry button for apply"); hasNot(root.innerHTML, 'data-edit="duties"', "…and no form to resubmit from"); hasS(root.innerHTML, 'data-act="ed-refresh"', "the follow-up is to refresh and look");
  await app.act("ed-refresh"); is(n, 1, "refreshing does not resend");
}
{
  // roles that cannot be edited say why, instead of building a request from guesses
  const { app, root, S } = boot({ hash: "#/office/analyst/edit" }); await app.load();
  const r = S.snap.roles.find((x) => x.id === "analyst");
  r.escalationRole = null; app.render(); hasS(root.innerHTML, "does not say who this position escalates to", "an unknown escalation role closes editing");
  r.escalationRole = "research-lead"; r.origin = "mailbox"; app.render(); hasS(root.innerHTML, "drawn from a mailbox", "a mailbox node is not editable as a role");
}
{
  // the master mapping on screen: mailbox nodes, unassigned node, letters by mailbox with their own states
  const { app, root, S } = boot(); await app.load();
  S.snap = mapMasterState({ panel: { ok: true, label: "Demo", operator: "boss", organization: { mailbox: "boss", label: "Director", members: [] } },
    boxes: [{ name: "boss", app: "Manual", online: true, counts: { waiting: 1, reminded: 1 }, identity: { binding: null, profile: { kind: "human", display_name: "Boss" } }, activity: { state: "unknown" },
      pending: [{ file: "x.md", status: "waiting", subject: "Hello", from: "a", mtime: 1 }, { file: "y.md", status: "reminded", subject: "Again", from: "a", mtime: 2 }] },
    { name: "late", app: "Codex", online: true, counts: {}, identity: {}, activity: {}, pending: [] }] }, Date.now() / 1000);
  S.view = "office"; S.roleId = "box:boss"; S.roleTab = "overview"; app.render();
  hasS(root.innerHTML, "Mailboxes not in the tree", "a mailbox the tree forgot is drawn"); hasS(root.innerHTML, "stands for one mailbox, not a formal position", "a mailbox node says what it is");
  hasS(root.innerHTML, "Delivery switch: on", "the delivery switch is shown as a switch"); hasS(root.innerHTML, 'class="st" data-st="unknown"', "…and the holder is drawn as unknown, not connected");
  S.roleTab = "work"; app.render(); hasS(root.innerHTML, "Waiting in mailbox", "a waiting letter keeps its state"); hasS(root.innerHTML, "Reminded, not consumed", "a reminded letter keeps its state");
  hasS(root.innerHTML, "by mailbox boss", "letters are labelled as attributed by mailbox"); hasNot(root.innerHTML, ">Accepted<", "nothing is flattened to accepted");
  S.roleTab = "edit"; app.render(); hasS(root.innerHTML, "does not offer role editing", "role editing is missing on master and stays closed");
}

// ---- threads: observe is not control ----
{
  const { app, root, S } = boot({ hash: "#/threads/t-pricing" }); await app.load();
  hasS(root.innerHTML, "Observing is not controlling", "observed thread explains itself"); hasNot(root.innerHTML, 'data-act="send"', "…and offers no input");
  S.threadId = "t-scan"; app.render(); hasS(root.innerHTML, 'data-act="send"', "a controllable thread offers input");
  app.input({ closest: () => ({ dataset: { draft: "send:t-scan" }, value: "do the thing" }) }, false);
  await app.act("send", { thread: "t-scan" }); hasS(root.innerHTML, "Accepted", "sent text shows accepted"); hasNot(root.innerHTML, ">Consumed<", "…never consumed");
  is(S.drafts["send:t-scan"], undefined, "the draft is cleared once accepted");
  S.threadId = "t-import"; app.render(); hasS(root.innerHTML, "waits for your approval", "pending binding says so");
  S.threadId = "t-scan"; app.render(); hasS(root.innerHTML, "118 earlier events", "a truncated buffer is announced");
}

// ---- new thread: a request, never a started session ----
{
  const { app, root, S } = boot(); await app.load();
  await app.act("newthread", { role: "builder" }); is(S.dialog, "newthread", "dialog opens");
  app.input({ closest: () => ({ dataset: { nt: "title" }, value: "Plan sprint" }) }, false);
  await app.act("nt-submit"); hasS(root.innerHTML, "has not started", "the result says the thread has not started"); await app.load(); yes(S.snap.approvals.some((a) => a.kind === "thread_create"), "the request shows up as a pending approval");
  app.key({ key: "Escape" }); is(S.dialog, null, "Esc closes the dialog");
}

// ---- work and mail are separate facts ----
{
  const { app, root, S } = boot({ hash: "#/work" }); await app.load();
  hasS(root.innerHTML, "The letter arrived, but the work is still", "a mail row says when the work is not done");
  hasS(root.innerHTML, "only says the letter arrived", "the page says mail is not work");
  const before = S.snap.approvals.filter((a) => a.state === "pending").length;
  await app.act("approve", { ap: "ap-2" }); is(S.snap.approvals.filter((a) => a.state === "pending").length, before - 1, "approving removes one pending approval");
}

// ---- HUD: unknown is never zero ----
{
  const none = hudCard(normalizeUsage({ input: 10, output: 5 }), 0);
  hasS(none, "unknown", "missing figures say unknown"); hasNot(none, ">0<", "…never 0"); hasS(none, "none is calculated here", "an unreported total is not summed");
  hasS(hudCard(null, 0), "unknown, not zero", "no source at all says unknown, not zero");
  const { app, root, S } = boot(); await app.load();
  hasS(root.innerHTML, 'aria-expanded="false"', "the HUD starts closed"); app.hudIn(target({ x: 1 })); is(S.hud, true, "hover/focus opens it");
  app.hudOut(null, null); is(S.hud, false, "leaving closes it (unpinned)");
  app.act("hud"); is(S.hud && S.hudPin, true, "click pins it"); app.key({ key: "Escape" }); is(S.hud, false, "Esc closes it");
}
{
  // Tab out of the HUD chip: the next element gets focus only after the handler returns, so the repaint must wait for it
  // (a repaint inside focusout replaced the element about to be focused and dropped focus to <body>).
  const timers = []; const root = { innerHTML: "", querySelectorAll() { return []; }, querySelector() { return null; }, contains() { return false; } };
  const win = { location: { hash: "" }, innerWidth: 1440, setTimeout: (f) => { timers.push(f); return 1; }, clearTimeout() {} };
  const app = createApp({ root, toastRoot: { textContent: "" }, adapter: createMockAdapter({ latency: 0 }), win, doc: null, locks: createMemoryLocks().tab() }); await app.load();
  app.hudIn(target({ x: 1 })); hasS(root.innerHTML, 'aria-expanded="true"', "the HUD opened on focus");
  app.hudOut({ closest: () => null }, null); is(timers.length, 1, "leaving toward another element defers the repaint");
  hasS(root.innerHTML, 'aria-expanded="true"', "…nothing is replaced while the focus move is under way"); timers[0](); hasS(root.innerHTML, 'aria-expanded="false"', "…and the repaint follows once it has landed");
}

// ---- IME safety, escaping, wiring ----
{
  const { app, root, S } = boot(); await app.load();
  app.setComposing(true); const before = root.innerHTML; S.view = "work"; app.render(); is(root.innerHTML, before, "no repaint while composing"); app.setComposing(false); yes(root.innerHTML !== before, "…and it catches up afterwards");
  const x = boot(); await x.app.load(); x.adapter.sameData; x.S.snap.roles[1].title = '<img src=x onerror=alert(1)>'; x.app.render(); hasNot(x.root.innerHTML, "<img src=x", "user text is escaped");
  // every data-act the views can emit is a known action
  const acts = new Set(); for (const m of root.innerHTML.matchAll(/data-act="([\w-]+)"/g)) acts.add(m[1]);
  for (const hash of ["#/office/analyst", "#/office/analyst/threads", "#/office/analyst/work", "#/office/analyst/edit", "#/threads/t-scan", "#/work"]) { const b = boot({ hash }); await b.app.load(); for (const m of b.root.innerHTML.matchAll(/data-act="([\w-]+)"/g)) acts.add(m[1]); }
  const src = readFileSync(new URL("../client/src/app.js", import.meta.url), "utf8");
  for (const a of acts) yes(src.includes(`"${a}"`) || src.includes(`${a}(`), `data-act="${a}" has a handler`);
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
