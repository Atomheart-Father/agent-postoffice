# BOXZ OFFICE 独立客户端（第一纵切）

> 状态：**原型 + 样例数据**。本批没有任何一项“真实接线已完成”。云端写入不等于本地已接通。
> 分支：`claude/office-client`（基于 `master` 的 `1ab0f54`，v1.14.1）。没有改动 `panel/index.html`，旧 Panel 原样可用。

## 1. 我读了什么（核对结论）

- 仓库 `Atomheart-Father/agent-postoffice`，生产 `master` = `1ab0f54`（含 `boxz-office-v0.0` 标签的 `0430973` 之后的 v1.14.1）。交接里的本地候选 `bbf3c8e` **不在 GitHub 上**，所以 `POST /api/role-diff`、`/api/role-apply` 在 `master` 的 `postoffice` 里不存在（`do_POST` 只有 `status / clear / archive-one / ack-one / send / edit-one / retract-one`）。
- 原前端：`panel/index.html`，单文件、无构建，我在 `4ceaa89` 做的组织树 + Harness 键墙 + 弹窗；之后 `master` 上有两次改动（`c076d47` 主题持久化、去掉“跟随系统”；`89757be` 逻辑地址升级提示）。本批的 `client/` 沿用它的美术语言和六套配色，**没有拷贝它的 JS**，也没有改它。
- 因此：能力表里“R0”一列只按 `master` 上真实存在的路由填写，本地候选版的新路由一律按“缺失（未发布）”处理，不猜载荷。

## 2. 怎么看

```
cd client && python3 -m http.server 8790      # 任何静态服务器都行，无构建、无依赖
# 浏览器打开 http://127.0.0.1:8790/
?scenario=default|empty|degraded|network|denied|missing|conflict|partial     # 样例场景（也可在「能力」弹窗里切换）
?live=1                                      # 样例世界每 6 秒推进一条事件（演示有界缓冲）
?backend=http&base=http://127.0.0.1:8765     # 切到 R0 HTTP 适配器（需要同源或后端放行 CORS，见 §7）
#/office/<role>[/threads|work|edit]   #/threads/<thread>   #/work?role=<role>      # 深链
```

测试（Node，无浏览器）：`node tests/client_contract_test.mjs`、`node tests/client_render_test.mjs`（共 228 条断言）。

## 3. 客户端怎么组织

```
client/
  index.html                入口（无构建，原生 ES module）
  styles/                   tokens.css（面板的 12 个颜色角色 × 6 套配色）· base.css（面板的树/连线/按钮）· office.css（新增）
  src/
    contract.js             数据契约：枚举都带 unknown/missing；数字是 number|null；normalizeSnapshot 从不猜
    capabilities.js         “能做什么”的唯一登记表：应用内「能力」弹窗和本文档的表都来自它
    adapters/               ★ 唯一的数据入口
      mock.js               样例后端（匿名夹具 + 8 种失败场景）
      http.js               R0 适配器：/api/state → 快照；其余明确返回 capability_missing
      index.js              按 URL 选择适配器
    fixtures/demo-company.js  匿名样例公司（Northwind Studio），不含任何真实数据
    flows/roleEdit.js       岗位编辑状态机（纯函数）：草稿→预览→确认→结果
    views/                  纯函数 (状态)→HTML 字符串：shell · office · role · roleEdit · threads · work · hud · dialogs · ui
    app.js                  控制器：一个状态对象 S、一个 render()、一张动作表；DOM 事件在 main.js 里翻译成 act()
    paint.js                重绘不丢位置：相同 HTML 不重绘、保留滚动、保留焦点与光标
    router.js · theme.js · i18n.js · strings.js · fx.js（连线与背景等高线）· host.js（桌面宿主接缝）· main.js
```

- **一个数据适配器**：视图只认 `getSnapshot / previewRoleChange / applyRoleChange / retryProjections / createThread / sendToThread / decideApproval / subscribe`。同一个页面可在 mock 与真实后端之间切换，没有第二份业务。所有返回都是 `{ok:true,data}` 或 `{ok:false,error:{code,message,retryable}}`，`code` ∈ `denied / stale / capability_missing / pending_approval / degraded / network / invalid`，界面每个 code 有一种诚实的呈现。
- **前端不存权威**：当前负责人、岗位、权限、可编辑与否都来自快照（`role.can.edit.{allowed,reason}`）；客户端只提交请求和显式确认，写前由后端重核。隐藏按钮不是权限系统：被拒绝的岗位照样能点进编辑页，看到后端给的原因，且没有表单。
- **能力透明**：快照里带每个功能的 `live/partial/mock/missing`；界面上所有 mock 区域都有「样例」标，未接线的字段有「原型」标；顶栏「能力」弹窗列出每项现状与接通所需。

