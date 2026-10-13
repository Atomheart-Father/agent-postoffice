# 章 1 · HTTP 实测清单（Data 线）

**除 §4 单列的「推断」项（源码可见、隔离环境未构造出，不是真机 fixture）外，其余条目均实测。**基线：**R0 候选 `bbf3c8e` → 已按 DESIGN 2026-10-13 04:31 刷新为本地候选 `e819f0e`**（worktree `<local worktree>`；hf-7 reattach boundaries 线；Python 3.9.6）。方法：隔离 temp HOME＋匿名箱（panelop/box_a/box_b/box_unlisted/box_c）＋匿名 config，`POSTOFFICE_HOME=<temp> POSTOFFICE_NO_NOTIFY=1 POSTOFFICE_POLL=1 postoffice panel --no-open --port <p>` 起面板，`curl` 逐条实跑。与 bbf3c8e 的差异段以 **【e819 增量】** 标出；读码推断的条目只在 §4 及行内标「推断」，不当事实。

## 1. 总览

- 绑定 `127.0.0.1`  only（`ThreadingHTTPServer(("127.0.0.1", port))`，实测 `Server: BaseHTTP/0.6 Python/3.9.6`，HTTP/1.0）。端口冲突时自动关旧同型面板进程，否则退出换端口。
- **无任何鉴权**（无 token/cookie/账号）。安全边界三条【e819 增量：Host 检查为新增】：①**Host 头精确白名单** `127.0.0.1:<port>`/`localhost:<port>`（＋可选 `POSTOFFICE_PANEL_BASE_URL` 的一个精确 netloc）——DNS rebinding 把攻击者域名解析到 127.0.0.1 也过不了（Host 头仍是攻击者域名）；②POST 必须 `Content-Type: application/json`（否则 403）；③POST/GET 均须同源 Origin 或**无 Origin**（本地 CLI 可读）（见 §3）。
- 响应头一律 `Content-Type: application/json; charset=utf-8`、`Cache-Control: no-store`。
- 12 条 API 路由 = GET 3 + POST 9（另有 `/` 与 `/index.html` 返回 HTML 页面，不计入 12 条；未知 GET 路径返回裸 `{}`＋404）。

| 路由 | 方法 | 作用 | 幂等 |
|---|---|---|---|
| `/api/state` | GET | 全量只读投影（面板一页数据） | 读，幂等 |
| `/api/letter` | GET | 读单封信全文（query: box/id） | 读，幂等 |
| `/api/outbox` | GET | operator 已发且仍在他人 inbox 的信 | 读，幂等 |
| `/api/status` | POST | 单/批/组 上下线 | 幂等 |
| `/api/clear` | POST | 整箱收进 archived/ | 幂等 |
| `/api/archive-one` | POST | 单封 inbox→done | 幂等（moved→already） |
| `/api/ack-one` | POST | 单封回执 | 幂等（recorded→already） |
| `/api/send` | POST | 发普通信 | **不幂等**（每次新 id） |
| `/api/edit-one` | POST | 改未接受的信 | 不幂等（可重复改，但不是重复创建） |
| `/api/retract-one` | POST | 撤回未接受的信 | 一次性（重复 404） |
| `/api/role-diff` | POST | 岗位改动三列差异＋基线哈希 | 读，幂等 |
| `/api/role-apply` | POST | 提交岗位改动 | **不幂等**（二次提交 409 stale） |

## 2. 逐条实测（请求/响应/错误/状态码）

### GET /api/state
- 请求：无参数。响应 200，顶层键（实测全集）：`home, boxes[], broadcasts[], groups[], aliases[], switches[], role_cards[], config, config_error, panel, log[]`（字段细目见章 2）。
- 错误：无 4xx 路径——配置坏了也返回 200，用 `config_error`/`panel.error` 表达（实测：panel.organization=None 时 200＋`panel:{ok:false,error:"panel 配置：organization 必须是对象",operator:""}`）。
- 一致性：内部 `read_context(bounded=True)`。**实测持 commit_lock 时该路由 5.5ms 返回 200**（拿不到锁立即退回无锁读，绝不阻塞/失败）；此时 config 与 routes 可能来自不同写入窗口（混读风险，代码注释自认"锁只护同窗"）。业务上按最终一致读。

