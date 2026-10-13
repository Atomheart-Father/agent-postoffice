// Client data layer: the contract normaliser never guesses, the mock adapter honours every failure scenario,
// the role-edit flow never loses a draft, and the master mapping reports what it cannot map. Pure Node, no browser.
import { normalizeSnapshot, validateSnapshot, normalizeUsage, normalizeDiff, normalizeApplied, ERR } from "../client/src/contract.js";
import { createMockAdapter, SCENARIOS } from "../client/src/adapters/mock.js";
import { mapMasterState, createHttpAdapter } from "../client/src/adapters/http.js";
import { startEdit, buildRequest, validateDraft, threeWay, editBlocker, isDirty, reduce, movedUpstream, parseResources } from "../client/src/flows/roleEdit.js";
import { mailOf } from "../client/src/views/ui.js";
import { parseHash, formatHash } from "../client/src/router.js";
import { compact } from "../client/src/views/hud.js";
import { readFileSync } from "node:fs";
import { getHost } from "../client/src/host.js";
import { FEATURES, sourceLabel, capsTableMarkdown } from "../client/src/capabilities.js";

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const is = (g, w, m) => (Object.is(g, w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));
const mk = (sc) => createMockAdapter({ scenario: sc, latency: 0 });

// ---- normaliser: unknown stays unknown, null stays null ----
{
  const s = normalizeSnapshot({ roles: [{ id: "a", incumbent: { id: "x", presence: "sleeping" } }], threads: [{ id: "t", roleId: "a", control: "yes" }], usage: { input: "5", total: null, coverage: "??" } });
  is(s.roles[0].incumbent.presence, "unknown", "an unrecognised presence is unknown, not a friendly default");
  is(s.threads[0].control, "unknown", "an unrecognised control state is unknown (read-only)");
  is(s.usage.input, null, "a non-number usage figure is null, not 0");
  is(s.usage.total, null, "a missing total stays null");
  is(normalizeUsage({ input: 1, output: 2, cacheRead: 3 }).total, null, "the total is never summed from the parts");
  is(normalizeUsage({ context: { tokens: 5, window: 0 } }).context, null, "context occupancy needs a reliable window");
  is(normalizeSnapshot(null).consistency, "degraded", "garbage in is degraded, not consistent");
  is(compact(null), null, "compact(null) is null");
}

// ---- the fixture is a valid snapshot and has every state the UI must show ----
{
  const r = await mk("default").getSnapshot();
  is(r.ok, true, "mock snapshot loads"); const s = r.data;
  is(validateSnapshot(s).length, 0, "the fixture has no structural problems");
  const pres = new Set(s.roles.filter((x) => x.incumbent).map((x) => x.incumbent.presence).concat(s.roles.flatMap((x) => x.candidates.map((c) => c.presence))));
  for (const p of ["connected", "busy", "power_save", "offline", "unconnected", "unknown"]) is(pres.has(p), true, `fixture shows presence ${p}`);
  is(s.roles.some((x) => x.kind === "position" && !x.incumbent), true, "fixture has a vacant role with candidates");
  for (const c of ["controllable", "observe_only", "pending_approval", "denied", "capability_missing", "stale"]) is(s.threads.some((t) => t.control === c), true, `fixture has a ${c} thread`);
  for (const d of ["sent", "accepted", "consumed", "failed", "retracted"]) is(s.mail.some((m) => m.delivery === d), true, `fixture has ${d} mail`);
  for (const w of ["open", "submitted", "verified", "accepted", "returned", "released"]) is(s.work.some((x) => x.state === w), true, `fixture has ${w} work`);
  is(s.threads.some((t) => t.usage && t.usage.total === null), true, "fixture has usage with an unreported total");
  is(s.threads.some((t) => t.droppedBefore > 0), true, "fixture has a truncated event buffer");
  for (const f of FEATURES.filter((f) => f.group !== "planned")) is(!!s.capabilities[f.key], true, `capability ${f.key} is reported`);
  is(s.company.name.includes("Northwind"), true, "anonymous sample company");
}

