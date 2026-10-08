// Organization and Harness are drawn from /api/state, not from the page: this test feeds the real inline script
// organizations the page has never seen (other names, other depth, other apps, no organization at all, a broken
// one) and checks that the output follows the data. It also pins the wire graph (every seat reaches the root)
// and the popup contract of the operator workbench. Runs in a Node vm with a minimal DOM stub, like the other
// panel tests: no browser, no network.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "..", "panel", "index.html"), "utf8");
const blocks = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const CODE = blocks.find((s) => s.includes("const STR")) || blocks[blocks.length - 1];

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const is = (g, w, m) => (Object.is(g, w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));
const has = (h, n, m) => (String(h).includes(n) ? ok(m) : bad(m, `missing ${JSON.stringify(n)}`));
const hasNot = (h, n, m) => (String(h).includes(n) ? bad(m, `unexpected ${JSON.stringify(n)}`) : ok(m));
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0)); };

const makeEl = (id) => ({ id, textContent: "", innerHTML: "", value: "", dataset: {}, attrs: {}, children: [], handlers: {}, style: {},
  hidden: false, className: "",
  classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } },
  setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
  addEventListener(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); }, querySelector() { return null; } });

function env(state, { width = 1440 } = {}) {
  const ids = ["title", "home", "hint", "err", "boxes", "groups", "aliases", "switches", "broadcasts", "log", "langs", "letter",
    "letter-status", "view-hk", "racks", "tab-org", "tab-hk", "op-shortcut", "drawer", "compose", "theme", "hk-stats", "h-groups"];
  const els = {}; for (const id of ids) els[id] = makeEl(id);
  const store = new Map();
  const mm = () => ({ matches: false, addEventListener() {}, addListener() {} });
  const ctx = { console, document: { title: "", documentElement: { dataset: {}, lang: "" }, body: makeEl("body"), _h: {},
      getElementById: (id) => els[id] || null, addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); }, createElement: makeEl },
    window: { localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) },
      addEventListener() {}, matchMedia: mm, location: { hash: "" }, innerWidth: width },
    navigator: { languages: ["en-US"], language: "en-US" }, matchMedia: mm,
    fetch: async (url, o = {}) => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify((o.method || "GET") === "GET" ? state : {})) }),
    confirm: () => true, setInterval: () => 0 };
  ctx.localStorage = ctx.window.localStorage; ctx.globalThis = ctx;
  vm.createContext(ctx); vm.runInContext(CODE, ctx);
  return { ctx, els, ev: (src) => vm.runInContext(src, ctx),
    fire: (id, t, e) => { for (const fn of els[id].handlers[t] || []) fn(e); },
    fireDoc: (t, e) => { for (const fn of ctx.document._h[t] || []) fn(e); } };
}
const box = (name, app, online = true, waiting = 0, extra = {}) => ({ name, app, method: app.toLowerCase().replace(/\W+/g, "_"), who: "", online, ident: "",
  counts: { waiting, reminded: 0, failed: 0 }, pending: [], acks: [], ...extra });
const mk = (boxes, panel, groups = []) => ({ home: "/x", boxes, groups, aliases: [], switches: [], broadcasts: [], config: "", config_error: "", log: [], panel });
const seatCount = (h, name) => (h.match(new RegExp(`class="item seat[^"]*" data-box="${name}"`, "g")) || []).length;

// ---- 1) an organization the page has never seen: nothing from another company leaks in, and every seat is drawn once ----
{
  const A = mk([box("ceo", "Manual", true, 2), box("tom", "Zeta"), box("ann", "Zeta", false), box("kim", "Omega", true, 1), box("lee", "Omega"), box("sue", "Omega")],
    { ok: true, label: "Foo", operator: "ceo", organization: { mailbox: "ceo", label: "Chief", children: [
      { label: "Platform", members: ["tom", "ann"], children: [{ mailbox: "kim", label: "Kim's team", children: [{ mailbox: "lee", label: "Lee" }] }] },
      { mailbox: "sue", label: "Solo seat" } ] } });
  const e = env(A); await settle();
  const h = e.els["boxes"].innerHTML;
  has(h, "Chief", "root label from the config"); has(h, "Platform", "team label from the config"); has(h, "Kim&#39;s team".replace("&#39;", "'"), "seat label from the config");
  is((h.match(/class="col"/g) || []).length, 2, "one column per child of the root");
  for (const n of ["tom", "ann", "kim", "lee", "sue"]) is(seatCount(h, n), 1, `${n} is drawn exactly once`);
  hasNot(h, "Director", "no company name from another fixture is baked into the page");
  has(h, "3 seats, 1 online".replace("3", "4").replace("1 online", "3 online"), "column header counts seats and online seats from the data");
  // the wire graph: every node reaches the root, and the head row hangs off it
  const reach = e.ev(`Object.keys(NODES).every((id) => chain(id).includes("root"))`);
  is(reach, true, "every seat, team and column head has a path to the root");
  is(e.ev(`NODES["s:lee"].parent`), "s:kim", "a nested seat hangs from its parent seat");
  is(/^[ht]\d+$/.test(e.ev(`NODES["s:tom"].parent`)), true, "a member hangs from its team column or team node");
  // same page, different data → different output
  const B = JSON.parse(JSON.stringify(A));
  B.panel.organization.children = [{ label: "Only", members: ["tom"] }];
  B.boxes = B.boxes.slice(0, 2);
  const e2 = env(B); await settle();
  hasNot(e2.els["boxes"].innerHTML, "Platform", "another organization replaces the first one entirely");
  is((e2.els["boxes"].innerHTML.match(/class="col"/g) || []).length, 1, "one column for one child");
}