### GET /api/letter?box=&id=
- 参数必须各出现且仅一次；id 过 `letter_id_ok`（`bare_presented_id` 且不带 `.md`、无 `*?[]`）。
- 实测 200：`{"ok":true,"box","id","sender","subject","need","body"}`（**无 kind/status/preview 字段**——这是全文响应，不等同 inbox 列表项）。
- 错误：缺参/多值/箱名非法/`.md` 结尾 → `400 {"ok":false,"error":"bad_request"}`；箱不存在 → `404 not_found`；id 合法但文件不存在 → `404 not_found`（实测 `id=not-a-valid-id` 因过了宽松校验也落到 404，不是 400——**校验宽严看 `bare_presented_id`，别在前端假设**）。
- 遍历防护实测：`box=../etc` → 400；`box=nosuchbox` → 404；`box=bad/name` → 400（box_name_ok 先拒）。
- **【e819 增量】id 必须 URL 编码**：`curl '...?id=<原始UTF-8>'`（中文编号原样进请求行）→ **400 且正文是 http.server 的 HTML 错误页，不是 JSON**（`http.server` 拒含原始非 ASCII 的请求行）。前端/客户端必须先 `encodeURIComponent`；这是硬契约，不是服务器 bug 描述。

### GET /api/outbox
- 无参数。返回 `{"ok":true,"outbox":[...]}`；行字段：`to,id,ref,subject,need,body,alias,mtime,status,reason`。operator 读不到（panel.operator 空或无效）→ `outbox: []`（实测：panel 配置坏时返回空数组，无错误码）。
- 只列 operator 发的、仍在他人 inbox 的普通直发信；`status` 与 edit/retract 同一套仲裁（pending 也可能在 POST 时竞争失败）。

### POST /api/status
- 单体 `{"name","status":"online|offline"}`→`200 {"ok":true,"error":""}`（实测 set 成功）；未知箱 → `400 {"ok":false,"error":""}`（无原因串）；status 非法值 → `400 bad_request`。
- 批量 `{"names":[...],"status"}`：一次提交；空数组/类型错 → 400 bad_request（实测）。
- 组 `{"group":"claude","status"}`：**group 不带 @**（代码内部拼 "@"；传 "@claude" 实测报 `配置里没有分组 @@claude`）；未知组 → 400 ＋ 中文原因串。

### POST /api/clear
- `{"name"}` → 200 `{"ok":true,"error":""}`（整箱 inbox→archived/，与 archive-one 的 done 是两回事）；未知箱 → 400 `{"ok":false,"error":""}`。

### POST /api/archive-one
- `{"box","id"}` → 首次 `200 {"ok":true,"state":"moved"}`；重复 `{"ok":true,"state":"already"}`（**幂等**）；id 非法 → 400 bad_request；文件不在 → 404 not_found；`letter_done_state` 抛 OSError → `{"ok":false,"error":"failed"}` 500（**推断，未触发；非真机 fixture**，源码可见）。

### POST /api/ack-one
- `{"box","id","note"?"}` → `200 {"ok":true,"state":"recorded","id","to"}`；重复 → `{"ok":true,"state":"already",...}`（幂等，不产生第二份回执）。note 非字符串 → 400 bad_request；`ack_letter` 抛 SystemExit/ValueError → `400 {"ok":false,"error":"refused","message":"<原因>"}`；OSError → 500 failed。

