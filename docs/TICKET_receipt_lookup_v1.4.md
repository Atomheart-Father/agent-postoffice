# 开发记录：回执只提醒编号，内容按需查询（v1.4）

本文记录 v1.4 的设计、实施与验收证据。基线：`9f46a10`（v1.3）。由两个 AI 会话协作完成：一个实现、一个设计验收。

## 最小接口与行为

1. 新公共命令 `postoffice receipt <我的信箱> <回执ID>`：精确查询，不做模糊搜索。普通 ID 沿用原信 ID；广播沿用广播 ID。模型 shell 工具即可调用；skill 给出准确例子，不引入 MCP 服务或第三方依赖。
2. 提醒只给来源、原事由（截短）、查询 ID 和查询命令，明确默认不答复。禁止自动附回执正文、note、广播各人的一句话；不要求模型先读通知文件。通知归档操作可以给路径，但不能要求 cat 该文件。如需查看内容，由模型主动调用查询命令。
3. 查询只返回与该信箱相关的确切 ID 的回执（`acks.jsonl` 中 `to=我的信箱`），给出来源、时间、内容。广播查询还可返回汇总状态/未回者，不推断其动机。未知 ID / 非我的回执 / 未登记信箱：非零退出，简明错误，不泄露其他信箱内容。该命令只读，不发信、不 ack、不移动信、不改在线状态、不叫醒任何会话。
4. `acks.jsonl` 继续是回执正文唯一来源；新普通通知文件只保存索引/元数据，不再复制 note。广播通知保留人数/截止等简短状态，详细 note 留在账本、查询时输出。不要另建第二套回执数据库。
5. 三条实际提醒路径都覆盖：Python reminder（Claude hook / Codex queue）及 OpenCode plugin。对旧版已有回执通知文件也只输出元数据，不能因 legacy 文件里有正文就再注入。
6. 默认不答复、不再 ack；仅必要问题遗漏才 send 具体追问，不能循环催促。误 ack 回执通知仍只归档、不生成回执。重复 ack 幂等。广播仍只发一封汇总；离线、空闲、限流、重启去重继续沿用现有运输。
7. `--wake` 保留兼容，但同样不自动注入正文。面板给人看的回执内容可以保留；不为此记录重构面板、安装器或模型权限。

## 实施边界

仅修改本仓库的 `postoffice`、`opencode/postoffice.ts`、`skill/postoffice/SKILL.md`、`docs/MAILBOX_GUIDE.md`、`docs/SPEC_broadcast_ack.md`、`README.md`、`README.zh-CN.md` 及 `tests/`。纯标准库、Python 3.9 兼容。

## 验证与交付

先从 send → ack → 提醒无正文 → receipt 按 ID 返回正文做一个 red→green 纵切。然后补必要反例：

- 正文 sentinel 不出现在普通、广播、`--wake`、legacy 提醒里；查询对应 ID 才出现。同时测 Python hook 输出、Codex queue mock 捕获输出、OpenCode mock prompt。
- ID 精确，不以子串匹配；异箱/不存在查询不泄露内容，查询前后邮局文件内容与收发行为不变。
- 普通/广播回执内容正确；旧账本兼容，无新增 note 也能查到旧记录。
- 忙时不投、空闲投、离线保留、重复不重投、防回执循环。
- 更新 smoke/receipt/plugin 测试，避免保留旧版“提醒必须含正文”的错误断言。正常回归跑一遍，不按人数重复全量。
- 所有测试在临时目录或模拟客户端；不向真实信箱发测试信、不调用额外模型。

失败只停受影响部分并保留首个失败；不改变既有通知权限或自动扩大范围。

## 实施证据

- 版本 1.3.0 → 1.4.0。改动文件：`postoffice`、`opencode/postoffice.ts`、`skill/postoffice/SKILL.md`、`docs/MAILBOX_GUIDE.md`、`docs/SPEC_broadcast_ack.md`、`README.md`、`README.zh-CN.md`、`tests/{smoke.sh,receipt_test.py,receipt_plugin_test.mjs}`。
- 新增只读命令 `postoffice receipt <信箱> <回执ID>`：只读 `acks.jsonl`（`id`+`to` 精确匹配），普通回执给来源/时间/内容；广播命中广播记录且 `from==信箱` 时追加「已回执 n/m、截止、汇总状态、未回执」，不泄露他箱；未知/非我/未登记非零退出。（初版按 `broadcasts/<id>.json` 定位；现行改用精确 ID 扫描，见“审阅修订一”。）
- `reminder()` 与插件提醒改为**元数据**：`来源`、`原事由`（截短 60）、`查询 ID`、`查询命令`，不读文件正文；旧通知（正文在文件里）同样只按前 6 行元数据提醒。
- `cmd_ack` 通知正文不再含 note，改为指针 `postoffice receipt <to> <id>`；`summarize_broadcasts` 汇总只给人数/截止/未回者 + 查询命令，不再列各人一句话。
- 提醒样本（示意）：`【联络总站回执｜<box>】来自 <sender> 的回执，原事由：<subject>` / `查询 ID：<id>` / `查询命令：postoffice receipt <box> <id>` / `默认不答复…回执正文不在通知里…`。

### 审阅修订一（同日）

- 普通信提醒恢复 `== <信件路径>` 与 `编号：`，普通信仍按路径读全文，不变成只有信头。
- 回执提醒补 `通知：<文件路径>` 与 `归档：mv <文件> <done>/`（只操作通知，不 cat、不输出正文）；压缩提示为 来源/原事由/通知/归档/ID/查询命令/默认不答复。
- 回执 ID 只做精确**标识**匹配：新增 `broadcast_record()` 扫描 `broadcasts/*.json` 按 `rec["id"]==id` 命中，不再用 `broadcasts/<id>.json` 拼路径；`../`、`../../../../etc/hosts` 等 ID 非零退出且不泄露。

### 审阅修订二（同日）

- 生成的 shell 命令全部做 POSIX 引用：Python 用 `shlex.quote`（reminder 的 `归档：mv` 与 `查询命令：`、通知文件正文指针、广播汇总指针）；OpenCode 插件用本地 `shq()` 单引号引用（`归档`/`查询命令`/普通信 `ack` 提示）。文件名可含 `（）';`，`POSTOFFICE_HOME` 可含空格。
- 最小实测 `test_generated_commands_are_shell_quoted`：`POSTOFFICE_HOME` 带空格，事由含括号/单引号/分号（id 同），把提醒里输出的 `查询命令` 与 `归档：mv` 原样交给 `sh -c` 执行——查询能取回哨兵、归档把通知移入 `done/`。

### 回归结果（分阶段）

- 初版：`py_compile`(3.9) OK；`node --check` OK；`tests/receipt_test.py` 10/10；plugin PASS；`tests/smoke.sh` 46/46。
- 审阅修订一后：receipt_test.py 11/11；plugin PASS；smoke 46/46。
- 审阅修订二后（现行）：receipt_test.py 12/12；plugin PASS；smoke 46/46；`py_compile`(3.9)+`node --check` OK。
