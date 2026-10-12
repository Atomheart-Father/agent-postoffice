// Token HUD. Official cumulative usage only. Missing is "unknown", never 0, and the total is shown only when the
// source reported one: it is never added up from input / output / cache here. Context occupancy (how full the window
// is right now) is a different number from cumulative use and has its own block, shown only when a reliable value exists.
import { esc, num, ago, isNum } from "../util.js";
import { t, getLang } from "../i18n.js";

export function compact(n) {
  if (!isNum(n)) return null;
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e11 ? 0 : 1).replace(/\.0$/, "") + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1).replace(/\.0$/, "") + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "k";
  return String(n);
}

const unknown = () => `<span class="unk">${esc(t("hud.unknown"))}</span>`;

// The card: the same markup in the popover (top bar) and inline in a thread.
export function hudCard(u, now) {
  if (!u) return `<div class="hud-card"><p class="fine">${esc(t("hud.none"))}</p></div>`;
  const L = getLang();
  const row = (k, v) => `<div class="r"><dt>${esc(t(k))}</dt><dd>${isNum(v) ? esc(num(v, L)) : unknown()}</dd></div>`;
  const ctx = u.context
    ? `<div class="ctx"><div class="r"><dt>${esc(t("hud.context"))}</dt><dd>${esc(num(u.context.tokens, L))} / ${esc(num(u.context.window, L))}</dd></div>
        <div class="ctxbar" role="img" aria-label="${esc(Math.round((u.context.tokens / u.context.window) * 100))}%"><i style="width:${Math.min(100, (u.context.tokens / u.context.window) * 100).toFixed(1)}%"></i></div></div>`
    : `<div class="ctx"><div class="r"><dt>${esc(t("hud.context"))}</dt><dd>${unknown()}</dd></div></div>`;
  return `<div class="hud-card">
    <dl class="figs">${row("hud.input", u.input)}${row("hud.output", u.output)}${row("hud.cacheRead", u.cacheRead)}${row("hud.cacheWrite", u.cacheWrite)}
      <div class="r tot"><dt>${esc(t("hud.total"))}</dt><dd>${isNum(u.total) ? esc(num(u.total, L)) : unknown()}</dd></div></dl>
    ${isNum(u.total) ? "" : `<p class="fine">${esc(t("hud.noTotal"))}</p>`}
    <dl class="meta"><div class="r"><dt>${esc(t("hud.model"))}</dt><dd>${u.model ? esc(u.model) : unknown()}</dd></div>
      <div class="r"><dt>${esc(t("hud.updated"))}</dt><dd>${u.updatedAt != null ? esc(ago(u.updatedAt, now, L)) : unknown()}</dd></div>
      <div class="r"><dt>${esc(t("hud.source"))}</dt><dd>${u.source ? esc(u.source) : unknown()}</dd></div>
      <div class="r"><dt>${esc(t("hud.coverage"))}</dt><dd>${esc(t("hud.cov." + u.coverage))}</dd></div></dl>
    ${ctx}<p class="fine">${esc(t("hud.apart"))}</p></div>`;
}

// The top-bar chip: hover, keyboard focus or a tap/Enter opens it; Esc closes it. State lives in the app, so a repaint keeps it open.
export function hudChip(u, open, now) {
  const v = u ? compact(u.total) : null;
  return `<div class="hud" data-hud>
    <button type="button" class="hud-btn" data-act="hud" data-fk="hud" aria-expanded="${open ? "true" : "false"}" aria-controls="hud-pop">
      <span class="k">${esc(t("hud.title"))}</span><b>${v ? esc(v) : esc(t("hud.dash"))}</b></button>
    <div class="hud-pop" id="hud-pop" role="group" aria-label="${esc(t("hud.title"))}" ${open ? "" : "hidden"}>${hudCard(u, now)}</div></div>`;
}
