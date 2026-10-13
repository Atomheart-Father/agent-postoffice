// HTTP adapter for the BOXZ backend's panel API (`postoffice panel`). Two backends speak it and the adapter tells them apart
// by what /api/state contains, never by a flag:
//   master 1ab0f54 (v1.14.1)    no role_cards, no role-diff / role-apply
//   local candidate e819f0e     /api/state also carries role_cards[] and aliases[]; POST /api/role-diff, /api/role-apply
// The candidate contract is the written pack at Atomheart-Father/agent-postoffice@9cd53d5 docs/cloud-client-api-e819/;
// tests/fixtures/office-e819 holds its five anonymous JSON fixtures (sha256 in SOURCE.txt). The candidate itself is not
// deployed: everything below that depends on it is verified against those fixtures only, not against a running candidate.
//
//   reads    GET  /api/state    boxes (facts per mailbox), aliases, role_cards (candidate only), panel.organization, panel.operator
//            GET  /api/letter   one letter from a recipient's inbox (the id is percent-encoded)
//            GET  /api/outbox   the operator's sent mail that is still observable, with the backend's verdict on edit / retract
//   writes   POST /api/send /api/ack-one /api/archive-one /api/edit-one /api/retract-one
//            POST /api/status (delivery switch: one mailbox, several, or a named group)   POST /api/clear (the WHOLE inbox of one mailbox)
//            POST /api/role-diff (preview, writes nothing) -> user confirms -> POST /api/role-apply (candidate only)
//
// Everything else (threads, work, approvals, usage, thread control, secretary) has no backend: it answers capability_missing
// instead of guessing a payload.
//
// Writes are never retried by this adapter. A send / edit / retract / status / clear / role-apply whose reply is lost comes
// back as `outcomeUnknown`; whoever called decides, after looking at /api/state or /api/outbox. (A browser or a proxy may
// resend a POST on a reused keep-alive connection by itself; JavaScript cannot see or stop that. The real backend answers
// HTTP/1.0 and closes every connection, which is why nothing here sets a retry or an idempotency key it cannot honour.)
//
// What this mapping refuses to do, on purpose:
//   * treat a mailbox as a role: role_cards become role positions (id = the role's logical name), mailboxes stay mailbox
//     nodes (id `box:<name>`, origin "mailbox") in their own team; the holder of a role is looked up by name, never merged;
//   * turn the delivery switch into "connected": online (switch), binding, presence (activity) and the backend's state code
//     are four separate facts; activity "unknown" is a real state and stays unknown;
//   * flatten waiting / reminded / failed letters into one "accepted"; `ride` (counted by the backend, undocumented) is shown as unknown;
//   * hide a mailbox the (valid) tree forgot: it is listed under an "unassigned" node;
//   * say which role a letter was for: the backend does not record it, so mail is attributed to its mailbox only;
//   * invent authorization: there is no token and no can.*; a 403 is explained only as far as e819f0e proves it, any other
//     403 is shown as the backend wrote it;
//   * call a write "done" when the backend only filed or acknowledged it, and never resend a write whose reply was lost.
// Same-origin is the supported way to run (docs/CLIENT.zh-CN.md §4). The adapter sends no credentials and no header except
// Content-Type: application/json on POST; the browser adds Origin.

import { ERR, ok, fail, unknownOutcome, normalizeSnapshot, normalizeLetter, normalizeOutboxRow, normalizeMailResult, normalizeDiff, normalizeApplied, CONTRACT_VERSION } from "../contract.js";
import { capabilityMap, FEATURES } from "../capabilities.js";

const MAIL = { waiting: "waiting", reminded: "reminded", failed: "failed" };
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

