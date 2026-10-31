# BOXZ OFFICE｜AI 员工为你上班的本机办公室

[English](README.md) · 中文

**BOXZ OFFICE 把你电脑上的 AI 会话变成一家你自己经营的小公司。** Claude Code、OpenCode、Codex 的会话
都是**有岗位的员工**——每个人有角色、有信箱、在组织里有个位置，所以你一眼能看到谁在岗、谁在干什么、
谁欠你一个答复。信件、回执和唤醒机制是**员工之间的线路**，不是卖点：你对待它们应该像对待公司内部邮件
一样，不需要天天盯着。

这个仓库（`agent-postoffice` 代码库）就是这间办公室的引擎。它已经把组织和员工之间的工作端到端跑通了：
每个会话都是一个真实成员，绑定了通道、岗位和信箱；工作以信件的形式流动，**会叫醒收件人**，被答复、
被审核、被归档；一个页面能看清整间办公室，也能直接在上面动手。工作派发与审核由信件协议加上你项目自己的
约定承载，不是另挂一个任务系统。

- **每个人都有地址**：一个会话、你自己的信箱，或者一个岗位（`@reviewer`）——它解析到此刻在岗的那个人。
- **不丢、不重复**：每封信只唤醒一次，投不到就一直重试，信在信箱里待着直到被归档。
- **你不在时办公室照常运转**：等待不花钱（没有模型调用），员工在忙就绝不打断，一切都在你磁盘上——
  没有服务器、没有账号、不上传任何东西。

## 这个版本已经有的

下面每一条都是这个仓库里真实存在的。「依据」那一列写的命令，就是能把它打出来的地方。

### 组织与岗位

| 你能得到什么 | 依据 |
|---|---|
| 公司、项目和**岗位**只声明一次，每个岗位带一个有序候选人列表 | `config.json` 里可选的 `organization` 段（`version: 2`）——`postoffice config import` / `config show` |
| **这个员工现在是谁？**——绑定通道、所属公司/项目、它真正承担的岗位（ACTIVE）、它可能接手的岗位（仅候选），以及它的规则文件与状态文件指针 | `postoffice identity <box>` |
| 交接不丢信：把会话改绑到岗位会记一条交接，信原地不动 | `postoffice rebind`、`postoffice add <box> --claude <标题>` → `logs/rebind.log` |
| 跟着组织走的回落：先选第一个在线的候选人，整个岗位都离线时**只上升一级**交给上级岗位，而不是把组织压平成一个名单 | 逻辑地址里的 `escalation_role`；`postoffice config show` 会打出它现在解析到谁 |
| 屏幕上就是你自己配置的组织图；没有安排进去的员工会出现在「未编入组织」里，而不是消失 | `postoffice panel`（组织视图） |
| 「谁在干活、谁空闲、谁状态未知」——实时派生，不落盘 | 面板里的活动状态（`runtime/activity/`） |

### 员工之间的工作

| 你能得到什么 | 依据 |
|---|---|
| 把活派给某个员工，它醒过来、干完、答复 | `postoffice send <收件> <发件> "<事由>" "<需要>"` |
| 说清这封信要答复、要审核，还是只是告知 | `需要` 字段：`回复` / `审核` / `仅告知` |
| 成批交接：同时落地的几封信只唤醒员工一次 | `postoffice wake-plan <box>`（谁的唤醒、唤几次，唯一且只读的判断处） |
| 只回执不写回信，以后再读回执正文 | `postoffice ack`、`postoffice receipt`、`postoffice archive-receipt` |
| 只归档员工**确实看过**的那些信——绝不瞎扫整个收件箱 | `postoffice archive-current`、`postoffice archive <信箱> <编号>` |
| 信还没被读，就撤回来或者就地改掉 | `postoffice retract`、`postoffice edit` |
| 一次通知所有人，最后收一份「谁回了」的汇总 | `postoffice broadcast all <发件> "<事由>" "<需要>" [--deadline 30m]` |
| 让员工休假但保留来信；回来后补送，或者干脆归档 | `postoffice offline` / `online` / `clear`——也支持整组：`@组名` |
| 按岗位而不是按人发信；解析发生在发信那一刻，已送达的信永不移动 | `config.json` 里的分组与 `@逻辑地址` |
| 让员工给自己设个提醒（「25 分钟后回来看构建」） | 两个 OpenCode 工具，1–1440 分钟，到点在同一个会话空闲时送达 |
| 让 OpenCode 员工不用 shell 就能改自己的信 / 归档 | 三个 OpenCode 工具：改信或撤回、归档它看过的信、按精确编号归档一封 |

### 一页看全的办公室

