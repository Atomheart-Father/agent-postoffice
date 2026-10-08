> **Historical record** (written for the release it names; kept for the reasoning and evidence). It is not current usage documentation: for how things work now, read the README and docs/PANEL_GUIDE.md.

# 交付报告 · Q-TICKET-V1-13-HUMAN-CONSOLE-MATURITY-01

- 分支：`human-console-maturity`
- 基线 SHA：`ce14581c185d5447bed65182ce65538f19cb1171`（v1.12.4）
- HEAD：`ce14581c185d5447bed65182ce65538f19cb1171` —— 全部改动**未提交**，在工作树里
- 工作树：`/Users/bozhongxiao/code/po-hc-maturity`
- 范围：A Runtime Presence、B Operator Outbox、C Appearance、D Mobile/iPad、§27-32 来源口径
- 未 merge、未 deploy（线上仍是 v1.12.4）

## 改动文件

修改（11）：
- `postoffice`（主程序）
- `opencode/postoffice.ts`（插件）
- `panel/index.html`
- `skill/postoffice/SKILL.md`
- `README.md`、`README.zh-CN.md`
- `tests/panel_console_ui_test.mjs`、`tests/panel_i18n_test.mjs`、`tests/panel_letter_ui_test.mjs`
- `tests/message_flow_test.py`、`tests/message_flow_plugin_test.mjs`（§27-32 旧信头负例）

新增（8，RED 测试 + 证据脚本）：
- `tests/outbox_test.py`(573) `tests/activity_test.py`(436) `tests/provenance_test.py`(209)
- `tests/activity_plugin_test.mjs`(160) `tests/panel_mobile_test.mjs`(148)
- `tests/mutation_second_check_test.py`（§11 claim 后二次复检 RED 能力 + 变异证据）
- `tests/activity_race_test.py`（§13-15 读改写竞态 / 旧绑定心跳负例）
- `tests/layout_probe.mjs`（§21 真机浏览器布局探测，opt-in）

交付文档：`TICKET_v1.13_human_console_maturity.md`、`PROBE_EVIDENCE_v1.13.md`（§15/§21/§11 真机证据）。

Diffstat（已跟踪文件）：11 files changed, 1382 insertions(+), 138 deletions(-)。

## B — Operator Outbox（发件箱）

- `GET /api/outbox`：纯读。读模型 `outbox_rows(operator)` 在请求时从真实状态推导 operator 仍可观察的已发**普通直发信**——不落第二份副本、不写台账、不认领、不唤醒、不重解析别名（`逻辑地址：` 原样展示）。
  - 响应 `{ok:true, outbox:[{to,id,ref,subject,need,body,alias,mtime,status,reason}]}`，`ref="<to>/<id>"`。
  - `status ∈ pending|locked|unknown`；非 pending 的 `reason` 为中文。
  - 发现规则：扫每个已登记信箱的 `inbox/`，只取 `来源 == operator` 的普通直发信；排除回执/广播/闹钟/切换事件头与来源 `postoffice`。无 operator → `{ok:true,outbox:[]}`。
- `POST /api/edit-one {to,id,subject?,need?,body?}` → 200 `{ok:true,state:"updated",id,to}`；400 bad_request（结构/编号非法/无字段）；404 not_found（未登记信箱或信不存在）；400 `{ok:false,error:"refused",message:<CLI 原句>}`；500 failed。
- `POST /api/retract-one {to,id}` → 200 `{ok:true,state:"retracted",id,to}`；错误类同上；原信 → `<to>/archived/<ts>/<id>.md`，不写通知、不产生第二封。
- 与 CLI `edit`/`retract` 共用同一核心：`mutation_target` + `apply_mutation`（认领→复检→变更），所以列表快照说 pending 也可能在竞争里失败（fail closed）。
- 发件人恒为 `config.panel.operator`；浏览器传 `from`/`sender` 一概忽略。
- 守卫与既有 POST 端点一致：错误 Content-Type / 恶意 Origin / 未知路径 → 403；JSON 非法 → 400。

## A — Runtime Presence（运行状态）

- 数据：`$POSTOFFICE_HOME/runtime/activity/<box>.json` = `{state, since, observed, source, binding}`。
- `/api/state` 每箱新增 `activity:{state:"working"|"idle"|"unknown", since:<float|null>}`。
- 判 UNKNOWN：无文件；state 不是 working/idle；`binding` 与 `route_binding(route)`（session_id→claude_session→thread_id）不符；`observed` 早于 `ACTIVITY_STALE=1800s`。
- 同 state 保留 `since`，state 变化重置 `since`。
- **不参与路由**：删掉整个 `runtime/` 目录，邮局核心行为一字不变。
- 写入方：Claude 钩子（`UserPromptSubmit → activity --state working`；watcher 上岗/心跳写 idle；唤醒前写 working）与 OpenCode 插件（busy→working / idle→idle，60s 节流，tmp+rename，fail-soft）。

