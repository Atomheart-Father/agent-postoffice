// Sets the palette before the first paint so a reload never flashes the wrong one. It reads storage and never writes.
// A classic script on purpose (not a module): it must run while the page is still being parsed, and it is a separate file so
// the page needs no inline script (a Content-Security-Policy of script-src 'self' blocks inline code).
// The list is the same as PALETTES in theme.js; tests/client_assets_test.mjs keeps the two equal.
(function () {
  try {
    var p = localStorage.getItem("boxz.office.theme");
    document.documentElement.dataset.theme = ["paper", "night", "mist", "blueprint", "pine", "ember"].indexOf(p) >= 0 ? p : "paper";
  } catch (e) { /* storage blocked: the default palette stays */ }
})();
