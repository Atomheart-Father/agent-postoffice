// The local candidate e819f0e, as far as it can be known without running it: the contract pack at
// Atomheart-Father/agent-postoffice@9cd53d5 (docs/cloud-client-api-e819) and its five anonymous fixtures, copied unchanged into
// tests/fixtures/office-e819 (SOURCE.txt lists their sha256). Everything here is FIXTURE-verified: the candidate itself was not
// available, so nothing in this file says the client works against a running e819f0e.
//
// What it checks: the fixtures are the pack's bytes; /api/state maps to roles that are not mailboxes; role-diff's real shape is
// understood; every documented HTTP answer (400/403/404/409/500, projection fresh/pending, the 403 {"ok":false} guard) lands in
// the right client state; and the requests the client sends have exactly the shape the pack demands.
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { mapMasterState, createHttpAdapter, GUARD } from "../client/src/adapters/http.js";
import { normalizeDiff, normalizeApplied, normalizeLetter, normalizeOutboxRow, ERR } from "../client/src/contract.js";
import { editBlocker, baseOf, startEdit, buildRequest, reduce } from "../client/src/flows/roleEdit.js";

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const is = (g, w, m) => (JSON.stringify(g) === JSON.stringify(w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));

const DIR = new URL("./fixtures/office-e819/", import.meta.url);
const raw = (f) => readFileSync(new URL(f, DIR));
const json = (f) => JSON.parse(raw(f).toString("utf8"));

// ---- the fixtures are the pack's bytes ----
const SUMS = readFileSync(new URL("SOURCE.txt", DIR), "utf8").split("\n").map((l) => l.match(/^([0-9a-f]{64})  (\S+\.json)$/)).filter(Boolean);
is(SUMS.length, 5, "SOURCE.txt lists five fixtures");
for (const [, sum, file] of SUMS) is(createHash("sha256").update(raw(file)).digest("hex"), sum, `${file} has the pack's sha256`);
is(readFileSync(new URL("SOURCE.txt", DIR), "utf8").includes("9cd53d5"), true, "SOURCE.txt names the contract commit 9cd53d5");

