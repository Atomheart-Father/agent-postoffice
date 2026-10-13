// The desktop-host seam. The client is a plain web app; a desktop shell (Tauri, Electron or anything else, not
// chosen yet) adds native abilities by providing `window.boxzHost` BEFORE the client's script runs (a preload /
// init script). This file is only the seam; a desktop client does not exist yet.
//
// Credentials stay in the host. The page never receives a base URL with headers, a token or a path to service.json:
// handing headers to web JavaScript would put a long-lived, high-privilege credential where any script on the page,
// an extension or an injected string could read and replay it. Instead the trusted host holds the connection and
// offers a small set of TYPED requests:
//
//   window.boxzHost = {
//     kind: "desktop",
//     capabilities: { notify: true, openExternal: true, request: true },
//     notify(title, body)        -> void                       // a system notification (e.g. a letter is waiting)
//     openExternal(url)          -> void                       // open an http(s) link in the system browser
//     request(op, args)          -> Promise<{ ok, data } | { ok: false, error: { code, message } }>
//   }
//
// `request` is not a proxy. `op` is a name from a fixed list the host implements (for example "state.get",
// "role.diff", "role.apply"); the host maps it to one backend call itself, adds the credential itself, checks the
// operation against the principal it authenticated and the scope it was given, and refuses anything else: no
// arbitrary URL, no arbitrary file path, no passing through of caller-chosen headers. The backend still re-checks
// every write. The exact op list and its authentication belong to the interface pack and are not decided here.
//
// In a browser none of this exists; the fallbacks below keep every feature honest (no notifications unless the user
// grants them, links open in a new tab, `request` answers capability_missing and the data adapter talks to the
// same-origin backend instead).

export function getHost() {
  const h = typeof window !== "undefined" && window.boxzHost;
  if (h && typeof h === "object") return { kind: "desktop", capabilities: { notify: !!h.notify, openExternal: !!h.openExternal, request: typeof h.request === "function" }, ...h };
  return {
    kind: "browser", capabilities: { notify: typeof Notification !== "undefined", openExternal: true, request: false },
    notify(title, body) { try { if (typeof Notification !== "undefined" && Notification.permission === "granted") new Notification(title, { body }); } catch { /* optional */ } },
    openExternal(url) { try { if (/^https?:\/\//i.test(String(url))) window.open(url, "_blank", "noopener"); } catch { /* optional */ } },
    async request() { return { ok: false, error: { code: "capability_missing", message: "There is no desktop host in a browser; the page talks to the same-origin backend." } }; },
  };
}
