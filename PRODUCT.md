# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Single file: `panel/index.html` (inline CSS and JS, no build step, no framework). Served by `postoffice panel` on 127.0.0.1 (optionally exposed through a Tailscale tunnel). Fonts may be embedded in the file (owner's decision); nothing is loaded from the network.

## Users

A person who runs several AI agent sessions (Claude Code, OpenCode, Codex) on one machine and is their organization's only human. Today that is one person (the maintainer), with the panel open all day on a Mac and opened on a phone or iPad when a Slack alert arrives. Because the tool is open source and the page renders whatever the user's `config.json` describes (any organization shape, any number of mailboxes, any labels, any groups and logical addresses, zh or en), the real audience is every such operator, each with a different organization behind the same page.

## Product Purpose

The human control surface of agent-postoffice, a local post office for AI sessions: letters are files, sessions wake when mail arrives. The panel lets the human see who is online and what each session is doing, work through their own inbox (reply, acknowledge, file), write letters, edit or retract letters that have not been accepted yet, and switch sessions and groups on and off. Success: the operator answers "who needs me, who is working, is anything stuck" in one glance, and clears their inbox in a few taps from a phone.

## Positioning

A post office whose citizens are agents and whose single human is the postmaster. It is not a chat client, not a monitoring dashboard and not a ticket tracker: it shows mail and presence, and the actions are the ones a postmaster has (read, answer, file, hold a route, open or close a counter).

## Operating Context

Letters, mailboxes, status switches, logical addresses (`@alias`), groups, a derived Operational Outbox (not a permanent sent history), per-mailbox runtime presence (Working / Idle / Unknown / Human, with duration), an optional organization tree from `config.json` (`panel.organization`), identity and rules per role, broadcasts with acknowledgement progress, a delivery log, and switch history. The page polls `/api/state` every 5 seconds. Locale follows the browser (zh or en) with a manual switch; appearance is System or one of six palettes (Paper, Night, Mist, Blueprint, Pine, Ember).

## Capabilities and Constraints

- Frozen behaviour that tests pin: the string tables (`STR`, both languages, same keys), ids and `data-*` hooks used by the UI tests, escaping of all user content, zero write requests from display-only controls, the reply partial-failure rules (never send twice, never ack), `.sug` absolute dropdown, `.modal .card` max-width, the operator workbench is a popup on every width (`.inspector` is 100vw at ≤900px), safe-area insets, 16px form controls, 44px targets.
- Mailbox names, subjects, bodies, paths and log lines are shown verbatim and never translated.
- Must work for 1 mailbox or 60, flat or nested organizations, no organization at all (flat grid), long unbroken strings, CJK and Latin mixed.
- Phone and iPad portrait are first-class (390 to 820 px); wide desktop is the home screen.
- Undecided: whether runtime presence ever gets history; whether the Activity drawer becomes a view of its own.

## Brand Commitments

Name: agent-postoffice; the page title is "联络总站" / "Post Office". The owner rejects: blue-violet gradients, glass, neon, "AI aesthetic"; uppercase, mono, card and dashboard looks used as costume; a sterile terminal look; anything that merely looks tidy but ordinary. The owner wants design-forward, premium, specific work that is still readable and finished. The earlier rule that the panel must not look like the owner's studio site is superseded: the panel has its own identity (see DESIGN.md) and the studio site's gallery look is only the fallback if a redesign fails.

## Evidence on Hand

Real screenshots of the current panel in `docs/screenshots/` (org, harness, letter, compose, zh and en). Real behaviour and data shapes in `docs/` and `tests/`. No logo or brand assets exist for the project.

## Product Principles

1. Presence first: who is here and what they are doing outranks everything except mail that waits for the human.
2. One human, many agents: the interface speaks to the postmaster and never pretends the operator is an agent (the operator is always HUMAN, never IDLE).
3. Never lose a letter, never double-send: every action states what it will do and what it will not do; destructive paths are explicit.
4. The page is data-driven by config: no assumption about names, depth or count of mailboxes; nothing in the design may depend on the maintainer's own organization.
5. Glanceable on a phone, complete on a desk.

## Accessibility & Inclusion

WCAG AA contrast, visible focus, keyboard path for every action, Esc closes the top layer, reduced motion respected, 44px touch targets, status never conveyed by colour alone (lamp plus word), zh and en equally supported, `prefers-color-scheme` followed unless the user chooses.