## C — Appearance（外观）

- 系统 / 浅色 / 深色三选，`<head>` 引导脚本在 app 脚本前设 `data-theme`（无闪烁；引导脚本不含 `const STR`）。
- `localStorage` 键 `postoffice.theme`；CSS 有 `color-scheme`；`#theme` 为 `role=group` + `[data-theme-choice=system|light|dark]` + aria-pressed + aria-label。

## D — Mobile / iPad

- `100dvh`、`env(safe-area-inset*)`、输入 16px 字号、`@media (max-width:900px)` 下 inspector `width:100vw`、`.modal .card` 有 max-width。

## 退修（Codex 复审 5 处 + 原票校准；含复验第 2 轮的收紧）

1. **活监视不得把 working 降级成 idle**：`cmd_hook` 心跳改为 `activity_touch`（保 state/since，只刷新 observed）；上岗仍无条件写 idle（Stop/SessionStart 触发）。负例：`tests/activity_test.py::test_live_watcher_heartbeat_never_overwrites_working`（真跑 `postoffice hook`，写 working，2.5s 后仍 working）。
2. **读改写互斥 + 绑定感知**：`activity_write`/`activity_touch` 全程持 `fcntl.flock`（os.replace 只让「写」原子，交错窗口会让旧心跳快照盖掉新 working）；`since`/节流续接要求 `同 state 且同 binding`；**旧绑定的心跳一律不写**（无记录/绑定不符/状态异常 → 直接返回，不再回落 `activity_write(idle)`，也不许把新绑定记录当空记录初始化）；`cmd_hook` 在岗时把 binding 钉死，上岗写 idle 也要求与当前路由一致。`opencode/postoffice.ts` 的 `writeActivity` 同样要求同 binding。
3. **发件箱行信息补全 + PENDING 只读详情**：行显示冻结的 `alias`、`id/ref`、`need`、`age`、状态与**具体**拒绝 `reason`（在途不再误标「已送达」）；点 PENDING 行进入只读详情（TO/ID/REF/ALIAS/SUBJECT/NEED/STATUS + 正文，字段全 `esc()`），别名不重解析。
4. **openEdit 作废在途 openLetter**：共用 `letterSeq` 请求失效；用例 32（deferred 的 openLetter 不覆盖编辑框）。
5. **SYSTEM 跟随系统主题变化**：`matchMedia('(prefers-color-scheme: dark)')` 变化时同步 `data-theme`，显式 LIGHT/DARK 免疫；用例 33。
6. **旧信头不得被 wake 再注入**（§27-32 全通道）：单封唤醒摘要不再回抄原始信头行，改为按解析出的 source/subject/need 重建中性元数据（`postoffice` 的 `wake_head()`、`opencode/postoffice.ts` 的 `wakeHead()`）；历史信不改写/不迁移，正文与来源信箱逐字节保留，无 boss 特例。负例：`provenance_test.py` 新增 3 例（旧格式 boss / 普通 AI 信箱 / 多封，均断言中性且文件字节不变）、`message_flow_test.py` 单封形态断言改为中性、`message_flow_plugin_test.mjs` 对单封/多封唤醒加 `assertNeutral`。

### 复验第 2 轮（Codex 复审 2 的 4 处，已逐条修）

7. **心跳 vs 真实 working 的写交错**：加互斥锁后，心跳与另一进程的 `postoffice activity --state working` 交错时 working 不会被旧快照覆盖。负例：`tests/activity_race_test.py::test_touch_never_clobbers_a_concurrent_working_write`（60 轮真线程交错）。变异复核：去掉锁/回落逻辑时该用例会 RED（见 `PROBE_EVIDENCE_v1.13.md` §11 同款白盒）。
8. **旧 watcher 换绑定后不得污染**：见上第 2 条；真 watcher 跨绑定负例 `tests/activity_test.py::test_live_watcher_rebind_never_publishes_the_old_identity`（s1 watcher 存活 → routes 改绑 s2 → s2 写 working → 2.5s 旧心跳后仍 working/s2，面板不 UNKNOWN）。
9. **从详情撤回后详情与列表一致**：详情层撤回成功后关闭详情并明确反馈「已撤回，原信已存档」（`retractDone`）；失败保留详情与真实原因。用例：`panel_console_ui_test.mjs` 34/35。
10. **证据补齐**：`layout_probe.mjs` 重写为覆盖 §21 全部点名表面（top rail/Organization/Harness/workbench/Inbox/Outbox/letter/outbox-edit/compose/recipient-dropdown/Activity-drawer/Appearance），必需表面打不开即 FAIL，不再吞 skip；四档原始输出见 `PROBE_EVIDENCE_v1.13.md`。§15 补真实 harness 版本（Claude 2.1.276 / OpenCode 1.18.35 / Codex ChatGPT 154.0.8037.98）并区分「已证」与「未证（本票不部署）」。§11 附删除第二次检查后的 RED 原始输出。
11. **wake 分支的 binding 保护（复审 3，postoffice:2977）**：直接把「旧绑定发布」堵住——Claude 侧三类观察点里，**watcher 上岗**与 **wake 退出 2 分支**经 `activity_publish`（只有 `binding == 当前路由 binding` 才写，旧绑定一律不写）；**心跳**经 `activity_touch`（绑定不符时本就不写，见第 2 条）；单次 **`activity` CLI** 则先按当前 `routes` 解析身份，天然是当前绑定。真机负例 `activity_test.py::test_live_watcher_rebind_wake_branch_never_publishes_old_binding`；白盒变异（去掉 wake 保护）→ `binding` 写回 s1、面板 UNKNOWN 的 RED 原始输出见证据文档。

