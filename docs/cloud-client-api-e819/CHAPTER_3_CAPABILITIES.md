# 章 3 · 能力与「观察／控制」矩阵（Interface 线编制）

**编制**：@postoffice.interface（`postoffice_interface`）　**编制日**：2026-10-12　**复核日**：2026-10-13
**对象树**：本地候选 **`e819f0e877107b2ff77d52b621b71cd118df4618`**（2026-10-13 复核基线；
先前版本记的是 R0 候选 `bbf3c8e7167662f2f1b952a07b1541142925d94a`）
**适配器 blob**：`po_adapters/opencode_v2.py` = `dec3eab5ca92b08a6cec1e1c689d409f95c52635`
（与我线 `ticket/v2-adapter-1` 的冻结版本**逐字节相同**；并在 `e819f0e` 上再核一次：
`git rev-parse e819f0e:po_adapters/opencode_v2.py` = **`dec3eab5…`，与 `bbf3c8e` 相同**，
两版之间 `git diff bbf3c8e e819f0e -- po_adapters/` **零改动**——所以 §3.1–§3.8 的适配器结论继续成立，
`e819f0e` 上**重跑实测也已复现**（`probes/iface_e819_caps_raw.json`、`probes/iface_e819_caps2_raw.json`）。
`postoffice` 本体在同期大改（`git diff --stat bbf3c8e e819f0e -- postoffice` = 658+/205−），
所以本章凡引 `postoffice:行号` 的都在 §3.1 与 §3.9 起标注 `e819f0e` 的**新行号**。）

**编制原则（照票执行）**：只写**已实现且实测**的；目标设计接口一律标「未证／mock」；
全部在隔离 `temp HOME` + 匿名数据上实跑；每条注明「实测自 SHA／命令」；读码推断单列「推断」。
未碰 R0 树（结束时 `git status --porcelain` 0 行）、未 push、未读真实 config 或凭据、
零模型调用、**零真实 HTTP**。

配套样例：`fixtures/capabilities.json`（含全部返回形状的 JSON 原样）。

---

## 3.0 一句话给前端

**适配器只有六个动词、三种能力状态，没有任何「列出会话」的动词。**
读＝`detect` / `resolve` / `presence` / `admit.probe`；写＝`admit` / `identity.synthetic`。
`accepted` 只代表宿主受理，不代表对方读了；`unknown` 不是失败，也不是成功。

---

## 3.1 Harness 注册表（实测）

实测自 `bbf3c8e`：`postoffice:5996-6026` 的 `ADAPTERS` / `adapter_for()`。
**在 `e819f0e` 复核**（行号已移动）：`ADAPTERS` = `postoffice:6787`、`_load_adapter` = `:6791`、
`adapter_for` = `:6811`、缓存 = `:6816`；**表的内容一字未变**（`probes/iface_e819_caps_raw.json`
的 `ADAPTERS_registry` / `adapter_for` 两项，与旧版逐项相同）。

| harness | `adapter_for()` |
| --- | --- |
| `opencode` | 载入模块 |
| `claude` | `None` |
| `pi` | `None` |
| `OPENCODE`（大写） | `None` |
| `""`（空串） | `None` |

`ADAPTERS = {"opencode": "po_adapters.opencode_v2"}` —— **目前只注册了一个 harness。**

**前端规则**
- harness 名**大小写敏感**。
- 取到 `None` ＝ **unsupported**：这个 harness 没接适配器。**不要重试、不要当错误展示**，
  更不要假设「功能坏了」——它压根不在能力面内。

> **推断（读码）**：`_load_adapter` 在「文件不在」或 import 失败时也返回 `None`，
> 即这些路径同样归 unsupported，不升级成 unknown。
>
> **接缝现状（审计 IF-01）**：`adapter_for()` 目前**零调用点**，`harness_ref` 只能由 CLI
> `--harness-ref` 手工传入。也就是说，上面这张表**现在没有任何代码在自动使用**——
> 是约定，不是强制。S3 票要求 postman 必须经 `adapter_for(harness)` 取适配器、
> 不得直接 import 模块，否则这张表继续靠自觉。
>
> **`e819f0e` 复核**：`rg -c 'adapter_for\(' postoffice` = **1**，且那唯一一处就是定义行本身
> ⇒ **仍是零调用点**，IF-01 在 `e819f0e` 上**未改善**（`e819f0e:6811`）。

---