// ---- /api/state -> read model ----
const state = json("state_ok.json");
const snap = mapMasterState(state, 1791863800);
const byId = (id) => snap.roles.find((r) => r.id === id);
is(snap.source, "candidate", "role_cards present: the backend is recognised as the candidate, by content and not by a flag");
is(mapMasterState({ ...state, role_cards: undefined }, 1).source, "master", "no role_cards: it is master");
const roles = snap.roles.filter((r) => r.origin === "role" && r.kind === "position");
is(roles.map((r) => r.id), ["office.design", "office.worker", "office.missing"], "roles come from role_cards, by their logical name");
is(snap.roles.filter((r) => r.origin === "mailbox" && r.kind === "position").map((r) => r.id).sort(), ["box:box_a", "box:box_b", "box:box_c", "box:box_unlisted", "box:panelop"], "mailboxes are separate nodes, one per registered mailbox");
is(roles.some((r) => r.id.startsWith("box:")), false, "a role is never a mailbox node");
is(byId("box:box_unlisted").parentId, "team:unassigned", "a registered mailbox the organisation tree forgot is listed as unassigned");
is(byId("team:unassigned").parentId, "team:mailboxes", "…under the mailboxes group, not among the roles");
is(byId("office.design").parentId, "team:project:anonproj", "roles are grouped by their project");
const d = byId("office.design");
is([d.title, d.duties, d.roleKind, d.project, d.configOk, d.confirmed], ["匿名设计岗", ["拟契约样例"], "execution", "anonproj", true, null], "role card fields are mapped as written (confirmed null stays null)");
is(d.resources, [{ kind: "doc", ref: "pack/契约", note: "", exists: false }], "a resource keeps whether its path exists, and the local path is dropped");
is(JSON.stringify(d).includes("<HOME>"), false, "no local path reaches the read model");
is([d.escalationRole, d.escalationTitle, d.escalationTarget, d.escalationAcceptor], ["office.worker", "匿名执行岗", "box_b", ""], "escalation role, title, target and acceptor are four separate raw fields");
is(d.statusFile, { pathExists: true, updated: "2026-10-13 04:56" }, "the status file's freshness is carried");
is([d.incumbent.id, d.incumbent.state, d.holderState], ["box_a", "online_notify", "online_notify"], "the holder is looked up by the card's `active`; the backend's state code is kept as written");
is([d.incumbent.switch, d.incumbent.binding, d.incumbent.presence, d.incumbent.activity], ["on", null, "unknown", null], "online (switch), binding, presence and activity stay four separate facts; activity unknown is unknown, not idle");
is(d.incumbent.presence === "connected", false, "online is not connected");
is(byId("office.worker").escalationRole, "", "no escalation is an empty string, which is a value and not 'missing'");
is(snap.mail.map((m) => [m.box, m.delivery, m.sourceStatus]).sort(), [["box_a", "reminded", "reminded"], ["box_a", "waiting", "waiting"], ["box_c", "failed", "failed"]], "letters keep waiting / reminded / failed, per mailbox");
is(snap.mail.every((m) => m.roleId === ""), true, "no letter is attributed to a role: the backend does not record it");
is(snap.mail.find((m) => m.box === "box_a" && m.delivery === "waiting").letterId, "20261013-044210_panelop_样例待唤醒", "the letter id is the file name without .md (still raw UTF-8: encoding happens at the request)");
is(snap.company.operatorBox, "panelop", "the operator is panel.operator");
is(snap.addresses.filter((a) => a.kind === "alias").map((a) => a.name), ["@office.design", "@office.worker", "@office.missing"], "logical addresses are offered as @name");
is(snap.threads.length + snap.work.length + snap.approvals.length, 0, "no thread, work or approval is invented"); is(snap.usage, null, "usage is unknown, not 0");
is(["thread.list", "thread.control", "work.state", "approvals", "usage.hud"].map((k) => snap.capabilities[k].status), ["missing", "missing", "missing", "missing", "missing"], "what the pack lists as missing is reported missing");
is(["mail.send", "mail.ack", "mail.archive", "mail.outbox", "mail.read", "role.edit", "role.detail"].map((k) => snap.capabilities[k].status), ["fixture", "fixture", "fixture", "fixture", "fixture", "fixture", "fixture"], "candidate capabilities are 'fixture', never 'live'");
is(editBlocker(d), "", "a role card with candidates and an escalation string is editable"); is(editBlocker(byId("box:box_a")), "mailbox", "a mailbox node is not");
is(d.can, {}, "no can.* is invented: the read model says nothing about rights");
is(snap.consistency, "consistent", "a state with a valid tree is consistent");
// ride: counted by the backend, meaning not in the pack
const withRide = JSON.parse(JSON.stringify(state)); withRide.boxes[1].pending.push({ file: "r.md", status: "ride", subject: "x", from: "p", mtime: 1 });
is(mapMasterState(withRide, 1).mail.find((m) => m.id === "box_a/r.md").delivery, "unknown", "an undocumented pending status (ride) is unknown, with the source word kept");
is(mapMasterState(withRide, 1).mail.find((m) => m.id === "box_a/r.md").sourceStatus, "ride", "…and shown as 'ride'");
// a role whose holder is not a registered mailbox
const ghost = JSON.parse(JSON.stringify(state)); ghost.role_cards[0].active = "nobody";
const g = mapMasterState(ghost, 1).roles.find((r) => r.id === "office.design");
is([g.incumbent.id, g.incumbent.registered, g.incumbent.presence, g.incumbent.switch], ["nobody", false, "unknown", "unknown"], "a holder that is not a mailbox is drawn as unregistered and unknown");
const vacant = JSON.parse(JSON.stringify(state)); vacant.role_cards[0].active = "";
is(mapMasterState(vacant, 1).roles.find((r) => r.id === "office.design").incumbent, null, "no active holder is vacant");
// a binding with a value
const bound = JSON.parse(JSON.stringify(state)); bound.boxes[1].identity.binding = { kind: "codex", id: "secret-session" };
const bi = mapMasterState(bound, 1).roles.find((r) => r.id === "office.design").incumbent;
is(bi.binding, "codex", "a filled binding is shown by its kind"); is(JSON.stringify(bi).includes("secret-session"), false, "…and its session id never reaches the page");
bound.boxes[1].identity.binding = [1];
is(mapMasterState(bound, 1).roles.find((r) => r.id === "office.design").incumbent.binding, "unrecognised", "an unknown binding shape is 'unrecognised', not guessed");

