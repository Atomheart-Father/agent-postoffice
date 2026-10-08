# Design

The panel's visual system. Read with PRODUCT.md (who and why) and docs/PANEL_GUIDE.md (how it is used and maintained).

## Idea

A post office is a tree of people who pass paper along wires. So the Organization page *is* the tree: a root (the human), numbered columns, seats on wires; a letter in transit flies along the wires from sender to recipient; and the one loud colour exists only for "a letter is waiting for someone". Harness is the other half of the job, the switchboard: one physical key per session, pressed to connect or cut it.

## Type

Instrument Sans (variable, `wdth` 75–100 and `wght` 400–700), embedded as a Latin subset. Big numerals and the stat figures use the condensed width (`font-stretch` 75–85%, weight 700); text is 15 px / 1.45. Chinese falls back to PingFang SC / Noto Sans CJK / Microsoft YaHei. No mono-spaced labels, no letter-spaced uppercase.

## Colour: twelve roles, six palettes

`--paper --paper-2 --ink --ink-2 --ink-3 --wire --rule --line (rgb) --line-a --red --on-red --bad --key-on --on-key --on-key-2 --scrim`. Components read only these. A palette is a block of the same roles: Paper `#ecebe7` / `#d5381a`, Night `#131312` / `#f0502e`, Mist `#dfe4e8` / `#2445d4`, Blueprint `#1b3d9a` / `#ffd23f`, Pine `#123c30` / `#ffad2e`, Ember `#2a2622` / `#ff6a3d`. `--red` is the accent and means exactly one thing: a letter is waiting (count boxes, the big button, the selected row marker, the route of a letter in flight, key hover). Errors use `--bad`. "System" resolves to Paper by day and Night when the OS is dark.

## Components

- **Seat**: lamp + name + id + app + activity word + count box. Lamp: square = agent, round = human, diamond = team; hollow = offline, grey = idle, slow blink = working. A word always accompanies the lamp.
- **Wires**: SVG, measured from the laid-out page (so any depth works), rounded elbows, a bus line 30 px above each column head, dashed to offline seats; hover/focus on a seat draws its path to the root in ink.
- **Key** (Harness): solid = on, dashed outline = off; the whole key is the switch. Group cards dim non-members on hover.
- **Dialogs**: head + scrolling body + sticky foot. The inbox window is centred (`min(1080px, 100vw − 64px)`), a letter docks over its reading pane on wide screens; both go full screen at ≤ 900 px.
- **Ground**: a fixed canvas of faint contour curves (one slow period ≈ 2 min, 50 ms throttle, stopped when hidden, one still frame under reduced motion). Curves, never straight, so they are never mistaken for the wires.

## Do and don't

- Do keep one accent meaning; do add palettes as role blocks; do keep every motion optional.
- Don't encode state by colour alone; don't hard-code colours in components; don't add gradients, glass, glow, or card-and-shadow dashboards; don't use uppercase mono micro-labels.

## How it was chosen

Earlier directions (stamp / line-art / signal-flag reskins and coloured-band mockups) were rejected by the owner as ordinary. The accepted direction came from restructuring the page around the organization tree rather than recolouring it. Research basis: single-accent theming in Linear and Mailchimp, Swiss grid-and-grotesque, and the frontend-design guidance (no eyebrow labels, no uppercase mono, no near-black-plus-acid palettes, motion slow and low contrast and always disableable).
