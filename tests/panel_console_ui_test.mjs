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
const script = html.match(/<script>([\s\S]*?)<\/script>/);
if (!script) { console.error("BAD: panel/index.html has no inline script"); process.exit(1); }
const CODE = script[1];

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
  mkBox("lead", "opencode_plugin", true),
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
                   archive = { status: 200, json: { ok: true, state: "moved" } }, hash = "", width = undefined } = {}) {
  const ids = ["title", "home", "hint", "err", "boxes", "groups", "aliases", "switches",
               "broadcasts", "log", "langs", "h-groups", "h-aliases", "h-switches",
               "h-broadcasts", "h-log", "letter", "letter-status",
               "view-org", "view-hk", "racks", "tab-org", "tab-hk", "btn-activity", "btn-compose",
               "op-shortcut", "drawer", "drawer-x", "compose", "compose-x", "compose-cancel",
               "compose-send", "compose-to", "compose-subject", "compose-need", "compose-body",
               "compose-err", "compose-from", "compose-fromnote", "compose-title", "compose-to-label",
               "compose-sug",
               "c-sj", "c-nd", "c-bd", "seg-mbox", "seg-alias", "sheet-bg"];
  const els = {};
  for (const id of ids) els[id] = makeEl(id);
  const btn = (l) => ({ dataset: { lang: l }, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; } });
  els["langs"].children = [btn("zh"), btn("en")];

  const localStorage = { getItem: () => null, setItem: () => {} };
  const calls = [];
  const confirmations = [];
  const reply = (status, obj) => ({
    ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(obj)),
  });
  const ctx = {
    console,
    document: {
      title: "", documentElement: {}, body: makeEl("body"),
      getElementById: (id) => els[id] || null,
      _h: {},
      addEventListener(type, fn) { (this._h[type] = this._h[type] || []).push(fn); },
      createElement: (t) => makeEl(t),
    },
    window: { localStorage, addEventListener() {}, location: { hash }, ...(width !== undefined ? { innerWidth: width } : {}) },
    navigator: { languages: langs, language: langs[0] || "" },
    localStorage,
    fetch: async (url, opts = {}) => {
      const method = opts.method || "GET";
      calls.push({ url, method, body: opts.body || null });
      if (method === "GET" && url.startsWith("/api/state")) return reply(200, state);
      if (method === "GET" && url.startsWith("/api/letter")) {
        const spec = Array.isArray(letter) ? (letter.shift() || { status: 200, json: {} }) : letter;
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
        const spec = Array.isArray(send) ? (send.shift() || { status: 200, json: { ok: true, id: "S", ref: "x/S" } }) : send;
        return reply(spec.status, spec.json);
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
  return { ctx, els, calls, confirmations, fire,
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
  has(b, "CH-01", "TX-6 通道标记");
  has(b, "Research", "部门 label 渲染");
  has(b, "Scribe", "独立 T1 渲染");
  has(b, "Analyst", "嵌套子节点渲染");
  has(b, "HUMAN OPERATOR", "根=操作员时显示 HUMAN OPERATOR");
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
  has(e.els["op-shortcut"].innerHTML, "OPERATOR · director", "右上操作员徽章");
  has(e.els["op-shortcut"].innerHTML, "2", "右上显示 waiting 计数");
  has(e.els["boxes"].innerHTML, "等待你处理", "inspector 默认是操作员工作台");
  e.fire("op-shortcut", "click", { target: {} }); await settle();
  has(e.els["boxes"].innerHTML, "等待你处理", "点击右上徽章后仍是操作员工作台");
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
  has(r, "MIXED", "混合状态 MIXED");
  has(r, "ON", "全在线 ON");
  has(r, "OFF", "全离线 OFF");
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
  has(e.els["racks"].innerHTML, "MIXED", "Harness 仍正常");
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
  has(e.els["racks"].innerHTML, "MIXED", "Harness 不受影响");
  is(e.els["letter"].hidden, true, "无副作用");
}

// ============ 10) UNASSIGNED: routes 已注册但未进组织的信箱不得被静默隐藏 ============
{
  const st = JSON.parse(JSON.stringify(ACME));
  st.boxes.push(mkBox("stray", "notify", true, 1));
  const e = makeEnv({ state: st }); await settle();
  const b = e.els["boxes"].innerHTML;
  has(b, "未分配", "UNASSIGNED 区域出现");
  has(b, 'data-box="stray"', "未分配信箱仍渲染为可点节点");
  is((b.match(/data-box="stray"/g) || []).length, 1, "不能 duplicate render（恰好一次）");
  e.fire("tab-hk", "click", { target: {} }); await settle();
  has(e.els["racks"].innerHTML, "stray", "Harness 仍按真实 method 出现");
  const st2 = JSON.parse(JSON.stringify(st));
  st2.panel.organization.children.push({ mailbox: "stray", label: "Stray" });
  const e2 = makeEnv({ state: st2 }); await settle();
  hasNot(e2.els["boxes"].innerHTML, "未分配", "全部入编后 UNASSIGNED 区域消失");
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
  has(s3.els["boxes"].innerHTML, "lead-who", "HARNESS 页收到单段深链切回工作台");
  is(s3.els["view-hk"].hidden, true, "视图已切回组织");
  const s2 = makeEnv({ hash: "#/mail/nobody..x" }); await settle();
  has(s2.els["letter-status"].textContent, "深链", "非法深链提示");
  is(s2.ctx.window.location.hash, "", "非法深链 hash 被清（不每 5s 重刷）");
  const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  is(/\.toggle\s*,\s*\.clear\s*\{[^}]*min-height:\s*44px/.test(css), true, ".toggle/.clear 有 ≥44px 规则");
}

// ============ 13) 工作台可关闭：桌面收起右轨、手机首屏不开、点节点/徽章重开 ============
{
  const closeIt = (env) => env.fire("boxes", "click", { target: { closest: (s) => (s === "[data-ins-close]" ? { dataset: {} } : null) } });
  const e = makeEnv(); await settle();
  has(e.els["boxes"].innerHTML, 'class="inspector', "桌面默认打开工作台");
  is(e.ctx.document.body.classList.contains("with-ins"), true, "桌面默认让出右轨");
  closeIt(e); await settle();
  hasNot(e.els["boxes"].innerHTML, 'class="inspector', "关闭后工作台不渲染");
  is(e.ctx.document.body.classList.contains("with-ins"), false, "关闭后收起右轨");
  has(e.els["boxes"].innerHTML, 'data-box="director"', "组织视图仍在");
  await e.ctx.load(); await settle();
  hasNot(e.els["boxes"].innerHTML, 'class="inspector', "关闭后轮询不得把它带回来");
  nodeClick("lead")(e); await settle();
  has(e.els["boxes"].innerHTML, 'class="inspector', "点节点重新打开");
  has(e.els["boxes"].innerHTML, "lead-who", "显示点击的信箱");
  await e.ctx.load(); await settle();
  has(e.els["boxes"].innerHTML, 'class="inspector', "点节点后轮询保持打开（insOpen 已置位）");
  closeIt(e); await settle();
  e.fire("op-shortcut", "click", { target: {} }); await settle();
  has(e.els["boxes"].innerHTML, 'class="inspector', "右上 OPERATOR 徽章重开工作台");
  has(e.els["boxes"].innerHTML, "director-who", "回到老板工作台");
  await e.ctx.load(); await settle();
  has(e.els["boxes"].innerHTML, 'class="inspector', "徽章重开后轮询保持打开");
  const m = makeEnv({ width: 390 }); await settle();
  hasNot(m.els["boxes"].innerHTML, 'class="inspector', "手机首屏不开工作台（组织视图优先）");
  is(m.ctx.document.body.classList.contains("with-ins"), false, "手机不让出右轨");
  nodeClick("lead")(m); await settle();
  has(m.els["boxes"].innerHTML, 'class="inspector', "手机点节点才打开");
  await m.ctx.load(); await settle();
  has(m.els["boxes"].innerHTML, 'class="inspector', "手机打开后轮询保持");
  closeIt(m); await settle();
  hasNot(m.els["boxes"].innerHTML, 'class="inspector', "手机关闭回到组织视图");
}

// ============ 14) 速修：HARNESS 页开 OPERATOR 不切回组织视图（工作台是全局右轨） ============
{
  const e = makeEnv(); await settle();
  e.fire("tab-hk", "click", { target: {} }); await settle();
  is(e.els["view-hk"].hidden, false, "先切到 HARNESS");
  has(e.els["boxes"].innerHTML, 'class="inspector', "HARNESS 页工作台仍在（右轨不随切页消失）");
  has(e.els["racks"].innerHTML, "MIXED", "racks 正常渲染");
  e.fire("boxes", "click", { target: { closest: (s) => (s === "[data-ins-close]" ? { dataset: {} } : null) } }); await settle();
  hasNot(e.els["boxes"].innerHTML, 'class="inspector', "HARNESS 页关闭后不渲染");
  e.fire("op-shortcut", "click", { target: {} }); await settle();
  is(e.els["view-hk"].hidden, false, "点 OPERATOR 不切回组织视图");
  is(e.els["tab-hk"].getAttribute("aria-pressed"), "true", "HARNESS 标签仍选中");
  has(e.els["boxes"].innerHTML, 'class="inspector', "工作台在当前视图打开");
  has(e.els["boxes"].innerHTML, "director-who", "打开的是操作员工作台");
  is(e.ctx.document.body.classList.contains("with-ins"), true, "打开后让出右轨");
  await e.ctx.load(); await settle();
  is(e.els["view-hk"].hidden, false, "轮询后仍是 HARNESS");
  has(e.els["boxes"].innerHTML, 'class="inspector', "轮询保持工作台");
  const m = makeEnv({ width: 390 }); await settle();
  m.fire("tab-hk", "click", { target: {} }); await settle();
  hasNot(m.els["boxes"].innerHTML, 'class="inspector', "手机 HARNESS 首屏无工作台");
  m.fire("op-shortcut", "click", { target: {} }); await settle();
  is(m.els["view-hk"].hidden, false, "手机点 OPERATOR 也不切页");
  has(m.els["boxes"].innerHTML, 'class="inspector', "手机就地打开工作台");
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
  is((sug().match(/<button/g) || []).length, 8, "空查询最多 8 条");
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

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
