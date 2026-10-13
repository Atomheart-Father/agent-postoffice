// Send de-duplication key (client side), the request log's states, its bounded retention and what survives a reload, under
// Node (no browser). The backend is a fake. NOTHING here says the real backend de-duplicates: that needs the updated pinned
// contract, its capability flag and a run on the real hosting chain, none of which exist yet. A fake counting calls proves how
// the CLIENT behaves, not what the server does with a repeated key.
import { readFileSync } from "node:fs";
import { createApp } from "../client/src/app.js";
import { createHttpAdapter } from "../client/src/adapters/http.js";
import { setLang } from "../client/src/i18n.js";
import { STR } from "../client/src/strings.js";
import {
  writesReduce, unconfirmed, evictable, ledgerFull, LIMIT, newClientRequestId, isUuid, sendDedupe, toStored, fromStored, STORED_FIELDS,
} from "../client/src/flows/writes.js";
import { createLedgerStore, KEY_PREFIX } from "../client/src/ledgerStore.js";

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const is = (g, w, m) => (JSON.stringify(g) === JSON.stringify(w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));
const yes = (c, m, x) => (c ? ok(m) : bad(m, x));
const hasS = (h, n, m) => (String(h).includes(n) ? ok(m) : bad(m, `missing ${JSON.stringify(n)}`));
const hasNot = (h, n, m) => (String(h).includes(n) ? bad(m, `unexpected ${JSON.stringify(n)}`) : ok(m));
const F = (f) => JSON.parse(readFileSync(new URL("./fixtures/office-e819/" + f, import.meta.url), "utf8"));
const state = F("state_ok.json");
const tick = () => new Promise((r) => setImmediate(r));
setLang("en");

// ---------- harness ----------
function backend(routes) {
  const log = [];
  const fetchImpl = async (url, opt = {}) => {
    const path = url.split("?")[0];
    log.push({ path, method: opt.method || "GET", raw: opt.body, body: opt.body ? JSON.parse(opt.body) : undefined });
    const r = routes[path];
    if (!r) return { ok: false, status: 404, text: async () => '{"ok":false,"error":"not_found"}' };
    let out = typeof r === "function" ? r(opt.body ? JSON.parse(opt.body) : undefined) : r;
    if (out && typeof out.then === "function") out = await out;
    const [status, body] = out;
    if (body === "LOST") { const e = new Error("t"); e.name = "AbortError"; throw e; }
    return { ok: status < 300, status, text: async () => (typeof body === "string" ? body : JSON.stringify(body)) };
  };
  return { fetchImpl, log };
}
const withOperator = (op) => { const s = JSON.parse(JSON.stringify(state)); s.panel.operator = op; return s; };
const memStorage = (init = {}) => { const m = new Map(Object.entries(init)); return { m, getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); }, removeItem: (k) => { m.delete(k); } }; };
function boot(routes, { st = state, storage = memStorage(), base = "http://office-a", hash = "#/work", newKey } = {}) {
  const root = { innerHTML: "", querySelectorAll() { return []; }, querySelector() { return null; }, contains() { return false; } };
  const win = { location: { hash }, innerWidth: 1440, setTimeout: () => 0, clearTimeout() {} };
  const b = backend({ "/api/state": [200, st], ...routes });
  const adapter = createHttpAdapter({ fetchImpl: b.fetchImpl, timeoutMs: 300, writeTimeoutMs: 300 });
  const app = createApp({ root, toastRoot: { textContent: "" }, adapter, win, doc: null, storage, ledgerBase: base, ...(newKey ? { newKey } : {}) });
  return { app, root, S: app.S, log: b.log, adapter, storage };
}
const posts = (t, path) => t.log.filter((l) => l.method === "POST" && (!path || l.path === path));
const OKSEND = (extra = {}) => [200, { ok: true, state: "sent", id: "20260101-000000-a", ref: "box_a/20260101-000000-a.md", to: "box_a", ...extra }];
async function sendOne(t, { to = "box_a", subject = "s", body = "b" } = {}) {
  t.app.act("compose"); t.S.compose.to = to; t.S.compose.subject = subject; t.S.compose.body = body;
  await t.app.act("compose-send");
}
const stored = (t) => [...t.storage.m.entries()].filter(([k]) => k.startsWith(KEY_PREFIX));

