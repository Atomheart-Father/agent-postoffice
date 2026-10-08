> **Historical record** (written for the release it names; kept for the reasoning and evidence). It is not current usage documentation: for how things work now, read the README and docs/PANEL_GUIDE.md.

# v1.13 能力探测与真机布局证据（§15 / §21）

本文件记录退修校准要求的**真机证据**，不是 DOM stub，也不是 CSS 字符串断言。

## §15 Activity capability probe（真机生命周期探测）

### 真实 harness 版本（本机实测，非凭配置推断）

| harness | 真实版本 | 取版本方式 |
|---|---|---|
| Claude Code | `2.1.276` | `claude --version`（`/Users/bozhongxiao/.local/bin/claude`） |
| OpenCode | 桌面 `1.18.35`；plugin host `@opencode-ai/plugin@1.18.31` | 进程 `--version=1.18.35`；`~/.config/opencode/package.json` |
| Codex | ChatGPT.app build `154.0.8037.98`（Codex Mac） | 进程 `Codex Framework ... --version`；`~/.codex/config.toml` |
| postoffice | 线上 `1.12.4`（未改动）；本票工作树 `1.13.0` | `postoffice --version` |

### 生命周期**支持依据**（不是「读了配置就算证」）

- **Claude Code 2.1.276**：Claude Code 的 hook 事件 `UserPromptSubmit`（用户提交提示词时触发）、`Stop`（一轮结束）、`SessionStart`（`startup|resume|clear|compact`）是该版本公开支持的 hook 事件；本机 `~/.claude/settings.json` 已实装 `Stop`/`SessionStart`。v1.13 `install-claude` 追加 `UserPromptSubmit → exec "<SELF>" activity --state working`。
- **OpenCode 1.18.35**：其 plugin event 里有 `session.status`（`busy`/`idle`）与 `session.idle`，现有 plugin（`opencode/postoffice.ts`）已在用这些事件，v1.13 仅在原 handler 内追加一次 `writeActivity`。
- **Codex（ChatGPT.app 154.0.8037.98）**：本机 `~/.codex/config.toml` 只有 `notify = [".../SkyComputerUseClient","turn-ended"]`，没有面向邮局的 busy/idle 生命周期事件；`turn-ended` 不是「正在干活/空闲」的可靠信号 → Codex 信箱一律 `UNKNOWN`（票 §15 明说，不强行拉齐）。

### 已证 vs 未证（诚实边界）

- **已证**（自动化，随本次交付）：
  - v1.13 CLI 写入器：`postoffice activity --state working|idle` 在解析到身份时写 `runtime/activity/<box>.json`，不可解析时 fail-soft（`tests/activity_test.py`）。
  - 同 state+同 binding 保 `since`、改绑即新观测、心跳不降级 WORKING、旧绑定心跳不污染新绑定（`tests/activity_test.py` + `tests/activity_race_test.py`，含真 watcher 跨绑定负例）。
  - OpenCode plugin 写入器（`tests/activity_plugin_test.mjs`，含换绑定负例）。
  - 面板读模型 `/api/state.activity` 的 unknown 规则（`tests/activity_test.py`）。
- **未证**（本票不部署，故无法端到端验证，如实声明）：**没有**在本机用真实 Claude/OpenCode/Codex 会话跑通「v1.13 钩子/插件真的被宿主触发并写出 working/idle」的端到端路径——线上仍是 v1.12.4（没有 activity 写入器、没有 UserPromptSubmit hook）。上面「真机现场演示」只是用真实 v1.13 CLI 手工喂钩子负载，证明**写入器**行为，不等同于证明宿主会触发它。

真机现场演示（临时 `POSTOFFICE_HOME`，未触碰真实部署）：

```
$ postoffice add cbox --claude "Demo" --claude-session S1
$ echo '{"session_id":"S1","hook_event_name":"UserPromptSubmit"}' | postoffice activity --state working
$ cat $POSTOFFICE_HOME/runtime/activity/cbox.json
{"state": "working", "since": 1791395853.552635, "observed": 1791395853.552635, "source": "claude_hook", "binding": "S1"}
$ echo '{"session_id":"S1"}' | postoffice activity --state idle
$ cat $POSTOFFICE_HOME/runtime/activity/cbox.json
{"state": "idle", "since": 1791395854.6417978, "observed": 1791395854.6417978, "source": "claude_hook", "binding": "S1"}
```

## §21 Mobile / iPad portrait（真机浏览器布局证据）