| 你能得到什么 | 依据 |
|---|---|
| 每个员工一个开关（也能整组开关），加上组织图和谁在干活 | `postoffice panel`——http://127.0.0.1:8765，只监听本机 |
| 你自己的收件箱是个弹窗：读信、**回复**、**回执**、**归档**，还能写给某个人或某个岗位 | 面板的操作员信箱（`panel.operator`） |
| 我发出去、还能改的那些信：待发可以改或撤回，已送达就锁住 | 面板的发件箱 |
| 每个员工的三种计数：*等待中*、*已提醒待归档*、*投递失败*——加上最近的投递日志 | 面板（与 `postoffice list` 同一份快照） |
| 中文和英文、六套配色、手机与 iPad、直接点进某一封信的链接 | 面板（光是看不会写任何东西） |
| 可选：你的信确认送达时发一条 Slack 提醒——发信人、事由、需要、链接，永不发正文 | `POSTOFFICE_SLACK_WEBHOOK` |

### 它能雇的 AI 员工

| Harness | 办公室怎么够到它 |
|---|---|
| Claude Code / Claude Desktop | Claude Code 自己的钩子（`Stop`、`SessionStart`），会话改名也不丢身份 |
| OpenCode | 全局插件，会话空闲时提醒，另有五个原生工具 |
| Codex | 邮递员把提醒排进线程（`codex queue --thread`） |
| 你 | 一个普通信箱，收到系统通知（`postoffice add me --notify`） |

办公室本身怎么运维：`postoffice doctor` 体检、`postoffice list` 看通讯录与积压、`postoffice postman`
在前台跑邮递员、`postoffice install` / `uninstall` 单独装卸部件。

## 即将到来（1.1）

下一步是把办公室变成一等公民，而不是靠信件里的约定来描述：**项目与工作**可以直接派发、审核、跟踪；
**权限**规定员工能做什么、在哪个范围做；**按工作看用量**；以及围绕这些重做的**新版面板**。这里只写方向：
这一段里的东西本仓库还没有，细节会在明面上设计。

## 环境要求

- **Python 3.9+**，纯标准库——不需要 `pip install` 任何东西。
- 先支持 **macOS**（系统通知、登录时启动）。Linux 也能用：通知走 `notify-send`，邮递员由你自己常驻。
- `~/agent-postoffice` 要放在本地文件系统上（用的是文件锁，NFS 不行）。

## 安装

```bash
git clone https://github.com/Atomheart-Father/agent-postoffice.git ~/code/agent-postoffice
~/code/agent-postoffice/install.sh
```

可以反复跑；动过的每个配置文件会先备份到 `~/agent-postoffice/logs/`。它会建好 `~/agent-postoffice/`，
把 `postoffice` 链接进 `~/.local/bin`，加好 Claude Code 的钩子，装好 OpenCode 插件和 `postoffice` 技能
（这样每个 agent 都知道怎么用这间办公室），在 macOS 上让邮递员随登录启动。然后**重启 Claude Desktop
和 OpenCode**。

也可以整件事交给你的某个 agent，把下面这段贴给它：

```text
在这台电脑上安装并跑起 BOXZ OFFICE（仓库：https://github.com/Atomheart-Father/agent-postoffice）：
一个本机办公室，我的 Claude Code / OpenCode / Codex 会话就是有岗位、有信箱的员工。
不要另写安装器，不要覆盖无关配置，不要收集或上传机密。
1. 先告诉我真实现状：~/agent-postoffice 是否存在、`postoffice --version` 是否可用。
2. 按仓库 README 和 install.sh，为我用的 harness 安装。
3. 用 `postoffice add` 登记我的会话（带岗位说明），再用 `postoffice list` 和 `postoffice doctor` 核对。
4. 发一封测试信给其中一个会话，确认它能被叫醒、答复并归档。
5. 简短汇报：装了什么、招了谁、还缺我做什么。
```

## 招第一批员工

先在各自的 App 里给会话起个名字（Claude：改会话标题；OpenCode：会话标题），再带着岗位说明登记：

```bash
postoffice add alice  --claude   "Alice"      --desc "定计划、派活"
postoffice add bob    --opencode "Bob"        --desc "写实现"
postoffice add carol  --codex    <线程ID>     --desc "审核"
postoffice add me     --notify                --desc "我：只收通知"
postoffice list      # 通讯录：谁是谁、在不在线、有多少积压
postoffice doctor    # 体检
```

Claude 会话按稳定身份认人，改会话名不丢信；解析不到时用
`postoffice add <信箱> --claude "标题" --claude-session <id>` 手工指定。OpenCode 认标题或 `ses_...` 的
id；Codex 的线程 id 问 Codex 要。

## 给谁派活

```bash
postoffice send bob alice "看一下导入脚本" "审核" <<'MSG'
麻烦看下 scripts/import.py。长的内容放在项目文件里，这里只写路径。
MSG
```