// ---- letter / outbox ----
const letter = normalizeLetter(json("letter_ok.json"));
is([letter.id, letter.box, letter.sender, letter.subject, letter.need], ["20261013-044210_panelop_样例待唤醒", "box_a", "panelop", "样例·待唤醒", "仅告知"], "letter_ok maps");
const rows = json("outbox_ok.json").outbox.map(normalizeOutboxRow);
is(rows.map((r) => r.status), ["unknown", "locked", "pending"], "outbox keeps pending / locked / unknown as the backend judged them");
is(rows[1].reason.length > 0 && rows[2].reason === "", true, "…with the backend's reason where it gave one");

// ---- role-diff: the real shape ----
const diffBody = json("role_diff_ok_response.json").body;
const diff = normalizeDiff(diffBody.diff);
is(diff.changes.map((c) => c.field), ["candidates", "duties"], "all fields arrive as _from/_to pairs; the client keeps only the ones that differ");
is(diff.changes[0], { field: "candidates", from: ["w1", "w2"], to: ["w2", "w1"], notes: [] }, "a reorder is a change");
is(diff.expected.map((x) => x.k), ["active_now", "active_after", "active_will_change", "acceptor_now", "acceptor_after", "note_key"], "expected keeps the backend's keys");
is(diff.expected.find((x) => x.k === "note_key").v, "roleNoteTiming", "the note code is carried as written (the pack has no text for it)");
is(diff.expected.find((x) => x.k === "active_will_change").v, true, "a boolean stays a boolean");
is(diff.facts.find((x) => x.k === "confirmed").v, null, "confirmed null stays null (unknown)");
is(diff.facts.find((x) => x.k === "candidates").v, "w1 (online), w2 (online)", "candidate states are listed with their codes");
is(diff.baseline, "<sha256 of config bytes, 64 hex>", "the baseline is passed through untouched");
is(normalizeDiff({ name: "x", change: { escalation_from: "a", escalation_to: "b", escalation_to_title: "B", candidates_from: ["x"], candidates_to: ["x"] } }).changes,
  [{ field: "escalation", from: "a", to: "b", notes: [{ k: "title", v: "B" }] }], "the escalation's new title rides with the change");

// ---- applied ----
const applied = (o) => normalizeApplied({ saved: true, ...o });
is(["fresh", "pending", "not_applicable", "unknown"].map((p) => applied({ projection: p }).projection), ["fresh", "pending", "not_applicable", "unknown"], "projection fresh / pending / not_applicable pass; anything else is unknown");
is(applied({ projection: "refreshing-ish" }).projection, "unknown", "an unrecognised projection is 'saved + projection unknown'");
is(applied({ projection: "pending", active_now: "box_b", acceptor_now: "" }).activeNow, ["box_b"], "active_now (a string) is carried");

