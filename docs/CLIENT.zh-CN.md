# BOXZ OFFICE 独立客户端（1.0 真实读写批 + 上下线 / 整箱清理 / 写入台账）

> 状态：**默认就是真实后端适配器**。邮件读写、上下线（status）、整箱清理（clear）已对真实 master v1.14.1 实际跑通（Node 集成测试 + 浏览器）；岗位 role-diff / role-apply **只对合同包里的匿名 fixture 核对过**，本地候选 `e819f0e` 没有部署，客户端从没碰过它。没有原生打包。Node 测试与截图不等于视觉验收，也不等于输入法验收。
> 分支：`claude/office-client`（基于 `master` 的 `1ab0f54`，v1.14.1）。没有改动 `panel/index.html`，旧 Panel 原样可用。
> **使用的后端合同：** `Atomheart-Father/agent-postoffice@9cd53d5ea47c3dfc37b7ae7d402705762ca02803` 的 `docs/cloud-client-api-e819/`（入口 `CLOUD_CLIENT_API_PACK.md`，校验 `SHA256SUMS.txt`）。客户端只按这份包与真实 master 的 `/api/state` 推接口，不读 master 去推 role-diff / role-apply（master 没有）。

## 1. 三个来源，分开看

| 来源 | 是什么 | 本客户端怎么对待 |
|---|---|---|
| **GitHub master v1.14.1**（`1ab0f54`） | 已发布的后端。`do_POST` 只有 `status / clear / archive-one / ack-one / send / edit-one / retract-one`，没有 `role-diff / role-apply`，`/api/state` 没有 `role_cards` | **真跑过。** 状态读、读信、已发信读、发信、收执、归档、改信、撤回、上下线（单个 / 多个 / 按组）、整箱清理都对它实际执行过；角色操作一律 `capability_missing` |
| **本地候选 `e819f0e`** | **不在 GitHub master 上，未部署。** 有 `POST /api/role-diff`、`/api/role-apply`，`/api/state` 另有 `role_cards`、`aliases` | 按合同包 9cd53d5 的匿名 fixture 做了映射与整套错误路径；**没有对运行中的候选跑过**。能力表里最高只标「已接线·仅 fixture 核对」，不标「已接通」 |
| **客户端 mock** | 样例数据（匿名公司 Northwind Studio） | 只在 `?backend=mock` 显式打开；每个用到它的区域都标「样例·未接线」，不再是默认 |

客户端用 `Array.isArray(state.role_cards)` 判断拿到的是候选还是 master（没有版本旗标）。适配器记住最近一次快照的来源，来源是 master 时拒绝发 role-diff / role-apply。

fixture 来源与校验（`tests/fixtures/office-e819/SOURCE.txt`，由 `tests/client_fixture_test.mjs` 检查 sha256）：

```
fea52b89a6740828a26182b9d2e4e6743e8c884927af4cb1dee8c15217b156a9  http_cases.json
9e60a4b342bf79fcf95fdd36786b3e6c07a43418f38468d384609ad5184e67b2  letter_ok.json
e9f3387b58a85f928d9c6dcd57dad37c414635c2e91463733d4ae5128f156698  outbox_ok.json
fdd124362e84d6ee4e39f85fec8ee5ff23306d0839f392b0ea0b5e0f022dbb5a  role_diff_ok_response.json
9ebfed73158acc54db0dbc9b4aa205d569da21a1306181023b87ef2e92f87ded  state_ok.json
```

## 2. 怎么看

```
# 真实后端（默认）：页面必须由后端自己的源提供，见 §4
POSTOFFICE_PANEL_BASE_URL=http://127.0.0.1:<端口> ...   # 精确的可信源；客户端静态资源要和后端同源承载

# 仅样例（显式开关，标「未接线」）：
cd client && python3 -m http.server 8790      # 无构建、无依赖
# 浏览器打开 http://127.0.0.1:8790/?backend=mock
?scenario=default|empty|degraded|network|denied|missing|conflict|projection   # mock 场景（也可在「能力」弹窗里切换）
?live=1                                      # mock 世界每 6 秒推进一条事件
#/office/<role>[/threads|work|edit]   #/threads/<thread>   #/work?role=<role>      # 深链
```

不带 `?backend=mock` 又不同源时，页面会读到 403 或网络错误，并如实显示出来，不会悄悄换成样例数据。

