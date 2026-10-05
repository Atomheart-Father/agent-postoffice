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
- **回执默认不答复**：`postoffice ack` 回执只记账、在空闲时给发信方发一条**元数据通知**（来源、原事由、一条查询命令），不再把回执正文塞进上下文；单条回执三行以内，同一轮里多条回执合并成一条“另有 N 条回执”清单、排在正式信后面。正文仅按需 `postoffice receipt <信箱> <ID>` 查询，处理提醒后即可 `postoffice archive-receipt <信箱> <ID>` 归档（无需先查正文）。广播一封变一封：`broadcast` 发出多封带编号的信，收件人 `ack`，全员回齐（或到截止时间）后发信方只收到一封汇总（各人一句话同样按需查询）。
- **操作面板**：`postoffice panel` 打开本机网页，每个会话一个开关，一键断开/恢复它的连接，还能看广播回执进度和最近投递记录。每个信箱显示 **待投递 / 已提醒待归档 / 投递失败** 三态计数，与列表取自同一份逐信快照（待投递=还没发出去；已提醒待归档=通道已接受提醒但信还在 `inbox/`；投递失败=该通道已放弃、需要人工看）。不新增状态库：已提醒取自 Claude 的 `.seen`、插件台账的 `DELIVERED` 行或邮递员的接受记录，不把 20 分钟兜底记号当提醒，`FAILED_FINAL` 算投递失败而不是已送达。归档按钮的确认框会**逐项列出总数和三类数量**（待投递 + 已提醒未归档 + 投递失败需人工处理），写明是把收件箱里的**全部**信件和通知一起存档、文件保留在 `archived/`，待投递的归档后不再补送、投递失败的也一并归档不再重试。页面按浏览器语言显示（`zh*` 中文，其余英文），并有明显的中文 / English 切换按钮，切换只重画：不改状态、不调用写入接口、不唤醒会话。信箱名、信件事由、回执正文、交接路径和原始日志一律按原文显示。
- **兜底**：会话没开、送不到，20 分钟后弹一次系统通知给你——计时起点取“信落地”和“该信箱最近一次上线”里更晚的那个，离线时长不计（`online_since` 只在真正离线→在线时更新，重复点在线不刷新；升级时已在线又没基线的信箱，以邮递员首次以新版运行的时间补基线）；提醒送到了但**需要处理**的信 30 分钟还躺在 inbox 里（会话卡住、Codex 线程没加载等），也弹一次。分类：回执通知/广播汇总、`需要：仅告知` → 不提醒；`需要：回复`/`审核`、自由文本只含肯定词（回复/审核/处理/修改/决定/确认）→ 提醒；自由文本只含否定词（仅告知/无需/不用回/默认不答复/无需操作）→ 不提醒；正负混合或识别不了 → 仍提醒（不宣称零误报）。`仅告知`、回执通知、广播汇总只算积压、不打扰人。
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

Claude 会话按**稳定身份**认人：Claude 桌面版把 `CLAUDE_CODE_HOST_SESSION_ID`（`local_…`）设在会话进程上，钩子子进程继承它——改名、回退都不会再丢信。取不到时按 `~/Library/Logs/Claude/main*.log` 里的“CLI id → local id”映射反查（行内时间戳最新者优先）；命令行版（`CLAUDE_CODE_ENTRYPOINT` 非 claude-desktop）用自己的 CLI session id。首次按标题匹配会自动绑定身份，**同名但不同身份不会接管**。手工指定用 `postoffice add boss --claude "总负责" --claude-session <id>`；`postoffice doctor` 显示每个 Claude 信箱是否已绑定。OpenCode 可以传标题也可以传 `ses_...` ID。Codex 线程 ID 可以让 Codex 自己告诉你。

## 日常用法

对任意会话说一句“用 postoffice 技能给 coder 发封信，让它……”就行。它会执行：

```bash
postoffice send coder boss "事由一句话" "需要：回复/审核/仅告知" <<'MSG'
正文要点，长内容写在项目文件里，这里给路径。
MSG
```

收件方被叫醒后读信、处理、回信，再把信挪进自己的 `done/`。

**回执规矩（copy that 与 ack）**：要等对方答复才能继续的信（需要：回复/审核），照旧用 `send` 回正式信；“仅告知”的信和收尾的 copy that，用 `ack` 记账，在空闲时给发信方发一条元数据通知（来源、原事由、一条查询命令），正文留在账本；单条回执三行以内，同一轮里多条回执合并成一条清单、排在正式信后面。收到回执默认不答复、不再 ack，处理提醒后即可用 `postoffice archive-receipt <我的信箱> <回执ID>` 归档（精确匹配、幂等，不读正文、不发信、不叫醒，无需先查正文）。`send` 成功后会打印**信件编号**（文件名去掉 `.md`）；`ack` 会顺手把信挪进 `done/`。广播则用 `broadcast`，收件人用 `ack` 回执，全员回齐（或到截止时间）后邮局给发信方投一封汇总信。任意回执正文都可用 `postoffice receipt <我的信箱> <回执ID>` 按需查询（只读、精确匹配、不叫醒；未知 ID 或别人的回执会被拒绝）。

