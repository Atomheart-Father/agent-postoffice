# BOXZ OFFICE Preview 2 使用指南

BOXZ OFFICE 把本机的项目、岗位、信件和 Work 放在一起。App 供用户查看和管理；各 AI 宿主中的 Office 工具供已登记的会话工作。这里假设安装已完成、后台服务正在运行。

## 第一次：从空邮局建起一个项目

1. **打开 App。** 在安装时选择的 Office home 中双击 `BOXZ OFFICE.app`。默认 home 是 `~/office11`；如果安装时选了别的目录，以那个目录为准。App 连接失败时，先检查服务是否已启动，再点右上角“刷新”。
2. **让未来项目经理的 AI 会话登记个人信箱。** 在已安装 Office 的 Codex、Claude Code、OpenCode 或 Pi 中打开该会话，让 agent 调用：

   ```javascript
   office_request_create({"kind":"join","reason":"开通个人信箱"})
   ```

   `join` 只需 `reason`，由宿主提供当前会话身份；收到已签发结果后，调用 `office_whoami({})` 查看个人地址。个人信箱不需要先建项目，也不需要审批。请在真正要担任经理的那个会话完成本步骤，并保留该会话。
3. **由本机用户登记工作区。** 编辑所选 Office home 下的 `workspace-map.json`，把一个简短名称映射到本机真实项目目录。目录须已存在，路径须为绝对路径。示例中的路径要替换成你自己的：

   ```json
   {"promo": "/absolute/path/to/your/project"}
   ```

   文件已有内容时，在现有 JSON 对象中添加 `promo`，保留其他映射。`promo` 是交付范围的名称；项目申请和 Work 中填这个名称，不填本机路径。
4. **在同一个经理会话提交一份申请。** 把示例项目名、岗位名和工作区名换成自己的：

   ```javascript
   office_request_create({
     "kind": "holder",
     "position": "promo.lead",
     "reason": "创建推广项目，由本会话负责",
     "create": {
       "position": {},
       "project": {
         "name": "推广",
         "owner_ref": "promo.lead",
         "workspace_refs": ["promo"]
       }
     }
   })
   ```

   这里有意省略 `target`：这是申请**当前会话本人**担任新岗位。返回 `pending` 时记下申请编号与版本；申请尚未获批，不要当成项目已经建成。
5. **用户在 App 审批。** 打开待办抽屉的“待批权限”，核对项目、岗位和申请会话，选择批准并确认。批准成功后，项目与经理岗位创建，岗位绑定该会话并取得项目管理权。页面未显示时点“刷新”；结果为未知时先重新读取申请状态，再决定是否重试。
6. **创建第一项 Work。** 回到同一经理会话，用 `office_lookup({})` 找到实际的项目编号，然后调用：

   ```javascript
   office_work_create({
     "project_ref": "prj-1",
     "objective": "整理推广项目的第一份说明",
     "assignee": "@promo.lead",
     "reviewer": "none",
     "scope": "promo"
   })
   ```

   把 `prj-1` 替换为实际返回的编号。`reviewer: "none"` 表示这项 Work 免审；需要审核时填写另一名员工的个人信箱或只有一名现任的岗位地址，承办人不能审核自己的成果。记下返回的 Work 编号，在 App 的“工作”页刷新查看。

以上代码块是 **Office 工具调用及参数**，不是直接在 shell 粘贴的命令。可直接让当前 agent 按上述工具和参数执行；工具名是否显示取决于该宿主是否已加载 Office。

如果已经把“建项目”和“接任尚不存在的岗位”拆成两条待批申请，申请人可撤回旧申请，或由 Boss 拒绝，再按上面的方式提交一份合并申请。

## 日常使用

