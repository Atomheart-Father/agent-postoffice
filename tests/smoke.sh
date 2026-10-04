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
n=$(grep -c '" hook' "$T/.claude/settings.json"); grep -q '"timeout": 604800' "$T/.claude/settings.json" || n=0; other=$(grep -c "echo other" "$T/.claude/settings.json")
[ "$n" -eq 2 ] && [ "$other" -eq 1 ] && ok "装钩子可重复运行且保留别人的钩子" || bad "装钩子 n=$n other=$other"
"$PO" doctor 2>/dev/null | grep -q "✅ Claude Code 收信钩子" && ok "体检认出已装的钩子" || bad "体检误报钩子未装"
"$PO" uninstall claude >/dev/null
! grep -q '" hook' "$T/.claude/settings.json" && grep -q "echo other" "$T/.claude/settings.json" && ok "卸钩子只卸自己的" || bad "卸钩子"

echo "x" | "$POSTOFFICE_HOME/bin/send.sh" bob alice "兼容旧命令" "仅告知" >/dev/null && ok "bin/send.sh 兼容" || bad "send.sh 兼容"
"$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM
grep -q "兼容旧命令" "$POSTOFFICE_HOME/.delivered.json" 2>/dev/null || grep -q "bob" "$POSTOFFICE_HOME/.delivered.json" && ok "邮递员投递通知信箱并记账" || bad "邮递员"
"$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM
[ $(grep -c "notify bob" "$POSTOFFICE_HOME/logs/postman.log") -eq 1 ] && ok "邮递员重启不重投" || bad "邮递员重投"

printf '#!/bin/sh\nexit 0\n' > "$T/fakecodex"; chmod +x "$T/fakecodex"   # 假 codex：queue 永远“受理成功”
"$PO" add carol --codex thread-1 >/dev/null
python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r['carol']['codex_cli']=sys.argv[2];json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$T/fakecodex"
echo "x" | "$PO" send carol alice "没人处理的信" "仅告知" >/dev/null
POSTOFFICE_POLL=1 POSTOFFICE_CONSUME_ALERT=2 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 6; kill $PM
[ $(grep -c "未处理 carol" "$POSTOFFICE_HOME/logs/postman.log") -eq 1 ] && ok "提醒后久未处理只告诉人一次" || bad "未处理提醒"
mv "$POSTOFFICE_HOME"/carol/inbox/*.md "$POSTOFFICE_HOME/carol/done/"
POSTOFFICE_POLL=1 POSTOFFICE_CONSUME_ALERT=2 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM
! grep -q carol "$POSTOFFICE_HOME/.woken.json" && ok "信挪进 done 即算处理完" || bad "处理完未清账"

"$PO" offline alice >/dev/null; echo "x" | "$PO" send alice bob "积压待清" "仅告知" >/dev/null
"$PO" clear alice >/dev/null
[ -z "$(ls "$POSTOFFICE_HOME/alice/inbox/"*.md 2>/dev/null)" ] && ls "$POSTOFFICE_HOME"/alice/archived/*/*积压待清*.md >/dev/null 2>&1 && ok "清空积压：存档不删、不再投递" || bad "清空积压"

# ---------- ack 记账与 broadcast 广播（docs/SPEC_broadcast_ack.md） ----------
"$PO" online alice >/dev/null
"$PO" add dave --notify >/dev/null
"$PO" add erin --notify >/dev/null

# 1. ack 一封普通信：写账、信挪到 done、不产生给原发信方的新信
bob_before=$(ls "$POSTOFFICE_HOME/bob/inbox/"*.md 2>/dev/null | wc -l | tr -d ' ')
out=$(echo "正文" | "$PO" send alice bob "需要回执的信" "回复")
lid=$(printf '%s\n' "$out" | awk -F'：' '/^编号/{print $2}')
[ -n "$lid" ] && ok "send 打印信件编号" || bad "send 编号"
"$PO" ack alice "$lid" "收到" >/dev/null && ok "ack 普通信记账" || bad "ack 普通信"
grep -q "\"id\": \"$lid\"" "$POSTOFFICE_HOME/acks.jsonl" && grep -q "\"to\": \"bob\"" "$POSTOFFICE_HOME/acks.jsonl" \
  && ok "ack 账目含编号与发信方" || bad "ack 账目"
