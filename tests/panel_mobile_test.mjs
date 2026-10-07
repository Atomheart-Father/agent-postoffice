// v1.13 ticket E: mobile / small-viewport guarantees for the Human Console.
//
// Static assertions over the real panel/index.html source (safe-area padding, dynamic viewport
// height, ≥16px form controls, no fixed panel wider than the viewport) plus the vm harness at the
// contract widths (390 / 430 / 768 / 820): the Organization view must render without error, the
// workbench must start closed and open on a node tap, and the OUTBOX tab must be reachable.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "..", "panel", "index.html"), "utf8");
// v1.13 contract C adds a <head> bootstrap <script>; pick the real app script (defines STR).
const scriptBlocks = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const APP = scriptBlocks.find((s) => s.includes("const STR")) || scriptBlocks[scriptBlocks.length - 1];
if (!APP) { console.error("BAD: panel/index.html has no inline script"); process.exit(1); }
const CODE = APP;
const styles = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join("\n");

let failures = 0;
const ok = (msg) => console.log("ok   - " + msg);
const bad = (msg, extra) => { failures++; console.log("BAD  - " + msg + (extra ? " :: " + extra : "")); };
const is = (got, want, msg) => (Object.is(got, want) ? ok(msg) : bad(msg, `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`));
const has = (hay, needle, msg) => (String(hay).includes(needle) ? ok(msg) : bad(msg, `missing ${JSON.stringify(needle)}`));
const hasNot = (hay, needle, msg) => (String(hay).includes(needle) ? bad(msg, `unexpected ${JSON.stringify(needle)}`) : ok(msg));
const settle = async (n = 4) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

// ---- fixture: an operator root (Director) with one child (lead) ----
const PANEL = { ok: true, label: "ACME", operator: "director",
  organization: { mailbox: "director", label: "Director", children: [{ mailbox: "lead", label: "Lead" }] } };
const BOX = (name, online = true, activity) => ({ name, app: "OpenCode", method: "opencode_plugin",
  who: name + "-who", online, ident: "", counts: { waiting: 0, reminded: 0, failed: 0 }, pending: [], acks: [],
  ...(activity ? { activity } : {}) });
const now = Math.floor(Date.now() / 1000);
const STATE = {
  home: "/tmp/po",
  boxes: [BOX("director"), BOX("lead", true, { state: "working", since: now - 17 * 60 })],
  groups: [], aliases: [], switches: [], broadcasts: [], config: "", config_error: "", log: [], panel: PANEL,
};

function makeEl(id) {
  return {
    id, textContent: "", innerHTML: "", value: "", dataset: {}, attrs: {}, children: [],
    handlers: {}, style: {}, hidden: false, className: "",
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { if (on === undefined) on = !this._s.has(c); if (on) this._s.add(c); else this._s.delete(c); return on; },
    },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    querySelector() { return null; },
  };
}

function makeEnv({ width = 390, state = STATE } = {}) {
  const ids = ["title", "home", "hint", "err", "boxes", "groups", "aliases", "switches",
               "broadcasts", "log", "langs", "view-org", "view-hk", "racks", "tab-org", "tab-hk",
               "btn-activity", "btn-compose", "op-shortcut", "drawer", "drawer-x", "letter",
               "letter-status", "sheet-bg", "theme", "outbox", "ins-tab-inbox", "ins-tab-outbox"];
  const els = {};
  for (const id of ids) els[id] = makeEl(id);
  const thBtn = (c) => ({ dataset: { themeChoice: c }, textContent: "", attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; } });
  els["theme"].children = [thBtn("system"), thBtn("light"), thBtn("dark")];
  const store = new Map();
  const localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) };
  const matchMedia = (q) => ({ matches: false, media: String(q), addEventListener() {}, addListener() {} });
  const reply = (status, obj) => ({ ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(obj)) });
  const calls = [];
  const ctx = {
    console,
    document: {
      title: "", documentElement: { dataset: {}, lang: "" }, body: makeEl("body"),
      getElementById: (id) => els[id] || null,
      _h: {},
      addEventListener(type, fn) { (this._h[type] = this._h[type] || []).push(fn); },
      createElement: (t) => makeEl(t),
    },
    window: { localStorage, addEventListener() {}, matchMedia, location: { hash: "" }, innerWidth: width },
    navigator: { languages: ["zh-Hans-CN"], language: "zh-Hans-CN" },
    localStorage,
    matchMedia,
    fetch: async (url, opts = {}) => {
      calls.push({ url, method: opts.method || "GET", body: opts.body || null });
      if ((opts.method || "GET") === "GET" && url.startsWith("/api/state")) return reply(200, state);
      if ((opts.method || "GET") === "GET" && url.startsWith("/api/outbox")) return reply(200, { ok: true, outbox: [] });
      if ((opts.method || "GET") !== "GET") return reply(200, {});
      return reply(200, state);
    },
    confirm: () => true,
    setInterval: () => 0,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(CODE, ctx, { filename: "panel/index.html" });
  const fire = (id, type, event) => { for (const fn of els[id].handlers[type] || []) fn(event); };
  return { ctx, els, calls, fire };
}