`tests/layout_probe.mjs`（opt-in，Playwright + Chromium，**不是** DOM stub）在 390 / 430 / 768 / 820 四个宽度，逐一打开票 §21 点名的**全部表面**：top rail、Organization、Harness、workbench + Operator Inbox、Operator Outbox、letter、outbox edit、compose、recipient dropdown、Activity drawer、Appearance。每个必需表面都要 `isVisible` 才算过——打不开记 `surface-missing` 并 FAIL，绝不计成 skip。量 `documentElement.scrollWidth - clientWidth`。

探测**发现了 DOM stub 漏掉的真横向溢出**并已修复：
- **768 / 820**：顶栏 `header.rail` 在 701–900 之间不让换行，`#op-shortcut` 等 aux 控件被顶出视口。修复：`@media (max-width:900px)` 给 `.rail`/`.aux` 加 `flex-wrap:wrap`。
- **390**：编外信箱行 `.t1row`（`[标签][t1node][连接线]` 同排）在窄屏顶出 2px。修复：`@media (max-width:700px)` 给 `.t1row` 加 `flex-wrap:wrap`、`.t1row .t1node{min-width:0;max-width:100%}`。

最终原始输出（`NODE_PATH=/tmp/pw/node_modules node tests/layout_probe.mjs`，面板 fixture：operator=boss，含 1 封 boss 外发信 + 1 封 boss 待办信）：

```
ok    390px  top-rail+Organization   docOver=0 bodyOver=0
ok    390px  Harness                 docOver=0 bodyOver=0
ok    390px  workbench+Inbox         docOver=0 bodyOver=0
ok    390px  OperatorOutbox          docOver=0 bodyOver=0
ok    390px  letter                  docOver=0 bodyOver=0
ok    390px  outbox-edit             docOver=0 bodyOver=0
ok    390px  compose                 docOver=0 bodyOver=0
ok    390px  recipient-dropdown      docOver=0 bodyOver=0
ok    390px  Activity-drawer         docOver=0 bodyOver=0
ok    390px  Appearance              docOver=0 bodyOver=0
（430 / 768 / 820 同：11 个表面 docOver=0 bodyOver=0）
PASS horizontal-overflow=0 surface-missing=0 at 390/430/768/820
```

**诚实说明**：Chromium 的视口宽度模拟 **不等于** 真实 iOS 键盘弹出/软键盘 viewport 变化；本证据覆盖宽度与出现性，不覆盖真机软键盘。iOS 侧另有源码级保证（`env(safe-area-inset*)`、`100dvh`、表单 `font-size:16px`）与 `tests/panel_mobile_test.mjs`。

复现：起临时面板后 `PANEL_URL=http://127.0.0.1:<port>/ NODE_PATH=/tmp/pw/node_modules node tests/layout_probe.mjs`。

## §11 认领后第二次检查：变异验收（不是「RED-capable」口头声明）

`apply_mutation` 的顺序是「第一遍判定 → claim_letter → 第二遍判定(skip_claim) → 变更」。
把第二遍那行改成 `why = ""`（删掉认领后的复检），同一白盒注入（claim 成功窗口里投递方写下
`DELIVERED` 台账）后，撤回会**不再** fail closed。原始输出：

```
== baseline (post-claim second check present) ==
baseline: refused(fail-closed)=True letter_still_in_inbox=True archived=False
== MUTANT (post-claim second check removed) ==
mutant: refused(fail-closed)=False letter_still_in_inbox=False archived=True
```

基线 fail closed、变异体放行并归档了原信 → `tests/mutation_second_check_test.py` 确实红得起来（RED-capable 已由变异证实，不再是口头）。

## §15 补充：旧绑定 watcher 的 wake 分支（Codex 复审 3，postoffice:2977）

Claude 侧三个观察发布点里，**watcher 上岗**与 **wake 退出 2 分支**过 `activity_publish`，
只有 `binding == 当前路由 binding` 才写；**心跳**过 `activity_touch`（绑定不符本就不写）；单次
`activity` CLI 先按当前 `routes` 解析身份。旧绑定一律不写。真机白盒变异（把 wake 分支换回无保护直写）：

```
== FIXED (wake guarded by activity_publish) ==
fixed: watcher_rc=2 activity.state=working binding=s2 OK(new binding kept)
== MUTANT (wake guard removed) ==
mutant: watcher_rc=2 activity.state=working binding=s1 BUG(overwrote to old binding -> panel UNKNOWN)
```

真机负例：`tests/activity_test.py::test_live_watcher_rebind_wake_branch_never_publishes_old_binding`
（s1 watcher 存活 → 改绑 s2 → s2 写 working → 给信箱一封普通信 → 旧 watcher 退出 2 → binding 仍 s2、面板不 UNKNOWN）。
