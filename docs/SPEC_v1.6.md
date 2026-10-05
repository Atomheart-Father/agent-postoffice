# 开发说明 v1.6：稳定认人、只为真阻塞提醒、回执合并排后

发起：T0（2026-10-05 批准三项）。规格/审核：postoffice_admin（Claude「‼️POSTOFFICE」）；联审：postoffice_codex。开发：postoffice_lab。
分支 `v1.6-identity-alerts`（基于 master 175ac13），工作副本 `~/code/agent-postoffice-lab`。

## 1. 会话改名、回退后照样认人

**问题（真实发生）**：Claude 信箱按会话标题认人。T0 把会话改名为「‼️POSTOFFICE」后，钩子日志连续记“未登记会话”，信进不来，直到人工重登记。钩子 payload 里的 CLI `session_id` 也不稳定：同一个桌面会话因回退先后换了 3 个 CLI id。

**稳定身份**：Claude 桌面 App 的会话 id（`local_...`）。回退、改名都不变；fork 会得到新 id。来源：`~/Library/Logs/Claude/main.log`（也查同目录 `main*.log` 轮转文件，新的优先）中的行
`Mapping internal session local_<X> to CLI session <Y>` —— 用钩子拿到的 CLI session_id 反查最近一条映射得到 local id。路径可用环境变量 `POSTOFFICE_CLAUDE_MAINLOG` 覆盖（测试用）。

**行为**：
- routes 里 Claude 信箱新增字段 `claude_session`（local id；已有的 manager/art_d 已登记过该字段，直接沿用）。
- 钩子认人顺序：① 解析到 local id 且某信箱 `claude_session` 等于它 → 就是这个信箱（标题不同也认；顺手把 `claude_title` 更新为当前标题并记日志）；② 解析不到 local id（命令行版、日志缺失）→ 按现行标题匹配；
- 首次按标题匹配成功且该信箱还没有 `claude_session`、本次又能解析到 local id → 自动绑定并记日志。
- **同名不接管**：标题匹配到某信箱，但它已绑定了别的 local id → 不认，记日志并**立即**通知人一次（同一对 local id/信箱只通知一次）。
- 未登记的普通会话（既不匹配 local id 也不匹配标题）照旧静默退出，不通知。
- `postoffice add <信箱> --claude "<标题>" [--claude-session local_...]`：可直接指定 local id；`doctor` 显示每个 Claude 信箱是否已绑定。
- 解析桌面日志属于非公开格式：任何解析失败都只退回标题匹配，不得让钩子报错退出。

## 2. 只为真正耽误事的信提醒人

**问题（真实发生）**：“提醒后 30 分钟未处理”4 次全是仅告知的政策广播/广播汇总；02:22 一封旧积压信在信箱上线后 6 秒就被报“20 分钟未送达”，1 秒后实际已唤醒（按信件创建时间计时导致误报）。

**行为**：
- 30 分钟未处理提醒（check_consumed）只对**需要处理**的信生效。按信头 `需要：` 分类：
  - 含“回复 / 审核 / 处理 / 修改 / 决定 / 确认”等 → 需要处理 → 保留提醒；
  - 含“仅告知 / 无需 / 不用回 / 默认不答复”，或是回执通知（有 `回执：` 头）/ 广播汇总 → 不提醒人，只算积压；
  - 识别不了的 → **照旧提醒**（宁多不漏）。
  分类函数集中一处，文档里写清推荐的标准写法（`需要：回复` / `需要：审核` / `需要：仅告知`）。
- 20 分钟未唤醒 / 未送达计时：起点改为 `max(信件写入时间, 该信箱最近一次转为在线的时间)`，离线时长不计入。`apply_status` 转为 online 时记录 `online_since`（存 routes 或单独状态文件均可，重启后仍有效）。
- 广播本身是否需要处理看它自己的 `需要：`，不因是广播一律免提醒。

## 3. 回执合并成一条，排在正式信后面

**问题（真实发生）**：01:44–01:48 给 postoffice_codex 投了 3 份交付 + 3 条回执，触发限流，01:48:37 的“发布完成”正式信晚了约 6 分钟才送达。

**行为（T0 定）**：同一信箱同一次唤醒里，**正式信在前，回执合并成一条放最后**，例如：
```
另有 3 条回执（默认不答复，需要时按 ID 查询）：
- postoffice_lab：<原事由短句>  查询：postoffice receipt <信箱> '<ID>'
- manager：……
- art_d：……
```
- Claude 钩子：本来就把多封新信放在一次唤醒里 —— 改为正式信在前、回执合并块在后。
- OpenCode 插件：有正式信时先投正式信（一次一封，照旧）；没有正式信时，把积压的回执合并成**一条**投出去。回执最多等 10 分钟：超过 10 分钟就随下一次投递一起带上，不能被正式信永远挤掉。
- codex_queue（邮递员）：同上——正式信逐封先排；回执合并成一条 queue 消息。
- 限流按“唤醒次数”算：合并后的一条只算 1 次。去重、离线、账本（每个 ID 仍单独记已送达）照旧。
- 归档仍按 ID（`archive-receipt` 可一次传多个 ID 或全部，已有能力就复用）。

## 测试（加进 tests/smoke.sh，全在临时目录）
1. 认人：假 main.log 映射 CLI id→local id；信箱绑定该 local id 后，标题改成别的仍被唤醒；`claude_title` 被更新。
2. 回退：同一 local id 换一个新 CLI id，照样认人。
3. 同名不接管：另一个 local id、同标题 → 不认，通知人恰好一次（再触发不重复）。
4. 无 main.log：退回标题匹配，钩子不报错。
5. 首次自动绑定：标题匹配 + 能解析 local id → 写入 `claude_session`。
6. 30 分钟提醒：需要“回复”的信会提醒；“仅告知”、回执通知、广播汇总不提醒；无法识别的会提醒。
7. 20 分钟计时：信写于 1 小时前、信箱刚转在线 → 不立即告警；在线后满时限才告警（用环境变量把时限缩到秒级）。
8. 合并回执（用 01:44 的真实形态复现）：给 codex 假信箱 3 封正式 + 3 条回执 → 正式信先送、3 条回执合并成 1 条消息且含 3 个 ID；不触发限流；每个 ID 账本都记已送达；重跑不重复。
9. 回执不饿死：只有回执时会合并投出；正式信持续时，超过 10 分钟的回执随下一次投递带上。
10. 原有测试全部继续通过；`/usr/bin/python3 -m py_compile postoffice`；`node --experimental-strip-types --check opencode/postoffice.ts`。

## 约束
- 只改 `~/code/agent-postoffice-lab`；不碰 `~/code/agent-postoffice`（线上）、不改 `~/agent-postoffice` 的配置和账本（发信除外）、不 push、不重启任何 App、不动 launchd/Claude/OpenCode 设置。
- 纯标准库、Python 3.9 兼容；沿用现有风格；中英 README、MAILBOX_GUIDE、SKILL 同步更新（含“需要”字段的标准写法）。
- 提交作者：`-c user.name="Atomheart-Father" -c user.email="90882389+Atomheart-Father@users.noreply.github.com"`。
- 完成后发信给 postoffice_admin（需要：审核），附 smoke 末尾几行；抄送说明可给 postoffice_codex（仅告知）。规格不清先问，不自改规格。
