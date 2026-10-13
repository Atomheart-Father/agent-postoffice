// The client's HTTP adapter against the REAL master backend (postoffice v1.14.1, the file in this repo), not against a
// hand-written fixture: a temporary POSTOFFICE_HOME with invented mailboxes and letters, `postoffice panel` started on a
// free port, and every mail operation driven through createHttpAdapter. What this proves: the adapter's requests are
// accepted by master and master's replies map onto the contract as the UI expects. What it does not prove: anything about
// the local candidate e819f0e, a browser, or real data. Skips (exit 0) when python3 is not available.
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";
import { createHttpAdapter } from "../client/src/adapters/http.js";

const here = dirname(fileURLToPath(import.meta.url)), PO = join(here, "..", "postoffice");
let bad = 0, total = 0;
const ok = (c, m, extra = "") => { total++; if (!c) { bad++; console.log("BAD  -", m, extra); } else console.log("ok   -", m); };
const is = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), m, `:: got ${JSON.stringify(a)} want ${JSON.stringify(b)}`);

if (spawnSync("python3", ["--version"]).status !== 0 || !existsSync(PO)) { console.log("SKIP - python3 or the postoffice file is not available"); process.exit(0); }

const home = mkdtempSync(join(tmpdir(), "client_master_"));
const env = { ...process.env, POSTOFFICE_HOME: home, POSTOFFICE_NO_NOTIFY: "1", POSTOFFICE_POLL: "1" };
delete env.CLAUDE_CODE_ENTRYPOINT; delete env.CLAUDE_CODE_HOST_SESSION_ID;
const po = (args, input) => spawnSync("python3", [PO, ...args], { env, input, encoding: "utf-8" });
const freePort = () => new Promise((res) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => res(p)); }); });
let proc = null;
const done = (code) => { if (proc) proc.kill(); try { rmSync(home, { recursive: true, force: true }); } catch { /* temp */ } console.log(bad ? `FAIL ${bad}` : "PASS", `(${total})`); process.exit(code); };

