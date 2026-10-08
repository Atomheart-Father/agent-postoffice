# Panel guide: use, optimise, maintain

> 中文: [PANEL_GUIDE.zh-CN.md](PANEL_GUIDE.zh-CN.md). Keep this file in step with `panel/index.html`: change the page's behaviour, change this guide.

The panel (`postoffice panel`, `http://127.0.0.1:8765` by default) is the **human console**, not a second post office. It reads `/api/state`; every write goes through the same core as the command line (switches, archive, ack, send, edit/retract). Close it and the post office keeps running.

![Organization](screenshots/org-en.png)

## 1. Daily use

### 1.1 Where to look

| You want to know | Look here |
| --- | --- |
| How many letters wait for me | The red number on the operator button (top right) and the big red button at the top of the Organization page (same thing) |
| Who is online / working | The **lamp** and the **word** next to every seat (1.2) |
| Which team has letters piling up | The line under each column title ("n seats, n online, n letters") and the red number on a seat |
| Cut or restore a session | Harness page: press its key (1.4) |
| How letters move between people | Every few seconds a small letter flies along the wires from the sender's seat to the recipient's seat |

The accent colour (red, or the accent of the palette you chose) means exactly one thing: **a letter is waiting**. It is never used for errors or for "online".

### 1.2 Reading the Organization page

- **Lamp**: square = AI session, round = human; filled = online; hollow = offline; grey filled = idle; slow blink = working; diamond = a team (no mailbox of its own). A word always sits next to it (Working / Idle / Unknown / Human / offline): **status never relies on colour**.
- **Wires**: from the root to each column and to each seat, rounded elbows; dashed wires lead to offline seats. Hover a seat and its path to the root thickens.
- **Columns**: numbered 01, 02… = the 1st, 2nd… child of the root in the config.
- **Not in the organization**: registered mailboxes the tree does not mention. The panel never guesses a department for them and never edits the config.
- **No organization configured**: mailboxes are grouped by the app they run in, and the page says why.
- **Unknown**: that session has no activity record (a hand-written channel, for example). Not a fault.

### 1.3 The boss inbox (popup)

The big red button, the operator button, or any seat opens a **centred window** (full screen on phones):

- Left: the letters in that mailbox (**Inbox**), or the letters the operator sent that are still observable (**Outbox**).
- Click a letter and its text **covers the right-hand reading area** (full screen on phones). Three buttons at the bottom:
  - **Reply (send reply & file)**: sends a reply, then files the original; **no extra receipt**. If the reply went out but filing failed, only "Retry filing" is offered: a second reply is never sent.
  - **Acknowledge & file**: sends a receipt to the sender and files the original (already handled = idempotent notice).
  - **File only**: the letter moves to `done/`, no receipt.
- Fixed at the bottom of the window: **Compose**, **Switch off/on**, **Archive all** (confirmed; archives the whole inbox, files stay in `archived/`).
- Outbox letters that are not yet delivered can be **edited or retracted**; delivered or unknown ones show the reason and no buttons.
- Close with the **Close** button, a click on the dimmed area, or `Esc`. Esc closes the top layer first: compose → activity drawer → letter → inbox window. While the window is open the page behind does not scroll, cannot be clicked or tabbed into; on close, focus returns to the control you used.
- A half-written letter is not lost by clicking the dimmed area (the card only shakes); Cancel, Close or Esc discard it on purpose.

![Inbox popup](screenshots/letter-en.png)

### 1.4 Harness

![Harness](screenshots/harness-en.png)

- One section per **channel** (grouped by the real route method), one **key per session**: solid dark = on, dashed hollow = off. **Press a key to flip it.**
- Off = that session is offline: letters are still kept locally, nothing is sent to it; switch it back on and the backlog is delivered within 10 s. To skip the backlog, press "Archive all" in its mailbox first.
- **Groups** (`groups` in `config.json`) are the cards above, with All on / All off; hover one and its members' keys light up while the rest dim. A section shows its own All on/off **only when its members are exactly a configured group's members** (partial overlap does not count, to avoid collateral switching).
- Switches change delivery only. Nothing is deleted.

