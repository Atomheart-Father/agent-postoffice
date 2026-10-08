> **Historical record** (written for the release it names; kept for the reasoning and evidence). It is not current usage documentation: for how things work now, read the README and docs/PANEL_GUIDE.md.

Q-TICKET-V1-13-HUMAN-CONSOLE-MATURITY-01

Postoffice v1.13
Runtime Presence + Operator Outbox + Appearance + Mobile/iPad Polish


BASE:
ce14581c185d5447bed65182ce65538f19cb1171


新建独立 branch。
完成后不要 merge / deploy。
交 Sol gate。


==================================================
0. 总原则
==================================================


这一票让 Human Console 更像长期使用的成熟产品。


四件事：


A. Runtime Presence
   WORKING / IDLE / UNKNOWN + duration


B. Operator Outbox
   老板可以查看自己当前仍可观察的已发普通信；
   未被收件方通道接受时，可编辑 / 撤回。


C. Appearance
   SYSTEM / LIGHT / DARK


D. iPhone / iPad portrait polish


除 Operator Outbox 复用现有 edit/retract core 外，
其余都是 presentation / runtime observation。


绝不改变 routing semantics。


==================================================
1. Operator Outbox：产品定义
==================================================


Human Operator 工作台增加：


收件箱 | 发件箱


英文：


INBOX | OUTBOX




这里的 OUTBOX 是：


Operational Outbox


不是永久 Sent History。




不要创建：


boss/sent/
sent.json
sent.db
第二份 message copy
第二份 delivery truth




普通信的唯一实体仍然是：


<recipient>/inbox/<id>.md




Outbox 只是从现有真实状态推导出来的 read model。




README 说清楚：


收件方把信归档后，
这封信可能不再出现在 Operational Outbox。


第一版不承诺永久发送历史。


==================================================
2. Outbox discovery
==================================================


operator 来自：


config.panel.operator


绝不硬编码 boss。




服务端只读扫描：


已登记 physical mailboxes 的 inbox/


找：


来源 == operator


并且是 ordinary direct mail。




排除：


回执
广播
闹钟
切换事件
postoffice system notices
其它现有 edit/retract 明确拒绝的 derived mail




不要从 UI 猜。


把“普通直发信判定”提取成共享 helper，
让：


CLI edit
CLI retract
Panel outbox


使用同一规则。


==================================================
3. Outbox row
==================================================


至少显示：


TO
subject
need
id/ref
age/time
delivery/mutation status




如果原来发给 @alias：


信里已有：


逻辑地址：@xxx


则显示：


@backend.supervisor
→ physical-target


但 mutation 的真实 ref 仍是：


<physical-recipient>/<id>




不要重新解析 alias。


旧信永远不 reroute。


==================================================
4. Outbox 状态
==================================================


建议三种 UI 语义：


PENDING
= 当前快照看起来仍可 mutation


DELIVERED / LOCKED
= 已被某投递通道接受或已经进入投递，
  不可 edit/retract


UNKNOWN
= 无法可靠判断，fail closed




PENDING：


[编辑] [撤回]




DELIVERED / LOCKED：


按钮不出现或 disabled，
明确：


已送达，不能修改




UNKNOWN：


明确：


状态无法确认，不能修改




不要把：


recipient mailbox online/offline


当作 mutation eligibility。


==================================================
5. Mutation eligibility：只能有一套
==================================================


当前生产代码已有：


retract_refused_reason()
refusal_for_methods()
mutation_refusal()
parse_message_ref()
rewrite_letter()


以及 cmd_edit / cmd_retract 的：


- sender validation
- ordinary-mail validation
- per-channel ledger check
- claim arbitration
- post-claim second check




不要复制这些逻辑到 Panel。




重构成很小的共享 core，例如语义上：


inspect_message_mutation(...)
edit_letter(...)
retract_letter(...)


CLI：


cmd_edit
cmd_retract


只做 argv / print adapter。




Panel：


调用同一个 core。




不要 subprocess 调自己的 CLI，
也不要写第二套 JavaScript eligibility engine。


==================================================
6. TOCTOU 必须 fail closed
==================================================


Outbox 列表：


只是 read-time snapshot。




例如：


09:00:00 UI 显示 PENDING


09:00:02 OpenCode 抢到 claim


09:00:03 老板点撤回




正确结果：