校准：
- §2 统一普通直发信判定：新增共享 `DERIVED_HEADERS`/`derived_headers()`/`is_ordinary_direct()`，`mutation_target` 与 `outbox_rows` 共用同一套。
- §11 claim 后二次复检 RED 证据：`tests/mutation_second_check_test.py`（monkeypatch `claim_letter` 抢到认领后写 DELIVERED，断言 `apply_mutation` 拒绝且原信不动）；另附变异（删第二次检查）的 RED 原始输出。
- §15/§21 真机证据见 `PROBE_EVIDENCE_v1.13.md`（生命周期能力探测 + 真实 harness 版本 + 390/430/768/820 真浏览器全表面横向溢出，含修复）。

## §27-32 来源口径

- `write_letter` 信头改为：`来源：{sender}（邮局只确认来源信箱；该来源的身份与权限按当前项目的组织/角色约定处理。）`。
- 唤醒文案含 `来源：<sender>`，且不含 不是人的新指令 / 协作者 / 非人类指令 / 这是人类指令 / 这是老板指令。
- 正文逐字节不变。`skill/postoffice/SKILL.md` 第 118 行改为中性表述。

## 回归

- `tests/smoke.sh` 252/0（复验第 2 轮后再跑一次）。
- Python：outbox 15、activity 12、activity_race 3、provenance 9、retract 35、message_flow 25、panel_send_ack 13、panel_letter 11、panel_presentation 11、panel_origin 7、panel_slack 10、receipt 15、env_file 12、hierarchy 9、alarm 46、mutation_second_check 2 —— 全 OK。
- Node：panel_console_ui（含新增 30/31/32/33/34/35）、panel_i18n、panel_mobile、panel_letter_ui、activity_plugin（6）、message_flow_plugin（13）、receipt_plugin、alarm_plugin（47）、alarm_install（21）—— 全 PASS。
- §21 真浏览器：390/430/768/820 四宽度、§21 点名的 11 个表面逐个打开，`horizontal-overflow=0 surface-missing=0`（`tests/layout_probe.mjs`）。
- `git diff --check` 干净；`py_compile`、TS `--check` 通过。
- 既有 flake：`tests/hook_rate_test.py::test_a_live_race_lets_only_one_side_win_the_claim`（“钩子一次都没赢过”）—— 已用 `git stash` 在基线 v1.12.4 复现同样失败，**与 v1.13 无关**。

## 评审

- 独立 code-review + Impeccable + Pro Max 已过。
- 修正了三处自相矛盾的 RED 测试（均为子代理所写，已核实）：
  1. `tests/outbox_test.py`：400 `bad` 列表删去 `{"to":"ghost"}`（ghost 与 nosuch 同为未登记信箱，契约为 404，与 archive-one/ack-one 一致）；撤回用例原先比较移动前后完整相对路径（不可能满足），改为比较 `.name` 基名。
  2. `tests/panel_console_ui_test.mjs` 第 26 例 en 块：断言 WORKING 前补 `nodeClick("lead")(en)`（默认 inspector 是 operator，否则 fresh en 显示 HUMAN）。
  3. `tests/panel_letter_ui_test.mjs`：仍取第一个 `<script>`（现为 head 引导），改用其它 UI 测试的 `scriptBlocks.find(s=>s.includes("const STR"))`。

## 边界 / 说明

- 发件箱是**运行态**而非永久历史：只反映当前仍可观察、且通道尚未接受的已发普通信。
- 运行状态仅用于展示，不参与任何路由或投递判定。
- 未提交、未 merge、未 deploy；线上 v1.12.4 未动。
