# 交付：组织与身份标准化 v1（PO-ORG-IDENTITY-V1）

- 隔离 worktree：`/Users/bozhongxiao/code/po-org-identity`
- 分支：`org-identity-v1`，基线 = v1.13 PR #5 head `ff9a69172ad89e69ff2be0ade0d2d5ba3bcc34d2`
- 当前状态：**全部改动未提交**（未 push、未合并、未部署）；真实部署（panel v1.12.4）一字未动
- 管理者/终审：`postoffice_codex`；实施：`postoffice_lab`

## 交付内容

| 文件 | 说明 |
|---|---|
| `postoffice` | config v2 校验 + org 关系校验（核心/组织两份）、身份模块、`identity` / `rebind` 子命令、`add` 拆分「只改资料 / 显式改绑」、`write_contact` 不再存动态任职、`panel_state` 信箱行新增 `identity`、注册/恢复短身份提示 `identity_hint` + `identity --hint`、`SessionStart` 恢复钩子、稳定 role 切换交接从 scope 派生 |
| `opencode/postoffice.ts` | 插件恢复接缝：首次看到会话生命周期事件时 `identity <box> --hint` 注入一次（无组织时空操作） |
| `panel/index.html` | 面板真实消费 `identity`：inspector 最小显示现职 / 可候选 / 规章 / 进度（复用中英文，未新增组织树） |
| `tests/org_identity_test.py` | 22 项公开 seam 测试（新增，未跟踪） |
| `tests/panel_console_ui_test.mjs` | 新增第 36 块：面板消费 identity（现职/可候选/指针，零写请求，中英） |
| `tests/activity_plugin_test.mjs` | 新增：恢复接缝恰好注入一次身份提示 |
| `tests/smoke.sh` | 2 处「错版本」样例从 version 2 改为 version 3（version 2 现在是合法配置） |
| `skill/postoffice/SKILL.md` | 新增「身份与职责（v2 组织配置）」一节 + 首次读取约定 |
| `README.md` / `README.zh-CN.md` | 新增「组织与身份（可选，version 2 配置）」小节 + 验证表行 + 测试命令 |
| `docs/ORG_IDENTITY_V1.md` | 契约：术语、v2 配置结构、命令、迁移/回滚、三 harness 接缝、展示记录 Binding 冲突、边界 |
| `docs/ORG_CONTRACT_EXAMPLE.md` | 公司规章 / 项目 STATUS / 票角色引用示例（新增，未跟踪） |

`git diff --stat`（未含未跟踪文件）：9 files changed, 893 insertions(+), 76 deletions(-)；
未跟踪：`docs/ORG_IDENTITY_V1.md`、`docs/ORG_CONTRACT_EXAMPLE.md`、`docs/DELIVERY_org_identity_v1.md`、`tests/org_identity_test.py`。

## 公开行为（最小纵切）

真实临时邮局上的现场输出（节选）：

```
$ postoffice config import org.json
已导入配置：.../config.json
  groups 0 个，aliases 2 个

$ postoffice identity dev
MAILBOX      dev
  display    开发者
  binding    （未绑定会话）
  status     online
MEMBER OF
  - company boxz（盒子）
  - project postoffice（邮局）
ACTIVE
  - po.owner · 开发 → dev
CANDIDATE ONLY
  （无）
COMPANY RULES
  - boxz: org/rules.md
PROJECT STATUS
  - postoffice: .../org/STATUS.md
AS OF        2026-10-07 22:11:03
FINGERPRINT  9582734ed582e686

$ postoffice identity backup --json     # 同一份结果的机器可读形态
{ "active": [], "candidate_only": ["po.owner"], "rules": ["org/rules.md"], "status": [".../org/STATUS.md"] }

$ postoffice add dev --desc "活跃开发者"     # 已登记、不给通道 = 只改资料
已更新资料 dev（绑定与收信方式未变）

$ postoffice rebind dev --claude "我的窗口" --claude-session local_abc --source user
已重绑 dev：（未绑定会话） → claude_hook:local_abc（Claude Code 收信钩子（Stop/SessionStart 钩子，asyncRewake））
交接记录写入 .../logs/rebind.log；交接路径 .../org/STATUS.md
```

