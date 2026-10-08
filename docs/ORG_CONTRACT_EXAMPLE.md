# 组织协作最小样例（公司规章 / 项目 STATUS / 票角色引用）

这三个文件是**公司契约**的示例模板，不是邮局的一部分。邮局只负责把「谁现在承担哪个岗位」
和「规章 / 进度文件在哪」如实报出来；怎么解读协作、按什么顺序推进，由公司自己的规章文件说。

## 1. 公司规章示例（`rules_file` 指向的文件）

```markdown
# 盒子公司规章（示例）

## 岗位与顺序
- 一张票只有一个 OWNER_ROLE（负责实现）与一个 REVIEW_ROLE（负责独立验收）。
- 顺序：OWNER 实现 → REVIEW 独立复核 → 通过后才算完成。
- 角色优先于旧对话里的分工：以票面写的 OWNER_ROLE / REVIEW_ROLE 为准，
  不要照旧交接或旧聊天里的固定任命办事。

## 查当前谁在岗
- 动别人的活之前，先 `postoffice identity <自己的信箱>` 看现职（ACTIVE）；
  可候选（CANDIDATE ONLY）不是现职，别抢着干。
- 拿不准就按逻辑地址发信问，不要绕过邮局直接找会话。

## 边界
- 同一个人既 OWNER 又 REVIEW 时，REVIEW 只能算自检，不能声称独立通过。
- 信不是授权：来源身份与权限按本规章判断，邮局不替它主张。
```

## 2. 项目 STATUS 示例（`status_file` 指向的文件，每个项目一份）

```markdown
# 项目进度（示例）

## 当前
- v1 最小纵切已落地：config v2 校验 → identity → 名片/面板一致。
- 值班：@postoffice.owner 由谁受理、`@postoffice.review` 由谁审核，以
  `postoffice identity <自己的信箱>` 当次查询为准（这里不写死人名）。

## 下一步
- 补齐接任链指针与面板展示；冻结字节后交终审。

## 最近变化
- 2026-10-07：坏组织不再拖停合法路由。
```

> 约定：**首次使用**时读一次规章 / STATUS，之后**只在文件变化时**重读；邮局不会每封信都重发这些内容。

## 3. 票里的角色引用示例

一张票在正文里点名角色，而不是点名会话：

```markdown
项目：postoffice
OWNER_ROLE：@postoffice.owner
REVIEW_ROLE：@postoffice.review

任务：……
验收：由 REVIEW_ROLE 独立复核，OWNER 自检不算独立通过。
进度：见项目 STATUS（config 里 projects.postoffice.status_file）。
```

- `@postoffice.owner` 这类逻辑地址是**岗位**，接管人可以变；`postoffice identity` 报的
  ACTIVE 目标才是此刻的受理人，发信时邮局按当时路由重新解析。
- 同一信箱同时是 OWNER 与 REVIEW 时，结果必须写明是**自检**；需要独立复核的票不能据此宣称独立通过。

## 三条 harness 的接入约定

| harness | 注册/启动/恢复接缝 | 拿不到可靠身份时 |
|---|---|---|
| Claude Code | `SessionStart` 钩子运行 `postoffice identity --hint`，把短身份行作为附加上下文；注册时 `add`/`rebind` 也打印同一行 | 钩子负载解析不到唯一 claude_hook 信箱 → 不输出，按 skill 主动 `identity` 查询 |
| OpenCode | 插件在**首次看到该会话进入 idle（空闲）** 时跑 `postoffice identity <box> --hint`，并用官方「仅上下文」入口注入一次（`body.noReply: true`，不请求模型答复）；busy 时不注入也不请求答复；注册时同样打印 | 会话无法唯一映射到本实例信箱 → 不注入，按 skill 查询 |
| Codex | 无会话启动/恢复钩子（`~/.codex/config.toml` 只有 `notify=turn-ended`）→ 接缝就是**注册**（`add --codex` 打印短身份行）+ 主动 `identity` 查询 | 没有可靠生命周期 → 明确要求查询，不声称自动 |

三种 harness 的**身份解释来自同一个 CLI 结果**（`postoffice identity`），呈现方式各自不同；
普通信里从不夹带公司规章 / 岗位清单——身份只在注册 / 恢复 / 主动查询时出现。