// One mailbox as the holder of its node. Presence comes from the activity signal only (it is fresh and bound, or it
// is absent): working -> busy, idle -> connected, anything else -> unknown. The delivery switch and the binding stay
// separate facts, because a switch that is on proves delivery is allowed, not that a Harness is attached and healthy,
// and a switch that is off may be the owner's choice, not a dead session.
// The shape of a filled binding is not in the pack (only null and strings were seen): a string is shown as it is, an object
// by its `kind`, anything else as "unrecognised" rather than guessed.
const bindingOf = (x) => (x == null || x === "" ? null : typeof x === "string" ? x : typeof x === "object" && typeof x.kind === "string" && x.kind ? x.kind : "unrecognised");
export function occupantOf(b) {
  const prof = (b.identity && b.identity.profile) || {};
  const act = b.activity && typeof b.activity === "object" ? b.activity : {};
  const live = act.state === "working" || act.state === "idle";
  const since = num(act.since);
  return {
    id: b.name, label: prof.display_name || b.who || b.name, kind: prof.kind === "human" || prof.kind === "agent" ? prof.kind : "unspecified",
    harness: b.app || null, model: null,
    presence: act.state === "working" ? "busy" : act.state === "idle" ? "connected" : "unknown", presenceAt: live ? since : null,
    switch: b.online === true ? "on" : b.online === false ? "off" : "unknown",
    binding: bindingOf(b.identity && b.identity.binding),
    activity: live ? { text: act.state, at: since, source: "activity hook" } : null,
  };
}

// The "To" suggestions. A mailbox is a place letters land; `@name` is a logical address the backend resolves at send
// time to one of its candidates. Neither is a role card. An alias entry the backend could not read keeps its error.
export function addressesOf(st) {
  const out = [];
  for (const b of Array.isArray(st && st.boxes) ? st.boxes : []) {
    const prof = (b.identity && b.identity.profile) || {};
    out.push({ kind: "box", name: b.name, label: prof.display_name || b.who || "", target: null, online: b.online === true ? true : b.online === false ? false : null, error: "" });
  }
  for (const a of Array.isArray(st && st.aliases) ? st.aliases : []) {
    if (!a || typeof a.name !== "string") continue;
    const cands = Array.isArray(a.candidates) ? a.candidates : [];
    out.push({ kind: "alias", name: "@" + a.name, label: "", target: typeof a.target === "string" && a.target ? a.target : null,
      online: cands.length ? cands.some((c) => c && c.online === true) : null, error: typeof a.error === "string" ? a.error : "" });
  }
  return out;
}