// ---- scenarios ----
{
  is((await mk("network").getSnapshot()).error.code, ERR.NETWORK, "network scenario: the read fails with network");
  is((await mk("empty").getSnapshot()).data.roles.length, 0, "empty scenario: no roles");
  const d = (await mk("degraded").getSnapshot()).data;
  is(d.consistency, "degraded", "degraded scenario says so"); is(d.roles.every((r) => !r.incumbent || r.incumbent.presence === "unknown"), true, "degraded: every presence is unknown"); is(d.usage, null, "degraded: usage is unknown");
}

// ---- role edit: the request is the backend's contract (name / candidates / escalation_role / duties / resources) ----
{
  const a = mk("default"); const s = (await a.getSnapshot()).data;
  const role = s.roles.find((r) => r.id === "analyst");
  let es = startEdit(role);
  is(editBlocker(role), "", "a position with candidates and a known escalation role is editable");
  is(isDirty(es), false, "a fresh draft is not dirty");
  is(Object.keys(buildRequest(es)).join(), "name,candidates", "an untouched draft sends only the name and the candidates (every other key absent = unchanged)");
  is(buildRequest(es).name, "analyst", "name is the logical role name");
  es = reduce(es, { type: "input", field: "duties", value: role.duties.concat("Cite every figure").join("\n") });
  is(Object.keys(buildRequest(es)).join(), "name,candidates,duties", "only the changed list is sent");
  const esc2 = reduce(startEdit(role), { type: "input", field: "escalation", value: "" });
  is(JSON.stringify(buildRequest(esc2).escalation_role), '""', "a cleared escalation is sent as an empty string, not dropped");
  is("escalation_role" in buildRequest(startEdit(role)), false, "an untouched escalation is never sent");
  is(buildRequest(es).duties.length, role.duties.length + 1, "a changed list is sent whole (replace semantics)");
  const cleared = reduce(startEdit(role), { type: "input", field: "resources", value: "" });
  is(JSON.stringify(buildRequest(cleared).resources), "[]", "an emptied list is sent as [] (clear), never dropped");
  is("duties" in buildRequest(cleared), false, "…while the untouched duties stay out of the request");
  is(JSON.stringify(parseResources("doc: a.md | the guide\nurl: https://x.test/a:b\nskill: s")), JSON.stringify([{ kind: "doc", ref: "a.md", note: "the guide" }, { kind: "url", ref: "https://x.test/a:b" }, { kind: "skill", ref: "s" }]), "resources parse as {kind, ref, note?}; the first colon ends the kind");
  is(buildRequest(es).resources, undefined, "no qualification, summary or other field ever enters the request");
  is(Object.keys(buildRequest(es)).every((k) => ["name", "candidates", "escalation_role", "duties", "resources"].includes(k)), true, "only contract keys are sent");

  const pv = await a.previewRoleChange(buildRequest(es));
  is(pv.ok, true, "preview succeeds"); const diff = pv.data.diff;
  is(typeof diff.baseline === "string" && diff.baseline.length > 0, true, "the preview carries a baseline the client did not make");
  is(diff.changes.map((c) => c.field).join(), "duties", "…and a change for the one edited list");
  is(Array.isArray(diff.expected) && Array.isArray(diff.facts), true, "expected / facts are lists");
  es = reduce(es, { type: "preview:ok", data: diff }); is(es.phase, "preview", "phase: preview");
  es = reduce(es, { type: "input", field: "duties", value: "x" }); is(es.phase, "idle", "editing after a preview drops the preview (and its baseline)");
  es = reduce(startEdit(role), { type: "input", field: "duties", value: role.duties.concat("Cite every figure").join("\n") });

  const wrong = await a.applyRoleChange(buildRequest(es), "not-the-baseline");
  is(wrong.error.code, ERR.STALE, "a baseline that does not match is refused"); is(wrong.error.saved, false, "…and the backend says nothing was saved");
  is("current" in wrong.error, false, "the stale reply does not carry the current role (the client must refetch)");
  const ap = await a.applyRoleChange(buildRequest(es), diff.baseline);
  is(ap.ok && ap.data.saved, true, "apply with the previewed baseline saves"); is(ap.data.projection, "fresh", "…and the derived cards are fresh");
  is((await a.getSnapshot()).data.roles.find((r) => r.id === "analyst").duties.includes("Cite every figure"), true, "the snapshot has the new duty");
  const again = await a.applyRoleChange(buildRequest(es), diff.baseline);
  is(again.error.code, ERR.STALE, "the same baseline is stale once the config moved");
}
{
  // the baseline covers the WHOLE config: a change to another role also invalidates a preview of this one
  const a = mk("default"); const s = (await a.getSnapshot()).data;
  const mine = startEdit(s.roles.find((r) => r.id === "analyst")), other = s.roles.find((r) => r.id === "fact-checker");
  const reqMine = buildRequest(reduce(mine, { type: "input", field: "duties", value: "Only this" }));
  const pv = await a.previewRoleChange(reqMine);
  const o = await a.previewRoleChange(buildRequest(reduce(startEdit(other), { type: "input", field: "duties", value: "Other change" })));
  is((await a.applyRoleChange(buildRequest(reduce(startEdit(other), { type: "input", field: "duties", value: "Other change" })), o.data.diff.baseline)).ok, true, "another role is saved");
  is((await a.applyRoleChange(reqMine, pv.data.diff.baseline)).error.code, ERR.STALE, "…which makes the earlier preview of this role stale, though this role did not change");
}
{
  // limits of the contract are enforced locally and again by the backend
  const role = (await mk("default").getSnapshot()).data.roles.find((r) => r.id === "analyst");
  const bad = (field, value) => validateDraft(reduce(startEdit(role), { type: "input", field, value }));
  is(bad("duties", "1\n2\n3\n4\n5\n6").some((i) => i.key === "count"), true, "more than 5 duties is refused");
  is(bad("duties", "x".repeat(81)).some((i) => i.key === "long"), true, "a duty over 80 characters is refused");
  is(bad("duties", "x".repeat(80)).length, 0, "…80 is fine");
  is(bad("resources", "tool: web").some((i) => i.key === "kind"), true, "an unknown resource kind is refused");
  is(bad("resources", "doc: " + "r".repeat(201)).some((i) => i.key === "refLong"), true, "a reference over 200 characters is refused");
  is(bad("resources", "doc: a | " + "n".repeat(61)).some((i) => i.key === "noteLong"), true, "a note over 60 characters is refused");
  is(bad("resources", Array.from({ length: 9 }, (_, i) => `doc: d${i}.md`).join("\n")).some((i) => i.key === "count"), true, "more than 8 resources is refused");
  is(bad("candidates", "").some((i) => i.key === "empty"), true, "candidates must not be empty");
  is(bad("candidates", "a\na").some((i) => i.key === "dup"), true, "candidates must not repeat");
  const a = mk("default");
  const r = await a.previewRoleChange({ name: "analyst", candidates: [], escalation_role: "research-lead" });
  is(r.error.code, ERR.INVALID, "the backend refuses what the contract refuses"); is(r.error.saved, false, "…and says nothing was saved");
  is((await a.previewRoleChange({ name: "ghost", candidates: ["x"], escalation_role: "" })).error.code, ERR.NOT_FOUND, "an unknown role is not_found");
}
{
  const a = mk("conflict"); const role = (await a.getSnapshot()).data.roles.find((r) => r.id === "analyst");
  let es = reduce(startEdit(role), { type: "input", field: "duties", value: role.duties.concat("My duty").join("\n") });
  const pv = await a.previewRoleChange(buildRequest(es)); es = reduce(es, { type: "preview:ok", data: pv.data.diff }); es = reduce(es, { type: "apply:start" });
  const r = await a.applyRoleChange(buildRequest(es), es.preview.baseline);
  is(r.error.code, ERR.STALE, "conflict scenario: apply is stale"); is("current" in r.error, false, "…without a current role");
  es = reduce(es, { type: "apply:fail", error: r.error }); is(es.phase, "error", "phase: error"); is(es.error.during, "apply", "the error knows it came from apply");
  is(es.text.duties.includes("My duty"), true, "the draft survives a conflict");
  const latest = (await a.getSnapshot()).data.roles.find((x) => x.id === "analyst");
  is(movedUpstream(es, latest), true, "the refetched role differs from where the draft started");
  const tw = threeWay(es, latest).find((x) => x.field === "duties");
  is(tw.conflict, true, "both sides changed the duties: a conflict"); is(tw.lost.join(), "Added by someone else while you were editing", "the upstream addition my whole-list draft would remove is named");
  const re = reduce(es, { type: "rebase", role: latest });
  is(re.text.duties.includes("My duty"), true, "rebase keeps what I typed"); is(re.base.duties.includes("Added by someone else while you were editing"), true, "…on a base that has the upstream change");
  is(movedUpstream(re, latest), false, "after the rebase the draft is current again");
  const pv2 = await a.previewRoleChange(buildRequest(re)); is(pv2.ok, true, "a new preview works");
  is((await a.applyRoleChange(buildRequest(re), pv2.data.diff.baseline)).ok, true, "…and applies on the fresh baseline (the conflict fired once)");
  // an untouched field follows upstream on rebase
  const res = reduce(startEdit(role), { type: "input", field: "resources", value: "doc: only.md" });
  const follow = reduce(res, { type: "rebase", role: latest });
  is(follow.text.duties, latest.duties.join("\n"), "a field I did not touch follows upstream");
  const elsewhere = threeWay(startEdit(role), role); is(elsewhere.length, 0, "no differences when nothing moved");
}
{
  // saved, derived cards pending: the config IS saved; there is nothing to apply again
  const a = mk("projection"); const role = (await a.getSnapshot()).data.roles.find((r) => r.id === "analyst");
  const es = reduce(startEdit(role), { type: "input", field: "duties", value: "Only duty" });
  const pv = await a.previewRoleChange(buildRequest(es));
  const r = await a.applyRoleChange(buildRequest(es), pv.data.diff.baseline);
  is(r.ok && r.data.saved, true, "projection scenario: saved is true"); is(r.data.projection, "pending", "…and the derived cards are pending");
  const done = reduce(es, { type: "apply:ok", data: r.data }); is(done.phase, "done", "the flow ends as done, not as an error");
  is(typeof a.retryProjections, "undefined", "there is no fake retryProjections on the adapter");
  is(typeof createHttpAdapter({}).retryProjections, "undefined", "…nor on the HTTP adapter");
  is(normalizeApplied({ saved: true, projection: "weird" }).projection, "unknown", "an unrecognised projection state is unknown");
  is(normalizeApplied({}).saved, false, "saved is never assumed");
  const nd = normalizeDiff({ name: "x", change: ["a"], expected: { k: 1 }, facts: "one" });
  is(nd.baseline, "", "a diff without a baseline has none (nothing can be applied)"); is(JSON.stringify(nd.expected), '[{"k":"k","v":"1"}]', "an object under expected is kept key by key"); is(JSON.stringify(nd.facts), '[{"k":"","v":"one"}]', "a string under facts stays a string");
  is(nd.changes.length, 1, "a change the client does not recognise is kept and shown raw"); is(nd.changes[0].raw, true, "…marked raw");
}
{
  const a = mk("denied"); const role = (await a.getSnapshot()).data.roles.find((r) => r.id === "analyst");
  is((await a.previewRoleChange(buildRequest(startEdit(role)))).error.code, ERR.DENIED, "denied scenario");
  const d = mk("default"); const s = (await d.getSnapshot()).data;
  const rv = s.roles.find((r) => r.id === "reviewer"), ow = s.roles.find((r) => r.id === "owner");
  const den = await d.previewRoleChange(buildRequest(startEdit(rv))); is(den.error.code, ERR.DENIED, "a role the actor may not edit is denied by the backend");
  is(den.error.message.includes("role.edit"), true, "the denial says which capability is missing");
  is((await d.previewRoleChange(buildRequest(startEdit(ow)))).error.code, ERR.DENIED, "the operator role cannot be edited from the client");
  const m = mk("missing"); is((await m.createThread({ roleId: "analyst", title: "t" })).error.code, ERR.MISSING, "missing scenario: capability_missing");
  is((await mk("default").createThread({ roleId: "analyst", title: "t", harness: "Codex" })).data.state, ERR.PENDING, "creating a thread is pending approval, never started");
  const team = s.roles.find((r) => r.id === "research"); is(editBlocker(team), "team", "a team is not edited as a position");
  is(editBlocker({ ...role, escalationRole: null }), "escalation", "an unknown escalation role closes editing (it is part of every request)");
  is(editBlocker({ ...role, candidates: [] }), "candidates", "an unknown candidate list closes editing");
  is(editBlocker({ ...role, origin: "mailbox" }), "mailbox", "a legacy mailbox node is not editable as a role");
}


