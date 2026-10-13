// Where the request log's minimal summary lives across a reload (localStorage), and nothing else.
//
// Scope: one entry per (backend address, operator). Another Office, another origin or another operator never sees these
// records, and a record never moves between scopes. The scope is also written INSIDE the payload and checked on load, so a
// key that was copied or collided cannot leak another scope's records in.
//
// Version: the key carries the schema version and so does the payload. A payload of another version is not read and not
// overwritten by guess; it is simply ignored (`unusable`).
//
// Failure: storage can be missing, blocked, full or hold garbage. Every call is wrapped; the caller gets a state word
// ("ok" | "empty" | "unusable" | "off") and the page keeps working with the log in memory only.
//
// This is a convenience for "what was I waiting for", not a security or audit record.

import { fromStored, toStored, mergeForSave, capList, LIMIT } from "./flows/writes.js";

export const VERSION = 1;
export const KEY_PREFIX = `boxz.office.requests.v${VERSION}`;

export function defaultStorage(win = globalThis) {
  try { const s = win.localStorage; return s && typeof s.getItem === "function" ? s : null; } catch { return null; }
}

export function createLedgerStore({ storage = defaultStorage(), base = "" } = {}) {
  const scopeOf = (operator) => ({ base: String(base || ""), operator: String(operator || "") });
  const keyOf = (sc) => `${KEY_PREFIX}|${encodeURIComponent(sc.base)}|${encodeURIComponent(sc.operator)}`;

  function read(sc) {
    if (!storage) return { state: "off", items: [] };
    let raw;
    try { raw = storage.getItem(keyOf(sc)); } catch { return { state: "off", items: [] }; }
    if (raw == null) return { state: "empty", items: [] };
    let p;
    try { p = JSON.parse(raw); } catch { return { state: "unusable", items: [] }; }
    if (!p || typeof p !== "object" || p.v !== VERSION || !p.scope || p.scope.base !== sc.base || p.scope.operator !== sc.operator || !Array.isArray(p.items)) return { state: "unusable", items: [] };
    const items = p.items.slice(0, LIMIT * 2).map(fromStored).filter(Boolean);
    return { state: "ok", items: capList(items) };
  }
  // Returns "ok" or "off" (the write did not happen). `seen` = ids this page has ever held, so another tab's records are kept.
  function save(operator, list, seen) {
    if (!storage) return "off";
    const sc = scopeOf(operator);
    const stored = read(sc);   // "unusable" is replaced by this page's own records: it was never going to be read
    if (stored.state === "off") return "off";   // a store that cannot be read back is not a store the log can be recovered from
    try {
      const items = mergeForSave(list, stored.state === "ok" ? stored.items : [], seen).map(toStored);
      storage.setItem(keyOf(sc), JSON.stringify({ v: VERSION, scope: sc, saved_at: Date.now() / 1000, items }));
      return "ok";
    } catch { return "off"; }
  }
  return { scopeOf, keyOf, load: (operator) => read(scopeOf(operator)), save };
}