// ---- the transport: every documented answer, replayed through a fake fetch ----
const calls = [];
const reply = (status, body) => async (url, opt) => { calls.push({ url, opt }); const text = typeof body === "string" ? body : JSON.stringify(body); return { ok: status >= 200 && status < 300, status, text: async () => text }; };
const adapter = (fetchImpl) => createHttpAdapter({ fetchImpl, timeoutMs: 300, writeTimeoutMs: 300 });
const candidateState = reply(200, state);
const cases = json("http_cases.json").cases;
const body = (name) => JSON.parse(cases.find((c) => c.name === name).body_resp);

// reads
{ const a = adapter(candidateState); const r = await a.getSnapshot(); is([r.ok, r.data.source], [true, "candidate"], "GET /api/state answers a candidate snapshot"); }
{ calls.length = 0; const a = adapter(reply(200, json("letter_ok.json"))); await a.readLetter({ box: "box_a", id: "20261013-044210_panelop_样例待唤醒" });
  is(calls[0].url, "/api/letter?box=box_a&id=" + encodeURIComponent("20261013-044210_panelop_样例待唤醒"), "the letter id is percent-encoded");
  is(/[^\x00-\x7f]/.test(calls[0].url), false, "…so the request line is plain ASCII (raw UTF-8 would be a 400 HTML page)");
  is(calls[0].opt.method, "GET", "a read is a GET"); is(calls[0].opt.headers, undefined, "a GET sends no header at all"); is("credentials" in calls[0].opt, false, "…and no credentials option"); }
{ const r = await adapter(reply(400, "<html><body>Error response</body></html>")).readLetter({ box: "box_a", id: "x" }); is([r.ok, r.error.code], [false, "invalid"], "an HTML 400 is an invalid request, not a crash"); }
for (const [name, status, code] of [["letter_bad_id_md", 400, "invalid"], ["letter_glob", 400, "invalid"], ["letter_unknown_box", 404, "not_found"], ["letter_bad_box_name", 400, "invalid"]]) {
  const r = await adapter(reply(status, body(name))).readLetter({ box: "b", id: "x" }); is([r.ok, r.error.code], [false, code], `${name}: ${status} → ${code}`);
}

// the 403 {"ok":false} guard: GET and POST, one meaning, never invented authorization
for (const name of ["x01_get_evil_origin", "x01_get_evil_host", "x01_post_evil_origin", "x01_post_evil_host", "x01_post_text_plain"]) {
  const c = cases.find((x) => x.name === name); const a = adapter(reply(c.status, c.body_resp));
  const r = c.method === "GET" ? await a.getSnapshot() : await a.sendMail({ from: "p", to: "b", subject: "s", need: "", body: "" });
  is([r.ok, r.error.code, r.error.message, r.error.saved, r.error.outcomeUnknown], [false, "denied", GUARD, false, undefined], `${name}: explained as the Host / Origin / Content-Type protection, nothing saved`);
}
is(GUARD.includes("no token") || GUARD.includes("no token to add"), true, "the explanation says there is no token to add");
{ const r = await adapter(reply(403, { ok: false, error: "scope" })).getSnapshot(); is([r.error.code, r.error.message.includes('"scope"')], ["denied", true], "an unknown 403 body is shown as written, not as the guard"); }
{ const r = await adapter(reply(403, "")).getSnapshot(); is([r.error.code, r.error.message.includes("(empty body)"), r.error.message === GUARD], ["denied", true, false], "an empty 403 is not explained either"); }
{ const r = await adapter(reply(401, { ok: false })).getSnapshot(); is([r.error.code, r.error.message === GUARD], ["denied", false], "a 401 is never the guard"); }

// mail writes
{ calls.length = 0; const a = adapter(reply(200, { ok: true, id: "i", ref: "box_b/i" })); await a.sendMail({ from: "panelop", to: "@office.design", subject: "s", need: "n", body: "b" });
  const c = calls[0]; is([c.url, c.opt.method, c.opt.headers], ["/api/send", "POST", { "Content-Type": "application/json" }], "POST /api/send: JSON content type and no other header");
  is(Object.keys(JSON.parse(c.opt.body)), ["from", "to", "subject", "need", "body"], "…with exactly the five fields"); }
