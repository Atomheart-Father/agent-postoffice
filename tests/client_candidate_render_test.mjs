// The client on the REAL adapter (HTTP), fed the candidate's anonymous fixture through a fake fetch, under Node (no browser).
// Fixture-verified only: e819f0e itself was not available. What it proves: the default adapter is the real one and mock is opt-in
// and labelled; roles and mailboxes are different things on screen; what the backend does not have shows "capability missing" and
// never a mock screen; the role-edit flow runs draft -> preview -> confirm -> result against the pack's answers, with the pack's
// exact failure shapes (pending projection, stale 409, lost reply) handled as the task demands.
import { readFileSync } from "node:fs";
import { createApp } from "../client/src/app.js";
import { createHttpAdapter } from "../client/src/adapters/http.js";
import { chooseAdapter } from "../client/src/adapters/index.js";
import { setLang, missing } from "../client/src/i18n.js";

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const is = (g, w, m) => (JSON.stringify(g) === JSON.stringify(w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));
const hasS = (h, n, m) => (String(h).includes(n) ? ok(m) : bad(m, `missing ${JSON.stringify(n)}`));
const hasNot = (h, n, m) => (String(h).includes(n) ? bad(m, `unexpected ${JSON.stringify(n)}`) : ok(m));

const F = (f) => JSON.parse(readFileSync(new URL("./fixtures/office-e819/" + f, import.meta.url), "utf8"));
const state = F("state_ok.json"), diffBody = F("role_diff_ok_response.json").body;
const cases = F("http_cases.json").cases; const resp = (n) => JSON.parse(cases.find((c) => c.name === n).body_resp);

// a fake backend: routes -> [status, body | function]; every request is recorded
function backend(routes) {
  const log = [];
  const fetchImpl = async (url, opt = {}) => {
    const path = url.split("?")[0]; log.push({ path, method: opt.method, body: opt.body ? JSON.parse(opt.body) : undefined });
    const r = routes[path]; if (!r) return { ok: false, status: 404, text: async () => '{"ok":false,"error":"not_found"}' };
    const [status, body] = typeof r === "function" ? r() : r;
    if (body === "LOST") { const e = new Error("t"); e.name = "AbortError"; throw e; }
    return { ok: status < 300, status, text: async () => (typeof body === "string" ? body : JSON.stringify(body)) };
  };
  return { fetchImpl, log };
}
function boot(routes, { hash = "", width = 1440 } = {}) {
  const root = { innerHTML: "", querySelectorAll() { return []; }, querySelector() { return null; }, contains() { return false; } };
  const win = { location: { hash }, innerWidth: width, setTimeout: () => 0, clearTimeout() {} };
  const b = backend(routes);
  const adapter = createHttpAdapter({ fetchImpl: b.fetchImpl, timeoutMs: 300, writeTimeoutMs: 300 });
  const app = createApp({ root, toastRoot: { textContent: "" }, adapter, win, doc: null });
  return { app, root, S: app.S, log: b.log, adapter };
}
const inp = (app) => (field, value) => app.input({ closest: () => ({ dataset: { edit: field }, value }) }, false);

for (const lang of ["en", "zh"]) {
  setLang(lang);
  const { app, root, S } = boot({ "/api/state": [200, state] });
  await app.load();
  for (const id of ["office.design", "office.worker", "office.missing", "box:box_a", "box:box_unlisted", "box:panelop", "team:unassigned"]) for (const tab of ["overview", "threads", "work", "edit"]) { S.view = "office"; S.roleId = id; S.roleTab = tab; app.render(); }
  for (const v of ["threads", "work"]) { S.view = v; S.roleId = null; app.render(); }
  S.dialog = "caps"; app.render();
}
is([...missing].join(","), "", "no wording key is missing on the real adapter, in either language");
setLang("en");

// ---- the default is the real adapter; mock is opt-in ----
is([chooseAdapter("").kind, chooseAdapter("?foo=1").kind, chooseAdapter("?backend=http").kind, chooseAdapter("?backend=mock").kind], ["http", "http", "http", "mock"], "no flag = the real backend on this origin; only ?backend=mock gives sample data");
is(chooseAdapter("?candidate=1").kind, "http", "the old ?candidate=1 switch is gone: the backend is recognised by what it sends");

