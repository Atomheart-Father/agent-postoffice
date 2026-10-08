# 组织与身份（organization / identity）v1 契约

一版把「谁负责什么」变成**可查询的事实**：一条经过校验的配置关系，一个推导出来的身份，
名片 / 面板 / 三条 harness 查询共用同一份结果。**没有**数据库、后台进程、第二份路由或
「当前负责人」缓存。物理信箱、普通投递、逻辑地址切换的既有行为一律不变。

## 术语（与 design/GLOSSARY.md 一致）

- **信箱（Mailbox）**：固定的物理收信身份，就是 `routes.json` 里的信箱名。换会话不改信箱、
  不改信件归属、不改公司成员与候选资格。
- **绑定（Binding）**：这个信箱当前把提醒投到哪个会话/通道。它**不是**标题，也不是「主人」。
- **公司（Company）**：共同工作规章 + 正式成员。
- **项目（Project）**：公司里的一个工作范围，带唯一的当前进度文件（STATUS）。
- **角色（Role）**：一个带职责名与稳定联系地址的工作位置，接管人可以变；候选顺序只有一处。
- **成员 / 候选 / 现职**：三个不同关系。候选不等于「现在归你」，现职才是这个地址此刻的受理人。
- **名片（Contact Card）**：信箱联系资料的呈现；一段自由备注不会创造角色或任命。

## 配置结构（`config.json`，version 2）

```json
{
  "version": 2,
  "groups": { "team": ["dev", "reviewer"] },
  "aliases": {
    "postoffice.owner": {
      "candidates": ["dev", "backup"],
      "role": { "scope": { "kind": "project", "id": "postoffice" }, "title": "开发" }
    },
    "postoffice.review": {
      "candidates": ["reviewer"],
      "role": { "scope": { "kind": "project", "id": "postoffice" }, "title": "邮局审核" }
    }
  },
  "organization": {
    "companies": {
      "boxz": { "name": "盒子", "rules_file": "rules.md", "members": ["dev", "reviewer", "backup"] }
    },
    "projects": {
      "postoffice": {
        "company_id": "boxz", "name": "邮局",
        "root": "/Users/you/work/postoffice", "status_file": "STATUS.md",
        "members": ["dev", "reviewer", "backup"]
      }
    }
  }
}
```

规则（导入时整份校验，任一处不合法则**拒绝整份、旧的 `config.json` 一个字节都不动**）：

- `version` 只能是 `1` 或 `2`。`organization` 只允许出现在 version 2。
- 公司：`name`、`rules_file`（字符串）、`members`（非空字符串数组）。
- 项目：`company_id` 必须指向已存在的公司；`name`、`root`、`status_file`；`members` **必须都属于该公司**。
- 成员、候选、角色引用必须是**已登记**的物理信箱。
- 角色（`aliases.<名>.role`，version 2 才允许）：
  `scope.kind ∈ {company, project}`，`scope.id` 必须指向已存在的公司/项目，`title` 非空；
  **项目范围的角色不许自带 `handoff_file`**（交接来自项目唯一的 `status_file`），
  公司范围的角色可以显式给 `handoff_file`。
- 老的、没有 `role` 的 alias（列表式或 `{candidates,notify,handoff}`）继续照旧工作。

## 命令

```bash
postoffice identity <信箱>            # 人读快照：绑定 / 成员 / 现职 / 可候选 / 公司规章 / 项目进度 / AS OF / 指纹
postoffice identity <信箱> --json     # 机器可读的同一份结果
postoffice add <信箱> --display-name 名字 --kind agent --desc "一句话"
                                      # 已登记信箱不给通道 = 只改资料：不碰绑定、不碰收信方式
postoffice add <信箱> --claude 标题 --claude-session local_xxx --source "谁改的"
                                      # 给通道 = 显式改绑：记一条交接（logs/rebind.log）
postoffice rebind <信箱> --claude ...  # 与上面等价、更直白的入口
```

- `identity` 是**快照**，读一份 routes + 一份 config；`active` 复用发信那一刻的同一个
  `alias_target`。发信永远按当时的路由重新解析，查询结果不代表下一次发信的落点。
- 收到别人的信、要判断「这是不是我的活」时，**先查一次自己的 `identity`**；交接信里写的旧职责会过期。
- 组织配置无效时，`identity` 明确报「已停用」，物理收发信照常，且不会捏造职责。

## 旧配置迁移 / 回滚

1. **不想用组织身份**：什么都不用做。version 1 的配置语义完全不变，`identity` 只显示物理事实。
2. **迁移到 version 2**：把 `"version"` 改成 `2`，按需加 `organization`；老 alias 原样保留。
   用 `postoffice config import new.json` 导入——导入前会整份校验并备份旧文件到
   `logs/config.json.<时间戳>.bak`。
3. **回滚**：把 `logs/config.json.<时间戳>.bak` 覆盖回 `config.json`（或重新 import 你留的旧文件）；
   再跑一次 `postoffice config show` 确认。导入是**整文件替换**，从不合并，所以回滚是干净的一步。

## 三条 harness 接缝 / 首次读取约定

身份只在**注册、恢复、主动查询**时出现，普通信里从不夹带公司规章或岗位清单。

- **注册**（三 harness 共用）：`postoffice add` / `rebind` 登记后打印一行短身份
  （现职 / 候选 / 规章与进度指针），这是最可靠的公共接缝。
- **Claude Code 恢复**：`install claude` 在 `SessionStart` 组里加一条 `postoffice identity --hint`
  钩子，把短身份行作为附加上下文；解析不到唯一 claude_hook 信箱时**不输出**，退回 skill 主动查询。
- **OpenCode 恢复**：插件在**首次**看到某会话生命周期事件时运行 `identity <box> --hint` 并注入一次
  （`hintTried` 去重）；无组织配置时输出为空、行为与从前完全一致。
- **Codex**：没有会话启动/恢复钩子（`~/.codex/config.toml` 只有 `notify=turn-ended`），所以接缝就是
  注册 + 主动 `identity` 查询——这是真实限制，不是「未实现」。
- **首次读取约定**：规章 / STATUS 首次用到时读一次，之后只在文件变化时重读；邮局不每封信重发。
- 三个 harness 的身份解释来自**同一个** CLI 结果，呈现方式各自不同。

模板见 [ORG_CONTRACT_EXAMPLE.md](ORG_CONTRACT_EXAMPLE.md)（公司规章、项目 STATUS、票角色引用示例）。

## 展示记录的 Binding 归属：按票报告真实冲突

原票要求「新展示的记录关联当时的 Binding，旧记录没有就是 unknown」。当前
`<box>/.presented.json` 是一份**裸信件 id 的扁平列表**（`read_presented` / `presented_update` /
`bare_presented_id`），`archive-current` 的「本会话已展示范围」直接依赖它。要按记录存 Binding
必须把它改成对象列表，这会改动 `archive-current` 的展示范围语义——票里明确说这种情形**报告冲突、
不要强改**。因此本版**不动** `.presented.json` 格式，如实记录这一冲突；需要时另开票设计带 Binding
的展示记录。

## 边界（这版不做什么）

不做信头 `need` 枚举迁移、全局清单索引、数据老化、额度探测、RBAC、签名、任务审批引擎、
组织权限继承、第二套路由或新后台进程。也不替别的项目就地改 `AGENTS.md` / `CLAUDE.md`；
真实规章文件与各会话的就地校准留给用户安排的停机窗口。