| 命令 | 作用 |
|---|---|
| `postoffice panel` | 打开网页操作面板（只监听本机 127.0.0.1），含广播回执进度、分组开关与逻辑地址当前解析对象 |
| `postoffice config import ./postoffice-config.json` | 导入 `config.json`（分组与逻辑地址）：先校验、备份旧文件、临时文件原子替换；整文件替换，不做合并 |
| `postoffice config show` | 只读查看：有哪些分组、逻辑地址、各自现在解析到谁（不创建配置、不改状态） |
| `postoffice offline @codex` / `online @codex` / `clear @codex` | 整组开关（`@` 加分组名）：先校验全部成员，再套用现有单信箱行为 |
| `postoffice send @project.manager <我的信箱> "<事由>" "<需要>"` | 发给逻辑地址：**发信那一刻**挑第一个在线候选并打印解析结果；候选全离线就失败并列出候选状态，不会把信塞进任何候选 |
| `postoffice ack <我的信箱> <信件编号\|广播编号> "一句话"` | 回执记账：挪进 `done/`、在空闲时通知发信方。普通信本来就会发通知，`--wake` 只把标题改成 copy that；广播默认不逐人发通知，加 `--wake` 才会另投一封 copy that 通知 |
| `postoffice receipt <我的信箱> <回执ID>` | 按精确 ID 查看回执正文（只读；不回信、不 ack、不移动信、不叫醒） |
| `postoffice archive-receipt <我的信箱> <回执ID>` | 回执看完归档：按精确 ID 把本箱的回执通知移到 `done/`（幂等；不读正文、不发信、不叫醒） |
| `postoffice broadcast <信箱列表\|all> <我的信箱> "<事由>" "<需要>"` | 广播：每人一封带编号的信，`--deadline 30m` 设截止，回执汇总成一封 |
| `postoffice offline codex1` | 对方没额度/下线：信照收，不提醒 |
| `postoffice online codex1` | 恢复：积压的信 10 秒内补送 |
| `postoffice clear codex1` | 清空积压：把还没送出的信存档到 `archived/`，不再发（面板上有同名按钮） |
| `postoffice remove coder` | 从通讯录移除 |
| `postoffice postman` | 前台运行邮递员（不想用开机自启时） |
| `postoffice uninstall claude` / `postman` | 卸载钩子 / 自启 |

## 分组开关与逻辑地址（可选）

没有 `~/agent-postoffice/config.json` 时这些都不存在，其余功能完全照旧。导入一份配置后多两个能力：

```json
{
  "version": 1,
  "groups": { "codex": ["manager_a", "reviewer_a"] },
  "aliases": {
    "project.manager": { "candidates": ["manager_a", "manager_b"],
                         "notify": ["coordinator_a"],
                         "handoff": "/path/to/handoff.md" }
  }
}
```

- **分组**只是已有信箱的名字：`offline @codex` / `online @codex` / `clear @codex` 直接复用单信箱行为（不新增组运行状态，信箱状态仍是唯一事实来源），面板上多一个 Groups 小区块，带“全开 / 全关”按钮，走同一个状态接口和本机来源检查。
- **逻辑地址**让发信方写 `send @project.manager …`，不必写死具体 manager。发信那一刻选第一个在线候选；已投递的旧信不搬、不重新路由；待切换也不延迟新信。
- **切换广播**：候选连续稳定 60 秒（测试可用 `POSTOFFICE_ALIAS_STABLE` 缩短）后，邮局确认一次切换——给 `notify` 里的信箱投一封现有广播（需 ack）、给接手方投一封 `需要：仅告知` 的交接提醒（只写 handoff 路径，邮局**不读**那个文件）、写一行 `logs/alias_switch.log`（事件编号、前后对象、原因、通知对象、交接目标），并给人一条系统通知。首次发现有目标只记基线、不广播；**首次发现就全员离线**同样要等满 60 秒才发一次「无人接任」通知，不会在刚启动时抢跑；稳定期内来回切换会撤销、什么也不发；重启保留基线与去重记录、不重复通知；候选全离线时只通知 `notify` 和人，不向任何候选投交接。广播与交接分步骤去重：交接失败后恢复只补交接，不重播广播。
- 同一次切换只有一个广播编号、每个收件人一封：中途失败后恢复只补没收到的那几家，即使状态没来得及落盘，邮局也会先翻收件箱确认那封信到底在不在（不重复投递、也不另建一份广播记录）。翻的范围包括收件箱、收件人处理过的 `done/` 和 `clear` 归档出来的 `archived/`——用户自己归档了旧广播，邮局也不会因此重投一遍。广播编号来自状态文件里单调递增的计数器而不是时钟，所以同一秒里确认的两个逻辑地址不会撞号，删掉再加回来也不会用回旧编号。从配置里删掉逻辑地址会立刻清掉它的基线，重新加回来从新基线开始、不会接回上次没确认完的切换。
- 导入会拒绝一切无法兑现的配置：引用未登记信箱、组与逻辑地址撞名、嵌套、重复成员/候选/通知对象、名字含路径空白通配符、JSON 重复键；被拒绝时旧文件字节不变。校验不只在导入时跑——每次用到都会重新完整校验，所以手改坏的配置（版本不对、成员写重了、通知对象指向已删除的信箱）会让分组和逻辑地址**一起停用**并说明原因，物理信箱照常收发、也不会顺手改任何信箱的状态；`postoffice config show` 照样把全部问题列出来给你看。
- 这些通知只报告收件路由变化：不授予谁额外权限、不自动开始任务，也不是人的新授权。

