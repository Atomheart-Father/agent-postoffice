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
node tests/client_requests_test.mjs             # 发信去重键 / 请求状态 / 有界保留 / 刷新恢复与存储隐私
node tests/client_writer_test.mjs               # 单写者（两标签页 / 关闭与崩溃接管 / 操作者切换 / 旧摘要升级 / 不支持锁）
node tests/client_master_integration_test.mjs   # 起一个真实 master 后端做邮件读写端到端
```

## 3. 客户端怎么组织

```
client/
  index.html                入口（无构建，原生 ES module；页面里没有内联脚本，也没有 data: 链接，兼容 script-src 'self'）
  styles/                   tokens.css · base.css · office.css（字体用系统字体栈，不带字体文件，宿主不需要 woff2 的 MIME）
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
    flows/writes.js         请求记录：一次操作一条（pending / acknowledged / rejected / unknown + dismissed 标记）、client_request_id、未决记录不自动删除、满 30 条暂停写入、存储摘要的纯函数
    ledgerStore.js          请求摘要的 localStorage 存取与 v1→v2 升级（读回核对后才删旧条目）、锁名（独立于版本）、地址规范化
    flows/writer.js         单写者：Web Locks 锁的获取 / 排队接管 / 释放；测试用的内存锁（可模拟标签页崩溃）
    flows/mailbox.js        上下线与整箱清理的状态机（纯函数）
    views/                  纯函数 (状态)→HTML 字符串
    app.js · paint.js · router.js · theme.js · theme-boot.js（首帧前设置主题的外部经典脚本，代替原来的内联脚本）· i18n.js · strings.js · fx.js · host.js · main.js
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

### 5.2 发信去重键与请求记录（不自动重试，丢回复 = 可能已发出）

**发信去重键 `client_request_id`**

- 用户每次明确点「发送」，在请求发出前生成一个 `crypto.randomUUID()`（没有就用 `getRandomValues` 拼 UUID v4；两者都没有则**不带键**，绝不用 `Math.random`），放进请求体：`{from,to,subject,need,body,client_request_id}`。这个键**不是**界面上显示的本地编号 `w-…`。
- 同一次操作只有一个键：双击只发出一个请求（发送中按钮不可用）；适配器原样传递调用方给的键，从不自造、从不替换；没有任何自动重试。**用户明确再发一次（哪怕内容完全相同，或改了草稿再发）就是新操作、新键**，原来的 unknown 记录保留，不被覆盖，客户端也不按「内容 + 时间窗」合并信件。
- **客户端不声称“已具备去重保护”。** `SEND_DEDUPE.contract` 现在是 `null`：在更新后的固定版本合同（能力标记 `features.send_request_id`、保留窗口、同键异参数的错误响应）和 fixture 到达并被本地确认之前，即使后端在 `/api/state` 里带了这个标记，客户端也不开放「同键重试」，**不新增任何“安全重试”按钮**。unknown 的发信只提供：看「我发出的」/ 刷新状态 / 明确关闭提示 / 「作为新信发送」（文案明确写着这是一封新的、独立的信，可能与上一封重复）。
- 旧后端（master v1.14.1）会忽略这个字段：集成测试里同一个键发两次，真实 master 确实归了**两封**信。所以带键不等于去重。
- 后端回复 `deduped:true` 时，界面显示“后端认出这是之前发送的重复请求，没有创建另一封信”和原信引用，不当作又一次发送结果。同键异参数的冲突（具体响应形状等合同）：不自动换键、不自动重发，保留原操作与草稿，显示后端原话。

**请求记录（一次客户端操作一条，不是公司的正式工作账，也不是审计记录）**

| 状态 | 含义 |
|---|---|
| pending 待确认 | 请求已发出，还没有可靠结果 |
| acknowledged 后端已确认 | 后端在可读的回复里明确说了这次操作的结果；发信的确认只说明它收下了，不说明对方读过、更不说明工作完成 |
| rejected 后端拒绝 | 后端明确拒绝，**且回复本身能证明没有执行**（见下）|
| unknown 结果未知 | 超时、断连、读不懂的回复，或证明不了“没执行”的错误：**可能已执行** |

- **HTTP 错误码本身不能证明“未执行”**（代理的 502 可能发生在后端已保存之后）。只有两种回复算“拒绝”：后端自己的拒绝形状（4xx、JSON、`ok:false`；发信只认 `bad_request` / `not_found` / `refused`，`conflict` 等在合同确定前一律 unknown）和合同明写的 `saved:false`（role-apply，任何状态码）。5xx、HTML、空 / 读不懂的响应体、`200 + ok:false`、其它形状的 401/403（`{"ok":false}` 的 403 保护除外）都是 unknown。role-apply 的 409 是基线冲突，不是首次响应的恢复；首次响应丢失后出现 409 也不能断言第一次没有保存。
- **关闭提示**（`dismissed`）只是“我不再关注这条提示”，是单独的标记：结果状态保持原样，不会把 unknown 改成成功。

**有界保留**：自动清理只针对“已结束”的记录。**pending / unknown（未关闭提示）永不被自动删除**——写操作时、读入存储时、旧存储升级时都一样。记录总数超过 30 时，新的写操作到来才会清走“已确认 / 已拒绝 / 用户已关闭提示”的记录，最旧的先走，直到腾出位置。**未关闭的未决记录达到 30 条，所有新增业务写入暂停**（发信、改、撤、收执、归档、上下线、整箱清理、岗位保存都在发出之前被拒，界面写明原因，顶栏显示“请求（已满）”）；读取、刷新、核对、翻页和关闭提示仍可用，摘要原样保留。存储里（手工改过、或旧版本留下）若有超过 30 条未决，全部载入，不截断，请求窗口**分页**（每页 10 条）供逐条核对；用户明确关闭足够多的提示（未关闭的未决记录降到 30 条以下）后写操作恢复。服务端去重窗口过期只表示不能再依赖同键重试，不表示失败。

