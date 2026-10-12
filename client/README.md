# BOXZ OFFICE client (first vertical slice)

A no-build web client: open `index.html` from any static server. Prototype on sample data; see
[`docs/CLIENT.zh-CN.md`](../docs/CLIENT.zh-CN.md) for the architecture, the real/mock capability table and the missing-backend list.

```
cd client && python3 -m http.server 8790      # then http://127.0.0.1:8790/
node ../tests/client_contract_test.mjs && node ../tests/client_render_test.mjs
```
