// Delivery switches, whole-mailbox clear, the ledger of write requests, backend words and the English default, under Node
// (no browser). The backend is a fake whose replies are the pack's (tests/fixtures/office-e819); the real master is driven
// by client_master_integration_test.mjs. Nothing here says anything about a running e819f0e.
import { readFileSync, readdirSync } from "node:fs";
import { createApp } from "../client/src/app.js";
import { createHttpAdapter, mapMasterState } from "../client/src/adapters/http.js";
import { setLang, getLang, missing } from "../client/src/i18n.js";
import { STR } from "../client/src/strings.js";
import { writesReduce, unconfirmed, newRequestId } from "../client/src/flows/writes.js";

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const is = (g, w, m) => (JSON.stringify(g) === JSON.stringify(w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));
const hasS = (h, n, m) => (String(h).includes(n) ? ok(m) : bad(m, `missing ${JSON.stringify(n)}`));
const hasNot = (h, n, m) => (String(h).includes(n) ? bad(m, `unexpected ${JSON.stringify(n)}`) : ok(m));
const F = (f) => JSON.parse(readFileSync(new URL("./fixtures/office-e819/" + f, import.meta.url), "utf8"));
const state = F("state_ok.json");
const cases = F("http_cases.json").cases; const resp = (n) => JSON.parse(cases.find((c) => c.name === n).body_resp);

