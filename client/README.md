# BOXZ OFFICE client (1.0, real read/write batch)

A no-build web client: serve `client/` from any static server. The default adapter talks to the real backend on the page's
own origin. Backend contract used: `Atomheart-Father/agent-postoffice@9cd53d5ea47c3dfc37b7ae7d402705762ca02803`,
`docs/cloud-client-api-e819/` (the local candidate e819f0e, not deployed; GitHub master 1ab0f54 has no role-diff / role-apply).
See [`docs/CLIENT.zh-CN.md`](../docs/CLIENT.zh-CN.md) for the architecture, the three-source capability table, the
write contracts and exactly what is wired, fixture-only, mock or missing.

```
# real backend (default): the page must be served from the backend's own origin (POSTOFFICE_PANEL_BASE_URL)
# explicit sample data, marked "not wired" on every screen:
cd client && python3 -m http.server 8790      # then http://127.0.0.1:8790/?backend=mock

node tests/client_contract_test.mjs && node tests/client_render_test.mjs && node tests/client_candidate_render_test.mjs \
  && node tests/client_fixture_test.mjs && node tests/client_master_integration_test.mjs
```

The backend has no CORS and a Host / Origin / Content-Type guard (`403 {"ok":false}`), so a page on another port cannot
read or write it; there is no token and no `can.*`. The page never holds credentials. Real-device Chinese IME testing is
still pending (steps in the docs, §10).