`rebind.log` 一行 JSON：`{"at":..., "box":"dev", "from":"（未绑定会话）", "to":"claude_hook:local_abc", "handoff":".../org/STATUS.md", "source":"user"}`。

## 测试结果（本 worktree，临时 POSTOFFICE_HOME）

- `tests/smoke.sh`：**通过 252，失败 0**
- `tests/org_identity_test.py`：**22 passed**（新增）
- 相关回归全绿：activity 12、outbox 15、provenance 9、panel_presentation 11、panel_letter 11、panel_send_ack 13、message_flow 25、retract 35、receipt 15、env_file 12、hierarchy 9、panel_origin 7（Python）；panel_console_ui（含第 36 块）/ panel_i18n / panel_mobile / panel_letter_ui / activity_plugin（7 checks）/ message_flow_plugin（Node）
- `py_compile postoffice` 通过；`node --check` 通过

## 退回修复（终审第 1 轮）

终审缺陷：version 2 同时有合法 alias 与坏 `organization` 时，运行期 `send @alias` 被整体
fail-closed 拦下（返回「配置无效；分组与逻辑地址已停用」），与「坏组织呈现不污染路由」相悖。

修复：把校验拆成两份——
- `config_errors`：只含核心 routing（version ∈ {1,2}、groups/aliases 形状、名字、引用、撞名）。运行期
  `load_config(validate=True)` 只按这份 fail-closed（坏 JSON / 重复键 / 未知版本 / 核心 routing 错误照旧停用）。
- `org_errors`（新）+ `organization_errors`：organization 语义 + role（scope 是否存在、项目角色不许自带
  handoff_file、role 需要 v2）。只影响身份呈现：坏 org 时 `identity` / 名片 / 面板报「已停用」，不捏造职责，
  物理收发信与 `@alias` 路由一字不变。
- `config import` 仍按「核心 + org」两份一起校验（组织语义错误在导入期整份拒绝）；`identity_ok(cfg, routes)`
  统一给 `write_contact` / `panel_state` / `org_context` 判定组织呈现是否可用。

负例（新增 `test_bad_org_does_not_disable_alias_routing`）：坏 organization（company member ghost 未登记）
被 import 拒绝；把同样内容手工放进运行期 config.json 后，`send @proj.owner` 仍投递到 dev、`list` 照常，
`identity` 报「停用」且无捏造职责；把 organization 修好后 `identity` 恢复现职。

## 终审第 2 轮补齐（一次性对照原票）

1. **名片不再存动态任职副本**：`write_contact` 删除「现职 / 可候选 / 当前由某人受理」行，只留物理身份、
   资料、`属于`、规章/进度指针与查询入口（`test_contact_has_no_dynamic_duty_copy`）。
2. **非成员 fallback 也拿到 scope 指针**：`identity_snapshot` 从**现职角色的 scope** 派生规章/STATUS
   （项目角色连带其所属公司的规章），但不把该信箱算成成员（`test_fallback_successor_gets_scope_pointers_without_membership`）。
3. **稳定 role 切换交接从 scope 派生**：新 `alias_handoff_path`（项目→status_file、公司→handoff_file 或
   rules_file、普通 alias→自身 handoff），`process_alias_switches` 交接提醒与 `config show` 都用它；
   alias 同时配 `role` 与 `alias.handoff` 在导入期被拒（`test_role_alias_handoff_derives_from_scope` /
   `test_role_plus_alias_handoff_is_rejected`）。切换事件与恢复去重原样保留。
4. **rebind_handoff_for 用统一 `identity_ok`**：坏 `project.company_id` 时不再记错误 STATUS 指针，
   交接留空并明确原因（`test_rebind_handoff_empty_and_explained_when_org_invalid`）。