## 3.2 三种能力状态（`detect().contract_ok` 三种取值都实跑到）

实测自 `bbf3c8e`，命令：`python probe_caps.py > caps_raw.json`。

| `contract_ok` | 实测 `detail` | 含义 | 前端怎么处理 |
| --- | --- | --- | --- |
| `"yes"` | `null` | 契约自检通过（官方文档路由在 + `/api/info` 可读） | 可继续用其余动词 |
| `"no"` | `GET /api/info → 200 且含 pid、OpenAPI 缺路由：/api/session/{}/prompt` | 宿主不是预期的 V2 形状 | 报「此宿主不支持」，**不要重试** |
| `"unknown"` | `本 uid 没有可读的服务登记文件` | **无法证明**（服务没起／读不到登记／传输断过） | **不等于失败**；不要显示「对方不在线」 |

**`"no"` 与 `"unknown"` 的区别是本节最要紧的一条**：`no` 是**证明过不行**，
`unknown` 是**证明不了**。前端若把两者都渲染成红色错误，会在宿主只是没启动时给出错误结论。

---

## 3.3 逐动词能力矩阵（全部实测）

命令：`python probe_caps.py`（主路径 ＋ 计数）、`python probe_caps2.py`（归属粒度、busy、probe、
逐动词 POST 计数、两种本地 rejected）。