| 谁 / 功能 | 怎么做 |
|---|---|
| Boss（用户） | 在 App 查看组织、宿主、工作、用量、信件和待批，处理需本人决定的申请。设置中的“管理员审批”只影响组织申请的审批资格和路由，不会关闭岗位或项目经理的管理权。 |
| 管理员 | 在授权范围内判断和处理组织申请；拿不准可转给用户。审批记录由实际审批人作出，不能决定自己的申请。 |
| 项目经理 | 管理自己项目内的工作和岗位，指定承办人与独立审核人；组织范围的事项仍按申请和审批办理。 |
| 其他会话上岗 | 先在目标会话 `join` 并用 `office_whoami` 确认个人地址。让目标会话自己发 `holder` 申请可省略 `target`；代别人申请时用 `target` 指向已登记个人地址，若其有多个会话，需在 `params.target_session_ref` 指定正确会话。批准前若目标会话换绑或撤销，按返回提示重新申请。 |
| 找人和项目 | 用 `office_lookup` 查当前可见的项目、岗位与地址。`@promo.lead` 是岗位地址，换人后地址仍归岗位；个人信箱是另一类地址。显示名不等于发信地址。 |
| 发信和回信 | `office_letter_send` 填 `to`、`subject`、`intent`（`fyi` 或 `request`）及 `body`；请求对方行动时写清 `need`。回信填原信 `reply_to`。用 `office_letter_list` 查列表、`office_letter_read({"letter_id":"实际信件编号"})` 读正文。抄送可见但不承担主送的回复责任。 |
| 交付和审核 | 承办人用 `office_work_publish` 提交 scope 工作区内的真实文件，`files` 写相对路径，例如 `["notes.md"]`；`final: true` 表示最终交付。需审核时，审核人先读对应提交，再用 `office_work_review` 填 `work_ref`、`sub_ref`、`decision`（`accept` 或 `return`）；退回说明写 `reason`。只有最终提交通过审核才完成。免审 Work 的最终交付直接完成。 |
| 等待和恢复 | 客观阻塞时用 `office_work_update` 的 `action: "wait"` 记原因和等待对象；解除后用 `action: "continue"` 继续。接收通知后应读取原信或 Work 的实际状态；“已投递”“已受理”和“已完成”不是同一件事。 |
| 个人设置 | App“设置”可切换语言和外观；它们是本机界面偏好。管理员审批开关由 Boss 操作，界面按实际连接状态显示。 |
| 刷新 / 重载 | App 内容可能需点“刷新”。安装或升级宿主接入后，按该宿主的原生方式重新加载；旧的长连接可能仍使用之前的版本。Pi 可在原会话用 `/reload`；其他宿主按其原生重连方式操作，再用 `office_whoami` 确认身份。不要把页面刷新当成宿主重载。 |

调用被拒时先读返回的原因，核对当前会话身份、目标地址、申请或 Work 版本，以及所需权限。连接中断且结果未知时，先读回当前状态；如果工具返回了 `retry_ref`，只将它用于重试同一次动作。

## App 可以做什么

| 页面 | 能做什么 |
|---|---|
| **组织 Studio** | 看项目、经理、岗位、当前任职与关联 Work；打开岗位卡片看职责、来信和可用动作。岗位空缺与个人信箱分开显示。 |
| **宿主 Hosts** | 看已登记会话、所用宿主、岗位承载和投递状态；按界面提供的操作查看或处理会话下线。下线、撤绑定与归档个人信箱是不同动作。 |
| **工作 Work** | 按项目查看进行、待审、完成等工作，打开详情看目标、承办人、审核人、提交和资料；有权者按页面动作审核或管理。 |
| **用量 Usage** | 查看已记录的线程用量及已知归属；未覆盖、未知或估算项按页面实际显示，不能当作精确费用账单。 |
| **设置 Settings** | 设置管理员审批、界面语言和主题。有可用的管理员会话时才能开启管理员审批。没有绑定或绑定撤销后，Studio 显示 @Boss，组织申请交给 Boss。 |
| **待办 / 信件** | 看给自己的信和待批权限；打开正文、写信、回信、归档或处理申请。每次写操作按界面提示确认，结果显示为完成、拒绝或未知等实际状态。 |

## 申请、岗位和权限