// ---------- 1 + 2: one operation = one key; every explicit new send = a new key ----------
{
  const t = boot({ "/api/send": OKSEND() });
  await t.app.load();
  t.app.act("compose"); t.S.compose.to = "box_a"; t.S.compose.subject = "same"; t.S.compose.body = "same";
  const a = t.app.act("compose-send"), b = t.app.act("compose-send");   // a double click: two calls in the same tick
  await a; await b;
  is(posts(t, "/api/send").length, 1, "a double click is one send operation, one request");
  const k1 = posts(t, "/api/send")[0].body.client_request_id;
  yes(isUuid(k1), "the request carries a UUID client_request_id", k1);
  yes(t.S.writes[0].clientRequestId === k1, "…the log record holds that same key");
  is(Object.keys(posts(t, "/api/send")[0].body).sort(), ["body", "client_request_id", "from", "need", "subject", "to"], "…and the existing send fields, nothing else");
  yes(k1 !== t.S.writes[0].id && !String(k1).startsWith("w-"), "the server key is not the local display id");
  // the same content again, explicitly: a second, independent operation
  t.app.act("close-dialog");
  t.app.act("compose"); t.S.compose.to = "box_a"; t.S.compose.subject = "same"; t.S.compose.body = "same";
  await t.app.act("compose-send");
  is(posts(t, "/api/send").length, 2, "sending identical content again is a second request (nothing is merged by content or time)");
  const k2 = posts(t, "/api/send")[1].body.client_request_id;
  yes(isUuid(k2) && k2 !== k1, "…with a different key", `${k1} ${k2}`);
  is(t.S.writes.filter((w) => w.kind === "send").length, 2, "…and two records in the log");
  // the adapter passes a given key through untouched and never makes one up
  const a1 = createHttpAdapter({ fetchImpl: backend({ "/api/send": OKSEND() }).fetchImpl });
  const bk = backend({ "/api/send": OKSEND() });
  const ad = createHttpAdapter({ fetchImpl: bk.fetchImpl });
  const req = { from: "p", to: "box_a", subject: "s", need: "仅告知", body: "b", client_request_id: "3f2b8c1e-4d5a-4e6f-8a7b-9c0d1e2f3a4b" };
  await ad.sendMail(req); await ad.sendMail(req);
  is(bk.log[0].raw === bk.log[1].raw, true, "calling the adapter twice with one operation's request sends byte-identical bodies (same key, same fields)");
  await ad.sendMail({ from: "p", to: "box_a", subject: "s", need: "x", body: "b" });
  is("client_request_id" in bk.log[2].body, false, "the adapter never invents a key");
  void a1;
  // an edited draft after a send is a new operation; the earlier record stays
  const t2 = boot({ "/api/send": [200, "LOST"] });
  await t2.app.load();
  await sendOne(t2, { subject: "first", body: "x" });
  const first = t2.S.writes[0];
  is([first.phase, first.dismissed], ["unknown", false], "a lost reply is unknown");
  t2.S.compose.body = "x edited";
  await t2.app.act("compose-send");
  is(posts(t2, "/api/send").length, 2, "sending after an edit is a new request");
  const ks = posts(t2, "/api/send").map((p) => p.body.client_request_id);
  yes(ks[0] !== ks[1] && isUuid(ks[1]), "…with a new key");
  yes(t2.S.writes.some((w) => w.id === first.id && w.phase === "unknown" && w.clientRequestId === ks[0]), "…and the earlier unknown record is still there, unchanged");
}

// ---------- 3: a lost reply is unknown and is never sent again ----------
{
  const t = boot({ "/api/send": [200, "LOST"] });
  await t.app.load();
  await sendOne(t);
  is(t.S.writes.map((w) => w.phase), ["unknown"], "request sent, reply lost: unknown");
  await tick(); await tick(); await t.app.act("writes-check"); await t.app.act("mail-refresh");
  is(posts(t, "/api/send").length, 1, "…nothing is resent, not by time, not by checking or refreshing");
  hasS(t.root.innerHTML, "may or may not have taken effect", "…and the dialog says it may have been sent");
}

