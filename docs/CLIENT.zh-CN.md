# BOXZ OFFICE 独立客户端（第一纵切 · 语义纠正批）

> 状态：**可运行的匿名交互原型，尚未真实接线，尚未原生打包。** 本批没有任何一项“真实接线已完成”；云端写入不等于本地已接通；Node 测试通过不等于视觉验收，也不等于输入法验收。
> 分支：`claude/office-client`（基于 `master` 的 `1ab0f54`，v1.14.1）。没有改动 `panel/index.html`，旧 Panel 原样可用。
> 本批依据本地团队对 `5aacda7` 的接线核对（对照本地加固候选 `e819f0e`）做语义纠正：先改适配层、语义和测试，没有重画界面。

## 1. 三个来源，分开看

| 来源 | 是什么 | 本客户端怎么对待 |
|---|---|---|
| **GitHub master v1.14.1**（`1ab0f54`） | 已发布的后端。`do_POST` 只有 `status / clear / archive-one / ack-one / send / edit-one / retract-one`，没有 `role-diff / role-apply` | 只映射 `/api/state` 里真实存在的字段（§7）；其余一律 `capability_missing` |
| **本地候选 `e819f0e`** | **不在 GitHub 上**。有 `POST /api/role-diff`、`/api/role-apply`，`/api/state` 另有 `role_cards` 等 | 只凭文字描述，客户端按其写入合同做了岗位编辑流程（§6），但 HTTP 适配器**还没有调用它**；等本地团队给匿名 fixture 后做一次映射 |
| **客户端 mock** | 本页的样例数据（匿名公司 Northwind Studio） | 任何界面都能演示，什么后端也不碰；所有 mock 区域都有「样例」标 |

旧文档里把 master 称作“已发布 R0”的叫法已取消：能力表的列名现在是「GitHub master v1.14.1」「本地候选 e819f0e」「客户端 mock」。

## 2. 怎么看

```
cd client && python3 -m http.server 8790      # 任何静态服务器都行，无构建、无依赖
# 浏览器打开 http://127.0.0.1:8790/
?scenario=default|empty|degraded|network|denied|missing|conflict|projection   # 样例场景（也可在「能力」弹窗里切换）
?live=1                                      # 样例世界每 6 秒推进一条事件（演示有界缓冲）
?backend=http                                # 切到 HTTP 适配器，请求同源的 /api/state（见 §4：只用于本地同源匿名隔离预览）
#/office/<role>[/threads|work|edit]   #/threads/<thread>   #/work?role=<role>      # 深链
```

`conflict` = 你预览之后配置被别处改了一次（apply 返回 409，且**不带** current）；`projection` = 配置已保存、派生名片待刷新。

测试（Node，无浏览器）：`node tests/client_contract_test.mjs`、`node tests/client_render_test.mjs`。

## 3. 客户端怎么组织

```
client/
  index.html                入口（无构建，原生 ES module）
  styles/                   tokens.css（面板的 12 个颜色角色 × 6 套配色）· base.css · office.css
  src/
    contract.js             数据契约：枚举都带 unknown；数字是 number|null；normalize* 从不猜
    capabilities.js         “能做什么”的唯一登记表，三个来源各一列：应用内「能力」弹窗与本文 §8 的表都从它生成
    adapters/               ★ 唯一的数据入口
      mock.js               样例后端（匿名夹具 + 8 种场景），按真实写入合同校验、按整配置基线判 stale
      http.js               master 适配器：/api/state → 快照（mapMasterState）；其余返回 capability_missing
      index.js              按 URL 选择适配器
    fixtures/demo-company.js  匿名样例公司，不含任何真实数据
    flows/roleEdit.js       岗位编辑状态机与请求构造（纯函数）：草稿→预览→确认→结果
    views/                  纯函数 (状态)→HTML 字符串
    app.js · paint.js · router.js · theme.js · i18n.js · strings.js · fx.js · host.js · main.js
```