测试（Node，无浏览器）：

```
node tests/client_contract_test.mjs
node tests/client_render_test.mjs
node tests/client_candidate_render_test.mjs
node tests/client_fixture_test.mjs              # 合同包 fixture + 错误路径
node tests/client_ops_test.mjs                  # 上下线 / 整箱清理 / 写入台账 / 英文默认与原词
node tests/client_master_integration_test.mjs   # 起一个真实 master 后端做邮件读写端到端
```

## 3. 客户端怎么组织

```
client/
  index.html                入口（无构建，原生 ES module）
  styles/                   tokens.css · base.css · office.css
  src/
    contract.js             数据契约：枚举都带 unknown；数字是 number|null；normalize* 从不猜
    capabilities.js         “能做什么”的唯一登记表（live/fixture/partial/mock/missing…），应用内「能力」弹窗与本文 §8 都从它生成
    adapters/               ★ 唯一的数据入口
      http.js               真实适配器：master 与候选两种 /api/state 的映射、邮件读写、role-diff / role-apply、403 解释
      mock.js               显式开关的样例后端（8 种场景）
      index.js              默认 http；?backend=mock；?base=
    fixtures/demo-company.js  匿名样例公司
    flows/roleEdit.js       岗位编辑状态机与请求构造（纯函数）
    flows/mail.js           邮件写入流（发信 / 回复 / 收执 / 归档 / 改 / 撤）
    flows/writes.js         写入台账：每个写请求一个一次性本地 request 标识与阶段（待确认 / 已受理 / 已拒绝 / 未知 / 已核对）
    flows/mailbox.js        上下线与整箱清理的状态机（纯函数）
    views/                  纯函数 (状态)→HTML 字符串
    app.js · paint.js · router.js · theme.js · i18n.js · strings.js · fx.js · host.js · main.js
```

- **一个数据适配器**：视图只认 `getSnapshot / readLetter / listOutbox / sendMail / ackMail / archiveMail / editMail / retractMail / previewRoleChange / applyRoleChange / …`。返回 `{ok:true,data}` 或 `{ok:false,error:{code,message,retryable,…}}`；`outcomeUnknown:true` 表示写入的回复丢了，**绝不自动重发**。
- **前端不存权威**：岗位、权限、可编辑与否都来自后端；写前由后端重核。隐藏按钮不是权限系统。
- **缺能力不可操作**：Work、审批、HUD、线程控制 / 线程列表、秘书在真实后端下显示「能力缺失」，没有按钮，也没有 mock 冒充。

## 4. 鉴权、同源与桌面宿主

- 后端的 Host / Origin / Content-Type 精确保护是**本地防跨站保护，不是老板 / 秘书身份认证，也不是角色权限系统**。当前没有 token，也没有 `can.*`：客户端不猜授权，不在前端造权限字段。
- **403 的解释**：按合同包，`403 {"ok":false}` 表示 Host / Origin / Content-Type 保护拒绝（GET 也会）。客户端显示一句说明：页面必须由后端自己的源（或 `POSTOFFICE_PANEL_BASE_URL` 设定的那一个精确源）提供，POST 必须是 `application/json`，没有 token 可加。**其它形状的 403 原样显示后端的话，不套用这句解释。**
- 后端不发任何 CORS 头：`:8790` 的页面直连 `:8765` 是跨源，不能靠删掉保护来接通。可行的方式只有**同源承载**：让后端与 `client/` 静态资源出自同一个源（同端口同进程或反向代理），并把该源设为 `POSTOFFICE_PANEL_BASE_URL`。**谁来承载 `/client/` 目前没有定义**（master 与候选的后端都不提供静态客户端），需要本地团队决定；我在浏览器验证里用一个临时的同源反向代理代替。
- 所有 POST 带 `Content-Type: application/json`；浏览器自动带同源 `Origin`。信件 id 一律 `encodeURIComponent` 后再放进 URL。
- **页面从不持有凭据。** 桌面宿主接缝仍是 `window.boxzHost.request(op, args)`（`src/host.js`，类型化操作名、不接受任意 URL / 文件路径 / header），本批不扩原生壳，也不改接缝。

## 5. 邮件：日常操作（只做后端真有的）