5. **面板真实消费 identity**：inspector 最小显示现职 / 可候选 / 公司规章 / 项目进度（复用中英文，不新增
   组织树），零写请求（`tests/panel_console_ui_test.mjs` 第 36 块）。
6. **三 harness 接缝 + 首次读取约定 + 示例**：注册接缝（`add`/`rebind` 打印短身份，三 harness 共用）、
   Claude `SessionStart` → `identity --hint`、OpenCode 插件首次生命周期事件注入一次；首次用到才读规章/
   STATUS，普通信不夹带；示例见 `docs/ORG_CONTRACT_EXAMPLE.md`。Codex 无会话启动钩子 → 注册 + 主动查询
   （真实限制）。展示记录的 Binding 归属：`.presented.json` 是裸 id 扁平列表、`archive-current` 依赖它，
   按记录存 Binding 需改格式与展示范围语义 → 按票**报告冲突**，本版不改（见 ORG_IDENTITY_V1.md）。

## 迁移 / 回滚（详见 docs/ORG_IDENTITY_V1.md）

- **不用组织身份**：不动。version 1 语义完全不变，`identity` 只显示物理事实。
- **迁移**：`version` 改 `2`，按需加 `organization`，老 alias 原样；`config import` 会先整份校验、备份旧文件到 `logs/config.json.<时间戳>.bak`，再整文件替换（从不合并）。
- **回滚**：用那个 `.bak` 覆盖回 `config.json`（或重新 import 旧文件），`config show` 确认。

## 终审第 3 轮：三个窄修（不改数据模型、不扩票）

依据 `logs/ORG_IDENTITY_REVIEW3_20261007.md`，每项都补了最小公共入口负例。

1. **稳定角色切换也校验组织；有效交接带 rules + STATUS。**
   `alias_handoff_path(name, spec, cfg, routes=None, ok=None)` 现在返回 `(path, reason)`：
   - 普通 alias 不受组织校验影响，照旧用 `spec.handoff`；
   - 角色 alias 必须组织有效（`identity_ok`），坏组织时 `path=""` 且 `reason` 说明为什么，
     `process_alias_switches` 与 `config show` 都用 `reason or path`，**绝不写出已停用组织的 STATUS/rules**；
   - 组织有效时，项目角色交接同时带 scope 所属公司的 `rules_file` 与项目的 `status_file`。
   负例：`test_role_switch_handoff_valid_carries_rules_and_status_broken_org_carries_neither`
   （真跑邮递员轮次，先证明有效组织带 rules+STATUS，再破坏 `company_id` 后证明切换仍照常发生、
   交接写「已停用」且不含 `STATUS.md`）。
2. **OpenCode 恢复提示改用官方「仅上下文」入口。**
   `client.session.prompt({ body: { noReply: true, parts } })`（SDK 文档：`body.noReply: true`
   只注入上下文、不请求模型答复；旧 SDK 缺 `prompt` 时回退 `promptAsync` 但同样带 `noReply: true`），
   且**只在 idle 接缝注入，busy 事件不再触发任何注入/答复**。负例（`activity_plugin_test.mjs`）：
   busy 不注入、不请求答复 → idle 恰好注入一次、`body.noReply === true`、不走 `promptAsync` → 再 busy/idle 不重复。
3. **hint 与面板都说清「坏组织为什么停用」，并区分未配置。**
   - `identity_snapshot.org_present` 改为「配置里是否存在 organization 段」（不再是「表非空」），
     所以「配了但坏了」不再被当成「没配」而静默；
   - `identity_hint` 现在把真实原因算出来传入（此前坏组织会显示空的 `（…）`）；未配置组织仍沉默；
   - `panel_state` 把 `config_errors + org_errors` 的合并原因传给 `identity_snapshot`
     （此前只传核心错误，坏组织的面板 `error` 为空）；面板 inspector 在
     `org_present && !config_ok` 时显示 `idDisabled` 行（中/英「组织配置已停用 / ORGANIZATION DISABLED」）+ 原因。
   负例：`test_identity_hint_explains_broken_org_and_stays_silent_when_unconfigured`、
   `test_panel_reports_org_disabled_reason_and_unconfigured_is_silent`（后者还证明普通投递不受影响）。
   - 示例 `docs/ORG_CONTRACT_EXAMPLE.md` 里 STATIC 的「人名任职行」改成角色引用（见下）。