for (const [name, call, state] of [["archive_one_moved", (a) => a.archiveMail({ box: "box_a", id: "i" }), "moved"], ["archive_one_already", (a) => a.archiveMail({ box: "box_a", id: "i" }), "already"], ["ack_one_recorded", (a) => a.ackMail({ box: "box_a", id: "i", note: "n" }), "recorded"],
  ["ack_one_already", (a) => a.ackMail({ box: "box_a", id: "i" }), "already"], ["edit_one_updated", (a) => a.editMail({ to: "box_b", id: "i", body: "b" }), "updated"], ["retract_one_ok", (a) => a.retractMail({ to: "box_b", id: "i" }), "retracted"]]) {
  const c = cases.find((x) => x.name === name); const r = await call(adapter(reply(c.status, c.body_resp))); is([r.ok, r.data.state], [true, state], `${name}: ${state}`);
}
for (const [name, call, code] of [["archive_one_bad_box_name", (a) => a.archiveMail({ box: "bad/name", id: "x" }), "not_found"], ["archive_one_md_suffix", (a) => a.archiveMail({ box: "box_a", id: "x.md" }), "invalid"],
  ["edit_one_not_sender", (a) => a.editMail({ to: "box_a", id: "i", subject: "s" }), "refused"], ["retract_one_repeat", (a) => a.retractMail({ to: "box_b", id: "i" }), "not_found"]]) {
  const c = cases.find((x) => x.name === name); const r = await call(adapter(reply(c.status, c.body_resp))); is([r.ok, r.error.code], [false, code], `${name}: ${code}`);
  if (name === "edit_one_not_sender") is(r.error.message, body(name).message, "…the backend's own reason is shown, not ours");
}

// role-diff
{ calls.length = 0; const a = adapter(reply(200, diffBody));
  const r = await a.previewRoleChange({ name: "build", candidates: ["w2", "w1"] });
  is([r.ok, r.data.diff.changes.length], [true, 2], "role-diff 200 → a diff");
  is([calls[0].url, calls[0].opt.method, calls[0].opt.headers], ["/api/role-diff", "POST", { "Content-Type": "application/json" }], "role-diff is a JSON POST with nothing else added"); }
for (const [name, code, status] of [["role_diff_400_duties6", "invalid", 400], ["role_diff_400_no_cands", "invalid", 400], ["role_diff_404", "not_found", 404]]) {
  const r = await adapter(reply(status, body(name))).previewRoleChange({ name: "x", candidates: [] }); is([r.ok, r.error.code, r.error.message], [false, code, body(name).error], `${name}: ${code}, the backend's reason shown as written`);
}
{ const r = await adapter(async () => { const e = new Error("t"); e.name = "AbortError"; throw e; }).previewRoleChange({ name: "x", candidates: ["a"] });
  is([r.ok, r.error.code, !!r.error.outcomeUnknown], [false, "network", false], "role-diff writes nothing: a lost reply is an ordinary network failure that may be asked again"); }
is((await adapter(reply(200, { ok: true })).previewRoleChange({ name: "x", candidates: ["a"] })).error.code, "invalid", "a role-diff reply with no diff is invalid");

