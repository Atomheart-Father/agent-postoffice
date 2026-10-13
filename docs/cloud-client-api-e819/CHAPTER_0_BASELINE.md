# 章 0 · 版本基线与能力状态表（DESIGN）

基线：**本地候选 e819f0e**（release/r0-hardened-20261013；未 push、未合 master、未部署）。**GitHub master = 1ab0f54（v1.14.1）**，客户端读到的公开代码是 master，**不含**下表标「候选」的接口。列名一律用：「GitHub master v1.14.1」「本地候选 e819f0e」「客户端 mock」。旧 `panel/index.html` 冻结不动（blob 5f82a766…）。

| 能力 | GitHub master v1.14.1 | 本地候选 e819f0e | 客户端 mock | 接线结论 |
|---|---|---|---|---|
| `GET /api/state`（boxes/aliases/role_cards/activity） | 有（字段较少） | 有（含 role_cards 投影；字段以章 2 实测为准） | 可 | 可接（待章 2 fixtures） |
| `GET /api/letter`、`/api/outbox` | 有 | 有 | 可 | 可接 |
| `POST /api/send`、`edit-one`、`retract-one`、`ack-one`、`archive-one`、`clear`、`status` | 有 | 有 | 可 | 可接（POST 须同源 Origin＋application/json） |
| `POST /api/role-diff`、`/api/role-apply` | **无** | 有 | 可 | 仅本地候选；GitHub 无此路由 |
| Host/Origin 精确白名单（GET 与 POST，陌生→403 `{"ok":false}`） | 无（GET 不校验） | 有（HF-4 X-01） | — | 本地防跨站；**不是身份认证** |
| 鉴权 token／`can.*` 强授权 | 无 | **无** | — | 不得在前端猜授权 |
| 任意 CORS | 无 | 无 | — | 首批接线用本地同源承载 |
| 通用 `retryProjections` 端点 | 无 | **无** | — | 不放重试按钮；不重发 apply |
| 线程列表／实时线程控制 | 无 | 无（适配器无「列出会话」动词） | 可 | mock，缺口见章 6 |
| 工作（work）／approvals／行为账 | 无 | 无（仅设计） | 可 | mock；工作状态不由邮件推出 |
| usage HUD／用量归属 | 无 | 账模块在候选（reader 未接） | 可 | mock |
| 活动 `activity` | 有（旧生产也有） | 有 | — | 有时效与来源，缺值≠空闲 |
| 邮件发收岗位引用 | 无 | 无（未完整记录） | — | 历史邮件岗位归属不得用当前 alias 反推 |

## 合同变更登记（对客户端有影响，e819f0e 相对 master）
1. GET/POST 均过 Host+Origin 白名单；陌生→403，不返回正文。允许集＝回环（带本面板端口）＋显式配置的 `POSTOFFICE_PANEL_BASE_URL`。
2. `presented-update` 对非法信箱名拒绝；`letter-kind`／`wake-plan` 入口校验信箱（影响脚本而非页面）。
3. 新增 `role-diff`／`role-apply`。
4. `saved:true,projection:"pending"` ＝ 权威配置已保存、派生名片待刷新，不是提交失败。
实测值、字段与错误体见 章 1、章 2、章 7 fixtures（Data 线，基线 e819f0e 重跑中）。
