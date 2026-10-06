// Bounded test of the panel page: runs the real inline script from panel/index.html against a
// minimal DOM / fetch / storage stub. No browser, no framework, no model call. It checks the
// language switch (default by browser language, manual choice persisted, storage unusable),
// that switching only redraws (never a write call), that our own wording is translated while
// user content (mailbox names, letter subjects, receipt notes, handoff paths, raw log lines)
// stays verbatim and escaped, and that the clear confirmation covers both new statistics.
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

// A mailbox name with a quote, a subject with markup and an ampersand, plus receipt notes and raw
// log lines — everything here must reach the page escaped and untranslated.
const STATE = {
  home: "/tmp/postoffice",
  boxes: [
    {
      name: 'dev"one', app: "OpenCode", method: "opencode_plugin", who: "开发者 <script>",
      online: true, ident: "ses_abc",
      counts: { waiting: 2, reminded: 1, failed: 1 },
      pending: [
        { file: "a.md", status: "waiting", subject: "<img src=x onerror=1>", from: "manager_a" },
        { file: "b.md", status: "waiting", subject: "第二封待投递", from: "manager_a" },
        { file: "c.md", status: "reminded", subject: "A&B 交接", from: "manager_b" },
        { file: "d.md", status: "failed", subject: "投递失败那封", from: "manager_b" },
      ],
      acks: [{ by: "manager_a", note: "收到 & 已归档" }],
    },
    { name: "empty_box", app: "手动", method: "notify", who: "", online: false, ident: "",
      counts: { waiting: 0, reminded: 0, failed: 0 }, pending: [], acks: [] },
  ],
  groups: [{ name: "codex", members: ["mgr_a", "mgr_b"], online: 1, total: 2,
             counts: { waiting: 2, reminded: 1, failed: 1 } }],
  aliases: [{ name: "boss", target: "manager_a",
              candidates: [{ name: "manager_a", online: true, known: true },
                           { name: "manager_b", online: false, known: true }],
              notify: ["manager_a"], confirmed: "manager_a", pending: "manager_b", pending_in: 42 }],
  switches: [{ text: "E1_boss_manager_a___manager_b 原因=候选上线" }],
  broadcasts: [{ id: "B1", subject: "同步进度", from: "manager_a", acked: 1, total: 2,
                 deadline: "2026-10-05 12:00", summarized: false, error: "" }],
  config: "",
  config_error: "",
  log: [{ t: "2026-10-05 04:30:01", src: "opencode_plugin", msg: "通知人：原始内容 keep & 原样" }],
};

function makeEl(id) {
  return {
    id, textContent: "", innerHTML: "", value: "", dataset: {}, attrs: {}, children: [],
    handlers: {}, style: {}, hidden: false, className: "",
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
  };
}

function makeEnv({ langs = ["en-US"], storage = new Map(), storageThrows = false, fetchFails = false, state = STATE,
                   letter = null, archive = null } = {}) {
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
      calls.push({ url, method: opts.method || "GET", body: opts.body || null });
      if (fetchFails) throw new Error("offline");
      if (letter && url.startsWith("/api/letter")) {
        return { ok: letter.status >= 200 && letter.status < 300, status: letter.status, json: async () => letter.json };
      }
      if (archive && url.startsWith("/api/archive-one")) {
        return { ok: archive.status >= 200 && archive.status < 300, status: archive.status, json: async () => archive.json };
      }
      if ((opts.method || "GET") !== "GET") return { ok: true, status: 200, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(state)) };
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
const settle = () => new Promise((r) => setTimeout(r, 0));

const target = (sel, props) => ({ closest: (s) => (s === sel ? props : null) });

// 1) Default language follows the browser: zh* → Chinese, anything else → English.
{
  const a = makeEnv({ langs: ["zh-Hans-CN"] }); await settle();
  is(a.els["title"].textContent, "联络总站", "zh 浏览器默认中文");
  is(a.els["h-groups"].textContent, "分组与逻辑地址", "zh 下分组标题是中文");
  const b = makeEnv({ langs: ["en-GB", "zh"] }); await settle();
  is(b.els["title"].textContent, "Post Office", "非 zh 浏览器默认英文（含 zh 在第二顺位）");
  is(b.els["h-groups"].textContent, "Groups and logical addresses", "en 下分组标题是英文");
}