// ---------- 4: no safe retry unless a pinned contract says so ----------
{
  const st = JSON.parse(JSON.stringify(state)); st.features = { send_request_id: true };
  const t = boot({ "/api/send": [200, "LOST"] }, { st });
  await t.app.load();
  await sendOne(t);
  is(sendDedupe(), { supported: false, reason: "contract-pending" }, "send de-duplication is not supported until a contract is pinned");
  t.S.dialog = "compose"; t.app.render(); const h1 = t.root.innerHTML;
  yes(!/data-act="[^"]*retry/.test(h1), "an old backend, even one claiming the flag, gets no same-key retry in the compose dialog");
  hasNot(h1, "safe retry", "…and no 'safe retry' wording");
  hasS(h1, "Send as a new letter", "the only way to send again says it is a new letter");
  hasS(h1, "never resends by itself", "…and that nothing is resent by itself");
  t.S.dialog = "writes"; t.app.render(); const h2 = t.root.innerHTML;
  yes(!/data-act="[^"]*retry/.test(h2), "the request list has no retry button either");
  hasS(h2, "No safe retry is offered", "…and says why");
  hasNot(h2, "already have", "nothing claims duplicate protection");
}

// ---------- 5: a key conflict (409) is shown, not worked around ----------
{
  const t = boot({ "/api/send": [409, { ok: false, error: "conflict", message: "client_request_id was used with different content" }] });
  await t.app.load();
  await sendOne(t, { body: "orig" });
  const key = posts(t, "/api/send")[0].body.client_request_id;
  await tick(); await t.app.act("writes-check");
  is(posts(t, "/api/send").length, 1, "a conflict reply does not make the client send again, with the old key or a new one");
  is(t.S.writes.map((w) => [w.phase, w.clientRequestId]), [["unknown", key]], "…the original record is kept with its key (a 4xx on a send does not prove 'not sent' until the contract says what it means)");
  hasS(t.root.innerHTML, "client_request_id was used with different content", "…and the backend's own words are shown");
  is(t.S.compose.body, "orig", "…the draft is kept for checking");
}

// ---------- error classification: only a proof says 'rejected' ----------
{
  const cases = [
    ["a 502 from a proxy (HTML) may come after the backend saved", [502, "<html>bad gateway</html>"], "unknown"],
    ["a 500 JSON error with no saved:false", [500, { ok: false, error: "failed" }], "unknown"],
    ["a 200 that says ok:false", [200, { ok: false, error: "x" }], "unknown"],
    ["a 4xx that is not JSON", [400, "<html>no</html>"], "unknown"],
    ["a 400 bad_request from the backend", [400, { ok: false, error: "bad_request" }], "rejected"],
    ["a 404 not_found from the backend", [404, { ok: false, error: "not_found" }], "rejected"],
    ["a 403 {ok:false} (documented guard)", [403, { ok: false }], "rejected"],
    ["any other 403", [403, "Forbidden by proxy"], "unknown"],
  ];
  for (const [name, reply, want] of cases) {
    const t = boot({ "/api/send": reply });
    await t.app.load(); await sendOne(t);
    is(t.S.writes.map((w) => w.phase), [want], `send: ${name} -> ${want}`);
  }
  // role-apply says saved:false itself: that is a proof even on a 500
  const ad = createHttpAdapter({ fetchImpl: backend({ "/api/role-apply": [500, { ok: false, saved: false, error: "backup failed" }] }).fetchImpl });
  const r = await ad.applyRoleChange({ name: "demo.review", candidates: ["a"] }, {});
  yes(r.ok === false && !r.error.outcomeUnknown && r.error.saved === false, "role-apply 500 with saved:false is a proof of 'not applied'", JSON.stringify(r));
  const ad2 = createHttpAdapter({ fetchImpl: backend({ "/api/role-apply": [502, "<html>"] }).fetchImpl });
  const r2 = await ad2.applyRoleChange({ name: "demo.review", candidates: ["a"] }, {});
  yes(r2.ok === false && r2.error.outcomeUnknown === true, "role-apply 502 is unknown, not 'not applied'");
}