**刷新恢复与隐私**

- 只在 `localStorage` 保存最小摘要：`local_id、client_request_id（仅发信）、operation、started_at、result_state、dismissed、last_checked_at`。**不保存**信件正文、主题、岗位草稿、收件人、完整请求体、凭据，**也不保存后端返回的信件引用（`result_ref`，也不用信箱名替代）**：真实 master 的信件 id 里带有「发件人 + 主题的缩写」，存下来就等于间接存了主题。当前页面里仍保留完整引用用于导航（刷新后没有，这是有意的）。
- **旧存储升级**（`boxz.office.requests.v1` → `…v2`）：上一版写过 `result_ref`。升级由写入页在接管时做：读出旧记录（**不读** `result_ref`），把 v2 里没有的 v1 记录并入 v2 的列表（同一条记录两边都有时按 v2 的），**不截断、不丢未决记录**（pending 变 unknown、unknown、关闭提示标记、请求键全部保留）；先写 v2，**读回并逐条核对**，全部在场之后才删除 v1 条目。任何一步失败（存储满、写不进、读回对不上）都**不删除旧存储**，旧记录继续显示，请求窗口提示“旧记录还在旧的存储位置”，下次保存成功时再转存。读不出来的旧条目（损坏、别的版本、别的范围）原样留着不动。只读页不改写存储。
- **同一后端地址 + 操作者只允许一个可写标签页**（Web Locks 独占锁）。**锁名独立于客户端版本和存储版本**：`boxz.office.writer|<规范化后端地址>|<操作者>`，规范化指主机名大小写、默认端口、末尾斜杠、查询和片段不影响，同一个地址的不同写法是同一把锁。上一版（`52db642`）用的锁名是 `boxz.office.writer.v2|…`，现在的页面**同时持有这两把锁**（固定顺序：先新名后旧名，两把都拿到才算写入页），所以仍开着的上一版页面确实被挡在外面（它拿不到自己的旧锁名，成为只读页），反过来旧页面先写时新页面也只读，等旧页面关闭后接管；谁先谁后任意，同一时刻只有一个写入页。**更早的、还没有这套锁的版本无法被限制**（它们根本不申请锁），需要关闭这些旧页面。以后的版本沿用同一个新锁名，不再随存储版本换名；旧锁名在确认旧页面都已不存在后再从 `LEGACY_LOCK_PREFIXES` 里去掉。**保证范围**：Web Locks 只在同一来源、同一浏览器存储分区（同一浏览器配置）内有效，**不跨浏览器、浏览器配置、隐私窗口或设备**，那些地方也不同步这份记录，页面里不声称更大的范围。其余标签页只读：不能发信、改、撤、收执、归档、上下线、整箱清理、保存岗位，也不能关闭提示或写存储，页面顶部和请求列表都写明原因；读取、刷新、核对和翻页仍可用。持有者关闭、跳走或崩溃时浏览器自动释放锁，排队的标签页自动接管：**接管时重新读取存储里的摘要（不信任自己之前读到的），不发送任何请求**，旧持有者没来得及收到回复的 pending 记录显示为 unknown。只读页通过 `storage` 事件跟随持有者的更新。操作者在页面里切换时，先释放旧作用域的锁，再为新作用域申请。存储只由持有者整份写入：没有合并，也没有用 localStorage 读改写或超时租约冒充互斥，所以旧页面状态不可能覆盖已确认的结果。
- **兼容性限制**：没有 Web Locks（浏览器太旧，或页面不是 https / localhost 这样的安全上下文）时无法保证单写者，**这里直接关闭写操作**并说明原因，不静默放行；读取、刷新和核对仍可用。
- 存储按（后端地址、操作者）分开：键、载荷内的 scope 都带两者，载荷里 scope 不符、版本不符、不是 JSON、记录不合格的一律不使用（并提示）；操作者在页面里切换时列表跟着换，互不可见。存储版本在键里（`boxz.office.requests.v2`；锁名不带版本）。
- 刷新后：旧 pending 变 unknown；**不自动发送任何恢复记录**；不从已发信里“找相似内容”判定成功；没有保存请求体，所以刷新后的未决发送**只能核对，不能重试**。
- 存储缺失 / 被禁止 / 满 / 读不回时页面正常工作，请求列表提示“未决记录仅在当前页面保留，刷新后无法恢复”。
- **发信键与未来的正式去重通道**：现在没有任何去重承诺（`SEND_KEY.required = false`），所以浏览器生成不了键时这封信仍会发出，但记录里标“没有请求标识，没有重复保护”，不声称有保护。正式去重合同固定后，同一次提交必须把 `SEND_KEY.required` 改成 `true`：此后生成不了键的发信**在发出之前被拒**，不会静默无键发送。
- **浏览器或代理（keep-alive）自动重送已发出但回复丢失的 POST，JS 看不到也拦不住**；去重是否真的在承载链上生效，必须由本地用真实浏览器链路验证。客户端测试里用 fake 数一数调用次数**不能**说明这个问题已解决。

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

- 发信键 / 请求记录 / 刷新恢复在 Chromium 对真实 master 跑过：每次发信带自己的 UUID、存储里没有主题、刷新不发请求、丢回复后刷新变 unknown 且无重试、关闭提示不改状态；**这些只验证客户端行为，不验证服务端去重**。两个真实标签页的单写者（后者只读、不发 POST；关闭或崩溃后接管并重读摘要）也在 Chromium 里跑过。
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