// 2) A manual choice switches every visible string and only redraws — no write call, no wake-up.
{
  const e = makeEnv({ langs: ["en-US"] }); await settle();
  has(e.els["boxes"].innerHTML, "2 waiting / 1 reminded to file", "英文下数量文案正确");
  has(e.els["boxes"].innerHTML, ">reminded, to file<", "英文下逐封状态文案正确");
  const writesBefore = e.calls.filter((c) => c.method !== "GET").length;
  e.fire("langs", "click", { target: { closest: (s) => (s === "button[data-lang]" ? e.els["langs"].children[0] : null) } });
  await settle();
  is(e.els["title"].textContent, "联络总站", "点中文后标题变中文");
  is(e.els["h-log"].textContent, "最近投递记录", "点中文后日志标题变中文");
  is(e.els["langs"].children[0].getAttribute("aria-pressed"), "true", "中文按钮 aria-pressed=true");
  is(e.els["langs"].children[1].getAttribute("aria-pressed"), "false", "英文按钮 aria-pressed=false");
  is(e.els["langs"].getAttribute("aria-label"), "切换界面语言", "语言组 aria-label 随语言变");
  is(e.calls.filter((c) => c.method !== "GET").length, writesBefore, "切换语言不调用任何写接口");
  is(e.calls.filter((c) => c.url === "/api/state").length, 2, "切换语言只重新读状态");
  has(e.els["boxes"].innerHTML, "待投递 2 / 已提醒待归档 1", "中文下数量文案正确");
  e.fire("langs", "click", { target: { closest: (s) => (s === "button[data-lang]" ? e.els["langs"].children[1] : null) } });
  await settle();
  is(e.els["title"].textContent, "Post Office", "可切回英文");
}

// 3) The manual choice survives a reload; an unusable storage must not break switching.
{
  const storage = new Map();
  const first = makeEnv({ langs: ["en-US"], storage }); await settle();
  first.fire("langs", "click", { target: { closest: (s) => (s === "button[data-lang]" ? first.els["langs"].children[0] : null) } });
  await settle();
  const again = makeEnv({ langs: ["en-US"], storage }); await settle();
  is(again.els["title"].textContent, "联络总站", "重载后仍用上次选的中文");
  is(storage.get("postoffice.lang"), "zh", "选择写进了 localStorage");
  const broken = makeEnv({ langs: ["en-US"], storageThrows: true }); await settle();
  is(broken.els["title"].textContent, "Post Office", "存储不可用时仍按浏览器语言出英文");
  broken.fire("langs", "click", { target: { closest: (s) => (s === "button[data-lang]" ? broken.els["langs"].children[0] : null) } });
  await settle();
  is(broken.els["title"].textContent, "联络总站", "存储不可用时手动切换仍然有效");
}

// 4) User content is shown verbatim (escaped, never translated); our wording is translated.
{
  const e = makeEnv({ langs: ["zh-Hans-CN"] }); await settle();
  const b = e.els["boxes"].innerHTML;
  has(b, "dev&quot;one", "信箱名里的引号被转义");
  hasNot(b, 'dev"one', "信箱名原样引号没有漏进 HTML");
  has(b, "&lt;img src=x onerror=1&gt;", "信件标题里的标记被转义");
  hasNot(b, "<img src=x", "信件标题原样标记没有漏进 HTML");
  has(b, "A&amp;B 交接", "标题里的 & 被转义");
  has(b, "开发者 &lt;script&gt;", "who 字段也被转义");
  has(b, "收到 &amp; 已归档", "回执正文原样且转义");
  hasNot(b, "Waiting", "英文状态词不出现在中文界面");
  has(e.els["log"].innerHTML, "通知人：原始内容 keep &amp; 原样", "原始投递记录保持原文并转义");
  has(e.els["switches"].innerHTML, "E1_boss_manager_a___manager_b 原因=候选上线", "切换记录保持原文");
  const a = makeEnv({ langs: ["en-US"] }); await settle();
  hasNot(a.els["log"].innerHTML, "Delivery log lines stay", "英文界面也不翻译原始日志");
  has(a.els["log"].innerHTML, "通知人：原始内容", "英文界面下原始日志仍是原文");
}

// 5) The clear confirmation covers all three statistics and the total, follows the language,
//    and archive stays a POST. The failed count must be in there: clear archives the whole inbox.
{
  for (const [l, index, needles, other] of [
    ["zh-Hans-CN", 0, ["全部 4 封信件和通知", "2 封待投递", "1 封已提醒未归档",
                      "1 封投递失败需人工处理", "收件箱"], "2 waiting"],
    ["en-US", 1, ["all 4 letters and notifications", "2 waiting", "1 reminded but not filed",
                  "1 failed delivery needing a human", "inbox"], "2 封待投递"],
  ]) {
    const e = makeEnv({ langs: [l] }); await settle();
    e.fire("boxes", "click", { target: target(".clear", { dataset: { clear: "dev_one", w: "2", r: "1",
                                                                    f: "1", n: "4" } }) });
    await settle();
    is(e.confirmations.length, 1, `${l} 归档前有一次确认`);
    for (const needle of needles) has(e.confirmations[0], needle, `${l} 确认文案含「${needle}」`);
    hasNot(e.confirmations[0], other, `${l} 确认文案不含另一种语言`);
    const post = e.calls.filter((c) => c.method === "POST");
    is(post.length, 1, `${l} 归档只发一次写请求`);
    is(post[0].url, "/api/clear", `${l} 归档走 /api/clear`);
  }
  // The button must pass the real counts from the snapshot, including failed.
  const btn = makeEnv({ langs: ["en-US"] }); await settle();
  has(btn.els["boxes"].innerHTML, 'data-f="1"', "归档按钮带上投递失败数量");
  has(btn.els["boxes"].innerHTML, 'data-n="4"', "归档按钮带上收件箱总数");
  const none = makeEnv({ langs: ["en-US"] }); await settle();
  is((none.els["boxes"].innerHTML.match(/data-clear=/g) || []).length, 1,
     "只有有信的信箱显示归档按钮（空信箱不给按钮）");
}