// role-apply
const req = { name: "office.worker", candidates: ["box_a", "box_b"] };
{ calls.length = 0; const a = adapter(reply(200, body("role_apply_fresh"))); const r = await a.applyRoleChange(req, "BASE");
  is([r.ok, r.data.saved, r.data.projection, r.data.activeNow], [true, true, "fresh", ["box_a"]], "role_apply_fresh: saved, cards fresh");
  is(JSON.parse(calls[0].opt.body), { ...req, baseline: "BASE" }, "apply sends the previewed request plus the baseline, nothing else"); is(calls.length, 1, "…once"); }
{ const r = await adapter(reply(200, body("role_apply_pending"))).applyRoleChange(req, "B"); is([r.ok, r.data.saved, r.data.projection], [true, true, "pending"], "role_apply_pending: saved:true, projection pending is a success (saved, cards to refresh), not a failure"); }
{ const r = await adapter(reply(200, { ok: true, saved: true, projection: "something-new", message: "m" })).applyRoleChange(req, "B"); is([r.ok, r.data.projection], [true, "unknown"], "an unknown projection is saved + projection unknown"); }
{ const r = await adapter(reply(409, body("role_apply_409_stale"))).applyRoleChange(req, "OLD"); is([r.ok, r.error.code, r.error.saved, r.error.projection, "current" in r.error], [false, "stale", false, "not_applicable", false], "role_apply_409_stale: stale, nothing saved, no current role to compare"); }
for (const [name, status, code] of [["role_apply_404", 404, "not_found"], ["role_apply_400_no_baseline", 400, "invalid"], ["role_apply_400_validate", 400, "invalid"], ["role_apply_400_unregistered_cand", 400, "invalid"], ["role_apply_500_backup_failed", 500, "server"]]) {
  const r = await adapter(reply(status, body(name))).applyRoleChange(req, "B"); is([r.ok, r.error.code, r.error.saved, r.error.outcomeUnknown], [false, code, false, undefined], `${name}: ${code}, saved:false, the config is untouched`);
  is(r.error.message, body(name).error, "…and the reason is the backend's own");
}
// unknown outcomes: never resent
{ calls.length = 0; const r = await adapter(async (u, o) => { calls.push(u); const e = new Error("t"); e.name = "AbortError"; throw e; }).applyRoleChange(req, "B");
  is([r.ok, r.error.code, r.error.outcomeUnknown, r.error.retryable], [false, "network", true, false], "an apply that times out is outcome unknown and not retryable"); is(calls.length, 1, "…and was sent exactly once"); }
{ const r = await adapter(reply(502, "<html>Bad Gateway</html>")).applyRoleChange(req, "B"); is([r.ok, r.error.outcomeUnknown], [false, true], "a 5xx that does not say saved:false is outcome unknown"); }
{ const r = await adapter(reply(200, { ok: true, projection: "fresh" })).applyRoleChange(req, "B"); is([r.ok, r.error.outcomeUnknown], [false, true], "a 200 that does not say saved:true is outcome unknown, not success"); }
{ const r = await adapter(reply(200, "not json")).applyRoleChange(req, "B"); is([r.ok, r.error.outcomeUnknown], [false, true], "a 200 with an unreadable body is outcome unknown"); }
{ const r = await adapter(reply(403, { ok: false })).applyRoleChange(req, "B"); is([r.ok, r.error.code, r.error.message === GUARD, r.error.saved], [false, "denied", true, false], "a guarded apply is denied, nothing saved"); }

// ---- the flow: an unknown apply keeps the draft and can only be previewed again ----
{
  const role = byId("office.design");
  let es = reduce(startEdit(role), { type: "input", field: "duties", value: "拟契约样例\n再来一条" });
  const rq = buildRequest(es); is(Object.keys(rq), ["name", "candidates", "duties"], "the request carries only the changed field plus name and candidates");
  es = reduce(es, { type: "preview:ok", data: diff }); es = reduce(es, { type: "apply:start" });
  es = reduce(es, { type: "apply:fail", error: { code: "network", outcomeUnknown: true, retryable: false, message: "lost" } });
  is([es.phase, es.error.during, es.text.duties], ["error", "apply", "拟契约样例\n再来一条"], "after an unknown outcome the draft is intact");
  es = reduce(es, { type: "preview:start" }); is(es.phase, "previewing", "…and the next step is a new preview, never an automatic apply");
  is(baseOf(role).escalation, "office.worker", "the base carries the escalation as the backend wrote it");
}

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