// ---------- deduped: the original letter is shown, nothing new is invented ----------
{
  const t = boot({ "/api/send": OKSEND({ deduped: true, id: "ORIG-ID", ref: "box_a/ORIG-ID.md" }) });
  await t.app.load(); await sendOne(t);
  t.S.dialog = "compose"; t.app.render();
  hasS(t.root.innerHTML, "did not create another letter", "a deduped reply says no new letter was created");
  hasS(t.root.innerHTML, "box_a/ORIG-ID.md", "…and names the original");
  is([t.S.writes.length, t.S.writes[0].phase, t.S.writes[0].ref, t.S.writes[0].deduped], [1, "acknowledged", "box_a/ORIG-ID.md", true], "…one record, pointing at the original");
}

// ---------- 6: bounded retention ----------
{
  const rec = (i, phase, extra = {}) => ({ id: `w-${i}`, kind: "send", target: "", at: i, phase, message: "", closedAt: null, clientRequestId: null, ref: "", dismissed: false, checkedAt: null, restored: false, deduped: false, ...extra });
  // 28 unresolved + 2 resolved: a new record pushes out a resolved one, never an unresolved one
  let l = [...Array.from({ length: 28 }, (_, i) => rec(100 + i, "unknown")), rec(1, "acknowledged"), rec(2, "rejected")].sort((a, b) => b.at - a.at);
  is(l.length, LIMIT, "30 records");
  l = writesReduce(l, { type: "start", id: "w-new", kind: "send", at: 999 });
  is([l.length, l.filter((w) => w.phase === "unknown").length, l.some((w) => w.id === "w-1"), l.some((w) => w.id === "w-2")], [LIMIT, 28, false, true], "a full log drops the oldest resolved record, keeps every unknown");
  l = writesReduce(l, { type: "start", id: "w-new2", kind: "send", at: 1000 });
  is([l.length, l.some((w) => w.id === "w-2"), l.filter((w) => w.phase === "unknown").length], [LIMIT, false, 28], "…then the other resolved one");
  // now 28 unknown + 2 pending: nothing may go
  is(ledgerFull(l), true, "30 unresolved records: the log is full");
  const same = writesReduce(l, { type: "start", id: "w-x", kind: "send", at: 1001 });
  is(same.length === LIMIT && !same.some((w) => w.id === "w-x") && l.filter((w) => w.phase === "unknown" || w.phase === "pending").length === 30, true, "…and the reducer refuses to drop anything to make room");
  // dismissing makes a record evictable, and it goes first
  const d = writesReduce(l.map((w) => (w.id === "w-new" ? { ...w, phase: "unknown" } : w)), { type: "dismiss", id: "w-new" });
  is([ledgerFull(d), evictable(d.find((w) => w.id === "w-new")), d.find((w) => w.id === "w-new").phase], [false, true, "unknown"], "a dismissed unknown record can make room, and is still 'unknown'");
}
{
  // the app refuses new writes when the log is full of unresolved records; reads, refresh and checking keep working
  const t = boot({ "/api/send": OKSEND(), "/api/status": [200, { ok: true, error: "" }], "/api/clear": [200, { ok: true, error: "" }] });
  await t.app.load();
  t.S.writes = Array.from({ length: LIMIT }, (_, i) => ({ id: `w-${i}`, kind: "send", target: "", at: i, phase: i % 2 ? "unknown" : "pending", message: "", closedAt: null, clientRequestId: null, ref: "", dismissed: false, checkedAt: null, restored: false, deduped: false }));
  t.app.render();
  hasS(t.root.innerHTML, "Requests (full)", "the top bar says the log is full");
  await sendOne(t);
  is(posts(t).length, 0, "no write was sent while the log is full of unresolved records");
  hasS(t.root.innerHTML, "Writes are paused", "…the dialog says why");
  is(t.S.writes.length, LIMIT, "…and the log is untouched");
  await t.app.act("dl-one", { box: "box_a", status: "offline" }); await t.app.act("clear-do");
  is(posts(t).length, 0, "status / clear are paused as well");
  const reads = t.log.length; await t.app.load(); yes(t.log.length > reads, "reading and refreshing still work");
  await t.app.act("writes-check"); yes(t.log.length > reads + 1, "…and so does checking");
  t.S.dialog = "writes"; t.app.render();
  hasS(t.root.innerHTML, "new writes are paused", "the request list explains the pause");
  hasS(t.root.innerHTML, "Dismissing does not mean the operation succeeded", "…and what dismissing means");
  // dismiss one unknown record -> writes resume
  t.app.act("writes-dismiss", { write: "w-1" });
  is(t.S.writes.find((w) => w.id === "w-1").phase, "unknown", "dismissing keeps the state");
  t.app.act("close-dialog"); await sendOne(t);
  is(posts(t, "/api/send").length, 1, "after one record is dismissed, writes work again");
  is(t.S.writes.some((w) => w.id === "w-1"), false, "…the dismissed record made room");
  is(t.S.writes.filter((w) => w.phase === "unknown" && !w.dismissed).length, 14, "…and no undismissed unknown record was dropped");
}

