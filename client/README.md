# BOXZ OFFICE client (first vertical slice)

A no-build web client: open `index.html` from any static server. A runnable, anonymous interaction prototype: it is
**not wired to a real backend and not packaged as a desktop app**. See [`docs/CLIENT.zh-CN.md`](../docs/CLIENT.zh-CN.md)
for the architecture, the three-source capability table (GitHub master v1.14.1 / local candidate e819f0e / client mock),
the role-edit write contract, and what is still missing.

```
cd client && python3 -m http.server 8790      # then http://127.0.0.1:8790/   (sample data)
node ../tests/client_contract_test.mjs && node ../tests/client_render_test.mjs
```

`?backend=http` reads `/api/state` from the page's own origin (same-origin, anonymous, isolated preview only). The
backend allows no cross-origin access, so a page on another port cannot read it.