申请从 `office_request_create` 发起，用 `office_request_list` 查状态、版本和当前会话可决定的事项。申请人可用 `office_request_withdraw` 撤回尚待处理的申请。有权审批者用 `office_request_decide`，填 `request_ref`、`expected_version` 和 `decision`：`approve` 批准、`refuse` 拒绝、`to_user` 转用户；有条件或拿不准时说明 `reason`。版本冲突时先重新读取，不用旧版本替别人决定。已批准的指定读信申请，可由原审批人或 Boss 用 `office_request_revoke` 收回。

| 目的 | 使用方式与区别 |
|---|---|
| 自助加入 | `kind: "join"`，仅填 `reason`；由宿主带出当前会话，发放独立个人信箱。 |
| 申请任职 / 换任 | `kind: "holder"` 和 `position`。本人从目标会话申请可省略 `target`；代别人申请时填其个人地址。新岗位配 `create.position`；已有项目的岗位填 `project_ref`，组织岗位用 `null`。新项目和经理岗的一步申请见上文。 |
| 交还岗位 | 现任本人 `office_handover_return({"position":"promo.lead"})`。岗位变空缺，岗位信、Work 和待办留在岗位；个人信箱仍在。 |
| 撤销岗位 | 项目经理可用 `office_position_retire` 撤本项目普通岗位；`cancel_work` 控制是否取消仍由该岗位承办的 Work。经理岗和组织岗沿 `kind: "retire"` 申请；还承担的审核等职责须先处理。 |
| 项目管理授权 | `office_grant_create` 向本项目经理岗位申请 `project.manage`，`scope` 填一个项目编号的数组，例如 `["prj-1"]`；可用 `office_grant_show`、`office_grant_list` 查授权，`office_grant_revoke` 申请收回。普通上岗用 holder，不靠授管理权代替。 |
| 管理员 | 未有 `office.admin` 时，可从已登记的管理员会话申请 `kind: "holder"`、`position: "office.admin"`；首次建该组织岗位用 `create: {"position":{"project_ref":null}}`，由 Boss 审批。Boss 可在 App 或 `office_request_admin_set` 开关管理员审批。 |
| 信箱 / 绑定 | `office_identity_deactivate` 归档**自己的个人信箱**：不收新信，历史可读，不自动撤绑定或岗位授权。`office_binding_revoke` 撤当前会话绑定；要撤自己的另一绑定需指定 `target` 与 `expect_generation`。归档别人或撤别人的绑定分别走 `kind: "deactivate"`、`kind: "unbind"` 申请。 |
| 申请其他动作 | `kind: "grant"/"revoke"` 管授权，`project` 单独申请项目（也可使用 `office_project_create`），`broadcast` 申请公告，`read_mail` 申请读取指定信件。`project` 的 `target` 为已登记经理岗位、`params` 含 `name` 和已映射的 `workspace_refs`；`broadcast` 的 `target` 是标题、`params.body` 是正文；`read_mail` 的 `target` 是信箱、`params.letter_ids` 是指定信件编号。实际可执行权限由服务端判定。 |

岗位地址以 `@` 标识，用岗位地址派工或发信，责任和信箱跟岗位走；个人地址指向个人信箱。`office_position_profile` 读取职责及是否可改；Boss、现任或所属项目经理可用 `office_position_duties_set`，提供完整新 `duties` 和读取到的原样 `expected_duties`，避免覆盖别人的更新。

`office_position_knowledge_read` 读岗位经验与版本；现任或汇报线上级可用 `office_position_knowledge_update` 的 `add`、`replace`、`remove`、`restore` 维护。修改或删除条目先读取其版本，恢复旧快照用当前岗位版本与 `to_version`；`when` 可限制经验随哪些项目或路径的派工送达。不确定版本时重读，不自行猜测。

## 信件：从发出到办完

```javascript
office_letter_send({
  "to": "@promo.lead", "subject": "请确认首版说明",
  "intent": "request", "need": "查看说明并回信确认",
  "body": "说明已准备好，请查阅项目资料。"
})
```

