# agent-postoffice — a local post office for AI agent sessions

English · [中文](README.zh-CN.md)

Let **Claude Code (including Claude Desktop)**, **OpenCode** and **Codex** sessions on the same machine send each other mail, and **wake the recipient session automatically when a letter arrives**. No more copy-pasting between windows, and no AI driving the mouse to poke another app.

```
coder (OpenCode) ──writes a letter──▶ ~/agent-postoffice/boss/inbox/xxx.md
                                          │  within 10 s
                                          ▼
                   Claude session "boss" wakes up, reads, works, replies
```

- **Idle costs nothing**: waiting for mail is a tiny Python loop — no model calls, no tokens.
- **Never interrupts**: if the recipient is busy, the reminder waits until its current turn ends; drafts in the input box are untouched.
- **Delivered once**: each letter triggers one reminder, even across restarts; at most 6 wakes per mailbox per 10 minutes, so agents can't spam each other.
- **Online / offline**: a session out of quota? `postoffice offline <name>` — letters are kept locally and not sent; `online` delivers the backlog; `clear` archives it instead (nothing is deleted).
- **Post office only**: agents should talk only through the post office, never call `codex queue` directly — messages that bypass it ignore the offline switch, pile up while the recipient has no quota, and all pop out when it comes back.
- **Receipts default to no reply**: `postoffice ack` records a receipt (and files the letter into `done/`) with a short metadata-only notification through the existing idle delivery channel — source, original subject and one lookup command, never the receipt body. A single receipt is at most three lines; several receipts landing in one wake are merged into one trailing "N receipts" block, with formal letters always first. Read the body only when needed with `postoffice receipt <box> <id>`, and file the notification with `postoffice archive-receipt <box> <id>`. A broadcast becomes one summary: `broadcast` sends each recipient a letter tagged with an ID, they `ack` it, and when everyone has replied (or the deadline passes) the sender gets exactly one summary letter (per-person notes are looked up on demand).
- **Control panel**: `postoffice panel` opens a local web page with one switch per session to cut or restore its connection, plus broadcast progress and the recent delivery log. Each mailbox shows **waiting / reminded-to-file / delivery-failed** counts taken from the same per-letter snapshot as the list (waiting = nothing sent yet, reminded-to-file = the channel accepted the reminder but the letter is still in `inbox/`, failed = that channel gave up and a human has to look). No new state store: reminded comes from the Claude `.seen` file, the plugin's `DELIVERED` rows or the postman's accept records — never from the 20-minute fallback marks, and `FAILED_FINAL` counts as failed, not delivered. The archive button's confirmation lists the total and all three counts (waiting + reminded-but-not-filed + failed delivery needing a human), says it archives **everything** in the inbox while keeping the files under `archived/`, and spells out that waiting letters will not be re-delivered afterwards and failed ones are archived without retrying. The page follows your browser language (Chinese for `zh*`, English otherwise) and has a visible 中文 / English switch that only redraws: it never changes a status, never calls a write endpoint and never wakes a session. Mailbox names, letter subjects, receipt bodies, handover paths and raw log lines are always shown verbatim in the original wording.
- **Fallbacks**: if a session can't be woken (not open), you get one system notification after 20 minutes — the clock starts from the later of when the letter landed and when that mailbox last came online (`online_since`, reset only on a real offline→online transition and seeded at the postman's first run after an upgrade), so offline time never counts. If a reminder went out but a letter that *needs action* is still in the inbox after 30 minutes (session stuck, Codex thread not loaded…), you get one too. Classification: receipt notifications and broadcast summaries never nag; an exact `need: 回复`/`审核` does, an exact `仅告知` doesn't; free text with only positive words (reply/review/handle/change/decide/confirm) nags, only negative words (FYI / no-reply / no action needed) doesn't, and mixed or unrecognized text still nags — we don't claim zero false positives.
- Pure standard-library Python 3.9+, no dependencies. macOS first (Linux works: notifications via `notify-send`, and you keep the postman running yourself).

## Install (three steps)