// ---- 2) no organization: one column per app under the operator, with the reason on screen ----
{
  const S = mk([box("ceo", "Manual"), box("a", "Zeta"), box("b", "Zeta"), box("c", "Omega")], { ok: true, label: "Foo", operator: "ceo", organization: null });
  const e = env(S); await settle();
  const h = e.els["boxes"].innerHTML;
  is((h.match(/class="col"/g) || []).length, 2, "grouped by app: Zeta and Omega");
  has(h, "No organization in config.json", "says why the tree is flat");
  for (const n of ["a", "b", "c"]) is(seatCount(h, n), 1, `${n} is a seat once`);
  has(h, 'data-box="ceo"', "the operator stays reachable (CTA)");
  const none = env(mk([], { ok: false, error: "", label: "", operator: "", organization: null })); await settle();
  has(none.els["boxes"].innerHTML, "address book is empty", "no mailboxes at all: an honest empty state");
}

// ---- 3) broken organization config: the error is shown, every mailbox is still reachable ----
{
  const S = mk([box("ceo", "Manual"), box("a", "Zeta")], { ok: false, error: "panel config: mailbox ghost not found", label: "", operator: "", organization: null });
  const e = env(S); await settle();
  has(e.els["boxes"].innerHTML, "panel config: mailbox ghost not found", "the config error is on the page");
  is(seatCount(e.els["boxes"].innerHTML, "a"), 1, "the mailbox is still a seat");
}

// ---- 4) Harness: sections come from the mailboxes' own route methods ----
{
  const S = mk([box("a", "Zeta"), box("b", "Zeta", false), box("c", "Omega")], { ok: true, label: "", operator: "", organization: null });
  const e = env(S); await settle();
  e.fire("tab-hk", "click", { target: {} }); await settle();
  const r = e.els["racks"].innerHTML;
  is((r.match(/class="app"/g) || []).length, 2, "one section per route method");
  has(r, "<h2>Zeta</h2>", "section title is the app name from the data"); has(r, "<h2>Omega</h2>", "second section from the data");
  is((r.match(/class="key toggle/g) || []).length, 3, "one key per mailbox");
  has(r, 'data-online="false"', "an off key says so");
}

// ---- 5) the operator workbench is a popup on every width ----
for (const width of [390, 820, 1440]) {
  const S = mk([box("ceo", "Manual", true, 1, { pending: [{ file: "x.md", status: "waiting", subject: "s", from: "a" }] }), box("a", "Zeta")],
    { ok: true, label: "", operator: "ceo", organization: { mailbox: "ceo", label: "Boss", children: [{ mailbox: "a", label: "A" }] } });
  const e = env(S, { width }); await settle();
  hasNot(e.els["boxes"].innerHTML, 'class="inspector', `${width}px: closed until opened`);
  e.fire("boxes", "click", { target: { closest: (s) => (s === "[data-box]" ? { dataset: { box: "ceo" } } : null) } }); await settle();
  const h = e.els["boxes"].innerHTML;
  has(h, 'role="dialog"', `${width}px: a dialog`); has(h, 'aria-modal="true"', `${width}px: modal`);
  is((h.match(/data-ins-close/g) || []).length >= 2, true, `${width}px: close button and dimmed backdrop both close it`);
  has(h, 'data-compose="1"', `${width}px: the actions live in the footer`);
  e.fireDoc("keydown", { key: "Escape" }); await settle();
  hasNot(e.els["boxes"].innerHTML, 'class="inspector', `${width}px: Esc closes it`);
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