| 动词 | 读/写 | 实测 POST 增量 | `state` 实测取值 | `sent` 字段 |
| --- | --- | --- | --- | --- |
| `detect` | 读 | **0** | （无 `state`，用 `contract_ok`） | **不存在** |
| `resolve` | 读 | **0** | `exists` / `absent` | `false` |
| `presence` | 读 | **0** | `idle` / `busy` / `unknown` | `false` |
| `admit.probe` | 读 | **0** | `seen` / `unknown` | `false` |
| `admit` | **写** | **1** | `accepted` / `rejected` / `unknown` | `true`/`false`/**`null`** |
| `identity.synthetic` | **写** | **1** | `accepted` / `rejected` / `unknown` | `true`/`false`/`null` |

POST 计数是**假宿主独立判据**（`post_count()` / `prompt_requests` / `synthetic_requests`），
不是适配器自报——这是「观察≠控制」的可核对依据。

### 返回形状（原样，匿名）

```jsonc
// detect（注意：没有 state，也没有 sent）
{ "contract_ok": "yes", "harness": "opencode", "version": "2.0.26",
  "service": { "service_id": "svc-fake-1", "service_version": "2.0.26" },
  "caps": ["detect","resolve","presence","admit","admit.probe","identity.synthetic"],
  "detail": null }

// resolve · exists
{ "state": "exists", "title": "demo-1", "location": "/tmp/demo/prj",
  "service_id": "svc-fake-1", "sent": false }
// resolve · absent（官方依据 SessionNotFoundError）
{ "state": "absent", "sent": false }

// presence · idle / busy（形状相同，只有 state 变）
{ "state": "idle", "since": 1791832893.501195, "observed_at": 1791832893.501195,
  "observer": "svc-fake-1", "source": "official", "sent": false }
// presence · unknown（active 认不出身份；实测措辞里明写「≠ idle」）
{ "state": "unknown", "verb": "presence", "sent": false,
  "detail": "active 响应无法判定（active 条目里认不出会话身份（非空对象形状未钉版））；≠ idle" }
// presence · unknown（观察者变了）
{ "state": "unknown", "verb": "presence", "sent": false,
  "detail": "观察者不一致（记录=SVC-OTHER 当前=svc-fake-1）" }

// admit · accepted
{ "state": "accepted", "verb": "admit", "harness_ref": "msg_5ba3…",
  "service_id": "svc-fake-1", "sent": true }
// admit · rejected（两种都可在**零请求**下证明）
{ "state": "rejected", "verb": "admit", "class": "not_office_owned", "sent": false,
  "detail": "会话不由 Office 创建或登记：只读，不 admit" }
{ "state": "rejected", "verb": "admit", "class": "payload_mismatch", "sent": false,
  "harness_ref": "msg_5ba3…", "detail": "同 token 异载荷：拒绝发送" }
// admit · unknown（契约不符时零请求）
{ "state": "unknown", "verb": "admit", "sent": false,
  "detail": "宿主契约不符：GET /api/info → 200 且含 pid、OpenAPI 缺路由：/api/session/{}/prompt" }

// admit.probe · seen（**从不**返回 accepted）
{ "state": "seen", "verb": "admit.probe", "harness_ref": "msg_5ba3…",
  "path": "/api/session/ses_demo_0001/inbox", "proves": "admission_only",
  "consumed": false, "accepted": false, "sent": false }
// admit.probe · 未受理 —— 注意 state 仍是 unknown
{ "state": "unknown", "verb": "admit.probe", "sent": false,
  "detail": "not_seen（可能是取消、清理或换了服务；不推为 rejected）" }
// admit.probe · 事实不齐
{ "state": "unknown", "verb": "admit.probe", "reason": "missing_facts",
  "missing": ["binding_gen","key"], "sent": false,
  "detail": "首次尝试事实不齐，无法补证：缺 binding_gen、key" }

// identity.synthetic · accepted（写线程身份提示）
{ "state": "accepted", "verb": "identity.synthetic", "harness_ref": "msg_id_cdba…",
  "service_id": "svc-fake-1", "sent": true }
```

---

## 3.4 「观察 ≠ 控制」（逐条都有实测依据）

1. **`presence` 的 `idle` 的准确含义是「官方 active 快照里没有你」，不是「你没在忙」。**
   实测归属粒度：假宿主把 `active` 指向 `OTHER` 时，`presence(OTHER)→busy`、
   `presence(SES)→idle`、`presence(未登记)→idle`——该端点是**服务端级**的，不是会话级的。
   > **推断（依官方 schema，未实测）**：`session.active` 的 `data` 是**单个 object**。
   > 若真宿主允许多个并发活跃会话，则「正忙但没被这个对象报告」的会话会被说成 `idle`——
   > 保守方向的**错报**。真宿主票要钉这一条。
2. **`presence` 不校验会话是否存在**：实测**未登记**会话同样返回 `idle`。
   要判存在性必须**另调 `resolve`**。不要用 `idle` 推断「这个线程还在」。
3. **`presence` 的返回里不带被观测会话 id**，前端无法自证归属，只能信 `observer`。
4. **`admit` 的 `accepted` 只代表宿主受理**（first-admission-wins），不代表对方读了、回信了。
5. **`admit.probe` 的 `seen` 只证明「受理记录存在」**，返回里显式带
   `proves:"admission_only"`、`consumed:false`、`accepted:false`——**`probe` 从不返回 `accepted`**（实测）。
6. **读动词一律零 POST**（实测 `post_delta=0`）；只有 `admit`／`identity.synthetic` 各 1 次。
7. **写动词的 `rejected` 都可证「一个字节都没发」**：实测两种 class 下
   `post_total=0`。这是 `rejected` 与 `unknown` 的根本差别——`unknown` 时**可能已经发出去了**。
8. **`sent` 是三态，不是布尔**：`true`＝受理／`false`＝可靠地没发／**`null`＝已发出但结果不可知**。
   `null` 必须走 `probe` 补证，**不得当 `false` 自动重发**。

---

## 3.5 「线程列表」有哪些真事实（实测）

| 事实 | 结论 |
| --- | --- |
| 有没有「列出全部会话」的动词 | **没有**。`CAPS` 实测六项里无任何 list/sessions 项 |
| 能拿到什么 | 按**已知** sessionID 逐个 `resolve` → `exists`/`absent` ＋ `title` ＋ `location` ＋ `service_id` |
| 列表路由 `GET /api/session` | 存在，但**只被契约自检读过一次**，不对外暴露（实测 `request_log` 里有 `list`，能力面无出口） |

**对云端客户端的直接后果**：「岗位 → 线程列表」这一纵切**目前没有数据源**。
岗位与线程的对应关系不在适配器能力面内（属邮局侧 round/binding，不在本章范围）。
**前端不要靠轮询 `resolve` 拼列表**——那既慢、又拿不到枚举、还会在会话多时放大 `unknown`。

**缺口归属**：S3 票（postman 必须经 `adapter_for(harness)`）＋ 真宿主票。
若产品确实要「岗位→线程列表」，这是一个**尚未立项的能力缺口**，需要单独出票，
不是在客户端硬凑。

---

## 3.6 建议的前端展示词（避免把三态压成两态）

| 内部值 | 建议文案 | 不要写成 |
| --- | --- | --- |
| `contract_ok:"no"` / `adapter_for→None` | 未接入此 Harness | 「功能故障」 |
| `unknown`（契约/传输） | 暂无法确认（不会丢，也不会重发） | 「对方不在线」／「投递失败」 |
| `unknown`（观察者不一致） | 观察来源已变，请刷新 | 「掉线」 |
| `admit.accepted` | 已投递给该线程（对方尚未必看到） | 「已送达」 |
| `probe.seen` | 投递记录存在（不等于已读） | 「已读」 |
| `presence.idle` | 当前无活跃执行 | 「线程已关闭」 |
| `rejected / not_office_owned` | 该线程未由 Office 登记，只读 | 「权限错误，请联系管理员」 |
| `rejected / payload_mismatch` | 同一投递标识内容不一致，已阻止 | 「发送失败」 |

---

## 3.7 真宿主未证（**勿混报**）

以下全部**未在真宿主验证过**（零真实 HTTP）。凡上面标「实测」的形状都来自假宿主或官方文档；
假宿主未覆盖的形状见审计 IF-05／IF-07。

1. `session.active` 的**非空**对象形状未钉版（钉版只测到 `{"data":{}}`）。
   真宿主若有活跃会话而形状不同，`presence` 会一直 `unknown`。**真宿主票第一件事核这个。**
2. 多并发活跃会话下 `presence` 的错报风险（3.4 第 1 条，推断）。
3. `Session.Message.Info` 字段未钉版（适配器对 inbox 与 message-get 用同一套证据要求，
   假设不成立只会退成 `unknown`，不会误判 `seen`）。
4. `resume:true` ＋ `delivery:"queue"` 何时真被消费、`steer` 打断语义未验。
5. `service.json` 真实路径与 Basic 用户名 `opencode:<password>`（本轮**未读凭据**）。
6. 真 V2 契约自检、Panel 读模型、S3 postman 驱动、S4 typed binding、S5 切换（L1/LoadError）全未做。
7. OpenAPI 路由自检在真宿主上从未跑过（钉版宿主不暴露 spec，故 `contract_ok` 会是 `"no"`）。

**S2 假宿主通过 ≠ V2 可升级交付。**

---

## 3.8 给 DESIGN 的三点提醒

1. **章 5／章 6 的交互状态机请照 §3.2 的三态写**：`unknown` 需要独立的一整套前端状态
   （文案、是否可重试、是否显示告警），压成「成功/失败」会直接误导操作。
2. **章 6 的缺口清单建议加上「线程列表能力不存在」这一条**——它是「岗位→线程」纵切的硬前提，
   且不是客户端能补的。
3. **HF-3 与本章节无冲突**：IF-08 改的是 Core 与 V1 插件的信头分类，
   本章的六个动词走的是适配器层，两条线不相交。但若 HF 期间适配器 blob 有变，
   请在章 0 的「合同版本登记」里记一笔（当前对照值 `dec3eab5`）。
   **`e819f0e` 复核结果：适配器 blob 仍是 `dec3eab5…`（未变）**，故此处无需改章 0。

---

## 3.9 四个正交事实：online / binding / presence / control（实测自 `e819f0e`）

**这一节是 2026-10-13 复核新增，专门回答云端 client 5aacda7 的三条校准意见**（Codex 回复 L56–L58）：
这四件事**互不推出**，前端不得把它们折成一个 `connected`。

| 事实 | 唯一来源（`e819f0e` 行号） | 实测取值 | 准确含义 | 前端规则 |
| --- | --- | --- | --- | --- |
| **online**（投递资格） | `routes.json` per-box `status`；读 `online(route)` `:305-306`（**缺字段＝默认 online**）；写 `apply_status` `:3481-3492`（只收 `online`/`offline`，非法拒；`offline→online` 写 `online_since`）；`identity --json` → `status` `:3021-3022`；`/api/state` box 行 → `online` `:8343` | `online` / `offline` 两种**落盘值**；`unknown` 是派生（未登记时 `:3021`） | 「邮局会往这个信箱投信」 | `false` **不是**断开证据（可能是老板手动 `offline`）；`true` 也**不是**连接健康 |
| **binding**（通道绑定） | 同一 `routes.json` 的 `methods` ＋ 每通道字段（`_BINDING_FIELDS` `:2757-2758`：`codex_queue→thread_id`、`opencode_plugin→session_id`、`claude_hook→claude_session`；`notify`＝无人通道；`office_peer`＝O2O）；`identity --json` → `binding` `:3021` | 见下四例 | 「信靠什么通道到人」 | 与 online **独立**：`offline` 时 binding 照旧在 |
| **presence**（线程活跃） | **只在适配器**：`po_adapters/opencode_v2.py` 的 `presence` 动词（`CAPS` `:103`），取值 `idle`/`busy`/`unknown` | `postoffice` 侧**无任何 presence 字段**（实测 `rg` 零命中） | 「宿主会话在不在忙」 | 与投递资格、与 binding 健康都无关；`unknown` 不得显示成「空闲」 |
| **control**（实时控制） | **不存在**：`postoffice` 侧无 `control` 字段（只有注释 `# ---------- control panel ----------` `:7760`）；适配器能写的只有 `admit` / `identity.synthetic` | — | 没有打断/停止/重排/读回复 | 属未证/mock（见 §3.11），**不得用对白式 UI 假装** |

**匿名实样（隔离 `temp HOME`，命令 `probes/probe_iface_e819.sh`）**：

```jsonc
// alpha：人类、只弹通知；仍算 online（投递资格）但没有通道绑定
{ "box":"alpha", "registered":true, "binding":null, "status":"online", "method":"notify" }
// beta：绑定 OpenCode 会话
{ "box":"beta", "registered":true,
  "binding":{"kind":"opencode_plugin","session_id":"ses_anon_0001"},
  "status":"online", "method":"opencode_plugin" }
// beta 被手动下线后：status 变、binding/method 一字未动 —— 两事实正交的直接证据
{ "box":"beta", "status":"offline", "binding":{"kind":"opencode_plugin","session_id":"ses_anon_0001"},
  "method":"opencode_plugin" }
// gamma：绑定 Codex 线程
{ "box":"gamma", "binding":{"kind":"codex_queue","thread_id":"t_anon_1"},
  "status":"online", "method":"codex_queue" }
// delta：没有通道 ⇒ add 拒绝登记 ⇒ 未登记快照
{ "box":"delta", "registered":false, "binding":null, "status":"unknown", "method":"unknown" }
```

`list` 实测（同一次会话）：`信箱 App 状态 积压 说明` → `beta OpenCode 在线 0 [ses_anon_0001]`、`gamma Codex 在线 0 [t_anon_1]`、`alpha 手动 在线 0 []`。
（`add` 实测：**新登记信箱必须给通道**，否则拒绝——`delta` 就是这样被拒的。）

---

## 3.10 邮件各阶段：源状态与可得证据（**不补成成功**）

| 阶段 | 谁产生 | 可得证据（实测/代码） | 不得写成 |
| --- | --- | --- | --- |
| 落盘到信箱 | 发信方 `send` | 文件落在 `<box>/inbox/<编号>_<来源>_<事由>.md`（实测：写入后 `letter-kind` 能逐封读出） | 「已送达」 |
| 通道受理 | 插件/邮递员投出一轮 | `DELIVERED <box>/<file> → <sessionID>` 日志行；`opencode_delivered.jsonl` 台账；`.claims/` 认领 | 「对方看到了」 |
| 提醒展示 waiting→reminded | `wake-plan` 判定 ＋ `presented-update`（唯一写入口） | `wake-plan` 的 `deliver`/`hold_until`/`reason`；presented 记录 | 「已读」 |
| 消费 | 收件方会话真的处理过 | **没有可核对的消费回执**；适配器 `admit.probe` 的 `seen` 只证「受理记录存在」（`proves:"admission_only"`, `consumed:false`, `accepted:false`） | 「已完成」 |

`wake-plan` 实测自 `e819f0e`（stdin 传候选集，`postoffice:3982-4046`；返回键 `deliver`/`carry_receipts`/`hold_until`/`alarm_round`/`reason`，`:822-823`）：

```jsonc
{"deliver":["20261013-anon-0001_alice.md"],"carry_receipts":[],"hold_until":null,"alarm_round":false,"reason":"window_elapsed"}  // 到期正式信
{"deliver":[],"carry_receipts":[],"hold_until":1791862888.32,"alarm_round":false,"reason":"window"}                              // 未到期，在等时间
{"deliver":[],"carry_receipts":[],"hold_until":null,"alarm_round":false,"reason":"receipts_wait"}                                // 单独 notice，不唤醒
```

**`reason` 是源状态、不是结论**：客户端不得把 `deliver:[]` 一律渲染成「投递失败」。

`letter-kind` 实测自 `e819f0e`（`postoffice:3952-3979`，逐封 `{id,kind,urgent}`，只读、不认领、不落文件）：

```jsonc
[{"id":"…0002_route.md","kind":"notice","urgent":false},
 {"id":"…0003_bob.md","kind":"formal","urgent":true},
 {"id":"…0004_carol.md","kind":"receipt","urgent":false},
 {"id":"…0005_alarm.md","kind":"alarm","urgent":false},
 {"id":"…9999_missing.md","kind":"formal","urgent":false,"error":"missing"}]
```

唯一分类规则（`head_kind_and_urgent` `:733-766`）：`回执：`→receipt；`闹钟：`→alarm；`升级求助：`→formal+urgent；
`来源：postoffice` ＋ `切换事件：<id>` **且 id 在 `alias_state.json` 登记过**→notice（`known_switch_event_ids` `:709-730`）；
`优先级：紧急`→urgent；缺件给 `error:"missing"` 而不是猜。
（该判据正是 HF-3/IF-02 的收口点，与本线 `ticket/hf-3-plugin` 已修的夹具一致。）

**`capability_missing` / `unsupported` / `unknown` 的前端语义**（Codex 回复 L39/L56/L58/L61/L76。
注意：这三个词在 `postoffice` 输出里**都不是字段名**——`rg capability_missing postoffice` 零命中，属**客户端侧**概念）：

| 词 | 何时用 | 前端表现 |
| --- | --- | --- |
| `unsupported` | 宿主/harness 不在能力面（`adapter_for→None`、`contract_ok:"no"`） | 「未接入此 Harness」；**不重试**、不报故障 |
| `unknown` | 证明不了（无连接证据、观察者不一致、`session.active` 形状认不出） | 「暂无法确认」；**≠失败、≠成功**；不得折算成 online/connected |
| `capability_missing` | 后端根本没有这个能力（无 `retryProjections` 端点、无 token/`can.*` 授权、失败项无结构化列表） | 照实显示源状态（如 `projection:"pending"`），**不放会重复治理写入的假重试按钮** |

---

## 3.11 能力缺失清单（逐项标「未证／mock」；照 Codex 回复 L61）

| 能力 | 现状 | 依据 |
| --- | --- | --- |
| 线程列表（岗位→线程） | **不存在**（无 list 动词；`GET /api/session` 只被契约自检读过一次，能力面无出口） | §3.5；`probes/iface_e819_caps_raw.json` |
| 实时线程控制（打断/停止/重排/读回复） | **不存在**（只有 `admit`/`identity.synthetic` 两个写动词） | §3.9 末行；`po_adapters/opencode_v2.py:103` |
| 工作 / 审批（work / approvals） | **mock**，未证 | Codex 回复 L61 |
| usage HUD | **mock**，未证 | Codex 回复 L61 |
| 通用重试 / 投影重算端点（`retryProjections`） | **不存在** | Codex 回复 L39；`e819f0e` 未见此路由 |
| 角色 token / `can.*` 授权查询 | **不存在**（Host/Origin 白名单＝CSRF 防护，不是身份；无任意 CORS） | Codex 回复 §4（L67-71） |
| 失败项结构化列表 | **不齐**（不从 message 猜字段） | Codex 回复 L39 |
| `adapter_for()` 真正接线（IF-01/S3） | **未改善**（`e819f0e` 仍零调用点） | §3.1 复核 |
| activity 真值 | 隔离 HOME 下**产不出**：`activity` 只由 Claude Code hook 写（`cmd_activity` `:5196-5214`），解析不到已绑定会话时**静默 rc=0、什么都不写**（实测三种 payload 均未落文件）；字段形状见 `activity_write` `:395-412`（`state`/`since`/`observed`/`source`/`binding`），消费端 `activity_state` `:461-473` **30 分钟 TTL**、binding 不符→`unknown` | `probes/iface_e819_activity_attempt.txt` |

**本节实测命令与原始输出**：脚本 `probes/probe_iface_e819.sh`（隔离 `temp HOME` ＋ 匿名信箱
alpha/beta/gamma/delta ＋ 匿名 sqlite 会话；不读真实 config／凭据、零真实 HTTP、零模型调用）；
原始输出 `probes/iface_e819_*`（identity×4＋offline 一例、`list.txt`、`letter_kind*.json`、
`wake_plan_*.json`、`activity_attempt.txt`；适配器侧 `iface_e819_caps_raw.json`／`caps2_raw.json`）。