```bash
git clone https://github.com/Atomheart-Father/agent-postoffice.git ~/code/agent-postoffice
~/code/agent-postoffice/install.sh
```

The installer (safe to re-run; every config file it touches is backed up to `~/agent-postoffice/logs/` first):

1. creates the post office at `~/agent-postoffice/` and links the command to `~/.local/bin/postoffice`;
2. adds two Claude Code hooks (`Stop` and `SessionStart` in `~/.claude/settings.json`, `asyncRewake`);
3. installs a global OpenCode plugin (`~/.config/opencode/plugins/postoffice.ts`) and lets OpenCode read/write the post office directory;
4. installs the `postoffice` skill into `~/.claude/skills` and `~/.agents/skills` (Codex and OpenCode read these too), so every agent knows how to use it;
5. on macOS, starts the postman at login (launchd).

Then **restart Claude Desktop and OpenCode**.

## Register sessions

First give each session a name in its app (Claude: rename the session; OpenCode: the session title), then register:

```bash
postoffice add boss   --claude   "Boss"       --who "plans and assigns work"
postoffice add coder  --opencode "Coder"      --who "implementation"
postoffice add review --codex    <thread-id>  --who "review"
postoffice add me     --notify                --who "me: notifications only"
postoffice list      # address book: who is who, online/offline, backlog
postoffice doctor    # health check
```

Claude sessions are identified by a **stable identity**: the desktop app sets `CLAUDE_CODE_HOST_SESSION_ID` (`local_…`) on the session process and the hook inherits it, so renames and rewinds no longer lose mail. If it is absent, the hook looks the payload's CLI session id up in `~/Library/Logs/Claude/main*.log` (newest in-line timestamp wins); a CLI-based Claude (`CLAUDE_CODE_ENTRYPOINT` not `claude-desktop`) uses its own CLI session id. The first title match binds the identity automatically, and a *different* identity with the same title never takes over. Set it by hand with `postoffice add boss --claude "Boss" --claude-session <id>`; `postoffice doctor` shows each Claude mailbox's binding. OpenCode accepts a title or a `ses_...` ID. Ask Codex for its thread ID.

## Everyday use

Just tell any session: "use the postoffice skill to send coder a letter asking it to …". It will run:

```bash
postoffice send coder boss "one-line subject" "need: reply / review / FYI" <<'MSG'
Key points. Put long content in a project file and give the path here.
MSG
```

The recipient wakes up, reads, does the work, replies, and moves the letter into its own `done/`.