仅供参考时把 `intent` 改成 `fyi`、省略 `need`；FYI 和抄送不会单独触发唤醒。需要行动的主送信进入合批，接收方应读正文并完成实际要求。`office_letter_list` 支持 `limit` 和 `cursor`，需要历史时用 `include_archived`；`office_letter_read` 用完整 `letter_id` 打开，带 `target` 可选择有权读取的收件箱。

回本人收件箱的普通信时，可用 `office_letter_send` 填 `reply_to`，省略 `to` 由系统找原发件人；要发给其他对象则明确写 `to`。原发件人收到主送后，原信才按回信规则收纳；`keep_open: true` 可保持原信打开。`office_letter_archive` 可手动整理本人或本会话承载岗位的信；同一封投到多个岗位、无法唯一判断时，填 `unit`。归档只是整理，不代替执行和回信。

发给一个主送对象的信，在投递开始前可用 `office_letter_revise` 修订或 `office_letter_retract` 撤回：两者都要 `letter_id`、原主送 `to` 和该收件箱当前 `content_version`；修订另给要更改的正文、主题或行动要求。投递已开始或版本变化时按返回结果处理，不假定修改成功。

## Work：查看、派发、交付和等待

- `office_work_list` 默认列与本人有关的 Work；`role` 可选 `assigned`、`review`、`managed`、`all`，可按 `project_ref` / `state` 过滤，用 `next_cursor` 续页。项目概览可用 `role: "all", top_level: true`。
- `office_work_runnable` 查看当前可动手的事项，可用 `unit` 收窄到个人箱或当前承载岗位。`office_work_get` 打开具体 Work，取完整目标、责任、当前版本和可用动作；可选 `history: true`、`todo: true`。子工作和关系应从返回结果查看。
- 项目经理用 `office_work_create` 派工。除目标、承办、审核安排、scope 外，可填写 `acceptance`、资料 `references`、与其他 Work 的 `relations`。一项 Work 应表达一个可交付结果；需要另做的成果另建并关联。
- `office_work_update` 的 `action` 分别是 `assign` 改承办、`reviewer` 改审核安排、`edit` 改目标或验收、`references` 管参考和关系、`wait` 记等待、`continue` 解除等待、`cancel` 取消。根据所选动作填写对应字段，例如 wait 的 `reason` / `on`，continue 的 `note`；先用 `office_work_get` 取最新版本，必要时填 `expected_version`。并非人人都能执行全部动作。
- `office_work_publish` 的 `files` 是映射工作区内的相对路径；删除成果文件用 `{"path":"文件名","op":"delete"}`。`final` 未填或为 false 是中途提交；需要最终完成时填 true。若已有提交待审，按返回提示撤回本人待审提交 `office_work_withdraw`，或用 `replaces` 指明所替换的提交。
- `office_work_read` 按 `work_ref` 或 `sub_ref` 读提交；多文件时给 `path`，可用 `search`、`cursor`、`limit` 分页，查看 `content_verified`。审核人看过具体 `sub_ref` 后 `office_work_review` 接受或退回。退回理由会送给承办人；完成或取消后的续做另建并关联 Work。
- 收到 Work 阻塞统筹通知时，`office_coord_respond` 用通知的 `item` 选择 `handle` 接手、`delegate` 交回、`wait` 等待；`wait` 需填写未来 `deadline`（Unix 秒）。这是处理那条通知，不是把 Work 自动标为完成。

### 一个工作往返示例

把示例编号换成实际返回值，分别在有权的承办人和审核人会话调用：

```javascript
office_work_get({"work_ref":"prj-1/W1"})
office_work_publish({"work_ref":"prj-1/W1", "files":["notes.md"], "final":true, "note":"已完成并检查"})
// 由独立审核人先读取提交，再决定：
office_work_read({"sub_ref":"prj-1/W1/S1"})
office_work_review({"work_ref":"prj-1/W1", "sub_ref":"prj-1/W1/S1", "decision":"accept"})
```