// /api/state -> snapshot. Pure so it can be tested against fixtures.
export function mapMasterState(st, now = Date.now() / 1000) {
  const boxes = Array.isArray(st && st.boxes) ? st.boxes : [];
  const box = (n) => boxes.find((b) => b.name === n);
  const panel = (st && st.panel) || {};
  const candidate = Array.isArray(st && st.role_cards);   // only the candidate sends role_cards
  const roles = [], degraded = [], seen = new Set();
  const blank = (id, parentId, order, title, extra = {}) => ({ id, parentId, order, kind: "team", title, summary: "", duties: [], resources: [], incumbent: null, candidates: [], origin: "mailbox", box: "", waiting: 0, can: {}, ...extra });
  const seat = (name, label, parentId, order) => {
    const b = box(name); seen.add(name);
    const c = (b && b.counts) || {};
    roles.push(blank(`box:${name}`, parentId, order, label || name, { kind: "position", box: name, incumbent: b ? occupantOf(b) : null, waiting: (num(c.waiting) || 0) + (num(c.reminded) || 0) }));
  };
  const walk = (n, parentId, order) => {
    if (!n) return null;
    const me = n.mailbox ? `box:${n.mailbox}` : `team:${parentId || "root"}:${order}`;
    if (n.mailbox) seat(n.mailbox, n.label, parentId, order); else roles.push(blank(me, parentId, order, n.label || ""));
    (n.members || []).forEach((m, i) => seat(m, "", me, i));
    (n.children || []).forEach((c, i) => walk(c, me, (n.members || []).length + i));
    return me;
  };
  // The mailbox side of the tree. Master: it IS the tree. Candidate: it hangs under a "Mailboxes" team beside the roles.
  const mailboxTree = (parentId) => {
    if (panel.ok && panel.organization) {
      const root = walk(panel.organization, parentId, 0);
      const lost = boxes.filter((b) => !seen.has(b.name));
      if (lost.length) {   // a valid tree can still forget a mailbox; it must not vanish from the page
        const id = "team:unassigned";
        roles.push(blank(id, parentId === null ? root : parentId, 1000, "Unassigned mailboxes", { summary: "Registered mailboxes that the organisation tree does not place." }));
        lost.forEach((b, i) => seat(b.name, "", id, i));
      }
    } else {
      degraded.push(panel.error ? `Organization config: ${panel.error}` : "No organization in config.json; mailboxes are listed under one group.");
      boxes.forEach((b, i) => seat(b.name, "", parentId, i));
    }
  };
  if (candidate) {
    roles.push(blank("root", null, 0, panel.label || "Office", { origin: "role" }));
    // roles: from role_cards. A role is a logical position with a holder; it is NOT a mailbox. Grouped by the project the card names.
    roles.push(blank("team:roles", "root", 0, "Roles", { origin: "role" }));
    const projects = [...new Set(st.role_cards.map((c) => (c && typeof c.project === "string" ? c.project : "")))];
    projects.forEach((p, i) => roles.push(blank(`team:project:${p}`, "team:roles", i, p, { origin: "role" })));
    const holder = (name, code) => {
      const b = box(name);
      if (!b) return { id: name, label: name, kind: "unspecified", harness: null, model: null, presence: "unknown", presenceAt: null, switch: "unknown", binding: null, activity: null, state: code || null, registered: false, note: "" };
      return { ...occupantOf(b), state: code || null, registered: !!(b.identity && b.identity.registered === true) };
    };
    st.role_cards.forEach((c, i) => {
      if (!c || typeof c.name !== "string" || !c.name) return;
      const sf = c.status_file && typeof c.status_file === "object" ? c.status_file : null;
      roles.push(blank(c.name, `team:project:${typeof c.project === "string" ? c.project : ""}`, i, c.title || c.name, {
        kind: "position", origin: "role", duties: Array.isArray(c.duties) ? c.duties : [], resources: Array.isArray(c.resources) ? c.resources : [],
        incumbent: c.active ? holder(c.active, c.holder_state) : null,
        candidates: (Array.isArray(c.candidates) ? c.candidates : []).filter((x) => x && typeof x.name === "string").map((x) => holder(x.name, x.state)),
        escalationRole: typeof c.escalation_role === "string" ? c.escalation_role : null, escalationTitle: c.escalation_title, escalationTarget: c.escalation_target,
        escalationAcceptor: typeof c.escalation_acceptor === "string" ? c.escalation_acceptor : null,
        holderState: c.holder_state, roleKind: c.kind, project: c.project, confirmed: c.confirmed, configOk: c.config_ok,
        statusFile: sf ? { pathExists: sf.path_exists, updated: sf.updated } : null,
      }));
    });
    roles.push(blank("team:mailboxes", "root", 1, "Mailboxes"));
    mailboxTree("team:mailboxes");
  } else if (panel.ok && panel.organization) {
    mailboxTree(null);
  } else {
    mailboxTree("root");
    roles.unshift(blank("root", null, 0, panel.label || "Office"));
  }
  const mail = [];
  for (const b of boxes) for (const p of b.pending || []) {
    mail.push({ id: `${b.name}/${p.file}`, letterId: String(p.file || "").replace(/\.md$/, ""), roleId: "", box: b.name, threadId: null, workId: null, dir: "in", from: p.from || "", to: b.name, subject: p.subject || "",
      at: num(p.mtime), delivery: MAIL[p.status] || "unknown", sourceStatus: typeof p.status === "string" ? p.status : null, kind: typeof p.kind === "string" && p.kind ? p.kind : null });
  }
  const src = candidate ? "candidate" : "master";
  return normalizeSnapshot({
    contract: CONTRACT_VERSION, source: src, asOf: now, rev: 0, consistency: degraded.length ? "degraded" : "consistent", degraded,
    company: { name: panel.label || "", operatorRoleId: panel.operator ? `box:${panel.operator}` : "", operatorBox: panel.operator || "" },
    addresses: addressesOf(st),
    groups: Array.isArray(st && st.groups) ? st.groups : [],
    capabilities: capabilityMap((f) => ({ status: ["live", "fixture", "partial"].includes(f.src[src]) ? f.src[src] : "missing", note: f.note ? f.note.en : "" })),
    roles, threads: [], mail, work: [], approvals: [], usage: null,
  });
}

