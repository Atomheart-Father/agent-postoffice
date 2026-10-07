// Panel letter-detail UI behaviour test (ticket 2, Red first).
//
// Runs the real inline script from panel/index.html against a minimal DOM / fetch stub. No browser,
// no framework, no model call, no network (all fetch is stubbed).
//
// Frozen DOM contract the implementation is expected to satisfy (the vm stub can only observe this
// shape — it is part of the ticket, not an implementation detail):
//   * a pending row inside #boxes carries a clickable element matching [data-letter] with
//     dataset { box, id, file } (id is the bare letter id, no .md); opening is delegated on #boxes.
//   * the detail view is rendered into the existing #letter element (innerHTML, same esc() style as
//     the rest of the page); it shows the 信件详情 label, the fetched sender / subject / need and the
//     full body, with raw HTML escaped.
//   * its buttons match [data-letter-action="done"] / [data-letter-action="close"]; clicks are
//     delegated on #letter.
//   * 仅归档 POSTs /api/archive-one {box, id}; on ok it closes the detail, re-fetches /api/state
//     and shows 已归档 / Filed somewhere visible (#letter-status or the existing status area).
//   * a language switch closes the open detail and never issues a write call.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "..", "panel", "index.html"), "utf8");
const scriptBlocks = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
// v1.13 contract C adds a <head> bootstrap <script> (theme before paint), so the first <script> is
// no longer the app. Pick the block that defines the string table (the real app script).
const CODE = scriptBlocks.find((s) => s.includes("const STR")) || scriptBlocks[scriptBlocks.length - 1];
if (!CODE) { console.error("BAD: panel/index.html has no inline script"); process.exit(1); }

let failures = 0;
const ok = (msg) => console.log("ok   - " + msg);
const bad = (msg, extra) => { failures++; console.log("BAD  - " + msg + (extra ? " :: " + extra : "")); };
const is = (got, want, msg) => (Object.is(got, want) ? ok(msg) : bad(msg, `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`));
const has = (hay, needle, msg) => (String(hay).includes(needle) ? ok(msg) : bad(msg, `missing ${JSON.stringify(needle)}`));
const hasNot = (hay, needle, msg) => (String(hay).includes(needle) ? bad(msg, `unexpected ${JSON.stringify(needle)}`) : ok(msg));
const settle = async (n = 4) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

// Three pending letters; row metadata deliberately differs from the detail fixture so the test can
// tell that the detail uses the GET /api/letter answer (the source of truth).
const STATE = {
  home: "/tmp/postoffice",
  boxes: [{
    name: "boxz", app: "手动", method: "notify", who: "面板测试", online: true, ident: "",
    counts: { waiting: 3, reminded: 0, failed: 0 },
    pending: [
      { file: "A.md", status: "waiting", subject: "行内主题 A", from: "row-from-A" },
      { file: "B.md", status: "waiting", subject: "行内主题 B", from: "row-from-B" },
      { file: "C.md", status: "waiting", subject: "行内主题 C", from: "row-from-C" },
    ],
    acks: [],
  }],
  groups: [], aliases: [], switches: [], broadcasts: [], config: "", config_error: "", log: [],
};

const LETTER = {
  ok: true, id: "A", box: "boxz",
  sender: "alice & bob",
  subject: "主题 & <script>alert(1)</script>",
  need: "处理 & 回复",
  body: "第一行 & <script>alert(1)</script>\n第二行 <b>粗体</b>",
};

function makeEl(id) {
  return {
    id, textContent: "", innerHTML: "", value: "", dataset: {}, attrs: {}, children: [],
    handlers: {}, style: {}, hidden: false, className: "",
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    querySelector() { return null; },
  };
}

function makeEnv({ langs = ["en-US"], storage = new Map(), storageThrows = false,
                   state = STATE, letter = { status: 200, json: LETTER },
                   archive = { status: 200, json: { ok: true, state: "moved" } } } = {}) {
  const ids = ["title", "home", "hint", "err", "boxes", "groups", "aliases", "switches",
               "broadcasts", "log", "langs", "h-groups", "h-aliases", "h-switches",
               "h-broadcasts", "h-log", "letter", "letter-status"];
  const els = {};
  for (const id of ids) els[id] = makeEl(id);
  const btn = (l) => ({ dataset: { lang: l }, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; } });
  els["langs"].children = [btn("zh"), btn("en")];

  const localStorage = {
    getItem: (k) => { if (storageThrows) throw new Error("denied"); return storage.has(k) ? storage.get(k) : null; },
    setItem: (k, v) => { if (storageThrows) throw new Error("denied"); storage.set(k, v); },
  };
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
      addEventListener() {}, createElement: (t) => makeEl(t),
    },
    window: { localStorage, addEventListener() {} },
    navigator: { languages: langs, language: langs[0] || "" },
    localStorage,
    fetch: async (url, opts = {}) => {
      const method = opts.method || "GET";
      calls.push({ url, method, body: opts.body || null });
      if (method === "GET" && url.startsWith("/api/state")) return reply(200, state);
      if (method === "GET" && url.startsWith("/api/letter")) {
        // `letter` 可以是单条响应，也可以是一列响应（供并发/竞态用例按请求顺序消费，支持 gate 挂起）
        const spec = Array.isArray(letter) ? (letter.shift() || { status: 200, json: {} }) : letter;
        if (spec.gate) await spec.gate;
        return reply(spec.status, spec.json);
      }
      if (method === "POST" && url.startsWith("/api/archive-one")) return reply(archive.status, archive.json);
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
  return { ctx, els, calls, confirmations, fire, storage };
}

