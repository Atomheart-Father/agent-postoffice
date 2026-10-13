# 章 4 · 权限与状态矩阵（DESIGN；一个真源）

**现实先说清**：当前面板无登录、无 token；任何能访问 127.0.0.1 同源页面的人等同「本机所有者」。Host/Origin 白名单只防跨站网页与 DNS 重绑，**不识别人，也不是角色权限**。以下「谁能做」分三列：**现实（已证）**、**目标设计（未证）**、**客户端应显示**。客户端不得把目标设计当现实授权。

## 4.1 操作矩阵
| 操作 | 现实（e819f0e） | 目标设计（未证，OVERALL_DESIGN §3.5–3.6） | 客户端显示 |
|---|---|---|---|
| 读 state／letter／outbox | 本机同源可读 | 按岗位投影（未实现） | 直接显示；缺值标 missing／unknown |
| 发信、撤回、编辑未读、确认、归档、清理 | 本机同源 POST（Origin＋JSON）；服务端按信件状态校验 | `decide()` 按岗位判定（未实现） | 失败时显示服务端 error，不预判权限 |
| 岗位 diff／apply | 本机同源；apply 以 baseline 校验并发 | 治理 op＋所有者批准（R4，未实现） | 预览→确认→apply；不暗示「需批准」除非后端返回 |
| 广播 | 当前任何信箱可广播 | 仅秘书（设计，未实现） | 不做权限提示 |
| 工作／批准／验收 | **无** | `office act`（设计） | 全 mock，标「未接线」 |

## 4.2 状态词汇（前端唯一词表；只用后端真实返回的，其余标 mock）
| 词 | 含义 | 来源 | 注意 |
|---|---|---|---|
| `saved` / `projection: fresh|pending|not_applicable` | 配置已写／派生名片是否刷新 | role-apply 响应 | `saved:true,pending` 不是失败，**不得重发 apply** |
| `stale`（HTTP 409） | 基线已变 | role-apply | **不带 current**；重新取快照与预览，保草稿 |
| 校验错 400／对象不存在 404／备份失败 500 | 未保存 | role-apply／diff | `saved:false` 才是提交失败 |
| 403 `{"ok":false}` | Host/Origin 不在允许集 | 所有路由 | 配置/部署问题，非业务拒绝 |
| `capability_missing` | 宿主缺能力（适配器侧） | 章 3 | 与 `unsupported`／`unknown` 区分（章 3） |
| `pending_approval`／`forbidden`／`not_covered` | 目标设计的 `office act` 结果 | **未实现** | 只在 mock 标「设计」，不当真 |
| 邮件 `waiting`／`reminded`／`failed` | 待提醒／已提醒／失败 | 邮件源状态 | 保留原词；不并成 accepted；落盘≠通道受理≠提醒展示≠消费 |
| `unknown` | 无证据 | 各处 | 不补成成功或 0 |

## 4.3 不得推断
岗位≠信箱（`mb:<box>` 只是遗留信箱节点，不能当岗位稳定 ID）；候选≠现任；升级受理人不兼任被升级岗位；`online=true` 只是投递资格，≠连接健康，`false` 可能是老板手动下线；`waiting` 不等于通道已受理；有效树漏信箱也要有「未编入」区。
