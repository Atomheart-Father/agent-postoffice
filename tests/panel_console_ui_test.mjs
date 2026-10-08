// v1.12 Human Console UI behaviour test (ticket v1.12, TDD).
//
// Runs the real inline script from panel/index.html against a minimal DOM / fetch stub.
// Covers: Organization view rendered GENERICALLY from config panel.organization (ACME fixture,
// no BOXZ strings), operator strip + top-right operator shortcut → operator workbench,
// Harness racks (method grouping, ON/OFF/MIXED, group control only on exact member-set match,
// individual switches), compose/reply/ack/file human-mail flows and the reply partial-failure
// contract (reply already sent → retry filing only, never a second send, never ack-one).
import { readFileSync } from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "..", "panel", "index.html"), "utf8");
// v1.13 contract C adds a tiny <head> bootstrap <script> (theme before paint), so the first
// <script> in the file is no longer guaranteed to be the app. Pick the block that defines the
// string table (the real app script); fall back to the last block.
const scriptBlocks = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const APP = scriptBlocks.find((s) => s.includes("const STR")) || scriptBlocks[scriptBlocks.length - 1];
if (!APP) { console.error("BAD: panel/index.html has no inline script"); process.exit(1); }
const CODE = APP;

let failures = 0;
const ok = (msg) => console.log("ok   - " + msg);
const bad = (msg, extra) => { failures++; console.log("BAD  - " + msg + (extra ? " :: " + extra : "")); };
const is = (got, want, msg) => (Object.is(got, want) ? ok(msg) : bad(msg, `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`));
const has = (hay, needle, msg) => (String(hay).includes(needle) ? ok(msg) : bad(msg, `missing ${JSON.stringify(needle)}`));
const hasNot = (hay, needle, msg) => (String(hay).includes(needle) ? bad(msg, `unexpected ${JSON.stringify(needle)}`) : ok(msg));
const settle = async (n = 4) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

// ---- generic ACME fixture (no BOXZ strings anywhere; the renderer must not need them) ----
const ACME_PANEL = {
  ok: true, label: "ACME", operator: "director",
  organization: {
    mailbox: "director", label: "Director",
    children: [
      { mailbox: "scribe", label: "Scribe" },
      { label: "Research", members: ["research-a", "research-b"], children: [
        { mailbox: "lead", label: "Lead", children: [
          { mailbox: "analyst", label: "Analyst" }, { mailbox: "runner", label: "Runner" },
        ] },
      ] },
      { label: "Operations", members: ["ops-a", "ops-b"], children: [{ mailbox: "ware", label: "Warehouse" }] },
    ],
  },
};
const mkBox = (name, method, online, waiting = 0) => ({
  name, app: method === "opencode_plugin" ? "OpenCode" : method === "claude_hook" ? "Claude Code"
    : method === "codex_queue" ? "Codex" : "Manual",
  method, who: name + "-who", online, ident: "",
  counts: { waiting, reminded: 0, failed: 0 }, pending: [], acks: [],
});
const ACME_BOXES = [
  { ...mkBox("director", "notify", true, 2), pending: [
        // from 是服务端 ≤30 字截断的显示串，可以 ≠ 规范 sender：回复预填必须用 sender，不能落到显示串
        { file: "L1.md", status: "waiting", subject: "部署 & 计划", from: "lead-display-trunc" } ] },
  mkBox("scribe", "opencode_plugin", true, 1),
  mkBox("research-a", "opencode_plugin", true),
  mkBox("research-b", "claude_hook", false),
  { ...mkBox("lead", "opencode_plugin", true), identity: {
      box: "lead", registered: true, org_present: true, config_ok: true, memberships: [],
      active_roles: [{ alias: "p.owner", title: "开发", active_target: "lead" }],
      candidate_only_roles: [{ alias: "p.review", title: "审核", active_target: "runner" }],
      company_rules: [{ company: "boxz", path: "/tmp/rules.md" }],
      project_status: [{ project: "alpha", path: "/tmp/STATUS.md" }] } },
  mkBox("analyst", "opencode_plugin", true),
  mkBox("runner", "codex_queue", false),
  mkBox("ops-a", "codex_queue", true),
  mkBox("ops-b", "claude_hook", true),
  mkBox("ware", "notify", true),
];
const ACME_GROUPS = [
  { name: "ocode", members: ["scribe", "research-a", "lead", "analyst"], online: 4, total: 4,
    counts: { waiting: 1, reminded: 0, failed: 0 } },
];
const ACME = {
  home: "/tmp/po", boxes: ACME_BOXES, groups: ACME_GROUPS, aliases: [], switches: [],
  broadcasts: [], config: "", config_error: "", log: [], panel: ACME_PANEL,
};

const LETTER = { ok: true, id: "L1", box: "director", sender: "lead",
  subject: "部署 & 计划", need: "决定", body: "第一行\n第二行 <script>alert(1)</script>" };

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