// Clicking a pending row: the stub honours whatever selector the implementation asks for, while
// staying out of the pre-existing .clear / .toggle branches (those must not fire on a row click).
const openRow = (e, id = "A", box = "boxz") => e.fire("boxes", "click", {
  target: { closest: (s) => (s === ".clear" || s === ".toggle" ? null
    : { dataset: { box, id, file: `${id}.md` } }) },
});
const act = (e, action) => e.fire("letter", "click", {
  target: { closest: () => ({ dataset: { letterAction: action } }) },
});
const detailText = (e) => (e.els["letter"].hidden ? "" : String(e.els["letter"].innerHTML));
const visible = (e) => `${detailText(e)} ${e.els["letter-status"].textContent} ${e.els["err"].textContent}`;
const posts = (e) => e.calls.filter((c) => c.method === "POST");
const stateGets = (e) => e.calls.filter((c) => c.url.startsWith("/api/state")).length;
const switchTo = (e, which) => e.fire("langs", "click", {
  target: { closest: (s) => (s === "button[data-lang]" ? e.els["langs"].children[which] : null) },
});

// 1) Clicking a pending row opens the detail, escaped, with the fetched (not inline) content.
{
  const e = makeEnv({ langs: ["zh-Hans-CN"] }); await settle();
  hasNot(e.els["letter"].innerHTML, "alice", "打开前没有详情内容");
  openRow(e, "A"); await settle();
  const t = detailText(e);
  has(t, "信件详情", "详情标题渲染");
  has(t, "alice &amp; bob", "sender 渲染且被转义");
  has(t, "主题 &amp; &lt;script&gt;alert(1)&lt;/script&gt;", "subject 渲染且被转义");
  has(t, "处理 &amp; 回复", "need 渲染且被转义");
  has(t, "第一行", "正文第一行渲染");
  has(t, "第二行", "正文第二行渲染（多行完整）");
  has(t, "&lt;script&gt;alert(1)&lt;/script&gt;", "正文里的脚本以文本形式出现");
  hasNot(t, "<script>", "原始 <script> 绝不进入标记");
  hasNot(t, "<b>粗体</b>", "正文里的 <b> 不进入标记");
  hasNot(t, "row-from-A", "详情用的是 GET /api/letter 的内容，不是行内元数据");
}

// 2) 仅归档: exactly one POST {box, id}, then close + state refresh + 已归档.
{
  const e = makeEnv({ langs: ["zh-Hans-CN"] }); await settle();
  openRow(e, "A"); await settle();
  has(detailText(e), "第一行", "详情已打开（成功用例正面对照）");
  const before = stateGets(e);
  act(e, "done"); await settle();
  const ps = posts(e);
  is(ps.length, 1, "仅归档恰好一次写请求");
  if (ps.length === 1) {
    is(ps[0].url, "/api/archive-one", "仅归档走 /api/archive-one");
    const body = JSON.parse(ps[0].body);
    is(body.box, "boxz", "归档请求带 box");
    is(body.id, "A", "归档请求带裸 id（不含 .md）");
    is(Object.keys(body).length, 2, "归档请求体恰好只有 box 与 id");
  }
  is(detailText(e).includes("第一行"), false, "成功后详情关闭");
  is(stateGets(e), before + 1, "成功后重新拉取 /api/state");
  has(visible(e), "已归档", "成功后显示已归档");
}

// 3) 关闭 closes without a single write call.
{
  const e = makeEnv({ langs: ["zh-Hans-CN"] }); await settle();
  openRow(e, "B"); await settle();
  has(detailText(e), "第一行", "详情已打开（关闭用例正面对照）");
  act(e, "close"); await settle();
  is(detailText(e).includes("第一行"), false, "关闭后详情关闭");
  is(posts(e).length, 0, "关闭不发任何写请求");
}

