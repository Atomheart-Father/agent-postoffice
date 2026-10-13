# CLOUD_CLIENT_API_PACK（单一交付入口；DESIGN 整合，2026-10-13）

对象：本地候选 **后端 e819f0e**（未 push／未合 master／未部署）；客户端待验 **claude/office-client = 1d21398**（作者验证 Node 203+159 通过，IME 未测；**非独立验收，非 PASS**）。GitHub master = 1ab0f54，不含本包所述候选接口。所有样例匿名，来自隔离 temp HOME 实跑；读码推断单列，缺的状态明写「未取到」。

## A. 云端可复制交接正文（T0 手动贴）
```
任务：把 office-client 1.0 接到真实后端，先做「真实读写与稳定」，不重画、不扩原生壳。
后端合同以本包为准（e819f0e 本地候选；GitHub master 无 role-diff/apply，别读 master 推接口）。
依据文件见下方 §B（每个文件给了 sha256）。规则：
1. 能接的真接：state/letter/outbox 读；send/edit-one/retract-one/ack-one/archive-one 写；role-diff→确认→role-apply。
2. 没有后端的（Work/审批/HUD/线程控制/秘书）呈现「能力缺失」，不要用 mock 冒充正式界面；mock 只能放在显式的 mock 开关后并带「未接线」标。
3. 不猜授权：没有 token、没有 can.*。403 {"ok":false} 在候选 e819f0e 上按已证错误体解释为 Host/Origin/Content-Type 保护命中（见 http_cases 的 x01_*）；未知 403 保留原错误体原样显示，不一律当部署问题。GitHub master 1ab0f54 的访问保护不同，表述须绑定版本。
4. letter 的 id 必须 encodeURIComponent；POST 必须 Content-Type: application/json 且同源 Origin。
5. role-apply 返回 saved:true,projection:"pending" 是「已保存、名片待刷新」，不是失败，不重发；409 stale 无 current，重新 role-diff。网络超时＝unknown：不重发 apply；/api/state 不提供整份 config 的 baseline，不能据它判断上次 apply 是否生效；可读 state 观察事实，必要时重新 role-diff 取当前 baseline 做新预览，但内容相同也不能证明是谁保存的，禁止据此自动再 apply。解析上保持 saved 与 projection 分离：收到未知 projection 显示「已保存＋投影状态未知」，不报整体失败。正常验收只看 fresh/pending。
6. 缺值显示 missing/unknown，不补 0、不补「空闲」；online/binding/presence/control 并列，不合并成 connected。
7. 交付时回报：你用的后端合同文件 sha、client commit SHA、哪些操作真接/哪些缺失；中文输入法（IME）必须真实测，写出步骤。
```

## B. 文件清单（发布根 = `docs/cloud-client-api-e819/`；以该目录 `SHA256SUMS.txt` 为发布版校验，下表 hash 仅为脱敏前来源文件的 hash，非发布 hash；整体以 commit 为版本依据）
| 文件 | 内容 | 来源 hash（脱敏前，非发布 hash）| 来源线 |
|---|---|---|---|
| `CLOUD_CLIENT_API_PACK.md` | 本文件 | （自引用，见邮件） | DESIGN |
| `CHAPTER_0_BASELINE.md` | 版本基线与能力状态表 | f65ea6d60680a36a | DESIGN |
| `DATA_CH1_HTTP.md` | 12 路由实测清单 | b980e60d29db80ba | Data |
| `DATA_CH2_READ_MODEL.md` | state/read_model 字段表，含 role_cards 16 键 | 6ae6cbbd09e1e838 | Data |
| `CHAPTER_3_CAPABILITIES.md` | 适配器能力矩阵（含 §3.9–3.11） | 401d0cda432c91b9 | Interface |
| `CHAPTER_4_PERMISSION_STATE.md` | 权限与状态词汇 | f83bd274facc26f4 | DESIGN |
| `CHAPTER_5_INTERACTION.md` | 交互状态机 | 2ebfb2c868f4505d | DESIGN |
| `CHAPTER_6_GAPS.md` | 缺口（已更新 2026-10-13） | 8ba7417d07e527fe | DESIGN |
| `DATA_CH7_FIXTURES.md` | fixtures 说明 | 26d5e4c6ea4827ca | Data |
| `fixtures-data/state_ok.json` | /api/state 200 | 9ebfed73158acc54 | Data |
| `fixtures-data/letter_ok.json` | /api/letter 200 | 9e60a4b342bf79fc | Data |
| `fixtures-data/outbox_ok.json` | /api/outbox 200 | e9f3387b58a85f92 | Data |
| `fixtures-data/http_cases.json` | 47 例（含 apply fresh/pending/409/500；diff 200/400/404） | e740b1527c352633 | Data |
| `fixtures-data/role_diff_ok_response.json` | role-diff 200 真实 change/expected/facts 形状（进程内实抓） | fdd124362e84d6ee | DESIGN |

