// v1.13 §21 mobile / iPad portrait probe — REAL browser layout, not a DOM stub.
//
// Opt-in (not part of smoke): needs Playwright + a Chromium-family browser.
//   PANEL_URL=http://127.0.0.1:8765 NODE_PATH=<playwright node_modules> node tests/layout_probe.mjs
//
// 覆盖 §21 点名的全部表面：top rail / Organization / Harness / Operator Inbox+Outbox / workbench /
// letter / outbox edit / compose / recipient dropdown / Activity drawer / Appearance。
// 每个必须出现的表面都要真的打开（isVisible）；打不开就是 FAIL，绝不计成 skip。
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require("playwright")); }
catch { console.error("SKIP: playwright 不可用（设 NODE_PATH 指向含 playwright 的 node_modules）"); process.exit(0); }

const URL = process.env.PANEL_URL || "http://127.0.0.1:8765/";
const WIDTHS = (process.env.PW_WIDTHS || "390,430,768,820").split(",").map((s) => parseInt(s, 10));
const CHANNEL = process.env.PW_CHANNEL || "";   // 空 = 用 Playwright 自带 chromium；设了才用该 channel

const measure = (page) => page.evaluate(() => {
  const de = document.documentElement;
  const desc = (el) => el.tagName.toLowerCase() + (el.id ? "#" + el.id : "")
    + (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : "");
  const vw = window.innerWidth;
  const culprits = [];
  for (const el of document.querySelectorAll("*")) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const overR = Math.round(r.right - vw), overL = Math.round(-r.left);
    if (overR > 1 || overL > 1)
      culprits.push({ d: desc(el), overR, overL, w: Math.round(r.width), pos: getComputedStyle(el).position });
  }
  culprits.sort((a, b) => Math.max(b.overR, b.overL) - Math.max(a.overR, a.overL));
  const all = [...document.querySelectorAll("*")].map((el) => ({ el, over: el.scrollWidth - el.clientWidth }));
  const worst = all.sort((a, b) => b.over - a.over)[0];
  return { docOver: de.scrollWidth - de.clientWidth, bodyOver: document.body.scrollWidth - document.body.clientWidth,
           worstOver: worst ? worst.over : 0, worstEl: worst ? desc(worst.el) : "", viewport: vw,
           culprits: culprits.slice(0, 3) };
});

let browser;
try { browser = await chromium.launch(CHANNEL ? { channel: CHANNEL } : {}); }
catch (e) { console.error(`SKIP: 打不开 ${CHANNEL || "chromium"}（${e.message.split("\n")[0]}）`); process.exit(0); }

let overflows = 0, missing = 0;
const vis = (page, sel) => page.locator(sel).first().isVisible().catch(() => false);
const clickIf = async (page, sel) => {
  const l = page.locator(sel).first();
  if (await l.count()) await l.click({ timeout: 4000 }).catch(() => {});
  await page.waitForTimeout(180);
};

for (const width of WIDTHS) {
  const ctx = await browser.newContext({ viewport: { width, height: 844 }, deviceScaleFactor: 2,
    isMobile: width <= 430, hasTouch: width <= 820 });
  const page = await ctx.newPage();
  page.setDefaultTimeout(5000);
  await page.goto(URL, { waitUntil: "load" });
  await page.waitForTimeout(400);

  const rows = [];
  const record = async (label, requiredSel) => {
    const m = await measure(page);
    const okOpen = requiredSel ? await vis(page, requiredSel) : true;
    if (!okOpen) { missing++; console.log(`  MISSING ${width}px ${label}: 必需表面 ${requiredSel} 未出现`); }
    rows.push([label, m, okOpen]);
  };

  await record("top-rail+Organization", "#boxes");           // 默认：顶栏 + 组织视图
  await clickIf(page, "#tab-hk");  await record("Harness", "#view-hk");
  await clickIf(page, "#tab-org");
  await clickIf(page, "#op-shortcut"); await record("workbench+Inbox", "#ins");
  await clickIf(page, "#ins-tab-outbox"); await record("OperatorOutbox", "#outbox");
  await clickIf(page, "#outbox .obrow .sj"); await record("letter", "#letter");
  await clickIf(page, "[data-edit-close]");
  await clickIf(page, '[data-outbox-action="edit"]'); await record("outbox-edit", "#edit-subject");
  await clickIf(page, "[data-edit-close]");
  await clickIf(page, ".close-x[data-ins-close]");   // 关掉工作台弹窗：它盖住整个页面，挡住 compose/activity
  await clickIf(page, "#btn-compose"); await record("compose", "#compose");
  const to = page.locator("#compose-to").first();
  if (await to.count()) { await to.click().catch(() => {}); await to.type("le", { delay: 20 }).catch(() => {}); await page.waitForTimeout(200); }
  await record("recipient-dropdown", "#compose-sug");
  await clickIf(page, "#compose-cancel");
  await clickIf(page, "#btn-activity"); await record("Activity-drawer", "#drawer.open");
  await clickIf(page, "#drawer-x");
  await clickIf(page, '[data-theme-choice="night"]'); await record("Appearance", "#theme");

  for (const [label, m, okOpen] of rows) {
    const over = Math.max(m.docOver, m.bodyOver);
    if (over > 1) overflows++;
    console.log(`${over > 1 ? "OVER" : "ok  "}  ${width}px  ${String(label).padEnd(22)}  docOver=${m.docOver} bodyOver=${m.bodyOver} worst=${m.worstOver} (${m.worstEl})${okOpen ? "" : "  [MISSING]"}`);
    for (const c of m.culprits || []) console.log(`        ↳ ${c.d} w=${c.w} pos=${c.pos} overR=${c.overR} overL=${c.overL}`);
  }
  await ctx.close();
}
await browser.close();
const ok = overflows === 0 && missing === 0;
console.log(`${ok ? "PASS" : "FAIL"} horizontal-overflow=${overflows} surface-missing=${missing} at ${WIDTHS.join("/")}`);
process.exit(ok ? 0 : 1);
