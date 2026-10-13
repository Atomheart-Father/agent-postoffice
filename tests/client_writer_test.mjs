// One writing tab per (backend address, operator): which page may write, how a waiting page takes over, what the stored summary
// looks like after an upgrade, and what a page that cannot get the guarantee does. Node, no browser: the lock manager is the
// in-memory stand-in in flows/writer.js (same semantics the app relies on: exclusive, ifAvailable, abortable queue, a page that
// can vanish without a word). The real Web Locks behaviour is checked in Chromium by the browser run, not here.
import { readFileSync } from "node:fs";
import { createApp } from "../client/src/app.js";
import { createHttpAdapter } from "../client/src/adapters/http.js";
import { setLang } from "../client/src/i18n.js";
import { LIMIT, SEND_KEY, writesReduce, ledgerFull } from "../client/src/flows/writes.js";
import { createLedgerStore, KEY_PREFIX, normalizeBase } from "../client/src/ledgerStore.js";
import { createMemoryLocks, createWriterGate } from "../client/src/flows/writer.js";

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const is = (g, w, m) => (JSON.stringify(g) === JSON.stringify(w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));
const yes = (c, m, x) => (c ? ok(m) : bad(m, x));
const hasS = (h, n, m) => (String(h).includes(n) ? ok(m) : bad(m, `missing ${JSON.stringify(n)}`));
const hasNot = (h, n, m) => (String(h).includes(n) ? bad(m, `unexpected ${JSON.stringify(n)}`) : ok(m));
const F = (f) => JSON.parse(readFileSync(new URL("./fixtures/office-e819/" + f, import.meta.url), "utf8"));
const state = F("state_ok.json");
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); };
setLang("en");