### 用户新增：有界勘察「Codex 信件界面不自动折叠」（只读，未改任何投递行为）

真实二进制 `/Applications/ChatGPT.app/.../codex`（`codex` 不在 PATH，邮局用 `find_codex()` 里这个绝对路径）：

- 线上投递即 `codex queue --thread <THREAD> --message <TEXT>`（`postoffice` 邮递员 ~3831 行）。
- `codex queue --help`（只读查看）**只有** `--thread/--message/--image/--model/--profile/-c/--cd` 等通用选项，
  **没有** `--fold/--collapse/--group/--combine/--batch` 之类折叠参数；顶层命令里也没有折叠/合并子命令
  （只有 `queue`、`migrate-rollouts` 等）。
- **区分气泡折叠 vs 同批合并**：现有「回执合并成一条 / 同轮同目标批量投递」属于**同批消息合并**
  （邮局侧行为，已存在）；用户想要的「气泡自动折叠」属于 **Codex 客户端 UI 渲染层**，不在 `queue` 的
  CLI 契约里，本机无可用官方接缝。
- **结论（真实限制）**：`queue` 无折叠参数、Codex CLI 未暴露 UI 折叠开关，邮局**无法**控制这个气泡行为；
  按信里要求**不新造通道、不造私有 UI 注入、不影响本票与线上**，此处仅如实报告。

## 逐条对照票（已证明 / 未证明）

- **A 契约与配置**：已证明。version 2 + `organization` 整份校验；坏引用（缺失公司、成员未登记、项目成员不属于公司、角色 scope 不存在、项目角色自带 handoff_file、v1 出现 organization）一律拒绝且旧 `config.json` 字节不变（`test_bad_refs_rejected_and_old_config_untouched`）。未证明：更深的公司/项目嵌套（本版只有两层）。
- **B 身份快照**：已证明。`identity` 现职 vs 可候选、未登记信箱、组织无效时「停用但不致命、不捏造职责」（`test_identity_active_vs_candidate` / `test_identity_org_invalid_is_diagnosable_not_fatal`）。`active` 复用发信解析器 `alias_target`。
- **C 登记解耦与改绑**：已证明。已登记不给通道只改资料、方法/绑定不变；显式通道 = 改绑并写 `rebind.log`；新信箱不给通道被拒（3 项测试）。
- **D 一处推导、三处呈现**：已证明 card（`CONTACT.md`）、panel（`/api/state` 的 `who` 与 `identity`）、CLI 报同一份推导身份（`test_panel_and_contact_match_identity`）。面板与名片都停用「实时在线/当前受理人」照抄与直连队列提示。
- **三条 harness 接缝**：已证明。注册接缝（`add`/`rebind` 打印短身份，三 harness 共用，`test_registration_prints_identity_hint_once`）、Claude `SessionStart` → `identity --hint`（`test_claude_install_adds_resume_hint_hook` / `test_identity_hint_resolves_from_claude_payload`）、OpenCode 插件首次生命周期事件注入一次（`activity_plugin_test.mjs` 恢复接缝项）；身份解释都来自同一个 CLI 结果。Codex 无会话启动钩子 → 注册 + 主动查询（真实限制）。普通信不夹带规章/岗位（`test_ordinary_letter_carries_no_rules_or_roles`）。
- **展示记录 Binding 归属**：按票**报告冲突**。`.presented.json` 是裸信件 id 扁平列表，`archive-current` 的展示范围依赖它；按记录存 Binding 会改动该语义 → 本版不改格式（详见 ORG_IDENTITY_V1.md）。
- **最小纵切优先**：遵循。先落地 config 校验 → identity → card/panel 一致；改绑/handoff 与旧固定任命清理随后。

