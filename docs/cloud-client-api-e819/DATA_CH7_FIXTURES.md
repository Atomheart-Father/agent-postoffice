# 章 7 · 匿名 fixtures（Data 线）

**基线 `e819f0e`**（本地候选 worktree `<local worktree>`；刷新自 R0 候选 `bbf3c8e`，DESIGN 2026-10-13 04:31 派票）。全部来自隔离 temp HOME＋匿名五箱的真实响应，temp 路径已替换为 `<HOME>`。能力类 fixtures 由 Interface 线补（章 3）。

## 匿名夹具（与 fixtures-data/ 同源）

- 信箱：`panelop`（operator，notify）、`box_a`（notify）、`box_b`（notify）、`box_unlisted`（notify，**已登记但不在组织树**）、`box_c`（opencode_plugin，假 session）。
- config：version 2；`organization{companies.anonco{rules_file→不存在文件},projects.anonproj{company_id,root,status_file→存在}}`；aliases `office.design`（候选 box_a，scope=project anonproj，带 duties/resources，escalation_role=office.worker）/`office.worker`（候选 box_b）/`office.missing`（候选 box_unlisted）；groups `notify[...]`；`panel{operator:panelop,label:ANON-LABEL,organization:{members:[box_a,box_b],label:匿名团队}}`。
- 挂起信三态各一：box_a `waiting`＋`reminded`（`.woken.json`）、box_c `failed`（opencode ledger `FAILED_FINAL`）。

| 文件 | 内容 |
|---|---|
| `state_ok.json` | GET /api/state 完整 200 响应（五箱、notify 组、三岗位卡、aliases、panel ok、config_error=""） |
| `letter_ok.json` | GET /api/letter 200（键：ok/box/id/sender/subject/need/body；id 经 URL 编码） |
| `outbox_ok.json` | GET /api/outbox 200（operator 已发信行，键：to/id/ref/subject/need/body/alias/mtime/status/reason） |
| `http_cases.json` | **47 例**逐条样例：12 路由成功＋错误＋幂等/状态转移；X-01 四种 403；role-apply 全状态码（200 fresh/pending、400×3、404、409 无 current、500 备份失败）；含匿名夹具说明与构造法 |

样例覆盖原则：每接口至少 **成功＋一类错误＋一类幂等/状态转移**；role-apply 覆盖 200/400/404/409/500 全态；404/409 先后顺序有专例。

## 缺口确认（本节为 Data 线取证，章 6 归口 DESIGN）

- **无 `retryProjections` 端点**：源码 grep（`retryProjections`）零命中，实测 POST 该路径 → 403（不在 9 条白名单）。名片投影待刷只能靠下次权威提交后自然刷新（role-apply 的 `projection:"pending"` 即告知语义）。
- **无角色 token / `can.*` 授权模型**：源码 grep（`role_token`/`can.`）零命中；全部写操作鉴权=Host/Origin/Content-Type＋operator 身份，没有任何 per-role capability 校验。

已知边界（章1/章2 已述，不重复）：读端点无 token 但已校 Host/Origin（X-01）；写端点同源＋JSON；`/api/state` 持锁时降级为无锁读；letter id 必须 URL 编码。