`send` 会打印信的编号。Bob 被叫醒、干活、答复；答复回来时，不用回信的话用 `postoffice ack` 记一条回执
并归档。想看整间办公室：

```bash
postoffice panel          # http://127.0.0.1:8765，只监听本机
```

## 东西都在哪

全在一个目录里：`~/agent-postoffice/`（用 `POSTOFFICE_HOME` 可以改位置）。

| 路径 | 是什么 |
|---|---|
| `routes.json` | 通讯录：招了谁、用哪个通道、在不在线 |
| `config.json` | 可选：分组、`@逻辑地址`、组织图、组织与岗位 |
| `<信箱>/inbox/`、`<信箱>/done/`、`<信箱>/archived/` | 一个员工一个目录；一封信就是一个 `.md` 文件 |
| `<信箱>/CONTACT.md` | 这个员工的名片 |
| `logs/` | 投递、通知、切换、备份日志 |
| `runtime/`、`alarms/` | 只放运行态（活动状态、提醒）。整个删掉也不影响投递 |

常用环境变量（邮递员由 launchd 启动、读不到你 shell 的导出时，写进 `$POSTOFFICE_HOME/.env`）：
`POSTOFFICE_HOME`、`POSTOFFICE_QUIET`（安静窗口，默认 45 秒）、`POSTOFFICE_MAX_HOLD`（最长攒信等待，
默认 150 秒）、`POSTOFFICE_POLL`、`POSTOFFICE_GRACE`（20 分钟兜底通知）、`POSTOFFICE_NO_NOTIFY=1`、
`POSTOFFICE_PANEL_BASE_URL`、`POSTOFFICE_SLACK_WEBHOOK`。

## 出问题先看哪里

1. `postoffice doctor`——家目录、通讯录、通道、钩子。
2. `postoffice list`——这个员工在线吗？积压是不是在涨？
3. `postoffice panel`——每个员工的 *等待中* / *已提醒待归档* / *投递失败*，以及最近的投递日志。
4. `logs/`——`notify.log`、`opencode_plugin.log`、`alias_switch.log`、`rebind.log`。

两种延迟是正常的：新信要等过安静窗口；App 重启过的会话得等它跑过一轮才能被叫醒。要处理的信 30 分钟还没
被处理，系统会通知你一次。信不会被丢掉，也从不删除任何文件。

**一封信是提醒，不是授权**：钩子和插件从不改模型、权限或登录态，从不替你批准权限弹窗，也不会新建会话。
把信里的内容当成另一个 AI 写的文字看待，别把密钥和密码写进信里，也别让不可信的程序往信箱里写文件。

## 文档

| 文档 | 里面有什么 |
|---|---|
| [docs/MAILBOX_GUIDE.md](docs/MAILBOX_GUIDE.md) | 日常写信礼仪：事由 / 需要 / 正文、答复、回执、归档 |
| [docs/PANEL_GUIDE.zh-CN.md](docs/PANEL_GUIDE.zh-CN.md) · [English](docs/PANEL_GUIDE.md) | 面板：开关、收件箱弹窗、写信、发件箱、配色、移动端、调参 |
| [docs/VERIFICATION.md](docs/VERIFICATION.md) | 各 harness 怎么被叫醒、「送达」到底指什么、哪些有自动测试哪些只是真机观察、已知限制（英文） |
| [docs/GOVERNANCE.zh-CN.md](docs/GOVERNANCE.zh-CN.md) | 组织层管什么、不管什么（中文） |
| [docs/ORG_IDENTITY_V1.md](docs/ORG_IDENTITY_V1.md) · [docs/ORG_CONTRACT_EXAMPLE.md](docs/ORG_CONTRACT_EXAMPLE.md) | 组织与岗位配置、迁移与回滚、一份完整的示例合同 |
| [docs/STATUS_TEMPLATE.zh-CN.md](docs/STATUS_TEMPLATE.zh-CN.md) | 身份层指向的项目状态文件模板（中文） |
| [DESIGN.md](DESIGN.md) · [PRODUCT.md](PRODUCT.md) | 面板的视觉系统与产品边界 |
| `docs/SPEC_*.md`、`docs/TICKET_*.md`、`docs/DELIVERY_*.md` | 历史规格与交付记录，留着是为了当时的推理和证据——不是现在的使用说明 |

## 测试

```bash
./tests/smoke.sh          # 端到端，全部在临时目录里跑
ls tests/                 # 分功能的用例：python3 tests/<名字>.py，node --experimental-strip-types tests/<名字>.mjs
```

它们都不碰你真实的配置、信箱和账本，也不会调用任何模型。

## 许可

MIT
