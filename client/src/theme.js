// Six named palettes (blocks of the same colour roles in styles/tokens.css) and the stored choice.
// Painting never writes; only an explicit choice does. Storage that throws costs persistence, never the ability to switch.
export const PALETTES = ["paper", "night", "mist", "blueprint", "pine", "ember"];
const KEY = "boxz.office.theme";
const LKEY = "boxz.office.lang";
const store = {
  get(k) { try { return window.localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { window.localStorage.setItem(k, v); } catch { /* private mode */ } },
};
export const cleanTheme = (c) => (PALETTES.includes(c) ? c : "paper");
export const loadTheme = () => cleanTheme(store.get(KEY));
export const saveTheme = (c) => store.set(KEY, cleanTheme(c));
export const paintTheme = (c) => { try { document.documentElement.dataset.theme = cleanTheme(c); } catch { /* no DOM */ } };
export const loadLang = () => store.get(LKEY);
export const saveLang = (l) => store.set(LKEY, l);