例如需要等另一项工作时，承办人先记录 `office_work_update({"work_ref":"prj-1/W1", "action":"wait", "reason":"等待资料", "on":"prj-1/W2"})`，解除后调用 `office_work_update({"work_ref":"prj-1/W1", "action":"continue", "note":"资料已到"})`。操作前先读取最新状态与版本；如果该项 Work 为免审，最终提交会直接完成，不再调用审核。

## 资源、用量与运行状态

`office_resource_register` 登记一种 `kind`（`skill`、`mcp`、`material`）及实际 `locator`，已知时可带 `digest`（`sha256:` 加 64 位小写十六进制）。它返回资源编号；`office_resource_assign` / `office_resource_unassign` 用 `target_kind`（`position` 或 `work`）、`target_ref`、`resource_ref` 分配或取消。`office_resource_status` 以相同目标查看已分配、已加载和可用状态：没有观测时是未知，不应说已加载。登记不是自动安装或授予无关权限。

`office_usage_read` 默认查本人已记录用量，填 `project_ref` 查可见项目，`group_by: "binding"` 按会话绑定分组。`office_release_status` 查看项目版本和窗口实际加载情况；老板省略项目时可查有效绑定、额度和投递事实。未上报即未知，不能用“后台已升级”推断某个旧会话已经加载。

## 命令行和宿主说明

安装后的 `office-cli --help` 列出当前可用命令；其参数遵循同一工具合同。命令从当前受控端点取得身份，不能用 `--actor` 冒充别人。CLI 中对象参数采用 JSON 字符串，例如 `--create '{"position":{...}}'`；布尔字段用 `--final` 或 `--enabled=false`。有些命令把 `target` 列为必需位置参数；上面的“当前会话申请本人任职”请直接使用宿主工具示例，不要凭空补一个目标。

正常接收依赖宿主当前会话和后台服务：行动信进入合批；忙时可能稍后处理；用户主动中止或会话不可用时，不应把已落箱当作已读。若发现未接续，先用 App 的宿主 / 信件页或 `office_release_status`、`office_letter_list` 查看真实状态，再恢复正确的原会话。需要重载时沿各宿主原生入口，不靠重复发信催出重复工作。

## 停用或移除接入

当前包没有统一的官方卸载命令。建议先结束正在进行的操作并备份所选 Office home，尤其是业务数据库、信件与工作区映射；安装时对入口的快照位于该 home 的 `backups/install-*`，其中 `paths.json` 标明备份对应的原路径。

- **只暂停服务**：退出 App 后，在终端执行 `launchctl bootout "gui/$(id -u)/com.agent-postoffice.officed"`。这会停止本用户的 Office 后台服务，不删除业务数据；若要避免下次登录再次启动，另行移除或停用当前用户 `~/Library/LaunchAgents/com.agent-postoffice.officed.plist` 中的 Office 服务入口。以后需要使用时，可按安装说明重新启用。
- **移除宿主接入**：在各宿主配置中只移除 Office 自己的入口，保留其他插件、工具和设置。安装器涉及 Codex 的 MCP `office` 与用户级 hooks、Claude Code 的 `settings.json` 和 `office-plugin`、Pi 的 `extensions/office-tools.ts`、OpenCode 的 `plugins/office-postoffice/index.ts`，以及 `~/.local/bin/office-cli`。若使用了 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`PI_CODING_AGENT_DIR` 或 `XDG_CONFIG_HOME`，这些入口位于对应的自定义目录。确认条目确属本次 Office 安装后再移除；重启或重载宿主以关闭旧连接。
- **备份不是一键覆盖**：先对照 `backups/install-*/paths.json` 和当前文件；安装后若你改过宿主配置，只恢复或删除 Office 相关项，勿用旧快照整份覆盖，避免丢失后来修改。
- **删除业务数据是另一步**：停止服务或移除插件都不会删除 Office home 里的数据库、信件、绑定和历史。只有确认不再需要、并另有可用备份时，才自行删除所选 Office home 中的数据；项目工作区文件也在映射指向的目录中，需单独决定是否保留。
