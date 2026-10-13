# 章 5 · 交互状态机（DESIGN）

## 5.1 岗位编辑（仅本地候选）
```
idle → editing(draft, base_snapshot) → previewing
previewing --role-diff 200--> reviewing(diff, baseline)
previewing --400/404--> editing(带 error)
reviewing --确认--> applying(同请求 + baseline)
applying --200 saved:true--> done
   projection=fresh|not_applicable → 完成
   projection=pending → done_with_pending_projection（显示「配置已保存，名片待刷新」；无重试按钮）
applying --409 stale--> conflict（保草稿 + 原基线 + 重新取快照/预览；三方对比由前端展示；不期待 current）
applying --400/404/500 saved:false--> failed（可改后重试）
applying --网络/超时--> unknown（不重发；先重新取快照，核 baseline 是否已变再决定）
```
规则：`duties`／`resources` 键缺失＝不改，`[]`＝清空；不得把「页面没显示该字段」变成 `[]`；整列表替换，上游新增条目可能被草稿覆盖，冲突时必须让用户看见。baseline 以服务端为准，不自造 rev。

## 5.2 邮件
`composing → sending → 落盘到信箱`；其后各阶段**只在后端给出证据时前进**：通道受理 → 提醒展示（`waiting→reminded`）→ 消费。`failed` 终态。任何阶段缺证据显示 `unknown`。「已发布」等不固定成单线通用状态。

## 5.3 通用数据态
`loading / empty / missing / degraded / unknown / error`：
- `missing`＝字段缺失，不补 0；`degraded`＝读模型一致性降级（章 2）；`unknown`＝无证据；`error`＝请求失败。
- `activity` 有时效与来源；缺值不显示「空闲」。`online/binding/presence/control` 并列显示，不合并成 connected。

## 5.4 实时/工作/HUD
全 mock：标记「未接线」，不得与真实数据混同；不据邮件推出工作状态；不为界面新增平行状态库。
