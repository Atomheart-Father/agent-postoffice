# 项目 STATUS 模板与约定（匿名）

> 这是一份**约定模板**，不是邮局的功能。邮局不解析、不追踪、不自动更新它；下面写明的自动化只有一条：`postoffice identity` 在展示 `rules_file` / `status_file` 指针时会读一眼文件，按 `AS_OF` 与 `SOURCE` 给出一行**只读提示**（见文末）。除此之外全部靠人工维护。

## STATUS 是什么

- STATUS 是**当前状态 + 证据指针**的一份简短摘要，回答「现在到哪了、下一步是什么、卡在哪」。
- 它**不是**能压倒更新的用户裁决、冻结票或新证据的缓存。当更新的裁决/证据出现而摘要还没改时，以更新的那份为准，并尽快同步 STATUS。
- 一个项目一份短 STATUS 即可；把「当前」与「历史」分开，历史留在报告/日志里，不要把上千行历史塞进 STATUS。

## 头部字段（建议）

在正文最前面给两行元数据：

```
AS_OF: 2026-10-08
SOURCE: handoff/dispatch/2026-10-08/TICKET.md
SOURCE: core/output/run5/result.json
```

- `AS_OF:` 写**这份摘要写于何时**（`YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM`）。可选的；缺省时无法判断新鲜度。
- `SOURCE:` 写**这份摘要依据的证据指针**（可重复多行，每条一个路径）。相对路径相对 STATUS 文件所在目录解析。
- 「来源晚于摘要」= 某个存在的 `SOURCE` 文件的修改时间晚于 `AS_OF`。这只说明「摘要可能落后于证据」，**不等于**审批/生效顺序，也不能自动代表谁对谁错。

## 正文章节（建议，保持简短）

```
CURRENT:   现在这一版做完了什么
STATE:     关键版本 / 路径 / 环境（含可回滚点）
LATEST DECISIONS: 最近影响方向的裁决（一条一句，带来源）
BLOCKER:   现在挡路的（没有就写 none）
NEXT:      下一步
OUTPUT:    交付物路径
```

## 未结项列表（`OPEN_ITEMS`，可选）

需要跟踪「谁欠什么、直到交付信把它关掉」时，用一段人工维护的列表明说，而不是把欠账藏在自由文本里：

```
OPEN_ITEMS:
- id: OI-1 | 事项: 交接 STATUS 需补 AS_OF/SOURCE | 责任岗位: @project.owner | 证据指针: handoff/.../TICKET.md | 状态: open
- id: OI-2 | 事项: 引用文件名规范化 | 责任岗位: @project.review | 证据指针: design/RECORD.md | 状态: closed
```

约定：

- 字段固定为 `id / 事项 / 责任岗位 / 证据指针 / 状态`；`状态` 只写 `open` 或 `closed`。
- **人工显式更新与关闭**：负责人改状态、写交付信是动作，列表只是记录。
- **不要**从信件的自由 `需要：` 正文自动推断欠账；`ack`（回执）**不代表**结项，只有人明确把状态改成 `closed`（或对应的交付信）才算。
- 邮局**不做**跨信件的欠账注册表 / workflow 数据库：这一节就是纯文本约定。

## 邮局做的事（唯一一处自动化，只读）

`postoffice identity <信箱>` 在展示指针时，会读一眼 `rules_file` / `status_file`：

- 文件不存在 → 标 `（缺文件）`。
- 文件有 `AS_OF`，且某个存在的 `SOURCE` 文件 mtime 晚于它 → 标 `（来源晚于摘要 AS_OF <值>，待刷新；仅提示）`。

这只是**提示**：mtime 不是审批时序，提示**不会**替你更新 STATUS、不会自动总结、不会写进任何信、不监控文件变化。设置组织后，`identity --json` 的 `company_rules` / `project_status` 每项带一个 `freshness` 对象（`state` / `as_of_text` / `stale` / `sources`），面板与 CLI 共用同一份推导。