// 6) Empty list, group switch and transport failure all speak the current language.
{
  const e = makeEnv({ langs: ["en-US"] }); await settle();
  has(e.els["groups"].innerHTML, "All on", "英文下分组按钮是英文");
  e.fire("groups", "click", { target: target("button[data-group]", { dataset: { group: "codex", status: "offline" } }) });
  await settle();
  const post = e.calls.filter((c) => c.method === "POST");
  is(post.length, 1, "分组按钮发一次写请求");
  is(post[0].url, "/api/status", "分组按钮走 /api/status");
  is(JSON.parse(post[0].body).group, "codex", "分组按钮带组名");
  is(JSON.parse(post[0].body).status, "offline", "分组按钮带目标状态");
  const blank = { ...STATE, switches: [], log: [], boxes: [], groups: [], aliases: [], broadcasts: [] };
  const z = makeEnv({ langs: ["zh-Hans-CN"], state: blank }); await settle();
  has(z.els["switches"].innerHTML, "还没有确认过切换", "中文空状态文案");
  has(z.els["log"].innerHTML, "还没有记录", "中文下投递记录空状态");
  has(z.els["boxes"].innerHTML, "通讯录是空的", "中文下无信箱空状态");
  has(z.els["aliases"].innerHTML, "没有配置逻辑地址", "中文下无逻辑地址空状态");
  const blankEn = makeEnv({ langs: ["en-US"], state: blank }); await settle();
  has(blankEn.els["switches"].innerHTML, "No confirmed switch yet", "英文空状态文案");
  has(blankEn.els["boxes"].innerHTML, "The address book is empty", "英文下无信箱空状态");
  has(blankEn.els["groups"].innerHTML, "No groups configured", "英文下无分组空状态");
  const down = makeEnv({ langs: ["en-US"], fetchFails: true }); await settle();
  is(down.els["err"].textContent, "Cannot reach the panel service (is postoffice panel still running?)",
     "英文下连接失败提示");
  const down2 = makeEnv({ langs: ["zh-Hans-CN"], fetchFails: true }); await settle();
  is(down2.els["err"].textContent, "连不上面板服务（postoffice panel 是否还开着？）", "中文下连接失败提示");
}

// 7) The letter-detail dialog: every new string exists in BOTH languages, and the dialog renders
//    in the language the page is in while user content (sender/subject/need/body) stays verbatim
//    and escaped.
{
  const zhDialog = ["信件详情", "处理完成", "关闭", "已处理", "未找到", "读取失败", "冲突", "归档失败"];
  const enDialog = ["Letter details", "File as done", "Close", "Filed", "Not found", "Read failed",
                    "Conflict", "Archive failed"];
  for (const s of zhDialog) has(html, s, `面板源码包含中文文案「${s}」`);
  for (const s of enDialog) has(html, s, `面板源码包含英文文案「${s}」`);

  const DIALOG_LETTER = { ok: true, id: "a", box: 'dev"one', sender: "manager_a",
                          subject: "第二封待投递", need: "仅告知",
                          body: "第一行 & <script>alert(1)</script>\n第二行" };
  const openRow = (e) => e.fire("boxes", "click", {
    target: { closest: (s) => (s === ".clear" || s === ".toggle" ? null
      : { dataset: { box: 'dev"one', id: "a", file: "a.md" } }) },
  });

  const zh = makeEnv({ langs: ["zh-Hans-CN"], letter: { status: 200, json: DIALOG_LETTER } }); await settle();
  openRow(zh); await settle();
  has(zh.els["letter"].innerHTML, "信件详情", "中文详情标题渲染");
  has(zh.els["letter"].innerHTML, "处理完成", "中文处理完成按钮渲染");
  has(zh.els["letter"].innerHTML, "关闭", "中文关闭按钮渲染");
  has(zh.els["letter"].innerHTML, "第二封待投递", "中文界面下用户内容原样");
  hasNot(zh.els["letter"].innerHTML, "<script>alert(1)</script>", "详情正文里的脚本被转义");

  const en = makeEnv({ langs: ["en-US"], letter: { status: 200, json: DIALOG_LETTER } }); await settle();
  openRow(en); await settle();
  has(en.els["letter"].innerHTML, "Letter details", "英文详情标题渲染");
  has(en.els["letter"].innerHTML, "File as done", "英文处理完成按钮渲染");
  has(en.els["letter"].innerHTML, "Close", "英文关闭按钮渲染");
  has(en.els["letter"].innerHTML, "第二封待投递", "英文界面下用户内容仍原样");
  hasNot(en.els["letter"].innerHTML, "信件详情", "英文界面下没有中文详情标题");
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);