| 操作 | 端点 | 说明 |
|---|---|---|
| 读信 | `GET /api/letter?box=&id=` | 只读收件箱里的那一封，不写、不叫醒任何人；已归档的读不到 |
| 发信 / 回复 | `POST /api/send` | 发件人固定为后端配置的 operator；回复就是发信，可选「同时把原信归档」，回复已发出而归档失败时**只重试归档** |
| 收执 | `POST /api/ack-one` | 记一份回执并归档，只表示「看到了」，不表示办完；重复调用不产生第二份回执 |
| 归档 | `POST /api/archive-one` | 移到 done/，不发回执；同名已存在时后端拒绝 |
| 已发信 | `GET /api/outbox`、`POST /api/edit-one`、`POST /api/retract-one` | 后端给出「能不能改 / 撤」的判断，POST 时再核；通道已受理的信改不了也撤不了 |

呈现规则：**已写入信箱 / 已提醒 / 通道已受理 ≠ 对方读过 ≠ 工作完成**。邮件状态保留后端的说法（waiting / reminded / failed，认不出的显示「状态未知」并保留原词），`ride` 在计数里出现但合同包没说明含义，不并入任何状态。回复丢失（网络）= 结果未知，不重发。

### 5.1 上下线与整箱清理（合同 §C 第 7 项）

| 操作 | 端点 | 说明 |
|---|---|---|
| 上下线 | `POST /api/status` | 单个 `{name,status}`；按组 `{group,status}`（组名不带 `@`，客户端会去掉前导 `@`）；`status` 只能是 `online` / `offline`。**master 没有 `names` 批量：** 选多个时客户端按顺序逐个发单个请求，遇到第一个错误就停，并列出已改 / 未试 / 失败的；候选 `e819f0e` 的 `names` 批量只有 fixture。按钮状态**以服务端返回为准**：每次写完都用 `loadFresh()` 重读，对比“我要求的”与“服务端现在的” |
| 整箱清理 | `POST /api/clear` `{name}` | **把整个 inbox 收进 `archived/<时间>/`**，不是 `done/`。后端回复 `{ok:true,error:""}`，**没有数量**，所以影响封数来自点击前的快照，写完再重读，写“现在列出 N 封” |

- **整箱清理 ≠ 归档单封。** 归档单封（archive-one）把一封信移到 `done/`；整箱清理是岗位面板里独立的危险分区，弹窗里明确写「整箱」「N 封」，不是「归档」按钮，也不共用文案。两步：先看影响（不发任何请求），再勾选确认框后按钮才可点。第二步时如果快照里的封数变了，会提示。
- 未知的信箱 / 组：master 回 400，客户端显示后端的话；403 `{"ok":false}` 仍只解释为 Host/Origin/Content-Type 保护。
- 在 mock 下两项都是“缺失”，不冒充。

### 5.2 写入台账：不自动重试，丢回复 = 可能已发出

- 每个写请求（send / apply / edit / retract / ack / archive / status / clear）在发出前生成一个**一次性本地 request 标识**（`w-…`），进入台账，顶栏出现「待确认请求 N」。标识**只在本地**：后端没有幂等键，所以**不发送**，也不能靠它去重。
- 阶段：待确认（pending）→ 已受理 / 已拒绝 / **未知（unknown，可能已发出）** → 已核对。断网 / 超时 = unknown：界面写「可能已发出」，让用户先看已发信 / 状态再决定，**不静默重发**；适配器没有任何自动重试。
- **客户端看不见的一种重发：** 浏览器或代理（keep-alive）可能自动重送“已发出但回复丢失”的 POST，这在 JS 里既看不到也拦不住；真实后端是 HTTP/1.0 无 keep-alive，我这边只用 route.abort 测过 UI 状态，**不是真实断连测试**，由本地真实浏览器链路补。

### 5.3 邮件里的原词与未明字段

邮件状态 waiting / reminded / failed 保留后端原词（旁边给统一英文释义）。「回执 / 岗位告知」类的信不会单独叫醒收件人（后端 `reason:"receipts_wait"`），所以**不等于普通待投递**，客户端不把它们并入 waiting。`kind`（formal / notice / receipt / alarm 以合同包 §3.10 为准）、`status` 里的 `ride`、`confirmed` / `escalation_acceptor` / `escalation_target` / `binding` 的含义**待本地核完补 fixture 与人话标签**，在此之前直接显示原值或「未知」，不推断。`saved` / `pending` / `stale`、`projection` 同样同时显示统一英文释义与原词。

