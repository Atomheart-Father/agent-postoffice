// Client views and controller under Node (no browser): every state renders, every data-act is wired, both languages
// are complete, drafts survive, and a missing number is never drawn as 0.
import { createApp } from "../client/src/app.js";
import { createMockAdapter, SCENARIOS } from "../client/src/adapters/mock.js";
import { setLang, missing, has } from "../client/src/i18n.js";
import { STR } from "../client/src/strings.js";
import { PRESENCE, DELIVERY, WORK_STATES, CONTROL } from "../client/src/contract.js";
import { hudCard } from "../client/src/views/hud.js";
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
  const app = createApp({ root, toastRoot: { textContent: "" }, adapter, win, doc: null });
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

// ---- role edit end to end through the controller ----
{
  const { app, root, S, adapter } = boot({ hash: "#/office/analyst/edit" }); await app.load();
  const inp = (field, value) => app.input({ closest: () => ({ dataset: { edit: field }, value }) }, false);
  inp("summary", "Collects, tabulates and cites sources.");
  is(S.edits.analyst.phase, "idle", "typing does not leave the draft state");
  await app.act("ed-preview"); is(S.edits.analyst.phase, "preview", "preview phase"); hasS(root.innerHTML, "Changed", "the diff is shown"); hasS(root.innerHTML, "Confirm and apply", "confirm is a separate step");
  await app.act("ed-apply"); is(S.edits.analyst.phase, "done", "done"); hasS(root.innerHTML, "every projection was written", "result says what was written");
  is(S.snap.roles.find((r) => r.id === "analyst").summary, "Collects, tabulates and cites sources.", "the snapshot was reloaded from the backend");
  // denied role: no form
  S.roleId = "reviewer"; S.roleTab = "edit"; app.render(); hasS(root.innerHTML, "role.edit on Engineering", "a role the actor may not edit shows the backend's reason"); hasNot(root.innerHTML, 'data-edit="duties"', "…and offers no form");
  // conflict keeps the draft
  adapter.setScenario("conflict"); await app.load(); S.roleId = "fact-checker"; S.roleTab = "edit"; app.render();
  inp("summary", "My draft"); await app.act("ed-preview"); await app.act("ed-apply");
  is(S.edits["fact-checker"].phase, "error", "conflict: error phase"); hasS(root.innerHTML, "Your draft is still here", "conflict says nothing was overwritten"); is(S.edits["fact-checker"].text.summary, "My draft", "conflict: the draft is kept");
  hasS(root.innerHTML, "Added by someone else", "conflict shows what changed upstream");
  adapter.setScenario("default"); await app.load(); await app.act("ed-rebase"); is(S.edits["fact-checker"].phase, "preview", "rebase re-previews on the new version");
}
{
  const { app, root, S } = boot({ hash: "#/office/analyst/edit", scenario: "partial" }); await app.load();
  app.input({ closest: () => ({ dataset: { edit: "summary" }, value: "S2" }) }, false); await app.act("ed-preview"); await app.act("ed-apply");
  hasS(root.innerHTML, "some projections failed", "partial failure is not reported as success"); hasS(root.innerHTML, "contacts/ could not be written", "…with the failing projection and reason");
  await app.act("ed-retryproj"); hasS(root.innerHTML, "Applied", "retry resolves it");
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
