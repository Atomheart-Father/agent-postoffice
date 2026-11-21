# BOXZ OFFICE 统一发布

暂存需 Git、Node、Bun、Python 3.12+，构建 macOS App 另需 Xcode。安装只使用当前用户和所选 Office home。

```sh
python office/tools/stage_release.py <发布完整提交> <新发布目录>
python <新发布目录>/office/tools/install_release.py --home "${POSTOFFICE_HOME:-$HOME/office11}"
```

暂存从同一提交的 `office/` 和 `app/` 构建联合包，版本清单记录两份源码的提交。需要测试独立 App 检出时，可以指定 `--app-repo`。
目录的父目录须已存在，发布目录必须不存在，路径中的任何部分不能是符号链接。命令导出该提交的 `office/`，写入 `DEPLOY_SOURCE.json`，检查 CLI、officed、MCP 和 Pi/OpenCode 原生入口，并用现有 `releaseVersion` 读取版本。失败时移除本次创建的目录；不会覆盖已有目录。

安装备份受影响的入口，把 CLI、后台服务、Pi、OpenCode、Claude Code 和 Codex 指到所选 home 的 `runtime/office`，把 App 放在同一个 home，启动服务并回读身份。首次安装保留宿主各自的原生信任流程。Codex MCP 已登记时只更新命令和参数，保留工具审批设置。

一次发布按同一个固定包切换后端和全局宿主入口。先备份配置、确认当前回合结束，再切换服务、更新入口并用宿主原生方式重载。检查现有宿主已发现新入口、实际工具或版本回执正常后，将旧包和已确认的重复 Office 入口移出加载目录。未重连的原会话单独记下实际加载状态。

OpenCode 2 全局入口在 `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins/office-postoffice/index.ts`。升级后通过宿主原生方式重载，再用受控身份回读业务数据和 `release.status` 的加载版本。Pi 仍用原生扩展、Claude Code 仍用原生插件及钩子，Codex 仍用原生 MCP 和用户级钩子。

安装生成 `UserPromptSubmit`、`PostModelRequest` 与 `Stop` 三条用户级钩子，保留其他钩子。新命令需经 Codex 原生信任，命令完全相同的重装会保留原条目。`/hooks` 用于 Codex 终端，桌面按客户端实际显示的审阅入口操作。原文关联、实际运行回执和轮次结束分别由这三个事件提供。`PostModelRequest` 同时报告本会话、本回合已落盘且带原生 response_id 的用量；无可靠 Work 回执时为未归属，费用未知。

本机 App 复用同一个安装目录下 `sock/` 的受控描述符。双击安装目录中的 `BOXZ OFFICE.app` 便会连接旁边的服务。显式指定 `BOXZ_OFFICE_HOME` 或 `POSTOFFICE_HOME` 可接隔离实例。