## 原理

| 收件方 | 谁来叫醒 | 怎么叫 |
|---|---|---|
| Claude Code | Claude Code 自己的钩子 | 每轮结束、会话打开时，钩子在后台起 `postoffice hook`，空等不调用模型；有信就以退出码 2 结束，Claude Code 把提醒交给会话并唤醒它。命令钩子默认 600 秒超时（实测 10 分钟后会被结束），所以安装时显式设 `timeout` 为 7 天 |
| OpenCode | 全局插件 | 每 10 秒和每次会话空闲时检查；会话空闲才用 OpenCode 自带接口 `session.promptAsync` 发一条提醒。多个 OpenCode 实例同时开着时靠认领文件保证只送一次；失败后的重试会重新认领，所以同一封信不会被两个实例各投一次 |
| Codex | 邮递员 | `codex queue --thread <id>` 往线程里排一条提醒 |
| 人 | 邮递员 | 系统通知 |

数据都在 `~/agent-postoffice/`（可用环境变量 `POSTOFFICE_HOME` 改）：`routes.json` 是唯一配置；每个信箱一个目录（`inbox/`、`done/`、`CONTACT.md`）；日志在 `logs/`。设 `POSTOFFICE_NO_NOTIFY=1` 可关掉系统通知（OpenCode 插件与邮递员改为只写一行日志）；OpenCode 插件在弹通知前总会先往 `logs/opencode_plugin.log` 写一行 `通知人：…`，邮递员在静默时写 `logs/notify.log`。

## 送达的含义

邮局区分三件事：

- **待投递**：还没发出去——信箱离线、会话正忙，或提醒还在排队。
- **已提醒**：钩子唤醒了 Claude 会话 / 插件给 OpenCode 发了提醒 / `codex queue` 返回成功。这只说明提醒发出去了。
- **已处理**：收件方把信挪进了自己的 `done/`。

已提醒却还躺在 `inbox/` 的信，面板上算“已提醒待归档”；插件的 `FAILED_FINAL` 单列为“投递失败”，因为它不会有人自己去处理。归档按钮的确认框会把这三类数量和总数都列出来，写明是把收件箱里的**全部**信件与通知一起存档、文件保留在 `archived/`、待投递的归档后不再补送。

已提醒但 30 分钟仍未处理，邮递员通知你一次——只针对**需要处理**的信：`需要：` 恰好是 `回复`/`审核`、或自由文本只含肯定词（回复/审核/处理/修改/决定/确认）才提醒；`需要：仅告知`、回执通知、广播汇总不提醒；正负混合或识别不了仍提醒（不宣称零误报）。已知情况：Codex 线程没有加载时，`codex queue` 也会返回成功，但线程不会自己恢复，这时就靠这条通知。

## 验证状态