撤回失败：
信已经进入投递流程。




绝不能：


因为 UI 曾显示 PENDING，
就移动文件。




POST edit/retract 每次必须重新：


检查
→ claim
→ 再检查
→ mutation




完全复用当前竞争语义。


==================================================
7. Panel API
==================================================


建议：


GET /api/outbox


纯读。




只返回：


当前 operator 的 ordinary outgoing mail。


不要接受 sender 参数。




POST /api/edit-one


POST /api/retract-one




同样：


sender 永远来自：


config.panel.operator


不能让浏览器传：


from=boss
sender=xxx


然后后端相信它。




必要参数例如：


edit:
{
  "to": "...",
  "id": "...",
  "subject": "...",
  "need": "...",
  "body": "..."
}


retract:
{
  "to": "...",
  "id": "..."
}




后端自己绑定 operator。




继续使用现有：


Origin protection
loopback/Tailscale exact origin rules。


==================================================
8. Edit UI
==================================================


点击 Outbox PENDING 信：


打开详情。




显示：


TO
ID / REF
SUBJECT
NEED
BODY
STATUS




点击编辑：


可改：


subject
need
body




不可改：


sender
recipient
id
ref
alias target




保存成功：


编号不变
文件名不变
来源不变
收件人不变




UI 刷新后显示新版内容。




不要做 autosave。
不要做 drafts。


==================================================
9. Retract UI
==================================================


PENDING 信提供：


撤回




需要一个明确但简短的 confirmation。




成功：


信按现有 retract 规则
原样进入 recipient archived/


不会通知对方
不会补发 correction




然后从 Outbox 消失。




失败：


显示现有 core 的真实原因。




特别是：


已投递 / claim 被抢


必须告诉老板：


已经不能撤回，请另发更正。


==================================================
10. Outbox read purity
==================================================


GET /api/outbox


绝不能：


claim
touch ledger
写 presented
移动信
改 mtime
改 routes
改 config




连续读取：


所有真实状态文件 byte-identical。


==================================================
11. Outbox tests
==================================================


至少：


operator → physical ordinary mail
未送达
→ PENDING
→ edit/retract available




operator → @alias
→ 显示 alias + frozen physical recipient
→ 不重新 resolve




其它 sender 的信
→ 不进入 operator outbox




broadcast / receipt / alarm / switch
→ 不进入可编辑 ordinary list




OpenCode DELIVERED
→ 不可 edit/retract




Claude .seen
→ 不可 edit/retract




Codex/notify .woken
→ 不可 edit/retract




claim already held
→ LOCKED




unknown method
→ UNKNOWN / fail closed




race:
GET 显示 PENDING
随后 delivery 抢 claim
POST retract/edit
→ mutation 必须失败




edit：
subject/need/body 改
id/sender/to 不变




retract：
原信进入 archived
无通知
无第二封信




sender spoof POST
→ 无法修改别人的信




GET purity
→ byte-identical。




Mutation test：


绕过 post-claim second check
→ race test RED。


==================================================
12. Inbox / Outbox UI
==================================================


Operator Inspector 顶部：


收件箱 02
发件箱 01




不要增加第三个主导航页。




切换只是 Operator workspace 内部 tab。




手机：


两个 tab >=44px
不横向溢出。




Outbox 空状态：


暂无当前可观察的已发普通信


不要写：


“从未发送过信”


因为我们没有永久历史。


==================================================
13. Runtime Presence
==================================================


保持之前裁决：


ONLINE/OFFLINE
≠
WORKING/IDLE/UNKNOWN




只三个状态：


WORKING
IDLE
UNKNOWN




显示 duration：


WORKING · 17m
IDLE · 42m




不参与 routing。




不要：


CPU polling
ps guess
queue=working
自动 reroute
自动 offline
productivity metrics。


==================================================
14. Activity storage
==================================================


runtime-only：


$POSTOFFICE_HOME/runtime/activity/<box>.json




每 mailbox 一份。




例如：


{
  "state": "working",
  "since": 1791352301.2,
  "observed": 1791352301.2,
  "source": "opencode_plugin",
  "binding": "..."
}




丢掉整个 activity dir：


邮局全部核心行为仍正常。




binding 与 routes 当前 session 不匹配：


UNKNOWN。




重复同状态：


不重置 since。


状态切换：


重置 since。