// 4) Read failures speak the right message and stop there.
{
  const notFound = makeEnv({ langs: ["zh-Hans-CN"], letter: { status: 404, json: { ok: false, error: "not_found" } } });
  await settle();
  openRow(notFound, "A"); await settle();
  has(visible(notFound), "未找到", "读取 404 显示未找到");
  hasNot(visible(notFound), "读取失败", "404 不显示读取失败");
  is(posts(notFound).length, 0, "读取失败不发写请求");

  const broken = makeEnv({ langs: ["zh-Hans-CN"], letter: { status: 500, json: { ok: false, error: "boom" } } });
  await settle();
  openRow(broken, "A"); await settle();
  has(visible(broken), "读取失败", "读取 500 显示读取失败");
  hasNot(visible(broken), "未找到", "500 不显示未找到");

  const en404 = makeEnv({ langs: ["en-US"], letter: { status: 404, json: { ok: false, error: "not_found" } } });
  await settle();
  openRow(en404, "A"); await settle();
  has(visible(en404), "Not found", "英文 404 显示 Not found");

  const en500 = makeEnv({ langs: ["en-US"], letter: { status: 500, json: { ok: false, error: "boom" } } });
  await settle();
  openRow(en500, "A"); await settle();
  has(visible(en500), "Read failed", "英文 500 显示 Read failed");
}

// 5) Archive failures: detail stays open, no success message, exactly one POST.
for (const [status, error, needle] of [[409, "conflict", "冲突"], [404, "not_found", "未找到"], [500, "boom", "归档失败"]]) {
  const e = makeEnv({ langs: ["zh-Hans-CN"], archive: { status, json: { ok: false, error } } });
  await settle();
  openRow(e, "A"); await settle();
  act(e, "done"); await settle();
  has(visible(e), needle, `归档 ${status} 显示「${needle}」`);
  has(detailText(e), "第一行", `归档 ${status} 失败后详情保持打开`);
  hasNot(visible(e), "已归档", `归档 ${status} 失败不显示已归档`);
  is(posts(e).length, 1, `归档 ${status} 失败也只发一次请求`);
}

// 6) English labels render in English and never translate user content.
{
  const e = makeEnv({ langs: ["en-US"] }); await settle();
  openRow(e, "A"); await settle();
  const t = detailText(e);
  has(t, "Letter details", "英文详情标题");
  has(t, "File only", "英文仅归档按钮");
  has(t, "Close", "英文关闭按钮");
  has(t, "主题 &amp;", "英文界面里用户内容仍原样");
  hasNot(t, "信件详情", "英文界面没有中文详情标题");
  hasNot(t, "已归档", "还没归档就不显示 Filed/已归档");
}

// 7) Switching language while the detail is open closes it and writes nothing.
{
  const e = makeEnv({ langs: ["zh-Hans-CN"] }); await settle();
  openRow(e, "A"); await settle();
  has(detailText(e), "仅归档", "中文详情已打开（切换用例正面对照）");
  switchTo(e, 1); await settle();
  is(posts(e).length, 0, "切换语言不发任何写请求");
  hasNot(detailText(e), "仅归档", "切换语言后不残留旧语言按钮");
  hasNot(detailText(e), "信件详情", "切换语言后不残留旧语言标题");
  is(e.els["title"].textContent, "Post Office", "页面已经切到英文");
}

// 8) 迟到的读取不得改写更新过的视图：请求序号只在最新一次生效（关闭/切换语言同样作废在途读取）。
{
  let releaseA; const gateA = new Promise((r) => (releaseA = r));
  const e = makeEnv({ langs: ["zh-Hans-CN"], letter: [
    { status: 200, json: LETTER, gate: gateA },
    { status: 200, json: { ...LETTER, sender: "second-reader", subject: "第二封主题" } },
  ] });
  await settle();
  openRow(e, "A");                        // 挂起，等 gateA
  openRow(e, "B"); await settle();        // 后打开的 B 先返回并显示
  has(detailText(e), "second-reader", "后打开的 B 先显示");
  releaseA(); await settle();
  has(detailText(e), "second-reader", "A 的迟到响应不改写 B 的详情");
  hasNot(detailText(e), "alice", "迟到的 A 内容不得出现");

  let releaseC; const gateC = new Promise((r) => (releaseC = r));
  const f = makeEnv({ langs: ["zh-Hans-CN"], letter: [{ status: 200, json: LETTER, gate: gateC }] });
  await settle();
  openRow(f, "A");
  switchTo(f, 1); await settle();         // 切换语言：关闭详情并作废在途读取
  releaseC(); await settle();
  is(detailText(f).includes("第一行"), false, "切换语言后，迟到的读取不得把详情又打开");
  is(posts(f).length, 0, "切换语言仍不发写请求");
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