### 1.5 Everything else

- **Activity** (left drawer): where each logical address points now, switch history, broadcast progress, the latest delivery log.
- **Compose**: in this window the sender is always the operator (the HTTP API itself accepts any registered `from`, see docs/GOVERNANCE.zh-CN.md); the recipient is a mailbox name or `@logical-address` (with suggestions); never a group.
- **中文 / English**: redraws only, no write request; user content (mailbox names, subjects, bodies, logs) is always shown verbatim.
- **Appearance** (six small squares, top right): Paper / Night / Mist / Blueprint / Pine / Ember. There is no "follow the system" — the palette you pick is the palette you keep, on reload and when you arrive from a Slack link. Stored in this browser only.

  ![Six palettes](screenshots/themes.png)

- **Phone / iPad**: same page. The bar wraps, the inbox window and letters go full screen, keys use two columns. Links in Slack alerts open the right mailbox or letter (`#/mail/<mailbox>` or `#/mail/<mailbox>/<letter-id>`).

  <img src="screenshots/mobile-en.png" width="260" alt="Phone">

## 2. How the page is generated from config

The page contains **no company, department or app names**. Everything comes from `/api/state`:

| On screen | Source |
| --- | --- |
| Tree, columns, seat names | `panel.organization` in `config.json` (`mailbox` / `members` / `label` / `children`); a seat's lamp and word come from that mailbox's `online`, `activity`, `app` |
| Columns when no organization | each mailbox's `app` |
| Red numbers, the big red button | mailbox `counts.waiting`; the operator is `panel.operator` |
| Letters in flight | `pending[]` entries with status waiting, flying from the `from` seat to the recipient's seat (only if both are on the tree) |
| Harness sections and keys | each mailbox's route `method`; groups = `groups` |
| Names on the page | `panel.organization.label` / seat `label`; else `identity.profile.display_name`; else the mailbox name |

Minimal example (any shape works and is laid out by the same rules; verified with a 15-mailbox example, more mailboxes follow the same rules and only make the page longer):

```json
{
  "version": 1,
  "panel": {
    "label": "My studio",
    "operator": "boss",
    "organization": {
      "mailbox": "boss", "label": "Boss",
      "children": [
        {"mailbox": "reporter", "label": "Newsroom"},
        {"label": "Backend", "members": ["be-a", "be-b"],
         "children": [{"mailbox": "be-q", "label": "QA", "children": [{"mailbox": "be-c", "label": "Build"}]}]}
      ]
    }
  }
}
```

`label`, `operator` and `organization` are all required in `panel`. A node has either `mailbox` (one seat) or `members` (a team whose members are seats), and may have `children`; a mailbox appears once in the tree; no logical addresses. A broken `panel` only puts an error note at the top of the Organization page: sending, aliases, groups and Harness are unaffected.

## 3. Optimisation

Already done in the page:

- Polls `/api/state` every 5 s; **unchanged HTML never touches the DOM**, and changed HTML keeps the window's scroll position and focus.
- The contour-line canvas draws at most one frame per 50 ms and stops when the tab is hidden; a letter flies every 5.2 s; with a hidden tab or the OS "reduce motion" setting nothing flies or blinks and the background is one still frame.
- No requests to external resources (it only talks to the local panel API), no build: one HTML file; the font (Instrument Sans, SIL OFL 1.1, Latin subset ≈ 54 KB) is embedded as base64; Chinese uses system fonts.
- The server re-reads `panel/index.html` on every request: edit, refresh the browser, no restart.

What you can do:

- Remote viewing: keep the panel on `127.0.0.1`, expose it with `tailscale serve`, set `POSTOFFICE_PANEL_BASE_URL` (see the README).
- Many mailboxes (dozens): split them into departments in `panel.organization` instead of hanging everyone off the root, so each column fits a screen.
- Quieter: pick Paper or Mist and turn on the OS "reduce motion".
- Zero distraction: skip the Organization page and use the Slack alert link straight into the inbox.