// ---- thread control: observe is not control; accepted is not consumed ----
{
  const a = mk("default");
  is((await a.sendToThread("t-pricing", "hi")).error.code, ERR.DENIED, "an observed thread refuses input");
  is((await a.sendToThread("t-import", "hi")).error.code, ERR.PENDING, "a pending binding refuses input");
  is((await a.sendToThread("t-pr41", "hi")).error.code, ERR.MISSING, "a harness without a write channel refuses input");
  is((await a.sendToThread("t-reading", "hi")).error.code, ERR.STALE, "an expired binding refuses input");
  const r = await a.sendToThread("t-scan", "go"); is(r.data.state, "accepted", "a controllable thread accepts");
  const ev = (await a.getSnapshot()).data.threads.find((t) => t.id === "t-scan").events.slice(-1)[0];
  is(ev.delivery, "accepted", "the sent instruction shows accepted, not consumed");
}
{
  const a = mk("default"); a.tick(); a.tick();
  const t = (await a.getSnapshot()).data.threads.find((x) => x.id === "t-scan");
  is(t.events.length <= t.bufferLimit, true, "the event buffer stays bounded"); is(t.droppedBefore > 118, true, "dropped events are counted, not hidden");
  const ap = await a.decideApproval("ap-1", "approved"); is(ap.ok, true, "an approval can be decided");
  is((await a.decideApproval("ap-1", "approved")).error.code, ERR.STALE, "deciding twice is stale");
}