- **一个数据适配器**：视图只认 `getSnapshot / previewRoleChange(request) / applyRoleChange(request, baseline) / createThread / sendToThread / decideApproval / subscribe`。**没有 `retryProjections`**：后端没有这个端点，放一个会重复治理写入的假按钮比缺失更糟。返回是 `{ok:true,data}` 或 `{ok:false,error:{code,message,retryable,…}}`，`code` ∈ `denied / stale / capability_missing / pending_approval / degraded / network / invalid / not_found / server`。
- **前端不存权威**：岗位、权限、可编辑与否都来自快照；客户端只提交请求和显式确认，写前由后端重核。隐藏按钮不是权限系统。
- **缺能力不可操作**：能力为「缺失」的功能保持不可点，不用假数据补全。

## 4. 鉴权、CORS 与桌面宿主

- 候选后端的 Host / Origin 精确允许集是**本地防跨站保护，不是老板 / 秘书身份认证，也不是角色权限系统**。当前没有 token 方案，也没有正式的 `can.*` 授权接口：客户端不在前端猜授权，`role.can.edit` 在真实后端接通前只会出现在 mock 里。
- 后端没有开放任意 CORS：`:8790` 的页面直连 `:8765` 是跨源，**不能靠删掉 Host/Origin 保护来接通**。因此**首个真实接线 = 本地同源、匿名、隔离的预览**：由本地团队让后端与 `client/` 静态资源同源承载（同一个端口、同一个进程或反向代理），页面带 `?backend=http` 读 `/api/state`。云端 Artifact 只演示匿名 mock，不连真实本地 Office。
- **桌面宿主接缝（`src/host.js`）只是预留，不代表桌面客户端已完成。** 早先写的 `backend() → {baseUrl, headers}` 是错的：把 headers 交给 Web JS，就等于把长期高权限凭据放到页面里，页面上任何脚本、扩展或被注入的字符串都读得到、重放得了，同时也否定了“Web 看不到凭据”的说法。现在的接缝是：`notify / openExternal / request(op, args)`。凭据只在**可信宿主**里；`request` 不是代理：`op` 是宿主实现的固定类型化操作名（如 `state.get`、`role.diff`、`role.apply`），宿主自己映射到一次后端调用、自己加凭据、按它认证的主体与作用域检查，**不接受任意 URL、任意文件路径、调用方自选的 header**；写入仍由后端重核。op 清单与认证方式属于接口包，本批不定。

## 5. 本批可预览的纵切：岗位 → 线程 / 邮件

1. **公司入口（Office）**：树 = 公司。座位用「标记 + 文字」呈现 现任 / 空缺 / 候选数，以及 已连接 · 工作中 · 省电 · 离线 · 未接通 · 状态未知（不靠颜色）。红色只表示“有事等你”。现任旁另列三个**独立事实**：投递开关（开 / 关）、会话绑定、最近活动（带时间与来源）。**开关打开不等于已连接；开关关闭也可能只是老板手动下线。** 没有新鲜的活动信号就是“状态未知”，不是空闲。
2. **岗位详情（右侧停靠，窄屏全屏）**：概览（现任、候选、升级给谁、职责、资源、资格[原型]）· 线程 · 邮件与工作 · 编辑。
3. **岗位编辑**：见 §6。
4. **线程**：观察 ≠ 控制；发出的指令只显示“已受理”，不假装已读取；事件是有界缓冲；新建线程只是申请。（仍是 mock）
5. **工作与邮件**：两列分开。邮件状态保留后端的说法：**在信箱待提醒 / 已提醒未取用 / 已受理 / 已被取用 / 失败**，认不出的状态显示“状态未知”并保留原词，不并成“已受理”。邮件被受理或取用都不等于工作完成；工作用一套通用状态，不是从邮件状态推出来的，也不把“已发布”固定成权威单线状态机。（工作仍是 mock）
6. **Token HUD**：只显示官方累计 input / output / cache / total、模型、更新时间、来源、覆盖；缺失显示“未知”而不是 0；不自己相加；上下文占用单独一块。（仍是 mock）

## 6. 岗位编辑：按候选后端的真实合同