### POST /api/send
- `{"from","to","subject","need","body"}` 五字段必须全为 str 且 from/to 非空 → `200 {"ok":true,"id","ref":"<to>/<id>"}`。
- `to` 以 `@` 开头走别名解析（first-online；全离线实测 `400 {"ok":false,"error":"refused","message":"发送失败…当前没有在线信箱（执行候选全部离线）\n候选：\n- box_a：离线"}`）。from 未登记 → 404 not_found；to 既非 @ 又非合法箱名 → 404；类型错/缺参 → 400 bad_request。**不幂等**：每次调用生成新 letter（无客户端幂等键）。

### POST /api/edit-one · /api/retract-one
- 共用入口：`{"to","id",...}`；**sender 恒取 `config.panel.operator`**，请求体里的 from/sender 一概无效。
- edit：`{"subject"|"need"|"body"}` 至少一个非 null → `200 {"ok":true,"state":"updated","id","to"}`；三键全 null/缺 → 400 bad_request；非 str → 400。
- retract：`200 {"ok":true,"state":"retracted",...}`；重复撤回 → 404 not_found（信已归档）。
- operator 未配置 → `400 {"ok":false,"error":"refused","message":"面板没有配置 operator，不能改信或撤回"}`（实测：panel 配置整体无效时 operator 为空即此路径）。
- 竞争：列表说 pending 也可能在 POST 复检时失败（fail closed，409/404）。

### POST /api/role-diff
- `{"name","candidates":[...],"escalation_role"?,"duties"?,"resources"?}`。键不存在=不改；显式 `[]`=清空（candidates 空数组/重复 → 400 中文原因）。
- 200：`{"ok":true,"diff":{name,change{...},expected{active_now,active_after,active_will_change,acceptor_now,acceptor_after,note_key},facts{active_now,acceptor_now,confirmed,candidates[{name,state}]},baseline}}`（baseline=config 字节 SHA256，diff 与基线同窗读取，防「A 预览＋B 基线」）。
- 错误：未知岗位 → 404 `没有岗位 @x`；缺 name → 400 缺少岗位名；校验不过 → 400 `校验不通过：…`。

### POST /api/role-apply
- 体=diff 请求＋`baseline`。缺 baseline → `400 {"ok":false,"saved":false,"projection":"not_applicable","error":"缺少基线哈希（请先查看差异）"}`。
- 200：`{"ok":true,"saved":true,"projection":"fresh|pending|not_applicable","message","backup":"config.role.<ns>-<rand>.bak","active_now","acceptor_now"}`（projection=fresh 全部名片刷新／pending 有待刷新／无受影响名片 not_applicable）。
- **基线不符 → 409 `{"ok":false,"stale":true,"saved":false,"projection":"not_applicable","error":"配置已被外部修改，页面差异已过期；请重新查看差异后再确认"}`**→ apply 不幂等：同一 baseline 二次提交必 409。
- **【e819 增量】409 无 `current` 字段**（已实测确认：响应只有 ok/stale/saved/projection/error，不回传当前配置）。
- **【e819 增量】检查顺序：baseline 先于别名存在性**——旧 baseline＋不存在别名 → 409（不是 404）；要拿 404 必须带**新鲜** baseline。
- **【e819 增量】错误响应一律带 `saved:false,projection:"not_applicable"`**（400/404/409/500 全一致），别把 not_applicable 当"这次没受影响"读。
- **【e819 增量】500 备份失败已实测**（`logs/` chmod 555 构造）：`{"ok":false,"saved":false,"projection":"not_applicable","error":"无法创建配置备份（回滚点），已中止本次提交；配置保持原样"}`——提交前先建 `config.role.<time_ns>-<rand>.bak`，失败即中止、配置不动。
- **【e819 增量】projection=pending 已实测**（受影响箱目录 chmod 555→project_contact 失败）：`saved:true`＋message 含"名片投影待刷新：box_b"。
- **【e819 增量】200+not_applicable 在一致状态下不可达**（代码路径＋实测双证）：合法 config 的候选必已登记→affected 恒非空→fresh/pending；候选未登记→400（"候选指向未登记信箱"）。