// ---------- 7: dismiss is not success ----------
{
  const t = boot({ "/api/send": [200, "LOST"] });
  await t.app.load(); await sendOne(t);
  t.app.act("writes-dismiss", { write: t.S.writes[0].id });
  is([t.S.writes[0].phase, t.S.writes[0].dismissed, unconfirmed(t.S.writes).length], ["unknown", true, 0], "dismissed: still unknown, flagged, no longer counted as needing a look");
  const [, raw] = stored(t)[0]; const it = JSON.parse(raw).items[0];
  is([it.result_state, it.dismissed], ["unknown", true], "…and that is what is stored");
  t.S.dialog = "writes"; t.app.render();
  hasS(t.root.innerHTML, "Dismissed", "the list marks it dismissed");
  hasS(t.root.innerHTML, "Unknown: possibly sent", "…and still says unknown");
  hasNot(t.root.innerHTML, 'data-act="writes-dismiss"', "…without offering to dismiss again");
}

// ---------- 8: a reload restores the summary, sends nothing, turns pending into unknown ----------
{
  const storage = memStorage();
  let release; const gate = new Promise((r) => { release = r; });
  const a = boot({ "/api/send": async () => { await gate; return OKSEND(); }, "/api/status": [200, "LOST"] }, { storage });
  await a.app.load();
  a.app.act("compose"); a.S.compose.to = "box_a"; a.S.compose.subject = "SUBJ-SECRET"; a.S.compose.body = "BODY-SECRET";
  const held = a.app.act("compose-send"); await tick();
  await a.app.act("dl-one", { box: "box_a", status: "offline" });   // reply lost -> unknown
  is(a.S.writes.map((w) => w.phase).sort(), ["pending", "unknown"], "before the reload: one pending (held), one unknown");
  // the page is reloaded: a new app on the same storage and backend address
  const b = boot({ "/api/send": OKSEND(), "/api/status": OKSEND() }, { storage });
  await b.app.load();
  is(posts(b).length, 0, "after the reload nothing is sent");
  is(b.S.writes.map((w) => [w.kind, w.phase, w.restored]).sort(), [["send", "unknown", true], ["status", "unknown", true]], "…both records are back, and the pending one is unknown");
  yes(b.S.writes.find((w) => w.kind === "send").clientRequestId === posts(a, "/api/send")[0].body.client_request_id, "…the send keeps its client_request_id");
  b.S.dialog = "writes"; b.app.render();
  hasS(b.root.innerHTML, "comes from before a reload", "…and says the request was not kept, so it can only be checked");
  yes(!/data-act="[^"]*retry/.test(b.root.innerHTML), "no retry is offered for a restored record");
  await b.app.act("writes-check");
  is(posts(b).length, 0, "checking after the reload still sends nothing");
  yes(b.S.writes.every((w) => typeof w.checkedAt === "number"), "…and records when it was checked");
  const [, raw] = stored(b)[0]; const items = JSON.parse(raw).items;
  yes(items.every((i) => typeof i.last_checked_at === "number"), "…which is stored");
  release(); await held;
}

