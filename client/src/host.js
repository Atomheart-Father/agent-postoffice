// The desktop-host seam. The client is a plain web app; a desktop shell (Tauri, Electron or anything else) adds
// native abilities by providing `window.boxzHost` BEFORE the client's script runs (a preload / init script).
// The web layer never reads files, never sees service.json or credentials, and never receives a token it could
// persist: the host owns the connection to the local backend and hands over only what a request needs.
//
//   window.boxzHost = {
//     kind: "desktop",
//     capabilities: { notify: true, openExternal: true, backend: true },
//     notify(title, body)            -> void           // a system notification (e.g. a letter is waiting)
//     openExternal(url)              -> void           // open in the system browser
//     backend()                      -> Promise<{ baseUrl, headers? }>   // where the local backend is, and how to talk to it
//   }
//
// In a browser none of these exist; the fallbacks below keep every feature honest (no notifications unless the user
// grants them, links open in a new tab, the backend comes from the URL).

export function getHost() {
  const h = typeof window !== "undefined" && window.boxzHost;
  if (h && typeof h === "object") return { kind: "desktop", capabilities: { notify: !!h.notify, openExternal: !!h.openExternal, backend: !!h.backend }, ...h };
  return {
    kind: "browser", capabilities: { notify: typeof Notification !== "undefined", openExternal: true, backend: false },
    notify(title, body) { try { if (typeof Notification !== "undefined" && Notification.permission === "granted") new Notification(title, { body }); } catch { /* optional */ } },
    openExternal(url) { try { window.open(url, "_blank", "noopener"); } catch { /* optional */ } },
    async backend() { return null; },
  };
}