function backend(routes) {
  const log = [];
  const fetchImpl = async (url, opt = {}) => {
    const path = url.split("?")[0];
    log.push({ path, method: opt.method || "GET", body: opt.body ? JSON.parse(opt.body) : undefined });
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
// each tab sees the same storage but counts its own writes
const viewOf = (s) => { const v = { writes: 0, getItem: (k) => s.getItem(k), setItem: (k, x) => { v.writes++; s.setItem(k, x); }, removeItem: (k) => { v.writes++; s.removeItem(k); } }; return v; };
const OKSEND = () => [200, { ok: true, state: "sent", id: "20260101-000000-a", ref: "box_a/20260101-000000-a.md", to: "box_a" }];

// the scope locks of the current release (each page also holds the name earlier releases used; those are not counted here)
const stableHolders = (w) => w.mgr.holders().filter((n) => n.startsWith("boxz.office.writer|"));
const world = ({ base = "http://office-a" } = {}) => ({ mgr: createMemoryLocks(), storage: memStorage(), base });
async function tab(w, routes = {}, { st = state, base = w.base, lockless } = {}) {
  const root = { innerHTML: "", querySelectorAll() { return []; }, querySelector() { return null; }, contains() { return false; } };
  const win = { location: { hash: "#/work" }, innerWidth: 1440, setTimeout: () => 0, clearTimeout() {} };
  const b = backend({ "/api/state": [200, st], ...routes });
  const adapter = createHttpAdapter({ fetchImpl: b.fetchImpl, timeoutMs: 300, writeTimeoutMs: 300 });
  const lk = lockless ? null : w.mgr.tab();
  const view = viewOf(w.storage);
  const app = createApp({ root, toastRoot: { textContent: "" }, adapter, win, doc: null, storage: view, ledgerBase: base, locks: lk });
  await app.load(); await settle();
  return { app, root, S: app.S, log: b.log, view, lk, writer: () => app.S.ledger.writer, posts: (p) => b.log.filter((l) => l.method === "POST" && (!p || l.path === p)) };
}
async function sendOne(t, o = {}) {
  t.app.act("compose"); t.S.compose.to = o.to || "box_a"; t.S.compose.subject = o.subject || "s"; t.S.compose.body = o.body || "b";
  await t.app.act("compose-send");
}
const stored = (w, key) => (w.storage.m.has(key) ? JSON.parse(w.storage.m.get(key)) : null);
const keyFor = (w, operator = "panelop", base = w.base) => createLedgerStore({ storage: w.storage, base }).keyOf({ base, operator });
const uuid = (n) => `3f2b8c1e-4d5a-4e6f-8a7b-${String(n).padStart(12, "0")}`;
const rec = (n, phase, extra = {}) => ({ local_id: `w-${n}`, client_request_id: null, operation: "send", started_at: n, result_state: phase, dismissed: false, last_checked_at: null, ...extra });

// ---------- two tabs, same scope ----------
{
  const w = world();
  const A = await tab(w, { "/api/send": [200, "LOST"], "/api/status": [200, { ok: true, error: "" }], "/api/clear": [200, { ok: true, error: "" }] });
  const B = await tab(w, { "/api/send": OKSEND(), "/api/status": [200, { ok: true, error: "" }], "/api/clear": [200, { ok: true, error: "" }] });
  is([A.writer(), B.writer()], ["holder", "reader"], "the first page of a scope writes, the second is read-only");
  hasS(B.root.innerHTML, 'data-writer="reader"', "the read-only page says so on the page itself");
  hasS(B.root.innerHTML, "This tab is read-only", "…in plain words");
  await sendOne(B);
  await B.app.act("dl-one", { box: "box_a", status: "offline" });
  is(B.posts().length, 0, "the read-only page sends no write");
  B.S.dialog = "compose"; B.app.render();
  hasS(B.root.innerHTML, "This tab is read-only", "…and the refused send says why");
  is(B.view.writes, 0, "…and writes nothing to storage");
  const reads = B.log.length; await B.app.load(); await B.app.act("writes-check");
  yes(B.log.length > reads, "…but it still reads, refreshes and checks");
  is(B.view.writes, 0, "…checking does not touch storage either");
  // the holder writes; the reader sees it when storage changes
  await sendOne(A);
  is(A.S.writes.map((x) => x.phase), ["unknown"], "the holder's lost reply is unknown");
  is(B.S.writes.length, 0, "the read-only page has not seen it yet");
  B.app.act("ledger-sync");
  is(B.S.writes.map((x) => x.phase), ["unknown"], "…it sees it after the storage event");
  B.S.dialog = "writes"; B.app.render();
  hasNot(B.root.innerHTML, 'data-act="writes-dismiss"', "…and has no dismiss button");
  B.app.act("writes-dismiss", { write: B.S.writes[0].id });
  is([B.view.writes, stored(w, keyFor(w)).items[0].dismissed], [0, false], "a dismiss from the read-only page changes nothing");
}

// ---------- different scopes do not block each other ----------
{
  const w = world();
  const A = await tab(w);
  const other = await tab(w, {}, { st: withOperator("someone_else") });
  const elsewhere = await tab(w, {}, { base: "http://office-b" });
  is([A.writer(), other.writer(), elsewhere.writer()], ["holder", "holder", "holder"], "another operator and another backend address each have their own writer");
  is(stableHolders(w).length, 3, "…three separate locks");
  const again = await tab(w);
  is(again.writer(), "reader", "…while the same scope again is read-only");
}

// ---------- the holder closes: the waiting page takes over ----------
{
  const w = world();
  let release; const gate = new Promise((r) => { release = r; });
  const A = await tab(w, { "/api/send": async () => { await gate; return OKSEND(); }, "/api/status": [200, "LOST"] });
  const B = await tab(w, { "/api/send": OKSEND() });
  A.app.act("compose"); A.S.compose.to = "box_a"; A.S.compose.subject = "s"; A.S.compose.body = "b";
  void A.app.act("compose-send"); await settle();
  await A.app.act("dl-one", { box: "box_a", status: "offline" });   // lost reply -> unknown
  is(A.S.writes.map((x) => x.phase).sort(), ["pending", "unknown"], "the holder has one pending (in flight) and one unknown request");
  const before = stored(w, keyFor(w)).items.map((i) => i.result_state).sort();
  is(before, ["pending", "unknown"], "…both are in the stored summary");
  is(B.writer(), "reader", "the second page is read-only meanwhile");
  A.app.destroy(); await settle();   // the page is closed
  is(B.writer(), "holder", "when the holder closes, the waiting page becomes the writer by itself");
  is(B.posts().length, 0, "…taking over sends nothing");
  is(B.S.writes.map((x) => [x.phase, x.restored]).sort(), [["unknown", true], ["unknown", true]], "…it read the summary again, and the pending request is unknown now");
  await sendOne(B);
  is(B.posts("/api/send").length, 1, "…and it can write");
  release();
}

// ---------- the holder crashes ----------
{
  const w = world();
  const A = await tab(w, { "/api/send": [200, "LOST"] });
  const B = await tab(w, { "/api/send": OKSEND() });
  const C = await tab(w, { "/api/send": OKSEND() });
  await sendOne(A);
  A.lk.crash(); await settle();   // no call into the page: the browser just drops its locks
  is([B.writer(), C.writer()], ["holder", "reader"], "after a crash the first waiting page takes over and the next one keeps waiting");
  is(B.S.writes.map((x) => x.phase), ["unknown"], "…it shows what the crashed page had stored, as unknown");
  is(B.posts().length, 0, "…and sent nothing");
  B.lk.crash(); await settle();
  is(C.writer(), "holder", "…and the next one takes over when that one goes too");
}

// ---------- the operator changes in a page ----------
{
  const w = world();
  let current = state;
  const A = await tab(w, { "/api/state": () => [200, current], "/api/send": [200, "LOST"] });
  const X = await tab(w, { "/api/state": [200, state] });
  await sendOne(A);
  is([A.writer(), X.writer()], ["holder", "reader"], "operator panelop: A writes, X waits");
  current = withOperator("someone_else");
  await A.app.load(); await settle();
  is(A.S.writes.length, 0, "A's page now shows the new operator's own (empty) records");
  is(A.writer(), "holder", "…and A is the writer for the new operator");
  is(X.writer(), "holder", "A released the old scope: X took over it");
  is(X.S.writes.map((x) => x.phase), ["unknown"], "…and X read A's old records as unknown");
  await sendOne(A, { subject: "from the new operator" });
  is([...w.storage.m.keys()].filter((k) => k.startsWith(KEY_PREFIX)).length, 2, "each operator has its own stored entry");
  yes(!JSON.stringify(stored(w, keyFor(w))).includes("from the new operator"), "…and the first one is untouched");
  // switching into a scope that someone else already writes: read-only there, and no longer holding the old one
  const D = await tab(w, {}, { st: withOperator("third_one") });
  current = withOperator("third_one");
  await A.app.load(); await settle();
  is([A.writer(), D.writer()], ["reader", "holder"], "switching into a scope another page writes: read-only");
  is(stableHolders(w).length, 2, "…and the page holds no lock for the scope it left");
}

// ---------- capacity: unresolved records are never dropped ----------
const idsIn = (html) => [...new Set(String(html).match(/w-\d+/g) || [])];
{
  const w = world();
  const key = keyFor(w);
  w.storage.setItem(key, JSON.stringify({ v: 2, scope: { base: w.base, operator: "panelop" }, items: Array.from({ length: LIMIT }, (_, i) => rec(100 + i, i % 2 ? "unknown" : "pending")) }));
  const A = await tab(w, { "/api/send": OKSEND() });
  is([A.writer(), A.S.writes.length, A.S.writes.every((x) => x.phase === "unknown")], ["holder", LIMIT, true], "30 unresolved records are loaded, all unknown");
  await sendOne(A);
  is(A.posts().length, 0, "…a full log pauses new writes after a takeover as well");
  hasS(A.root.innerHTML, "Writes are paused", "…with the reason");
  is(stored(w, key).items.length, LIMIT, "…and the stored list stays at the limit");
  is(writesReduce(A.S.writes, { type: "start", id: "w-x", kind: "send", at: 999 }), A.S.writes, "the reducer does not make room by dropping an unresolved record either");
}
{
  // 31 records, none finished, none dismissed: nothing is dropped on load, on reload, or by any write; writes stop; every record can be paged to
  const w = world();
  const key = keyFor(w);
  const all = Array.from({ length: 31 }, (_, i) => rec(100 + i, i % 3 ? "unknown" : "pending"));
  w.storage.setItem(key, JSON.stringify({ v: 2, scope: { base: w.base, operator: "panelop" }, items: all }));
  const A = await tab(w, { "/api/send": OKSEND(), "/api/status": [200, { ok: true, error: "" }] });
  is([A.S.writes.length, A.S.writes.every((x) => x.phase === "unknown")], [31, true], "31 unresolved records: all 31 load (no cap on load), pending shown as unknown");
  yes(ledgerFull(A.S.writes), "…the log counts as full");
  hasNot(A.root.innerHTML, "were not loaded", "…and nothing claims records were left out");
  await sendOne(A);
  is(A.posts().length, 0, "a new send is not made");
  await A.app.act("dl-one", { box: "box_a", status: "offline" });
  is(A.posts().length, 0, "…nor any other business write (the guard is one gate for all of them)");
  is(stored(w, key).items.length, 31, "…storage still holds all 31");
  A.app.act("writes-check", {}); await settle();
  is(A.S.writes.length, 31, "checking state still works and keeps all 31");
  // paging: every record is reachable
  A.S.dialog = "writes"; A.app.render();
  hasS(A.root.innerHTML, "1–10 of 31", "the dialog shows the first page and the total");
  const seen = new Set(idsIn(A.root.innerHTML));
  is(seen.size, 10, "…ten rows on a page");
  for (let i = 0; i < 4; i++) { A.app.act("writes-page", { dir: "next" }); idsIn(A.root.innerHTML).forEach((x) => seen.add(x)); }
  is(seen.size, 31, "…and paging reaches all 31");
  hasS(A.root.innerHTML, "31–31 of 31", "…the last page is bounded");
  A.app.act("writes-page", { dir: "prev" }); hasS(A.root.innerHTML, "21–30 of 31", "…and goes back");
  // a reader pages too
  const R = await tab(w); R.S.dialog = "writes"; R.app.render();
  is(R.writer(), "reader", "(a second page)");
  R.app.act("writes-page", { dir: "next" }); hasS(R.root.innerHTML, "11–20 of 31", "a read-only page can page as well");
  // dismissing: 31 -> 30 open is still full, 29 resumes
  const open = () => A.S.writes.filter((x) => !x.dismissed).length;
  A.app.act("writes-dismiss", { write: "w-130" });
  is([open(), ledgerFull(A.S.writes)], [30, true], "dismissing one of 31 leaves 30 open: still paused");
  await sendOne(A); is(A.posts().length, 0, "…still no send");
  A.app.act("writes-dismiss", { write: "w-129" });
  is([open(), ledgerFull(A.S.writes)], [29, false], "dismissing a second one: 29 open, writes may resume");
  await sendOne(A);
  is(A.posts().length, 1, "…and the next send goes out");
  const kept = A.S.writes.filter((x) => x.id !== A.S.writes[0].id);
  is([A.S.writes.length, kept.filter((x) => !x.dismissed).length], [30, 29], "…making room removed only the two dismissed records; the 29 open ones are all still there");
  is(stored(w, key).items.length, 30, "…and storage matches");
}
{
  // mixed: open records are never evicted; finished and dismissed ones go, oldest first, only when a write needs room
  const w = world();
  const key = keyFor(w);
  const items = [
    ...Array.from({ length: 20 }, (_, i) => rec(1 + i, "unknown")),
    ...Array.from({ length: 20 }, (_, i) => rec(21 + i, i % 2 ? "rejected" : "acknowledged")),
    ...Array.from({ length: 3 }, (_, i) => rec(41 + i, "unknown", { dismissed: true })),
  ];
  w.storage.setItem(key, JSON.stringify({ v: 2, scope: { base: w.base, operator: "panelop" }, items }));
  const A = await tab(w, { "/api/send": OKSEND() });
  is(A.S.writes.length, 43, "43 mixed records all load");
  is(stored(w, key).items.length, 43, "…loading alone rewrites nothing");
  await sendOne(A);
  is(A.posts().length, 1, "a write is allowed: only 20 are open");
  const ids = A.S.writes.map((x) => x.id);
  is(A.S.writes.length, LIMIT, "…and the log is back at the limit");
  is(Array.from({ length: 20 }, (_, i) => `w-${1 + i}`).every((x) => ids.includes(x)), true, "…every one of the 20 open records is still there");
  is(Array.from({ length: 14 }, (_, i) => `w-${21 + i}`).some((x) => ids.includes(x)), false, "…the 14 that went are the oldest finished ones");
  is(["w-35", "w-40", "w-41", "w-42", "w-43"].every((x) => ids.includes(x)), true, "…the newer finished and the dismissed ones are still there");
  is(stored(w, key).items.length, LIMIT, "…storage matches");
  // 29 open + 1 finished: the finished one makes room, an open one never
  const w2 = world();
  w2.storage.setItem(keyFor(w2), JSON.stringify({ v: 2, scope: { base: w2.base, operator: "panelop" }, items: [...Array.from({ length: 29 }, (_, i) => rec(1 + i, "unknown")), rec(50, "acknowledged")] }));
  const B = await tab(w2, { "/api/send": [200, "LOST"] });
  await sendOne(B);
  is([B.posts().length, B.S.writes.length, B.S.writes.filter((x) => x.phase === "unknown").length], [1, LIMIT, 30], "at 29 open + 1 finished the finished one gives way; the 30th open record is the new one");
  await sendOne(B);
  is(B.posts().length, 1, "…and now 30 are open: the next send is refused");
}

// ---------- an old summary is upgraded ----------
{
  const w = world();
  const sc = { base: w.base, operator: "panelop" };
  const legacy = { v: 1, scope: sc, saved_at: 1, items: [
    rec(1, "pending", { client_request_id: uuid(1), result_ref: "bob/20261013-154020_owner_Heldsend" }),
    rec(2, "unknown", { client_request_id: uuid(2), dismissed: true }),
    rec(3, "acknowledged", { client_request_id: uuid(3), result_ref: "bob/20261013-154021_owner_Another" }),
    rec(4, "unknown"),
  ] };
  const lk = createLedgerStore({ storage: w.storage, base: w.base }).legacyKeyOf(sc);
  w.storage.setItem(lk, JSON.stringify(legacy));
  // a holder already exists for the scope: the new page is only a reader and must not rewrite anything
  const holder = w.mgr.tab(); holder.request(createLedgerStore({ storage: w.storage, base: w.base }).lockNameOf(sc), async () => new Promise(() => {}));
  const R = await tab(w);
  is(R.writer(), "reader", "a page that is not the writer");
  is(R.S.writes.map((x) => [x.id, x.phase, x.ref]).sort(), [["w-1", "unknown", ""], ["w-2", "unknown", ""], ["w-3", "acknowledged", ""], ["w-4", "unknown", ""]], "…shows the old records (pending as unknown) and no reference");
  is(R.view.writes, 0, "…and writes nothing: the old entry is still there");
  yes(w.storage.m.has(lk), "(the old key is untouched until a writer exists)");
  holder.crash(); await settle();
  is(R.writer(), "holder", "the lock is free: this page becomes the writer");
  yes(!w.storage.m.has(lk), "the old key is gone");
  const all = [...w.storage.m.entries()].map(([k, v]) => k + v).join("\n");
  hasNot(all, "result_ref", "no result_ref anywhere in storage"); hasNot(all, "Heldsend", "…nor any letter reference text"); hasNot(all, "Another", "…of either record");
  const now = stored(w, keyFor(w));
  is([now.v, now.items.length], [2, 4], "the records are carried over into the new schema");
  is(now.items.map((i) => [i.local_id, i.result_state, i.dismissed]).sort(), [["w-1", "unknown", false], ["w-2", "unknown", true], ["w-3", "acknowledged", false], ["w-4", "unknown", false]], "…unresolved ones kept (pending is unknown), the dismissed flag kept, nothing turned into a success");
  is(now.items.find((i) => i.local_id === "w-1").client_request_id, uuid(1), "…and the request keys are kept");
  // damaged old data does not stop a new one
  const w3 = world(); w3.storage.setItem(createLedgerStore({ storage: w3.storage, base: w3.base }).legacyKeyOf({ base: w3.base, operator: "panelop" }), "{{{{");
  const Z = await tab(w3);
  is([Z.writer(), Z.S.writes.length], ["holder", 0], "an unreadable old entry is ignored");
}

// ---------- upgrade v1 -> v2 never loses a record and never deletes on failure ----------
const store0 = (w) => createLedgerStore({ storage: w.storage, base: w.base });
const scope0 = (w) => store0(w).scopeOf("panelop");
const v1key = (w) => store0(w).legacyKeyOf(scope0(w));
const putV1 = (w, items) => w.storage.setItem(v1key(w), JSON.stringify({ v: 1, scope: scope0(w), saved_at: 1, items }));
{
  // 31 unresolved records in an old store: all 31 arrive in v2, v1 goes only after v2 was written and read back
  const w = world();
  putV1(w, Array.from({ length: 31 }, (_, i) => rec(100 + i, i % 2 ? "unknown" : "pending", { result_ref: `bob/secret-${i}` })));
  const A = await tab(w, { "/api/send": OKSEND() });
  const now = stored(w, keyFor(w));
  is([A.writer(), A.S.writes.length, now.items.length], ["holder", 31, 31], "31 unresolved v1 records: all 31 are carried into v2, none cut");
  yes(!w.storage.m.has(v1key(w)), "…the v1 key is removed after that");
  hasNot([...w.storage.m.values()].join("\n"), "secret-", "…and no result_ref is left anywhere");
  await sendOne(A); is(A.posts().length, 0, "…and the 31 open records pause new writes");
}
{
  // both keys exist: v1-only records join v2's, a record both have is kept once (v2's version)
  const w = world();
  w.storage.setItem(keyFor(w), JSON.stringify({ v: 2, scope: scope0(w), items: [rec(5, "acknowledged"), rec(6, "unknown")] }));
  putV1(w, [rec(5, "unknown"), rec(7, "unknown"), rec(8, "pending")]);
  const A = await tab(w);
  const now = stored(w, keyFor(w));
  is(now.items.map((i) => [i.local_id, i.result_state]).sort(), [["w-5", "acknowledged"], ["w-6", "unknown"], ["w-7", "unknown"], ["w-8", "unknown"]], "v1-only records are added to an existing v2 list; a shared record is kept once, as v2 has it");
  yes(!w.storage.m.has(v1key(w)), "…then v1 is removed");
  void A;
}
{
  // the new entry cannot be written: nothing is deleted anywhere, the records stay on the page
  const w = world();
  const removed = [];
  const base = memStorage();
  w.storage = { ...base, m: base.m, getItem: base.getItem, removeItem: (k) => { removed.push(k); base.removeItem(k); }, setItem: (k, v) => { if (k.startsWith("boxz.office.requests.v2")) throw new Error("QuotaExceededError"); base.setItem(k, v); } };
  putV1(w, Array.from({ length: 12 }, (_, i) => rec(1 + i, "unknown")));
  const before = w.storage.m.get(v1key(w));
  const A = await tab(w);
  is([A.writer(), A.S.writes.length], ["holder", 12], "v2 cannot be written: the page still shows all 12 records");
  is([w.storage.m.get(v1key(w)) === before, removed.length], [true, 0], "…the v1 entry is byte for byte what it was and nothing was removed");
  is(A.S.ledger.store, "off", "…the store says it is off");
  A.S.dialog = "writes"; A.app.render();
  hasS(A.root.innerHTML, "still in the old storage place", "…and the dialog says the old records are still in the old place");
}
{
  // a storage that accepts the write and then does not hold it: read-back catches it, v1 stays
  const w = world();
  const base = memStorage();
  const removed = [];
  w.storage = { m: base.m, getItem: base.getItem, removeItem: (k) => { removed.push(k); base.removeItem(k); }, setItem: (k, v) => { if (!k.startsWith("boxz.office.requests.v2")) base.setItem(k, v); } };
  putV1(w, [rec(1, "unknown"), rec(2, "pending")]);
  const A = await tab(w);
  is([A.S.writes.length, w.storage.m.has(v1key(w)), removed.length, A.S.ledger.store], [2, true, 0, "off"], "a write that did not stick: v1 is kept, nothing removed, store 'off'");
}
{
  // an old entry that cannot be read (damaged, other schema) is never touched
  const w = world();
  w.storage.setItem(v1key(w), "{{{{");
  const A = await tab(w);
  is([A.writer(), A.S.writes.length, w.storage.m.get(v1key(w))], ["holder", 0, "{{{{"], "an unreadable old entry is ignored and left in place");
  const w2 = world();
  w2.storage.setItem(v1key(w2), JSON.stringify({ v: 3, scope: scope0(w2), items: [rec(1, "unknown")] }));
  await tab(w2);
  yes(w2.storage.m.has(v1key(w2)), "an entry of another schema is left in place as well");
  // a record the reader cannot make sense of is not a record; the readable ones are carried
  const w3 = world(); putV1(w3, [rec(1, "unknown"), { nonsense: true }, null]);
  const C = await tab(w3);
  is([C.S.writes.length, w3.storage.m.has(v1key(w3))], [1, false], "entries without an id and time are not records: the readable one is carried over");
}

// ---------- the lock name: one per backend address + operator, whatever the version ----------
{
  const w = world();
  const st = store0(w);
  const name = st.lockNameOf(st.scopeOf("panelop"));
  yes(!/\.v\d/.test(name) && !name.includes("requests"), "the lock name carries no version number: " + name);
  is(createLedgerStore({ storage: w.storage, base: "HTTP://Office-A:80/" }).lockNameOf({ base: normalizeBase("HTTP://Office-A:80/"), operator: "panelop" }), name, "…and the same address spelled differently gives the same name");
  is(normalizeBase("https://x.test:443/a/?q=1#h"), "https://x.test/a", "address normalisation: default port, trailing slash, query and fragment go");
  yes(st.lockNameOf(st.scopeOf("other")) !== name && (() => { const b = createLedgerStore({ storage: w.storage, base: "http://office-b" }); return b.lockNameOf(b.scopeOf("panelop")); })() !== name, "…and another operator or another address gives another name");
  is(st.legacyLockNamesOf(st.scopeOf("panelop")), ["boxz.office.writer.v2|" + encodeURIComponent(st.scopeOf("panelop").base) + "|panelop"], "the name the previous release used is known, for the transition");
}

// ---------- old and new releases compete for the same scope ----------
{
  // a page of the previous release holds ITS lock name: this release's page is a reader although its own name is free
  const w = world();
  const st = store0(w);
  const oldName = st.legacyLockNamesOf(st.scopeOf("panelop"))[0];
  const oldPage = (tabLocks) => { const seen = []; const g = createWriterGate({ locks: tabLocks, onChange: (x) => seen.push(x) }); return { g, seen, last: () => seen[seen.length - 1] }; };
  const O = oldPage(w.mgr.tab()); O.g.claim(oldName); await settle();
  is(O.last(), "holder", "(the old page is the writer)");
  const A = await tab(w, { "/api/send": OKSEND() });
  const B = await tab(w, { "/api/send": OKSEND() });
  is([A.writer(), B.writer()], ["reader", "reader"], "the old page holds the old name: both pages of this release are read-only");
  await sendOne(A); is(A.posts().length, 0, "…and send nothing");
  O.g.release(); await settle();
  is([A.writer(), B.writer()].sort(), ["holder", "reader"], "the old page leaves: exactly one of this release takes over");
  const hold = A.writer() === "holder" ? A : B, other = hold === A ? B : A;
  is(hold.posts().length + other.posts().length, 0, "…and the takeover sent nothing");
  // now the old page tries again while this release writes
  const O2 = oldPage(w.mgr.tab()); O2.g.claim(oldName); await settle();
  is(O2.last(), "reader", "an old page that starts now is held out: it is a reader, so this release's lock really restricts it");
  await sendOne(hold); is(hold.posts("/api/send").length, 1, "(the writer of this release can send)");
  hold.lk.crash(); await settle();
  // the old page was queued on its own name and may be granted it first; either way exactly one of the two writes
  is([other.writer(), O2.last()].filter((x) => x === "holder").length, 1, "the writer crashes: of the waiting page of this release and the waiting old page, exactly one becomes the writer");
  is(other.posts().length, 0, "…and the takeover sent nothing");
}
{
  // a future release (other storage version) asks for the same stable name: it competes with this one, not beside it
  const w = world();
  const st = store0(w);
  const stable = st.lockNameOf(st.scopeOf("panelop"));
  const A = await tab(w);
  const seen = []; const fut = createWriterGate({ locks: w.mgr.tab(), onChange: (x) => seen.push(x) }); fut.claim([stable]); await settle();
  is([A.writer(), seen[seen.length - 1]], ["holder", "reader"], "a release with only the stable name is held out by this one");
  A.lk.crash(); await settle();
  is(seen[seen.length - 1], "holder", "…and takes over when this one is gone");
  const w2 = world(); const f2 = []; const fg = createWriterGate({ locks: w2.mgr.tab(), onChange: (x) => f2.push(x) });
  fg.claim([w2.base ? createLedgerStore({ storage: w2.storage, base: w2.base }).lockNameOf(scope0(w2)) : ""]); await settle();
  const C = await tab(w2);
  is([f2[f2.length - 1], C.writer()], ["holder", "reader"], "and the other way round: if that release writes first, this one reads");
}
{
  // a page of this release that holds the stable name but cannot yet have the old one does not count as the writer
  const w = world();
  const st = store0(w);
  const oldName = st.legacyLockNamesOf(st.scopeOf("panelop"))[0];
  const oldHolder = w.mgr.tab(); oldHolder.request(oldName, async () => new Promise(() => {}));
  const A = await tab(w, { "/api/send": OKSEND() });
  is(A.writer(), "reader", "holding only the new name is not enough to write");
  yes(stableHolders(w).length === 1, "…it does hold the new name while it waits for the old one (so a second page of this release queues behind it)");
  await sendOne(A); is(A.posts().length, 0, "…and sends nothing");
  oldHolder.crash(); await settle();
  is(A.writer(), "holder", "the old holder crashes: this page becomes the writer");
  is(A.S.writes.length, 0, "(it re-read storage on taking over)");
}

// ---------- dismiss never changes the meaning ----------
{
  const w = world();
  const A = await tab(w, { "/api/send": [200, "LOST"] });
  const B = await tab(w);
  await sendOne(A);
  A.app.act("writes-dismiss", { write: A.S.writes[0].id });
  is([A.S.writes[0].phase, A.S.writes[0].dismissed], ["unknown", true], "dismissed: still unknown");
  A.lk.crash(); await settle();
  is(B.S.writes.map((x) => [x.phase, x.dismissed]), [["unknown", true]], "…and after another page took over: still unknown, still dismissed");
}

// ---------- no way to get the guarantee ----------
{
  const w = world();
  const L = await tab(w, { "/api/send": OKSEND() }, { lockless: true });
  is(L.writer(), "unsupported", "no Web Locks: writing is off");
  hasS(L.root.innerHTML, "cannot guarantee a single writing tab", "…the page says why");
  await sendOne(L);
  is(L.posts().length, 0, "…nothing is sent");
  is(L.view.writes, 0, "…and nothing is stored");
  const reads = L.log.length; await L.app.load(); yes(L.log.length > reads, "…reading still works");
  // the default (no lock manager given) in a place without navigator.locks behaves the same
  const root = { innerHTML: "", querySelectorAll() { return []; }, querySelector() { return null; }, contains() { return false; } };
  const b = backend({ "/api/state": [200, state], "/api/send": OKSEND() });
  const app = createApp({ root, toastRoot: { textContent: "" }, adapter: createHttpAdapter({ fetchImpl: b.fetchImpl }), win: { location: { hash: "#/work" }, innerWidth: 1440, setTimeout: () => 0, clearTimeout() {} }, doc: null, storage: memStorage(), ledgerBase: "x" });
  await app.load(); await settle();
  is(app.S.ledger.writer, "unsupported", "the default in a Node environment (no navigator.locks) is 'unsupported', not 'allowed'");
  // a lock manager that throws
  const gate = []; const g = createWriterGate({ locks: { request() { throw new Error("SecurityError"); } }, onChange: (s) => gate.push(s) });
  g.claim("x"); is(gate, ["checking", "unsupported"], "a lock manager that throws means unsupported, never writer");
}

// ---------- the request key is required once a contract promises protection ----------
{
  const w = world();
  const A = await tab(w, { "/api/send": OKSEND() });
  void A;
  const mk = async (newKey) => {
    const w2 = world();
    const root = { innerHTML: "", querySelectorAll() { return []; }, querySelector() { return null; }, contains() { return false; } };
    const b = backend({ "/api/state": [200, state], "/api/send": OKSEND() });
    const app = createApp({ root, toastRoot: { textContent: "" }, adapter: createHttpAdapter({ fetchImpl: b.fetchImpl }), win: { location: { hash: "#/work" }, innerWidth: 1440, setTimeout: () => 0, clearTimeout() {} }, doc: null, storage: memStorage(), ledgerBase: "k", locks: w2.mgr.tab(), newKey });
    await app.load(); await settle();
    return { app, root, S: app.S, posts: () => b.log.filter((l) => l.method === "POST") };
  };
  const bare = await mk(() => null);
  await sendOne(bare);
  is([bare.posts().length, "client_request_id" in bare.posts()[0].body], [1, false], "while no protection is promised, a send whose key cannot be made goes out without one");
  bare.S.dialog = "writes"; bare.app.render();
  hasS(bare.root.innerHTML, "No request key", "…and the record says it has no duplicate protection");
  SEND_KEY.required = true;
  try {
    const strict = await mk(() => null);
    await sendOne(strict);
    is(strict.posts().length, 0, "once protection is promised, a send whose key cannot be made is not sent");
    hasS(strict.S.compose.error.message, "A request key could not be made", "…and says why");
    is(strict.S.writes.length, 0, "…and leaves no record of a request that never left");
    const fine = await mk(undefined);
    await sendOne(fine);
    is(fine.posts().length, 1, "…while a send with a key still goes out");
  } finally { SEND_KEY.required = false; }
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
