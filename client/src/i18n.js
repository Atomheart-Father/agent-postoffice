// Language and wording. The table lives in strings.js; this file is the lookup. User content (role names, thread
// titles, mail subjects, event text) is never translated, only the client's own words are.
import { STR } from "./strings.js";
import { fmt } from "./util.js";

export const LANGS = ["zh", "en"];
let lang = "en";
export const missing = new Set();   // keys asked for but absent from the table (tests assert this stays empty)
export const getLang = () => lang;
export function setLang(l) { lang = LANGS.includes(l) ? l : "en"; return lang; }
export function detectLang(nav) {
  const ls = (nav && (nav.languages || [nav.language])) || [];
  return String(ls[0] || "en").toLowerCase().startsWith("zh") ? "zh" : "en";
}
// t("key") or t("key", {n: 3}). A missing key prints the key itself so a gap is visible, never blank.
export function t(key, vars) {
  const e = STR[key];
  if (!e) { missing.add(key); return key; }
  return fmt(lang === "zh" ? e[0] : e[1], vars);
}
export const has = (key) => !!STR[key];
