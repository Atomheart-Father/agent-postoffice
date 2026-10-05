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
- **Receipts default to no reply**: `postoffice ack` records a receipt (and files the letter into `done/`) with a metadata-only notification through the existing idle delivery channel — source, original subject and a lookup ID, never the receipt body. Read the body only when needed with `postoffice receipt <box> <id>`. A broadcast becomes one summary: `broadcast` sends each recipient a letter tagged with an ID, they `ack` it, and when everyone has replied (or the deadline passes) the sender gets exactly one summary letter (per-person notes are looked up on demand).
- **Control panel**: `postoffice panel` opens a local web page with one switch per session to cut or restore its connection, plus the backlog, broadcast progress and recent delivery log.
- **Fallbacks**: if a session can't be woken (not open), you get one system notification after 20 minutes; if a reminder went out but the letter is still in the inbox after 30 minutes (session stuck, Codex thread not loaded…), you get one too.
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

Claude sessions are identified by **session title**, so don't rename a session after registering it (if you do, run `add` again). OpenCode accepts a title or a `ses_...` ID. Ask Codex for its thread ID.

## Everyday use

Just tell any session: "use the postoffice skill to send coder a letter asking it to …". It will run:

```bash
postoffice send coder boss "one-line subject" "need: reply / review / FYI" <<'MSG'
Key points. Put long content in a project file and give the path here.
MSG
```

The recipient wakes up, reads, does the work, replies, and moves the letter into its own `done/`.

**Receipt rule (copy that / ack)**: a letter you must answer before you can continue ("need: reply / review") still gets a proper `send` back. For "FYI" letters and the closing copy that, use `ack` instead: it records the receipt and files the letter into `done/`, with a metadata-only notification (source, original subject, lookup ID) through the existing idle delivery channel — the body stays in the ledger. A receipt notification defaults to no reply and no further ack: read it, move it to `done/`, and only follow up with `send` when an omitted part of the original request is needed to continue. `send` prints the **letter ID** (the file name minus `.md`), which is what `ack` takes. A broadcast (`broadcast`) sends one tagged letter per recipient; they ack the broadcast ID, and the sender gets **one** summary once everyone has replied or the deadline passes. To read a receipt body, run `postoffice receipt <box> <id>` (exact ID; read-only — no send, no ack, no moving letters, no waking; an unknown ID or another mailbox's receipt is refused).

| Command | What it does |
|---|---|
| `postoffice panel` | Open the web control panel (listens on 127.0.0.1 only), including broadcast progress |
| `postoffice ack boss 20261004-223334_coder_hello "one line"` | Record a receipt: file the letter into `done/`, notify the sender when idle. For a letter the notification is sent anyway, so `--wake` only changes the subject to `copy that`; for a broadcast (which otherwise sends no per-recipient notification) `--wake` sends one extra `copy that` notification |
| `postoffice receipt boss 20261004-223334_coder_hello` | Read a receipt body by exact ID (read-only; letter ID or broadcast ID); never wakes anyone |
| `postoffice broadcast all boss "subject" "need"` | Send every other mailbox a tagged letter; `--deadline 30m`; acks are summarized into one letter to you |
| `postoffice offline codex1` | Recipient out of quota / away: letters are kept, no reminders |
| `postoffice online codex1` | Back: the backlog is delivered within 10 s |
| `postoffice clear codex1` | Clear backlog: archive unsent letters to `archived/`, never send them (same button in the panel) |
| `postoffice remove coder` | Remove from the address book |
| `postoffice postman` | Run the postman in the foreground (if you don't want it at login) |
| `postoffice uninstall claude` / `postman` | Remove the hooks / the login item |

## How it works

| Recipient | Who wakes it | How |
|---|---|---|
| Claude Code | Claude Code's own hooks | At the end of every turn and when a session starts, a hook launches `postoffice hook` in the background; it waits without calling the model. On new mail it exits with code 2, and Claude Code hands the reminder to the session and wakes it. Command hooks default to a 600 s timeout (observed: killed after 10 minutes), so the installer sets an explicit 7-day `timeout` |
| OpenCode | Global plugin | Checks every 10 s and whenever a session goes idle; only when the session is idle does it send a reminder through OpenCode's own `session.promptAsync`. With several OpenCode instances open, claim files ensure a letter is delivered once |
| Codex | Postman | `codex queue --thread <id>` queues a reminder in the thread |
| You | Postman | System notification |

Everything lives in `~/agent-postoffice/` (override with `POSTOFFICE_HOME`): `routes.json` is the single config; one directory per mailbox (`inbox/`, `done/`, `CONTACT.md`); logs in `logs/`.

## What "delivered" means

The post office distinguishes two things:

- **Reminded**: the hook woke the Claude session / the plugin sent OpenCode a reminder / `codex queue` returned success. This only means the reminder went out.
- **Processed**: the recipient moved the letter into its `done/`.

If a letter was reminded but not processed within 30 minutes, the postman notifies you once. Known case: when a Codex thread isn't loaded, `codex queue` still returns success but the thread won't resume on its own — this alert covers it.

## Verification status

| Item | Status |
|---|---|
| Send/receive, dedup, rate limit, online/offline, clear, ack bookkeeping, receipt lookup, broadcast summaries, install/uninstall, unprocessed alert | 46 automated checks in `tests/smoke.sh` |
| Receipt reminders are metadata-only; `postoffice receipt` is exact-ID, read-only and never leaks another mailbox | `tests/receipt_test.py` (12 checks, incl. legacy notifications and shell-quoted commands) and `tests/receipt_plugin_test.mjs` |
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
- **Delivery stage in the panel**: show "queued / reminded / processed" per letter.
- **Linux**: install the postman as a systemd user service.

## Safety

- A letter is a reminder, not an authorization: the hooks and plugin don't change models, permissions or auth, never approve permission prompts, and never create sessions.
- Treat letter contents as text written by another AI: each session still acts under its own permission settings. Don't let untrusted programs write into the mailboxes.
- Never put keys or passwords in letters.

## Tests

```bash
./tests/smoke.sh                                              # 46 checks, entirely in a temp directory
python3 tests/receipt_test.py                                 # receipt flow: metadata-only reminders (Claude hook and a Codex-queue mock), exact lookup, no cross-box leak, read-only, legacy notifications, broadcast, --wake
node --experimental-strip-types tests/receipt_plugin_test.mjs # OpenCode plugin (mock client, no model calls)
```

None of these touches your real config, mailboxes or ledger, and none calls a model.

## License

MIT