本地候选有 `POST /api/role-diff` 与 `POST /api/role-apply`（master 没有）。客户端按下列合同实现（`src/flows/roleEdit.js`），**候选尚未部署，不宣称已接通**。

**请求**（匿名示意）

```json
{ "name": "demo.review", "candidates": ["reviewer_one", "reviewer_two"], "escalation_role": "demo.manager",
  "duties": ["核查提交与证据"], "resources": [{"kind": "doc", "ref": "review.md", "note": "审查指引"}] }
```

- `name` 是**逻辑岗位名**，不是信箱节点 id。`candidates` 必填、非空、无重复、**顺序有意义**；`escalation_role` 可为空。
- `duties` / `resources`：**键缺失 = 不改，显式 `[]` = 清空，给了新值 = 整张列表替换。** 客户端只在草稿真的改了某张列表时才带这个键；编辑页没显示的字段绝不会被当成空数组发出去。
- 资源形状是 `{kind, ref, note?}`，不是 `{kind,label}`。当前 `kind` ∈ `doc / dir / skill / url`；职责 ≤ 5 条、每条 ≤ 80 字符；资源 ≤ 8 条、`ref` ≤ 200、`note` ≤ 60。表单即时提示这些限制，后端再校验，以后端为准。
- 请求里**没有**别的键：资格、策略、能力分配仍属设计，表单里只有一行“设计中，尚未接线”的说明，没有字段，也不往载荷里加任何 mock 字段。

**diff 成功**：HTTP 200，`{ok:true, diff:{name, change, expected, facts, baseline}}`。`baseline` 是**整份配置字节的 SHA-256**，对客户端不透明：原样带回 apply，绝不自己生成；预览没有 `baseline` 就不能提交。`change` 给候选 / 升级 / 职责 / 资源的 from→to，`expected` 给预计影响，`facts` 给本次事实。后两者的内部形状我们还没有 fixture，所以按后端写的原样显示（字符串仍是字符串，其余紧凑打印），不从里面推导任何东西。

**apply** = 同一个请求 + `baseline`：

| 结果 | HTTP | 客户端怎么呈现 |
|---|---|---|
| 已保存 | 200 `ok:true, saved:true, projection: fresh \| pending \| not_applicable`（另有 `message / backup / active_now / acceptor_now`） | **“已保存”和“派生名片是否新鲜”分开两件事。** `pending` = 权威配置已保存、名片待刷新：不报成“提交失败”，不提供重新提交；只给“刷新状态”（重新读快照）。没有通用 `retryProjections` 端点，失败项明细接口也不齐，不从 message 猜结构化字段 |
| 基线冲突 | 409 `ok:false, stale:true, saved:false, projection:not_applicable` | **当前不返回 `current`。** 保住草稿，重新取快照，把“你开始编辑时 / 你的草稿 / 最新快照”三方摆出来 |
| 校验错 / 不存在 / 备份失败 | 400 / 404 / 500，带 `error`，`saved:false` | 显示后端的 error，并说明后端报告没有保存任何东西 |
| 没收到回复（网络） | — | **结果未知**，不是“失败”：不自动重发、不给一键重试，只给“刷新状态”；若刷新后的快照已与草稿一致，提示“很可能已保存” |

**并发**：当前后端是整列表替换 + 整配置基线 hash，冲突就拒绝，客户端忠实实现这一点。基线覆盖**整份配置**，所以别的岗位变了，这个岗位的预览同样会 stale（mock 也这样）。冲突时：没动过的字段随上游，动过的字段保留我的草稿；若上游新加了职责 / 资源，而我的整表草稿会把它们移除，页面会点名警告，不静默重放旧编辑。操作式补丁是可研究的后续合同（需要稳定条目身份、重复与顺序语义、冲突与权限），不在本批，也不会因此要求后端加接口。

## 7. master 映射：做了什么、拒绝做什么

`mapMasterState`（`src/adapters/http.js`）对真实的 `/api/state`：

