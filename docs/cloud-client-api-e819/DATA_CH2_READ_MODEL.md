# 章 2 · read_model 字段表（Data 线）

实测基线：R0 候选 `bbf3c8e`，**本轮已刷新为本地候选 `e819f0e`**（worktree `<local worktree>`）。方法：隔离 temp HOME＋匿名五箱（panelop/box_a/box_b/box_unlisted/box_c）＋**完整合法**匿名 config（organization/aliases/groups/panel 全合法，role 带 scope——否则 role 投影全空且 config_ok=false，这是本轮实测的匿名数据坑）。每个字段标注：来源事实 / 一致性 / 缺失语义 / 前端可见权限。**missing≠0、unknown≠0、空数组≠错误**。差异段以【e819 增量】标出。样例全文见 `fixtures-data/state_ok.json`。

## 1. /api/state 顶层

| 字段 | 来源事实 | 一致性 | 缺失/异常语义 |
|---|---|---|---|
| `home` | POSTOFFICE_HOME 路径 | 每次请求实读 | 恒有字符串 |
| `boxes[]` | routes 表×投递快照×身份投影合并 | bounded 锁→持锁时**混读可能**（章1§2） | 恒数组 |
| `config` | config.json 路径字符串 | 同上 | `cfg is None` 时为 `""` |
| `config_error` | 读/解析 config 的错误 | 同上 | 无错 `""`；有错中文串（物理事实仍展示，组织静默） |
| `panel` | `panel_config()` 独立校验 panel 段 | 与路由资格无关 | 坏配置→`{ok:false,error:"panel 配置：…",label:"",operator:"",organization:null}`（实测两种坏法：缺 organization 键、organization=null） |
| `groups[]`/`aliases[]`/`switches[]`/`broadcasts[]`/`role_cards[]`/`log[]` | 各自只读派生 | 同 boxes | 恒数组（实测空=[]，非 null） |

## 2. boxes[] 行（键全集实测：name/app/method/who/online/ident/pending/counts/activity/identity/acks）

| 字段 | 来源 | 一致性/缺失语义 |
|---|---|---|
| `name` | routes 键 | 恒有 |
| `app` | `APP[methods[0]]` 显示名 | methods 缺省 `["notify"]`→"手动" |
| `method` | methods[0] | 同上 |
| `who` | profile.description 兼容显示 | 无则 `""` |
| `online` | routes.status=="online" | 布尔；**offline≠不存在** |
| `ident` | claude_title→session_id→thread_id 回退链 | 无绑定 `""` |
| `pending[]` | inbox 信件×投递状态 | 项：`file,status(waiting/reminded/failed/ride),kind,subject,from,mtime`；空=[] |
| `counts` | 同 pending 分桶计数 | 四键恒在，0 是真实 0 |
| `activity` | activity 写点 | `{state:unknown|busy|idle,since:null|ts}`；**unknown 是真实状态不是缺失**，删了无妨 |
| `identity` | identity_snapshot（详见 §3） | — |
| `acks` | recent_acks | 数组 |

## 3. identity（与 CONTACT/CLI/harness hint 同一派生；键全集实测）

`box, registered, profile{display_name,kind,description,legacy_note}, binding, status, method, as_of, memberships[], active_roles[], candidate_only_roles[], company_rules[], project_status[], org_present, config_ok, error, fingerprint`

- `binding`：null=未绑定 harness（**不是错误**）；有值时含 adapter/ref 等（v5.4 后 typed binding，本基线实测 null 与字符串两种均出现过）。
- `active_roles[]` vs `candidate_only_roles[]`：**候选≠任职**（v4 验收红线）：只在某岗位候选里而当前非执行目标的进 candidate_only，绝不显示成现职。
- `company_rules[]`：`{company,path,freshness{state:ok|stale,as_of_text,stale,sources[]}}`——stale 是新鲜度判断，不是缺失。
- `config_ok/error`：组织段校验结果；`config_ok:false` 时角色投影仍尽力给物理事实。
- `fingerprint`：身份短指纹（展示用）。

## 4. 权限可见性（谁能看见什么）