## 发布前文档清理（本轮做掉的部分）

- `README.md` / `README.zh-CN.md` 顶部各加一段**版本状态**说明：写清本分支代码的当前行为
  （`VERSION 1.13.0`，PR #5）、组织/身份层是**可选且默认关闭**（没有 `config.json` 就没有组织，
  其余信箱一字不变）、**v1.12 / v1.13 小节属已发布历史**、旧版本说明与过往发布记录在 `docs/`
  属参考资料而非现行指引，并且「不把未部署功能写成线上事实」——任何安装实际跑哪个版本以
  `postoffice --version` 为准。
- 修正两处**过期事实**：双语验证表里 `tests/org_identity_test.py` 的项数从 10 改为 22。
- `docs/ORG_CONTRACT_EXAMPLE.md` 的 STATUS 示例里那条**静态人名任职行**改为**角色引用**
  （`@postoffice.owner` / `@postoffice.review` 由谁受理以 `postoffice identity` 当次查询为准），
  避免教程本身再制造一份静态任职副本。
- 两条规则现在各只有一处权威表述：身份查询入口在 `docs/ORG_IDENTITY_V1.md` + skill 一节，
  README 只保留概述并指向它；不再在多处重复不同措辞。
- 历史设计/发布记录（`docs/DELIVERY_v1.13.md`、`docs/PROBE_EVIDENCE_v1.13.md`、
  `docs/TICKET_v1.13_human_console_maturity.md`、`docs/DELIVERY_org_identity_v1.md` 等）
  **未做无差别删除**，仅从现行指引中移除。

## 集成基线与顺序（PR5 / PR6 / 本组织票；均未合并、未部署）

| 分支 | PR | 基线 | head SHA | 状态 |
|---|---|---|---|---|
| `human-console-maturity` | **#5** | master `ce14581`（v1.12.4） | `ff9a69172ad89e69ff2be0ade0d2d5ba3bcc34d2` | 冻结，已推；未合并 |
| `slack-grace-timeout` | **#6** | master `ce14581`（v1.12.4） | `98b5451087f0059a4a00f5ab391bdba81308b5ff` | 冻结，已推；未合并 |
| `org-identity-v1`（本票） | 待开 | **PR #5 的 head `ff9a691`** | 未提交（工作树差异，9 files changed / +964 / -77，另有 4 个未跟踪文件） | 待终审 |

建议的合并顺序（每步一次必要回归，不重复全量）：

1. **PR #5**（v1.13.0）先合 master —— 它是本票的基线。
2. **PR #6**（Slack grace 超时同步）再合 —— 它基于 master `ce14581`，只需在 #5 之上重放一次。
3. **本组织票**最后合（它直接长在 #5 之上），必要时 rebase 到 #5+#6 合并后的 master。

**冲突预期**：三条分支都改到 `postoffice` 与 `README.md` / `README.zh-CN.md`；#5 还改
`panel/index.html`、`opencode/postoffice.ts` 与 `tests/`。本票与 #5 在 `panel/index.html`、
`opencode/postoffice.ts`、`skill/postoffice/SKILL.md`、`tests/panel_console_ui_test.mjs`、
`tests/smoke.sh` 上重叠（因为本票基于 #5，重放即可）。合并后**一次必要回归**即可，
不需要重跑全部历史用例。

**未合项**：#5、#6、本组织票**三条都还没合并到 master，也都没部署**；本机线上仍是 v1.12.4。
按转述安排：合 master ≠ 重启/替换本机线上服务，本机工作流重整另按用户安排。

## 边界 / 未决

- 跨会话组织权限、真实规章文件的地就位校准一律**不做**（票内排除项）。
- 公司 `rules_file` 目前按配置原样显示为指针（建议写绝对路径）；项目 `status_file` 相对路径从项目 `root` 解析。
- 展示记录 Binding 归属未做（按票报告冲突，见上）。
- 未提交、未 push、未合并、未部署；等 Codex 终审后按用户指示处理提交。