## 4. 桌面宿主接缝（`src/host.js`）

客户端是纯 Web 应用。桌面壳（Tauri / Electron / 其他，本批不选）只需在页面脚本之前注入 `window.boxzHost`：

```
{ kind:"desktop", capabilities:{notify,openExternal,backend},
  notify(title, body), openExternal(url), backend() -> Promise<{baseUrl, headers?}> }
```
Web 层**不读本地文件、看不到 service.json 或凭据、不持久化 token**：连接本地后端的方式、地址与鉴权头由宿主给；浏览器里则退化为 URL 参数 + 无通知。不开放无鉴权的控制服务，也没有为了预览改后端的任何安全检查。

## 5. 本批可预览的纵切：岗位 → 线程 / 邮件

1. **公司入口（Office）**：树 = 公司。岗位座位用「标记 + 文字」呈现 现任 / 空缺 / 候选数，以及 已连接 · 工作中 · 省电 · 离线 · 未接通 · 状态未知 六种状态（不靠颜色）。红色只表示“有事等你”（待处理信 + 待批准）。
2. **岗位详情（右侧停靠，窄屏全屏）**：概览（现任与候选分开、职责、资源、资格[原型]）· 线程 · 邮件与工作 · 编辑。
3. **岗位编辑**：草稿 → 差异 → 确认 → 结果。冲突时不覆盖：显示上游变化，三个出口（在新版本上重新预览我的草稿 / 放弃重来 / 继续编辑）；部分投影失败不报成功，列出失败项并可重试；草稿在轮询、切换页面、失败后都保留；输入时不重绘（中文输入法安全）。
4. **线程**：列表 + 详情；每个线程明说“Office 现在能对它做什么”：可控制 / 仅观察 / 待批准 / 无权限 / 不支持 / 已过期。观察 ≠ 控制，只有后端确认可控的才出现输入框；发出的指令只显示“已受理”，不假装已读取。事件是有界缓冲，被丢弃的条数明示，完整历史留在 Harness。新建线程只是申请，结果是“待批准，线程尚未启动”。
5. **工作与邮件**：两列分开。邮件“已受理 / 已被取用”≠ 工作完成；工作用一套通用状态（进行中 / 已提交 / 已验证 / 已接受 / 已退修 / 已发布）。邮件指向未完成的工作时，逐行说明。顶部是“等你处理”（待批准，批准/拒绝由后端执行）。
6. **Token HUD**（悬停 / 键盘聚焦 / 点按都能开，Esc 关）：只显示官方累计的 input / output / cache / total、模型、更新时间、来源、覆盖；缺失显示“未知”而不是 0；来源没给合计就不自己相加；上下文占用单独一块，没有可靠值不显示。

## 6. 能力表（真实 / mock）

“本批客户端”列 = 样例数据能演示；“已发布 R0”列 = `master` 上真实后端能不能给。

