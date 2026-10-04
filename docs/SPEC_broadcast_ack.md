# 开发说明：回执记账（ack）与广播（broadcast）

> v1.3 用户裁决（2026-10-05）覆盖下文旧版“不叫醒”的规则：收到回执默认不答复、不再 ack，读完直接移到 done/；只有模型发现原请求遗漏且影响继续工作时才具体追问，不重复催促。普通回执通过现有投递通道在空闲时提醒，离线时保留；广播仍只发送一封汇总。 `--wake` 保留 copy that 标题。下文其余为 v1.2 开发历史。

发起：T0；规格与审核：postoffice_admin（Claude 会话「Agent 邮局唤醒机制」）；开发：postoffice_lab（OpenCode 会话「邮局实验」）。
分支 `broadcast-ack`，工作副本 `~/code/agent-postoffice-lab`。

## 要解决的问题

1. 每封信都要回执（copy that），每封回执都会叫醒对方：“仅告知”的信和收尾的 copy that 也要叫醒一次，打断对方上下文、浪费调用。
2. 通知所有人只能逐个发信，每人再各回一封 copy that，发信方被一封封回执反复叫醒，信箱很乱。

## 规则（要写进文档）

- 对方要等你答复才能继续（需要：回复 / 审核）→ 照旧用 `send` 回一封正式信（会叫醒对方）。
- “仅告知”的信、收尾的 copy that → 用 `ack` 记账，不叫醒对方。
- 广播 → 每人用 `ack` 回执；全员回齐（或到截止时间）才给发信方投**一封**汇总信。

## 功能

### A. 信件编号
- 信件编号 = 文件名去掉 `.md`（例如 `20261004-223334_manager_事由`）。`send` 成功后打印编号。
- 三行信头格式不变（来源 / 事由 / 需要）。广播信在第 4 行加 `广播：<广播编号>`。

### B. `postoffice ack <我的信箱> <信件编号|广播编号> ["一句话"]`
- 由**收件人**执行。在 `<HOME>/acks.jsonl` 追加一行：`{time, by, id, kind: "letter"|"broadcast", note, to}`，`to` 为原发信方（取自信头“来源：”的第一个词；广播取广播记录里的发信方）。
- 自动把对应原信从 `<我的信箱>/inbox/` 挪到 `done/`（ack 即“已处理”）。信已在 `done/` 也照常记账。
- 默认**不叫醒**原发信方。加 `--wake` 时，再用 `send` 给原发信方投一封 `copy that：<原事由>`（正常叫醒）。
- 同一人对同一编号重复 ack：只保留第一次（幂等，打印“已回执过”）。
- 编号不存在或不是发给我的：报错，退出码非 0，不写账。

### C. `postoffice broadcast <信箱,信箱,...|all> <发信信箱> "<事由>" "<需要>" [--deadline 30m] [--file F]`
- 正文从标准输入或 `--file`。`all` = 通讯录中除发信方外的全部信箱。
- 生成广播编号 `B<YYYYMMDD-HHMMSS>_<发信方>`，记录到 `<HOME>/broadcasts/<编号>.json`：`{id, from, subject, need, to:[...], deadline, created, acks:{}, summarized:false}`。
- 给每个收件人各投一封信（走与 `send` 相同的写入路径，受各自在线 / 离线开关管：离线者信存在本地）。信头第 4 行 `广播：<编号>`，正文末尾自动附一句“回执请用：postoffice ack <你的信箱> <编号> \"一句话\"”。
- 默认截止时间 30 分钟。

### D. 邮递员（`cmd_postman`）汇总
- 每轮检查 `broadcasts/*.json`：全员已 ack，或已过截止时间且未汇总 → 给发信方投**一封**汇总信（会叫醒），然后标记 `summarized: true`。
- 汇总信内容：广播事由；已回执的人和各自的一句话；未回执的人（注明离线的）。
- 汇总只发一次；邮递员重启不重复。

### E. 面板
- `/api/state` 增加 `broadcasts`（最近 10 个：编号、事由、发信方、已回 n/总 m、是否已汇总、截止时间）和每个信箱最近收到的 ack。
- 页面增加“广播”区块，显示每个广播的回执进度；信箱卡片上显示“最近回执”。

### F. 文档
- 中英两份 README（`README.md` 英文、`README.zh-CN.md` 中文）、`docs/MAILBOX_GUIDE.md`、`skill/postoffice/SKILL.md` 都写清上面的“规则”和新命令。Future work 里删掉已完成的“广播与回执统计”。

## 测试（加进 `tests/smoke.sh`，全部在临时目录里跑）
1. ack 一封普通信：写账、信挪到 done、不产生给原发信方的新信。
2. `ack --wake`：原发信方 inbox 多一封 `copy that：…`。
3. 重复 ack 幂等；ack 不存在的编号报错、不写账。
4. broadcast 给 3 个信箱（其中 1 个离线）：3 封信都在各自 inbox，第 4 行有广播编号。
5. 2 人 ack 后跑邮递员：未到截止、未全员 → 不汇总；全员 ack → 发信方收到恰好一封汇总；再跑邮递员不重复。
6. 截止时间到但有人没回：汇总列出未回执的人（用环境变量把截止时间调成秒级）。
7. 原有 18 项全部继续通过。

## 约束
- 只改 `~/code/agent-postoffice-lab` 里的文件。**不要碰** `~/code/agent-postoffice`（线上在用的代码），不要改 `~/agent-postoffice` 里的配置和账本（发信除外），不要 push，不要动 launchd / Claude / OpenCode 的设置。
- 纯标准库，兼容 Python 3.9（用 `/usr/bin/python3 -m py_compile postoffice` 检查）。
- 改动小而集中，沿用现有代码风格。
- 完成后：在分支上提交（作者用 `-c user.name="Atomheart-Father" -c user.email="90882389+Atomheart-Father@users.noreply.github.com"`），然后发信给 `postoffice_admin`：改了什么、测试结果（贴 smoke 输出最后几行）、未完成项。合并和发布由 postoffice_admin 负责。
- 遇到规格说不清或有更好的做法：先发信问 `postoffice_admin`，别自己改规格。