try {
  for (const b of ["owner", "alice", "bob", "carol", "dave"]) { const r = po(["add", b, "--notify", "--who", `Synthetic ${b}`]); if (r.status !== 0) throw new Error(r.stdout + r.stderr); }
  writeFileSync(join(home, "config.json"), JSON.stringify({ version: 1, groups: {}, aliases: { helpers: ["alice", "bob"] },
    panel: { label: "Synthetic Office", operator: "owner", organization: { mailbox: "owner", children: [{ label: "Team", members: ["alice", "bob"] }] } } }));
  const send = (to, from, subject, need, body) => { const r = po(["send", to, from, subject, need], body); if (r.status !== 0) throw new Error(r.stdout + r.stderr); return /编号：(\S+)/.exec(r.stdout)[1]; };
  const fromAlice = send("owner", "alice", "Question from Alice", "回复", "Where is the file?\n");
  const toBob = send("bob", "owner", "Please review", "回复", "Review the draft.\n");
  const toCarol = send("carol", "owner", "Heads up", "仅告知", "FYI.\n");
  // dave's channel already accepted this one: master records that in .woken.json, which makes it "reminded" and locks it
  const toDave = send("dave", "owner", "Already taken", "回复", "Taken.\n");
  writeFileSync(join(home, ".woken.json"), JSON.stringify({ [join(home, "dave", "inbox", toDave + ".md")]: 1 }));
  const port = await freePort();
  proc = spawn("python3", [PO, "panel", "--no-open", "--port", String(port)], { env, stdio: "ignore" });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 60; i++) { try { if ((await fetch(base + "/api/state")).ok) break; } catch { /* starting */ } await new Promise((r) => setTimeout(r, 250)); }

  const a = createHttpAdapter({ baseUrl: base });

  // ---------- read model ----------
  const snap = (await a.getSnapshot());
  ok(snap.ok, "the real /api/state maps");
  const S = snap.data;
  is(S.company.operatorBox, "owner", "operatorBox comes from panel.operator");
  ok(S.addresses.some((x) => x.kind === "box" && x.name === "alice"), "mailboxes are listed as addresses");
  ok(S.addresses.some((x) => x.kind === "alias" && x.name === "@helpers"), "logical addresses keep their @");
  ok(S.roles.every((r) => r.origin === "mailbox" || r.kind === "team"), "every node is a mailbox node: no role is invented from a mailbox");
  const mOwner = S.mail.find((m) => m.box === "owner");
  ok(mOwner && mOwner.letterId === fromAlice && !mOwner.letterId.endsWith(".md"), "a letter carries its bare id for /api/letter");
  is(mOwner.delivery, "waiting", "a fresh letter is waiting, not accepted");
  is(S.capabilities["mail.send"].status, "live", "mail.send is live against master");
  is(S.capabilities["role.edit"].status, "missing", "role.edit is missing against master");

  // ---------- read a letter ----------
  const rl = await a.readLetter({ box: "owner", id: fromAlice });
  ok(rl.ok && rl.data.sender === "alice" && rl.data.subject === "Question from Alice" && rl.data.need === "回复" && rl.data.body === "Where is the file?", "readLetter returns sender, subject, need and body");
  const r404 = await a.readLetter({ box: "owner", id: "nope" }); is([r404.ok, r404.error.code], [false, "not_found"], "an unknown letter is not_found");
  const r400 = await a.readLetter({ box: "owner", id: "x.md" }); is([r400.ok, r400.error.code], [false, "invalid"], "an id ending in .md is invalid (bad_request)");
  const rbox = await a.readLetter({ box: "ghost", id: fromAlice }); is([rbox.ok, rbox.error.code], [false, "not_found"], "an unregistered mailbox is not_found");

  // ---------- send ----------
  const s1 = await a.sendMail({ from: "owner", to: "carol", subject: "Sent via client", need: "仅告知", body: "Hello Carol" });
  ok(s1.ok && s1.data.state === "sent" && s1.data.to === "carol" && /^carol\//.test(s1.data.ref), "send to a mailbox: filed, ref names the mailbox", JSON.stringify(s1));
  ok(existsSync(join(home, "carol", "inbox", s1.data.ref.split("/")[1] + ".md")), "the letter really is in carol's inbox");
  const s2 = await a.sendMail({ from: "owner", to: "@helpers", subject: "Sent to a logical address", need: "回复", body: "Who takes this?" });
  ok(s2.ok, "send to @helpers is accepted", JSON.stringify(s2));
  const s3 = await a.sendMail({ from: "owner", to: "ghost", subject: "x", need: "", body: "" });
  is([s3.ok, s3.error && s3.error.code], [false, "refused"], "an unknown recipient is refused");
  ok(s3.error && /ghost/.test(s3.error.message), "…with the backend's own reason", s3.error && s3.error.message);
  const s4 = await a.sendMail({ from: "nobody", to: "carol", subject: "x", need: "", body: "" });
  is([s4.ok, s4.error && s4.error.code], [false, "not_found"], "an unknown sender is not_found");

  // ---------- outbox: edit / retract ----------
  const ob = await a.listOutbox();
  ok(ob.ok, "outbox reads");
  const rowBob = ob.data.rows.find((r) => r.id === toBob);
  ok(rowBob && rowBob.status === "pending" && rowBob.to === "bob" && rowBob.body === "Review the draft.", "an unread letter by the operator is listed as pending with its body");
  ok(!ob.data.rows.some((r) => r.id === fromAlice), "a letter sent TO the operator is not in the sent list");
  const e1 = await a.editMail({ to: "bob", id: toBob, subject: "Please review (v2)" });
  ok(e1.ok && e1.data.state === "updated", "edit one field");
  const after = await a.readLetter({ box: "bob", id: toBob });
  ok(after.ok && after.data.subject === "Please review (v2)" && after.data.body === "Review the draft.", "…and only that field changed");
  const e2 = await a.editMail({ to: "bob", id: toBob });
  is([e2.ok, e2.error.code], [false, "invalid"], "an edit with no field is invalid");
  const e3 = await a.editMail({ to: "bob", id: fromAlice });
  is([e3.ok, e3.error && e3.error.code], [false, "not_found"], "editing a letter that is not in that inbox is not_found");
  const t1 = await a.retractMail({ to: "carol", id: toCarol });
  ok(t1.ok && t1.data.state === "retracted", "retract a pending letter");
  const t2 = await a.retractMail({ to: "carol", id: toCarol });
  is([t2.ok, t2.error.code], [false, "not_found"], "retracting it again: it is gone from the inbox, not_found");
  const ob2 = await a.listOutbox(); ok(!ob2.data.rows.some((r) => r.id === toCarol), "a retracted letter leaves the sent list");

  // ---------- a letter a channel already accepted: the backend locks it, the client must show that and not offer it ----------
  const dave = S.mail.find((m) => m.id === `dave/${toDave}.md`);
  is(dave && dave.delivery, "reminded", "a letter the channel accepted is 'reminded', not 'waiting' and not 'consumed'");
  const rowDave = ob.data.rows.find((r) => r.id === toDave);
  is(rowDave && rowDave.status, "locked", "the sent list marks it locked");
  ok(rowDave && /已送达/.test(rowDave.reason), "…with the backend's reason", rowDave && rowDave.reason);
  const e4 = await a.editMail({ to: "dave", id: toDave, body: "changed" });
  is([e4.ok, e4.error && e4.error.code], [false, "refused"], "editing a locked letter is refused by the backend even though the client asked");
  ok(e4.error && /已送达/.test(e4.error.message), "…and the refusal carries the backend's reason", e4.error && e4.error.message);
  const t3 = await a.retractMail({ to: "dave", id: toDave });
  is([t3.ok, t3.error && t3.error.code], [false, "refused"], "so is retracting it");
  const still = await a.readLetter({ box: "dave", id: toDave });
  ok(still.ok && still.data.body === "Taken.", "the locked letter is untouched");

  // ---------- ack / archive ----------
  const k1 = await a.ackMail({ box: "owner", id: fromAlice, note: "seen, will answer" });
  ok(k1.ok && k1.data.state === "recorded", "ack records a receipt", JSON.stringify(k1));
  ok(!existsSync(join(home, "owner", "inbox", fromAlice + ".md")), "…and the original left the inbox (it is filed, not 'done work')");
  const k2 = await a.ackMail({ box: "owner", id: fromAlice });
  is([k2.ok, k2.ok && k2.data.state], [true, "already"], "a second ack answers 'already': no second receipt");
  const own = send("owner", "bob", "File me", "仅告知", "x\n");
  const f1 = await a.archiveMail({ box: "owner", id: own });
  ok(f1.ok && f1.data.state === "moved", "archive moves inbox → done");
  const f2 = await a.archiveMail({ box: "owner", id: own });
  ok(f2.ok && f2.data.state === "already", "archiving again is 'already', nothing moves");
  const f3 = await a.archiveMail({ box: "owner", id: "missing-one" });
  ok(!f3.ok && f3.error.code === "not_found", "archiving an unknown letter is not_found");

  // ---------- the backend's own guards ----------
  const evil = createHttpAdapter({ baseUrl: base, fetchImpl: (u, o) => fetch(u, { ...o, headers: { ...(o && o.headers), Origin: "http://evil.example" } }) });
  const g1 = await evil.sendMail({ from: "owner", to: "carol", subject: "csrf", need: "", body: "" });
  is([g1.ok, g1.error.code], [false, "denied"], "a write from a foreign Origin is refused by the backend (denied)");
  ok(/same origin|POSTOFFICE_PANEL_BASE_URL/.test(g1.error.message), "…and the message says how to host the client");

  const own2 = createHttpAdapter({ baseUrl: base, fetchImpl: (u, o) => fetch(u, { ...o, headers: { ...(o && o.headers), Origin: base } }) });
  const g2 = await own2.sendMail({ from: "owner", to: "carol", subject: "same origin", need: "", body: "" });
  ok(g2.ok, "the backend's own origin is accepted");

  // ---------- network: reads fail, writes are 'outcome unknown' ----------
  const dead = createHttpAdapter({ baseUrl: `http://127.0.0.1:${await freePort()}`, timeoutMs: 800, writeTimeoutMs: 800 });
  const n1 = await dead.getSnapshot(); is([n1.ok, n1.error.code, !!n1.error.outcomeUnknown], [false, "network", false], "an unreachable backend on a read is a plain network error");
  const n2 = await dead.sendMail({ from: "owner", to: "carol", subject: "x", need: "", body: "" });
  is([n2.ok, n2.error.code, n2.error.outcomeUnknown, n2.error.retryable], [false, "network", true, false], "…and on a write it is 'outcome unknown', not retryable by itself");

  // ---------- master has no role-diff: the adapter learned that from the state and does not call it ----------
  const off = await a.previewRoleChange({ name: "x", candidates: ["alice"] });
  is([off.ok, off.error.code], [false, "capability_missing"], "role-diff is not called on a backend whose state has no role_cards");
} catch (e) { console.log("BAD  - harness error:", e && e.stack || e); bad++; }
done(bad ? 1 : 0);
