# agent-postoffice｜AI 会话之间的本机邮局

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
- **操作面板**：`postoffice panel` 打开本机网页，每个会话一个开关，一键断开/恢复它的连接，还能看积压的信和最近投递记录。
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

对任意会话说一句“用 postoffice 技能给 coder 发封信，让它……”就行。它会执行：

```bash
postoffice send coder boss "事由一句话" "需要：回复/审核/仅告知" <<'MSG'
正文要点，长内容写在项目文件里，这里给路径。
MSG
```

收件方被叫醒后读信、处理、回信，再把信挪进自己的 `done/`。

**回执规矩**：收到任何信都至少回一句“copy that”和下一步；原发信方再回一句“copy that”，这次交流就结束。AI 会话要收到消息才会开始新的一轮，不回执，对方就会一直停着。

| 命令 | 作用 |
|---|---|
| `postoffice panel` | 打开网页操作面板（只监听本机 127.0.0.1） |
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

## 验证状态（v1.1）

| 项目 | 状态 |
|---|---|
| 收发信、去重、限流、在线/离线、安装卸载、未处理提醒 | `tests/smoke.sh` 17 项自动测试 |
| Claude 桌面版：空闲几分钟后被外部来信叫醒并处理信件 | 真机多次观察到 |
| Claude 桌面版：不显式设 timeout 时，钩子 10 分钟后被结束 | 真机观察到（v1.0 的缺陷，v1.1 已显式设 7 天） |
| Claude 桌面版：设了长 timeout 后，空闲 30 分钟以上仍能被叫醒 | 待真机长时间测试 |
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

- **广播与回执统计**（未开发）：现在要通知所有人，只能逐个发信，每个人再各回一封 copy that，发信方会被一封封回执反复叫醒，信箱也乱。计划：
  - `postoffice broadcast <信箱列表|all> "<事由>" "<需要>"` 生成一个广播编号，给每个收件人投一封带编号的信；
  - 收件人用 `postoffice ack <编号> ["一句话"]` 回执：只记账，不单独叫醒发信方；
  - 全员回执齐了（或到截止时间），邮局给发信方投**一封**汇总信：谁回了、各自说了什么、谁还没回；
  - 面板上显示每个广播的回执进度。
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

## English (short)

A local mailbox for AI coding agents on one machine. Drop a `.md` file in `~/agent-postoffice/<name>/inbox/` (or `postoffice send`), and the recipient session is woken automatically: Claude Code via `Stop`/`SessionStart` hooks with `asyncRewake` (zero model calls while idle), OpenCode via a global plugin using `session.promptAsync` when the session is idle, Codex via `codex queue`. Deliver-once ledgers, rate limiting, online/offline per mailbox, a 20-minute not-woken alert and a 30-minute reminded-but-unprocessed alert. Note: Claude Code command hooks default to a 600 s timeout, so the installer sets an explicit 7-day `timeout` on the async hook. Run `./install.sh`, restart the apps, then `postoffice add …`. MIT licensed.