[ ! -e "$POSTOFFICE_HOME/alice/inbox/$lid.md" ] && [ -e "$POSTOFFICE_HOME/alice/done/$lid.md" ] \
  && ok "ack 把信挪到 done" || bad "ack 挪信"
bob_after=$(ls "$POSTOFFICE_HOME/bob/inbox/"*.md 2>/dev/null | wc -l | tr -d ' ')
[ "$bob_after" -eq "$((bob_before+1))" ] && ok "ack 产生一封无需答复的回执通知" || bad "ack 回执通知"

# 2. ack --wake：原发信方 inbox 多一封 copy that
out=$(echo "正文" | "$PO" send alice bob "需要回执的信2" "回复")
lid2=$(printf '%s\n' "$out" | awk -F'：' '/^编号/{print $2}')
"$PO" ack alice "$lid2" "办好了" --wake >/dev/null
grep -lq "事由：copy that：需要回执的信2" "$POSTOFFICE_HOME/bob/inbox/"*.md 2>/dev/null \
  && ok "ack --wake 补发 copy that" || bad "ack --wake"

# 3. 重复 ack 幂等；ack 不存在的编号或别人的信报错、不写账
ackn=$(wc -l < "$POSTOFFICE_HOME/acks.jsonl" | tr -d ' ')
out=$("$PO" ack alice "$lid2" 2>&1)
[ $? -eq 0 ] && printf '%s' "$out" | grep -q "已回执过" \
  && [ "$(wc -l < "$POSTOFFICE_HOME/acks.jsonl" | tr -d ' ')" = "$ackn" ] \
  && ok "重复 ack 幂等" || bad "重复 ack"
"$PO" ack alice "19000101-000000_nobody_无此信" >/dev/null 2>&1 \
  && bad "ack 不存在编号应报错" || { [ "$(wc -l < "$POSTOFFICE_HOME/acks.jsonl" | tr -d ' ')" = "$ackn" ] \
  && ok "ack 不存在编号报错且不写账" || bad "ack 不存在编号却写账"; }
other=$(basename "$(grep -l "copy that：需要回执的信2" "$POSTOFFICE_HOME/bob/inbox/"*.md)" .md)
"$PO" ack dave "$other" >/dev/null 2>&1 && bad "ack 别人的信应报错" \
  || { [ "$(wc -l < "$POSTOFFICE_HOME/acks.jsonl" | tr -d ' ')" = "$ackn" ] \
  && ok "ack 不是发给我的信报错且不写账" || bad "ack 别人的信却写账"; }

# 4. broadcast 给 3 个信箱（其中 erin 离线）：各自 inbox 一封、第 4 行带广播编号
"$PO" offline erin >/dev/null
out=$(printf '广播正文\n' | "$PO" broadcast bob,dave,erin alice "全员通知" "仅告知")
bid=$(printf '%s\n' "$out" | awk -F'：' '/^广播编号/{print $2}')
[ -n "$bid" ] && [ -e "$POSTOFFICE_HOME/broadcasts/$bid.json" ] \
  && ok "broadcast 生成编号与记录" || bad "broadcast 编号"
hit=0
for bx in bob dave erin; do
  grep -lq "^广播：$bid$" "$POSTOFFICE_HOME/$bx/inbox/"*.md 2>/dev/null && hit=$((hit+1))
done
[ "$hit" -eq 3 ] && ok "broadcast 给 3 个信箱各投一封（含离线）" || bad "broadcast 投递 hit=$hit"
grep -q "回执请用：postoffice ack dave $bid" "$POSTOFFICE_HOME/dave/inbox/"*.md 2>/dev/null \
  && ok "广播信正文附 ack 指引" || bad "广播 ack 指引"

# 5. 2 人 ack 后跑邮递员：未到截止、未全员 → 不汇总；全员 ack → 恰好一封；再跑不重复
"$PO" ack bob "$bid" "收到一号" >/dev/null
"$PO" ack dave "$bid" "收到二号" >/dev/null
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null
! grep -lq "广播汇总" "$POSTOFFICE_HOME/alice/inbox/"*.md 2>/dev/null \
  && ok "未到截止未全员：不汇总" || bad "提前汇总"
