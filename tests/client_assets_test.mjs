// What the page asks the host for: no inline script (a Content-Security-Policy of script-src 'self' blocks it), every file the
// page, its stylesheets and its modules point at exists, and no font file is needed (a host need not know woff2). Node only.
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import vm from "node:vm";
import { PALETTES } from "../client/src/theme.js";

let failures = 0;
const ok = (m) => console.log("ok   - " + m);
const bad = (m, x) => { failures++; console.log("BAD  - " + m + (x ? " :: " + x : "")); };
const yes = (c, m, x) => (c ? ok(m) : bad(m, x));
const is = (g, w, m) => (JSON.stringify(g) === JSON.stringify(w) ? ok(m) : bad(m, `got ${JSON.stringify(g)} want ${JSON.stringify(w)}`));

const root = new URL("../client/", import.meta.url).pathname;
const html = readFileSync(join(root, "index.html"), "utf8");

// ---- the page has no inline code ----
const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
yes(scripts.length >= 2, "the page has scripts");
yes(scripts.every(([, attrs, body]) => /\bsrc="[^"]+"/.test(attrs) && body.trim() === ""), "every <script> is external and has no inline body");
yes(!/\son[a-z]+\s*=/i.test(html), "no inline event handler attributes in the page");
yes(!/javascript:/i.test(html), "no javascript: URLs in the page");
yes(!/<style\b/i.test(html), "no <style> block in the page");
yes(!/(?:src|href)="data:/i.test(html), "no data: URL in the page (default-src 'self' blocks it as an image)");

// ---- every file the page points at exists ----
const refs = [...html.matchAll(/\b(?:src|href)="([^"#?]+)"/g)].map((m) => m[1]).filter((u) => !/^(?:[a-z]+:|\/\/|data:)/i.test(u));
for (const u of refs) yes(existsSync(join(root, u)), `index.html -> ${u} exists`);
const css = refs.filter((u) => u.endsWith(".css"));
for (const f of css) {
  const text = readFileSync(join(root, f), "utf8");
  const urls = [...text.matchAll(/url\(\s*['"]?([^'")]+)['"]?\s*\)/g)].map((m) => m[1]).filter((u) => !/^(?:data:|#)/.test(u));
  for (const u of urls) yes(existsSync(resolve(dirname(join(root, f)), u)), `${f} -> ${u} exists`);
  yes(!/\.woff2?\b|\.ttf\b|\.otf\b/i.test(text), `${f} asks for no font file`);
  yes(!/@import/.test(text), `${f} imports nothing`);
}
yes(!existsSync(join(root, "fonts")) || readdirSync(join(root, "fonts")).length === 0, "no font file ships with the client");

// ---- relative module imports resolve ----
const walk = (d) => readdirSync(d).flatMap((n) => { const p = join(d, n); return statSync(p).isDirectory() ? walk(p) : [p]; });
const mods = walk(join(root, "src")).filter((p) => p.endsWith(".js"));
let missing = [];
for (const f of mods) {
  const text = readFileSync(f, "utf8");
  for (const m of text.matchAll(/(?:from|import)\s*\(?\s*["'](\.{1,2}\/[^"']+)["']/g)) if (!existsSync(resolve(dirname(f), m[1]))) missing.push(`${f.slice(root.length)} -> ${m[1]}`);
}
is(missing, [], "every relative import in src/ resolves");

// ---- theme-boot.js: same palettes as theme.js, reads storage, never writes ----
const boot = readFileSync(join(root, "src/theme-boot.js"), "utf8");
const run = (stored, throws = false) => {
  const writes = [];
  const documentElement = { dataset: {} };
  const localStorage = { getItem: (k) => { if (throws) throw new Error("blocked"); return k === "boxz.office.theme" ? stored : null; }, setItem: (...a) => writes.push(a), removeItem: (...a) => writes.push(a) };
  vm.runInNewContext(boot, { document: { documentElement }, localStorage });
  return { theme: documentElement.dataset.theme, writes };
};
for (const p of PALETTES) is(run(p).theme, p, `theme-boot keeps the stored palette: ${p}`);
is(run("nonsense").theme, "paper", "theme-boot ignores a palette it does not know");
is(run(null).theme, "paper", "…and falls back to the default when nothing is stored");
is(run(null, true).theme, undefined, "…and when storage throws it leaves the default (the stylesheet's) alone");
is(run("night").writes, [], "theme-boot never writes storage");
yes(boot.includes(JSON.stringify(PALETTES).slice(1, -1).replace(/","/g, '", "')), "theme-boot lists exactly the palettes of theme.js");

console.log(failures ? `FAIL ${failures}` : "PASS");
process.exit(failures ? 1 : 0);