==================================================
15. Activity capability probe
==================================================


实现前先验证真实本机版本。




OpenCode：


现有 plugin 已经监听：


session.idle


优先复用 plugin event lifecycle。


不要新 plugin / daemon。




Claude：


调查实际 hooks：
UserPromptSubmit / Stop / SessionEnd 等。




Codex：


调查本机真实 hooks / lifecycle。




只有可靠信号：


才 WORKING / IDLE。




否则：


UNKNOWN。




不要为了三个 harness
强行做到完全一致。


==================================================
16. Activity UI
==================================================


Organization node：


● ONLINE · OpenCode
◉ WORKING · 17m




Harness：


MAILBOX
MAIL
ACTIVITY
FOR




Inspector：


MAIL
ONLINE


ACTIVITY
WORKING


SINCE
17m




Human boss：


不要显示假的：


IDLE · 4h




可显示：


HUMAN


或不显示 AI activity。


==================================================
17. Appearance
==================================================


三态：


SYSTEM
LIGHT
DARK




默认：


SYSTEM




browser-local：


localStorage




绝不写：


config
routes
server state。




SYSTEM：


prefers-color-scheme


系统变化时实时跟随。




手动 LIGHT/DARK：


覆盖系统。


==================================================
18. Dark style
==================================================


继续：


BOXZ Precision Editorial




不是纯黑白反转。




Dark：


warm charcoal / gunmetal
warm off-white
muted metal rules
signal orange
dim green / amber / red




不要：


gradient
glow
glass
neon
cyberpunk。




现有 CSS variables 继续 token override。


审计硬编码色。


==================================================
19. Theme first paint
==================================================


避免手动 DARK 时白闪。




允许 head 里极小 bootstrap：


localStorage
→ data-theme




正确设置：


color-scheme


以及合理：


theme-color




localStorage 出错：


SYSTEM fallback。


==================================================
20. Appearance control
==================================================


不要 Settings 页面。




现有顶栏中增加小型：


外观 / Appearance




SYSTEM
LIGHT
DARK




不要只放一个含义不明的月亮 icon。




touch >=44px
keyboard/focus/aria 完整。


==================================================
21. Mobile / iPad portrait
==================================================


保留所有 v1.12.x 修复。




重点：


390
430
768
820




检查：


top rail
Organization
Harness
Operator Inbox/Outbox
workbench
letter
outbox edit
compose
recipient dropdown
Activity drawer
Appearance




iOS：


safe-area
100dvh
keyboard
form font-size >=16px
no global horizontal overflow。


==================================================
22. Freeze v1.12.x behavior
==================================================


这些全部是 regression：


- mobile first load 不开 workbench
- workbench 可关闭
- HARNESS 点 OPERATOR 不切回 Organization
- recipient dropdown 浮层
- focus/click 都能打开
- outside click 关闭
- 全量 mailbox+alias，不截 8 条
- dropdown 自己滚动
- workbench 写信预填当前 mailbox
- top-level +写信 保持空
- reply 不受影响


任何一条回归都 STOP。


==================================================
23. 不做
==================================================


本票不要：


永久 Sent History
sender-side message copy
database
threads
drafts
search
read receipts redesign
delivery redesign
websocket
analytics
charts
employee ranking
activity-driven routing
new framework
Settings page。


==================================================
24. Tests / regression
==================================================


新增：


outbox core/API/UI
activity
appearance
mobile




然后全跑现有：


smoke
message_flow
hierarchy
retract
receipt
hook_rate


alarm
alarm_plugin
alarm_install


message_flow_plugin
receipt_plugin


panel_letter
panel_send_ack
panel_presentation
panel_origin
panel_slack
env_file


panel_console_ui
panel_letter_ui
panel_i18n




py_compile
node --check
git diff --check。




==================================================
25. Review
==================================================


独立 code-review：


重点看：


1. Outbox 是否制造第二份 truth
2. Panel edit/retract 是否真正共享现有 core
3. TOCTOU 是否仍靠 claim + second check
4. sender 是否只能是 operator
5. activity 是否完全不参与 routing
6. dark/mobile 是否只影响 presentation




然后：


Impeccable 一轮
Pro Max a11y/mobile 一轮。




不要无限 polish。




==================================================
26. 交付
==================================================


不要 merge。


回报：


- branch
- BASE SHA
- HEAD
- changed files


