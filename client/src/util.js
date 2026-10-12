// Small helpers shared by every view. Pure functions, no DOM: the views return strings, so they run under Node in tests.

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// "{n} online" with {n} replaced; unknown keys stay visible instead of printing "undefined".
export const fmt = (tpl, vars = {}) => String(tpl).replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));

export const isNum = (n) => typeof n === "number" && Number.isFinite(n);

// Relative time for "updated 2 min ago". `ts` is epoch seconds (what the panel API uses) or null.
export function ago(ts, now = Date.now() / 1000, lang = "en") {
  if (!isNum(ts)) return null;
  const d = Math.max(0, now - ts);
  const z = lang === "zh";
  if (d < 45) return z ? "刚刚" : "just now";
  if (d < 3600) { const m = Math.round(d / 60); return z ? `${m} 分钟前` : `${m} min ago`; }
  if (d < 86400) { const h = Math.round(d / 3600); return z ? `${h} 小时前` : `${h} h ago`; }
  const dd = Math.round(d / 86400);
  return z ? `${dd} 天前` : `${dd} d ago`;
}

// Whole numbers with thin grouping; null / non-finite is "unknown" and the caller decides how to say so (never 0).
export function num(n, lang = "en") {
  if (!isNum(n)) return null;
  return n.toLocaleString(lang === "zh" ? "zh-CN" : "en-US");
}

// Stable key for attribute selectors / ids.
export const slug = (s) => String(s ?? "").replace(/[^\w.-]+/g, "_");

export const clone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));

export function deepEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || !a || !b) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => deepEqual(a[k], b[k]));
}
