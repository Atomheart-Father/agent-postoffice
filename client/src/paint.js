// Repaint without losing the user's place. The page redraws after every poll and every state change, so a repaint
// must (1) do nothing when the markup is identical, (2) keep the scroll of every [data-keep] region, and (3) keep
// keyboard focus (and the caret in a text box) on the element carrying the same data-fk. All DOM access is guarded so
// the same function runs under the Node tests' tiny DOM stub.

export function paint(root, html) {
  if (!root || root.__html === html) return false;
  const keep = {};
  let fk = null, sel = null;
  try {
    for (const el of root.querySelectorAll("[data-keep]")) keep[el.getAttribute("data-keep")] = el.scrollTop;
    const a = document.activeElement;
    if (a && root.contains(a) && a.getAttribute && a.getAttribute("data-fk")) {
      fk = a.getAttribute("data-fk");
      if (typeof a.selectionStart === "number") sel = [a.selectionStart, a.selectionEnd];
    }
  } catch { /* stub DOM */ }
  root.__restoring = true;   // replacing the markup fires blur/focus events that are not the user's doing
  root.innerHTML = html; root.__html = html;
  try {
    for (const el of root.querySelectorAll("[data-keep]")) { const k = el.getAttribute("data-keep"); if (k in keep) el.scrollTop = keep[k]; }
    if (fk) {
      const el = root.querySelector(`[data-fk="${String(fk).replace(/["\\]/g, "\\$&")}"]`);
      if (el && el.focus) { el.focus({ preventScroll: true }); if (sel && el.setSelectionRange) el.setSelectionRange(sel[0], sel[1]); }
    }
  } catch { /* stub DOM */ }
  root.__restoring = false;
  return true;
}
