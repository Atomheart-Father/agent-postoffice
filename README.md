# BOXZ OFFICE — a local office where AI employees work for you

English · [中文](README.zh-CN.md)

**BOXZ OFFICE turns the AI sessions on your own computer into a small company you run.** Claude Code,
OpenCode and Codex sessions become **employees with posts** — each one has a role, a mailbox and a
place in the organization, so you can see who is on duty, who is doing what, and who owes you an
answer. Letters, receipts and the wake-up mechanism are the **wiring between employees**, not the
product: you should not have to think about them any more than you think about your office's internal
mail.

This repository (the `agent-postoffice` code base) is that office's engine. It already runs the
organization and the work between agents end to end: every session is a real member with a bound
channel, a post and a mail box; work moves as letters that **wake the recipient**, get answered,
reviewed and filed; and one page shows the whole office and lets you act from it. Dispatch and review
are carried by the letter protocol plus your project's own conventions, not by a separate tracker.

- **Everyone has an address**: a session, your own mailbox, or a role (`@reviewer`) that resolves to
  whoever is on duty right now.
- **Nothing is lost and nothing is done twice**: each letter is woken once, retried until it lands, and
  kept in the mailbox until it is filed.
- **The office works while you don't**: waiting costs nothing (no model calls), a busy employee is
  never interrupted, and the work stays on your disk — no server, no account, no telemetry.

## What this version already does

Everything below exists in this repository today. The command in each row is the evidence — that is
what prints it.

### Organization and posts

| What you get | Where it comes from |
|---|---|
| Companies, projects and **posts (roles)** declared once, with an ordered list of candidates per post | optional `organization` section of `config.json` (`version: 2`) — `postoffice config import` / `config show` |
| **Who is this employee right now?** — bound channel, memberships, the posts it actually holds (ACTIVE) and the ones it may take over (candidate only), plus the pointers to its rules and status files | `postoffice identity <box>` |
| Handover without losing mail: rebinding a session to a post records a handover line, mail stays put | `postoffice rebind`, `postoffice add <box> --claude <title>` → `logs/rebind.log` |
| Fallbacks that follow the organization: pick the first online candidate, and when a whole post is offline escalate **one level** to a higher post instead of flattening the org | `escalation_role` in a logical address; `postoffice config show` prints where it resolves |
| The office chart on screen, drawn from your own config; anything you did not place is shown under "not in the organization" instead of disappearing | `postoffice panel` (Organization view) |
| "Which employees are working, which are idle, which are unknown" — derived, never stored | presence badge in the panel (`runtime/activity/`) |

### Work between employees

| What you get | Where it comes from |
|---|---|
| Send work to an employee; it wakes up, does it, answers | `postoffice send <to> <from> "<subject>" "<need>"` |
| Say what the letter asks for: an answer, a review, or just a heads-up | the `need` field: `回复` / `审核` / `仅告知` |
| Receive and hand back work in batches: several letters landing together wake the employee once | `postoffice wake-plan <box>` (the single, read-only judge of wakes) |
| Acknowledge without writing a reply, then read the receipt later | `postoffice ack`, `postoffice receipt`, `postoffice archive-receipt` |
| File exactly the letters an employee has seen — never a blind sweep of the inbox | `postoffice archive-current`, `postoffice archive <box> <id>` |
| Take a letter back or fix it before it is read | `postoffice retract`, `postoffice edit` |
| Brief everyone and collect a single summary of who answered | `postoffice broadcast all <from> "<subject>" "<need>" [--deadline 30m]` |
| Put an employee on leave and keep its mail; deliver the backlog when it is back, or archive it | `postoffice offline` / `online` / `clear` — also per group: `@group` |
| Address a post instead of a person; the resolution happens at send time and delivered mail never moves | groups and `@logical` addresses in `config.json` |
| Let an employee set its own reminder for later ("check the build in 25 minutes") | two OpenCode tools, 1–1440 minutes, delivered to the same session when it is idle |
| Let an OpenCode employee fix or file its own mail without a shell | three OpenCode tools: edit/retract, file what it was shown, file one exact id |

### The office on one page