OUTBOX：
- discovery method
- shared mutation core
- race proof
- API
- UI
- tests


ACTIVITY：
- OpenCode source
- Claude source
- Codex source
- UNKNOWN boundaries
- runtime schema


APPEARANCE：
- SYSTEM/LIGHT/DARK
- first-paint strategy


MOBILE：
- 390/430/768/820 audit


REGRESSION：
- full results


REVIEW：
- independent findings
- Impeccable
- Pro Max
- known boundaries


然后停下交 Sol gate。

==================================================
27. BUGFIX：投递 prompt 不得错误降级 Human Operator
==================================================


真机发现当前 OpenCode wake prompt 会生成：


来源：boss（协作者，不是人的新指令；人的原话要注明“转述”）


这是错误语义。


BOXZ 中：


boss


是真实 Human Operator。


老板通过 Panel 亲自发送的普通信，
不能被 Postoffice delivery layer 强行描述成：


“协作者”
“不是人的新指令”


否则会导致收件 AI 将老板的直接指令错误降级。




但修复时禁止：


if sender == "boss"


禁止硬编码：


boss
owner
human


也禁止让：


config.panel.operator


变成 routing / authorization / trust engine。




原因：


Postoffice transport layer 的职责只是：


- 证明来源 mailbox
- 投递信件
- 保留 provenance


它不应该自己裁决：


- 此来源是不是人类
- 此来源是不是上级
- 此来源有没有命令权限


这些属于项目本地组织/角色约定。




==================================================
28. 正确 wake prompt 语义
==================================================


把现有类似：


来源：<sender>（协作者，不是人的新指令；人的原话要注明“转述”）


改成中性的 provenance 表达。


推荐语义：


来源：<sender>
（邮局只确认来源信箱；该来源的身份与权限按当前项目的组织/角色约定处理。）


或者更短：


来源：<sender>
（身份与权限按项目约定处理；邮局不自行提升或降低来源权限。）




不要出现：


“这是人类指令”
“这不是人类指令”
“这是协作者”
“这是上级”


transport 不知道这些事实。




收件 AI 已经能从 BOXZ project-local instructions 知道：


boss = 老板 / Human Operator


记者 / Q / A / B / 接口管理员等
各自是什么组织身份。




因此：


boss → AI


会按本地规则识别为老板来信。




AI → AI


也不会因为邮局而被自动提升成人类权限。




==================================================
29. 转述语义
==================================================


“人的原话要注明转述”这条如果仍然需要，


放在：


project-local agent etiquette / skill guidance


而不是每一封 wake prompt 对 sender 身份做断言。




例如规则可以继续是：


如果一个 AI mailbox 转述老板原话，
发件 AI 应在正文中明确标注这是转述。




但 Postoffice transport 本身：


不能根据 mailbox 名猜它是不是转述。




==================================================
30. Audit 所有投递通道
==================================================


不要只修 OpenCode。


搜索所有实际 delivery / wake prompt：


- OpenCode plugin
- Claude hook
- Codex queue
- postman notify/wake text
- shared skill 中与这一语义相关的模板


确认不存在同类：


“sender = collaborator”
“not human instruction”
“human instruction”


这种 transport 层自行判断身份的文案。




各通道应统一保持：


source provenance yes
authority inference no




==================================================
31. Provenance regression tests
==================================================


至少覆盖：


A. sender = boss


生成 wake text：


必须包含：


来源：boss


不得包含：


不是人的新指令
协作者（作为身份断言）
非人类指令




B. sender = ordinary AI mailbox


例如：


opencode_q


必须包含：


来源：opencode_q


但同样不得自动声称：


“这是人类指令”
“这是老板指令”




也就是说两边都保持 neutral。




C. 信件正文 byte-for-byte 不因本修复改变。




D. sender metadata 不改变。




E. routing / alias / delivery / receipt
全部不受影响。




F. OpenCode / Claude / Codex
若各自有 wake-text fixture，
全部钉住同一 provenance 原则。




==================================================
32. 架构裁决
==================================================


这一修复不是：


“给 boss 加特权代码”。


而是：


“删除 transport layer 对 sender authority 的错误推断”。




最终职责边界：


Postoffice：
谁发来的，我可以证明。


Project role instructions：
这个人是谁、我应该多大程度听他的。




不要把两层重新混起来。