// master's POST error words -> the client's error codes. The backend's own `message` (when it sends one) always wins
// over our wording, because it is the reason (e.g. "already delivered, cannot retract").
const ERR_WORDS = { bad_request: ERR.INVALID, not_found: ERR.NOT_FOUND, refused: ERR.REFUSED, conflict: ERR.CONFLICT, failed: ERR.SERVER };
// e819f0e proves exactly one meaning for `403 {"ok":false}`: the Host / Origin / Content-Type protection (GET too). The
// client sends no token and has none to send, so it explains that and nothing more.
export const GUARD = "403 {\"ok\":false}: the backend's Host / Origin / Content-Type protection refused this request (as documented for e819f0e). It accepts a page only from its own origin (or the one origin set in POSTOFFICE_PANEL_BASE_URL) and POST only as application/json. Open this client from the backend's own address; there is no token to add.";
const isGuardBody = (d) => !!d && typeof d === "object" && !Array.isArray(d) && d.ok === false && Object.keys(d).length === 1;

// Does this error reply PROVE that the write was not applied? An HTTP status alone never does: a proxy can answer 502 (or
// 504) after the backend already saved, and a body can be lost on the way back. What does prove it:
//   * the backend's own refusal shape - a 4xx whose body is a JSON object with ok:false - because validation and permission
//     checks run before anything is written; except for send, where only bad_request / not_found / refused count (master's own validation words) until the pinned
//     contract says what a key conflict or any other 4xx on a keyed send means;
//   * the contract's explicit `saved:false` (role-apply), at any status.
// Everything else (5xx, an HTML page, an empty or unreadable body, a 2xx that says ok:false) is "unknown: it may have run".
function provesNotApplied(status, data, path) {
  if (!data || typeof data !== "object" || Array.isArray(data) || data.ok !== false) return false;
  if (data.saved === false) return true;
  if (status < 400 || status >= 500) return false;
  if (path === "/api/send") return data.error === "bad_request" || data.error === "not_found" || data.error === "refused";
  return true;
}