// ---- the English default (the client opens in English; Chinese is a choice) ----
is(getLang(), "en", "the interface starts in English");
{
  const main = readFileSync(new URL("../client/src/main.js", import.meta.url), "utf8");
  hasNot(main, "detectLang(", "the browser's language is not consulted on first open");
  hasS(main, 'loadLang() === "zh" ? "zh" : "en"', "only a stored choice of Chinese switches it");
  const cjk = /[㐀-鿿＀-￯]/;
  const offenders = Object.entries(STR).filter(([, v]) => !v[1] || cjk.test(v[1])).map(([k]) => k);
  is(offenders, [], "no English string is empty or contains Chinese");
  const emptyZh = Object.entries(STR).filter(([, v]) => !v[0]).map(([k]) => k);
  is(emptyZh, [], "no Chinese string is empty");
  const src = (f) => readFileSync(new URL("../client/src/" + f, import.meta.url), "utf8");
  const files = readdirSync(new URL("../client/src/views/", import.meta.url)).map((f) => "views/" + f);
  const hard = files.filter((f) => cjk.test(src(f)));
  is(hard, ["views/shell.js"], "no view hard-codes Chinese, except the language switch's own label");
  hasS(src("views/shell.js"), '["en", "zh"].map', "the language switch lists English first");
  const used = new Set();
  for (const f of ["app.js", ...files, "flows/mail.js", "flows/roleEdit.js"]) for (const m of src(f).matchAll(/\bt\(\s*["'`]([A-Za-z0-9_.:-]+)["'`]/g)) if (!/[.-]$/.test(m[1])) used.add(m[1]);
  is([...used].filter((k) => !STR[k]), [], "every wording key used in the views exists");
}

// ---- a fake backend with replies that can be held back or lost ----
function backend(routes) {
  const log = []; const holds = [];
  const fetchImpl = async (url, opt = {}) => {
    const path = url.split("?")[0];
    log.push({ path, method: opt.method || "GET", headers: opt.headers, body: opt.body ? JSON.parse(opt.body) : undefined });
    const r = routes[path];
    if (!r) return { ok: false, status: 404, text: async () => '{"ok":false,"error":"not_found"}' };
    let out = typeof r === "function" ? r(opt.body ? JSON.parse(opt.body) : undefined) : r;
    if (out && typeof out.then === "function") out = await out;
    const [status, body] = out;
    if (body === "LOST") { const e = new Error("t"); e.name = "AbortError"; throw e; }
    return { ok: status < 300, status, text: async () => (typeof body === "string" ? body : JSON.stringify(body)) };
  };
  return { fetchImpl, log, holds };
}
const hold = () => { let release; const p = new Promise((r) => { release = r; }); return { p, release }; };
function boot(routes, { hash = "", width = 1440, st = state } = {}) {
  const root = { innerHTML: "", querySelectorAll() { return []; }, querySelector() { return null; }, contains() { return false; } };
  const win = { location: { hash }, innerWidth: width, setTimeout: () => 0, clearTimeout() {} };
  const b = backend({ "/api/state": [200, st], ...routes });
  const adapter = createHttpAdapter({ fetchImpl: b.fetchImpl, timeoutMs: 300, writeTimeoutMs: 300 });
  const app = createApp({ root, toastRoot: { textContent: "" }, adapter, win, doc: null });
  return { app, root, S: app.S, log: b.log, adapter };
}
const box = (dataset, extra = {}) => ({ closest: () => ({ dataset, getAttribute: () => null, ...extra }) });
const posts = (t, path) => t.log.filter((l) => l.method === "POST" && (!path || l.path === path));
const swOf = (t, name) => { const r = t.S.snap.roles.find((x) => x.box === name); return r && r.incumbent ? r.incumbent.switch : "gone"; };
const LANGS = ["en", "zh"];

// ---- adapter: what is sent for status / clear, and what is never resent ----
{
  const t = boot({ "/api/status": [200, resp("status_offline")], "/api/clear": [200, resp("clear_ok")] });
  await t.app.load();
  const r1 = await t.adapter.setDelivery({ name: "box_b", status: "offline" });
  is(posts(t, "/api/status")[0].body, { name: "box_b", status: "offline" }, "status, one mailbox: {name, status}");
  is(posts(t, "/api/status")[0].headers, { "Content-Type": "application/json" }, "…with Content-Type: application/json and no other header (no token, no request id)");
  await t.adapter.setDelivery({ names: ["box_b", "box_unlisted"], status: "offline" });
  is(posts(t, "/api/status")[1].body, { names: ["box_b", "box_unlisted"], status: "offline" }, "status, several on the candidate: {names, status}, one request");
  await t.adapter.setDelivery({ group: "@notify", status: "online" });
  is(posts(t, "/api/status")[2].body, { group: "notify", status: "online" }, "status, a group: {group, status} and the name goes without @");
  await t.adapter.clearMailbox({ name: "box_b" });
  is(posts(t, "/api/clear")[0].body, { name: "box_b" }, "clear: {name} to /api/clear");
  is([r1.ok, Object.keys(r1.data).sort()], [true, ["names", "status"]], "the reply to status says what was asked, not what is now");
  is((await t.adapter.setDelivery({ name: "box_b", status: "sideways" })).error.code, "invalid", "a status other than online/offline is not sent");
  is(posts(t).length, 4, "four writes, four requests: nothing was repeated");
}
{
  const t = boot({ "/api/status": [400, resp("status_unknown_box")], "/api/clear": [400, { ok: false, error: "" }] }); await t.app.load();
  const r = await t.adapter.setDelivery({ name: "ghost", status: "online" });
  is([r.ok, r.error.code, r.error.status], [false, "invalid", 400], "status, unknown mailbox: 400 -> invalid");
  hasS(r.error.message, "400", "…with no invented reason (the backend gives none)");
  const g = boot({ "/api/status": [400, resp("status_group_unknown")] }); await g.app.load();
  const rg = await g.adapter.setDelivery({ group: "nosuch", status: "online" });
  hasS(rg.error.message, "配置里没有分组", "status, unknown group: the backend's own reason, as written");
  const d = boot({ "/api/status": [403, { ok: false }], "/api/clear": [403, { ok: false }] }); await d.app.load();
  hasS((await d.adapter.setDelivery({ name: "box_b", status: "online" })).error.message, "Host / Origin / Content-Type protection", "status 403 {ok:false}: explained as the documented protection");
  const e = boot({ "/api/clear": [403, { ok: false, error: "scope" }] }); await e.app.load();
  const re = await e.adapter.clearMailbox({ name: "box_b" });
  hasS(re.error.message, "scope", "clear, an unknown 403: shown as written"); hasNot(re.error.message, "Host / Origin / Content-Type protection", "…and not explained as the guard");
}
{
  const t = boot({ "/api/status": [200, "LOST"], "/api/clear": [200, "LOST"] }); await t.app.load();
  const a = await t.adapter.setDelivery({ name: "box_b", status: "offline" });
  is([a.ok, a.error.outcomeUnknown, a.error.retryable], [false, true, false], "status, no reply: outcome unknown, not retryable");
  const b = await t.adapter.clearMailbox({ name: "box_b" });
  is([b.ok, b.error.outcomeUnknown], [false, true], "clear, no reply: outcome unknown");
  is(posts(t).length, 2, "…and each was sent once");
}
{
  // master has no `names`: several mailboxes go one by one, and stop at the first error
  const master = { ...state }; delete master.role_cards;
  const t = boot({ "/api/status": (b) => (b.name === "ghost" ? [400, { ok: false, error: "" }] : [200, { ok: true, error: "" }]) }, { st: master }); await t.app.load();
  const r = await t.adapter.setDelivery({ names: ["box_a", "ghost", "box_b"], status: "offline" });
  is(posts(t, "/api/status").map((p) => p.body.name), ["box_a", "ghost"], "master, several: one request per mailbox, stopping at the first error");
  is([r.error.applied, r.error.notTried, r.error.failedAt], [["box_a"], ["box_b"], "ghost"], "…the error names what was applied and what was not tried");
}

// ---- the write ledger ----
{
  const ids = new Set(); let seq = 0; for (let i = 0; i < 50; i++) ids.add(newRequestId(++seq, () => (i * 0.137) % 1));
  is(ids.size, 50, "request ids are unique"); ok("id shape " + [...ids][3]);
  let l = writesReduce([], { type: "start", id: "w-1", kind: "send", target: "x", at: 1 });
  is([l[0].phase, unconfirmed(l).length], ["pending", 1], "a started write is awaiting confirmation");
  l = writesReduce(l, { type: "fail", id: "w-1", error: { outcomeUnknown: true, message: "m" }, at: 2 });
  is([l[0].phase, unconfirmed(l).length], ["unknown", 1], "no usable reply -> possibly sent, still unconfirmed");
  l = writesReduce(l, { type: "dismiss", id: "w-1" }); is([l[0].phase, l[0].dismissed, unconfirmed(l).length], ["unknown", true, 0], "the user's dismissal stops the reminder and does not change the state");
  l = writesReduce(writesReduce([], { type: "start", id: "w-2", kind: "send", at: 1 }), { type: "ok", id: "w-2", at: 2 });
  is(writesReduce(l, { type: "dismiss", id: "w-2" })[0].phase, "acknowledged", "an acknowledged write cannot be 'dismissed' into something else");
}
for (const lang of LANGS) {
  setLang(lang);
  // send: held reply -> awaiting confirmation; lost reply -> possibly sent; nothing is resent
  const h = hold();
  const t = boot({ "/api/send": async () => { await h.p; return [200, "LOST"]; } }, { hash: "#/work" });
  await t.app.load();
  t.app.act("compose"); t.S.compose.to = "box_a"; t.S.compose.subject = "s"; t.S.compose.body = "b";
  const pending = t.app.act("compose-send");
  await new Promise((r) => setImmediate(r));
  is(t.S.writes.map((w) => w.phase), ["pending"], `[${lang}] a send in flight is awaiting confirmation in the ledger`);
  hasS(t.root.innerHTML, 'data-act="writes"', `[${lang}] …the top bar says so while the dialog is open`);
  h.release(); await pending;
  is(t.S.writes.map((w) => w.phase), ["unknown"], `[${lang}] a lost reply is 'possibly sent', not failed`);
  is(posts(t, "/api/send").length, 1, `[${lang}] the send was sent once`);
  hasS(t.root.innerHTML, "Check sent list" === "" ? "" : 'data-act="compose-check"', `[${lang}] the compose dialog points at the sent list`);
  t.S.dialog = "writes"; t.app.render();
  hasS(t.root.innerHTML, 'data-phase="unknown"', `[${lang}] the request list shows the unconfirmed entry`);
  hasS(t.root.innerHTML, lang === "en" ? "Unknown: possibly sent" : "结果未知，可能已发出", `[${lang}] …in plain words`);
  hasS(t.root.innerHTML, 'class="raw"', `[${lang}] …with its local id`);
  hasNot(t.root.innerHTML, 'data-act="compose-send"', `[${lang}] …and no 'send again' there`);
  await t.app.act("writes-check");
  is(posts(t, "/api/send").length, 1, `[${lang}] checking the state resends nothing`);
  is(t.log.filter((l) => l.path === "/api/state").length >= 2, true, `[${lang}] …it re-reads the state`);
  t.app.act("writes-dismiss", { write: t.S.writes[0].id });
  is(unconfirmed(t.S.writes).length, 0, `[${lang}] dismissing closes it`);
  is(Object.keys(posts(t, "/api/send")[0].body).sort(), ["body", "client_request_id", "from", "need", "subject", "to"], `[${lang}] the request carries the contract's keys plus the one-off client_request_id (the local display id is not sent)`);
}
setLang("en");

// ---- delivery: buttons follow the server ----
{
  // the backend says yes but the state still says on: the result is shown as a difference, and the button still offers 'off'
  const t = boot({ "/api/status": [200, { ok: true, error: "" }] }, { hash: "#/office/box:box_a" });
  await t.app.load(); t.S.view = "office"; t.S.roleId = "box:box_a"; t.S.roleTab = "overview"; t.app.render();
  let h = t.root.innerHTML;
  hasS(h, "Delivery switch (server state)", "a mailbox node has a delivery section"); hasS(h, "On (online)", "…showing the server's state");
  hasS(h, 'data-act="dl-one"', "…with a button"); hasS(h, 'data-status="offline"', "…that offers the opposite of what the server says"); hasNot(h, 'data-status="online"', "…and not the state it is already in");
  await t.app.act("dl-one", { box: "box_a", status: "offline" });
  h = t.root.innerHTML;
  is(posts(t, "/api/status")[0].body, { name: "box_a", status: "offline" }, "the click sends one status request");
  hasS(h, "The backend accepted the request", "an accepted request is reported as accepted");
  hasS(h, "differs from what was asked", "…and the re-read state that still says on is shown as a difference");
  hasS(h, 'data-status="offline"', "…the button still follows the server (on -> offers off)");
  is(t.log.filter((l) => l.path === "/api/state").length, 2, "the state was read again after the write");
  is(posts(t).length, 1, "…and nothing was resent");
}
{
  // a backend whose state really changes
  let on = true;
  const st = () => { const s = JSON.parse(JSON.stringify(state)); s.boxes.find((b) => b.name === "box_a").online = on; return [200, s]; };
  const t = boot({ "/api/state": st, "/api/status": () => { on = false; return [200, { ok: true, error: "" }]; } });
  await t.app.load(); t.S.view = "office"; t.S.roleId = "box:box_a"; t.app.render();
  await t.app.act("dl-one", { box: "box_a", status: "offline" });
  const h = t.root.innerHTML;
  hasS(h, "Server now: Off (offline) ✓", "when the re-read state matches, it says so"); hasS(h, 'data-status="online"', "…and the button now offers the other direction");
  is(swOf(t, "box_a"), "off", "the snapshot holds the server's value");
  // switch is not a connection
  hasNot(h, 'class="pw">Connected<', "turning the switch does not produce 'connected'");
}
{
  // the dialog: several, a group, busy state
  const h = hold();
  const t = boot({ "/api/status": async () => { await h.p; return [200, { ok: true, error: "" }]; } });
  await t.app.load(); t.app.act("delivery");
  let html = t.root.innerHTML;
  hasS(html, "Delivery switches (online / offline)", "the dialog opens"); hasS(html, "@notify", "…it lists the backend's groups"); hasS(html, "box_c", "…and the mailboxes");
  hasS(html, "disabled", "…with the selection buttons disabled while nothing is selected");
  t.app.click(box({ act: "dl-pick", box: "box_a" }, { checked: true })); t.app.click(box({ act: "dl-pick", box: "box_b" }, { checked: true }));
  is(t.S.delivery.sel, ["box_a", "box_b"], "ticking selects");
  const p = t.app.act("dl-sel", { status: "offline" }); await new Promise((r) => setImmediate(r));
  is(t.S.delivery.phase, "busy", "a request in flight is busy"); hasS(t.root.innerHTML, "Sending to the backend", "…and says so");
  is(posts(t, "/api/status").length, 1, "…once"); t.app.act("dl-sel", { status: "offline" }); await new Promise((r) => setImmediate(r));
  is(posts(t, "/api/status").length, 1, "a second click while busy sends nothing");
  h.release(); await p;
  is(posts(t, "/api/status")[0].body, { names: ["box_a", "box_b"], status: "offline" }, "selected -> {names, status}");
  const g = t.app.act("dl-group", { group: "notify", status: "online" }); await g;
  is(posts(t, "/api/status")[1].body, { group: "notify", status: "online" }, "a group button -> {group, status}");
  t.app.act("dl-group-pick", { group: "notify" }); is(t.S.delivery.sel, ["panelop", "box_a", "box_b", "box_unlisted"], "'select members' ticks the group's members");
  hasNot(t.root.innerHTML, 'data-act="clear-ask"', "the switch dialog has no clear button: clearing is another entry");
}
{
  // a lost reply to a status request
  const t = boot({ "/api/status": [200, "LOST"] }); await t.app.load(); t.app.act("delivery");
  t.app.click(box({ act: "dl-pick", box: "box_a" }, { checked: true })); await t.app.act("dl-sel", { status: "offline" });
  hasS(t.root.innerHTML, "may have been sent and applied, or not", "no reply -> possibly sent");
  is([t.S.writes[0].kind, t.S.writes[0].phase], ["status", "unknown"], "…and it sits in the ledger as unconfirmed");
  is(posts(t, "/api/status").length, 1, "…sent once, and not again");
}

// ---- clear the whole mailbox ----
for (const lang of LANGS) {
  setLang(lang);
  let cleared = false;
  const st = () => { const s = JSON.parse(JSON.stringify(state)); if (cleared) { const b = s.boxes.find((x) => x.name === "box_a"); b.pending = []; b.counts = { waiting: 0, reminded: 0, failed: 0, ride: 0 }; } return [200, s]; };
  const t = boot({ "/api/state": st, "/api/clear": () => { cleared = true; return [200, resp("clear_ok")]; } });
  await t.app.load(); t.S.view = "office"; t.S.roleId = "box:box_a"; t.S.roleTab = "overview"; t.app.render();
  let h = t.root.innerHTML;
  hasS(h, 'data-act="clear-ask"', `[${lang}] a mailbox has its own clear entry`);
  hasS(h, lang === "en" ? "Whole mailbox" : "整箱", `[${lang}] …labelled as the whole mailbox`);
  hasS(h, lang === "en" ? "2 letters are in this mailbox" : "2 封信", `[${lang}] …with how many letters the snapshot lists`);
  hasNot(h.slice(h.indexOf('data-act="clear-ask"') - 40, h.indexOf('data-act="clear-ask"') + 200), "File as processed", `[${lang}] …and the clear entry does not borrow the archive wording`);
  t.app.act("clear-ask", { box: "box_a" }); h = t.root.innerHTML;
  hasS(h, lang === "en" ? "Clear the whole mailbox: box_a" : "清理整个信箱：box_a", `[${lang}] the clear dialog names the mailbox`);
  hasS(h, lang === "en" ? "ALL 2 letters" : "全部 2 封信", `[${lang}] …and says all N letters`);
  hasNot(h, 'data-act="letter-do"', `[${lang}] …it reuses nothing from the letter dialog`); hasNot(h, 'data-kind="archive"', `[${lang}] …nor the archive action`);
  hasS(h, 'data-act="clear-next"', `[${lang}] step one only offers to continue`); hasNot(h, 'data-act="clear-do"', `[${lang}] …there is no clear button on step one`);
  await t.app.act("clear-do"); is(posts(t, "/api/clear").length, 0, `[${lang}] clear-do on step one sends nothing`);
  t.app.act("clear-next"); h = t.root.innerHTML;
  hasS(h, 'data-act="clear-do"', `[${lang}] step two has the clear button`); hasS(h, 'data-act="clear-do" data-fk="clear-do" disabled', `[${lang}] …disabled until the box is ticked`);
  await t.app.act("clear-do"); is(posts(t, "/api/clear").length, 0, `[${lang}] …and clicking it anyway sends nothing`);
  t.app.click(box({ act: "clear-ack" }, { checked: true })); h = t.root.innerHTML;
  hasNot(h, 'data-act="clear-do" data-fk="clear-do" disabled', `[${lang}] ticking enables it`);
  hasS(h, lang === "en" ? "Clear all 2 letters from box_a" : "清理 box_a 的全部 2 封信", `[${lang}] …and the button says how many`);
  await t.app.act("clear-do");
  is(posts(t, "/api/clear").map((p) => p.body), [{ name: "box_a" }], `[${lang}] the clear sent {name} once`);
  h = t.root.innerHTML;
  hasS(h, lang === "en" ? "The backend accepted the clear" : "后端接受了整箱清理", `[${lang}] the result is reported`);
  hasS(h, lang === "en" ? "now lists 0 letters" : "现在有 0 封", `[${lang}] …with the re-read count (the reply has none)`);
  is(t.S.writes.map((w) => [w.kind, w.phase]), [["clear", "acknowledged"]], `[${lang}] …and the ledger has it as accepted`);
  t.app.act("close-dialog"); is(t.S.clear, null, `[${lang}] closing the dialog drops the flow`);
}
setLang("en");
{
  // Escape / cancel never sends; a lost reply is possibly done; a changed count is called out
  const t = boot({ "/api/clear": [200, "LOST"] }); await t.app.load(); t.S.view = "office"; t.S.roleId = "box:box_a"; t.app.render();
  t.app.act("clear-ask", { box: "box_a" }); t.app.act("clear-next"); t.app.act("close-dialog"); is(posts(t, "/api/clear").length, 0, "cancelling sends nothing");
  t.app.act("clear-ask", { box: "box_a" }); t.app.act("clear-next"); t.app.click(box({ act: "clear-ack" }, { checked: true }));
  t.S.snap.mail = t.S.snap.mail.filter((m) => m.box !== "box_a" || m.sourceStatus !== "waiting"); t.app.render();
  hasS(t.root.innerHTML, "It was 2 letters when you opened this window", "a changed count is called out before the final click");
  await t.app.act("clear-do");
  hasS(t.root.innerHTML, "may or may not have run", "no reply -> the clear may or may not have run");
  hasNot(t.root.innerHTML, 'data-act="clear-do"', "…and nothing offers to run it again");
  is(posts(t, "/api/clear").length, 1, "…it was sent once");
}
{
  // a mailbox the snapshot does not list has an unknown count, never 0
  const t = boot({}); await t.app.load(); t.app.act("clear-ask", { box: "box_c_missing" });
  is(t.S.clear && t.S.clear.count, null, "an unlisted mailbox has an unknown count");
  const u = boot({}); await u.app.load(); u.S.view = "office"; u.S.roleId = "box:box_unlisted"; u.app.render();
  hasS(u.root.innerHTML, 'data-act="clear-ask"', "the pane of an empty mailbox still has the entry");
  hasS(u.root.innerHTML, "0 letters are in this mailbox", "…and a listed empty inbox says 0");
}

// ---- backend words: raw word + one gloss; Chinese protocol words are not the only text ----
for (const lang of LANGS) {
  setLang(lang);
  const t = boot({}); await t.app.load(); t.S.view = "office"; t.S.roleId = "box:box_a"; t.S.roleTab = "work"; t.app.render();
  const h = t.root.innerHTML;
  hasS(h, '<code class="raw"', `[${lang}] a letter's state shows the backend's own word`); hasS(h, ">waiting</code>", `[${lang}] …waiting`); hasS(h, ">reminded</code>", `[${lang}] …reminded`);
  hasS(h, ">formal</code>", `[${lang}] a letter's kind is shown as the backend wrote it`);
  hasS(h, lang === "en" ? "Waiting in mailbox" : "在信箱，待提醒", `[${lang}] …beside a plain gloss`);
  t.S.roleTab = "overview"; t.app.render();
  hasS(t.root.innerHTML, lang === "en" ? "Manual" : "手动", `[${lang}] the harness label`);
  if (lang === "en") hasNot(t.root.innerHTML, "手动", "…is not left as Chinese in the English interface");
}
setLang("en");
{
  const s = JSON.parse(JSON.stringify(state)); s.boxes[1].pending[0].status = "ride"; s.boxes[1].pending[0].kind = "receipt";
  const m = mapMasterState(s).mail.find((x) => x.box === "box_a" && x.sourceStatus === "ride");
  is([m.delivery, m.sourceStatus, m.kind], ["unknown", "ride", "receipt"], "an undocumented status (ride) stays unknown with its raw word; kind is carried as written");
  const t = boot({}, { st: s }); await t.app.load(); t.S.view = "office"; t.S.roleId = "box:box_a"; t.S.roleTab = "work"; t.app.render();
  hasS(t.root.innerHTML, ">ride</code>", "…and the screen shows 'ride'"); hasS(t.root.innerHTML, ">receipt</code>", "…and the kind 'receipt'");
  hasNot(t.root.innerHTML, "Accepted", "…and neither is folded into 'accepted'");
}
{
  const t = boot({}); await t.app.load(); t.app.act("compose"); const h = t.root.innerHTML;
  hasS(h, 'value="回复" label="Reply requested (回复)"', "the need list keeps the backend's word and glosses it");
  hasNot(h, "Recommended: 回复", "no English sentence depends on reading Chinese");
}
{
  // role-apply result: the backend's own words next to the gloss
  const diffBody = F("role_diff_ok_response.json").body;
  const t = boot({ "/api/role-diff": [200, diffBody], "/api/role-apply": [200, resp("role_apply_pending")] }, { hash: "#/office/office.design/edit" }); await t.app.load();
  t.app.input({ closest: () => ({ dataset: { edit: "duties" }, value: "x" }) }, false); await t.app.act("ed-preview"); await t.app.act("ed-apply");
  hasS(t.root.innerHTML, ">saved: true</code>", "role-apply: 'saved: true' is shown as the backend wrote it"); hasS(t.root.innerHTML, ">projection: pending</code>", "…and 'projection: pending'");
  is(t.S.writes.map((w) => [w.kind, w.phase]), [["role-apply", "acknowledged"]], "…and the apply is in the ledger");
  is(posts(t, "/api/role-apply").length, 1, "…sent once");
}
is([...missing].join(","), "", "no wording key is missing in either language");

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