**Receipt rule (copy that / ack)**: a letter you must answer before you can continue ("need: reply / review") still gets a proper `send` back. For "FYI" letters and the closing copy that, use `ack` instead: it records the receipt and files the letter into `done/`, with a short metadata-only notification (source, original subject, one lookup command) through the existing idle delivery channel — the body stays in the ledger. A single receipt is at most three lines; several receipts in one wake are merged into one trailing list after the formal letters. A receipt notification defaults to no reply and no further ack: read it, file it with `postoffice archive-receipt <box> <id>` (exact ID, idempotent; it never reads the body, sends, or wakes), and only follow up with `send` when an omitted part of the original request is needed to continue. `send` prints the **letter ID** (the file name minus `.md`), which is what `ack` takes. A broadcast (`broadcast`) sends one tagged letter per recipient; they ack the broadcast ID, and the sender gets **one** summary once everyone has replied or the deadline passes. To read a receipt body, run `postoffice receipt <box> <id>` (exact ID; read-only — no send, no ack, no moving letters, no waking; an unknown ID or another mailbox's receipt is refused).

| Command | What it does |
|---|---|
| `postoffice panel` | Open the web control panel (listens on 127.0.0.1 only), including broadcast progress, group switches and where each logical address resolves |
| `postoffice config import ./postoffice-config.json` | Install `config.json`: groups + logical addresses (validates first, backs up the old file, atomic whole-file replace — no merge) |
| `postoffice config show` | Read-only: groups, logical addresses, who each one resolves to right now (creates nothing) |
| `postoffice offline @codex` / `online @codex` / `clear @codex` | Switch a whole group at once (`@` + group name); validates every member first, then reuses the single-mailbox rules |
| `postoffice send @project.manager me "subject" "need" ` | Send to a logical address: the first online candidate is picked **at send time** and the resolution is printed; if nobody is online it fails and lists the candidates (nothing is dropped into a candidate inbox) |
| `postoffice ack boss 20261004-223334_coder_hello "one line"` | Record a receipt: file the letter into `done/`, notify the sender when idle. For a letter the notification is sent anyway, so `--wake` only changes the subject to `copy that`; for a broadcast (which otherwise sends no per-recipient notification) `--wake` sends one extra `copy that` notification |
| `postoffice receipt boss 20261004-223334_coder_hello` | Read a receipt body by exact ID (read-only; letter ID or broadcast ID); never wakes anyone |
| `postoffice archive-receipt boss 20261004-223334_coder_hello` | File this box's receipt notification for that exact ID into `done/` (idempotent; never reads the body, sends, or wakes) |
| `postoffice broadcast all boss "subject" "need"` | Send every other mailbox a tagged letter; `--deadline 30m`; acks are summarized into one letter to you |
| `postoffice offline codex1` | Recipient out of quota / away: letters are kept, no reminders |
| `postoffice online codex1` | Back: the backlog is delivered within 10 s |
| `postoffice clear codex1` | Clear backlog: archive unsent letters to `archived/`, never send them (same button in the panel) |
| `postoffice remove coder` | Remove from the address book |
| `postoffice postman` | Run the postman in the foreground (if you don't want it at login) |
| `postoffice uninstall claude` / `postman` | Remove the hooks / the login item |

## Groups and logical addresses (optional)

Without `~/agent-postoffice/config.json` none of this exists and everything else behaves exactly as before. Import a config and you get two things:

```json
{
  "version": 1,
  "groups": { "codex": ["manager_a", "reviewer_a"] },
  "aliases": {
    "project.manager": { "candidates": ["manager_a", "manager_b"],
                         "notify": ["coordinator_a"],
                         "handoff": "/path/to/handoff.md" }
  }
}
```

- **Groups** are just names for existing mailboxes: `offline @codex` / `online @codex` / `clear @codex` reuse the single-mailbox behaviour (no group runtime state, the mailbox status stays the only truth), and the panel gets a small Groups area with an all-on / all-off button through the same status endpoint and local-origin check.
- **Logical addresses** let a sender write `send @project.manager …` instead of hard-coding a manager. The alias picks the first online candidate when `send` runs. Letters already delivered are never moved or re-routed; a pending switch never delays a new letter.
- **Switch announcements**: once a new target has been stable for 60 s (`POSTOFFICE_ALIAS_STABLE` shortens it in tests) the postman confirms the switch once — one regular broadcast to the `notify` boxes (acks are tallied as usual), one `need: FYI` handover note to the new target carrying the handoff *path* (the post office never reads that file), a `logs/alias_switch.log` line with event id, before/after, reason, notify boxes and handover target, plus one system notification. The first sighting only records a baseline (no broadcast) — including when *no* candidate is online, which still waits out the same 60 s before the single "nobody took over" notice instead of firing at startup. A flap inside the stability window is cancelled silently, and a restart keeps the baseline and the de-duplication record. With no online candidate only `notify` and you are told — nothing is dropped into a candidate inbox. Broadcast and handover are de-duplicated per step, so a failed handover resumes without re-broadcasting.
- One switch, one broadcast id, one letter per recipient: after a partial failure the resume only reaches the boxes that never got one, and even if the state did not reach disk the post office checks the recipient's inbox first instead of re-delivering or opening a second broadcast record. Removing an alias from the config drops its baseline right away, so re-adding it starts from a fresh baseline instead of resuming a switch that was never confirmed.
- Import refuses anything it cannot honour: an unknown mailbox, a name that is both a group and an alias, nesting, duplicate members/candidates/notify targets, names with paths, spaces or globs, and duplicate JSON keys. A refused import leaves the old file byte-identical. The same checks run again every time the config is used, not only at import: a config that was hand-edited into something invalid (wrong version, a member listed twice, a notify target that was removed) disables groups *and* logical addresses together with an explanation, while physical mailboxes keep working and no mailbox status is touched on the way out. `postoffice config show` still lists every problem.
- These notes only report a routing change. They grant nobody extra permission, start no work, and are not a new authorisation from the human.

## How it works

| Recipient | Who wakes it | How |
|---|---|---|
| Claude Code | Claude Code's own hooks | At the end of every turn and when a session starts, a hook launches `postoffice hook` in the background; it waits without calling the model. On new mail it exits with code 2, and Claude Code hands the reminder to the session and wakes it. Command hooks default to a 600 s timeout (observed: killed after 10 minutes), so the installer sets an explicit 7-day `timeout` |
| OpenCode | Global plugin | Checks every 10 s and whenever a session goes idle; only when the session is idle does it send a reminder through OpenCode's own `session.promptAsync`. With several OpenCode instances open, claim files ensure a letter is delivered once; a failed delivery releases its claim and the retry re-claims, so two instances never each retry the same letter |
| Codex | Postman | `codex queue --thread <id>` queues a reminder in the thread |
| You | Postman | System notification |

Everything lives in `~/agent-postoffice/` (override with `POSTOFFICE_HOME`): `routes.json` is the single config; one directory per mailbox (`inbox/`, `done/`, `CONTACT.md`); logs in `logs/`. Set `POSTOFFICE_NO_NOTIFY=1` to keep system notifications off — the OpenCode plugin and the postman then only write a log line instead of popping one. The plugin always logs a `通知人：…` line to `logs/opencode_plugin.log` before it notifies; the postman writes to `logs/notify.log` when notifications are suppressed.

## What "delivered" means

The post office distinguishes three things:

- **Waiting**: nothing has been sent yet — the mailbox was offline, the session is busy, or the reminder is still queued.
- **Reminded**: the hook woke the Claude session / the plugin sent OpenCode a reminder / `codex queue` returned success. This only means the reminder went out.
- **Processed**: the recipient moved the letter into its `done/`.

A letter that was reminded but is still in `inbox/` is what the panel counts as "reminded, to file"; `FAILED_FINAL` from the plugin is shown as "delivery failed" instead, because nobody is going to act on it on its own. The archive confirmation lists all three counts and the total, says it archives **everything** in the inbox while keeping the files under `archived/`, and spells out that waiting letters will not be re-delivered afterwards.

If a letter that **needs action** (its `need:` header says reply / review / …) was reminded but not processed within 30 minutes, the postman notifies you once; FYI letters, receipt notifications and broadcast summaries are backlog only and never nag. Known case: when a Codex thread isn't loaded, `codex queue` still returns success but the thread won't resume on its own — this alert covers it.

## Verification status

| Item | Status |
|---|---|
| Send/receive, dedup, rate limit, online/offline, clear, ack bookkeeping, receipt lookup, broadcast summaries, install/uninstall, need-based alerts, stable Claude identity, merged receipts | automated checks in `tests/smoke.sh` |
| Config import validation, group switches, logical-address resolution and fallback, switch announcements (baseline / stable / cancelled flap / nobody / recovery / primary returns / resume after a failed handover), panel groups and buttons, ack by physical letter ID, archive-before-count | the same run of `tests/smoke.sh` (237 checks total, the v1.7 group runs in its own temp post office; six mutants of the new rules were verified to fail these checks) |
| Panel per-letter status and counts (waiting / reminded-to-file / failed), the Chinese/English switch, verbatim escaped user content, confirmation wording covering both statistics | `tests/panel_i18n_test.mjs` (runs the real page script against a DOM stub: default by browser language, manual choice persisted, unusable storage still switchable, switching issues no write call, user content escaped and untranslated; six mutants verified to fail) plus the `tests/smoke.sh` API checks for the three statistics, `.delivered.json` fallbacks and `FAILED_FINAL` |
| The six regression negatives from the review round: full config validation at use time (wrong version / duplicated member / notify target removed all disable groups and logical addresses together with no change to routes or inboxes), a first sighting with nobody online that still waits out the window before one notice, an alias baseline that is really persisted when the alias is removed, one broadcast record per switch that is not replayed even when the delivered list is lost, an empty or malformed `广播:` header rejected without touching the ledger or the letter, and an archive confirmation listing failed deliveries plus the total | block 14 of the same `tests/smoke.sh` (50 assertions, each case in its own temp post office); eight mutants were tried, one of them swapping the whole event step back to the pre-fix implementation |
| Group switches and logical addresses against real provider quotas, and the handover path file | Not verified on a real machine: no real config was imported and no real group was switched (v1.7 is not released yet) |
| Receipt reminders are short and metadata-only; `postoffice receipt` is exact-ID/read-only and `postoffice archive-receipt` files only the matching notification | `tests/receipt_test.py` (15 checks, incl. legacy notifications, shell-quoted commands and archive idempotence) and `tests/receipt_plugin_test.mjs` |
| Claude Desktop: idle for minutes, woken by external mail, processes the letter | Observed repeatedly on a real machine |
| Claude Desktop: without an explicit timeout the hook is killed after 10 minutes | Observed (a v1.0 bug; v1.1 sets 7 days) |
| Claude Desktop: with the long timeout, still wakes after 30+ minutes idle | Measured: watcher alive 33 min, woken 3 s after mail arrived |
| Claude Desktop: Stop or rewind in the app kills the session's background process and its watcher | Observed (see "Known limitations") |
| OpenCode: idle session gets the reminder within 10 s | Observed repeatedly |
| Codex: `codex queue` delivers to a loaded thread | Observed in live use between sessions; not tested in isolation from the postman |

## Known limitations

In these cases a Claude session can't be woken for a while; letters are never lost — the postman notifies you after 20 minutes, and saying anything to that session fixes it:

- **After restarting the Claude app**: each session must run one turn before its watcher is armed.
- **After pressing Stop or rewinding a session in the app**: the app kills that session's background process, the watcher goes with it, and it comes back with the next message.
- **A session completely untouched for 7 days**: the watcher expires. Every turn restarts the 7-day clock.
- **Codex thread not loaded**: `codex queue` still returns success but the thread won't wake; the 30-minute "reminded but unprocessed" alert covers it.
- **After upgrading agent-postoffice**: updating the script in place needs no Claude restart — the existing watcher loads the new code on the next `Stop`/`SessionStart` hook, whereas restarting the Claude app drops every session's watcher until it runs another turn. A running OpenCode instance keeps its old plugin until you restart it. Reload the Claude app only when the hook configuration itself changed (first install, or hooks added/removed).

## Future work

- **Waking a stopped Claude session**: when a session's background process is gone, hooks can't help. Possible approach: relay through the Claude app's own cross-session messaging via an always-on session (costs one model call per relay).
- **Linux**: install the postman as a systemd user service.

## Safety

- A letter is a reminder, not an authorization: the hooks and plugin don't change models, permissions or auth, never approve permission prompts, and never create sessions.
- Treat letter contents as text written by another AI: each session still acts under its own permission settings. Don't let untrusted programs write into the mailboxes.
- Never put keys or passwords in letters.

## Tests

```bash
./tests/smoke.sh                                              # 237 checks, entirely in temp directories
python3 tests/receipt_test.py                                 # receipt flow: metadata-only reminders (Claude hook and a Codex-queue mock), exact lookup, no cross-box leak, read-only, legacy notifications, broadcast, --wake
node --experimental-strip-types tests/receipt_plugin_test.mjs # OpenCode plugin (mock client, no model calls)
node --experimental-strip-types tests/panel_i18n_test.mjs     # panel page: per-letter statistics, Chinese/English switch, verbatim user content
```

None of these touches your real config, mailboxes or ledger, and none calls a model.

## License

MIT