| 项目 | 状态 |
|---|---|
| 收发信、去重、限流、在线/离线、ack 记账、回执查询、广播汇总、安装卸载、按需提醒、稳定认人、回执合并 | `tests/smoke.sh` 自动测试 |
| 配置导入校验、分组开关、逻辑地址解析与回退、切换广播（基线 / 稳定后通知 / 稳定期内撤销 / 无人 / 恢复 / 主事复归 / 交接失败后只补交接）、面板分组与按钮、按物理信编号回执、归档先报数量 | 同一次 `tests/smoke.sh`（共 244 项；v1.7 用例在独立临时邮局里跑；每条新规则都用变异体验证过这些断言会真的变红） |
| 审核退回的六处负例：运行时完整校验配置（版本非法 / 成员写重 / notify 指向已删除信箱时分组与逻辑地址一起停用、routes 与 inbox 无副作用）、首轮全员离线等满稳定期才通知一次、删除逻辑地址后基线真的落盘、一次切换只一份广播记录且送达名单丢失也不重播、空或非法「广播：」信头回执被拒且账本与原信不动、归档确认框列出投递失败与总数 | 同一次 `tests/smoke.sh` 的第 14 块（共 50 条断言，每个用例跑在各自的临时邮局里）；另用 8 个变异体验证，其中一个是把事件步骤整段换回退修前的实现 |
| 阻塞退修的两处：广播编号不撞（同秒确认两个逻辑地址 / 通知对象相同与不同 / 各自独立回执 / 非收件信箱不能代回）、真实硬中断后的恢复（信留在收件箱、已归档到 `done/`、已被 `clear` 归档三种位置各跑一遍） | 同一次 `tests/smoke.sh` 的第 14 块（新增 4b/4c 共 19 条断言）；硬中断用例在进程内加载真实 `postoffice` 模块，投信成功落地后抛一个 `BaseException`（普通 `except Exception` 抓不到），再从磁盘重新加载模块跑第二轮，并断言恢复分支确实走到了 |
| 分组开关与逻辑地址配合真实 provider 额度、以及交接路径文件 | 真实机器未验证：没有导入真实配置、也没有真的整组上下线（v1.7 尚未发布） |
| 回执提醒精简且只含元数据；`postoffice receipt` 精确 ID 只读，`postoffice archive-receipt` 只归档本箱对应通知 | `tests/receipt_test.py`（15 项，含旧通知文件、shell 引用与归档幂等）和 `tests/receipt_plugin_test.mjs` |
| Claude 桌面版：空闲几分钟后被外部来信叫醒并处理信件 | 真机多次观察到 |
| Claude 桌面版：不显式设 timeout 时，钩子 10 分钟后被结束 | 真机观察到（v1.0 的缺陷，v1.1 已显式设 7 天） |
| Claude 桌面版：设了长 timeout 后，空闲 30 分钟以上仍能被叫醒 | 真机实测：监视活过 33 分钟，空闲 33 分钟后来信 3 秒内被叫醒 |
| Claude 桌面版：在 App 里按停止或回退后，该会话的后台进程被关，监视随之消失 | 真机观察到（见“已知限制”） |
| OpenCode：空闲会话 10 秒内收到提醒 | 真机多次观察到 |
| Codex：`codex queue` 投递到已加载的线程 | 联调中实际使用；未与邮递员其他路径隔离单测 |

## 已知限制

这些情况下 Claude 会话暂时叫不醒；信不会丢，20 分钟后邮递员会通知你，你跟那个会话说一句话就恢复：

- **重开 Claude App 之后**：每个会话要先跑过一轮，收信监视才会挂上。
- **在 App 里对会话按了停止或回退**：App 会关掉这个会话的后台进程，监视跟着没了，要等下一条消息。
- **会话连续 7 天完全没动**：监视到期。会话每跑一轮，7 天就重新计时。
- **Codex 线程没加载**：`codex queue` 仍返回成功，但线程不会自己醒；靠“已提醒 30 分钟未处理”的通知兜底。
- **升级 agent-postoffice 之后**：只是就地更新脚本的话，Claude 不用重启——现有监视在下一轮 Stop/SessionStart 钩子时会加载新代码；重启 Claude App 反而会让各会话的监视消失，得先跑一轮恢复。已开着的 OpenCode 会一直用旧插件，需重启。只有钩子配置本身变了（首次安装，或增删钩子）才需要重载 Claude App。

## Future work

- **叫醒已停止的 Claude 会话**：会话后台进程不在时，钩子无能为力。可研究借 Claude App 自带的会话间消息，由一个常驻会话代为转达（每次转达要花一次模型调用）。
- **Linux**：邮递员的 systemd 用户服务安装。

## 安全须知

- 信只是提醒，不是授权：钩子和插件不改模型、权限、认证，不批准任何权限请求，不新建会话。
- 收到的信件内容等同于别的 AI 写的文字：各会话仍按自己的权限设置行事。别让来源不明的程序往信箱里写信。
- 信里不要放密钥和密码。

## 测试

```bash
./tests/smoke.sh                                              # 244 项，全在临时目录里跑
python3 tests/receipt_test.py                                 # 回执：元数据提醒（Claude 钩子与 Codex 队列模拟）、精确查询、不跨箱泄露、只读、旧通知文件、广播、--wake
node --experimental-strip-types tests/receipt_plugin_test.mjs # OpenCode 插件（模拟客户端，不调用模型）
node --experimental-strip-types tests/panel_i18n_test.mjs     # 面板页面：逐信统计、中英切换、用户原文显示
```

都不碰真实配置、信箱和账本，也不调用模型。
