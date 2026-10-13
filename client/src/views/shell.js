// The frame: brand, the three places (Office, Threads, Work), the HUD, capabilities, language and palette.
// The page behind it is whichever view is active; a stale or failed read never blanks data already on screen.
import { esc, fmt, ago } from "../util.js";
import { t, getLang } from "../i18n.js";
import { PALETTES } from "../theme.js";
import { hudChip } from "./hud.js";
import { note, mockChip } from "./ui.js";
import { officeView } from "./office.js";
import { rolePane } from "./role.js";
import { threadsView } from "./threads.js";
import { workView } from "./work.js";
import { needsYou } from "./office.js";
import { capsDialog, newThreadDialog } from "./dialogs.js";
import { letterDialog, composeDialog } from "./mail.js";
import { deliveryDialog, clearDialog, writesDialog, writesChip, deliveryOn } from "./mailbox.js";

const TABS = [["office", "tab.office"], ["threads", "tab.threads"], ["work", "tab.work2"]];

function bar(snap, S, adapter, now) {
  const need = snap ? needsYou(snap) : 0;
  const L = getLang();
  const mock = adapter && adapter.kind === "mock";
  return `<header class="bar" id="bar"><div class="brand"><b>BOXZ OFFICE</b><span>${esc(snap && snap.company.name || "")}</span></div>
    <nav class="tabs" aria-label="${esc(t("nav"))}">${TABS.map(([k, label]) => `<button type="button" data-act="go" data-view="${k}" data-fk="tab:${k}" aria-pressed="${S.view === k ? "true" : "false"}">${esc(t(label))}${k === "work" && need ? `<span class="counter">${need}</span>` : ""}</button>`).join("")}</nav>
    <span class="sp"></span>
    <div class="aux">${snap ? hudChip(snap.usage, S.hud, now) : ""}
      ${snap && deliveryOn(snap) ? `<button type="button" class="act" data-act="delivery" data-fk="delivery">${esc(t("dl.open"))}</button>` : ""}${writesChip(S)}
      <button type="button" class="act" data-act="caps" data-fk="caps">${esc(t("cap.open"))}${mock ? ` ${mockChip()}` : ""}</button>
      <span class="langs" role="group" aria-label="${esc(t("lang"))}">${["en", "zh"].map((l) => `<button type="button" data-lang="${l}" aria-pressed="${L === l ? "true" : "false"}" data-fk="lang:${l}">${l === "zh" ? "中文" : "EN"}</button>`).join("")}</span>
      <span class="themes" role="group" aria-label="${esc(t("appearance"))}">${PALETTES.map((p) => `<button type="button" data-theme-choice="${p}" aria-pressed="${S.theme === p ? "true" : "false"}" data-fk="theme:${p}" title="${esc(t("pal." + p))}" aria-label="${esc(t("pal." + p))}">${esc(t("pal." + p))}</button>`).join("")}</span></div></header>`;
}

function page(snap, S, adapter, now) {
  if (S.load === "loading" && !snap) return `<div class="pad">${note("loading", t("loading"))}</div>`;
  if (!snap) return `<div class="pad">${note("error", S.err ? S.err.message : t("nk.error"), `<div class="actions"><button type="button" class="btn primary" data-act="reload" data-fk="reload">${esc(t("retry"))}</button></div>`)}${adapter && adapter.kind === "http" ? `<p class="fine">${esc(t("err.mockHint"))}</p>` : ""}</div>`;
  const stale = S.err ? note("error", fmt(t("stale.data"), { s: ago(snap.asOf, now, getLang()) || t("unknown"), e: S.err.message }), `<button type="button" class="btn" data-act="reload" data-fk="reload2">${esc(t("retry"))}</button>`) : "";
  const strip = adapter && adapter.kind === "mock" ? `<div class="strip">${esc(t("mock.strip"))}</div>` : "";
  let main;
  if (S.view === "threads") main = threadsView(snap, S, now);
  else if (S.view === "work") main = workView(snap, S, now);
  else main = `<div class="office${S.roleId ? " with-pane" : ""}">${officeView(snap, S)}${S.roleId ? rolePane(snap, S, now) : ""}</div>`;
  return `${strip}${stale ? `<div class="stale">${stale}</div>` : ""}${main}`;
}

export function shell(snap, S, adapter, now) {
  const layer = S.dialog === "caps" ? capsDialog(snap, S, adapter) : S.dialog === "newthread" && snap ? newThreadDialog(snap, S)
    : S.dialog === "letter" && snap && S.letter ? letterDialog(snap, S, now) : S.dialog === "compose" && snap && S.compose ? composeDialog(snap, S)
    : S.dialog === "delivery" && snap ? deliveryDialog(snap, S) : S.dialog === "clear" && snap && S.clear ? clearDialog(snap, S, now) : S.dialog === "writes" ? writesDialog(S, now) : "";
  return `${bar(snap, S, adapter, now)}<main id="main" tabindex="-1" class="view-${esc(S.view)}">${page(snap, S, adapter, now)}</main>${layer}`;
}