"$PO" ack erin "$bid" "收到三号" >/dev/null
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null
sum=$(grep -l "广播汇总" "$POSTOFFICE_HOME/alice/inbox/"*.md 2>/dev/null | wc -l | tr -d ' ')
[ "$sum" -eq 1 ] && ok "全员 ack 后发信方收到恰好一封汇总" || bad "汇总封数 sum=$sum"
sf=$(grep -l "广播汇总" "$POSTOFFICE_HOME/alice/inbox/"*.md 2>/dev/null)
grep -q "已回执 3/3" $sf && grep -q "收到一号" $sf && grep -q "收到三号" $sf \
  && ok "汇总内容含全员与各自的一句话" || bad "汇总内容"
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null
[ "$(grep -l "广播汇总" "$POSTOFFICE_HOME/alice/inbox/"*.md 2>/dev/null | wc -l | tr -d ' ')" -eq 1 ] \
  && ok "邮递员重启不重复汇总" || bad "重复汇总"

# 6. 截止时间到但有人没回：汇总列出未回执的人（环境变量把截止调成秒级）
out=$(printf '过期正文\n' | POSTOFFICE_BROADCAST_DEADLINE=1 "$PO" broadcast dave,erin bob "过期广播" "仅告知")
bid2=$(printf '%s\n' "$out" | awk -F'：' '/^广播编号/{print $2}')
"$PO" ack dave "$bid2" "我回了" >/dev/null
sleep 2
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null
s2=$(grep -l "广播汇总" "$POSTOFFICE_HOME/bob/inbox/"*.md 2>/dev/null)
grep -q "dave：我回了" $s2 && grep -q "erin：未回执" $s2 \
  && ok "截止后汇总列出已回执与未回执" || bad "截止汇总内容"

# 7. 发信方校验 + 坏广播记录不能搞挂邮递员（审核退回的必修 bug）
before_bc=$(ls "$POSTOFFICE_HOME/broadcasts/"*.json 2>/dev/null | wc -l | tr -d ' ')
echo x | "$PO" broadcast bob ghost "坏发信方" "仅告知" >/dev/null 2>&1 && bad "未登记发信方应报错" \
  || ok "broadcast 校验发信方已登记"
[ "$(ls "$POSTOFFICE_HOME/broadcasts/"*.json 2>/dev/null | wc -l | tr -d ' ')" = "$before_bc" ] \
  && ok "校验失败不建广播记录" || bad "校验失败却建了记录"
python3 - "$POSTOFFICE_HOME" <<'PY'
import json, sys, time
from pathlib import Path
h = Path(sys.argv[1])
(h / "broadcasts").mkdir(exist_ok=True)
(h / "broadcasts" / "B20260101-000000_ghost.json").write_text(json.dumps({
    "id": "B20260101-000000_ghost", "from": "ghost", "subject": "坏记录", "need": "仅告知",
    "to": ["bob"], "deadline": time.time() - 1, "created": "x", "acks": {}, "summarized": False}))
PY
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3
kill -0 $PM 2>/dev/null && ok "坏广播记录不搞挂邮递员" || bad "邮递员被坏记录搞挂"
grep -q "广播汇总失败 B20260101-000000_ghost.json" "$POSTOFFICE_HOME/logs/postman.log" \
  && ok "坏记录写进日志并通知人" || bad "坏记录无日志"
grep -q '"summarized": "error"' "$POSTOFFICE_HOME/broadcasts/B20260101-000000_ghost.json" \
  && ok "坏记录标记为已处理（不再重试）" || bad "坏记录未标记"
kill $PM 2>/dev/null; wait $PM 2>/dev/null
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null; wait $PM 2>/dev/null
[ "$(grep -c "广播汇总失败 B20260101-000000_ghost.json" "$POSTOFFICE_HOME/logs/postman.log")" -eq 1 ] \
  && ok "坏记录不重复重试" || bad "坏记录重复重试"

rm -rf "$T"
echo "通过 ${pass}，失败 ${fail}"; [ $fail -eq 0 ]