function makeEnv({ langs = ["zh-Hans-CN"], state = ACME, letter = { status: 200, json: LETTER },
                   ack = null, send = { status: 200, json: { ok: true, id: "S1", ref: "lead/S1" } },
                   archive = { status: 200, json: { ok: true, state: "moved" } },
                   outbox = { status: 200, json: { ok: true, outbox: [] } },
                   edit = { status: 200, json: { ok: true } },
                   retract = { status: 200, json: { ok: true, state: "retracted" } },
                   storage = undefined, storageThrows = false, matchDark = false,
                   hash = "", width = undefined } = {}) {
  const ids = ["title", "home", "hint", "err", "boxes", "groups", "aliases", "switches",
               "broadcasts", "log", "langs", "h-groups", "h-aliases", "h-switches",
               "h-broadcasts", "h-log", "letter", "letter-status",
               "view-org", "view-hk", "racks", "tab-org", "tab-hk", "btn-activity", "btn-compose",
               "op-shortcut", "drawer", "drawer-x", "compose", "compose-x", "compose-cancel",
               "compose-send", "compose-to", "compose-subject", "compose-need", "compose-body",
               "compose-err", "compose-from", "compose-fromnote", "compose-title", "compose-to-label",
               "compose-sug",
               "c-sj", "c-nd", "c-bd", "seg-mbox", "seg-alias", "sheet-bg",
               "ins-tab-inbox", "ins-tab-outbox", "outbox", "theme",
               "edit-subject", "edit-need", "edit-body"];
  const els = {};
  for (const id of ids) els[id] = makeEl(id);
  const btn = (l) => ({ dataset: { lang: l }, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; } });
  els["langs"].children = [btn("zh"), btn("en")];
  // top-bar palette group: static buttons, aria-pressed/labels driven by applyLang (mirrors `langs`)
  const thBtn = (c) => ({ dataset: { themeChoice: c }, textContent: "", attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; } });
  els["theme"].children = ["system", "paper", "night", "mist", "blueprint", "pine", "ember"].map(thBtn);

  const store = storage || new Map();
  const localStorage = {
    getItem: (k) => { if (storageThrows) throw new Error("denied"); return store.has(k) ? store.get(k) : null; },
    setItem: (k, v) => { if (storageThrows) throw new Error("denied"); store.set(k, v); },
  };
  const calls = [];
  const confirmations = [];
  const deferred = [];
  const reply = (status, obj) => ({
    ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(obj)),
  });
  const nth = (spec) => (Array.isArray(spec) ? (spec.shift() || { status: 200, json: {} }) : spec);
  const mqDark = {
    matches: !!matchDark, media: "(prefers-color-scheme: dark)", _handlers: [],
    addEventListener(t, fn) { if (t === "change") this._handlers.push(fn); },
    removeEventListener() {},
    addListener(fn) { this._handlers.push(fn); }, removeListener() {},
    _set(m) { this.matches = m; for (const fn of this._handlers) fn(this); },
  };
  const matchMedia = (q) => (/prefers-color-scheme:\s*dark/.test(String(q)) ? mqDark
    : { matches: false, media: String(q), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
  const ctx = {
    console,
    document: {
      title: "", documentElement: { dataset: {}, lang: "" }, body: makeEl("body"),
      getElementById: (id) => els[id] || null,
      _h: {},
      addEventListener(type, fn) { (this._h[type] = this._h[type] || []).push(fn); },
      createElement: (t) => makeEl(t),
    },
    window: { localStorage, addEventListener() {}, matchMedia, location: { hash }, ...(width !== undefined ? { innerWidth: width } : {}) },
    navigator: { languages: langs, language: langs[0] || "" },
    localStorage,
    matchMedia,
    fetch: async (url, opts = {}) => {
      const method = opts.method || "GET";
      calls.push({ url, method, body: opts.body || null });
      if (method === "GET" && url.startsWith("/api/state")) return reply(200, state);
      if (method === "GET" && url.startsWith("/api/letter")) {
        const spec = nth(letter);
        if (spec && spec.defer) return new Promise((resolve) => { deferred.push(() => resolve(reply(spec.status, spec.json))); });
        return reply(spec.status, spec.json);
      }
      if (method === "GET" && url.startsWith("/api/outbox")) {
        const spec = nth(outbox);
        return reply(spec.status, spec.json);
      }
      if (method === "POST" && url.startsWith("/api/archive-one")) {
        if (archive.throw) throw new Error("network down");
        return reply(archive.status, archive.json);
      }
      if (method === "POST" && url.startsWith("/api/ack-one")) {
        return reply(ack ? ack.status : 200, ack ? ack.json : { ok: true, state: "recorded", id: "L1", to: "lead" });
      }
      if (method === "POST" && url.startsWith("/api/send")) {
        const spec = nth(send);
        return reply(spec.status, spec.json);
      }
      if (method === "POST" && url.startsWith("/api/edit-one")) {
        return reply(edit.status, edit.json);
      }
      if (method === "POST" && url.startsWith("/api/retract-one")) {
        return reply(retract.status, retract.json);
      }
      if (method !== "GET") return reply(200, {});
      return reply(200, state);
    },
    confirm: (msg) => { confirmations.push(msg); return true; },
    setInterval: () => 0,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(CODE, ctx, { filename: "panel/index.html" });
  const fire = (id, type, event) => {
    for (const fn of els[id].handlers[type] || []) fn(event);
  };
  return { ctx, els, calls, confirmations, storage: store, fire,
           systemDark: (m) => mqDark._set(m),
           resolveDeferred: async () => { for (const f of deferred.splice(0)) f(); },
           fireDoc: (type, event) => { for (const fn of ctx.document._h[type] || []) fn(event); } };
}

const clickIn = (container, matcher) => (e) => e.fire(container, "click", { target: { closest: matcher } });
const rowClick = clickIn("boxes", (s) => (s === ".clear" || s === ".toggle" ? null
  : { dataset: { box: "director", id: "L1", file: "L1.md" } }));
const nodeClick = (name) => clickIn("boxes", (s) => (s === "[data-box]" ? { dataset: { box: name } } : null));
const letterAct = (action, disabled = false) => (e) => e.fire("letter", "click", {
  target: { closest: (s) => (s === "[data-letter-action]" ? { dataset: { letterAction: action }, disabled } : null) },
});
const posts = (e, url) => e.calls.filter((c) => c.method === "POST" && (!url || c.url.startsWith(url)));
const detail = (e) => (e.els["letter"].hidden ? "" : String(e.els["letter"].innerHTML));

// ============ 1) Organization: generic render (ACME) ============
{
  const e = makeEnv(); await settle();
  const b = e.els["boxes"].innerHTML;
  has(b, "Director", "根节点用 presentation label");
  has(b, "01", "部门大编号 01");
  has(b, "02", "部门大编号 02");
  has(b, "5 个席位，3 在线", "栏头概览：席位数、在线数（含子孙）");
  has(b, "Research", "部门 label 渲染");
  has(b, "Scribe", "独立 T1 渲染");
  has(b, "Analyst", "嵌套子节点渲染");
  has(b, "人类操作员", "根=操作员时显示人类操作员");
  has(b, 'data-box="lead"', "节点带 data-box 可点");
  hasNot(b, "BOXZ", "generic 视图不出现 BOXZ");
  is((b.match(/data-box="lead"/g) || []).length >= 1, true, "lead 节点存在");
  has(b, "dead", "offline 节点带 dead 类");
  has(b, "research-b", "offline 成员出现");
  // escaping of presentation labels
  const evil = JSON.parse(JSON.stringify(ACME));
  evil.panel.organization.children[1].label = "R<b>x</b>";
  const ev = makeEnv({ state: evil }); await settle();
  has(ev.els["boxes"].innerHTML, "R&lt;b&gt;x&lt;/b&gt;", "label 被转义");
  hasNot(ev.els["boxes"].innerHTML, "<b>x</b>", "label 原始 HTML 不进标记");
}

// ============ 2) Operator strip / top-right shortcut → operator workbench ============
{
  const e = makeEnv(); await settle();
  has(e.els["op-shortcut"].innerHTML, "director", "右上操作员按钮显示信箱名（原样）");
  has(e.els["op-shortcut"].innerHTML, "收件箱", "右上操作员按钮写明是收件箱");
  has(e.els["op-shortcut"].innerHTML, "2", "右上显示 waiting 计数");
  hasNot(e.els["boxes"].innerHTML, 'class="inspector', "工作台是弹窗：首屏不打开");
  e.fire("op-shortcut", "click", { target: {} }); await settle();
  has(e.els["boxes"].innerHTML, "等待你处理", "点击右上按钮打开操作员工作台");
  // node click switches inspector
  e.fire("boxes", "click", { target: { closest: (s) => (s === "[data-box]" ? { dataset: { box: "lead" } } : null) } });
  await settle();
  const ins = e.els["boxes"].innerHTML;
  has(ins, "lead", "点节点后 inspector 切到该信箱");
  has(ins, "lead-who", "inspector 显示 who");
}

// ============ 3) Harness: method grouping / ON-OFF-MIXED / exact-match group control ============
{
  const e = makeEnv(); await settle();
  e.fire("tab-hk", "click", { target: {} }); await settle();
  const r = e.els["racks"].innerHTML;
  has(r, "部分开", "混合状态：部分开");
  has(r, "已开", "全在线：已开");
  has(r, "关", "离线成员的钥匙写明：关");
  has(r, "@ocode", "成员集恰好等于 config group 的 rack 显示组控制");
  is((r.match(/data-group="ocode"/g) || []).length, 1, "组控制只出现一次（精确匹配）");
  has(r, "仅可单独开关", "无匹配组的 rack 提示仅 individual");
  const g = e.els["groups"].innerHTML;
  has(g, "@ocode", "已配置分组列出");
  has(g, "全开", "分组按钮（legacy 键，zh）");
  // rack group control posts once
  e.calls.length = 0;
  e.fire("racks", "click", { target: { closest: (s) => (s === "button[data-group]" ? { dataset: { group: "ocode", status: "offline" } } : null) } });
  await settle();
  const p1 = posts(e, "/api/status");
  is(p1.length, 1, "rack 组控制发一次 /api/status");
  is(JSON.parse(p1[0].body).group, "ocode", "rack 组控制带组名");
  // individual switch
  e.calls.length = 0;
  e.fire("racks", "click", { target: { closest: (s) => (s === ".toggle" ? { dataset: { name: "runner", online: "false" } } : null) } });
  await settle();
  const p2 = posts(e, "/api/status");
  is(p2.length, 1, "individual 开关发一次 /api/status");
  is(JSON.parse(p2[0].body).name, "runner", "individual 开关带 name");
  is(JSON.parse(p2[0].body).status, "online", "offline → online");
}

// ============ 4) Compose: fixed operator / group rejected / double-click / success ref ============
{
  const e = makeEnv(); await settle();
  e.fire("btn-compose", "click", { target: {} }); await settle();
  is(e.els["compose"].hidden, false, "写信弹窗打开");
  is(e.els["compose-from"].textContent, "director", "FROM 固定 operator");
  e.els["compose-to"].value = "ocode";                       // a configured group
  e.fire("compose-send", "click", { target: {} }); await settle();
  is(posts(e, "/api/send").length, 0, "分组收件人被拒绝，不发请求");
  has(e.els["compose-err"].textContent, "分组", "提示不能发给分组");
  e.els["compose-to"].value = "lead";
  e.els["compose-subject"].value = "s";
  e.els["compose-body"].value = "b";
  e.fire("compose-send", "click", { target: {} });
  e.fire("compose-send", "click", { target: {} });           // double click
  await settle();
  const sends = posts(e, "/api/send");
  is(sends.length, 1, "双击只发一次");
  const body = JSON.parse(sends[0].body);
  is(body.from, "director", "发送者固定 operator");
  is(body.to, "lead", "收件人正确");
  is(e.els["compose"].hidden, true, "成功后关闭弹窗");
  has(e.els["letter-status"].textContent, "S1", "成功显示编号");
  has(e.els["letter-status"].textContent, "lead/S1", "成功显示稳定引用");
  // alias target
  e.calls.length = 0;
  e.fire("btn-compose", "click", { target: {} }); await settle();
  e.els["compose-to"].value = "@ocode";
  e.fire("compose-send", "click", { target: {} }); await settle();
  is(posts(e, "/api/send").length, 1, "@逻辑地址允许");
  is(JSON.parse(posts(e, "/api/send")[0].body).to, "@ocode", "@目标原样传给 /api/send");
}

// ============ 5) Reply semantics ============
{
  // 5a. success path: send → archive-one, NEVER ack-one
  const e = makeEnv(); await settle();
  rowClick(e); await settle();
  has(detail(e), "部署 &amp; 计划", "详情打开（正面对照）");
  letterAct("reply")(e); await settle();
  is(e.els["compose"].hidden, false, "回复打开写信弹窗");
  is(e.els["compose-to"].value, "lead", "回复预填 to=原 sender");
  is(e.els["compose-subject"].value, "Re: 部署 & 计划", "回复预填 Re: 主题");
  is(e.els["compose-need"].value, "仅告知", "回复默认 仅告知");
  e.els["compose-body"].value = "回复正文";
  e.calls.length = 0;
  e.fire("compose-send", "click", { target: {} }); await settle();
  const sends = posts(e, "/api/send"), files = posts(e, "/api/archive-one"), acks = posts(e, "/api/ack-one");
  is(sends.length, 1, "回复恰好一次 send");
  is(files.length, 1, "回复成功后归档原信");
  is(acks.length, 0, "回复绝不 ack-one");
  is(e.calls.findIndex((c) => c.url.startsWith("/api/send")) < e.calls.findIndex((c) => c.url.startsWith("/api/archive-one")),
     true, "先 send 后 archive");
  is(e.els["letter"].hidden, true, "成功后详情关闭");
  // 5b. Re: 不无限叠
  const reState = JSON.parse(JSON.stringify(ACME));
  reState.boxes[0].pending[0].subject = "Re: 已有前缀";
  const f = makeEnv({ state: reState }); await settle();
  rowClick(f); await settle();
  letterAct("reply")(f); await settle();
  is(f.els["compose-subject"].value, "Re: 已有前缀", "已有 Re: 不再叠加");
  // 5c. send fail → no archive, letter stays open
  const g = makeEnv({ send: { status: 400, json: { ok: false, error: "refused", message: "发送失败：没有在线信箱" } } }); await settle();
  rowClick(g); await settle();
  letterAct("reply")(g); await settle();
  g.els["compose-body"].value = "x";
  g.calls.length = 0;
  g.fire("compose-send", "click", { target: {} }); await settle();
  is(posts(g, "/api/send").length, 1, "send 尝试了一次");
  is(posts(g, "/api/archive-one").length, 0, "send 失败绝不归档");
  is(g.els["letter"].hidden, false, "原信详情保持打开");
  has(g.els["compose-err"].textContent, "发送失败", "弹窗内显示拒绝原因");
  // 5d. send ok + archive fail → banner + retry-archive-only, no duplicate send, no ack
  const archiveFail = { status: 409, json: { ok: false, error: "conflict" } };
  const h = makeEnv({ archive: archiveFail }); await settle();
  rowClick(h); await settle();
  letterAct("reply")(h); await settle();
  h.els["compose-body"].value = "x";
  h.calls.length = 0;
  h.fire("compose-send", "click", { target: {} }); await settle();
  is(posts(h, "/api/send").length, 1, "回复 send 一次");
  is(posts(h, "/api/archive-one").length, 1, "归档尝试一次");
  has(h.els["letter-status"].textContent, "回复已经发送，原信归档失败", "部分失败横幅");
  has(detail(h), "重试归档", "只提供重试归档按钮");
  hasNot(detail(h), "回复（发送回复并归档）", "不再提供再次回复");
  // after a sent reply the original can only be filed: acknowledging is disabled AND refused by the handler
  has(detail(h), 'data-letter-action="ack" disabled', "回复已发出后「收到并归档」按钮禁用");
  h.calls.length = 0;
  letterAct("ack")(h); await settle();
  is(posts(h, "/api/ack-one").length, 0, "回复已发出后即使触发 ack 也不发请求");
  is(posts(h, "/api/send").length, 0, "回复已发出后不会再 send");
  h.calls.length = 0;
  letterAct("retry")(h); await settle();
  is(posts(h, "/api/send").length, 0, "重试归档不再 send");
  is(posts(h, "/api/ack-one").length, 0, "重试归档绝不 ack-one");
  is(posts(h, "/api/archive-one").length, 1, "重试归档只发 archive-one");
  // 5e. send ok + 归档请求在网络层抛错（非 4xx/5xx）→ 同样是「回复已出门」：只重试归档，绝无二次 send
  const hNet = makeEnv({ archive: { throw: true } }); await settle();
  rowClick(hNet); await settle();
  letterAct("reply")(hNet); await settle();
  hNet.els["compose-body"].value = "x";
  hNet.calls.length = 0;
  hNet.fire("compose-send", "click", { target: {} }); await settle();
  is(posts(hNet, "/api/send").length, 1, "网络抛错：send 恰好一次");
  is(posts(hNet, "/api/archive-one").length, 1, "网络抛错：归档尝试一次");
  has(hNet.els["letter-status"].textContent, "回复已经发送，原信归档失败", "网络抛错也进部分失败横幅");
  has(detail(hNet), "重试归档", "网络抛错只提供重试归档");
  hasNot(detail(hNet), "回复（发送回复并归档）", "网络抛错不再提供再次回复");
}

// ============ 6) Ack / File only / pure read ============
{
  const e = makeEnv({ ack: { status: 200, json: { ok: true, state: "recorded", id: "L1", to: "lead" } } }); await settle();
  rowClick(e); await settle();
  e.calls.length = 0;
  letterAct("ack")(e); await settle();
  const a = posts(e, "/api/ack-one");
  is(a.length, 1, "收到并归档调用 ack-one");
  is(JSON.parse(a[0].body).box, "director", "ack-one 带 box");
  has(e.els["letter-status"].textContent, "已回执", "recorded 提示");
  is(e.els["letter"].hidden, true, "ack 后关闭");
  const e2 = makeEnv({ ack: { status: 200, json: { ok: true, state: "already" } } }); await settle();
  rowClick(e2); await settle();
  letterAct("ack")(e2); await settle();
  has(e2.els["letter-status"].textContent, "已处理过", "already 幂等提示");
  const e3 = makeEnv({ ack: { status: 200, json: { ok: true, state: "receipt_notice" } } }); await settle();
  rowClick(e3); await settle();
  letterAct("ack")(e3); await settle();
  has(e3.els["letter-status"].textContent, "回执通知", "receipt_notice 提示");
  // file only (legacy done action)
  const e4 = makeEnv(); await settle();
  rowClick(e4); await settle();
  e4.calls.length = 0;
  letterAct("done")(e4); await settle();
  const mv = posts(e4, "/api/archive-one");
  is(mv.length, 1, "仅归档调用 archive-one");
  is(Object.keys(JSON.parse(mv[0].body)).sort().join(","), "box,id", "请求体恰好 box+id");
  has(e4.els["letter-status"].textContent, "已归档", "moved → 已归档");
  // opening = pure read
  const e5 = makeEnv(); await settle();
  e5.calls.length = 0;
  rowClick(e5); await settle();
  is(e5.calls.length, 1, "打开信件只有一次 GET");
  is(e5.calls[0].url.startsWith("/api/letter"), true, "那一次 GET 是 /api/letter");
}

// ============ 7) Operator invalid: compose/reply/ack disabled, everything else normal ============
{
  const bad = JSON.parse(JSON.stringify(ACME));
  bad.panel.operator = "ghost";
  const e = makeEnv({ state: bad }); await settle();
  e.fire("btn-compose", "click", { target: {} }); await settle();
  has(e.els["compose-err"].textContent, "操作员", "写信弹窗提示操作员配置问题");
  e.els["compose-to"].value = "lead";
  e.fire("compose-send", "click", { target: {} }); await settle();
  is(posts(e, "/api/send").length, 0, "operator 无效时 send 被禁止");
  e.fire("tab-hk", "click", { target: {} }); await settle();
  has(e.els["racks"].innerHTML, "部分开", "Harness 仍正常");
  rowClick(e); await settle();
  has(detail(e), "操作员", "信件详情显示操作员提示");
  has(detail(e), "disabled", "reply/ack 按钮禁用");
  letterAct("ack", true)(e); await settle();
  is(posts(e, "/api/ack-one").length, 0, "禁用后 ack 不发请求");
}

// ============ 8) Deep link #/mail/<box>/<id> ============
{
  const e = makeEnv({ hash: "#/mail/director/L1" }); await settle();
  is(e.els["letter"].hidden, false, "合法深链打开信件");
  const f = makeEnv({ hash: "#/mail/../etc/L1" }); await settle();
  is(f.els["letter"].hidden, true, "非法 box 深链不开信");
  has(f.els["letter-status"].textContent, "深链", "非法深链提示");
  const g = makeEnv({ hash: "#/nothing" }); await settle();
  is(g.els["letter"].hidden, true, "无关 hash 不动作");
}

// ============ 9) bad panel config: view-level error only ============
{
  const badp = JSON.parse(JSON.stringify(ACME));
  badp.panel = { ok: false, error: "panel 配置：信箱不存在", label: "", operator: "", organization: null };
  const e = makeEnv({ state: badp }); await settle();
  has(e.els["boxes"].innerHTML, "panel 配置：信箱不存在", "组织视图显示 presentation 错误");
  e.fire("tab-hk", "click", { target: {} }); await settle();
  has(e.els["racks"].innerHTML, "部分开", "Harness 不受影响");
  is(e.els["letter"].hidden, true, "无副作用");
}

// ============ 10) UNASSIGNED: routes 已注册但未进组织的信箱不得被静默隐藏 ============
{
  const st = JSON.parse(JSON.stringify(ACME));
  st.boxes.push(mkBox("stray", "notify", true, 1));
  const e = makeEnv({ state: st }); await settle();
  const b = e.els["boxes"].innerHTML;
  has(b, "未编入组织", "编外区域出现");
  has(b, 'data-box="stray"', "未分配信箱仍渲染为可点节点");
  is((b.match(/data-box="stray"/g) || []).length, 1, "不能 duplicate render（恰好一次）");
  e.fire("tab-hk", "click", { target: {} }); await settle();
  has(e.els["racks"].innerHTML, "stray", "Harness 仍按真实 method 出现");
  const st2 = JSON.parse(JSON.stringify(st));
  st2.panel.organization.children.push({ mailbox: "stray", label: "Stray" });
  const e2 = makeEnv({ state: st2 }); await settle();
  hasNot(e2.els["boxes"].innerHTML, "未编入组织", "全部入编后编外区域消失");
  is((e2.els["boxes"].innerHTML.match(/data-box="stray"/g) || []).length, 1, "正式位置恰好一次");
  const n0 = e2.calls.length;
  await e2.ctx.load(); await settle();
  const added = e2.calls.slice(n0);
  is(added.filter((c) => c.method !== "GET" || !c.url.startsWith("/api/state")).length, 0,
     "render 轮询零写请求（routes/config 不被改）");
}

// ============ 11) Harness 组控制：member-set 恰好相等才显示（部分重叠不算） ============
{
  const st = JSON.parse(JSON.stringify(ACME));
  st.groups.push({ name: "part", members: ["scribe", "research-b"], online: 1, total: 2,
    counts: { waiting: 0, reminded: 0, failed: 0 } });
  const e = makeEnv({ state: st }); await settle();
  e.fire("tab-hk", "click", { target: {} }); await settle();
  const r = e.els["racks"].innerHTML;
  hasNot(r, 'data-group="part"', "部分重叠组不得显示组控制");
  has(r, "@ocode", "恰好相等的组仍显示");
}

// ============ 12) 深链生命周期 + 工作台控件可见性 ============
{
  const e = makeEnv({ hash: "#/mail/director/L1" }); await settle();
  is(e.els["letter"].hidden, false, "深链打开");
  e.fire("letter", "click", { target: { closest: (s) => (s === "[data-letter-action]" ? { dataset: { letterAction: "close" } } : null) } });
  await settle();
  is(e.els["letter"].hidden, true, "关闭");
  is(e.ctx.window.location.hash, "", "关闭时清掉 hash");
  await e.ctx.load(); await settle();
  is(e.els["letter"].hidden, true, "轮询后不得重开");
  const s1 = makeEnv({ hash: "#/mail/lead" }); await settle();
  has(s1.els["boxes"].innerHTML, "lead-who", "#/mail/<box> 打开 inspector");
  is(s1.els["letter-status"].textContent.includes("深链"), false, "单段深链不报错");
  // 单段深链在 HARNESS 页也要能落到工作台（自动切回组织视图）
  const s3 = makeEnv(); await settle();
  s3.fire("tab-hk", "click", { target: {} }); await settle();
  is(s3.els["view-hk"].hidden, false, "先切到 HARNESS 页");
  s3.ctx.window.location.hash = "#/mail/lead";
  await s3.ctx.load(); await settle();
  has(s3.els["boxes"].innerHTML, "lead-who", "HARNESS 页收到单段深链就地打开工作台");
  is(s3.els["view-hk"].hidden, false, "工作台是弹窗：不切页，HARNESS 保持");
  const s2 = makeEnv({ hash: "#/mail/nobody..x" }); await settle();
  has(s2.els["letter-status"].textContent, "深链", "非法深链提示");
  is(s2.ctx.window.location.hash, "", "非法深链 hash 被清（不每 5s 重刷）");
  const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  is(/\.toggle[^{}]*\.clear[^{}]*\{[^}]*min-height:\s*44px/.test(css), true, ".toggle/.clear 有 ≥44px 规则");
}

// ============ 13) 工作台是弹窗：默认关、点节点/徽章/CTA 开、关闭键与遮罩关、轮询保持、所有宽度一致 ============
{
  const closeIt = (env) => env.fire("boxes", "click", { target: { closest: (s) => (s === "[data-ins-close]" ? { dataset: {} } : null) } });
  for (const width of [undefined, 1440, 820, 390]) {
    const tag = width === undefined ? "默认" : `${width}px`;
    const e = makeEnv(width === undefined ? {} : { width }); await settle();
    hasNot(e.els["boxes"].innerHTML, 'class="inspector', `${tag}：首屏不开工作台（弹窗）`);
    has(e.els["boxes"].innerHTML, 'data-box="director"', `${tag}：组织视图在`);
    is(e.ctx.document.body.classList.contains("has-layer"), false, `${tag}：未开弹窗时页面可滚动`);
    nodeClick("lead")(e); await settle();
    has(e.els["boxes"].innerHTML, 'class="inspector', `${tag}：点节点打开`);
    has(e.els["boxes"].innerHTML, "lead-who", `${tag}：显示点击的信箱`);
    has(e.els["boxes"].innerHTML, 'role="dialog"', `${tag}：是对话框`);
    is(e.ctx.document.body.classList.contains("has-layer"), true, `${tag}：弹窗打开时背景锁滚动`);
    await e.ctx.load(); await settle();
    has(e.els["boxes"].innerHTML, 'class="inspector', `${tag}：轮询保持打开`);
    closeIt(e); await settle();
    hasNot(e.els["boxes"].innerHTML, 'class="inspector', `${tag}：关闭后不渲染`);
    has(e.els["boxes"].innerHTML, 'data-box="director"', `${tag}：关闭后组织视图仍在`);
    is(e.ctx.document.body.classList.contains("has-layer"), false, `${tag}：关闭后解除滚动锁`);
    await e.ctx.load(); await settle();
    hasNot(e.els["boxes"].innerHTML, 'class="inspector', `${tag}：关闭后轮询不得带回来`);
    e.fire("op-shortcut", "click", { target: {} }); await settle();
    has(e.els["boxes"].innerHTML, 'class="inspector', `${tag}：右上操作员按钮重开`);
    has(e.els["boxes"].innerHTML, "director-who", `${tag}：回到老板工作台`);
    closeIt(e); await settle();
    // the CTA on the organization page is a [data-box] button for the operator
    nodeClick("director")(e); await settle();
    has(e.els["boxes"].innerHTML, "等待你处理", `${tag}：根节点按钮打开老板收件箱`);
  }
  // the close control and the dimmed backdrop are both [data-ins-close]; Esc closes the top layer
  const e2 = makeEnv(); await settle();
  e2.fire("op-shortcut", "click", { target: {} }); await settle();
  const html2 = e2.els["boxes"].innerHTML;
  is((html2.match(/data-ins-close/g) || []).length >= 2, true, "关闭键和遮罩都带 data-ins-close（点外面也能关）");
  e2.fireDoc("keydown", { key: "Escape" }); await settle();
  hasNot(e2.els["boxes"].innerHTML, 'class="inspector', "Esc 关闭工作台");
}

// ============ 14) 工作台弹窗在 HARNESS 页就地打开，不切页 ============
{
  const e = makeEnv(); await settle();
  e.fire("tab-hk", "click", { target: {} }); await settle();
  is(e.els["view-hk"].hidden, false, "先切到 HARNESS");
  hasNot(e.els["boxes"].innerHTML, 'class="inspector', "HARNESS 首屏没有弹窗");
  has(e.els["racks"].innerHTML, "部分开", "racks 正常渲染");
  e.fire("op-shortcut", "click", { target: {} }); await settle();
  is(e.els["view-hk"].hidden, false, "点操作员按钮不切回组织视图");
  is(e.els["tab-hk"].getAttribute("aria-pressed"), "true", "HARNESS 标签仍选中");
  has(e.els["boxes"].innerHTML, 'class="inspector', "弹窗在当前页打开");
  has(e.els["boxes"].innerHTML, "director-who", "打开的是操作员工作台");
  await e.ctx.load(); await settle();
  is(e.els["view-hk"].hidden, false, "轮询后仍是 HARNESS");
  has(e.els["boxes"].innerHTML, 'class="inspector', "轮询保持弹窗");
  e.fire("boxes", "click", { target: { closest: (s) => (s === "[data-ins-close]" ? { dataset: {} } : null) } }); await settle();
  hasNot(e.els["boxes"].innerHTML, 'class="inspector', "HARNESS 页关闭后不渲染");
  is(e.els["view-hk"].hidden, false, "关闭后仍在 HARNESS");
  const m = makeEnv({ width: 390 }); await settle();
  m.fire("tab-hk", "click", { target: {} }); await settle();
  hasNot(m.els["boxes"].innerHTML, 'class="inspector', "手机 HARNESS 首屏无弹窗");
  m.fire("op-shortcut", "click", { target: {} }); await settle();
  is(m.els["view-hk"].hidden, false, "手机点操作员按钮也不切页");
  has(m.els["boxes"].innerHTML, 'class="inspector', "手机就地打开弹窗");
}

// ============ 15) 速修：收件人下拉 + 前缀模糊匹配（名单来自 /api/state，非第二份路由真相） ============
{
  const st = JSON.parse(JSON.stringify(ACME));
  st.aliases = [{ name: "escalate", target: "lead", confirmed: "lead", pending: "", pending_in: 0,
    notify: [], handoff: "", candidates: [{ name: "lead", known: true, online: true }] }];
  const e = makeEnv({ state: st }); await settle();
  e.fire("btn-compose", "click", { target: {} }); await settle();
  const sug = () => String(e.els["compose-sug"].innerHTML);
  const open = () => e.els["compose-sug"].hidden === false;
  is(open(), false, "打开写信时下拉默认收起");
  e.fire("compose-to", "focus", {}); await settle();
  is(open(), true, "聚焦即出下拉");
  is((sug().match(/<button/g) || []).length, ACME_BOXES.length + 1, "空查询列出全部信箱+逻辑地址（不截断）");
  has(sug(), "ware", "列表尾部的信箱也在（不再被 8 条截断）");
  has(sug(), "director", "下拉含物理信箱");
  is(e.els["compose-to"].getAttribute("aria-expanded"), "true", "aria-expanded 同步");
  e.els["compose-to"].value = "res";
  e.fire("compose-to", "input", {}); await settle();
  has(sug(), "research-a", "前缀命中 research-a");
  has(sug(), "research-b", "前缀命中 research-b");
  hasNot(sug(), "director", "不匹配项被过滤");
  e.els["compose-to"].value = "b";           // 分段前缀
  e.fire("compose-to", "input", {}); await settle();
  has(sug(), "research-b", "段前缀命中 research-b");
  has(sug(), "ops-b", "段前缀命中 ops-b");
  e.els["compose-to"].value = "@esc";
  e.fire("compose-to", "input", {}); await settle();
  has(sug(), "@escalate", "逻辑地址前缀命中");
  e.fire("compose-sug", "click", { target: { closest: (s) => (s === "button[data-v]" ? { dataset: { v: "research-a" } } : null) } });
  await settle();
  is(e.els["compose-to"].value, "research-a", "点选写入输入框");
  is(open(), false, "点选后收起");
  is(e.els["compose-to"].getAttribute("aria-expanded"), "false", "aria-expanded 复位");
  e.els["compose-to"].value = "dir";
  e.fire("compose-to", "input", {}); await settle();
  is(open(), true, "再次输入重新打开");
  let stopped = false;
  e.fire("compose-to", "keydown", { key: "Escape", stopPropagation: () => { stopped = true; } });
  is(open(), false, "Escape 只收下拉");
  is(stopped, true, "Escape 被拦住不冒泡");
  is(e.els["compose"].hidden, false, "写信弹窗保持打开");
  e.els["compose-subject"].value = "s"; e.els["compose-body"].value = "b";
  e.fire("compose-send", "click", { target: {} }); await settle();
  is(posts(e, "/api/send").length, 1, "点选后照常发送一次");
  e.fire("btn-compose", "click", { target: {} }); await settle();
  is(open(), false, "重开写信下拉收起");
  // v1.12.4：从某信箱工作台点「写信」→ 收件人默认就是它；下拉点开即可见（截断已移除）
  nodeClick("scribe")(e); await settle();
  e.fire("boxes", "click", { target: { closest: (s) => (s === "[data-compose]" ? {} : null) } }); await settle();
  is(e.els["compose-to"].value, "scribe", "工作台写信预填当前信箱");
  e.fire("compose-to", "click", { target: {} }); await settle();
  has(sug(), "scribe", "下拉含预填信箱");
  e.fire("compose-sug", "click", { target: { closest: (s) => (s === "button[data-v]" ? { dataset: { v: "ware" } } : null) } });
  await settle();
  is(e.els["compose-to"].value, "ware", "预填仍可改选别的信箱");
}

// ============ 16) 收件人下拉：点输入框保持、点外面才收起（真实浏览器事件序列回归） ============
{
  const e = makeEnv(); await settle();
  e.fire("btn-compose", "click", { target: {} }); await settle();
  const open = () => e.els["compose-sug"].hidden === false;
  e.fire("compose-to", "focus", {}); await settle();
  is(open(), true, "聚焦后下拉打开");
  // 点输入框本身：document 外点守卫不得收起（mousedown 后 focus 打开下拉，click 目标仍是输入框）
  e.fireDoc("click", { target: { closest: (s) => (s === "#compose-to" ? e.els["compose-to"] : null) } });
  is(open(), true, "点输入框下拉保持打开");
  is(e.els["compose-to"].getAttribute("aria-expanded"), "true", "aria-expanded 保持 true");
  // 点下拉内部：同样不收起
  e.fireDoc("click", { target: { closest: (s) => (s === "#compose-sug" ? e.els["compose-sug"] : null) } });
  is(open(), true, "点下拉内部保持打开");
  // 点弹窗其它地方：收起
  e.fireDoc("click", { target: { closest: () => null } });
  is(open(), false, "点外面收起下拉");
  is(e.els["compose-to"].getAttribute("aria-expanded"), "false", "收起后 aria-expanded 复位");
  // 焦点没变时再点输入框：重新打开（focus 不会再触发）
  e.fire("compose-to", "click", {}); await settle();
  is(open(), true, "焦点已在输入框时再点一下重新打开");
  // 下拉必须悬浮定位：撑高弹窗会让 mousedown/mouseup 落在不同元素，外点守卫误判收起
  has(html, ".sug{position:absolute", "下拉绝对定位（不撑高弹窗）");
  has(html, ".to-wrap{position:relative}", "下拉锚定收件人行");
}

/* ============================ v1.13 Human Console maturity ============================
   A) operator inspector INBOX|OUTBOX tabs + outbox edit/retract
   B) runtime presence display (activity badges + duration + human/operator)
   C) appearance control (system/light/dark, persisted, head bootstrap)
   All rendering here is presentation-only: zero write requests.
   ==================================================================================== */

// the workbench is a dialog that starts closed: open the operator's first, then switch the tab
const insTab = (tab) => (e) => {
  if (!String(e.els["boxes"].innerHTML).includes('class="inspector')) e.fire("op-shortcut", "click", { target: {} });
  e.fire("boxes", "click", { target: { closest: (s) => {
    if (s.includes("ins-tab") || s.includes("data-ins-tab")) return { id: "ins-tab-" + tab, dataset: { insTab: tab } };
    return null;
  } } });
};
const outboxAct = (action, row) => (e) => e.fire("boxes", "click", { target: { closest: (s) => {
  if (s.includes("outbox-action")) return { dataset: { outboxAction: action, to: row.to, id: row.id } };
  if (s.includes("data-outbox")) return { dataset: { to: row.to, id: row.id } };
  return null;
} } });
// 点击发件箱行本体（不落在按钮上）：closest("[data-outbox-action]") 返回 null，closest("[data-outbox]") 命中
const outboxOpen = (row) => (e) => e.fire("boxes", "click", { target: { closest: (s) => {
  if (s.includes("outbox-action")) return null;
  if (s.includes("data-outbox")) return { dataset: { to: row.to, id: row.id } };
  return null;
} } });
// 点击发件箱详情层里的按钮（#letter 层的事件委托：[data-outbox-action]）
const detailAct = (e, action, row) => e.fire("letter", "click", { target: { closest: (s) => {
  if (s.includes("outbox-action")) return { dataset: { outboxAction: action, to: row.to, id: row.id } };
  return null;
} } });
const editSave = (e) => e.fire("letter", "click", { target: { closest: (s) => (s.includes("edit-save") ? { dataset: {} } : null) } });
const boxSideHTML = (e) => String((e.els["boxes"] && e.els["boxes"].innerHTML) || "")
  + String((e.els["outbox"] && e.els["outbox"].innerHTML) || "");
const insHTML = (e) => { const h = boxSideHTML(e); const i = h.indexOf('class="inspector'); return i < 0 ? "" : h.slice(i); };
const tabPressed = (e, id) => {
  const el = e.els[id];
  const direct = el && el.getAttribute ? el.getAttribute("aria-pressed") : undefined;
  if (direct != null) return direct;
  const tag = (boxSideHTML(e).match(new RegExp('<[^>]*\\bid="' + id + '"[^>]*>')) || [])[0] || "";
  const m = tag.match(/aria-pressed="(true|false)"/);
  return m ? m[1] : undefined;
};
const thBtn = (e, choice) => (e.els["theme"].children || []).find((b) => b.dataset && b.dataset.themeChoice === choice) || null;
const themeClick = (choice) => (e) => e.fire("theme", "click", { target: { closest: (s) =>
  (s.includes("theme-choice") || s.includes("data-theme") ? { dataset: { themeChoice: choice } } : null) } });

// ============ 17) Inspector tabs INBOX | OUTBOX: counts, zero-write switch, row model ============
{
  const stA = JSON.parse(JSON.stringify(ACME));
  stA.boxes.find((b) => b.name === "director").pending.push({ file: "L2.md", status: "waiting", subject: "第二封", from: "scribe" });
  const OBOX = [
    { to: "lead", id: "O1", subject: "第一封外发", need: "仅告知", body: "正文一", status: "pending" },
    { to: "scribe", id: "O2", subject: "第二封外发", need: "决策", body: "正文二", status: "pending" },
    { to: "ops-a", id: "O3", subject: "已送达的信", need: "仅告知", body: "正文三", status: "locked" },
    { to: "ware", id: "O4", subject: "状态未知的信", need: "仅告知", body: "正文四", status: "unknown" },
  ];
  const e = makeEnv({ state: stA, outbox: { status: 200, json: { ok: true, outbox: OBOX } } }); await settle();
  e.fire("op-shortcut", "click", { target: {} }); await settle();
  has(e.els["boxes"].innerHTML, 'id="ins-tab-inbox"', "工作台有收件箱标签");
  has(e.els["boxes"].innerHTML, 'id="ins-tab-outbox"', "工作台有发件箱标签");
  is(tabPressed(e, "ins-tab-inbox"), "true", "默认收件箱标签选中");
  is(tabPressed(e, "ins-tab-outbox"), "false", "默认发件箱标签未选中");
  has(boxSideHTML(e), "收件箱 02", "收件箱标签旁显示待处理计数 02");
  is(posts(e).length, 0, "首屏渲染零写请求");
  e.calls.length = 0;
  insTab("outbox")(e); await settle();
  is(tabPressed(e, "ins-tab-outbox"), "true", "切到发件箱后标签选中");
  is(tabPressed(e, "ins-tab-inbox"), "false", "切到发件箱后收件箱取消选中");
  is(e.els["view-hk"].hidden, true, "切标签仍是组织视图");
  has(boxSideHTML(e), 'data-box="director"', "切标签后组织树仍在");
  is(posts(e).length, 0, "切标签零写请求");
  is(e.calls.filter((c) => c.url.startsWith("/api/outbox")).length, 1, "切到发件箱只读一次 /api/outbox");
  const h = boxSideHTML(e);
  has(h, 'id="outbox"', "发件箱容器存在");
  is((h.match(/data-outbox(?=[\s=>])/g) || []).length, 4, "发件箱 4 行");
  has(h, 'data-to="lead"', "行带 data-to");
  has(h, 'data-id="O1"', "行带 data-id");
  has(h, "发件箱 04", "发件箱标签旁显示行数 04");
  is((h.match(/data-outbox-action="edit"/g) || []).length, 2, "仅 pending 行有编辑按钮");
  is((h.match(/data-outbox-action="retract"/g) || []).length, 2, "仅 pending 行有撤回按钮");
  has(h, "已送达，不能修改", "locked 行显示锁定文案");
  has(h, "状态无法确认，不能修改", "unknown 行显示状态未知文案");
}

// ============ 18) Outbox: empty state is honest (never claims permanent history) ============
{
  const e = makeEnv({ outbox: { status: 200, json: { ok: true, outbox: [] } } }); await settle();
  insTab("outbox")(e); await settle();
  const h = boxSideHTML(e);
  has(h, "暂无当前可观察的已发普通信", "空发件箱文案");
  hasNot(h, "从未发送过信", "空发件箱不谎称从未发过信");
}

// ============ 19) Outbox rows escape user content ============
{
  const evil = [{ to: 'a"b', id: "O1", subject: "<script>alert(1)</script>", need: "x", body: "y", status: "pending" }];
  const e = makeEnv({ outbox: { status: 200, json: { ok: true, outbox: evil } } }); await settle();
  insTab("outbox")(e); await settle();
  const h = boxSideHTML(e);
  has(h, "&lt;script&gt;alert(1)&lt;/script&gt;", "outbox 主题被转义");
  hasNot(h, "<script>alert(1)</script>", "outbox 原始脚本不进标记");
  has(h, "a&quot;b", "outbox 收件人引号被转义");
}

// ============ 20) Outbox edit: prefill, exactly-one POST (no sender), refetch, new subject ============
{
  const row = { to: "lead", id: "O1", subject: "旧主题", need: "仅告知", body: "旧正文", status: "pending" };
  const after = { to: "lead", id: "O1", subject: "新主题", need: "请示", body: "新正文", status: "pending" };
  const e = makeEnv({ outbox: [
    { status: 200, json: { ok: true, outbox: [row] } },
    { status: 200, json: { ok: true, outbox: [after] } },
  ] }); await settle();
  insTab("outbox")(e); await settle();
  e.calls.length = 0;
  outboxAct("edit", { to: "lead", id: "O1" })(e); await settle();
  is(e.els["letter"].hidden, false, "编辑打开信件 overlay");
  has(e.els["letter"].innerHTML, "edit-subject", "overlay 有主题编辑框");
  has(e.els["letter"].innerHTML, "edit-need", "overlay 有需要编辑框");
  has(e.els["letter"].innerHTML, "edit-body", "overlay 有正文编辑框");
  is(e.els["edit-subject"].value, "旧主题", "主题预填");
  is(e.els["edit-need"].value, "仅告知", "需要预填");
  is(e.els["edit-body"].value, "旧正文", "正文预填");
  has(e.els["letter"].innerHTML, "data-edit-save", "有保存按钮");
  e.els["edit-subject"].value = "新主题";
  e.els["edit-need"].value = "请示";
  e.els["edit-body"].value = "新正文";
  editSave(e); await settle();
  const edits = posts(e, "/api/edit-one");
  is(edits.length, 1, "保存只发一次 edit-one");
  const eb = edits[0] ? JSON.parse(edits[0].body) : {};
  is(Object.keys(eb).sort().join(","), "body,id,need,subject,to", "edit 请求体恰好 to,id,subject,need,body（无 sender/from）");
  is(eb.to, "lead", "edit to 正确"); is(eb.id, "O1", "edit id 正确");
  is(eb.subject, "新主题", "edit subject 用新值"); is(eb.need, "请示", "edit need 用新值"); is(eb.body, "新正文", "edit body 用新值");
  is(e.calls.filter((c) => c.url.startsWith("/api/outbox")).length, 1, "保存成功后重新读一次 outbox");
  has(boxSideHTML(e), "新主题", "行显示新主题");
}

// ============ 21) Outbox edit failure: server message, overlay kept ============
{
  const row = { to: "lead", id: "O1", subject: "旧", need: "仅告知", body: "b", status: "pending" };
  const e = makeEnv({ outbox: { status: 200, json: { ok: true, outbox: [row] } },
    edit: { status: 400, json: { ok: false, error: "refused", message: "已经不能改" } } }); await settle();
  insTab("outbox")(e); await settle();
  outboxAct("edit", { to: "lead", id: "O1" })(e); await settle();
  editSave(e); await settle();
  has(e.els["letter-status"].textContent, "已经不能改", "编辑失败显示服务端消息");
  is(e.els["letter"].hidden, false, "编辑失败保留 overlay");
}

// ============ 22) Outbox retract: confirm, exactly-one POST, row disappears after refetch ============
{
  const O1 = { to: "lead", id: "O1", subject: "甲", need: "仅告知", body: "", status: "pending" };
  const O2 = { to: "scribe", id: "O2", subject: "乙", need: "仅告知", body: "", status: "pending" };
  const e = makeEnv({ outbox: [
    { status: 200, json: { ok: true, outbox: [O1, O2] } },
    { status: 200, json: { ok: true, outbox: [O2] } },
  ] }); await settle();
  insTab("outbox")(e); await settle();
  const before = e.confirmations.length;
  e.calls.length = 0;
  outboxAct("retract", { to: "lead", id: "O1" })(e); await settle();
  is(e.confirmations.length, before + 1, "撤回前有一次确认");
  const rs = posts(e, "/api/retract-one");
  is(rs.length, 1, "撤回发一次 retract-one");
  is(rs[0] ? Object.keys(JSON.parse(rs[0].body)).sort().join(",") : "", "id,to", "撤回请求体恰好 to,id");
  hasNot(boxSideHTML(e), 'data-id="O1"', "撤回首行消失");
  has(boxSideHTML(e), 'data-id="O2"', "其余行保留");
}

// ============ 23) Outbox retract failure: message shown, row kept ============
{
  const O1 = { to: "lead", id: "O1", subject: "甲", need: "仅告知", body: "", status: "pending" };
  const e = makeEnv({ outbox: { status: 200, json: { ok: true, outbox: [O1] } },
    retract: { status: 400, json: { ok: false, error: "refused", message: "不能撤回" } } }); await settle();
  insTab("outbox")(e); await settle();
  outboxAct("retract", { to: "lead", id: "O1" })(e); await settle();
  has(e.els["letter-status"].textContent, "不能撤回", "撤回失败显示消息");
  has(boxSideHTML(e), 'data-id="O1"', "撤回失败保留行");
}

// ============ 24) Runtime presence: org node badges + duration (pure presentation) ============
{
  const ACT = JSON.parse(JSON.stringify(ACME));
  const now = Math.floor(Date.now() / 1000);
  const byName = {}; for (const b of ACT.boxes) byName[b.name] = b;
  byName["lead"].activity = { state: "working", since: now - (17 * 60 + 20) };
  byName["research-a"].activity = { state: "idle", since: now - (4 * 3600 + 12 * 60 + 20) };
  byName["ops-a"].activity = { state: "unknown", since: null };
  const e = makeEnv({ state: ACT }); await settle();
  const b = e.els["boxes"].innerHTML;
  has(b, "act-badge", "组织节点有活动徽章");
  has(b, 'data-activity="working"', "working 节点徽章");
  has(b, 'data-activity="idle"', "idle 节点徽章");
  has(b, 'data-activity="unknown"', "unknown 节点徽章");
  has(b, "运行中", "working 标签");
  has(b, "空闲", "idle 标签");
  has(b, "未知", "unknown 标签");
  has(b, "17m", "分钟时长 · 17m");
  has(b, "4h 12m", "小时+分钟时长 · 4h 12m");
  const um = b.match(/data-activity="unknown"[^>]*>([^<]*)</);
  is(!!um && !um[1].includes("·"), true, "unknown 不带时长");
  is(posts(e).length, 0, "活动展示零写请求");
  const en = makeEnv({ langs: ["en-US"], state: ACT }); await settle();
  const eb = en.els["boxes"].innerHTML;
  has(eb, "Working", "en working 标签");
  has(eb, "Idle", "en idle 标签");
  has(eb, "Unknown", "en unknown 标签");
}

// ============ 25) Runtime presence: harness rows carry the same badge ============
{
  const ACT = JSON.parse(JSON.stringify(ACME));
  const now = Math.floor(Date.now() / 1000);
  const byName = {}; for (const b of ACT.boxes) byName[b.name] = b;
  byName["lead"].activity = { state: "working", since: now - (17 * 60 + 20) };
  byName["research-a"].activity = { state: "idle", since: now - (4 * 3600 + 12 * 60 + 20) };
  byName["ops-a"].activity = { state: "unknown", since: null };
  const e = makeEnv({ state: ACT }); await settle();
  e.fire("tab-hk", "click", { target: {} }); await settle();
  const r = e.els["racks"].innerHTML;
  has(r, 'data-activity="working"', "racks working 行活动");
  has(r, 'data-activity="idle"', "racks idle 行活动");
  has(r, 'data-activity="unknown"', "racks unknown 行活动");
  has(r, "17m", "racks 显示时长");
  is(posts(e).length, 0, "racks 活动零写请求");
}

// ============ 26) Runtime presence: inspector row; operator is HUMAN, never a fake IDLE ============
{
  const ACT = JSON.parse(JSON.stringify(ACME));
  const now = Math.floor(Date.now() / 1000);
  const byName = {}; for (const b of ACT.boxes) byName[b.name] = b;
  byName["lead"].activity = { state: "working", since: now - (17 * 60 + 20) };
  const e = makeEnv({ state: ACT }); await settle();
  nodeClick("lead")(e); await settle();
  const ins = insHTML(e);
  has(ins, "活动", "inspector 有活动标签");
  has(ins, "运行中", "inspector 显示 working");
  has(ins, "17m", "inspector 显示时长");
  is(posts(e).length, 0, "inspector 活动零写请求");
  e.fire("op-shortcut", "click", { target: {} }); await settle();
  const op = insHTML(e);
  has(op, "活动", "操作员 inspector 有活动标签");
  has(op, "人类", "操作员显示 HUMAN");
  hasNot(op, "空闲", "操作员不渲染假 IDLE");
  hasNot(op, 'data-activity="idle"', "操作员无假 idle 徽章");
  const en = makeEnv({ langs: ["en-US"], state: ACT }); await settle();
  nodeClick("lead")(en); await settle();
  has(insHTML(en), "Activity", "en inspector 活动标签");
  has(insHTML(en), "Working", "en inspector working");
}

// ============ 27) Appearance: default system, click persists, no writes, no language change ============
{
  const e = makeEnv({ storage: new Map(), matchDark: true }); await settle();
  is(thBtn(e, "system").getAttribute("aria-pressed"), "true", "默认选中跟随系统");
  is(thBtn(e, "paper").getAttribute("aria-pressed"), "false", "默认纸白未选中");
  is(thBtn(e, "night").getAttribute("aria-pressed"), "false", "默认夜未选中");
  is(e.ctx.document.documentElement.dataset.theme, "night", "跟随系统+系统深色 → data-theme=night");
  is(posts(e).length, 0, "主题初始零写请求");

  const storage = new Map();
  const d = makeEnv({ storage, matchDark: false }); await settle();
  is(d.ctx.document.documentElement.dataset.theme, "paper", "跟随系统+系统浅色 → data-theme=paper");
  const title = d.els["title"].textContent;
  d.calls.length = 0;
  themeClick("night")(d); await settle();
  is(d.ctx.document.documentElement.dataset.theme, "night", "点夜设置 data-theme=night");
  is(storage.get("postoffice.theme"), "night", "夜写入 localStorage postoffice.theme");
  is(thBtn(d, "night").getAttribute("aria-pressed"), "true", "夜按钮选中");
  is(d.els["title"].textContent, title, "切主题不改语言");
  is(posts(d).length, 0, "切主题零写请求");
  for (const pal of ["mist", "blueprint", "pine", "ember", "paper"]) {
    themeClick(pal)(d); await settle();
    is(d.ctx.document.documentElement.dataset.theme, pal, `点 ${pal} 设置 data-theme=${pal}`);
  }
  themeClick("neon")(d); await settle();
  is(d.ctx.document.documentElement.dataset.theme, "paper", "未知配色回到跟随系统（此处系统浅色 → paper）");
  is(storage.get("postoffice.theme"), "system", "未知配色存为 system");
}

// ============ 28) Appearance: persist across reload, system resolves, storage unusable, legacy values ============
{
  const storage = new Map([["postoffice.theme", "mist"]]);
  const reload = makeEnv({ storage, matchDark: true }); await settle();
  is(thBtn(reload, "mist").getAttribute("aria-pressed"), "true", "重载后沿用已存的 mist");
  is(reload.ctx.document.documentElement.dataset.theme, "mist", "存了配色时系统深色也被覆盖");

  // choices saved by the previous two-state switch keep working
  const old = makeEnv({ storage: new Map([["postoffice.theme", "dark"]]), matchDark: false }); await settle();
  is(old.ctx.document.documentElement.dataset.theme, "night", "旧版存的 dark → night");
  is(thBtn(old, "night").getAttribute("aria-pressed"), "true", "旧版 dark 对应夜按钮选中");
  const old2 = makeEnv({ storage: new Map([["postoffice.theme", "light"]]), matchDark: true }); await settle();
  is(old2.ctx.document.documentElement.dataset.theme, "paper", "旧版存的 light → paper");

  const s2 = new Map();
  const e = makeEnv({ storage: s2, matchDark: true }); await settle();
  themeClick("paper")(e); await settle();
  is(e.ctx.document.documentElement.dataset.theme, "paper", "点纸白");
  themeClick("system")(e); await settle();
  is(e.ctx.document.documentElement.dataset.theme, "night", "跟随系统解析为 night");
  is(s2.get("postoffice.theme"), "system", "跟随系统存储 system");
  is(thBtn(e, "system").getAttribute("aria-pressed"), "true", "系统按钮选中");

  const broken = makeEnv({ storageThrows: true, matchDark: true }); await settle();
  is(thBtn(broken, "system").getAttribute("aria-pressed"), "true", "存储不可用回退 system");
  is(broken.ctx.document.documentElement.dataset.theme, "night", "存储不可用仍能解析");
  themeClick("pine")(broken); await settle();
  is(broken.ctx.document.documentElement.dataset.theme, "pine", "存储不可用仍可切换");
}

// ============ 29) Appearance: labels via STR both languages + head bootstrap + color-scheme ============
{
  const zh = makeEnv({ langs: ["zh-Hans-CN"], matchDark: true }); await settle();
  is(thBtn(zh, "system").textContent, "跟随系统", "zh 系统标签");
  is(thBtn(zh, "paper").textContent, "纸白", "zh 纸白标签");
  is(thBtn(zh, "night").textContent, "夜", "zh 夜标签");
  has(String(zh.els["theme"].getAttribute("aria-label") || ""), "外观", "zh 主题组 aria-label=外观");
  const en = makeEnv({ langs: ["en-US"], matchDark: true }); await settle();
  is(thBtn(en, "system").textContent, "System", "en System 标签");
  is(thBtn(en, "paper").textContent, "Paper", "en Paper 标签");
  is(thBtn(en, "ember").textContent, "Ember", "en Ember 标签");
  has(String(en.els["theme"].getAttribute("aria-label") || ""), "Appearance", "en 主题组 aria-label=Appearance");
  is(thBtn(en, "night").getAttribute("title"), "Night", "色块按钮 title 写明名字（颜色之外也有文字）");

  // source: a head bootstrap script reads postoffice.theme and sets data-theme, before the app script
  const scripts = html.match(/<script(?:\s[^>]*)?>[\s\S]*?<\/script>/g) || [];
  const boot = scripts.find((s) => s.includes("postoffice.theme") && (s.includes("data-theme") || s.includes("dataset.theme")));
  is(!!boot, true, "源码含引导脚本：读 postoffice.theme 并设 data-theme");
  const idxBoot = html.indexOf("postoffice.theme");
  const idxApp = html.indexOf("const STR");
  is(idxBoot > -1 && idxBoot < idxApp, true, "引导脚本在主脚本之前（head，先于样式渲染）");
  has(html, "color-scheme", "CSS 定义 color-scheme");
  // every palette the switch offers has a block in the stylesheet defining the same role variables
  const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  for (const pal of ["paper", "night", "mist", "blueprint", "pine", "ember"]) {
    const m = css.match(new RegExp("\\[data-theme=" + pal + "\\]\\{([^}]*)\\}"));
    is(!!m, true, `${pal} 配色块存在`);
    for (const role of ["--paper:", "--ink:", "--red:", "--on-red:", "--wire:", "--key-on:", "--scrim:"])
      is(!!m && m[1].includes(role), true, `${pal} 定义 ${role}`);
  }
}

// ============ 30) Outbox rows: need + frozen alias + id/ref + age + the SPECIFIC reason ============
{
  const now = Math.floor(Date.now() / 1000);
  const OBOX = [
    { to: "lead", id: "O1", ref: "lead/O1", subject: "外发一", need: "决定", body: "正文一", alias: "@ocode.lead", mtime: now - 17 * 60, status: "pending", reason: "" },
    { to: "scribe", id: "O2", ref: "scribe/O2", subject: "在途", need: "仅告知", body: "正文二", alias: "", mtime: now - 60, status: "locked", reason: "投递进行中（已有投递认领）" },
    { to: "ware", id: "O3", ref: "ware/O3", subject: "已送达", need: "仅告知", body: "正文三", alias: "", mtime: now - 120, status: "locked", reason: "已送达（插件台账 DELIVERED）" },
    { to: "ops-a", id: "O4", ref: "ops-a/O4", subject: "未知", need: "仅告知", body: "正文四", alias: "", mtime: now - 200, status: "unknown", reason: "无法核实该通道（codex_queue）是否已接受，fail closed 不撤回" },
  ];
  const e = makeEnv({ outbox: { status: 200, json: { ok: true, outbox: OBOX } } }); await settle();
  insTab("outbox")(e); await settle();
  const h = boxSideHTML(e);
  has(h, "lead/O1", "行显示 id/ref");
  has(h, "@ocode.lead", "行显示发送时冻结的 alias（不重新解析）");
  has(h, "决定", "行显示 need");
  has(h, "17m", "行显示 age/time");
  const rowOf = (html, id) => { const i = html.indexOf(`data-id="${id}"`); if (i < 0) return ""; const j = html.indexOf('data-id="', i + 10); return html.slice(i, j < 0 ? undefined : j); };
  const r2 = rowOf(h, "O2");
  has(r2, "投递进行中", "在途行显示具体原因");
  hasNot(r2, "已送达", "在途行不得泛称已送达");
  has(rowOf(h, "O3"), "已送达", "已送达行显示已送达原因");
  has(rowOf(h, "O4"), "fail closed", "unknown 行显示 fail-closed 原因");
}

// ============ 31) Outbox PENDING click → read-only detail; locked → reason, no edit entry ============
{
  const OBOX = [
    { to: "lead", id: "O1", ref: "lead/O1", subject: "外发一", need: "决定", body: "正文一", alias: "@ocode.lead", mtime: 1, status: "pending", reason: "" },
    { to: "ware", id: "O3", ref: "ware/O3", subject: "已送达", need: "仅告知", body: "正文三", alias: "", mtime: 1, status: "locked", reason: "已送达（插件台账 DELIVERED）" },
  ];
  const e = makeEnv({ outbox: { status: 200, json: { ok: true, outbox: OBOX } } }); await settle();
  insTab("outbox")(e); await settle();
  e.calls.length = 0;
  outboxOpen({ to: "lead", id: "O1" })(e); await settle();
  is(e.els["letter"].hidden, false, "点 PENDING 行打开详情层");
  const d = e.els["letter"].innerHTML;
  has(d, "lead/O1", "详情显示 ID/REF");
  has(d, "决定", "详情显示 NEED");
  has(d, "正文一", "详情显示 BODY");
  has(d, "pending", "详情显示 STATUS");
  has(d, 'data-outbox-action="edit"', "PENDING 详情给编辑入口");
  is(posts(e).length, 0, "详情是只读，零写请求");
  e.fire("letter", "click", { target: { closest: (s) => (s.includes("edit-close") ? { dataset: {} } : null) } }); await settle();
  outboxOpen({ to: "ware", id: "O3" })(e); await settle();
  const d3 = e.els["letter"].innerHTML;
  has(d3, "已送达", "locked 详情显示具体原因");
  hasNot(d3, 'data-outbox-action="edit"', "locked 详情无编辑入口");
}

// ============ 32) Open edit invalidates an in-flight letter read (out-of-order response) ============
{
  const row = { to: "lead", id: "O1", ref: "lead/O1", subject: "外发", need: "仅告知", body: "外发正文", status: "pending" };
  const e = makeEnv({
    letter: { defer: true, status: 200, json: LETTER },
    outbox: { status: 200, json: { ok: true, outbox: [row] } },
  }); await settle();
  insTab("outbox")(e); await settle();
  // 先发起一封收件信读取（尚未返回）
  e.fire("boxes", "click", { target: { closest: (s) => (s === "[data-letter]" ? { dataset: { box: "director", id: "L1" } } : null) } });
  await settle();
  // 立刻打开编辑层（同一层）
  outboxAct("edit", { to: "lead", id: "O1" })(e); await settle();
  is(e.els["letter"].innerHTML.includes("edit-subject"), true, "编辑层已打开");
  // 迟到的读信返回：不得覆盖编辑层，也不得改编辑目标
  await e.resolveDeferred(); await settle();
  is(e.els["letter"].innerHTML.includes("edit-subject"), true, "迟到读信不得覆盖编辑层");
  is(e.els["edit-subject"].value, "外发", "编辑目标未被改变");
}

// ============ 33) Appearance: SYSTEM follows live media change; explicit choice is immune ============
{
  const e = makeEnv({ storage: new Map(), matchDark: false }); await settle();
  is(e.ctx.document.documentElement.dataset.theme, "paper", "初始 system + 系统浅色");
  e.systemDark(true); await settle();
  is(e.ctx.document.documentElement.dataset.theme, "night", "system 时系统转深色 → 实时跟随");
  e.systemDark(false); await settle();
  is(e.ctx.document.documentElement.dataset.theme, "paper", "system 时系统转浅色 → 实时跟随");
  themeClick("mist")(e); await settle();
  e.systemDark(true); await settle();
  is(e.ctx.document.documentElement.dataset.theme, "mist", "显式配色不受系统变化影响");
  themeClick("night")(e); await settle();
  e.systemDark(false); await settle();
  is(e.ctx.document.documentElement.dataset.theme, "night", "显式夜不受系统变化影响");
  is(posts(e).length, 0, "系统跟随零写请求");
}

// ============ 34) Retract from the detail layer: detail closes, success shown, list refreshed ============
{
  const O1 = { to: "lead", id: "O1", ref: "lead/O1", subject: "外发一", need: "决定", body: "正文一", status: "pending", reason: "" };
  const e = makeEnv({ outbox: [
    { status: 200, json: { ok: true, outbox: [O1] } },
    { status: 200, json: { ok: true, outbox: [] } },
  ] }); await settle();
  insTab("outbox")(e); await settle();
  outboxOpen({ to: "lead", id: "O1" })(e); await settle();
  is(e.els["letter"].hidden, false, "详情层已打开");
  e.calls.length = 0;
  const before = e.confirmations.length;
  detailAct(e, "retract", { to: "lead", id: "O1" }); await settle();
  is(e.confirmations.length, before + 1, "从详情撤回前有一次确认");
  is(posts(e, "/api/retract-one").length, 1, "从详情撤回发一次 retract-one");
  is(e.els["letter"].hidden, true, "撤回成功后详情层关闭，与列表一致");
  has(e.els["letter-status"].textContent, "已撤回", "撤回成功后明确反馈");
  hasNot(boxSideHTML(e), 'data-id="O1"', "列表该行消失");
}

// ============ 35) Retract from detail failure: detail kept + real reason ============
{
  const O1 = { to: "lead", id: "O1", ref: "lead/O1", subject: "外发一", need: "决定", body: "正文一", status: "pending", reason: "" };
  const e = makeEnv({ outbox: { status: 200, json: { ok: true, outbox: [O1] } },
    retract: { status: 400, json: { ok: false, error: "refused", message: "已送达，不能撤回" } } }); await settle();
  insTab("outbox")(e); await settle();
  outboxOpen({ to: "lead", id: "O1" })(e); await settle();
  detailAct(e, "retract", { to: "lead", id: "O1" }); await settle();
  is(e.els["letter"].hidden, false, "撤回失败保留详情层");
  has(e.els["letter-status"].textContent, "已送达，不能撤回", "失败显示真实原因");
}

// ============ 36) Panel consumes the derived identity (ACTIVE / CANDIDATE / RULES / STATUS) ============
{
  const e = makeEnv({ state: ACME }); await settle();
  nodeClick("lead")(e); await settle();
  const ins = insHTML(e);
  has(ins, "组织身份", "inspector 有组织身份标签");
  has(ins, "现职", "inspector 显示现职");
  has(ins, "p.owner", "inspector 显示现职 alias");
  has(ins, "可候选", "inspector 显示可候选");
  has(ins, "/tmp/STATUS.md", "inspector 显示项目进度指针");
  has(ins, "/tmp/rules.md", "inspector 显示公司规章指针");
  is(posts(e).length, 0, "身份展示零写请求");
  const en = makeEnv({ langs: ["en-US"], state: ACME }); await settle();
  nodeClick("lead")(en); await settle();
  has(insHTML(en), "Active", "en inspector 显示 Active");
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