// ---- the screen ----
{
  const { app, root, S } = boot({ "/api/state": [200, state] }, { hash: "#/office" });
  await app.load(); const h = root.innerHTML;
  hasNot(h, "Not wired", "no mock label on a real backend's office");
  hasS(h, "匿名设计岗", "a role is drawn by its title"); hasS(h, "Mailboxes", "mailboxes are a separate group"); hasS(h, ">Roles<", "…beside the roles");
  hasS(h, "status unknown", "with no activity signal the column says so, instead of 'connected 0'");
  hasNot(h, 'class="pw">Connected<', "no seat claims to be connected");
  S.view = "office"; S.roleId = "office.design"; S.roleTab = "overview"; app.render(); const o = root.innerHTML;
  hasS(o, "拟契约样例", "duties are drawn"); hasS(o, "path not found", "a resource whose path does not exist says so"); hasNot(o, "&lt;HOME&gt;", "…without showing a local path");
  hasS(o, "online_notify", "the backend's state code is shown as written"); hasS(o, "Delivery switch: on", "the delivery switch is its own fact"); hasS(o, "匿名执行岗", "the escalation role's title is shown");
  hasS(o, "resolves to", "…and the mailbox it resolves to"); hasS(o, "Config check", "the card's config check is shown"); hasS(o, "unknown", "confirmed null is shown as unknown");
}
// the box page shows the mailbox facts only (no role invented)
{
  const { app, root, S } = boot({ "/api/state": [200, state] }, { hash: "" });
  await app.load(); S.view = "office"; S.roleId = "box:box_a"; S.roleTab = "overview"; app.render();
  hasS(root.innerHTML, "stands for one mailbox", "a mailbox node says it is a mailbox, not a position");
  S.roleTab = "work"; app.render(); hasS(root.innerHTML, "Waiting in mailbox", "its waiting letter keeps its state"); hasS(root.innerHTML, "Reminded, not consumed", "…and the reminded one its own");
  S.roleTab = "edit"; app.render(); hasS(root.innerHTML, "drawn from a mailbox", "it cannot be edited as a role");
}

// ---- what has no backend says so, and shows no sample screen ----
{
  const { app, root, S } = boot({ "/api/state": [200, state] });
  await app.load();
  S.view = "threads"; app.render(); hasS(root.innerHTML, "does not offer threads", "threads: capability missing"); hasNot(root.innerHTML, "Not wired", "…not a mock screen");
  S.view = "work"; app.render(); hasS(root.innerHTML, "Not offered", "work / approvals: capability missing"); hasNot(root.innerHTML, 'data-act="approve"', "…no approve button"); hasNot(root.innerHTML, 'data-act="deny"', "…no deny button");
  S.view = "office"; S.roleId = "office.design"; S.roleTab = "threads"; app.render(); hasNot(root.innerHTML, 'data-act="newthread"', "no 'new thread' button on a backend that cannot start one");
  S.dialog = "caps"; app.render(); hasS(root.innerHTML, "Wired, fixture-checked only", "the capability table separates fixture-checked from live");
}