export function createHttpAdapter({ baseUrl = "", fetchImpl = (typeof fetch === "function" ? fetch : null), timeoutMs = 8000, writeTimeoutMs = 15000, pollMs = 5000 } = {}) {
  const base = String(baseUrl || "").replace(/\/+$/, "");
  const listeners = new Set();
  let timer = null;
  let source = null;   // "master" | "candidate" once a snapshot has told which backend this is

  // One request. Reads fail as `network`; a WRITE that never gets a readable answer is `outcome unknown` instead,
  // because the backend may have applied it. A 4xx/5xx with a body is a real answer and is mapped, not guessed.
  // `write:false` on a POST marks a pure preview (role-diff): a lost reply there is an ordinary network failure.
  async function call(method, path, body, { write = false, wordsOf = ERR_WORDS } = {}) {
    if (!fetchImpl) return fail(ERR.NETWORK, "fetch is not available in this environment.");
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const to = ctl ? setTimeout(() => ctl.abort(), write ? writeTimeoutMs : timeoutMs) : null;
    let res;
    try {
      res = await fetchImpl(base + path, { method, signal: ctl ? ctl.signal : undefined, ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
    } catch (e) {
      const why = e && e.name === "AbortError" ? "The backend did not answer in time." : "Cannot reach the backend. Is `postoffice panel` running, and is this page served from the same origin?";
      return write ? unknownOutcome(`${why} The request may or may not have been applied; look before trying again.`) : fail(ERR.NETWORK, why);
    } finally { if (to) clearTimeout(to); }
    let raw = "", data = null;
    try { raw = typeof res.text === "function" ? await res.text() : JSON.stringify(await res.json()); } catch { /* an unreadable body is handled below */ }
    try { data = raw ? JSON.parse(raw) : null; } catch { /* not JSON */ }
    if (res.status === 401 || res.status === 403) {
      // A refusal happens before the request is looked at, so nothing was saved. Only the one body e819f0e documents is explained;
      // any other 401 / 403 is shown exactly as the backend wrote it.
      if (res.status === 403 && isGuardBody(data)) return fail(ERR.DENIED, GUARD, { status: 403, saved: false, guard: true });
      // Any other 401 / 403 is shown as written. Nothing documents what else could answer that way (a proxy in front, say), so a
      // write refused like this is not claimed to be "not applied".
      return fail(ERR.DENIED, `The backend answered ${res.status}: ${raw ? raw.slice(0, 400) : "(empty body)"}`, { status: res.status, raw: raw.slice(0, 400), ...(write ? { outcomeUnknown: true, retryable: false } : { saved: false }) });
    }
    if (!res.ok || (data && data.ok === false)) {
      const word = data && typeof data.error === "string" ? data.error : "";
      const code = wordsOf[word] || (res.status === 404 ? ERR.NOT_FOUND : res.status >= 500 ? ERR.SERVER : ERR.INVALID);
      const msg = (data && typeof data.message === "string" && data.message) || (data && typeof data.error === "string" && !wordsOf[data.error] ? data.error : "") || `The backend answered ${res.status}${word ? ` (${word})` : ""}.`;
      // For a WRITE, an error is only "not applied" when the reply itself proves it. Otherwise it may have run (see provesNotApplied).
      const unproven = write && !provesNotApplied(res.status, data, path);
      return fail(code, msg, { status: res.status, word, ...(unproven ? { outcomeUnknown: true, retryable: false } : {}), ...(data && data.stale === true ? { stale: true } : {}), ...(data && typeof data.saved === "boolean" ? { saved: data.saved } : {}), ...(data && typeof data.projection === "string" ? { projection: data.projection } : {}) });
    }
    if (data == null) return write ? unknownOutcome(`The backend answered ${res.status} without a readable body. The request may have been applied; look before trying again.`) : fail(ERR.NETWORK, `The backend answered ${res.status} without a readable body.`);
    return ok(data);
  }
  const post = (path, body, o) => call("POST", path, body, { write: true, ...o });
  const q = (o) => Object.entries(o).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
  const missing = (what) => async () => fail(ERR.MISSING, `${what} is not offered by this backend.`);
  const done = (kind) => (r) => (r.ok ? ok(normalizeMailResult(kind, r.data)) : r);

  // ---- role-diff / role-apply (e819f0e). Their error bodies carry the reason as Chinese text in `error`, so no word table.
  const roleOps = {
    // role-diff writes nothing: a lost reply is a plain network failure and may be asked again.
    async previewRoleChange(req) {
      if (source === "master") return fail(ERR.MISSING, "This backend (master) has no role-diff; role editing is not offered.");
      const r = await call("POST", "/api/role-diff", req, { write: false, wordsOf: {} });
      if (!r.ok) return r;
      return r.data && r.data.diff && typeof r.data.diff === "object" ? ok({ diff: normalizeDiff(r.data.diff) }) : fail(ERR.INVALID, "The reply to role-diff carried no diff.");
    },
    // role-apply is never resent by the client. 409 = the config moved (no current role comes back: the user previews again);
    // 400 / 404 / 500 with saved:false = nothing was saved; a 5xx without saved:false, a lost reply or a 200 that does not say
    // saved:true is outcome unknown.
    async applyRoleChange(req, baseline) {
      if (source === "master") return fail(ERR.MISSING, "This backend (master) has no role-apply; role editing is not offered.");
      const r = await post("/api/role-apply", { ...req, baseline }, { wordsOf: {} });
      if (r.ok) return r.data && r.data.saved === true ? ok(normalizeApplied(r.data)) : unknownOutcome("The reply to role-apply did not say the change was saved. It may or may not have been applied; preview again before doing anything.");
      const e = r.error;
      if (e.outcomeUnknown) return r;
      if (e.status === 409 || e.stale) return fail(ERR.STALE, e.message || "The configuration changed after you previewed.", { saved: false, projection: "not_applicable" });
      if (e.status >= 500 && e.saved !== false) return unknownOutcome(`${e.message} The backend did not say that nothing was saved; preview again before doing anything.`);
      return r;
    },
  };

  return {
    kind: "http", label: base || "this origin", scenario: () => null,
    async getSnapshot() { const r = await call("GET", "/api/state"); if (!r.ok) return r; const snap = mapMasterState(r.data); source = snap.source; return ok(snap); },
    subscribe(cb) {
      listeners.add(cb);
      if (!timer && typeof setInterval === "function") timer = setInterval(() => listeners.forEach((f) => f()), pollMs);
      return () => { listeners.delete(cb); if (!listeners.size && timer) { clearInterval(timer); timer = null; } };
    },

    // ---- mail ----
    async readLetter({ box, id }) { const r = await call("GET", `/api/letter?${q({ box, id })}`); return r.ok ? ok(normalizeLetter(r.data)) : r; },
    async listOutbox() { const r = await call("GET", "/api/outbox"); return r.ok ? ok({ rows: (Array.isArray(r.data.outbox) ? r.data.outbox : []).map(normalizeOutboxRow) }) : r; },
    // client_request_id: the one-off key of THIS send operation (made by the caller before the first attempt). It is sent as given and
    // never invented or replaced here. An old backend ignores it; that is not de-duplication and nothing here treats it as such.
    async sendMail({ from, to, subject, need, body, client_request_id }) {
      const key = typeof client_request_id === "string" && client_request_id ? { client_request_id } : {};
      return done("send")(await post("/api/send", { from, to, subject, need, body, ...key }));
    },
    async ackMail({ box, id, note = "" }) { return done("ack")(await post("/api/ack-one", { box, id, note })); },
    async archiveMail({ box, id }) { return done("archive")(await post("/api/archive-one", { box, id })); },
    async editMail({ to, id, subject, need, body }) { return done("edit")(await post("/api/edit-one", { to, id, subject, need, body })); },
    async retractMail({ to, id }) { return done("retract")(await post("/api/retract-one", { to, id })); },

    // ---- delivery switch and whole-mailbox clear ----
    // status: POST /api/status {name,status} | {names:[...],status} (candidate only) | {group,status} (group name WITHOUT "@").
    // The backend answers {ok:true,error:""}; an unknown mailbox is 400 {ok:false,error:""} with no reason; an unknown group is 400
    // with a reason. The reply says nothing about the resulting state: the caller re-reads /api/state, which is the truth.
    // Several names on master (it has no `names`): one request per mailbox, in order, stopping at the first that fails; the error
    // then lists what was applied and what was not tried, and nothing is resent.
    async setDelivery({ name, names, group, status }) {
      if (!["online", "offline"].includes(status)) return fail(ERR.INVALID, "status must be online or offline.");
      const hit = (b) => post("/api/status", b, { wordsOf: {} });
      if (typeof group === "string" && group) { const g = group.replace(/^@/, ""); const r = await hit({ group: g, status }); return r.ok ? ok({ status, group: g, names: [] }) : r; }
      const list = [...new Set((Array.isArray(names) ? names : name ? [name] : []).filter((x) => typeof x === "string" && x))];
      if (!list.length) return fail(ERR.INVALID, "No mailbox was chosen.");
      if (list.length === 1) { const r = await hit({ name: list[0], status }); return r.ok ? ok({ status, names: list }) : r; }
      if (source === "candidate") { const r = await hit({ names: list, status }); return r.ok ? ok({ status, names: list }) : r; }
      const applied = [];
      for (const n of list) {
        const r = await hit({ name: n, status });
        if (!r.ok) return fail(r.error.code, `${r.error.message}${applied.length ? ` Applied before this: ${applied.join(", ")}.` : ""}`, { ...r.error, applied: [...applied], notTried: list.slice(applied.length + 1), failedAt: n });
        applied.push(n);
      }
      return ok({ status, names: applied });
    },
    // clear: POST /api/clear {name} moves EVERY letter in that inbox to archived/<time>/ (kept on disk, never delivered). It is not
    // archive-one, which files one letter into done/. The reply carries no count; the caller re-reads the state.
    async clearMailbox({ name }) {
      if (typeof name !== "string" || !name) return fail(ERR.INVALID, "No mailbox was chosen.");
      const r = await post("/api/clear", { name }, { wordsOf: {} });
      return r.ok ? ok({ name }) : r;
    },

    ...roleOps,
    createThread: missing("Starting a thread"), sendToThread: missing("Sending to a thread"), decideApproval: missing("Approvals"),
    stop() { if (timer) clearInterval(timer); timer = null; listeners.clear(); },
  };
}
export const HTTP_FEATURES = FEATURES;
