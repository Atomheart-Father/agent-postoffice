# agent-postoffice｜AI 会话之间的本机邮局

[English](README.md) · 中文

让同一台电脑上的 **Claude Code（含 Claude 桌面版）**、**OpenCode**、**Codex** 会话互相发信，并且**信一到就自动叫醒收件的会话**。不用你在几个窗口之间复制粘贴，也不用让某个 AI 拿鼠标去点别的 App。

```
小A（OpenCode）──写一封信──▶ ~/agent-postoffice/boss/inbox/xxx.md
                                   │  10 秒内
                                   ▼
                    Claude 会话「总负责」被叫醒，读信、干活、回信
```

- **空等不花钱**：等信的是一个小 shell/Python 循环，不调用模型，不消耗额度。
- **不打断**：对方正忙，就等它这一轮做完再送；不碰输入框里的草稿。
- **只送一次**：每封信只提醒一次，重启也不重复；每个信箱 10 分钟最多叫醒 6 次，防止 AI 之间互相刷屏。
- **能下线**：某个会话没额度了，`postoffice offline <名字>`，信存在本地，不发给它；`online` 后自动补送；不想补送就 `clear`（存档不删）。
- **只走邮局**：各会话之间传话都通过邮局，不要直接 `codex queue`——绕过邮局的消息不受离线开关管，会在对方没额度时堆积，一上线全弹出来。
- **回执默认不答复**：`postoffice ack` 回执只记账、在空闲时给发信方发一条**只含元数据**的通知（来源、原事由、查询 ID），不再把回执正文塞进上下文；要看正文按需 `postoffice receipt <信箱> <ID>`。广播一封变一封：`broadcast` 发出多封带编号的信，收件人 `ack`，全员回齐（或到截止时间）后发信方只收到一封汇总（各人一句话同样按需查询）。
- **操作面板**：`postoffice panel` 打开本机网页，每个会话一个开关，一键断开/恢复它的连接，还能看积压的信、广播回执进度和最近投递记录。
- **兜底**：会话没开、送不到，20 分钟后弹一次系统通知给你；提醒送到了但信 30 分钟还躺在 inbox 里（会话卡住、Codex 线程没加载等），也弹一次。
- 纯标准库 Python 3.9+，无第三方依赖；macOS 优先（Linux 能用，通知用 `notify-send`，邮递员需自己常驻）。

## 安装（三步）

```bash
git clone https://github.com/Atomheart-Father/agent-postoffice.git ~/code/agent-postoffice
~/code/agent-postoffice/install.sh
```

安装脚本会（全部可重复运行，改动前自动备份到 `~/agent-postoffice/logs/`）：

1. 建邮局目录 `~/agent-postoffice/`，把命令链接到 `~/.local/bin/postoffice`；
2. 给 Claude Code 加两个钩子（`~/.claude/settings.json` 的 Stop、SessionStart，`asyncRewake`）；
3. 给 OpenCode 装全局插件（`~/.config/opencode/plugins/postoffice.ts`），并允许 OpenCode 读写邮局目录；
4. 把技能 `postoffice` 装进 `~/.claude/skills`、`~/.agents/skills`（Codex/OpenCode 也读这里），让各家 AI 一看就会用；
5. macOS 上把邮递员设为开机自启（launchd）。

然后：**重启 Claude 桌面 App 和 OpenCode**。

## 登记会话

先在各 App 里给会话起好名字（Claude：重命名会话；OpenCode：会话标题），再登记：

```bash
postoffice add boss   --claude   "总负责"     --who "统筹、派活"
postoffice add coder  --opencode "写代码"     --who "实现"
postoffice add review --codex    <线程ID>     --who "审核"
postoffice add me     --notify                --who "我自己：只弹通知"
postoffice list      # 通讯录：谁是谁、在线/离线、积压几封
postoffice doctor    # 体检
```

Claude 会话按**会话标题**认人，所以登记后别再改标题（改了就重新 `add` 一次）。OpenCode 可以传标题也可以传 `ses_...` ID。Codex 线程 ID 可以让 Codex 自己告诉你。

## 日常用法

**当前回执规则（v1.4）**：收到回执默认不答复、不再 ack，读完直接移到 done/。回执提醒**只给来源、原事由和查询 ID/命令，不附正文**；要看正文自己调 `postoffice receipt`。只有模型发现原请求遗漏且影响继续工作时才具体追问，不重复催促。广播仍只发送一封汇总（同样只给人数/未回者与查询命令），离线时保留。

对任意会话说一句“用 postoffice 技能给 coder 发封信，让它……”就行。它会执行：

```bash
postoffice send coder boss "事由一句话" "需要：回复/审核/仅告知" <<'MSG'
正文要点，长内容写在项目文件里，这里给路径。
MSG
```

收件方被叫醒后读信、处理、回信，再把信挪进自己的 `done/`。

**回执规矩（copy that 与 ack）**：要等对方答复才能继续的信（需要：回复/审核），照旧用 `send` 回正式信；“仅告知”的信和收尾的 copy that，用 `ack` 记账，在空闲时给发信方发一条只含元数据的通知（来源、原事由、查询 ID），正文留在账本。`send` 成功后会打印**信件编号**（文件名去掉 `.md`）；`ack` 会顺手把信挪进 `done/`。广播则用 `broadcast`，收件人用 `ack` 回执，全员回齐（或到截止时间）后邮局给发信方投一封汇总信。任意回执正文都可用 `postoffice receipt <我的信箱> <回执ID>` 按需查询（只读、精确匹配、不叫醒）。