- **树节点是信箱，不是岗位。** 节点 id 为 `box:<name>`，带 `origin:"mailbox"`；它不能成为岗位稳定 id，也**不能当岗位编辑**（页面写明“这是按信箱画的旧节点”）。岗位与信箱不是一对一：同一员工可有多个职责，候选不是现任，升级受理人也不兼任被升级的岗位。
- **在线 ≠ 连接。** `online`（投递开关）、会话绑定（`identity.binding.kind`）、`activity`（`working / idle`，带 `since`）各自保留；只有**新鲜的**活动信号才决定状态（working → 忙，idle → 已连接），否则一律“状态未知”。缺值不补成空闲。
- **有效的树也会漏信箱**：未被纳入的信箱列在“未编入的信箱”节点下，不会消失；只有整棵树无效时才退化成全部列在一个根下并标“降级”。
- **邮件**：`pending[].status` 的 `waiting / reminded / failed` 原样保留；认不出的是 `unknown` 并保留原词。后端没有记录信是发给哪个**岗位**的，所以邮件只归属**信箱**（标“按信箱 X”），`roleId` 留空，不伪造“当时的岗位”。
- 线程 / 工作 / 批准 / 用量：master 没有，快照里就是空，不编造。

本地候选的 `role_cards`（`name / title / active / candidates / duties / resources / escalation_acceptor / kind`）**尚未映射**：字段类型只有文字描述。等本地团队给匿名 e819f0e fixture，在适配层映射一次。

## 8. 能力表（由 `capabilities.js` 生成，测试保证与之一致）

状态：已接通 / 部分 / 样例（mock）/ 缺失（该来源不提供）/ 未核对 / 后续。

<!-- caps:begin -->
| 功能 | GitHub master v1.14.1 | 本地候选 e819f0e | 客户端 mock | 说明 | 接通需要 |
| --- | --- | --- | --- | --- | --- |
| 公司 / 岗位树与在线状态 | 部分 | 部分 | 样例 | master：树来自 config 的 panel.organization，节点是信箱，不是岗位。树漏掉的信箱会列在「未编入」下。 | read_model.roles, read_model.presence |
| 岗位职责 / 资源 / 资格 | 缺失 | 部分 | 样例 | master 的 /api/state 不含岗位职责与资源。本地候选的 role_cards 有 name / title / active / candidates / duties / resources / kind，字段形状待匿名 fixture 核对后再映射。 | read_model.roles[].duties, resources, qualification |
| 现任与候选的区分 | 部分 | 部分 | 样例 | master 只有每个信箱的事实：投递开关、会话绑定、最近活动。只有新鲜的活动信号才算「已接通 / 忙」，开关打开不等于已连接。 | read_model.roles[].incumbent, candidates |
| 岗位编辑：草稿 → 差异 → 确认 | 缺失 | 部分 | 样例 | 本地候选有 POST /api/role-diff 与 /api/role-apply，master 没有。客户端按其合同做了流程（整配置基线、已保存 / 派生名片分开），HTTP 适配器尚未调用。资格 / 策略 / 能力分配仍是设计，不在写入合同里。 | anonymous role-diff / role-apply fixtures incl. 409 and projection-pending samples; same-origin hosting |
| 线程列表与归属岗位 | 缺失 | 未核对 | 样例 | master 没有会话列表接口。 | read_model.threads |
| 线程文本 / 工具事件（有界缓冲） | 缺失 | 未核对 | 样例 | 客户端只保留最近 N 条，可从快照恢复；不复制 Harness 的完整历史。 | event stream or cursor API, snapshot resume |
| 线程控制（观察 ≠ 控制） | 缺失 | 缺失 | 样例 | 只有后端确认的 Office 绑定可输入。创建 / 发送在 mock 里只返回样例结果，没有连接任何 Harness。 | thread create / send API, control state, approval flow |
| 邮件收执状态 | 部分 | 部分 | 样例 | 待处理邮件保留后端状态：在信箱待提醒 / 已提醒 / 失败，不并成「已受理」。后端不记录信是发给哪个岗位的，所以只按信箱归属。 | read_model.mail with delivery + threadId / workId |
| 工作提交 / 验证 / 接受 / 退修 / 发布 | 缺失 | 缺失 | 样例 | 一套通用状态，不是某个行业专用的状态机。后端未接线。 | work module read model + transitions |
| 待批准事项 | 缺失 | 缺失 | 样例 | 批准决定由后端执行。 | approvals read model + decide API |
| Token HUD（官方累计用量） | 缺失 | 缺失 | 样例 | 用量账模块已验收，但真实 reader / HUD 接口未交付；缺失值显示「未知」，不当作 0。 | usage reader API: input / output / cache / total / model / updatedAt / source / coverage; context occupancy |
| 实时推送与快照恢复 | 缺失 | 未核对 | 样例 | master 为 5 秒轮询。 | stream with resume token |
| 秘书（受控工具 / 批准条件） | 后续 | 后续 | 后续 | 后续方向，等后端能力包。 | capability pack |
| 会议 | 后续 | 后续 | 后续 | 后续方向。 | capability pack |
| 技能与插件 | 后续 | 后续 | 后续 | 后续方向。 | capability pack |
| 跨 Office | 后续 | 后续 | 后续 | 后续方向。 | capability pack |
<!-- caps:end -->

