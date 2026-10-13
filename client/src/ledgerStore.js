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
// `result_ref`; v1 is read ONLY to carry its records forward - the reference is never read.
//
// Nothing is dropped here. Not on load, not on save, not on the upgrade: the store keeps whatever records it is given, and what
// may leave the log (finished or dismissed records, to make room) is decided in flows/writes.js, at the moment a new write
// starts. An unresolved record never leaves by itself, so no storage state can make one disappear.
//
// Upgrade v1 -> v2 (the writer does it, on taking over): the records of v1 that v2 does not already have are added to the v2
// list, v2 is written, READ BACK and compared, and only then is the v1 key removed. If any step fails, the v1 key stays exactly
// as it was (a damaged or foreign v1 entry is never touched either) and the page says the old records are still in the old
// place. An old page that still writes v1 after that simply starts a new v1 entry, which is carried forward the same way.
//
// Lock name: the writer lock does not carry the storage version or the client version. It is fixed by the normalized backend
// address and the operator (see lockNameOf), so every release of this client - older or newer storage schema - asks for the
// same lock. The lock the previous release used (`LEGACY_LOCK_PREFIXES`) is still taken as well, see flows/writer.js.
// Web Locks cover pages of the same origin in the same browser storage partition. Not another browser, another profile, a
// private window or another device.
//
// Failure: storage can be missing, blocked, full or hold garbage. Every call is wrapped; the caller gets a state word
// ("ok" | "empty" | "unusable" | "off") and the page keeps working with the log in memory only.
//
// This is a convenience for "what was I waiting for", not a security or audit record.

import { fromStored, toStored } from "./flows/writes.js";

export const VERSION = 2;
export const KEY_PREFIX = `boxz.office.requests.v${VERSION}`;
const LEGACY_PREFIX = "boxz.office.requests.v1";
// Independent of VERSION on purpose: a future schema must not get its own lock.
export const LOCK_PREFIX = "boxz.office.writer";
// Lock names an earlier release of this client used. Kept so those pages are really kept out while this release writes.
// (Releases before the first lock cannot be: they ask for no lock at all.)
export const LEGACY_LOCK_PREFIXES = ["boxz.office.writer.v2"];

export function defaultStorage(win = globalThis) {
  try { const s = win.localStorage; return s && typeof s.getItem === "function" ? s : null; } catch { return null; }
}

// The backend address as a scope: the same address spelled differently (case of the host, default port, trailing slash,
// query or fragment) is one address. Not an address at all -> trimmed text, so a test stand-in still works.
export function normalizeBase(base) {
  const raw = String(base == null ? "" : base).trim();
  if (!raw) return "";
  try {
    const u = new URL(raw);
    if (!u.host) throw new Error("no host");
    const path = u.pathname.replace(/\/+$/, "");
    return `${u.protocol}//${u.host}${path}`;
  } catch { return raw.replace(/\/+$/, ""); }
}

const idOf = (w) => `${w.id}|${w.at}`;

export function createLedgerStore({ storage = defaultStorage(), base = "" } = {}) {
  const scopeOf = (operator) => ({ base: normalizeBase(base), operator: String(operator || "").trim() });
  const suffix = (sc) => `|${encodeURIComponent(sc.base)}|${encodeURIComponent(sc.operator)}`;
  const keyOf = (sc) => KEY_PREFIX + suffix(sc);
  const legacyKeyOf = (sc) => LEGACY_PREFIX + suffix(sc);
  const lockNameOf = (sc) => LOCK_PREFIX + suffix(sc);
  const legacyLockNamesOf = (sc) => LEGACY_LOCK_PREFIXES.map((p) => p + suffix(sc));

  // One payload under `key`, for `sc`, at `version`. Never returns anything but records built by fromStored.
  // `items` = the records; `raw` = how many entries the payload held; `parsed` = how many became records.
  function readKey(key, sc, version) {
    let raw;
    try { raw = storage.getItem(key); } catch { return { state: "off", items: [] }; }
    if (raw == null) return { state: "empty", items: [] };
    let p;
    try { p = JSON.parse(raw); } catch { return { state: "unusable", items: [] }; }
    if (!p || typeof p !== "object" || p.v !== version || !p.scope || p.scope.base !== sc.base || p.scope.operator !== sc.operator || !Array.isArray(p.items)) return { state: "unusable", items: [] };
    return { state: "ok", items: p.items.map(fromStored).filter(Boolean) };
  }
  // The summary of one scope: { state, items, legacy }. Newest first. `legacy` = a v1 key exists, so the writer should save once
  // to carry it over (save decides whether the old key may go).
  function read(sc) {
    if (!storage) return { state: "off", items: [], legacy: false };
    const cur = readKey(keyOf(sc), sc, VERSION);
    if (cur.state === "off") return { state: "off", items: [], legacy: false };
    const old = readKey(legacyKeyOf(sc), sc, 1);
    const legacyThere = old.state !== "empty" && old.state !== "off";
    // v2 first; a v1 record that v2 does not have is added (an older page may have written v1 after the upgrade)
    const seen = new Set(cur.items.map(idOf));
    const items = [...cur.items, ...old.items.filter((w) => !seen.has(idOf(w)))].sort((a, b) => b.at - a.at);
    const ok = cur.state === "ok" || old.state === "ok";
    return { state: ok ? "ok" : cur.state, items, legacy: legacyThere };
  }
  // The writer saves its whole list. Returns "ok" when v2 holds it (read back and compared), "off" when it does not. The v1 key is
  // removed only when it was readable, belonged to this scope, and every record in it is in the list that was just verified.
  function save(operator, list) {
    if (!storage) return "off";
    const sc = scopeOf(operator);
    try {
      storage.setItem(keyOf(sc), JSON.stringify({ v: VERSION, scope: sc, saved_at: Date.now() / 1000, items: list.map(toStored) }));
    } catch { return "off"; }
    const back = readKey(keyOf(sc), sc, VERSION);
    const have = new Set(back.items.map(idOf));
    if (back.state !== "ok" || back.items.length !== list.length || !list.every((w) => have.has(idOf(w)))) return "off";
    try {
      const old = readKey(legacyKeyOf(sc), sc, 1);
      if (old.state === "ok" && old.items.every((w) => have.has(idOf(w)))) storage.removeItem(legacyKeyOf(sc));
    } catch { /* the v2 entry is what counts; v1 stays and is carried again next time */ }
    return "ok";
  }
  // Is a v1 entry still there that has not been carried over? (After a failed or refused upgrade.)
  function legacyLeft(operator) {
    if (!storage) return false;
    try { return storage.getItem(legacyKeyOf(scopeOf(operator))) != null; } catch { return false; }
  }
  return { scopeOf, keyOf, legacyKeyOf, lockNameOf, legacyLockNamesOf, legacyLeft, load: (operator) => read(scopeOf(operator)), save };
}