// ---- the role-edit flow against the pack's answers ----
const routes = (apply) => ({ "/api/state": [200, state], "/api/role-diff": [200, diffBody], "/api/role-apply": apply });
async function toPreview(apply) {
  const t = boot(routes(apply), { hash: "#/office/office.design/edit" }); await t.app.load();
  inp(t.app)("duties", "拟契约样例\n再来一条"); await t.app.act("ed-preview"); return t;
}
{
  const t = await toPreview([200, resp("role_apply_fresh")]); const h = t.root.innerHTML;
  is(t.S.edits["office.design"].phase, "preview", "draft -> preview");
  hasS(h, "roleNoteTiming", "the backend's note code is shown as written"); hasS(h, "Holder will change", "expected is shown key by key"); hasS(h, "w1 (online), w2 (online)", "facts list the candidates' states");
  is(t.log.filter((l) => l.path === "/api/role-diff").map((l) => Object.keys(l.body)), [["name", "candidates", "duties"]], "role-diff was sent once, with only the changed field besides name and candidates");
  is(t.log.some((l) => l.path === "/api/role-apply"), false, "nothing was applied by previewing");
  hasS(h, 'data-act="ed-apply"', "applying is a separate, explicit step");
  await t.app.act("ed-apply"); is(t.S.edits["office.design"].phase, "done", "confirm -> result"); hasS(t.root.innerHTML, "Derived cards are fresh", "projection fresh is reported");
  const ap = t.log.filter((l) => l.path === "/api/role-apply"); is(ap.length, 1, "apply was sent once"); is(ap[0].body.baseline, "<sha256 of config bytes, 64 hex>", "…with the baseline the preview returned");
  await t.app.act("ed-refresh"); await t.app.act("ed-apply"); is(t.log.filter((l) => l.path === "/api/role-apply").length, 1, "nothing sends a second apply");
}
{
  const t = await toPreview([200, resp("role_apply_pending")]); await t.app.act("ed-apply"); const h = t.root.innerHTML;
  is(t.S.edits["office.design"].phase, "done", "saved + projection pending ends as done, not as an error");
  hasS(h, "Derived cards are pending", "…and says the cards are still to refresh"); hasS(h, "Do not apply again", "…with the instruction not to send it again"); hasNot(h, 'data-act="ed-apply"', "…and no apply button");
}
{
  const t = await toPreview([200, { ok: true, saved: true, projection: "weird", message: "m" }]); await t.app.act("ed-apply"); const h = t.root.innerHTML;
  is(t.S.edits["office.design"].phase, "done", "saved + unknown projection ends as done"); hasS(h, "projection state is unknown", "…and says the projection state is unknown");
}
{
  const t = await toPreview([409, resp("role_apply_409_stale")]); await t.app.act("ed-apply"); const h = t.root.innerHTML;
  is(t.S.edits["office.design"].phase, "error", "409 -> error phase"); hasS(h, "nothing was saved", "…nothing was saved"); hasS(h, 'data-act="ed-rebase"', "…the draft is kept and can be previewed against the latest state");
  is(t.S.edits["office.design"].text.duties, "拟契约样例\n再来一条", "…the draft text is kept"); is(t.log.filter((l) => l.path === "/api/role-apply").length, 1, "…and nothing was resent");
}
{
  const t = await toPreview([200, "LOST"]); await t.app.act("ed-apply"); const h = t.root.innerHTML;
  is(t.S.edits["office.design"].phase, "error", "a lost reply -> error phase"); hasS(h, "not known whether the apply took effect", "…outcome unknown"); hasS(h, 'data-act="ed-preview"', "…the way forward is a fresh preview");
  hasNot(h, 'data-act="ed-apply"', "…there is no apply button"); hasNot(h, 'data-act="ed-retry"', "…and no retry"); is(t.S.edits["office.design"].text.duties, "拟契约样例\n再来一条", "…the draft is kept"); await t.app.act("ed-back"); hasS(t.root.innerHTML, "再来一条", "…and \"keep editing\" shows it again");
  is(t.log.filter((l) => l.path === "/api/role-apply").length, 1, "…the apply was sent once");
  await t.app.act("ed-refresh"); is(t.log.filter((l) => l.path === "/api/role-apply").length, 1, "refreshing does not resend it");
  await t.app.act("ed-preview"); is(t.S.edits["office.design"].phase, "preview", "preview again -> a new preview, awaiting an explicit confirm");
  is(t.log.filter((l) => l.path === "/api/role-apply").length, 1, "…which still applied nothing");
}
{
  const t = await toPreview([500, resp("role_apply_500_backup_failed")]); await t.app.act("ed-apply");
  hasS(t.root.innerHTML, "无法创建配置备份", "a 500 shows the backend's reason"); hasS(t.root.innerHTML, "nothing was saved", "…and that nothing was saved");
}
{
  const t = boot({ "/api/state": [200, state], "/api/role-diff": [403, { ok: false }] }, { hash: "#/office/office.design/edit" }); await t.app.load();
  inp(t.app)("duties", "x"); await t.app.act("ed-preview");
  hasS(t.root.innerHTML, "Host / Origin / Content-Type protection", "403 {ok:false} is explained as the documented protection"); hasS(t.root.innerHTML, "there is no token to add", "…and says there is no token");
  hasS(t.root.innerHTML, "The read model carries no edit right", "the read model's silence about rights is said, not guessed");
}
{
  const t = boot({ "/api/state": [200, state], "/api/role-diff": [403, { ok: false, error: "scope" }] }, { hash: "#/office/office.design/edit" }); await t.app.load();
  inp(t.app)("duties", "x"); await t.app.act("ed-preview");
  hasS(t.root.innerHTML, "scope", "an unknown 403 is shown as written"); hasNot(t.root.innerHTML, "Host / Origin / Content-Type protection", "…and not explained as the guard");
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
