# 小票交付：20 分钟未确认唤醒提醒同步 Slack

- 工作树：`/Users/bozhongxiao/code/po-slack-grace`，分支 `slack-grace-timeout`，基线 `ce14581c185d5447bed65182ce65538f19cb1171`（master v1.12.4）。
- 与 v1.13 完全隔离（manager 裁决 A：从 master 明确基线另建 worktree）；未提交、未合并、未部署。
- 线的组织/身份标准化**不在本票**（仍在设计，未发实施票）。

## 合同与实现

复用 `POSTOFFICE_SLACK_WEBHOOK` / `POSTOFFICE_PANEL_BASE_URL`，不新增 webhook 配置。

- 抽出共享 `slack_post(text)`：一条尽力而为的 webhook POST，`timeout=3`、失败只记类型/HTTP code（绝不记 URL）、绝不重试。既有 `slack_alert` 改用它（行为不变）。
- 新增 `slack_grace_alert(name, p)`，接在 `cmd_postman` 的 **claude_hook / opencode_plugin GRACE 到期分支**（`notify(...)` + `mark(key, reminded=False)` 之后）：
  - 原桌面弹窗保留；
  - 向同一 Slack 追加一条短告警，只含**目标信箱、事由、面板信件深链**，**绝不含正文**；
  - 措辞如实写「`20 分钟未确认成功唤醒/投递`」——不宣称没读 / 模型忽略（本系统无已读检测）；
  - **沿用同一超时去重依据**（该分支原有的 `mark`），不写第二套投递真相，不把告警记成已送达；
  - webhook 未设置 → 零请求；失败/慢响应 → 短超时、不影响桌面弹窗与记账、不循环重试、不回显密钥。
- 语义保持：离线不催（离线时不进该分支）、上线重新计时（`online_since`）、已投递/已接受不告警、闹钟信不催人；只同步「这一类」超时弹窗，不把所有 `notify` 泛化转发。

水位纠正：`slack_alert` 旧 docstring 写的「hook/plugin grace-timeout path never calls here」已随本票作废；README/README.zh-CN 里把 Slack 描述为「仅确认投递」的旧叙述、以及「Slack 提醒跟着邮递员轮次走」的旧措辞，已同步为「确认投递告警 + 超时同步告警」。

## 改动文件

- `postoffice`（`slack_post` / `slack_grace_alert` / GRACE 分支一行）
- `README.md`、`README.zh-CN.md`（Slack 段、FAQ、测试表）
- `tests/slack_grace_test.py`（新增，12 项）

## 测试（RED 先行）

`tests/slack_grace_test.py` 12 项，本地假 webhook：

- 正：GRACE 到期 → 恰好一条，`{text}` 体，含目标信箱 + 事由 + `#/mail/<box>/<id>` 深链，措辞「20 分钟未确认成功唤醒/投递」，无正文；桌面弹窗照旧（`notify.log`）。
- 负：webhook 未设置零请求；GRACE 未到零请求；已接受（`.seen`）零请求；离线零请求；闹钟信零请求；下一轮不重发；notify 路径保持自己的「有新信」措辞、不混超时措辞。
- 稳健：失败 500 → 仍记账、信留在 inbox、桌面弹窗照旧、不重发；8 秒慢 webhook → 不把下一个信箱的超时记账拖过 6.5s；密钥不进日志。

RED 证明：临时 `git stash` 掉 `postoffice` 改动后重跑 → `FAILED (failures=3, errors=1)`；恢复实现后 12 项全过。

回归：`tests/smoke.sh`、`panel_slack_test.py`(10)、`env_file_test.py`(12)、`receipt_test.py`、`message_flow_test.py` 全过；`py_compile`、`git diff --check` 干净。

## 边界

- v1.13 门审不受本票影响；本票单独验收。未来集成在真实提交树做一次必要回归即可。
- 未提交、未合并、未部署；线上 v1.12.4 未动。