### 5.4 语言

对外默认**英文**（首次打开不看浏览器语言），中文是可选语言，选择会记住；视觉不变。后端状态词用统一英文释义，不把中文系统术语当唯一界面文字。用户自己写的信件正文 / 主题 / 岗位职责不翻译。邮局协议本身的英文化由本地做，客户端不改协议、不猜后端字段（`need` 的 回复 / 审核 / 仅告知 是协议词，按原样显示并加释义）。

## 6. 岗位：读模型与编辑（候选合同，fixture 核对）

**读模型**（`role_cards[]`，16 个键）：标题、现任 `active`、候选、职责、结构化资源（`{kind, ref, note, exists}`，**不显示资源的本机路径**）、升级岗位及其标题、`state` / `holder_state` 状态码、`config_ok`、状态文件新鲜度。**岗位 ≠ 信箱**：树里「岗位」按项目分组、「信箱」另列；现任的信箱事实按名字去 `boxes[]` 查，投递开关 / 绑定 / 活动 / 后端状态码并列显示，**不合成「已连接」**；`aliases[]` 说明某个名字解析到哪个信箱。缺值显示「缺失 / 未知」，不补 0、不补「空闲」；活动 `unknown` 是后端的真实状态，保持未知。

**请求**（匿名示意）

```json
{ "name": "demo.review", "candidates": ["reviewer_one", "reviewer_two"], "escalation_role": "demo.manager",
  "duties": ["核查提交与证据"], "resources": [{"kind": "doc", "ref": "review.md", "note": "审查指引"}] }
```

- `name` 是**逻辑岗位名**，不是信箱节点 id。`candidates` 必填、非空、无重复、**顺序有意义**。
- `duties` / `resources` / `escalation_role`：**键缺失 = 不改，显式 `[]` = 清空，给了新值 = 整张列表替换。** 客户端只在草稿真的改了该字段时才带这个键。
- 限制（表单即时提示，后端再校验，以后端为准）：职责 ≤ 5 条、每条 ≤ 80；资源 ≤ 8 条、`ref` ≤ 200、`note` ≤ 60。
- 请求里没有别的键：资格、策略、能力分配仍属设计，不往载荷里加任何字段。

**流程：草稿 → role-diff 预览 → 用户确认 → role-apply → 结果。** 预览与应用是两步，确认按钮在预览之后才出现。

- **diff 成功**：`{ok:true, diff:{name, change, expected, facts, baseline}}`。`change` 是 `*_from / *_to` 成对键（总是齐全），`expected` / `facts` 是对象；`note_key`（如 `roleNoteTiming`）合同包没有文案，**按原样显示代码**。`baseline` 对客户端不透明，原样带回 apply，没有就不能提交。
- **apply**：

| 结果 | HTTP | 客户端怎么呈现 |
|---|---|---|
| 已保存 | 200 `saved:true, projection: fresh \| pending \| not_applicable` | **“已保存”与“名片是否新鲜”分开。** `pending` = 已保存、名片待刷新：不是失败，不重发，只给「刷新状态」。**未知的 projection 值显示「已保存 + 投影状态未知」。** |
| 基线冲突 | 409 `stale:true, saved:false` | **没有 `current`。** 保住草稿，提示重新 role-diff 做新预览 |
| 校验 / 不存在 / 备份失败 | 400 / 404 / 500，`error` 为中文消息，`saved:false` | 显示后端的 error，并说明后端报告没有保存 |
| 403 `{"ok":false}` | 403 | 见 §4 |
| 没收到回复（超时 / 网络） | — | **结果未知**，不重发 apply。`/api/state` 里没有整份配置的 baseline，不能据它判断上次是否生效；只允许重新 role-diff 做新预览，**禁止据此自动再 apply**。 |

整份配置基线：别的岗位变了，这个岗位的预览同样会 409。master 没有这些端点，在 master 上岗位操作一律「能力缺失」。

## 7. master 映射：做了什么、拒绝做什么

`mapMasterState`（`src/adapters/http.js`）对真实的 `/api/state`：