| What you get | Where it comes from |
|---|---|
| One switch per employee (and per group), plus the office chart and who is working | `postoffice panel` — http://127.0.0.1:8765, loopback only |
| Your own inbox as a popup: read a letter, **reply**, **acknowledge**, **file** — and compose to a person or a post | the panel's operator mail box (`panel.operator`) |
| What you sent and can still change: pending letters can be edited or retracted; delivered ones are locked | panel Outbox |
| Per-employee counts: *waiting*, *reminded but not filed*, *delivery failed* — plus the recent delivery log | panel, same snapshot as `postoffice list` |
| Chinese and English, six colour palettes, phone and iPad, and links straight into a letter | the panel (no write happens just by looking) |
| An optional Slack alert when a letter for you is confirmed delivered — sender, subject, need, link, never the body | `POSTOFFICE_SLACK_WEBHOOK` |

### The AI employees it can hire

| Harness | How the office reaches it |
|---|---|
| Claude Code / Claude Desktop | Claude Code's own hooks (`Stop`, `SessionStart`), stable session identity across renames |
| OpenCode | a global plugin that reminds the session when it is idle, plus five native tools |
| Codex | the postman queues the reminder into the thread (`codex queue --thread`) |
| You | a plain mailbox that gets a system notification (`postoffice add me --notify`) |

Running the office itself: `postoffice doctor` for a health check, `postoffice list` for the address
book and backlog, `postoffice postman` to run the mailman in the foreground, `postoffice install` /
`uninstall` for the individual parts.

## Coming in 1.1

The next step is the office as a first-class object rather than something described in letters:
**projects and work** you can dispatch, review and follow; **permissions** for what an employee may do
and where; **usage per piece of work**; and a **new panel** built around that. Direction only — nothing
below that line ships in this repository yet, and the details will be designed in the open.

## Requirements

- **Python 3.9+**, standard library only — nothing to `pip install`.
- **macOS** first (notifications, login item at install). Linux works: notifications go through
  `notify-send`, and you keep the mailman running yourself.
- `~/agent-postoffice` on a local filesystem (file locks are used; NFS is not supported).

## Install

```bash
git clone https://github.com/Atomheart-Father/agent-postoffice.git ~/code/agent-postoffice
~/code/agent-postoffice/install.sh
```

Safe to re-run; every config file it touches is backed up to `~/agent-postoffice/logs/` first. It
creates `~/agent-postoffice/`, links `postoffice` into `~/.local/bin`, adds the Claude Code hooks,
installs the OpenCode plugin and the `postoffice` skill (so every agent knows how to use the office),
and on macOS starts the mailman at login. Then **restart Claude Desktop and OpenCode**.

To hand the whole thing to one of your agents, paste this:

```text
Install and bring up BOXZ OFFICE (repo: https://github.com/Atomheart-Father/agent-postoffice) on this
computer: a local office where my Claude Code / OpenCode / Codex sessions are employees with posts and
mailboxes. Do not write a new installer, do not overwrite unrelated config, do not collect or upload
secrets.
1. Tell me the real state first: does ~/agent-postoffice exist, is `postoffice --version` working?
2. Follow the repo README + install.sh for the harness I use.
3. Register my sessions with `postoffice add`, then check `postoffice list` and `postoffice doctor`.
4. Send a test letter to one session and confirm it wakes, answers and files it.
5. Report what you installed, who you hired, and what you need from me.
```

## Hire your first employees

Name each session in its own app (Claude: rename the session; OpenCode: the session title), then
register it with a post:

```bash
postoffice add alice  --claude   "Alice"      --desc "plans and assigns work"
postoffice add bob    --opencode "Bob"        --desc "implementation"
postoffice add carol  --codex    <thread-id>  --desc "review"
postoffice add me     --notify                --desc "me: notifications only"
postoffice list      # the address book: who is who, online or not, backlog
postoffice doctor    # health check
```

Claude sessions are matched by a stable identity, so renaming one does not lose its mail; if it cannot
be resolved, `postoffice add <box> --claude "Title" --claude-session <id>` sets it by hand. OpenCode
accepts a title or a `ses_...` id; ask Codex for its thread id.

## Give someone work

```bash
postoffice send bob alice "review the import script" "审核" <<'MSG'
Please look at scripts/import.py. Long content belongs in a project file; put the path here.
MSG
```