- 全部 GET 路由**无鉴权**（无 token/账号，章1§3）：【e819 增量】服务端**已校 Host＋Origin**（X-01 精确白名单，见章1§3）——本机任意进程直连（无 Origin）仍可读 `/api/state`、`/api/letter`（含正文）、`/api/outbox`；浏览器跨站脚本与 DNS rebinding 被 403 挡住。**这是产品现状事实**，不是建议；secret 类内容本来就不该进信件正文（知识红线在上层保证）。
- `operator`（`config.panel.operator`）只影响写路径（edit/retract/outbox 可见范围）。
- 读模型不含：`routes.status` 之外的任何凭据、宿主会话内容、模型输出原文。

## 5. 缺失语义速查（前端词汇）

| 现象 | 含义 | 不是 |
|---|---|---|
| `activity.state=unknown` | 没有观测 | 不是离线 |
| `binding=null` | 未接 harness | 不是错误 |
| `config_error` 非空 | 组织段不可用 | 不是信箱不存在 |
| `panel.ok=false` | 面板配置坏（operator 空→写不可用） | 不是 state 失败 |
| `pending=[]` | 真的没信 | 不是读失败 |
| `counts.*=0` | 真实零 | 不是 missing |
| GET `/api/letter` 404 | 信不在/箱不存在 | 不是没权限 |
| role-diff 404 | 没有这个岗位 | 不是配置坏 |
| 【e819 增量】letter id 原样 UTF-8 → 400 HTML 页 | 请求行非法 | 不是 JSON 错误契约；先 encode |
| 【e819 增量】信箱已登记但不在 `panel.organization.members` | 未编入组织树 | **API 不标记、不给错误**；前端用 `boxes[].name − members` 自行差集（实测 box_unlisted/box_c 如此） |
| 【e819 增量】role_apply 409 | 基线过期（配置被外部改过） | 不是权限/不存在；且 409 先于 404 |

## 6. 一致性分级（章1已测，汇总）

- **consistent**：持锁读成功（role-diff/apply 阻塞路径）——diff 与 baseline 同窗。
- **degraded（有界回退）**：`/api/state` 持锁时 5.5ms 无锁读——快照可能跨窗口；不失败、不阻塞。
- **投影待刷**：role-apply 的 `projection:"pending"`=权威已存、名片缓存未刷新（不得当作失败）。
- **未知**：写竞争败者只保证 fail closed，不保证具体错误码。

## 7. 已实测样例（【e819 增量】role_cards/aliases 由推断升级为实测）

- `role_cards[]`：list，每卡 16 键实测全集：`name,title,project,active,kind,escalation_acceptor,holder_state,candidates[{name,state}],escalation_role,escalation_title,escalation_target,duties[],resources[{kind,ref,note,path,exists}],confirmed,config_ok,status_file{path_exists,updated}`。`holder_state`/ candidates.state = 结构化状态码（`online_notify`/`offline_bound`/`unregistered`…，绝不含会话 ID/线程号）。
- `aliases[]`：list，每行 15 键实测全集：`name,target,confirmed,kind,escalation_role,escalation,pending,pending_in,notify[],handoff,has_role,duties[],resources[{kind,ref,note}],candidates[{name,known,online}]`。
- `pending[]` 三态实测样本：`status=waiting`（ inbox 未被提醒）、`reminded`（notify 箱经 `.woken.json`）、`failed`（opencode_plugin 箱经 ledger `FAILED_FINAL`）；`counts` 同步分桶。
- `identity.company_rules[]`：freshness 实测 `missing` 态（rules_file 指向不存在文件）；`project_status[]` freshness `ok` 态（STATUS.md 存在）。
- 完整 JSON：`fixtures-data/state_ok.json`。

## 8. 推断（未实测）

- `binding` 有值时的完整字段形状：本基线匿名数据只有 null 与字符串两态，typed binding 展开形状未实测（以 v5.4 契为准，标**未证**）。
- `switches/broadcasts` 丰富样本（切换记录、广播）：仅见到空数组，未构造，标**未证**。
- `POSTOFFICE_PANEL_BASE_URL` 第二可信 Origin：未设置实测，标**未证**。