- **树节点是信箱，不是岗位**（id `box:<name>`，`origin:"mailbox"`），不能当岗位编辑。
- **在线 ≠ 连接。** `online`（投递开关）、会话绑定、`activity`（working / idle，带 `since`）各自保留；只有新鲜的活动信号才决定状态，否则「状态未知」。
- 有效的树也会漏信箱：未被纳入的列在「未编入的信箱」下。
- 邮件只能归属信箱（后端没记录信是发给哪个岗位的），`roleId` 留空。
- 线程 / 工作 / 批准 / 用量：master 没有，快照里是空，不编造。

## 8. 能力表（由 `capabilities.js` 生成，测试保证与之一致）

状态：已接通（live，对真实后端跑过）/ 已接线·仅 fixture 核对 / 部分 / 样例·未接线（mock）/ 缺失（该来源不提供）/ 未核对 / 后续。

<!-- caps:begin -->
| 功能 | GitHub master v1.14.1 | 本地候选 e819f0e | 客户端 mock | 说明 | 接通需要 |
| --- | --- | --- | --- | --- | --- |
| 公司 / 岗位树与在线状态 | 部分 | 已接线·仅 fixture 核对 | 样例·未接线 | master：树来自 config 的 panel.organization，节点是信箱，不是岗位；树漏掉的信箱列在「未编入」下。候选：根下分「岗位」（来自 role_cards，按项目分组）和「信箱」两组，岗位不是信箱。 | read_model.roles, read_model.presence |
| 岗位职责 / 资源 / 资格 | 缺失 | 已接线·仅 fixture 核对 | 样例·未接线 | master 的 /api/state 不含岗位职责与资源。候选的 role_cards 给出标题、职责、结构化资源（含路径是否存在）、升级岗位及其标题、状态码、配置检查、状态文件新鲜度；客户端原样显示，不显示资源的本机路径。 | read_model.roles[].duties, resources, qualification |
| 现任与候选的区分 | 部分 | 已接线·仅 fixture 核对 | 样例·未接线 | master 只有每个信箱的事实：投递开关、会话绑定、最近活动。候选的岗位有 active（现任）与候选名单，现任的信箱事实按名字查，投递开关 / 绑定 / 活动 / 后端状态码并列显示，不合成「已连接」。活动「unknown」是后端的真实状态，保持未知，不当作空闲。 | read_model.roles[].incumbent, candidates |
| 岗位编辑：草稿 → 差异 → 确认 | 缺失 | 已接线·仅 fixture 核对 | 样例·未接线 | 候选有 POST /api/role-diff（只预览，不写）与 /api/role-apply（需要预览给出的基线，确认后才发）。键不存在 = 不改，[] = 清空，整份替换；基线不符 409（没有 current，需重新预览）；saved:true + projection:pending = 已保存、名片待刷新，不是失败，也不重发；回复丢失按「结果未知」处理，不自动重发，只允许重新预览。没有 token 与 can.*，是否允许由后端在写入时判定。资格 / 策略 / 能力分配不在写入合同里。 | a deployed candidate (e819f0e is not) to verify role-diff / role-apply against; same-origin hosting |
| 线程列表与归属岗位 | 缺失 | 缺失 | 样例·未接线 | master 与候选都没有会话列表接口。 | read_model.threads |
| 线程文本 / 工具事件（有界缓冲） | 缺失 | 缺失 | 样例·未接线 | 客户端只保留最近 N 条，可从快照恢复；不复制 Harness 的完整历史。 | event stream or cursor API, snapshot resume |
| 线程控制（观察 ≠ 控制） | 缺失 | 缺失 | 样例·未接线 | master 与候选都没有线程创建、发送、控制接口。真实后端下这些按钮不出现；只有 ?backend=mock 才能看到界面，且标「未接线」。 | thread create / send API, control state, approval flow |
| 邮件收执状态 | 部分 | 已接线·仅 fixture 核对 | 样例·未接线 | 待处理邮件保留后端状态：待提醒 / 已提醒 / 失败；候选还有计数里的 ride，契约包没有说明它的含义，不并入任何状态。后端不记录信是发给哪个岗位的，所以只按信箱归属。 | read_model.mail with delivery + threadId / workId |
| 查看信件 | 已接通 | 已接线·仅 fixture 核对 | 样例·未接线 | GET /api/letter：只读收件箱里的那一封，不写任何东西，也不叫醒任何人。已归档的信读不到。 |  |
| 发信与回复 | 已接通 | 已接线·仅 fixture 核对 | 样例·未接线 | POST /api/send：信写进收件人的信箱，发件人固定为后端配置的 operator。「已写入信箱」不等于对方收到、读过或办完。回复就是发信；可选「同时把原信归档」，回复发出后归档失败只重试归档，不会再发一次。 |  |
| 收执（回执） | 已接通 | 已接线·仅 fixture 核对 | 样例·未接线 | POST /api/ack-one：记一份回执、把原信归档，并在对方空闲时通知发信人。回执只表示「看到了」，不表示工作完成；重复调用不会产生第二份回执。 |  |
| 归档（处理完成） | 已接通 | 已接线·仅 fixture 核对 | 样例·未接线 | POST /api/archive-one：把这一封从收件箱移到 done/，不发回执。同名文件已存在时后端拒绝并什么都不动。 |  |
| 已发信：修改与撤回 | 已接通 | 已接线·仅 fixture 核对 | 样例·未接线 | GET /api/outbox 列出 operator 发出、仍在对方收件箱里的普通信，并给出后端对「还能不能改 / 撤」的判断；POST 时后端会重新检查，可能因竞争而拒绝。通道已受理的信改不了也撤不了。 |  |
| 上下线（投递开关） | 已接通 | 已接线·仅 fixture 核对 | 缺失 | POST /api/status：单个、批量（候选的 names；master 没有，客户端逐个发、遇错即停）或按组（组名不带 @）。回复只说「后端接受了」，开关到底是什么以重新读到的状态为准；按钮永远按服务端状态显示。开关不等于连接，也不是老板的权限。 |  |
| 整箱清理（不是归档单封） | 已接通 | 已接线·仅 fixture 核对 | 缺失 | POST /api/clear：把这个信箱 inbox 里的全部信收进 archived/<时间>/（留在磁盘上，不再投递）。和归档单封（archive-one → done/）是两回事：独立入口、写明「整箱」和封数、二次确认。回复不带封数，之后重新读状态。 |  |
| 工作提交 / 验证 / 接受 / 退修 / 发布 | 缺失 | 缺失 | 样例·未接线 | 一套通用状态，不是某个行业专用的状态机。后端未接线。 | work module read model + transitions |
| 待批准事项 | 缺失 | 缺失 | 样例·未接线 | 批准决定由后端执行。 | approvals read model + decide API |
| Token HUD（官方累计用量） | 缺失 | 缺失 | 样例·未接线 | 用量账模块已验收，但真实 reader / HUD 接口未交付；缺失值显示「未知」，不当作 0。 | usage reader API: input / output / cache / total / model / updatedAt / source / coverage; context occupancy |
| 实时推送与快照恢复 | 缺失 | 缺失 | 样例·未接线 | master 与候选都是读取 /api/state 的 5 秒轮询，没有推送。 | stream with resume token |
| 秘书（受控工具 / 批准条件） | 后续 | 后续 | 后续 | 后续方向，等后端能力包。 | capability pack |
| 会议 | 后续 | 后续 | 后续 | 后续方向。 | capability pack |
| 技能与插件 | 后续 | 后续 | 后续 | 后续方向。 | capability pack |
| 跨 Office | 后续 | 后续 | 后续 | 后续方向。 | capability pack |
<!-- caps:end -->