## 3. 鉴权 / CORS / 来源校验（实测）

| 场景 | 结果 |
|---|---|
| POST 无 Origin（curl 默认） | 200（本地进程直连可行） |
| POST `Origin: http://127.0.0.1:<port>` 或 `http://localhost:<port>` | 200 |
| POST 其它 Origin（ evil.example.com ） | **403 `{"ok":false}`** |
| POST `Content-Type: text/plain`（Origin 正确） | **403**（先排空 body 再拒，防 RST） |
| POST body 非 JSON 对象 | 按空对象处理→参错 400 |
| **【e819 增量】GET 带 evil Origin** | **403 `{"ok":false}`**（实测 `/api/state`＋`Origin: http://evil.example.com`） |
| **【e819 增量】GET 带 evil Host** | **403 `{"ok":false}`**（实测 `Host: evil.example.com`；防 DNS rebinding） |
| **【e819 增量】GET 无 Origin（curl）** | 200（本地进程直读仍可行） |
| GET 带合法 Origin（127.0.0.1/localhost＋本端口） | 200 |
| 监听地址 | 127.0.0.1 only |

结论（给云端客户端的硬事实）：**e819f0e 起读端点也过同一 Host/Origin 精确白名单**（X-01：无通配、无后缀匹配；只认本机回环带端口与显式配置的可信 origin）；写端点=白名单＋JSON＋Content-Type。浏览器跨站脚本与 DNS rebinding 均被 403 挡住。`POSTOFFICE_PANEL_BASE_URL` 可再加**一个**精确可信 Origin（本次未设，标未证）。

## 4. 推断（未实测，不当事实）

- `ack-one` 的 OSError→500：源码路径可见，隔离环境未构造出该故障，标**推断**。`archive-one` 的 500 同。
- 并发写竞争的精确败者形态（409 vs 404 分布）：只观察到 fail-closed 存在，未做并发压测，标**未证**。
- HTTP/1.0、无 keep-alive：响应行实测，客户端复用连接行为**未测**。
- `POSTOFFICE_PANEL_BASE_URL` 增加第二个可信 Origin 的行为：未设置实测，标**未证**。
- **e819 增量中由"推断"升级为"实测"的项**：role-apply 500 备份失败、projection=pending、409 无 current、409 先于 404、200+not_applicable 不可达、GET 来源校验——均为本轮隔离实跑，不再是推断。

## 5. 复现命令

```bash
T=$(mktemp -d); PO=<local worktree>/postoffice
export POSTOFFICE_HOME=$T POSTOFFICE_NO_NOTIFY=1 POSTOFFICE_POLL=1
# 匿名箱：add panelop/box_a/box_b/box_unlisted --notify；box_c(opencode_plugin) 直接写 routes.json
# 匿名 config.json：version 2 + organization{companies{anonco{rules_file}},projects{anonproj{company_id,root,status_file}}}
#   + aliases{office.design/office.worker/office.missing（role 须带 scope）} + groups{notify:[...]}
#   + panel{operator:panelop,label,organization:{members:[box_a,box_b],label}}（organization 必须是对象）
P=$(python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])")
$PO panel --no-open --port $P &
curl -s http://127.0.0.1:$P/api/state
curl -s -X POST -H 'Content-Type: application/json' -d '{"name":"box_b","status":"online"}' http://127.0.0.1:$P/api/status
# 403 复现：curl -H 'Host: evil.example.com' http://127.0.0.1:$P/api/state
#           curl -X POST -H 'Origin: http://evil.example.com' -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:$P/api/status
# letter id 必须 encodeURIComponent；原始 UTF-8 → 400 HTML 页
# role-apply 全状态码构造：fresh=正常；pending=chmod 555 受影响箱目录；500=chmod 555 $T/logs；409=中途改 config
```
