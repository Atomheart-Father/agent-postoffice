// Client data layer: the contract normaliser never guesses, the mock adapter honours every failure scenario,
// the role-edit flow never loses a draft, and the R0 mapping reports what it cannot map. Pure Node, no browser.
import { normalizeSnapshot, validateSnapshot, normalizeUsage, ERR } from "../client/src/contract.js";
import { createMockAdapter, SCENARIOS } from "../client/src/adapters/mock.js";
import { mapR0State, createHttpAdapter } from "../client/src/adapters/http.js";
import { startEdit, patchOf, isDirty, reduce, movedUpstream, parseResources } from "../client/src/flows/roleEdit.js";
import { parseHash, formatHash } from "../client/src/router.js";
import { compact } from "../client/src/views/hud.js";
import { FEATURES } from "../client/src/capabilities.js";

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

// ---- role edit flow against the mock backend ----
{
  const a = mk("default"); let s = (await a.getSnapshot()).data;
  let role = s.roles.find((r) => r.id === "analyst");
  let es = startEdit(role);
  is(isDirty(es), false, "a fresh draft is not dirty");
  es = reduce(es, { type: "input", field: "duties", value: role.duties.concat("Cite every figure").join("\n") });
  is(isDirty(es), true, "typing makes it dirty"); is(Object.keys(patchOf(es)).join(), "duties", "only changed fields are in the patch");
  const pv = await a.previewRoleChange(role.id, patchOf(es), es.baseRev);
  is(pv.ok && pv.data.changes.length, 1, "preview returns one added duty"); is(pv.data.changes[0].op, "add", "…as an add");
  es = reduce(es, { type: "preview:ok", data: pv.data }); is(es.phase, "preview", "phase: preview");
  es = reduce(es, { type: "input", field: "summary", value: "x" }); is(es.phase, "idle", "editing after a preview goes back to draft (stale preview dropped)");
  const bad1 = await a.applyRoleChange(role.id, patchOf(es), es.baseRev, "bogus"); is(bad1.error.code, ERR.INVALID, "apply with a token that does not match is refused");
  const ap = await a.applyRoleChange(role.id, { duties: [...role.duties, "Cite every figure"] }, role.rev, pv.data.token);
  is(ap.ok && ap.data.state, "applied", "apply succeeds"); is(ap.data.rev, role.rev + 1, "revision moves on");
  const again = await a.applyRoleChange(role.id, { duties: [] }, role.rev, pv.data.token); is(again.error.code, ERR.STALE, "the same base revision is now stale");
  const den = await a.previewRoleChange("reviewer", { summary: "x" }, 6); is(den.error.code, ERR.DENIED, "a role the actor may not edit is denied by the backend");
  is(den.error.message.includes("role.edit"), true, "the denial says which capability is missing");
  const own = await a.previewRoleChange("owner", { summary: "x" }, 4); is(own.error.code, ERR.DENIED, "the operator role cannot be edited from the client");
}
{
  const a = mk("conflict"); const s = (await a.getSnapshot()).data; const role = s.roles.find((r) => r.id === "analyst");
  let es = reduce(startEdit(role), { type: "input", field: "summary", value: "New summary" });
  const pv = await a.previewRoleChange(role.id, patchOf(es), es.baseRev); es = reduce(es, { type: "preview:ok", data: pv.data }); es = reduce(es, { type: "apply:start" });
  const r = await a.applyRoleChange(role.id, patchOf(es), es.baseRev, es.preview.token);
  is(r.error.code, ERR.STALE, "conflict scenario: apply is stale"); es = reduce(es, { type: "apply:fail", error: r.error });
  is(es.text.summary, "New summary", "the draft survives a conflict"); is(es.phase, "error", "phase: error");
  es = reduce(es, { type: "rebase" }); is(es.baseRev, r.error.current.rev, "rebase moves the base to the new revision"); is(es.text.summary, "New summary", "…and still keeps the draft");
  is(es.base.duties.includes("Added by someone else while you were editing"), true, "…and the base now contains what changed upstream");
  is(movedUpstream(startEdit(role), { ...role, rev: role.rev + 1 }), true, "movedUpstream detects a changed revision");
}
{
  const a = mk("partial"); const role = (await a.getSnapshot()).data.roles.find((r) => r.id === "analyst");
  const pv = await a.previewRoleChange(role.id, { summary: "S" }, role.rev);
  const r = await a.applyRoleChange(role.id, { summary: "S" }, role.rev, pv.data.token);
  is(r.data.state, "partial", "partial scenario: saved with failed projections"); is(r.data.projections.filter((p) => !p.ok).length, 1, "one projection failed and is named");
  const rt = await a.retryProjections(role.id, ["Contact cards"]); is(rt.data.projections[0].ok, true, "retry fixes the failed projection");
}
{
  const a = mk("denied"); is((await a.previewRoleChange("analyst", { summary: "x" }, 3)).error.code, ERR.DENIED, "denied scenario");
  const m = mk("missing"); is((await m.createThread({ roleId: "analyst", title: "t" })).error.code, ERR.MISSING, "missing scenario: capability_missing");
  is((await mk("default").createThread({ roleId: "analyst", title: "t", harness: "Codex" })).data.state, ERR.PENDING, "creating a thread is pending approval, never started");
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

// ---- R0 mapping: real fields in, honest gaps out ----
{
  const st = { panel: { ok: true, label: "Demo", operator: "boss", organization: { mailbox: "boss", label: "Director", children: [{ label: "Team", members: ["a", "b"] }] } },
    boxes: [{ name: "boss", app: "Manual", method: "manual", online: true, counts: { waiting: 2 }, pending: [{ file: "x.md", status: "waiting", subject: "Hi", from: "a", mtime: 1 }] },
      { name: "a", app: "OpenCode", online: true, counts: { waiting: 0 }, pending: [] }, { name: "b", app: "Codex", online: false, counts: { waiting: 0 }, pending: [] }] };
  const s = mapR0State(st, 100);
  is(validateSnapshot(s).length, 0, "an R0 state maps to a structurally valid snapshot");
  is(s.roles.find((r) => r.id === "mb:b").incumbent.presence, "offline", "R0 online=false is offline");
  is(s.roles.find((r) => r.id === "mb:a").incumbent.presence, "connected", "R0 online=true is connected (it cannot say busy)");
  is(s.mail[0].delivery, "accepted", "an R0 pending letter is accepted, not done");
  is(s.capabilities["thread.list"].status, "missing", "threads are reported missing");
  is(s.capabilities["office.tree"].status, "partial", "the tree is reported partial");
  is(s.threads.length + s.work.length + s.approvals.length, 0, "nothing is invented for threads, work or approvals");
  is(s.usage, null, "usage is unknown, not zero");
  const flat = mapR0State({ panel: { ok: false, error: "bad config" }, boxes: st.boxes }, 100);
  is(flat.consistency, "degraded", "a broken organization config is degraded"); is(flat.roles.filter((r) => r.kind === "position").length, 3, "every mailbox is still a role");
  const h = createHttpAdapter({ fetchImpl: async () => { throw new Error("x"); } });
  is((await h.getSnapshot()).error.code, ERR.NETWORK, "an unreachable backend is a network error");
  is((await h.previewRoleChange()).error.code, ERR.MISSING, "role editing is capability_missing on R0 (no guessed payload)");
  const h2 = createHttpAdapter({ fetchImpl: async () => ({ ok: false, status: 403 }) });
  is((await h2.getSnapshot()).error.code, ERR.DENIED, "a 403 is denied");
}

// ---- misc ----
{
  is(parseResources("repo: notes/\nWeb search").map((r) => r.kind).join(), "repo,resource", "resource lines parse as kind: label");
  const r = parseHash("#/office/analyst/edit"); is(r.roleId + r.roleTab, "analystedit", "route: role + tab");
  is(parseHash("#/threads/t-1").threadId, "t-1", "route: thread"); is(parseHash("#/work?role=builder").workRole, "builder", "route: work filter");
  is(parseHash("#/nonsense").view, "office", "an unknown route falls back to office");
  is(formatHash({ view: "office", roleId: "a b", roleTab: "edit", workFilter: {} }), "#/office/a%20b/edit", "route round-trips with escaping");
  is(compact(14686000), "14.7M", "compact keeps one decimal"); is(SCENARIOS.length, 8, "eight sample scenarios");
}
console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