// ============ 1) Static CSS: safe-area + dynamic viewport + ≥16px inputs ============
{
  has(html, "env(safe-area-inset", "CSS 使用 env(safe-area-inset*) 安全区内边距");
  has(html, "100dvh", "CSS 使用 100dvh（移动端动态视口高度）");
  const rules = [...styles.matchAll(/([^{}]+)\{([^{}]*)\}/g)];
  const bigInput = rules.some(([, sel, body]) => /\b(input|textarea)\b/.test(sel) && /font-size\s*:\s*16px/.test(body));
  is(bigInput, true, "input/textarea 控件 font-size:16px（iOS 不缩放）");
}

// ============ 2) Static CSS: nothing fixed wider than a 390px viewport ============
{
  is(/@media\s*\(max-width:\s*900px\)[\s\S]*?\.inspector[^{}]*\{[^}]*width\s*:\s*100vw/.test(styles),
     true, ".inspector 在 ≤900px 用 width:100vw");
  is(/\.modal\s+\.card[^{}]*\{[^}]*max-width/.test(styles), true, ".modal .card 用 max-width 约束");
}

// ============ 3) vm at contract widths: renders, workbench closed→open, outbox reachable ============
{
  const clickNode = (e, name) => e.fire("boxes", "click", { target: { closest: (s) => (s === "[data-box]" ? { dataset: { box: name } } : null) } });
  const clickOutboxTab = (e) => e.fire("boxes", "click", { target: { closest: (s) =>
    (s.includes("ins-tab") || s.includes("data-ins-tab") ? { id: "ins-tab-outbox", dataset: { insTab: "outbox" } } : null) } });
  const side = (e) => String((e.els["boxes"] && e.els["boxes"].innerHTML) || "")
    + String((e.els["outbox"] && e.els["outbox"].innerHTML) || "");
  for (const width of [390, 430, 768, 820]) {
    let e;
    try { e = makeEnv({ width }); await settle(); }
    catch (err) { bad(`${width}px 渲染抛错`, String(err && err.message)); continue; }
    is(e.els["err"].textContent, "", `${width}px 组织视图渲染无错误`);
    has(e.els["boxes"].innerHTML, "Director", `${width}px 组织视图渲染`);
    is(e.els["letter"].hidden, true, `${width}px 首屏 #letter 关闭`);
    if (width === 390) hasNot(e.els["boxes"].innerHTML, 'class="inspector', `${width}px 首屏工作台关闭`);
    clickNode(e, "lead"); await settle();
    has(e.els["boxes"].innerHTML, 'class="inspector', `${width}px 点节点后工作台打开`);
    is(e.els["letter"].hidden, true, `${width}px #letter 仍关闭（工作台非信件层）`);
    e.calls.length = 0;
    clickOutboxTab(e); await settle();
    has(side(e), 'id="outbox"', `${width}px 发件箱标签可达`);
    is(e.calls.filter((c) => c.method !== "GET").length, 0, `${width}px 交互零写请求`);
  }
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
