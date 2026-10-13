// One writing tab per (backend address, operator).
//
// The request log is only trustworthy if exactly one page writes it and sends the writes it describes. The mechanism is a Web
// Locks exclusive lock named after the scope: the page that holds it is the writer; every other page of the same scope is
// read-only and says so. localStorage cannot do this job (a read-modify-write on it is not atomic, and a "lease with a timeout"
// built on it is a guess about the other tab's health), so this file never uses it for mutual exclusion.
//
//   holder       this page holds the lock: it may send writes and it is the only one that saves the summary
//   reader       another page holds it: this page can read, refresh and check, and cannot write; it asks for the lock and
//                takes over by itself when the holder goes away
//   checking     the first answer has not arrived yet (writes wait)
//   unsupported  this browser / page has no Web Locks (they need a secure context: https or localhost). There is no safe way to
//                tell whether another tab writes, so writing is OFF here, with a message. It is never silently allowed.
//
// Scope of the guarantee: Web Locks are shared by pages of the same origin in the same browser storage partition (one browser
// profile; not another browser, another profile, a private window or another device). It is exactly the reach of localStorage,
// which is where the log lives; the guarantee is claimed for that reach and for nothing wider.
//
// Release: the lock is released when the holder calls release() (scope change, shutdown) and by the browser when the page is
// closed, navigated away from or crashes - that is how Web Locks work; it needs no timer and no heartbeat. The waiting tab is
// granted the lock then and calls onChange("holder"): the app reads the summary again from storage (it never trusts what it
// loaded earlier) and sends nothing.
//
// Nothing here sends anything.

export function createWriterGate({ locks, onChange }) {
  let cur = null;
  const usable = !!(locks && typeof locks.request === "function");

  function release() {
    if (!cur) return;
    const me = cur; cur = null;
    me.dead = true;
    try { if (me.ctl) me.ctl.abort(); } catch { /* nothing waiting */ }
    if (me.free) me.free();
  }

  // `names[0]` is the scope's lock (the same for every release of this client); the rest are names earlier releases used.
  // All of them are taken, always in this order, and the page is the holder only when it holds every one: a page of an
  // earlier release that still holds only its own name keeps this page a reader, and once this page holds the whole set
  // that page is a reader too. Nobody waits for a name while holding a later one, so two pages cannot lock each other up.
  // The answer is always reported under names[0].
  function claim(spec) {
    release();
    const names = (Array.isArray(spec) ? spec : [spec]).filter((n, i, a) => typeof n === "string" && n && a.indexOf(n) === i);
    const name = names[0];
    if (!usable || !name) { onChange("unsupported", name); return; }
    const me = { name, dead: false, free: null, ctl: typeof AbortController === "function" ? new AbortController() : null };
    cur = me;
    const say = (state) => { if (!me.dead) onChange(state, name); };
    const hold = () => { if (me.dead) return undefined; say("holder"); return new Promise((res) => { me.free = res; if (me.dead) res(); }); };
    // Take names[i] and, inside it, the rest. The returned promise settles when this page lets go of names[i] and all after it.
    const take = (i) => {
      const proceed = () => (i === names.length - 1 ? hold() : take(i + 1));
      return locks.request(names[i], { ifAvailable: true }, async (lock) => {
        if (me.dead) return undefined;
        if (lock) return proceed();
        say("reader");
        // Not available: wait for it (still holding the earlier names). A pending request is cancelled with the signal when the
        // scope changes or the page shuts down. The wait is returned, so the earlier names stay held while it lasts.
        return locks.request(names[i], me.ctl ? { signal: me.ctl.signal } : {}, async () => (me.dead ? undefined : proceed())).catch(() => { /* aborted */ });
      });
    };
    say("checking");
    try { Promise.resolve(take(0)).catch(() => say("unsupported")); } catch { say("unsupported"); }
  }
  return { claim, release, usable };
}

// A stand-in for navigator.locks with the semantics this app relies on, for tests and nothing else: exclusive locks by name,
// ifAvailable, an abort signal for a pending request, FIFO grant, and a tab that can disappear without telling anyone
// (`crash()`), which is what a browser does to the locks of a page that is gone.
export function createMemoryLocks() {
  const held = new Map();    // name -> entry
  const queues = new Map();  // name -> entry[]
  const abortErr = () => { const e = new Error("The request was aborted."); e.name = "AbortError"; return e; };

  function grant(entry) {
    held.set(entry.name, entry);
    Promise.resolve().then(async () => {
      try { const r = await entry.cb({ name: entry.name }); if (!entry.tab.dead) entry.resolve(r); }
      catch (e) { if (!entry.tab.dead) entry.reject(e); }
      finally { if (held.get(entry.name) === entry) { held.delete(entry.name); next(entry.name); } }
    });
  }
  function next(name) {
    const q = queues.get(name) || [];
    while (q.length) { const e = q.shift(); if (!e.tab.dead) { grant(e); return; } }
  }
  function tab() {
    const me = { dead: false };
    return {
      request(name, a, b) {
        const opts = typeof a === "function" ? {} : a || {}, cb = typeof a === "function" ? a : b;
        return new Promise((resolve, reject) => {
          if (me.dead) return;   // a dead page asks for nothing
          const entry = { name, cb, resolve, reject, tab: me };
          if (opts.signal) {
            if (opts.signal.aborted) { reject(abortErr()); return; }
            opts.signal.addEventListener("abort", () => {
              const q = queues.get(name) || []; const i = q.indexOf(entry);
              if (i >= 0) { q.splice(i, 1); reject(abortErr()); }
            }, { once: true });
          }
          if (!held.has(name)) grant(entry);
          else if (opts.ifAvailable) Promise.resolve().then(async () => { try { resolve(await cb(null)); } catch (e) { reject(e); } });
          else { if (!queues.has(name)) queues.set(name, []); queues.get(name).push(entry); }
        });
      },
      // the page is gone: its locks are released and its waiters vanish, and nothing in the page is told
      crash() {
        me.dead = true;
        for (const [name, e] of [...held]) if (e.tab === me) { held.delete(name); next(name); }
        for (const q of queues.values()) for (let i = q.length - 1; i >= 0; i--) if (q[i].tab === me) q.splice(i, 1);
      },
      holders: () => [...held.keys()],
    };
  }
  return { tab, holders: () => [...held.keys()] };
}
