# 第 6 章：mock 契约与后端缺口（DESIGN 汇总；随三线交付持续追加）

## 缺口清单
| # | 缺口 | 影响 | 来源 | 状态 |
|---|---|---|---|---|
| G1 | **线程列表能力不存在**：适配器 CAPS 无 list/sessions 动词；`GET /api/session` 不对外暴露；只能对已知 sessionID 逐个 resolve。岗位↔线程对应在 round/binding，不在适配器能力面 | 「岗位→线程」纵切没有数据源，客户端不得靠轮询 resolve 枚举拼凑 | Interface CHAPTER_3（实测） | 后端缺口，需新接口票；前端本批用 mock |
| G2 | 线程创建／实时事件／工作流／权限矩阵执行／秘书／usage HUD 接口均未交付 | 只能 mock，标「未证」 | PLAN | 未证 |

## 前端必须按三态写（Interface 实测）
- contract_ok：`yes` / `no`（证明不行）/ `unknown`（证明不了）；不得压成成功/失败。
- `sent` 三态，`null` 走 probe，不得当 false 重发；probe 只证 admission，从不返回 accepted。
- 观察动词零 POST、写动词才产生 POST；`accepted` 仅代表受理。

## 2026-10-13 更新（基线 e819f0e；Data 章1/7、Interface 章3 实测）
| # | 事实 | 状态 |
|---|---|---|
| G3 | 无 `retryProjections` 端点（POST 实测 403，不在 9 条白名单） | 已证 |
| G4 | 无角色 token／`can.*` 授权；写操作只有 Host/Origin/Content-Type＋operator | 已证 |
| G5 | 适配器有 `/interrupt` 动词、有 session 创建，**但 HTTP 面板不暴露**；线程列表仍无（G1） | 已证（Interface） |
| G6 | `activity` 有时效与来源；V2 `session.active` 非空形状已抓到，面板侧 activity 字段对 V2 **未证** | 未证 |
| G7 | `role-apply` 200+`not_applicable` 在一致状态不可达（候选必已登记）；客户端只需处理 fresh/pending | 已证（代码+实测） |
| G8 | role-diff 200 的 `change` 形状已由进程内实抓补入 `fixtures-data/role_diff_ok_response.json`（非 curl） | 已取到 |
| G9 | 客户端不得读 GitHub master 推断接口：master=1ab0f54 无 role-diff/apply、无 Host 校验 | 已证 |