// ---------- 9: what is stored, and who can see it ----------
{
  const storage = memStorage();
  const t = boot({ "/api/send": OKSEND() }, { storage });
  await t.app.load();
  await sendOne(t, { to: "box_a", subject: "SUBJ-SECRET", body: "BODY-SECRET" });
  t.S.writes.push({ ...t.S.writes[0], id: "w-draft" });   // (a record never holds more than its summary)
  const all = stored(t).map(([, v]) => v).join("\n");
  hasNot(all, "SUBJ-SECRET", "the stored text has no subject"); hasNot(all, "BODY-SECRET", "…no body");
  hasNot(all, '"target"', "…no target field");
  hasNot(all.toLowerCase(), "token", "…no credential-looking field"); hasNot(all, "password", "…no password");
  const p = JSON.parse(stored(t)[0][1]);
  is(Object.keys(p.items[0]).sort(), [...STORED_FIELDS].sort(), "each item has exactly the summary fields");
  is(p.v, 1, "…with a schema version");
  is(p.items[0].result_ref, "box_a/20260101-000000-a.md", "…the reference the backend returned is kept");
  is(p.scope, { base: "http://office-a", operator: "panelop" }, "…and the scope is written inside the payload");
  // another Office, another operator
  const other = boot({}, { storage, base: "http://office-b" }); await other.app.load();
  is(other.S.writes.length, 0, "another backend address does not see these records");
  const op2 = boot({}, { storage, st: withOperator("someone_else") }); await op2.app.load();
  is(op2.S.writes.length, 0, "another operator does not see them either");
  const again = boot({}, { storage }); await again.app.load();
  is(again.S.writes.length, 1, "the same backend and operator do");
  // the operator changes while the page is open: the list follows the operator, and nothing is mixed
  let current = state;
  const sw = boot({ "/api/state": () => [200, current] }, { storage }); await sw.app.load();
  is(sw.S.writes.length, 1, "operator A: A's record");
  current = withOperator("someone_else"); await sw.app.load();
  is(sw.S.writes.length, 0, "operator switched: A's records are not shown to B");
  await sendOne(sw, { subject: "from B" });
  yes(stored(sw).length === 2, "…B's records go to B's own entry", String(stored(sw).length));
  current = state; await sw.app.load();
  is(sw.S.writes.map((w) => w.kind), ["send"], "switching back shows A's own record again");
  yes(!sw.S.writes.some((w) => w.at === sw.S.writes[0].at && w.id === "w-draft"), "…without B's");
}

