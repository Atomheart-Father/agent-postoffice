#!/bin/bash
# 冒烟测试：全部在临时目录里跑，不碰真实的 ~/.claude、~/agent-postoffice。
set -u
PO="$(cd "$(dirname "$0")/.." && pwd)/postoffice"
T=$(mktemp -d); export HOME="$T" POSTOFFICE_HOME="$T/po" POSTOFFICE_NO_NOTIFY=1
pass=0; fail=0
ok()  { echo "✅ $1"; pass=$((pass+1)); }
bad() { echo "❌ $1"; fail=$((fail+1)); }
hook_in() { echo "{\"transcript_path\":\"$T/$1.jsonl\"}"; }
echo '{"type":"custom-title","customTitle":"会话A"}' > "$T/a.jsonl"
echo '{"type":"custom-title","customTitle":"陌生会话"}' > "$T/x.jsonl"

"$PO" init >/dev/null
"$PO" add alice --claude "会话A" --who "测试A" >/dev/null && ok "登记 Claude 信箱" || bad "登记 Claude 信箱"
"$PO" add bob --notify >/dev/null && ok "登记通知信箱" || bad "登记通知信箱"

echo "正文" | "$PO" send alice bob "这是一个很长很长很长的中文事由，包含/斜杠:冒号" "仅告知" >/dev/null
ls "$POSTOFFICE_HOME/alice/inbox/"*.md >/dev/null 2>&1 && ok "发信（长中文事由）" || bad "发信"

out=$(hook_in a | "$PO" hook 2>&1); rc=$?
[ $rc -eq 2 ] && echo "$out" | grep -q "事由：这是一个" && ok "钩子唤醒并带开头三行" || bad "钩子唤醒 rc=$rc"

hook_in a | "$PO" hook 2>/dev/null & P=$!; sleep 12
kill -0 $P 2>/dev/null && ok "同一封信不重报" || bad "重复报信"
hook_in a | "$PO" hook 2>/dev/null & P2=$!; sleep 2
kill -0 $P 2>/dev/null && bad "旧监视未退场" || ok "新监视接班旧监视"
kill $P2 2>/dev/null; wait 2>/dev/null

[ $(hook_in x | "$PO" hook; echo $?) -eq 0 ] && ok "未登记会话直接退出" || bad "未登记会话"

"$PO" offline alice >/dev/null
echo "离线期间的信" | "$PO" send alice bob "离线测试" "仅告知" >/dev/null
hook_in a | "$PO" hook 2>"$T/off.err" & P=$!; sleep 12
kill -0 $P 2>/dev/null && ok "离线时不唤醒" || bad "离线时被唤醒"
"$PO" online alice >/dev/null; sleep 12
kill -0 $P 2>/dev/null && bad "上线后没补送" || { grep -q "离线测试" "$T/off.err" && ok "上线后补送" || bad "补送内容不对"; }

mkdir -p "$T/.claude"; echo '{"theme":"dark","hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo other"}]}]}}' > "$T/.claude/settings.json"
"$PO" install claude >/dev/null; "$PO" install claude >/dev/null
n=$(grep -c '" hook' "$T/.claude/settings.json"); other=$(grep -c "echo other" "$T/.claude/settings.json")
[ "$n" -eq 2 ] && [ "$other" -eq 1 ] && ok "装钩子可重复运行且保留别人的钩子" || bad "装钩子 n=$n other=$other"
"$PO" doctor 2>/dev/null | grep -q "✅ Claude Code 收信钩子" && ok "体检认出已装的钩子" || bad "体检误报钩子未装"
"$PO" uninstall claude >/dev/null
! grep -q '" hook' "$T/.claude/settings.json" && grep -q "echo other" "$T/.claude/settings.json" && ok "卸钩子只卸自己的" || bad "卸钩子"

echo "x" | "$POSTOFFICE_HOME/bin/send.sh" bob alice "兼容旧命令" "仅告知" >/dev/null && ok "bin/send.sh 兼容" || bad "send.sh 兼容"
"$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM
grep -q "兼容旧命令" "$POSTOFFICE_HOME/.delivered.json" 2>/dev/null || grep -q "bob" "$POSTOFFICE_HOME/.delivered.json" && ok "邮递员投递通知信箱并记账" || bad "邮递员"
"$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM
[ $(grep -c "notify bob" "$POSTOFFICE_HOME/logs/postman.log") -eq 1 ] && ok "邮递员重启不重投" || bad "邮递员重投"

rm -rf "$T"
echo "通过 $pass，失败 $fail"; [ $fail -eq 0 ]
