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
mkdir -p "$HOME/.local/bin"
ln -sfn "$PO" "$HOME/.local/bin/postoffice"
echo "命令已链接：~/.local/bin/postoffice"

if [ -d "$HOME/.claude" ] || command -v claude >/dev/null; then
  "$PO" install claude
else
  echo "未发现 Claude Code，跳过钩子（装好后运行：postoffice install claude）"
fi
if [ -d "$HOME/.config/opencode" ] || command -v opencode >/dev/null || [ -d /Applications/OpenCode.app ]; then
  "$PO" install opencode
else
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