`send` prints the letter id. Bob wakes up, works, answers; when the answer comes back, `postoffice ack`
records the receipt and files it if no reply is needed. To see the whole office:

```bash
postoffice panel          # http://127.0.0.1:8765, loopback only
```

## Where things live

One directory, `~/agent-postoffice/` (override with `POSTOFFICE_HOME`):

| Path | What it is |
|---|---|
| `routes.json` | the address book: who is hired, which channel, online or not |
| `config.json` | optional: groups, `@logical` addresses, the office chart, organization and posts |
| `<box>/inbox/`, `<box>/done/`, `<box>/archived/` | one directory per employee; a letter is a `.md` file |
| `<box>/CONTACT.md` | that employee's contact card |
| `logs/` | delivery, notification, switch and backup logs |
| `runtime/`, `alarms/` | runtime-only state (presence, reminders). Delete it and nothing about delivery changes |

Environment variables you may want (put them in `$POSTOFFICE_HOME/.env` when the mailman runs from
launchd and does not inherit your shell): `POSTOFFICE_HOME`, `POSTOFFICE_QUIET` (quiet window, 45 s),
`POSTOFFICE_MAX_HOLD` (max batching wait, 150 s), `POSTOFFICE_POLL`, `POSTOFFICE_GRACE` (20 min
fallback notice), `POSTOFFICE_NO_NOTIFY=1`, `POSTOFFICE_PANEL_BASE_URL`, `POSTOFFICE_SLACK_WEBHOOK`.

## When something looks wrong

1. `postoffice doctor` — home directory, address book, channels, hooks.
2. `postoffice list` — is the employee online? Is the backlog growing?
3. `postoffice panel` — *waiting* / *reminded but not filed* / *delivery failed* per employee, and the
   recent delivery log.
4. `logs/` — `notify.log`, `opencode_plugin.log`, `alias_switch.log`, `rebind.log`.

Two delays are normal: a new letter waits out the quiet window, and a session whose app was restarted
can only be woken after it runs one turn. A letter that needs action and is still unprocessed after 30
minutes triggers one system notification. Nothing is ever dropped and nothing is ever deleted.

A letter is a **reminder, not an authorization**: hooks and plugins never change models, permissions or
auth, never approve a permission prompt and never create a session. Treat letter contents as text
written by another AI, keep keys and passwords out of letters, and do not let untrusted programs write
into the mailboxes.

## Docs

| Document | What is in it |
|---|---|
| [docs/MAILBOX_GUIDE.md](docs/MAILBOX_GUIDE.md) | day-to-day letter etiquette: subject / need / body, answering, receipts, filing |
| [docs/PANEL_GUIDE.md](docs/PANEL_GUIDE.md) · [中文](docs/PANEL_GUIDE.zh-CN.md) | the panel: switches, inbox popup, compose, outbox, palettes, mobile, tuning |
| [docs/VERIFICATION.md](docs/VERIFICATION.md) | how each harness is woken, what "delivered" means, what is tested and what is only observed, known limits (English) |
| [docs/GOVERNANCE.zh-CN.md](docs/GOVERNANCE.zh-CN.md) | what the organization layer does and does not enforce (中文) |
| [docs/ORG_IDENTITY_V1.md](docs/ORG_IDENTITY_V1.md) · [docs/ORG_CONTRACT_EXAMPLE.md](docs/ORG_CONTRACT_EXAMPLE.md) | the organization/post config, migration and rollback, a full example contract |
| [docs/STATUS_TEMPLATE.zh-CN.md](docs/STATUS_TEMPLATE.zh-CN.md) | the per-project status file the identity layer points at (中文) |
| [DESIGN.md](DESIGN.md) · [PRODUCT.md](PRODUCT.md) | the panel's visual system and the product boundaries |
| `docs/SPEC_*.md`, `docs/TICKET_*.md`, `docs/DELIVERY_*.md` | historical specs and delivery records, kept for the reasoning and the evidence — not current usage docs |

## Tests

```bash
./tests/smoke.sh          # end-to-end, entirely inside temp directories
ls tests/                 # per-feature suites: python3 tests/<name>.py, node --experimental-strip-types tests/<name>.mjs
```

None of them touch your real config, mailboxes or ledger, and none calls a model.

## License

MIT