## 9. 还需要本地团队给什么

0. **`kind` / `ride` / `confirmed` / `escalation_acceptor` / `escalation_target` / `binding` / `roleNoteTiming`** 的真实含义与人话标签（客户端现在显示原值或「未知」）。
1. **部署候选 `e819f0e` 或给一个可连的匿名实例**，让 role-diff / role-apply 和 `role_cards` 对真实后端跑一次；目前只有合同包 fixture。
2. **同源承载方案**：谁来提供 `/client/` 静态文件，以及 `POSTOFFICE_PANEL_BASE_URL` 怎么设；不要为了预览开 CORS 或删保护。
3. **合同包没说明的值**：pending 邮件计数里的 `ride`、`role_cards[].kind` 的取值、`confirmed` 的语义、`escalation_acceptor` 与 `escalation_target` 的区别、`binding` 非 null 时的形状、`roleNoteTiming` 的文案；在得到回答前客户端按原样显示、不解释。
4. role-apply 的 `200 + projection:not_applicable` 现在不可达（合同包说明），已按字面处理。
5. Work、审批、HUD、线程列表 / 控制、秘书：后端没有接口，客户端显示「能力缺失」，**不由前端临时造一套**。
6. 浏览器对复用的 keep-alive 连接上掉线的 POST 会**静默重发**一次（我用 keep-alive 的 fixture 替身重现过：apply 被记了两次）。真实后端是 HTTP/1.0、无 keep-alive，我的验证中没有出现；若以后改成 keep-alive 或前面挂了代理，这点需要重新评估。**真实浏览器链路的断连测试由本地做**，客户端无法在 JS 里检测或阻止这种重发，只能靠台账让用户先核对。