## 9. 还需要本地团队给什么（按最短路径）

最短路径：语义纠正（本批）→ 准确候选 fixture / 错误样例 → 本地同源隔离读接线 → 实际岗位编辑回归 → 线程 / 工作 / 用量按能力增量接入。

1. **匿名 e819f0e fixture**：`/api/state`（含 `boxes[].identity / activity`、`role_cards`、`aliases`）一份；`role-diff` 成功、400、404 各一；`role-apply` 成功（`projection` 三值各一）、409、500 各一；`diff.change / expected / facts` 的真实内部形状。
2. **同源承载方案**：让后端与 `client/` 同源，页面才能读到真实数据；不要为了预览开 CORS 或删 Host/Origin 保护。
3. **邮件的发 / 收岗位引用**：目前没记录，邮件只能按信箱画线。
4. 线程列表 / 事件游标 / 绑定与批准、工作模块读模型、用量 reader 与 HUD 接口：未交付前继续 mock，**不由前端临时造一套**。
5. 身份与鉴权：客户端以谁的身份请求、`can.*` 的判定主体是谁。

## 10. 验证状态（如实）

- Node：`client_contract_test.mjs`、`client_render_test.mjs`。覆盖：请求只含合同键、缺键 / `[]` / 替换、限制与校验、基线整配置失效、stale 无 `current`、三方冲突与“会被移除”的警告、`saved + pending` 不重发 apply 且无假重试、网络丢回复=结果未知、`online ≠ connected`、有效树漏信箱、waiting / reminded 区分、邮件只按信箱归属、宿主接缝无 headers、能力表列名与 docs 一致。
- 浏览器（我在 Chromium 里看过截图）：桌面与窄屏的编辑 / 冲突 / 已保存待刷新 / 能力弹窗，以及用同源假后端喂匿名 master 样例的树与邮件。**这不是视觉最终验收，也不替老板宣布审美通过**；桌面侧栏、窄屏全屏、HUD 键盘 / 点按交互继续作为原型，等老板看实际预览。
- **中文输入法没有被验证。** 自动化只测了“组合输入期间不重绘”这一条内部行为，这不等于输入法可用。请在真实浏览器（至少 Safari / Chrome on macOS）人工测：在「职责」文本框用拼音输入——组合中不丢字、不跳光标；用回车确认候选词时不触发提交；确认后「预览改动」按钮与限制提示随之更新；组合中切换窗口再回来不丢草稿。

## 11. 取舍（我自己定的，不满意可改）

- 不用框架与构建：原生 ES module，视图是纯函数，测试直接在 Node 里跑；换框架时 `contract / adapters / flows` 原样保留。
- 岗位详情停靠在右侧而不是弹窗；窄屏退化为全屏并锁定背景。
- “能力”弹窗里的场景切换器是评审用的，只有 mock 适配器才出现。
- 「秘书 / 会议 / 技能与插件 / 跨 Office」只在能力表里登记为“后续”，没有做任何假界面。
- 没做：原生打包、桌面壳选型、Last10m、美元费用仪表。
