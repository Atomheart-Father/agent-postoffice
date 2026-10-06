#!/bin/bash
# agent-postoffice 一键安装。可重复运行；改动过的配置文件都会先备份到 ~/agent-postoffice/logs/。
# 用法：./install.sh            安装全部能装的部件
#       ./install.sh --no-postman   不装开机自启的邮递员
set -e
cd "$(dirname "$0")"
PO="$PWD/postoffice"
chmod +x "$PO"
command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }

"$PO" init

if [ -d "$HOME/.claude" ] || command -v claude >/dev/null; then
  "$PO" install claude
else
  echo "未发现 Claude Code，跳过钩子（装好后运行：postoffice install claude）"
fi
if [ -d "$HOME/.config/opencode" ] || command -v opencode >/dev/null || [ -d /Applications/OpenCode.app ]; then
  # 这里不要自己 ln。链接归 `postoffice install opencode` 管，它在同一个事务里先换链接、再复制
  # 插件，复制失败会把链接撤回。install.sh 若在这儿提前 ln，一旦后面的插件复制失败，就会留下
  # link=本 checkout + plugin=上一份 checkout 的错配 —— 正是 install opencode 内部那套事务要防的。
  "$PO" install opencode
else
  # 没有 OpenCode 就没有插件复制这一步，也就不存在事务问题：PATH 便利链接照旧直接建。
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$PO" "$HOME/.local/bin/postoffice"
  echo "命令已链接：$HOME/.local/bin/postoffice"
  echo "未发现 OpenCode，跳过插件（装好后运行：postoffice install opencode）"
fi
"$PO" install skill
if [ "$1" != "--no-postman" ]; then "$PO" install postman; fi

cat <<'MSG'

✅ 安装完成。接下来：
1. 重启 Claude 桌面 App 和 OpenCode（让钩子和插件生效）。
2. 给每个会话登记一个信箱（会话标题要先在 App 里起好名字）：
     postoffice add boss   --claude   "我的Claude会话标题"  --who "总负责"
     postoffice add coder  --opencode "我的OpenCode会话标题" --who "写代码"
     postoffice add codex1 --codex    <Codex线程ID>          --who "Codex"
3. 体检：postoffice doctor
4. 让各会话知道邮局：直接对它说“用 postoffice 技能给 coder 发封信”即可。
MSG
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "提示：把 ~/.local/bin 加进 PATH，或用完整路径 $PO";; esac
