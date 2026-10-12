// Wires and the flowing ground. Visual only: nothing here carries information that the words and marks do not.
// Wires are measured from the laid-out page, so any depth and any number of roles work; the ground is a fixed canvas of
// faint contour curves (curves, never straight, so they are never mistaken for wires). Both are optional: under a stub
// DOM, hidden tabs or reduced motion they do nothing / draw one still frame.
import { NODES } from "./views/office.js";

const q = (id) => document.getElementById(id);
const reduced = () => { try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch { return false; } };
const qa = (v) => String(v).replace(/["\\]/g, "\\$&");
let busY = {};

function anchorOf(cv, id) {
  const c = cv.getBoundingClientRect();
  const el = cv.querySelector(`[data-id="${qa(id)}"]`); if (!el) return null;
  const m = el.querySelector(":scope>.lamp,:scope>.mk") || (el.matches && el.matches(".lamp") ? el : null);
  const r = (m || el).getBoundingClientRect();
  return { x: r.left + r.width / 2 - c.left, y: r.top + r.height / 2 - c.top, top: el.getBoundingClientRect().top - c.top };
}
function link(cv, id) {
  const n = NODES[id]; if (!n || !n.parent) return [];
  const a = anchorOf(cv, id), p = anchorOf(cv, n.parent); if (!a || !p) return [];
  if (n.kind === "head") { const by = busY[Math.round(a.top)]; return [[a.x, a.y], [a.x, by], [p.x, by], [p.x, p.y]]; }
  return [[a.x, a.y], [p.x, a.y], [p.x, p.y]];
}
export function rounded(pts, r = 7) {
  if (!pts.length) return "";
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], [x2, y2] = pts[i + 1];
    const l1 = Math.hypot(x1 - x0, y1 - y0), l2 = Math.hypot(x2 - x1, y2 - y1);
    const rr = Math.min(r, l1 / 2, l2 / 2);
    if (rr < 1) { d += ` L${x1},${y1}`; continue; }
    d += ` L${x1 - (x1 - x0) / l1 * rr},${y1 - (y1 - y0) / l1 * rr} Q${x1},${y1} ${x1 + (x2 - x1) / l2 * rr},${y1 + (y2 - y1) / l2 * rr}`;
  }
  const l = pts[pts.length - 1];
  return d + ` L${l[0]},${l[1]}`;
}
export function drawWires(isDead) {
  try {
    const cv = q("canvas"), svg = q("wires"); if (!cv || !svg || !cv.querySelectorAll) return;
    busY = {};
    for (const h of cv.querySelectorAll(".head")) { const a = anchorOf(cv, h.dataset.id); if (a) { const tp = Math.round(a.top); busY[tp] = tp - 30; } }
    let s = "";
    for (const id in NODES) {
      if (id === "root") continue;
      const pts = link(cv, id); if (!pts.length) continue;
      s += `<path class="${isDead(NODES[id]) ? "off" : ""}" d="${rounded(pts)}"/>`;
    }
    svg.innerHTML = s + `<path class="hl" id="hl" d=""/>`;
  } catch { /* stub DOM */ }
}
const chain = (id) => { const a = []; while (id && NODES[id]) { a.push(id); id = NODES[id].parent; } return a; };
export function hoverPath(id) {
  try {
    const cv = q("canvas"), hl = q("hl"); if (!cv || !hl) return;
    if (!id) { hl.setAttribute("d", ""); return; }
    let pts = [];
    for (const n of chain(id)) { if (n === "root") break; const l = link(cv, n); pts = pts.concat(pts.length ? l.slice(1) : l); }
    hl.setAttribute("d", rounded(pts));
  } catch { /* stub DOM */ }
}

const ground = { c: null, ctx: null, w: 0, h: 0, last: 0, t0: 0 };
function size() {
  try {
    const c = q("ground"); if (!c || !c.getContext) return;
    const d = Math.min(2, window.devicePixelRatio || 1);
    ground.c = c; ground.ctx = c.getContext("2d"); ground.w = window.innerWidth; ground.h = window.innerHeight;
    c.width = ground.w * d; c.height = ground.h * d; ground.ctx.setTransform(d, 0, 0, d, 0, 0);
  } catch { /* no canvas */ }
}
export function drawGround(t) {
  try {
    const { ctx, w, h } = ground; if (!ctx) return;
    if (t === undefined) t = (performance.now() - ground.t0) / 1000;
    const cs = getComputedStyle(document.documentElement);
    const rgb = cs.getPropertyValue("--line").trim(), a = parseFloat(cs.getPropertyValue("--line-a")) || .07;
    ctx.clearRect(0, 0, w, h);
    const N = Math.max(18, Math.round(h / 34)), k1 = 2 * Math.PI / (w * 1.15), k2 = 2 * Math.PI / (w * .42);
    for (let i = 0; i < N; i++) {
      const f = i / (N - 1), base = -h * .08 + f * h * 1.16, amp = h * (.05 + .05 * Math.sin(f * Math.PI));
      ctx.strokeStyle = `rgba(${rgb},${(a * (.35 + .65 * Math.sin(f * Math.PI))).toFixed(3)})`; ctx.lineWidth = 1; ctx.beginPath();
      for (let x = -20; x <= w + 20; x += 14) {
        const y = base + amp * Math.sin(x * k1 + t * .045 + i * .32) + amp * .42 * Math.sin(x * k2 - t * .07 + i * .19) + amp * .18 * Math.sin(x * k2 * 2.3 + t * .03 - i * .5);
        if (x <= -20) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  } catch { /* no canvas */ }
}
function tick(now) {
  try {
    if (!document.hidden && now - ground.last > 50) { ground.last = now; drawGround((now - ground.t0) / 1000); }
    if (!reduced()) requestAnimationFrame(tick);
  } catch { /* gone */ }
}
export function startGround(onResize) {
  try {
    if (typeof requestAnimationFrame !== "function" || !q("ground")) return;
    ground.t0 = performance.now(); size(); drawGround(0);
    if (!reduced()) requestAnimationFrame(tick);
    window.addEventListener("resize", () => { size(); drawGround(); if (onResize) onResize(); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => onResize && onResize());
  } catch { /* stub DOM */ }
}
export const redrawGround = () => drawGround();