## 4. Maintenance

### 4.1 File layout (`panel/index.html`, one file)

1. `<style>`: palette variables (12 roles × 6 palettes) → bar → Organization → Harness → dialogs (inbox / letter / drawer / compose) → tablet and phone → reduced motion.
2. `<style id="font">`: the embedded font (one base64 line, do not hand-edit).
3. A tiny `<head>` script that sets `data-theme` before first paint so a dark OS never flashes light.
4. `<body>`: bar, `#boxes` (Organization + the inbox window), `#view-hk` (Harness), drawer, letter, compose.
5. The main script: string table `STR` (zh + en) → themes → Organization render → wires and effects (optional) → outbox / window / Harness render → letter and layer manager → compose → `load()` poll → delegated events.

### 4.2 Common changes

- **Wording**: edit both languages of `STR`; keys must exist in both (tested). Mailbox names, subjects, bodies and logs are never translated.
- **A new palette**: add a `[data-theme=name]{…}` block (the same roles as the existing six), a `data-theme-choice="name"` button in `#theme`, its `.themes [data-theme-choice=name]` swatch, the name in `PALETTES`, and `themeName` in `STR` (both languages). Block 29 of `panel_console_ui_test.mjs` checks every palette defines every role.
- **Change looks, not behaviour**: colours only through the role variables, never hard-coded in a component; the accent (`--red`) only means "a letter is waiting", errors use `--bad`.
- **A new field on a seat or key**: add it in `seatHTML()` / `harnessHTML()` and pass user content through `esc()`.

### 4.3 Contracts the tests pin

- All user content is escaped; mailbox names, subjects and paths are shown verbatim.
- Display-only controls (language, palette, tabs, opening/closing the window) make **zero write requests**.
- Reply: send first; only on success file the original; if filing fails only "Retry filing" exists: never a second send, never an ack.
- The operator always shows "Human", never an invented "Idle".
- Status = lamp + word, never colour alone.
- Polling never resets the window's scroll or steals focus; it never brings a closed window back; a deep link is consumed once.
- Element ids and `data-*` hooks (`#ins`, `data-ins-close`, `data-ins-tab`, the compose ids…) and `STR` keys are referenced by tests; rename them together with the tests.
- At 390 / 430 / 768 / 820 / 1024 / 1180 / 1280 / 1440 px the page must not scroll horizontally.

### 4.4 Verification checklist

```sh
for t in panel_console_ui panel_i18n panel_letter_ui panel_mobile panel_org_render; do node tests/${t}_test.mjs; done
python3 tests/panel_presentation_test.py          # panel config validation and isolation
./tests/smoke.sh                                  # everything
# open every surface in a real browser (needs playwright):
PANEL_URL=http://127.0.0.1:8765/ NODE_PATH=<node_modules with playwright> node tests/layout_probe.mjs
```

Then look at the Organization page, Harness and the window at 390 and 1440 px, in three palettes, and once with the OS "reduce motion" on.

### 4.5 Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Blank page | Open the browser console; usually a script syntax error. `node tests/panel_console_ui_test.mjs` reports it when it loads the script |
| Red bar "Cannot reach the panel service" | `postoffice panel` is not running or the port changed (`--port`) |
| Edited the page, nothing changed | The server re-reads the file per request: hard-reload the browser; if still nothing, check you edited the repo copy that is being served |
| Error note on top of the Organization page | The `panel` section of `config.json` has a problem; the note names the first one; nothing else is affected |
| A seat says "is not a registered mailbox" | The tree names an unregistered mailbox: `postoffice add` it or fix the config |
| Everything says Unknown | Those sessions have no activity record (the plugin/hook is not installed, or a hand-written channel) |
| Wires look misaligned | They redraw after fonts load; if one still looks off, resize the window and report it |