未取到：role-apply 200+`not_applicable`（一致状态不可达，见 G7）；V2 面板侧 activity（G6）。

## C. 1.0 客户端必交操作清单（目标=本批可用；接线=后端已证）
| # | 操作 | 后端 | 说明 |
|---|---|---|---|
| 1 | 读公司/岗位/信箱与绑定状态 | GET /api/state（boxes、aliases、role_cards、panel、config_error、activity） | 缺值 missing/unknown；岗位≠信箱 |
| 2 | 读单封信全文 | GET /api/letter?box&id | id URL 编码；无 kind/status 字段 |
| 3 | 读我已发且未被接受的信 | GET /api/outbox | operator 空→`[]` |
| 4 | 发信 | POST /api/send | 不幂等，禁止自动重试；`to` 可 `@岗位` |
| 5 | 编辑／撤回未接受的信 | POST /api/edit-one、/api/retract-one | sender 恒为 operator；撤回二次=404 |
| 6 | 收执（回执）／归档 | POST /api/ack-one、/api/archive-one | 均幂等（recorded/already，moved/already） |
| 7 | 上下线、整箱清理 | POST /api/status、/api/clear | clear≠archive-one |
| 8 | 岗位预览与保存 | POST /api/role-diff → /api/role-apply | 见 §A 第5条；`fixtures-data/role_diff_ok_response.json` 有真实形状 |
| 9 | 错误态呈现 | 400/404/409/500/403 | 用 http_cases 的 47 例做渲染测试 |

### 能力缺失（按真实缺失呈现；补接顺序）
1. **线程列表／岗位→线程**：适配器无「列出会话」动词（G1）→ 先需新接口票（read-only，observe≠control）。
2. **usage HUD**：账模块在候选，reader 未接 → 第二。
3. **Work／审批／验收／行为账**：仅设计（1.1），晚于以上。
4. **线程实时控制（中断等）**：适配器有 `/interrupt`，面板不暴露；需带 binding 授权的窄接口，最后。
5. **强授权（token／can.*）**：不存在；任何 UI 不得暗示。

## D. 同源承载窄票（Codex 校准版；可派 Lab，不等 V2 其它票；不是生产部署授权）
- 目标：client 在独立地址 `/client/` 与面板同源提供，页面以同源 Origin 调现有 API；原 panel 与 Host/Origin 保护不变。
- **白名单＝指定 client 提交的发布清单**（当前 client 为原生 ES module、无构建）：清单列出 index、相对模块依赖、CSS 等**需要的资产**及各自 sha256；README／fixture／tests／sourcemap 不入清单。不得在启动时把固定根下全部文件自动入白名单。
- **字节核验**：服务时核被服务的准确字节哈希，启动后文件被改动不得悄悄服务未验证内容（读后校验或启动载入内存）。
- **路径**：固定根的解析与读取均防符号链接／路径逃逸；无通用任意路径接口；`/client` 与 `/client/` 规范化；明确嵌套路径与 MIME（`.js` 模块用 JS MIME，不被 nosniff 拦掉；`.css`、`.html`）。
- **CSP 仅作用于 `/client/*`**，按 1d21398 真实资源给最小策略或迁出内联主题初始化脚本；当前 index 的内联主题初始化、视图动态 style、data favicon 会被 `default-src 'self'` 阻断——不全局放宽 script `unsafe-inline`，不改原 panel。
- 其余约束：不加通配 CORS；不向页面交凭据（本就无）；Host/Origin 白名单原样套用；`Cache-Control: no-store`。
- **验收（必须含）**：①负例：`/client/../`、编码／双重编码／反斜杠、符号链接、非清单文件均不可得，且不得被误路由成 API 或原主页而「看似成功」；②陌生 Host/Origin=403；③真实浏览器同源模块加载并读写成功；④**看控制台无 CSP 错误**、主题持久化、组织连线、岗位编辑与弹层正常（不止 HTTP 200）；⑤原 panel 页字节不变。
- 发布：随下一 checkpoint 过发布门；验收绑定后端 SHA＋client SHA。

## E. GitHub 可读候选（Codex 已允许准备；push 前按发布／分享边界核准确文件）
不推生产树。若 Codex 同意：新建隔离候选目录，仅含本包 .md＋fixtures-data（已匿名），脱敏扫描（路径、邮箱名、sha 外无标识）后由 Codex/Q 决定推送。本整合**未 push**。

## F. 下一轮验收绑定
报告须写：后端 SHA（e819f0e 或其后继）＋client SHA；匿名隔离公共入口纵切（状态读→发信→收执→岗位 diff/apply 各一例）；真实中文输入法步骤（输入、候选选择、回车不误发）。
