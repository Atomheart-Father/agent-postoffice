// Where the request log's minimal summary lives across a reload (localStorage), and nothing else.
//
// ONE WRITER. For a given (backend address, operator) only one tab may write (see flows/writer.js: a Web Locks lock). This
// module therefore does no merging and no read-modify-write protocol: the writer saves its own list, whole, and every other tab
// only reads. Nothing here pretends localStorage can arbitrate between two writers - it cannot, and this code never relies on it.
//
// Scope: one entry per (backend address, operator). Another Office, another origin or another operator never sees these
// records, and a record never moves between scopes. The scope is also written INSIDE the payload and checked on load.
//
// Version: v2 is the current schema (no letter reference in it). The v1 key written by the previous client carried a
// `result_ref`; v1 is read ONLY to carry its records forward - the reference is never read - and the writer deletes the v1 key
// once the v2 entry is written.
//
// Failure: storage can be missing, blocked, full or hold garbage. Every call is wrapped; the caller gets a state word
// ("ok" | "empty" | "unusable" | "off") and the page keeps working with the log in memory only.
//
// This is a convenience for "what was I waiting for", not a security or audit record.

import { fromStored, toStored, capList } from "./flows/writes.js";

export const VERSION = 2;
export const KEY_PREFIX = `boxz.office.requests.v${VERSION}`;
const LEGACY_PREFIX = "boxz.office.requests.v1";
export const LOCK_PREFIX = `boxz.office.writer.v${VERSION}`;

export function defaultStorage(win = globalThis) {
  try { const s = win.localStorage; return s && typeof s.getItem === "function" ? s : null; } catch { return null; }
}

export function createLedgerStore({ storage = defaultStorage(), base = "" } = {}) {
  const scopeOf = (operator) => ({ base: String(base || ""), operator: String(operator || "") });
  const suffix = (sc) => `|${encodeURIComponent(sc.base)}|${encodeURIComponent(sc.operator)}`;
  const keyOf = (sc) => KEY_PREFIX + suffix(sc);
  const legacyKeyOf = (sc) => LEGACY_PREFIX + suffix(sc);
  const lockNameOf = (sc) => LOCK_PREFIX + suffix(sc);

  // One payload under `key`, for `sc`, at `version`. Never returns anything but records built by fromStored.
  function readKey(key, sc, version) {
    let raw;
    try { raw = storage.getItem(key); } catch { return { state: "off", items: [] }; }
    if (raw == null) return { state: "empty", items: [] };
    let p;
    try { p = JSON.parse(raw); } catch { return { state: "unusable", items: [] }; }
    if (!p || typeof p !== "object" || p.v !== version || !p.scope || p.scope.base !== sc.base || p.scope.operator !== sc.operator || !Array.isArray(p.items)) return { state: "unusable", items: [] };
    return { state: "ok", items: p.items.map(fromStored).filter(Boolean) };
  }
  // The summary of one scope: { state, items, dropped, legacy }. `legacy` = the records came from (or a leftover exists of) the v1
  // key, so the writer should save once to upgrade it and delete the old key.
  function read(sc) {
    if (!storage) return { state: "off", items: [], dropped: 0, legacy: false };
    const cur = readKey(keyOf(sc), sc, VERSION);
    let old = { state: "empty", items: [] };
    if (cur.state !== "off") old = readKey(legacyKeyOf(sc), sc, 1);
    const legacyThere = old.state !== "empty" && old.state !== "off";
    if (cur.state === "off") return { state: "off", items: [], dropped: 0, legacy: false };
    // v2 wins when it exists; v1 is only used when v2 has nothing usable
    const use = cur.state === "ok" ? cur : old.state === "ok" ? old : cur;
    const { list, dropped } = capList(use.items);
    return { state: cur.state === "ok" || old.state === "ok" ? "ok" : cur.state, items: list, dropped, legacy: legacyThere };
  }
  // The writer saves its whole list. Returns "ok" or "off" (the write did not happen).
  function save(operator, list) {
    if (!storage) return "off";
    const sc = scopeOf(operator);
    try {
      storage.setItem(keyOf(sc), JSON.stringify({ v: VERSION, scope: sc, saved_at: Date.now() / 1000, items: list.map(toStored) }));
    } catch { return "off"; }
    try { if (storage.getItem(legacyKeyOf(sc)) != null) storage.removeItem(legacyKeyOf(sc)); } catch { /* the v2 entry is what counts */ }
    return "ok";
  }
  return { scopeOf, keyOf, legacyKeyOf, lockNameOf, load: (operator) => read(scopeOf(operator)), save };
}