## 10. 验证状态（如实）

**真正接通且对真实后端跑过（master v1.14.1，`1ab0f54`）：** `/api/state`、`/api/letter`、`/api/outbox` 读；`send`、`ack-one`、`archive-one`、`edit-one`、`retract-one`、`status`（单个 / 多个逐个 / 按组）、`clear`（整箱）写。证据：`tests/client_master_integration_test.mjs`（Node，起真实 `postoffice panel`）与浏览器端到端（同源反向代理，`POSTOFFICE_PANEL_BASE_URL` 指向代理源）。

**只对 fixture 核对（候选 e819f0e，未部署）：** `role_cards` 映射、role-diff、role-apply（fresh / pending / 未知 projection / 409 / 400 / 404 / 500 / 丢回复 / 403 保护）、`aliases`、`status` 的 `names` 批量。浏览器里用一个按合同包 fixture 回放的同源替身主机跑过整条编辑流，**这不是对候选的实测**。

**mock（`?backend=mock`，标「未接线」）：** 线程、线程控制、Work、审批、HUD、秘书。

**不支持 / 未接：** 上述各项在真实后端下显示「能力缺失」。

- 上下线 / 整箱清理 / 台账 / 中英文切换在 Chromium（zh-CN 区域设置）对真实 master 跑过：英文首次打开、批量与按组各发了预期的 POST、clear 只发 1 个 `/api/clear` 且没有 archive-one、丢回复后不自动重发（route.abort，只验证 UI 状态）。
- 键盘、窄屏、草稿保留：在 Chromium 里测过（Tab 顺序、Escape、窄屏全屏岗位页、刷新时不丢草稿、HUD 芯片 Tab 移出不丢焦点——这是一个原有的键盘陷阱，已修并加了回归测试）。**这不是视觉最终验收，也不替老板宣布审美通过。**
- **中文输入法没有被验证。** 自动化只用 CDP 的 `Input.imeSetComposition` 模拟了“组合输入期间不重绘”，这**不是**真实输入法测试。需要真机（macOS Safari + Chrome，Windows 微软拼音至少一个）按下面步骤：
  1. 打开客户端（真实后端）→ 写信：在正文里切到中文拼音输入法，输入 `nihao` 不选词，让候选条保持打开。
  2. 此时等待一次后台刷新（约 5 秒，或点「刷新」）——正文里的拼音预编辑不能丢、不能跳光标、候选条不能消失。
  3. 用数字键 / 空格选词，**按回车确认候选**：不能因此发送或提交，只是上屏。再写几个字，确认草稿完整；切到别的窗口再回来，草稿仍在。
  4. 在岗位编辑页的「职责」文本框重复 1–3，确认「预览改动」按钮和限制提示在上屏后才更新；单行字段（资源 `ref` / `note`、候选名）同样测。
  5. 在组合进行中用 Tab / 点击移走焦点，不应出现丢字。
  结果请回报给我：哪个系统 / 浏览器 / 输入法，哪一步失败。

## 11. 取舍（我自己定的，不满意可改）

- 不用框架与构建：原生 ES module，视图是纯函数，测试直接在 Node 里跑。
- 岗位详情停靠在右侧而不是弹窗；窄屏退化为全屏并锁定背景。
- 「能力」弹窗里的场景切换器只有 mock 适配器才出现。
- 「秘书 / 会议 / 技能与插件 / 跨 Office」只在能力表里登记为“后续”，没有任何假界面。
- 没做：原生打包、桌面壳选型、Last10m、美元费用仪表。