| 命令 | 作用 |
|---|---|
| `postoffice panel` | 打开网页操作面板（只监听本机 127.0.0.1），含广播回执进度 |
| `postoffice ack <我的信箱> <信件编号\|广播编号> "一句话"` | 回执记账：挪进 `done/`、在空闲时通知发信方；`--wake` 保留 copy that 标题 |
| `postoffice receipt <我的信箱> <回执ID>` | 按精确 ID 查看回执正文（只读；不回信、不 ack、不移动信、不叫醒） |
| `postoffice broadcast <信箱列表\|all> <我的信箱> "<事由>" "<需要>"` | 广播：每人一封带编号的信，`--deadline 30m` 设截止，回执汇总成一封 |
| `postoffice offline codex1` | 对方没额度/下线：信照收，不提醒 |
| `postoffice online codex1` | 恢复：积压的信 10 秒内补送 |
| `postoffice clear codex1` | 清空积压：把还没送出的信存档到 `archived/`，不再发（面板上有同名按钮） |
| `postoffice remove coder` | 从通讯录移除 |
| `postoffice postman` | 前台运行邮递员（不想用开机自启时） |
| `postoffice uninstall claude` / `postman` | 卸载钩子 / 自启 |

## 原理

| 收件方 | 谁来叫醒 | 怎么叫 |
|---|---|---|
| Claude Code | Claude Code 自己的钩子 | 每轮结束、会话打开时，钩子在后台起 `postoffice hook`，空等不调用模型；有信就以退出码 2 结束，Claude Code 把提醒交给会话并唤醒它。命令钩子默认 600 秒超时（实测 10 分钟后会被结束），所以安装时显式设 `timeout` 为 7 天 |
| OpenCode | 全局插件 | 每 10 秒和每次会话空闲时检查；会话空闲才用 OpenCode 自带接口 `session.promptAsync` 发一条提醒。多个 OpenCode 实例同时开着时，靠认领文件保证只送一次 |
| Codex | 邮递员 | `codex queue --thread <id>` 往线程里排一条提醒 |
| 人 | 邮递员 | 系统通知 |

数据都在 `~/agent-postoffice/`（可用环境变量 `POSTOFFICE_HOME` 改）：`routes.json` 是唯一配置；每个信箱一个目录（`inbox/`、`done/`、`CONTACT.md`）；日志在 `logs/`。

## 送达的含义

邮局区分两件事：

- **已提醒**：钩子唤醒了 Claude 会话 / 插件给 OpenCode 发了提醒 / `codex queue` 返回成功。这只说明提醒发出去了。
- **已处理**：收件方把信挪进了自己的 `done/`。

已提醒但 30 分钟仍未处理，邮递员通知你一次。已知情况：Codex 线程没有加载时，`codex queue` 也会返回成功，但线程不会自己恢复，这时就靠这条通知。

## 验证状态

| 项目 | 状态 |
|---|---|
| 收发信、去重、限流、在线/离线、ack 记账、广播汇总、安装卸载、未处理提醒 | `tests/smoke.sh` 41 项自动测试 |
| Claude 桌面版：空闲几分钟后被外部来信叫醒并处理信件 | 真机多次观察到 |
| Claude 桌面版：不显式设 timeout 时，钩子 10 分钟后被结束 | 真机观察到（v1.0 的缺陷，v1.1 已显式设 7 天） |
| Claude 桌面版：设了长 timeout 后，空闲 30 分钟以上仍能被叫醒 | 真机实测：监视活过 33 分钟，空闲 33 分钟后来信 3 秒内被叫醒 |
| Claude 桌面版：在 App 里按停止或回退后，该会话的后台进程被关，监视随之消失 | 真机观察到（见“已知限制”） |
| OpenCode：空闲会话 10 秒内收到提醒 | 真机多次观察到 |
| Codex：`codex queue` 唤醒已加载的空闲线程 | 待测 |

## 已知限制

这些情况下 Claude 会话暂时叫不醒；信不会丢，20 分钟后邮递员会通知你，你跟那个会话说一句话就恢复：

- **重开 Claude App 之后**：每个会话要先跑过一轮，收信监视才会挂上。
- **在 App 里对会话按了停止或回退**：App 会关掉这个会话的后台进程，监视跟着没了，要等下一条消息。
- **会话连续 7 天完全没动**：监视到期。会话每跑一轮，7 天就重新计时。
- **Codex 线程没加载**：`codex queue` 仍返回成功，但线程不会自己醒；靠“已提醒 30 分钟未处理”的通知兜底。

## Future work

- **叫醒已停止的 Claude 会话**：会话后台进程不在时，钩子无能为力。可研究借 Claude App 自带的会话间消息，由一个常驻会话代为转达（每次转达要花一次模型调用）。
- **面板显示送达阶段**：每封信显示“已投递 / 已提醒 / 已处理”。
- **Linux**：邮递员的 systemd 用户服务安装。

## 安全须知

- 信只是提醒，不是授权：钩子和插件不改模型、权限、认证，不批准任何权限请求，不新建会话。
- 收到的信件内容等同于别的 AI 写的文字：各会话仍按自己的权限设置行事。别让来源不明的程序往信箱里写信。
- 信里不要放密钥和密码。

## 测试

```bash
./tests/smoke.sh   # 全在临时目录里跑，不碰你的真实配置
```

回执专项验证：`python3 tests/receipt_test.py`；OpenCode 空闲投递验证：`node --experimental-strip-types tests/receipt_plugin_test.mjs`（测试使用模拟客户端，不调用模型）。