// ---- master mapping: real fields in, honest gaps out ----
{
  const boxes = [
    { name: "boss", app: "Manual", online: true, counts: { waiting: 1, reminded: 1, failed: 0 }, identity: { binding: null, profile: { kind: "human", display_name: "Boss" } },
      activity: { state: "unknown", since: null }, pending: [{ file: "x.md", status: "waiting", subject: "Hi", from: "a", mtime: 1 }, { file: "y.md", status: "reminded", subject: "Again", from: "a", mtime: 2 }] },
    { name: "a", app: "OpenCode", online: true, counts: { waiting: 0 }, identity: { binding: { kind: "opencode_plugin" }, profile: { kind: "agent" } }, activity: { state: "working", since: 90 }, pending: [{ file: "z.md", status: "failed", subject: "Bad", from: "boss", mtime: 3 }] },
    { name: "b", app: "Codex", online: false, counts: { waiting: 0 }, identity: { binding: { kind: "claude_hook" } }, activity: { state: "idle", since: 80 }, pending: [{ file: "w.md", status: "mystery", subject: "?", from: "a", mtime: 4 }] },
    { name: "c", app: "Codex", online: true, counts: {}, identity: {}, activity: {}, pending: [] }];
  const st = { panel: { ok: true, label: "Demo", operator: "boss", organization: { mailbox: "boss", label: "Director", children: [{ label: "Team", members: ["a", "b"] }] } }, boxes };
  const s = mapMasterState(st, 100);
  is(validateSnapshot(s).length, 0, "a master state maps to a structurally valid snapshot");
  is(s.roles.every((r) => r.id.startsWith("box:") || r.id.startsWith("team:")), true, "no node id looks like a role name");
  is(s.roles.filter((r) => r.origin === "mailbox").length, s.roles.length, "every mapped node says it comes from the mailbox tree");
  const inc = (n) => s.roles.find((r) => r.id === "box:" + n).incumbent;
  // online is a delivery switch, not a connection
  is(inc("c").presence, "unknown", "online=true with no activity signal is NOT connected"); is(inc("c").switch, "on", "…the switch is kept as its own fact");
  is(inc("b").switch, "off", "online=false is a switch that is off"); is(inc("b").presence, "connected", "…and a fresh idle signal still says connected (the switch is not presence)");
  is(inc("a").presence, "busy", "a fresh working signal is busy"); is(inc("a").activity.text, "working", "the activity is kept"); is(inc("a").activity.at, 90, "…with its time"); is(inc("a").activity.source, "activity hook", "…and its source");
  is(inc("a").binding, "opencode_plugin", "the session binding is its own fact"); is(inc("boss").binding, null, "no binding is null, not a made-up one");
  is(inc("boss").activity, null, "an unknown activity is null, never idle"); is(inc("boss").kind, "human", "profile kind is kept");
  // a valid tree can forget a mailbox; it must not vanish
  const un = s.roles.find((r) => r.id === "team:unassigned"); is(!!un, true, "a mailbox missing from a VALID tree is listed under an unassigned node");
  is(s.roles.find((r) => r.id === "box:c").parentId, "team:unassigned", "…and sits under it");
  const full = mapMasterState({ ...st, boxes: boxes.slice(0, 3) }, 100); is(full.roles.some((r) => r.id === "team:unassigned"), false, "no unassigned node when the tree covers every mailbox");
  // mail: source states survive
  const m = (f) => s.mail.find((x) => x.id.endsWith("/" + f));
  is(m("x.md").delivery, "waiting", "waiting stays waiting"); is(m("y.md").delivery, "reminded", "reminded stays reminded"); is(m("z.md").delivery, "failed", "failed stays failed");
  is(m("w.md").delivery, "unknown", "an unrecognised state is unknown, not accepted"); is(m("w.md").sourceStatus, "mystery", "…and the original word is kept");
  is(s.mail.some((x) => x.delivery === "accepted"), false, "nothing is flattened to accepted");
  is(s.mail.every((x) => x.roleId === "" && x.box), true, "a letter is attributed to its mailbox; the role is left empty");
  const boss = s.roles.find((r) => r.id === "box:boss"), aNode = s.roles.find((r) => r.id === "box:a");
  is(mailOf(s, boss).length, 2, "a mailbox node lists the letters in its mailbox"); is(mailOf(s, aNode).map((x) => x.subject).join(), "Bad", "…and not another mailbox's");
  is(mailOf(s, s.roles.find((r) => r.id === "team:unassigned")).length, 0, "a team node lists none");
  is(boss.waiting, 2, "waiting counts waiting + reminded letters");
  is(s.roles.find((r) => r.id === "box:boss").escalationRole, null, "the escalation role is unknown, not none");
  is(editBlocker(boss), "mailbox", "a mailbox node cannot be edited as a role");
  is(s.capabilities["thread.list"].status, "missing", "threads are reported missing"); is(s.capabilities["role.edit"].status, "missing", "role editing is reported missing on master");
  is(s.capabilities["office.tree"].status, "partial", "the tree is reported partial");
  is(s.threads.length + s.work.length + s.approvals.length, 0, "nothing is invented for threads, work or approvals"); is(s.usage, null, "usage is unknown, not zero");
  is(s.company.operatorRoleId, "box:boss", "the operator node follows the same id rule");
  const flat = mapMasterState({ panel: { ok: false, error: "bad config" }, boxes }, 100);
  is(flat.consistency, "degraded", "a broken organization config is degraded"); is(flat.roles.filter((r) => r.kind === "position").length, 4, "every mailbox is still listed");
  const h = createHttpAdapter({ fetchImpl: async () => { throw new Error("x"); } });
  is((await h.getSnapshot()).error.code, ERR.NETWORK, "an unreachable backend is a network error");
  const hm = createHttpAdapter({ fetchImpl: async () => ({ ok: true, status: 200, text: async () => JSON.stringify({ boxes: [], panel: { ok: false } }) }) });
  await hm.getSnapshot();
  is((await hm.previewRoleChange({})).error.code, ERR.MISSING, "once the state shows master (no role_cards) role editing is capability_missing: no guessed payload");
  is((await hm.applyRoleChange({}, "b")).error.code, ERR.MISSING, "…and so is apply");
  const h2 = createHttpAdapter({ fetchImpl: async () => ({ ok: false, status: 403, text: async () => "nope" }) });
  const r403 = await h2.getSnapshot(); is(r403.error.code, ERR.DENIED, "a 403 is denied"); is(r403.error.message.includes("nope"), true, "…and an unknown 403 body is shown as written, not explained");
}
// ---- capability sources are named for what they are ----
{
  is(FEATURES.every((f) => f.group === "planned" || ["master", "candidate", "mock"].every((k) => typeof f.src[k] === "string")), true, "every feature states its status for master, candidate and mock");
  is(FEATURES.some((f) => "r0" in f), false, "no feature is labelled with the old R0 column");
  is(sourceLabel("master", "en"), "GitHub master v1.14.1", "the master column names the published version"); is(sourceLabel("candidate", "zh"), "本地候选 e819f0e", "the candidate column names the commit"); is(sourceLabel("mock", "zh"), "客户端 mock", "the mock column");
  is(FEATURES.find((f) => f.key === "role.edit").src.master, "missing", "master has no role edit"); is(FEATURES.find((f) => f.key === "usage.hud").src.candidate, "missing", "the candidate has no usage reader yet");
}


