# BOXZ OFFICE 统一发布

暂存需 Git、Node、Bun、Python 3.12+，构建 macOS App 另需 Xcode。安装只使用当前用户和所选 Office home。

```sh
python office/tools/stage_release.py <发布完整提交> <新发布目录>
python <新发布目录>/office/tools/install_release.py --home "${POSTOFFICE_HOME:-$HOME/office11}"
```

暂存从同一固定提交的 `office/` 和 `app/` 构建联合包，版本清单记录两份源码的提交。
目录的父目录须已存在，发布目录必须不存在，路径中的任何部分不能是符号链接。命令导出该提交的 `office/`，写入 `DEPLOY_SOURCE.json`，检查 CLI、officed、MCP 和 Pi/OpenCode 原生入口，并用现有 `releaseVersion` 读取版本。失败时移除本次创建的目录；不会覆盖已有目录。

安装备份受影响的入口，把 CLI、后台服务、Pi、OpenCode、Claude Code 和 Codex 指到所选 home 的 `runtime/office`，把 App 放在同一个 home，启动服务并回读身份。首次安装保留宿主各自的原生信任流程。Codex MCP 已登记时只更新命令和参数，保留工具审批设置。

一次发布按同一个固定包切换后端和全局宿主入口。先备份配置、确认当前回合结束，再切换服务、更新入口并用宿主原生方式重载。检查现有宿主已发现新入口、实际工具或版本回执正常后，将旧包和已确认的重复 Office 入口移出加载目录。未重连的原会话单独记下实际加载状态。

OpenCode 2 全局入口在 `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins/office-postoffice/index.ts`。Pi 使用原生扩展，Claude Code 使用原生插件及钩子。Codex 的工具走 MCP，收信走原生队列。安装会移除旧的 Office Codex 钩子并保留用户其他钩子。升级后通过宿主原生方式重载，再用受控身份回读业务数据和 `release.status` 的加载版本。各宿主的收信时机见[投递验收](../TESTING.md#mail-delivery-by-host)。

本机 App 复用同一个安装目录下 `sock/` 的受控描述符。双击安装目录中的 `BOXZ OFFICE.app` 便会连接旁边的服务。显式指定 `BOXZ_OFFICE_HOME` 或 `POSTOFFICE_HOME` 可接隔离实例。

## 第一次建项目

1. 在计划担任项目经理的会话，用 `office_request_create` 提交 `kind: "join"` 开通个人信箱。
2. 由本机操作者在所选 Office home 的 `workspace-map.json` 登记项目目录。已有映射时，保留原有条目并添加新的名称。项目申请引用映射的名称，例如 `promo`。

   ```json
   {"promo": "/absolute/path/to/promotion"}
   ```

3. 从经理会话用 `office_request_create` 提交下面的一份申请。Boss 在 App 批准后，邮局会创建岗位和项目，把岗位绑定到这个会话，并授予该岗位项目管理权。

   ```json
   {
     "kind": "holder",
     "position": "promo.lead",
     "reason": "创建推广项目，由本会话负责",
     "create": {
       "position": {},
       "project": {"name": "推广", "owner_ref": "promo.lead", "workspace_refs": ["promo"]}
     }
   }
   ```

未任命 `@office.admin` 时由 Boss 处理组织审批。需要委托审批时，从已登记的管理员会话提交 `kind: "holder"`、`position: "office.admin"` 的申请。首次建此岗位填 `create: {"position": {"project_ref": null}}`，由 Boss 批准。
