// Browser entry: wires real DOM events to the controller. Everything with behaviour lives in app.js.
import { createApp } from "./app.js";
import { chooseAdapter } from "./adapters/index.js";
import { detectLang, setLang } from "./i18n.js";
import { loadTheme, paintTheme, loadLang } from "./theme.js";
import { startGround } from "./fx.js";

const lang = setLang(loadLang() || detectLang(navigator));
const theme = loadTheme();
paintTheme(theme);
const adapter = chooseAdapter(location.search);
const root = document.getElementById("app");
const app = createApp({ root, toastRoot: document.getElementById("toasts"), adapter, win: window, doc: document, theme, lang });

root.addEventListener("click", (e) => app.click(e.target));
root.addEventListener("input", (e) => app.input(e.target, false));
root.addEventListener("change", (e) => app.input(e.target, true));
root.addEventListener("compositionstart", () => app.setComposing(true));
root.addEventListener("compositionend", () => app.setComposing(false));
root.addEventListener("mouseover", (e) => { app.hudIn(e.target); app.hover(e.target, true); });
root.addEventListener("mouseout", (e) => { app.hudOut(e.relatedTarget, document.activeElement); app.hover(e.target, false); });
root.addEventListener("focusin", (e) => { if (root.__restoring) return; app.hudIn(e.target); app.hover(e.target, true); });
root.addEventListener("focusout", (e) => { if (root.__restoring) return; app.hudOut(e.relatedTarget, null); app.hover(e.target, false); });
document.addEventListener("keydown", (e) => { if (app.key(e)) e.preventDefault(); });
window.addEventListener("hashchange", app.onHash);
window.addEventListener("resize", () => app.redraw());

app.render();
app.load();
adapter.subscribe(() => app.load());
startGround(() => app.redraw());
window.__office = { app, adapter };   // for review in the console; carries no authority