// ---- the host seam never hands credentials to the page ----
{
  const h = getHost();
  is(h.kind, "browser", "without a host the client is a plain browser app"); is(h.capabilities.request, false, "…with no host request channel");
  is("backend" in h, false, "the seam has no backend() returning a base URL with headers");
  const r = await h.request("state.get", {}); is(r.error.code, ERR.MISSING, "a browser's host.request is capability_missing");
  is(JSON.stringify(Object.keys(h).sort()), JSON.stringify(["capabilities", "kind", "notify", "openExternal", "request"]), "the seam is exactly: notify, openExternal, request");
}

// ---- the docs say what the code says ----
{
  const doc = readFileSync(new URL("../docs/CLIENT.zh-CN.md", import.meta.url), "utf8");
  const m = doc.match(/<!-- caps:begin -->\n([\s\S]*?)\n<!-- caps:end -->/);
  is(!!m && m[1] === capsTableMarkdown(), true, "the capability table in docs/CLIENT.zh-CN.md is generated from capabilities.js");
  is(doc.includes("capabilities:{notify,openExternal,backend}") || doc.includes("backend() -> Promise"), false, "the docs do not describe the old host seam that handed headers to the page");
  is(doc.includes("| 已发布 R0 |") || doc.includes("已发布的 R0"), false, "no capability column is labelled published R0");
  is(doc.includes("中文输入法没有被验证"), true, "the docs say the IME is not verified");
}

// ---- misc ----
{
  const r = parseHash("#/office/analyst/edit"); is(r.roleId + r.roleTab, "analystedit", "route: role + tab");
  is(parseHash("#/threads/t-1").threadId, "t-1", "route: thread"); is(parseHash("#/work?role=builder").workRole, "builder", "route: work filter");
  is(parseHash("#/nonsense").view, "office", "an unknown route falls back to office");
  is(formatHash({ view: "office", roleId: "a b", roleTab: "edit", workFilter: {} }), "#/office/a%20b/edit", "route round-trips with escaping");
  is(compact(14686000), "14.7M", "compact keeps one decimal"); is(SCENARIOS.length, 9, "nine sample scenarios");
}
console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