// ---------- 10: broken storage ----------
{
  const mk = (over) => ({ ...memStorage(), ...over });
  const variants = {
    "storage missing": null,
    "getItem throws": mk({ getItem() { throw new Error("blocked"); } }),
    "setItem throws (quota / private mode)": mk({ setItem() { throw new Error("quota"); } }),
    "the whole object throws": mk({ getItem() { throw new Error("x"); }, setItem() { throw new Error("y"); } }),
  };
  for (const [name, storage] of Object.entries(variants)) {
    const t = boot({ "/api/send": [200, "LOST"] }, { storage });
    await t.app.load();
    await sendOne(t);
    is(t.S.writes.map((w) => w.phase), ["unknown"], `${name}: the send and its record work`);
    t.S.dialog = "writes"; t.app.render();
    hasS(t.root.innerHTML, "cannot be recovered after a reload", `${name}: the page says unresolved records live on this page only`);
  }
  const key = (t) => createLedgerStore({ storage: t.storage, base: "http://office-a" }).keyOf({ base: "http://office-a", operator: "panelop" });
  const probe = boot({}); const k = key(probe);
  const sc = { base: "http://office-a", operator: "panelop" };
  const good = { local_id: "w-1", client_request_id: null, operation: "send", started_at: 5, result_state: "acknowledged", dismissed: false, result_ref: "", last_checked_at: null };
  const bads = {
    "not JSON": "{{{{",
    "an array": "[]",
    "an old schema version": JSON.stringify({ v: 0, scope: sc, items: [good] }),
    "a newer schema version": JSON.stringify({ v: 2, scope: sc, items: [good] }),
    "another scope's payload under this key": JSON.stringify({ v: 1, scope: { base: "http://office-b", operator: "panelop" }, items: [good] }),
    "items that are not records": JSON.stringify({ v: 1, scope: sc, items: [1, null, "x", { local_id: 5 }, { local_id: "w-2" }] }),
  };
  for (const [name, raw] of Object.entries(bads)) {
    const t = boot({}, { storage: memStorage({ [k]: raw }) });
    let threw = false; try { await t.app.load(); t.S.dialog = "writes"; t.app.render(); } catch { threw = true; }
    is(threw, false, `${name}: the page loads`);
    is(t.S.writes.length, 0, `${name}: nothing from it is used`);
    if (name !== "items that are not records") hasS(t.root.innerHTML, "could not be read", `${name}: the list says so`);
    // …and the next write is still stored normally
  }
  const hurt = boot({ "/api/send": OKSEND() }, { storage: memStorage({ [k]: "{{{{" }) });
  await hurt.app.load(); await sendOne(hurt);
  const fresh = createLedgerStore({ storage: hurt.storage, base: "http://office-a" }).load("panelop");
  is([fresh.state, fresh.items.length], ["ok", 1], "after damaged data, a new write replaces it with a readable record");
  // an unrecognised stored state is never read as success
  const odd = fromStored({ ...good, result_state: "totally-ok" });
  is(odd.phase, "unknown", "a stored state nobody recognises is unknown, not a success");
  is(fromStored({ ...good, result_state: "pending" }).phase, "unknown", "a stored pending is unknown after a reload");
  is(fromStored({ ...good, client_request_id: "not-a-uuid" }).clientRequestId, null, "a stored key that is not a UUID is dropped");
  is(Object.keys(toStored(fromStored(good))).sort(), [...STORED_FIELDS].sort(), "a record round-trips to exactly the summary fields");
}

// ---------- the key itself ----------
{
  yes(isUuid(newClientRequestId()), "newClientRequestId makes a UUID with the platform's crypto");
  const noUuid = { getRandomValues: (a) => { for (let i = 0; i < a.length; i++) a[i] = (i * 37 + 11) & 255; return a; } };
  yes(isUuid(newClientRequestId(noUuid)), "…or from getRandomValues when randomUUID is missing (an insecure page)");
  is(newClientRequestId({}), null, "…and with no CSPRNG, no key at all (Math.random is never used for it)");
  is(newClientRequestId(null), null, "…also when crypto is absent");
  const t = boot({ "/api/send": OKSEND() }, { newKey: () => null });
  await t.app.load(); await sendOne(t);
  is("client_request_id" in posts(t, "/api/send")[0].body, false, "without a key the request goes out without the field");
  is(t.S.writes[0].clientRequestId, null, "…and the record says there was none");
}

// ---------- words ----------
{
  const used = ["wr.phase.pending", "wr.phase.acknowledged", "wr.phase.rejected", "wr.phase.unknown", "wr.dismissed", "wr.full", "wr.fullMsg", "wr.storeOff", "wr.storeBad", "wr.scope", "wr.key", "wr.ref", "wr.deduped", "wr.noRetrySend", "wr.noRetryRestored", "wr.openFull", "cp.newLetterWarn", "ml.done.send.deduped"];
  is(used.filter((k) => !STR[k] || !STR[k][0] || !STR[k][1]), [], "every new wording exists in both languages");
  is(Object.keys(STR).filter((k) => /^wr\.phase\.(accepted|refused|checked)$/.test(k)), [], "the old phase names are gone from the wording table");
  yes(!/[㐀-鿿]/.test(STR["wr.phase.acknowledged"][1] + STR["wr.full"][1] + STR["wr.scope"][1]), "the English text has no Chinese");
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