| 功能 | 本批客户端（mock 适配器） | 已发布 R0（HTTP 适配器） | 接通需要后端提供 |
|---|---|---|---|
| 公司 / 岗位树与在线状态 | 样例 | 部分：R0：树来自 config 的 panel.organization，状态只有「在线」开关；没有 busy / 省电 / 未接通的区分。 | read_model.roles, read_model.presence |
| 岗位职责 / 资源 / 资格 | 样例 | 缺失：R0 的 /api/state 不含岗位职责与资源；每个信箱的 identity 有 memberships / active_roles，映射待接口包确认。 | read_model.roles[].duties, resources, qualification |
| 现任与候选的区分 | 样例 | 部分：R0 的 identity.active_roles / candidate_only_roles 与此对应，HTTP 适配器暂未映射。 | read_model.roles[].incumbent, candidates |
| 岗位编辑：草稿 → 差异 → 确认 | 样例 | 缺失：职责 / 资源的 preview / apply 在本地候选版有路由，未发布到 GitHub，载荷不在交接包内。资格 / 策略字段尚未接线（原型）。 | POST role-diff / role-apply payload + error samples; qualification and policy fields |
| 线程列表与归属岗位 | 样例 | 缺失：R0 没有会话列表接口。 | read_model.threads |
| 线程文本 / 工具事件（有界缓冲） | 样例 | 缺失：客户端只保留最近 N 条，可从快照恢复；不复制 Harness 的完整历史。 | event stream or cursor API, snapshot resume |
| 线程控制（观察 ≠ 控制） | 样例 | 缺失：只有后端确认的 Office 绑定可输入。创建 / 发送在 mock 里只返回样例结果，没有连接任何 Harness。 | thread create / send API, control state, approval flow |
| 邮件收执状态 | 样例 | 部分：R0：pending[] 映射为「已受理」，失败映射为「失败」；没有「已消费」与发件箱以外的关联。 | read_model.mail with delivery + threadId / workId |
| 工作提交 / 验证 / 接受 / 退修 / 发布 | 样例 | 缺失：一套通用状态，不是某个行业专用的状态机。后端未接线。 | work module read model + transitions |
| 待批准事项 | 样例 | 缺失：批准决定由后端执行。 | approvals read model + decide API |
| Token HUD（官方累计用量） | 样例 | 缺失：用量账模块已验收，但真实 reader / HUD 接口未交付；缺失值显示「未知」，不当作 0。 | usage reader API: input / output / cache / total / model / updatedAt / source / coverage; context occupancy |
| 实时推送与快照恢复 | 样例 | 缺失：R0 为 5 秒轮询。 | stream with resume token |
| 秘书（受控工具 / 批准条件） | 后续 | 后续：后续方向，等后端能力包。 | capability pack |
| 会议 | 后续 | 后续：后续方向。 | capability pack |
| 技能与插件 | 后续 | 后续：后续方向。 | capability pack |
| 跨 Office | 后续 | 后续：后续方向。 | capability pack |


## 7. 缺失的后端字段与交互取舍（请本地 DESIGN / Data / Interface 回答）

**读模型（`office-readmodel/0-draft`，见 `client/src/contract.js`；以本地接口包为准，字段名会改）**
1. `roles[]`：`id / parentId / kind(team|position) / title / summary / duties[] / resources[{kind,label}] / qualification{level,note} / rev / can.edit{allowed,reason}`；`incumbent` 与 `candidates[]`（`presence` 六值 + `presenceAt`，presence 必须有“未知”）。
2. `threads[]`：`roleId / harness / model / source(office|observed) / control(六值+reason) / status / events[]（带 seq，可从快照恢复）/ bufferLimit / droppedBefore / usage`。
3. `mail[]`：要有 `workId`、`threadId`，以及发件/收件的**岗位 id**（现在只有显示名，没法画“信在岗位之间飞”的连线）；`delivery` 要区分 已受理 / 已被取用。
4. `work[]`：通用状态 + `history[]`；`difficulty` 与岗位 `qualification` 是两条独立的轴。
5. `approvals[]` 与 `decide` 接口；`usage`（公司、岗位、线程三级）与 `context` 占用；快照级 `consistency / degraded[] / asOf / rev`；流式推送必须能从快照 + 游标恢复。
6. 身份与鉴权：客户端以谁的身份请求？`can.*` 的判定主体是谁？目前**没有**任何 token 方案，HTTP 适配器不带凭据。

**写接口**
7. `role-diff / role-apply`：载荷、错误码样例、冲突时是否回 `current`、部分投影失败的返回形态（本批 mock 用 `applied|partial + projections[]`、`stale + current{rev,…}`，如果不一致请告诉我）。
8. **并发编辑**：现在是“整字段覆盖 + 修订号”，上游新增的一条职责会在重新预览里显示为“将被移除”。更稳的做法是后端接受**操作式补丁**（加一条/删一条职责）并由后端合并，这需要接口支持。
9. 线程创建 / 发送 / 绑定：请求体、`pending_approval` 的返回、批准后线程如何出现在快照里。
10. 浏览器直连本地后端要不要开 CORS、是否只走桌面宿主代理。

**取舍（我自己定的，不满意可改）**
- 不用框架与构建：原生 ES module，视图是纯函数，测试直接在 Node 里跑；换框架时，`contract / adapters / flows` 原样保留。
- 岗位详情停靠在右侧而不是弹窗：ERP 里“看树的同时看详情”比弹窗更顺手；窄屏退化为全屏并锁定背景。
- “能力”弹窗里的场景切换器是评审用的，只有 mock 适配器才出现。
- 「秘书 / 会议 / 技能与插件 / 跨 Office」只在能力表里登记为“后续”，没有做任何假界面。
- 没做：Last10m、美元费用仪表、原生打包、桌面壳选型、移动端专用布局之外的平板特化。
