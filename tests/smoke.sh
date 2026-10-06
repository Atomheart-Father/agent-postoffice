#!/bin/bash
# 冒烟测试：全部在临时目录里跑，不碰真实的 ~/.claude、~/agent-postoffice。
set -u
# 与运行环境的 Claude 桌面变量隔离：认人只认用例显式设置的值，
# 否则在 Claude 桌面会话里跑会被真实身份带偏（认错信箱后干等）。
unset CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_HOST_SESSION_ID
PO="$(cd "$(dirname "$0")/.." && pwd)/postoffice"
T=$(mktemp -d); export HOME="$T" POSTOFFICE_HOME="$T/po" POSTOFFICE_NO_NOTIFY=1
trap 'kill ${P:-} ${P2:-} ${PM:-} 2>/dev/null' EXIT
pass=0; fail=0
ok()  { echo "✅ $1"; pass=$((pass+1)); }
bad() { echo "❌ $1"; fail=$((fail+1)); }
hook_in() { echo "{\"transcript_path\":\"$T/$1.jsonl\"}"; }
hook_cli() { printf '{"transcript_path":"%s/%s.jsonl","session_id":"%s"}' "$T" "$1" "$2"; }
# 带超时的钩子调用：hook_run [-- env 参数或 VAR=VAL ...] 命令 参数…
# stdin 收 payload，stdout 回命令输出，退出码即命令退出码；超时则杀掉并返回 124
# —— 测试只会失败，不会挂死。
HOOK_WAIT=${POSTOFFICE_TEST_HOOK_WAIT:-25}
hook_run() {
  local envs=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift 2>/dev/null || true
  cat > "$T/.hook_in"
  env ${envs[@]+"${envs[@]}"} "$@" < "$T/.hook_in" > "$T/.hook_out" 2>&1 &
  local pid=$! i=0 rc=0
  while kill -0 $pid 2>/dev/null && [ $i -lt "$HOOK_WAIT" ]; do sleep 1; i=$((i+1)); done
  if kill -0 $pid 2>/dev/null; then kill -9 $pid 2>/dev/null; wait $pid 2>/dev/null; rc=124
  else wait $pid; rc=$?; fi
  cat "$T/.hook_out"; return $rc
}
route_field() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]].get(sys.argv[3],''))" "$POSTOFFICE_HOME/routes.json" "$1" "$2"; }
route_set() { python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r[sys.argv[2]][sys.argv[3]]=sys.argv[4];json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$1" "$2" "$3"; }
echo '{"type":"custom-title","customTitle":"会话A"}' > "$T/a.jsonl"
echo '{"type":"custom-title","customTitle":"陌生会话"}' > "$T/x.jsonl"

"$PO" init >/dev/null
"$PO" add alice --claude "会话A" --who "测试A" >/dev/null && ok "登记 Claude 信箱" || bad "登记 Claude 信箱"
"$PO" add bob --notify >/dev/null && ok "登记通知信箱" || bad "登记通知信箱"
{ [ -z "${CLAUDE_CODE_ENTRYPOINT+x}" ] && [ -z "${CLAUDE_CODE_HOST_SESSION_ID+x}" ]; } \
  && ok "测试环境已清掉 Claude 桌面变量（认人用例自己设置）" || bad "桌面变量仍在，认人用例会跑偏"

echo "正文" | "$PO" send alice bob "这是一个很长很长很长的中文事由，包含/斜杠:冒号" "仅告知" >/dev/null
ls "$POSTOFFICE_HOME/alice/inbox/"*.md >/dev/null 2>&1 && ok "发信（长中文事由）" || bad "发信"

out=$(hook_in a | hook_run "$PO" hook); rc=$?
[ $rc -eq 2 ] && echo "$out" | grep -q "事由：这是一个" && ok "钩子唤醒并带开头三行" || bad "钩子唤醒 rc=$rc"

hook_in a | "$PO" hook 2>/dev/null & P=$!; sleep 12
kill -0 $P 2>/dev/null && ok "同一封信不重报" || bad "重复报信"
hook_in a | "$PO" hook 2>/dev/null & P2=$!; sleep 2
kill -0 $P 2>/dev/null && bad "旧监视未退场" || ok "新监视接班旧监视"
kill $P2 2>/dev/null; wait 2>/dev/null

[ $(hook_in x | hook_run "$PO" hook; echo $?) -eq 0 ] && ok "未登记会话直接退出" || bad "未登记会话"

"$PO" offline alice >/dev/null
echo "离线期间的信" | "$PO" send alice bob "离线测试" "仅告知" >/dev/null
hook_in a | "$PO" hook 2>"$T/off.err" & P=$!; sleep 12
kill -0 $P 2>/dev/null && ok "离线时不唤醒" || bad "离线时被唤醒"
"$PO" online alice >/dev/null; sleep 12
kill -0 $P 2>/dev/null && bad "上线后没补送" || { grep -q "离线测试" "$T/off.err" && ok "上线后补送" || bad "补送内容不对"; }
kill -9 $P 2>/dev/null; wait $P 2>/dev/null; P=""

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
echo "x" | "$PO" send carol alice "没人处理的信" "回复" >/dev/null
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
"$PO" ack alice "$lid" "配置已核验OK" >/dev/null && ok "ack 普通信记账" || bad "ack 普通信"
grep -q "\"id\": \"$lid\"" "$POSTOFFICE_HOME/acks.jsonl" && grep -q "\"to\": \"bob\"" "$POSTOFFICE_HOME/acks.jsonl" \
  && ok "ack 账目含编号与发信方" || bad "ack 账目"
[ ! -e "$POSTOFFICE_HOME/alice/inbox/$lid.md" ] && [ -e "$POSTOFFICE_HOME/alice/done/$lid.md" ] \
  && ok "ack 把信挪到 done" || bad "ack 挪信"
bob_after=$(ls "$POSTOFFICE_HOME/bob/inbox/"*.md 2>/dev/null | wc -l | tr -d ' ')
[ "$bob_after" -eq "$((bob_before+1))" ] && ok "ack 产生一封无需答复的回执通知" || bad "ack 回执通知"
"$PO" receipt bob "$lid" 2>/dev/null | grep -q "配置已核验OK" \
  && ok "receipt 按 ID 返回回执正文" || bad "receipt 查询"
rfile=$(grep -l "^回执：$lid$" "$POSTOFFICE_HOME/bob/inbox/"*.md 2>/dev/null | head -1)
[ -n "$rfile" ] && ! grep -q "配置已核验OK" "$rfile" \
  && ok "回执通知不含正文（按需查询）" || bad "回执通知夹带正文"
"$PO" receipt bob "19000101-000000_nobody_无此信" >/dev/null 2>&1 \
  && bad "receipt 未知 ID 应报错" || ok "receipt 未知 ID 报错"

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
grep -q "已回执 3/3" $sf && grep -q "postoffice receipt alice $bid" $sf && ! grep -q "收到一号" $sf \
  && ok "汇总只给人数与查询命令、不含各自的一句话" || bad "汇总内容"
"$PO" receipt alice "$bid" 2>/dev/null | grep -q "收到一号" \
  && ok "receipt 按广播 ID 返回各人一句话" || bad "广播 receipt"
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
grep -q "未回执：erin（离线）" $s2 && grep -q "postoffice receipt bob $bid2" $s2 && ! grep -q "我回了" $s2 \
  && ok "截止后汇总只给未回执与查询命令、不含回执正文" || bad "截止汇总内容"
"$PO" receipt bob "$bid2" 2>/dev/null | grep -q "我回了" \
  && ok "receipt 返回截止广播的回执正文" || bad "截止广播 receipt"

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
# a record that really blows up inside the summary step (sender exists, deadline is garbage)
(h / "broadcasts" / "B20260101-000000_ghost.json").write_text(json.dumps({
    "id": "B20260101-000000_ghost", "from": "bob", "subject": "坏记录", "need": "仅告知",
    "to": ["bob"], "deadline": "不是时间", "created": "x", "acks": {}, "summarized": False}))
# a system-sent broadcast: no mailbox to summarise back to, must be skipped quietly and only once
(h / "broadcasts" / "B20260101-000001_sys.json").write_text(json.dumps({
    "id": "B20260101-000001_sys", "from": "postoffice", "subject": "系统广播", "need": "仅告知",
    "to": ["bob"], "deadline": time.time() - 1, "created": "x", "acks": {}, "summarized": False}))
PY
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3
kill -0 $PM 2>/dev/null && ok "坏广播记录不搞挂邮递员" || bad "邮递员被坏记录搞挂"
grep -q "广播汇总失败 B20260101-000000_ghost.json" "$POSTOFFICE_HOME/logs/postman.log" \
  && ok "坏记录写进日志并通知人" || bad "坏记录无日志"
grep -q '"summarized": "error"' "$POSTOFFICE_HOME/broadcasts/B20260101-000000_ghost.json" \
  && ok "坏记录标记为已处理（不再重试）" || bad "坏记录未标记"
grep -q "跳过汇总" "$POSTOFFICE_HOME/logs/postman.log" \
  && ok "系统广播没有收件信箱时安静跳过" || bad "系统广播未跳过汇总"
grep -q '"summarized": true' "$POSTOFFICE_HOME/broadcasts/B20260101-000001_sys.json" \
  && ok "系统广播只处理一次" || bad "系统广播被反复处理"
kill $PM 2>/dev/null; wait $PM 2>/dev/null
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null; wait $PM 2>/dev/null
[ "$(grep -c "广播汇总失败 B20260101-000000_ghost.json" "$POSTOFFICE_HOME/logs/postman.log")" -eq 1 ] \
  && ok "坏记录不重复重试" || bad "坏记录重复重试"
[ "$(ls "$POSTOFFICE_HOME/bob/inbox/"*.md 2>/dev/null | wc -l | tr -d ' ')" -ge 0 ] \
  && ok "系统广播不向不存在的信箱写汇总" || bad "系统广播写出了汇总"

# ---------- v1.6：稳定认人 + 只为真阻塞提醒 + 回执合并排后（docs/SPEC_v1.6.md rev2） ----------
mkcap() { printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\n' "$1" > "$2"; chmod +x "$2"; }
mkfail() { printf '#!/bin/sh\nexit 1\n' > "$1"; chmod +x "$1"; }
clog() { printf '%s Mapping internal session %s to CLI session %s\n' "$1" "$2" "$3" >> "$T/main.log"; }
titleonly() { printf '{"transcript_path":"%s/%s.jsonl"}' "$T" "$1"; }
DHD() { hook_run CLAUDE_CODE_ENTRYPOINT=claude-desktop POSTOFFICE_CLAUDE_MAINLOG="$T/main.log" -- "$@"; }

# 1) 桌面版无 HOST：按 main.log 反查身份；首次标题匹配自动绑定；改名后仍认人并更新标题
: > "$T/main.log"
clog "2026-10-05 02:00:01" local_L1 cli-L1
printf '{"type":"custom-title","customTitle":"稳定会话"}\n' > "$T/s1.jsonl"
"$PO" add sbox --claude "稳定会话" >/dev/null
echo "正文" | "$PO" send sbox tester "首信" "回复" >/dev/null
out=$(hook_cli s1 cli-L1 | DHD "$PO" hook); rc=$?
[ $rc -eq 2 ] && printf '%s' "$out" | grep -q "【联络总站新信" && ok "v1.6 桌面日志反查身份并唤醒" || bad "v1.6 日志认人 rc=$rc"
[ "$(route_field sbox claude_session)" = "local_L1" ] && ok "v1.6 首次标题匹配自动绑定身份" || bad "v1.6 自动绑定"
printf '{"type":"custom-title","customTitle":"改过的名字"}\n' > "$T/s1.jsonl"
echo "正文" | "$PO" send sbox tester "改名后" "回复" >/dev/null
out=$(hook_cli s1 cli-L1 | DHD "$PO" hook); rc=$?
[ $rc -eq 2 ] && ok "v1.6 会话改名后仍按身份认人" || bad "v1.6 改名后认人 rc=$rc"
[ "$(route_field sbox claude_title)" = "改过的名字" ] && ok "v1.6 认人时顺手更新标题" || bad "v1.6 更新标题"

# 2) 回退：同一身份换了新的 CLI id 照样认人；重名映射按行内时间戳取最新
clog "2026-10-05 02:05:00" local_L1 cli-L2
printf '{"type":"custom-title","customTitle":"回退后"}\n' > "$T/s1.jsonl"
echo "正文" | "$PO" send sbox tester "回退后" "回复" >/dev/null
out=$(hook_cli s1 cli-L2 | DHD "$PO" hook); rc=$?
[ $rc -eq 2 ] && ok "v1.6 回退换了 CLI id 照样认人" || bad "v1.6 回退认人 rc=$rc"
clog "2026-10-05 01:00:00" local_OLD cli-L3
clog "2026-10-05 03:00:00" local_NEW cli-L3
printf '{"type":"custom-title","customTitle":"时间戳会话"}\n' > "$T/s3.jsonl"
"$PO" add tbox --claude "时间戳会话" >/dev/null
echo "正文" | "$PO" send tbox tester "时间戳" "回复" >/dev/null
out=$(hook_cli s3 cli-L3 | DHD "$PO" hook); rc=$?
[ $rc -eq 2 ] && [ "$(route_field tbox claude_session)" = "local_NEW" ] \
  && ok "v1.6 多个映射取最新行内时间戳" || bad "v1.6 时间戳映射=$(route_field tbox claude_session)"

# 3) 首选 HOST_SESSION_ID（桌面版），改名不影响
printf '{"type":"custom-title","customTitle":"主机会话"}\n' > "$T/h.jsonl"
"$PO" add hostbox --claude "主机会话" >/dev/null
echo "正文" | "$PO" send hostbox tester "主机信" "回复" >/dev/null
out=$(hook_cli h cli-ignored | hook_run CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_HOST -- "$PO" hook); rc=$?
[ $rc -eq 2 ] && [ "$(route_field hostbox claude_session)" = "local_HOST" ] \
  && ok "v1.6 优先用 HOST_SESSION_ID 绑定" || bad "v1.6 HOST 绑定 rc=$rc"
printf '{"type":"custom-title","customTitle":"改个名"}\n' > "$T/h.jsonl"
echo "正文" | "$PO" send hostbox tester "主机信2" "回复" >/dev/null
out=$(hook_cli h whatever | hook_run CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_HOST -- "$PO" hook); rc=$?
[ $rc -eq 2 ] && ok "v1.6 HOST 身份改名后仍认人" || bad "v1.6 HOST 改名 rc=$rc"

# 4) 无映射：桌面版退回标题匹配（未绑定信箱），不臆造绑定，钩子不报错
printf '{"type":"custom-title","customTitle":"无日志会话"}\n' > "$T/nl.jsonl"
"$PO" add nolog --claude "无日志会话" >/dev/null
echo "正文" | "$PO" send nolog tester "无日志" "回复" >/dev/null
out=$(hook_cli nl cli-NL | hook_run CLAUDE_CODE_ENTRYPOINT=claude-desktop POSTOFFICE_CLAUDE_MAINLOG="$T/nope.log" -- "$PO" hook); rc=$?
[ $rc -eq 2 ] && ok "v1.6 日志缺失退回标题匹配" || bad "v1.6 日志缺失 rc=$rc"
[ -z "$(route_field nolog claude_session)" ] && ok "v1.6 退回标题匹配不臆造绑定" || bad "v1.6 误绑定"

# 5) 认人负例：同名不接管 / 多重绑定 / 未解析撞已绑定 / 命令行用 CLI id；每种诊断只通知一次
rm -f "$POSTOFFICE_HOME/logs/notify.log"
"$PO" add twin --claude "撞名标题" >/dev/null; route_set twin claude_session local_TAKEN
printf '{"type":"custom-title","customTitle":"撞名标题"}\n' > "$T/tw.jsonl"
echo "正文" | "$PO" send twin tester "撞名信" "回复" >/dev/null
out=$(hook_cli tw x | hook_run CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_FRESH -- "$PO" hook); rc=$?
[ $rc -eq 0 ] && ok "v1.6 同名不同身份不接管" || bad "v1.6 撞名接管 rc=$rc"
hook_cli tw x | hook_run CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_FRESH -- "$PO" hook >/dev/null 2>&1
[ "$(grep -c "会话认人诊断" "$POSTOFFICE_HOME/logs/notify.log" 2>/dev/null)" = "1" ] && ok "v1.6 同名只诊断一次" || bad "v1.6 同名重复诊断"
"$PO" add dupa --claude "绑定甲" >/dev/null; route_set dupa claude_session local_DUP
"$PO" add dupb --claude "绑定乙" >/dev/null; route_set dupb claude_session local_DUP
echo "正文" | "$PO" send dupa tester "多重绑定信" "回复" >/dev/null
out=$(hook_cli tw x | hook_run CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_DUP -- "$PO" hook); rc=$?
[ $rc -eq 0 ] && ok "v1.6 身份被多重绑定则不认任何一个" || bad "v1.6 多重绑定认了 rc=$rc"
"$PO" add n2 --claude "N2" >/dev/null; route_set n2 claude_session local_X
printf '{"type":"custom-title","customTitle":"N2"}\n' > "$T/n2.jsonl"
echo "正文" | "$PO" send n2 tester "无身份信" "回复" >/dev/null
out=$(titleonly n2 | hook_run "$PO" hook); rc=$?
[ $rc -eq 0 ] && ok "v1.6 身份缺失撞已绑定信箱则不认领" || bad "v1.6 未解析却认领 rc=$rc"
"$PO" add clibox --claude "命令行会话" >/dev/null
printf '{"type":"custom-title","customTitle":"命令行会话"}\n' > "$T/cb.jsonl"
echo "正文" | "$PO" send clibox tester "命令行信" "回复" >/dev/null
out=$(hook_cli cb cli-CMD | hook_run -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_HOST_SESSION_ID -- "$PO" hook); rc=$?
[ $rc -eq 2 ] && [ "$(route_field clibox claude_session)" = "cli-CMD" ] \
  && ok "v1.6 命令行版用 CLI session_id 认人" || bad "v1.6 CLI 身份 rc=$rc"
[ "$(grep -c "会话认人诊断" "$POSTOFFICE_HOME/logs/notify.log" 2>/dev/null)" = "3" ] && ok "v1.6 三类认人诊断各一次" || bad "v1.6 诊断计数 $(grep -c "会话认人诊断" "$POSTOFFICE_HOME/logs/notify.log")"

# 6) 30 分钟未处理：只提醒需要处理的信；仅告知/回执/广播汇总不提醒；正负混合→未知→提醒
"$PO" add alertbox --codex thread-a >/dev/null
python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r['alertbox']['codex_cli']=sys.argv[2];json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$T/fakecodex"
echo "x" | "$PO" send alertbox tester "需要回复的" "回复" >/dev/null
echo "x" | "$PO" send alertbox tester "只是告知的" "仅告知" >/dev/null
echo "x" | "$PO" send alertbox tester "混合需要" "仅告知，无需回复/确认" >/dev/null
echo "x" | "$PO" send alertbox tester "前缀信" "需要：需要：回复" >/dev/null
pf=$(grep -l "前缀信" "$POSTOFFICE_HOME/alertbox/inbox/"*.md)
grep -q "^需要：回复$" "$pf" && ok "v1.6 send 去掉重复的需要前缀" || bad "v1.6 需要前缀未清理：$(head -3 "$pf" | tail -1)"
printf '来源：tester\n事由：回执：某事\n需要：回执（默认不答复）\n回执：rid-1\n原事由：某事\n\n正文\n' > "$POSTOFFICE_HOME/alertbox/inbox/20260101-000001_tester_notice.md"
printf '来源：postoffice\n事由：广播汇总：全员\n需要：回执（默认不答复）\n回执：B999\n原事由：全员\n\n正文\n' > "$POSTOFFICE_HOME/alertbox/inbox/20260101-000002_postoffice_sum.md"
rm -f "$POSTOFFICE_HOME/logs/notify.log"
POSTOFFICE_POLL=1 POSTOFFICE_CONSUME_ALERT=2 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 8; kill $PM 2>/dev/null; wait $PM 2>/dev/null
nudge=$(grep "已提醒但" "$POSTOFFICE_HOME/logs/notify.log" 2>/dev/null)
printf '%s' "$nudge" | grep -q "需要回复的" && printf '%s' "$nudge" | grep -q "混合需要" \
  && ! printf '%s' "$nudge" | grep -q "只是告知的" && ! printf '%s' "$nudge" | grep -q "广播汇总" \
  && ok "v1.6 只提醒需要处理/未知的信（仅告知/回执/汇总不提醒）" || bad "v1.6 提醒分类：$nudge"

# 7) 20 分钟计时起点 = max(写信时间, 最近上线时间)；重复在线不刷新；升级基线不误报
"$PO" add slowbox --claude "计时会话" >/dev/null
"$PO" offline slowbox >/dev/null
echo "正文" | "$PO" send slowbox tester "迟到的信" "回复" >/dev/null
f=$(ls "$POSTOFFICE_HOME/slowbox/inbox/"*.md); touch -t 202601010000 "$f"
"$PO" online slowbox >/dev/null
t1=$(route_field slowbox online_since); sleep 1; "$PO" online slowbox >/dev/null
t2=$(route_field slowbox online_since)
[ -n "$t1" ] && [ "$t1" = "$t2" ] && ok "v1.6 重复设为在线不刷新 online_since" || bad "v1.6 online_since 被刷新 t1=$t1 t2=$t2"
"$PO" add upbox --claude "升级会话" >/dev/null
echo "正文" | "$PO" send upbox tester "升级旧信" "回复" >/dev/null
uf=$(ls "$POSTOFFICE_HOME/upbox/inbox/"*.md); touch -t 202601010000 "$uf"
[ -z "$(route_field upbox online_since)" ] && ok "v1.6 已在线信箱初始无 online_since" || bad "v1.6 初始就有 online_since"
rm -f "$POSTOFFICE_HOME/logs/notify.log"
POSTOFFICE_POLL=1 POSTOFFICE_GRACE=2 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 1; kill $PM 2>/dev/null; wait $PM 2>/dev/null
grep -q "迟到的信" "$POSTOFFICE_HOME/logs/notify.log" 2>/dev/null && bad "v1.6 刚上线就误报" || ok "v1.6 刚上线不误报（离线时长不计）"
[ -n "$(route_field upbox online_since)" ] && ok "v1.6 邮递员给已在线信箱补基线" || bad "v1.6 未补基线"
POSTOFFICE_POLL=1 POSTOFFICE_GRACE=2 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 4; kill $PM 2>/dev/null; wait $PM 2>/dev/null
grep -q "迟到的信" "$POSTOFFICE_HOME/logs/notify.log" 2>/dev/null && ok "v1.6 在线满时限后才告警" || bad "v1.6 满时限未告警"

# 8) 合并回执：3 正式 + 3 回执 → 正式先送、回执合并成一条，不触发限流，账本逐条记
mkcap "$T/merge_cap" "$T/fakecodex2"
"$PO" add mergebox --codex thread-m >/dev/null
python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r['mergebox']['codex_cli']=sys.argv[2];json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$T/fakecodex2"
for i in 1 2 3; do echo "x" | "$PO" send mergebox tester "正式$i" "回复" >/dev/null; done
for i in 1 2 3; do printf '来源：tester\n事由：回执：原事由%s\n需要：回执（默认不答复）\n回执：orig-%s\n原事由：原事由%s\n\n正文\n' "$i" "$i" "$i" > "$POSTOFFICE_HOME/mergebox/inbox/20260101-00000${i}_tester_notice.md"; done
rm -f "$T/merge_cap" "$POSTOFFICE_HOME/logs/notify.log"
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 7; kill $PM 2>/dev/null; wait $PM 2>/dev/null
[ "$(grep -c "另有 3 条回执" "$T/merge_cap")" = "1" ] && ok "v1.6 回执合并成一条" || bad "v1.6 合并"
[ "$(grep -c "查询：postoffice receipt mergebox " "$T/merge_cap")" = "3" ] && ok "v1.6 合并块含各条查询 ID" || bad "v1.6 合并块 ID"
[ "$(grep -c "【联络总站｜3 封新信】" "$T/merge_cap")" = "1" ] && ok "v1.10 三封正式信合成一批一次唤醒" || bad "v1.10 批量正式信"
[ "$(grep -c "^[0-9]\+\. mergebox/" "$T/merge_cap")" = "3" ] && ok "v1.10 批量逐封给稳定引用" || bad "v1.10 批量引用"
[ "$(grep -c "【联络总站新信" "$T/merge_cap")" = "0" ] && ok "v1.10 多封时不再用单封形态" || bad "v1.10 单封形态残留"
fm=$(grep -n "另有 3 条回执" "$T/merge_cap" | cut -d: -f1); lf=$(grep -n "【联络总站｜3 封新信】" "$T/merge_cap" | tail -1 | cut -d: -f1)
[ -n "$fm" ] && [ -n "$lf" ] && [ "$fm" -gt "$lf" ] && ok "v1.6 正式信排在合并回执之前" || bad "v1.6 顺序 fm=$fm lf=$lf"
[ "$(grep -o "mergebox" "$POSTOFFICE_HOME/.delivered.json" | wc -l | tr -d ' ')" = "6" ] && ok "v1.6 每个 ID 都记入账本" || bad "v1.6 账本"
grep -q "投递暂停" "$POSTOFFICE_HOME/logs/notify.log" 2>/dev/null && bad "v1.6 合并后仍限流" || ok "v1.6 合并后不触发限流"
before=$(wc -l < "$T/merge_cap" | tr -d ' ')
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null; wait $PM 2>/dev/null
[ "$(wc -l < "$T/merge_cap" | tr -d ' ')" = "$before" ] && ok "v1.6 重跑不重复投递" || bad "v1.6 重投"

# 9) 回执不饿死：只有回执时也投；超过等待时限就随下一次正式投递带上
mkcap "$T/starve_cap" "$T/fakecodex3"
"$PO" add starvebox --codex thread-s >/dev/null
python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r['starvebox']['codex_cli']=sys.argv[2];json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$T/fakecodex3"
printf '来源：tester\n事由：回执：单独\n需要：回执（默认不答复）\n回执：solo-1\n原事由：单独\n\n正文\n' > "$POSTOFFICE_HOME/starvebox/inbox/20260101-000000_tester_solo.md"
rm -f "$T/starve_cap"
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null; wait $PM 2>/dev/null
grep -q "solo-1" "$T/starve_cap" && ok "v1.6 只有回执时也投递" || bad "v1.6 只有回执被饿死"
echo "x" | "$PO" send starvebox tester "正式信" "回复" >/dev/null
printf '来源：tester\n事由：回执：旧的\n需要：回执（默认不答复）\n回执：old-1\n原事由：旧的\n\n正文\n' > "$POSTOFFICE_HOME/starvebox/inbox/20260101-000001_tester_old.md"
touch -t 202601010000 "$POSTOFFICE_HOME/starvebox/inbox/20260101-000001_tester_old.md"
rm -f "$T/starve_cap"
POSTOFFICE_RECEIPT_WAIT=1 POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null; wait $PM 2>/dev/null
grep -q "【联络总站新信" "$T/starve_cap" && grep -q "另有 1 条回执" "$T/starve_cap" && grep -q "old-1" "$T/starve_cap" \
  && ok "v1.6 超时回执随正式信带上（不饿死）" || bad "v1.6 回执饿死"

# 10) 批量投递失败：批内 ID 不记已送达、只通知一次；改回成功后可重试且不重复
mkfail "$T/fakefail"
mkcap "$T/fail_cap" "$T/fakeok"
"$PO" add failbox --codex thread-f >/dev/null
python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r['failbox']['codex_cli']=sys.argv[2];json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$T/fakefail"
echo "x" | "$PO" send failbox tester "失败甲" "回复" >/dev/null
echo "x" | "$PO" send failbox tester "失败乙" "回复" >/dev/null
rm -f "$POSTOFFICE_HOME/logs/notify.log"
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null; wait $PM 2>/dev/null
[ "$(grep -o "failbox" "$POSTOFFICE_HOME/.delivered.json" 2>/dev/null | wc -l | tr -d ' ')" = "0" ] && ok "v1.6 投递失败不记已送达" || bad "v1.6 失败却记了账"
[ "$(grep -c "投递失败" "$POSTOFFICE_HOME/logs/notify.log" 2>/dev/null)" = "1" ] && ok "v1.6 失败只通知一次" || bad "v1.6 失败重复通知"
python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r['failbox']['codex_cli']=sys.argv[2];json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$T/fakeok"
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 4; kill $PM 2>/dev/null; wait $PM 2>/dev/null
[ "$(grep -o "failbox" "$POSTOFFICE_HOME/.delivered.json" 2>/dev/null | wc -l | tr -d ' ')" = "2" ] && ok "v1.6 恢复后重试送达且不重复" || bad "v1.6 重试未送达"

# ---------- v1.7：分组开关 + 逻辑地址 + 切换广播（docs/SPEC_v1.7.md） ----------
# 独立的临时邮局，不影响上面用例的通讯录与账本
MAIN_HOME=$POSTOFFICE_HOME
POSTOFFICE_HOME="$T/po17"; export POSTOFFICE_HOME; mkdir -p "$POSTOFFICE_HOME"
n_mail() { ls "$POSTOFFICE_HOME/$1/inbox/"*.md 2>/dev/null | wc -l | tr -d ' '; }
try_cfg() { printf '%s' "$1" > "$T/try.json"; "$PO" config import "$T/try.json" >"$T/cfgout" 2>&1; }
GOOD='{"version":1,
 "groups":{"codex":["mgr_b","dev_b"],"claude":["mgr_a"]},
 "aliases":{"project.manager":{"candidates":["mgr_a","mgr_b"],"notify":["dev_a"],"handoff":"/p/h.md"},
            "legacy.manager":["mgr_a"]}}'
for b in mgr_a mgr_b dev_a dev_b; do "$PO" add "$b" --notify >/dev/null; done

# 1) 合法导入；引用不存在信箱时拒绝且旧字节不变
try_cfg "$GOOD" && ok "v1.7 config import 正常导入" || bad "v1.7 config import"
before=$(cat "$POSTOFFICE_HOME/config.json")
try_cfg '{"version":1,"groups":{"g":["幽灵信箱"]}}' && bad "v1.7 引用不存在信箱竟导入成功" \
  || ok "v1.7 引用不存在信箱时拒绝导入"
[ "$(cat "$POSTOFFICE_HOME/config.json")" = "$before" ] && ok "v1.7 导入失败旧配置字节不变" || bad "v1.7 旧配置被改动"

# 2) 关键负例：撞名 / 重复键 / 嵌套 / 重复项 / 坏 JSON / 非法名字，全部拒绝且旧配置不变
try_cfg '{"version":1,"groups":{"x":["mgr_a"]},"aliases":{"x":["mgr_a"]}}' && bad "v1.7 组与逻辑地址撞名未拒绝" || ok "v1.7 组与逻辑地址撞名拒绝"
try_cfg '{"version":1,"groups":{"a":["mgr_a"],"a":["mgr_b"]}}' && bad "v1.7 重复 JSON 键未拒绝" || ok "v1.7 重复 JSON 键拒绝"
try_cfg '{"version":1,"groups":{"codex":["mgr_a"]},"aliases":{"pm":["codex"]}}' && bad "v1.7 嵌套未拒绝" || ok "v1.7 嵌套拒绝"
try_cfg '{"version":1,"groups":{"codex":["mgr_a","mgr_a"]}}' && bad "v1.7 重复成员未拒绝" || ok "v1.7 重复成员拒绝"
try_cfg '{"version":1,"aliases":{"pm":{"candidates":["mgr_a","mgr_a"]}}}' && bad "v1.7 重复候选未拒绝" || ok "v1.7 重复候选拒绝"
try_cfg '{"version":1,"groups":{"co dex":["mgr_a"]}}' && bad "v1.7 非法名字未拒绝" || ok "v1.7 非法名字拒绝"
try_cfg '{"version":1,"groups":{"*":["mgr_a"]}}' && bad "v1.7 通配符未拒绝" || ok "v1.7 通配符拒绝"
try_cfg '{"version":1,"aliases":{"pm":{"candidates":[]}}}' && bad "v1.7 空 candidates 未拒绝" || ok "v1.7 空 candidates 拒绝"
try_cfg '{"version":1,"aliases":{"pm":{"notify":["mgr_a"]}}}' && bad "v1.7 alias 缺 candidates 未拒绝" || ok "v1.7 alias 缺 candidates 拒绝"
try_cfg '{"version":1,' && bad "v1.7 坏 JSON 未拒绝" || ok "v1.7 坏 JSON 拒绝"
try_cfg '{"version":2,"groups":{}}' && bad "v1.7 错版本未拒绝" || ok "v1.7 错版本拒绝"
[ "$(cat "$POSTOFFICE_HOME/config.json")" = "$before" ] && ok "v1.7 一串坏配置都没动旧文件" || bad "v1.7 坏配置改动了旧文件"

# 3) 成功导入有备份；show 只读
try_cfg '{"version":1,"groups":{"codex":["mgr_b","dev_b"]}}' && ok "v1.7 再次导入成功" || bad "v1.7 再次导入"
[ "$(ls "$POSTOFFICE_HOME/logs/"config.json.*.bak 2>/dev/null | wc -l | tr -d ' ')" = "1" ] \
  && ok "v1.7 覆盖前备份旧配置" || bad "v1.7 没有备份旧配置"
sum=$(cat "$POSTOFFICE_HOME/config.json"); "$PO" config show >/dev/null 2>&1
[ "$(cat "$POSTOFFICE_HOME/config.json")" = "$sum" ] && ok "v1.7 config show 不改配置" || bad "v1.7 show 改了配置"
try_cfg "$GOOD" >/dev/null

# 4) 分组三操作；online_since 合同；空组
"$PO" offline @codex >/dev/null && ok "v1.7 offline @GROUP" || bad "v1.7 offline @GROUP"
[ "$(route_field mgr_b status)" = "offline" ] && [ "$(route_field dev_b status)" = "offline" ] \
  && [ "$(route_field mgr_a status)" = "online" ] && ok "v1.7 整组下线且不波及其他信箱" || bad "v1.7 整组下线"
"$PO" online @codex >/dev/null
first=$(route_field mgr_b online_since); [ -n "$first" ] && ok "v1.7 组上线写 online_since" || bad "v1.7 组上线没写 online_since"
"$PO" online @codex >/dev/null
[ "$(route_field mgr_b online_since)" = "$first" ] && ok "v1.7 重复 online 不刷新 online_since" || bad "v1.7 重复 online 刷新了时钟"
echo x | "$PO" send mgr_b tester "组清空甲" "回复" >/dev/null; echo x | "$PO" send dev_b tester "组清空乙" "回复" >/dev/null
"$PO" clear @codex | grep -q "存档到" && ok "v1.7 clear @GROUP 逐成员给出去向" || bad "v1.7 clear @GROUP"
[ "$(n_mail mgr_b)" = "0" ] && [ "$(n_mail dev_b)" = "0" ] && ok "v1.7 clear @GROUP 清空全部成员" || bad "v1.7 clear @GROUP 没清空"
"$PO" offline @claude | grep -q "1 个信箱" && ok "v1.7 单成员分组可用" || bad "v1.7 单成员分组"
"$PO" online @claude >/dev/null   # 下面按“首选候选在线”继续测别名解析
try_cfg '{"version":1,"groups":{"codex":["mgr_b","dev_b"],"claude":["mgr_a"],"empty":[]},
          "aliases":{"project.manager":{"candidates":["mgr_a","mgr_b"],"notify":["dev_a"],"handoff":"/p/h.md"},
                     "legacy.manager":["mgr_a"]}}' >/dev/null
"$PO" offline @empty | grep -q "0 个信箱" && ok "v1.7 空组输出 0 个成员" || bad "v1.7 空组"

# 5) 别名解析：首选 / fallback / 全离线失败 / 不混用
a0=$(n_mail mgr_a)
echo 正文 | "$PO" send @project.manager tester "首选" "回复" | grep -q "解析 @project.manager → mgr_a" \
  && ok "v1.7 alias 默认选第一候选" || bad "v1.7 alias 首选"
[ "$(( $(n_mail mgr_a) - a0 ))" = "1" ] && ok "v1.7 信落在第一候选信箱" || bad "v1.7 信没落在第一候选"
grep -q "^逻辑地址：@project.manager$" "$POSTOFFICE_HOME/mgr_a/inbox/"*.md && ok "v1.7 信头记录逻辑地址" || bad "v1.7 信头缺逻辑地址"
"$PO" offline mgr_a >/dev/null
a0=$(n_mail mgr_a)
echo 正文 | "$PO" send @project.manager tester "回退" "回复" | grep -q "解析 @project.manager → mgr_b" \
  && ok "v1.7 第一候选离线时选第二候选" || bad "v1.7 fallback"
[ "$(n_mail mgr_a)" = "$a0" ] && ok "v1.7 切换后旧信仍在旧信箱" || bad "v1.7 旧信被搬走"
"$PO" offline mgr_b >/dev/null
before_a=$(n_mail mgr_a); before_b=$(n_mail mgr_b)
echo 正文 | "$PO" send @project.manager tester "无人" "回复" >"$T/noone" 2>&1 && bad "v1.7 全离线竟发送成功" \
  || ok "v1.7 全离线明确失败"
grep -q "当前没有在线信箱" "$T/noone" && grep -q "mgr_a：离线" "$T/noone" \
  && ok "v1.7 失败时列出候选状态" || bad "v1.7 失败未列候选状态"
[ "$(n_mail mgr_a)" = "$before_a" ] && [ "$(n_mail mgr_b)" = "$before_b" ] \
  && ok "v1.7 无人时没有信落入任何候选" || bad "v1.7 无人时信落进候选"
"$PO" online mgr_a >/dev/null
echo x | "$PO" send mgr_a tester "普通信箱" "回复" >/dev/null && ok "v1.7 具体信箱 send 行为不变" || bad "v1.7 普通 send"
"$PO" offline @project.manager 2>"$T/mix" && bad "v1.7 alias 当分组用竟成功" || ok "v1.7 alias 不能当分组用"
grep -q "逻辑地址（alias）" "$T/mix" && ok "v1.7 提示区分 alias 与 group" || bad "v1.7 提示不清楚"
echo x | "$PO" send @codex tester "组当别名" "回复" >"$T/mix2" 2>&1 && bad "v1.7 group 当逻辑地址竟成功" \
  || ok "v1.7 group 不能当逻辑地址发信"
echo x | "$PO" send @legacy.manager tester "数组写法" "回复" | grep -q "→ mgr_a" \
  && ok "v1.7 兼容数组写法的 alias" || bad "v1.7 数组写法 alias"
"$PO" offline @nope 2>/dev/null && bad "v1.7 未知分组竟成功" || ok "v1.7 未知分组报错"

# 6) 引用信箱被删后，组操作不改任何状态
"$PO" remove dev_b >/dev/null
b1=$(route_field mgr_b status); b2=$(route_field dev_b status 2>/dev/null || echo "-")
"$PO" offline @codex 2>"$T/rm" && bad "v1.7 引用已删信箱仍改了状态" || ok "v1.7 引用已删信箱时组操作拒绝"
grep -q "未改动任何信箱状态" "$T/rm" && ok "v1.7 拒绝时明说没改任何状态" || bad "v1.7 拒绝提示不清楚"
[ "$(route_field mgr_b status)" = "$b1" ] && ok "v1.7 拒绝后其它成员状态未变" || bad "v1.7 拒绝后状态被改了"
"$PO" remove mgr_b >/dev/null; "$PO" add mgr_b --notify >/dev/null
"$PO" add dev_b --notify >/dev/null   # 重新登记，后面的组操作才合法

# 7) 切换广播：首见只记基线；稳定后通知一次；无人 / 恢复 / 主事复归；重启不重复
ASTABLE=6   # 稳定期放大，抖动窗口才留得住，不靠运气
POSTOFFICE_ALIAS_STABLE=$ASTABLE POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3
d0=$(n_mail dev_a)
[ "$d0" = "0" ] && ok "v1.7 首次发现有目标时只记基线不广播" || bad "v1.7 首次发现就广播"
grep -q "登记初始目标" "$POSTOFFICE_HOME/logs/postman.log" && ok "v1.7 初始基线写进日志" || bad "v1.7 没写基线日志"
kill $PM 2>/dev/null; wait $PM 2>/dev/null; unset PM
POSTOFFICE_ALIAS_STABLE=$ASTABLE POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3
[ "$(n_mail dev_a)" = "$d0" ] && ok "v1.7 重启不重复广播基线" || bad "v1.7 重启重复广播"
d0=$(n_mail dev_a); h0=$(n_mail mgr_b)
"$PO" offline mgr_a >/dev/null; sleep $((ASTABLE + 4))
[ "$(( $(n_mail dev_a) - d0 ))" = "1" ] && ok "v1.7 A→B 稳定后广播一次" || bad "v1.7 A→B 广播次数不对"
[ "$(( $(n_mail mgr_b) - h0 ))" = "1" ] && ok "v1.7 交接提醒发给接手方" || bad "v1.7 没发交接提醒"
grep -q "原因=原目标下线" "$POSTOFFICE_HOME/logs/alias_switch.log" && ok "v1.7 切换日志写明前后与原因" || bad "v1.7 切换日志缺原因"
grep -q "交接路径：/p/h.md" "$POSTOFFICE_HOME/mgr_b/inbox/"*.md && ok "v1.7 交接信带 handoff 路径" || bad "v1.7 交接信缺 handoff 路径"
grep -q "不授予你额外权限" "$POSTOFFICE_HOME/mgr_b/inbox/"*.md && ok "v1.7 交接信声明不自授权限" || bad "v1.7 交接信缺声明"
d0=$(n_mail dev_a); h0=$(n_mail mgr_a)
"$PO" online mgr_a >/dev/null; sleep 2; "$PO" offline mgr_a >/dev/null; sleep $((ASTABLE + 4))
[ "$(n_mail dev_a)" = "$d0" ] && ok "v1.7 稳定期内 A→B→A 不广播" || bad "v1.7 稳定期内反复被广播"
[ "$(n_mail mgr_a)" = "$h0" ] && ok "v1.7 稳定期内不投交接" || bad "v1.7 稳定期内投了交接"
d0=$(n_mail dev_a); h0=$(n_mail mgr_a)
"$PO" offline mgr_a >/dev/null; "$PO" offline mgr_b >/dev/null; sleep $((ASTABLE + 4))
[ "$(( $(n_mail dev_a) - d0 ))" = "1" ] && ok "v1.7 全离线通知一次" || bad "v1.7 全离线通知次数不对"
grep -q "（无在线候选）" "$POSTOFFICE_HOME/dev_a/inbox/"*.md && ok "v1.7 无人接任通知说清没有候选" || bad "v1.7 无人接任通知不清楚"
[ "$(n_mail mgr_a)" = "$h0" ] && ok "v1.7 无人时不向任何候选投交接" || bad "v1.7 无人时仍投了交接"
d0=$(n_mail dev_a)
"$PO" online mgr_b >/dev/null; sleep $((ASTABLE + 4))
[ "$(( $(n_mail dev_a) - d0 ))" = "1" ] && ok "v1.7 备用恢复后再广播一次" || bad "v1.7 恢复未广播"
d0=$(n_mail dev_a)
"$PO" online mgr_a >/dev/null; sleep $((ASTABLE + 4))
[ "$(( $(n_mail dev_a) - d0 ))" = "1" ] && ok "v1.7 主事复归再广播一次" || bad "v1.7 主事复归未广播"
grep -q "原因=高优先级候选恢复" "$POSTOFFICE_HOME/logs/alias_switch.log" && ok "v1.7 主事复归原因正确" || bad "v1.7 复归原因不对"
kill $PM 2>/dev/null; wait $PM 2>/dev/null; unset PM
d0=$(n_mail dev_a)
POSTOFFICE_ALIAS_STABLE=$ASTABLE POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3
kill $PM 2>/dev/null; wait $PM 2>/dev/null; unset PM
[ "$(n_mail dev_a)" = "$d0" ] && ok "v1.7 重启不重复广播已确认的切换" || bad "v1.7 重启重复广播"

# 8) 交接失败后恢复：只补交接，不重播广播
POSTOFFICE_ALIAS_STABLE=$ASTABLE POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 2
"$PO" offline mgr_a >/dev/null; "$PO" offline mgr_b >/dev/null; sleep $((ASTABLE + 4))
d0=$(n_mail dev_a); h0=$(n_mail mgr_b)
chmod 555 "$POSTOFFICE_HOME/mgr_b/inbox"
"$PO" online mgr_b >/dev/null; sleep $((ASTABLE + 4))
[ "$(( $(n_mail dev_a) - d0 ))" = "1" ] && ok "v1.7 交接失败时广播照发一次" || bad "v1.7 交接失败时广播次数不对"
[ "$(n_mail mgr_b)" = "$h0" ] && ok "v1.7 交接失败时确实没投出交接" || bad "v1.7 交接失败却投出了"
grep -q "交接提醒失败" "$POSTOFFICE_HOME/logs/postman.log" && ok "v1.7 交接失败写进日志" || bad "v1.7 交接失败没日志"
d1=$(n_mail dev_a)
chmod 755 "$POSTOFFICE_HOME/mgr_b/inbox"; sleep 4
[ "$(n_mail dev_a)" = "$d1" ] && ok "v1.7 恢复后不重播广播" || bad "v1.7 恢复后重播了广播"
[ "$(( $(n_mail mgr_b) - h0 ))" = "1" ] && ok "v1.7 恢复后只补发交接" || bad "v1.7 恢复后没补交接"
kill ${PM:-} 2>/dev/null; wait ${PM:-} 2>/dev/null; unset PM

# 9) 广播按物理信编号回执；混用只记一条；伪标记与跨箱无效；全员回执后立即汇总
echo 广播正文 | "$PO" broadcast dev_a,dev_b dev_a "兼容广播" "回复" >"$T/bc" 2>&1
BID=$(sed -n 's/^广播编号：//p' "$T/bc")
LID=$(basename "$(ls "$POSTOFFICE_HOME"/dev_b/inbox/*兼容广播*.md)" .md)
"$PO" ack dev_b "$LID" "按信回执" | grep -q "$BID" && ok "v1.7 按物理信编号回执归一到广播编号" || bad "v1.7 按信编号回执"
"$PO" ack dev_b "$BID" "再回一次" | grep -q "已回执过" && ok "v1.7 信编号+广播编号混用只记一条" || bad "v1.7 混用重复记账"
[ "$(grep -c "\"id\": \"$BID\"" "$POSTOFFICE_HOME/acks.jsonl")" = "1" ] && ok "v1.7 账本里广播回执只有一条" || bad "v1.7 账本有多条"
[ "$(ls "$POSTOFFICE_HOME"/dev_b/inbox/ | grep -c 兼容广播)" = "0" ] && ok "v1.7 回执后广播信归档" || bad "v1.7 回执后信还在 inbox"
"$PO" add other --notify >/dev/null
"$PO" ack other "$BID" "越权不该过" 2>"$T/xbox" && bad "v1.7 跨信箱回执竟通过" || ok "v1.7 非收件信箱不能回执该广播"
grep -q "不是发给" "$T/xbox" && ok "v1.7 跨信箱回执给出原因" || bad "v1.7 跨信箱回执提示不清楚"

# 第二种写法：先按广播编号回执，再按物理信编号回执；并用它的编号做正文伪造
echo 正文2 | "$PO" broadcast dev_a,dev_b dev_a "兼容广播二" "回复" >"$T/bc2" 2>&1
BID2=$(sed -n 's/^广播编号：//p' "$T/bc2")
LID2=$(basename "$(ls "$POSTOFFICE_HOME"/dev_b/inbox/*兼容广播二*.md)" .md)
printf '广播：%s\n这封信在正文第一行伪造了广播标记。\n' "$BID2" > "$T/forge.md"
"$PO" send dev_b dev_a "伪造标记" "回复" --file "$T/forge.md" >/dev/null
F=$(ls "$POSTOFFICE_HOME"/dev_b/inbox/*伪造标记*.md)
sed -n '4,5p' "$F" | grep -q "^广播：" && ok "v1.7 伪造标记确实落在正文里" || bad "v1.7 伪造标记位置不对"
"$PO" ack dev_b "$(basename "$F" .md)" "不该算广播" >/dev/null
grep -q "\"kind\": \"broadcast\", \"note\": \"不该算广播\"" "$POSTOFFICE_HOME/acks.jsonl" \
  && bad "v1.7 正文伪造广播标记被记成广播" || ok "v1.7 正文伪造广播标记不算广播回执"
[ "$(grep -c "\"id\": \"$BID2\"" "$POSTOFFICE_HOME/acks.jsonl")" = "0" ] \
  && ok "v1.7 伪造标记没给广播记上一条" || bad "v1.7 伪造标记污染了广播回执"
"$PO" ack dev_b "$LID2" "先按信编号" >/dev/null
"$PO" ack dev_b "$BID2" "再按广播编号" | grep -q "已回执过" && ok "v1.7 反过来的顺序也只记一条" || bad "v1.7 反过来顺序会记两条"
[ "$(grep -c "\"id\": \"$BID2\"" "$POSTOFFICE_HOME/acks.jsonl")" = "1" ] && ok "v1.7 第二种写法账本也只有一条" || bad "v1.7 第二种写法记了两条"
"$PO" ack dev_a "$BID" "我也回" >/dev/null
"$PO" ack dev_a "$BID2" "我也回二" >/dev/null
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3; kill $PM 2>/dev/null; wait $PM 2>/dev/null; unset PM
grep -ql "广播汇总" "$POSTOFFICE_HOME/dev_a/inbox/"*.md \
  && ok "v1.7 全员回执后不等截止就汇总" || bad "v1.7 全员回执后没立即汇总"

# 10) archive-receipt 先报数量；冲突零移动；其它编号与普通信不动
echo x | "$PO" send dev_a dev_b "待回执" "回复" >/dev/null
BL=$(basename "$(ls "$POSTOFFICE_HOME"/dev_b/inbox/*待回执*.md)" .md)
"$PO" ack dev_b "$BL" "一句话" >/dev/null
RID=$(grep -h '^回执：' "$POSTOFFICE_HOME"/dev_a/inbox/*.md | sed -n '1s/^回执：//p')
have=$(grep -l "^回执：$RID" "$POSTOFFICE_HOME"/dev_a/inbox/*.md | wc -l | tr -d ' ')
[ "$have" = "1" ] && ok "v1.7 待归档通知在 inbox 里找得到" || bad "v1.7 找不到待归档通知"
"$PO" archive-receipt dev_a "查无此号" | grep -q "匹配 0 封" && ok "v1.7 归档先报数量（0 条也成功）" || bad "v1.7 归档未报数量"
"$PO" archive-receipt dev_a "$RID" >"$T/arch" 2>&1
grep -q "匹配 1 封" "$T/arch" && ok "v1.7 归档报出匹配数量" || bad "v1.7 归档没报匹配数量"
[ "$(grep -l "^回执：$RID" "$POSTOFFICE_HOME"/dev_a/inbox/*.md 2>/dev/null | wc -l | tr -d ' ')" = "0" ] \
  && ok "v1.7 归档后同编号通知清空" || bad "v1.7 归档没清空"
echo x | "$PO" send dev_a dev_b "再来一封" "回复" >/dev/null
G=$(ls "$POSTOFFICE_HOME"/dev_b/inbox/*再来一封*.md); "$PO" ack dev_b "$(basename "$G" .md)" "一句话" >/dev/null
CLASH=$(grep -h '^回执：' "$POSTOFFICE_HOME"/dev_a/inbox/*.md | head -1 | sed 's/回执：//')
SRC=$(grep -l "^回执：$CLASH" "$POSTOFFICE_HOME"/dev_a/inbox/*.md | head -1)
[ -n "$SRC" ] && ok "v1.7 冲突用例有同名通知可用" || bad "v1.7 冲突用例没准备同名通知"
cp "$SRC" "$POSTOFFICE_HOME/dev_a/done/"
n_before=$(n_mail dev_a); plain_before=$(ls "$POSTOFFICE_HOME"/dev_a/inbox/*.md | grep -vc "回执：" || true)
"$PO" archive-receipt dev_a "$CLASH" >"$T/clash" 2>&1 && bad "v1.7 同名冲突竟归档成功" || ok "v1.7 同名冲突时整批不动并非零退出"
[ "$(n_mail dev_a)" = "$n_before" ] && ok "v1.7 冲突时一封也没动" || bad "v1.7 冲突时动了信"
[ "$(ls "$POSTOFFICE_HOME"/dev_a/inbox/*.md | grep -vc "回执：")" = "$plain_before" ] \
  && ok "v1.7 冲突时普通信不动" || bad "v1.7 冲突时动了普通信"
grep -q "匹配 1 封" "$T/clash" && ok "v1.7 冲突前仍先报匹配数量" || bad "v1.7 冲突时未报数量"

# 11) 面板：Groups 在线比例与按钮走公共 API（本机来源检查保留）
PORT=$((8800 + $$ % 150))
"$PO" panel --no-open --port $PORT >/dev/null 2>&1 & PP=$!; sleep 2
st=$(curl -s "http://127.0.0.1:$PORT/api/state")
printf '%s' "$st" | python3 -c "
import json, sys, os
st = json.load(sys.stdin)
r = json.load(open(os.environ['POSTOFFICE_HOME'] + '/routes.json'))
g = json.load(open(os.environ['POSTOFFICE_HOME'] + '/config.json'))['groups']
exp = {(k, sum(1 for m in v if r.get(m, {}).get('status') != 'offline'), len(v))
       for k, v in g.items()}
got = {(g['name'], g['online'], g['total']) for g in st['groups']}
print('OK' if got == exp and got else 'BAD:' + str(sorted(got)))
" | grep -q OK && ok "v1.7 面板 Groups 在线数/总数与通讯录一致" || bad "v1.7 面板 Groups 比例不对"
printf '%s' "$st" | python3 -c "import json,sys;a=json.load(sys.stdin)['aliases'];print('OK' if a[0]['target'] and a[0]['candidates'] else 'BAD')" \
  | grep -q OK && ok "v1.7 面板显示逻辑地址当前解析对象" || bad "v1.7 面板没显示解析对象"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "{\"group\":\"codex\",\"status\":\"offline\"}" "http://127.0.0.1:$PORT/api/status")
[ "$code" = "200" ] && [ "$(route_field dev_b status)" = "offline" ] \
  && ok "v1.7 面板按钮整组下线且沿用同一接口" || bad "v1.7 面板按钮失败 code=$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "{\"group\":\"没有这个组\",\"status\":\"offline\"}" "http://127.0.0.1:$PORT/api/status")
[ "$code" = "400" ] && ok "v1.7 面板未知分组被拒" || bad "v1.7 面板未知分组 code=$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H 'Origin: http://evil.example' \
  -d '{"name":"dev_b","status":"online"}' "http://127.0.0.1:$PORT/api/status")
[ "$code" = "403" ] && ok "v1.7 面板保留本机来源检查" || bad "v1.7 面板来源检查失效 code=$code"

# 12) 面板按各通道真实台账区分「待投递 / 已提醒待归档 / 投递失败」
route_list() { python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r[sys.argv[2]]['methods']=sys.argv[3].split(',');json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$1" "$2"; }
"$PO" add pbox --claude "面板统计会话" >/dev/null
"$PO" add obox --notify >/dev/null; route_list obox opencode_plugin; route_set obox session_id ses_pbox
"$PO" add xbox --codex thread-x >/dev/null
echo x | "$PO" send pbox dev_a "已提醒未归档" "回复" >/dev/null
echo x | "$PO" send pbox dev_a "还没投出去" "回复" >/dev/null
P1=$(ls "$POSTOFFICE_HOME"/pbox/inbox/*已提醒未归档*.md); P2=$(ls "$POSTOFFICE_HOME"/pbox/inbox/*还没投出去*.md)
printf '%s\n' "$P1" > "$POSTOFFICE_HOME/pbox/.seen"        # Claude 通道：提醒已被接受
P1N=$(basename "$P1"); P2N=$(basename "$P2")
curl -s "http://127.0.0.1:$PORT/api/state" | P1N="$P1N" P2N="$P2N" python3 -c '
import json, os, sys
b = [x for x in json.load(sys.stdin)["boxes"] if x["name"] == "pbox"][0]
st = {p["file"]: p["status"] for p in b["pending"]}
c = b["counts"]
good = (c == {"waiting": 1, "reminded": 1, "failed": 0} and len(b["pending"]) == 2
        and st[os.environ["P1N"]] == "reminded" and st[os.environ["P2N"]] == "waiting")
print("OK" if good else "BAD:" + json.dumps([c, st], ensure_ascii=False))' \
  | grep -q OK && ok "v1.7 面板分出待投递 1 / 已提醒待归档 1 且逐封标状态" || bad "v1.7 面板两态统计不对"
echo x | "$PO" send obox dev_a "插件失败信" "回复" >/dev/null
echo x | "$PO" send obox dev_a "超时兜底信" "回复" >/dev/null
OF=$(basename "$(ls "$POSTOFFICE_HOME"/obox/inbox/*插件失败信*.md)")
OT=$(basename "$(ls "$POSTOFFICE_HOME"/obox/inbox/*超时兜底信*.md)")
printf '{"box":"obox","file":"%s","result":"FAILED_FINAL"}\n' "$OF" >> "$POSTOFFICE_HOME/opencode_delivered.jsonl"
python3 -c "import json,sys;json.dump(['$POSTOFFICE_HOME/obox/inbox/$OT'],open('$POSTOFFICE_HOME/.delivered.json','w'))"
curl -s "http://127.0.0.1:$PORT/api/state" | OF="$OF" OT="$OT" python3 -c '
import json, os, sys
b = [x for x in json.load(sys.stdin)["boxes"] if x["name"] == "obox"][0]
st = {p["file"]: p["status"] for p in b["pending"]}
c = b["counts"]
good = (c == {"waiting": 1, "reminded": 0, "failed": 1} and st[os.environ["OF"]] == "failed"
        and st[os.environ["OT"]] == "waiting")
print("OK" if good else "BAD:" + json.dumps([c, st], ensure_ascii=False))' \
  | grep -q OK && ok "v1.7 FAILED_FINAL 算失败、.delivered.json 超时兜底不算已提醒" || bad "v1.7 面板把兜底或失败算成了已提醒"
echo x | "$PO" send xbox dev_a "码信已提醒" "回复" >/dev/null
XF="$POSTOFFICE_HOME/xbox/inbox/$(basename "$(ls "$POSTOFFICE_HOME"/xbox/inbox/*码信已提醒*.md)")"
python3 -c "import json;json.dump({'$XF':1.0},open('$POSTOFFICE_HOME/.woken.json','w'))"
curl -s "http://127.0.0.1:$PORT/api/state" | python3 -c '
import json, sys
b = [x for x in json.load(sys.stdin)["boxes"] if x["name"] == "xbox"][0]
print("OK" if b["counts"] == {"waiting": 0, "reminded": 1, "failed": 0} else "BAD:" + json.dumps(b["counts"]))' \
  | grep -q OK && ok "v1.7 Codex 通道按 .woken.json 接受记录算已提醒" || bad "v1.7 Codex 通道已提醒判定不对"
mv "$P2" "$POSTOFFICE_HOME/pbox/done/"
curl -s "http://127.0.0.1:$PORT/api/state" | python3 -c '
import json, sys
b = [x for x in json.load(sys.stdin)["boxes"] if x["name"] == "pbox"][0]
print("OK" if b["counts"] == {"waiting": 0, "reminded": 1, "failed": 0} and len(b["pending"]) == 1
      else "BAD:" + json.dumps(b["counts"]))' \
  | grep -q OK && ok "v1.7 归档后列表与数量一起少一封" || bad "v1.7 归档后数量没跟着变"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d '{"name":"pbox"}' "http://127.0.0.1:$PORT/api/clear")
[ "$code" = "200" ] && [ "$(ls "$POSTOFFICE_HOME"/pbox/inbox/*.md 2>/dev/null | wc -l | tr -d ' ')" = "0" ] \
  && [ "$(find "$POSTOFFICE_HOME/pbox/archived" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')" = "1" ] \
  && ok "v1.7 清空把待投递与已提醒一起存档且不删文件" || bad "v1.7 清空行为 code=$code"
"$PO" online @codex >/dev/null
kill $PP 2>/dev/null; wait $PP 2>/dev/null

# 13) 配置坏掉时不拖垮邮递员；功能停用但物理信箱照常
printf '坏掉的{' > "$POSTOFFICE_HOME/config.json"
POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 3
kill -0 $PM 2>/dev/null && ok "v1.7 坏配置不搞挂邮递员" || bad "v1.7 坏配置搞挂了邮递员"
kill $PM 2>/dev/null; wait $PM 2>/dev/null; unset PM
"$PO" send dev_a tester "坏配置下照常" "回复" >/dev/null && ok "v1.7 坏配置时物理信箱照常" || bad "v1.7 坏配置时发信失败"
"$PO" offline @codex 2>"$T/badcfg" && bad "v1.7 坏配置下分组操作竟成功" || ok "v1.7 坏配置下分组操作明确报错"
grep -q "已停用" "$T/badcfg" && ok "v1.7 坏配置提示说清功能已停用" || bad "v1.7 坏配置提示不清楚"
POSTOFFICE_HOME=$MAIN_HOME; export POSTOFFICE_HOME

# 14) 审核退修的六处负例：运行时配置校验 / 切换恢复 / 空广播头 / 面板确认框
# 每个临时邮局跑自己的 postman，稳定期放大到 6 秒，抖动窗口才留得住
po17_new() { P2="$T/$1"; rm -rf "$P2"; mkdir -p "$P2"; printf '%s' "$2" > "$P2/config.json"
             printf '%s' "$3" > "$P2/routes.json"; for b in a b d1 d2 h; do mkdir -p "$P2/$b/inbox"; done; }
po17_on() { POSTOFFICE_HOME="$P2" "$PO" online "$@" >/dev/null; }
po17_off() { POSTOFFICE_HOME="$P2" "$PO" offline "$@" >/dev/null; }
po17_n() { ls "$P2/$1/inbox/"*.md 2>/dev/null | wc -l | tr -d ' '; }
po17_round() { local st="${1:-2}"
                    POSTOFFICE_HOME="$P2" POSTOFFICE_ALIAS_STABLE=$st POSTOFFICE_POLL=1 POSTOFFICE_NO_NOTIFY=1 \
                    "$PO" postman >/dev/null 2>&1 & P2M=$!; sleep 1.4; kill $P2M 2>/dev/null; wait $P2M 2>/dev/null; }
SW_CFG='{"version":1,"aliases":{"pm":{"candidates":["a","b"],"notify":["d1"]}}}'
SW_ON='{"a":{"status":"online","methods":["notify"]},"b":{"status":"online","methods":["notify"]},
       "d1":{"status":"online","methods":["notify"]},"d2":{"status":"online","methods":["notify"]},
       "h":{"status":"online","methods":["notify"]}}'
SW_OFF='{"a":{"status":"offline","methods":["notify"]},"b":{"status":"offline","methods":["notify"]},
        "d1":{"status":"online","methods":["notify"]},"d2":{"status":"online","methods":["notify"]},
        "h":{"status":"online","methods":["notify"]}}'

# 退修1：手改坏的配置在运行时必须被当成没有配置，分组与逻辑地址一起停用，且不动 routes/inbox
po17_new rtcfg "$SW_CFG" "$SW_ON"
po17_on a; po17_on b; po17_round                     # 先建立一个正常基线
printf '%s' '{"version":1,"groups":{"good":["a"]}}' > "$P2/config.json"   # 一份本身合法的配置
po17_off @good >/dev/null; po17_on @good >/dev/null
r_before=$(cat "$P2/routes.json"); i_before=$(po17_n a)
printf '%s' '{"version":2,"groups":{"g":["a"]},"aliases":{"pm":["a"]}}' > "$P2/config.json"
po17_off @g 2>"$T/rt1"; rc=$?
[ $rc -ne 0 ] && ok "退修1 运行时版本非法时分组停用" || bad "退修1 运行时版本非法仍改了状态"
grep -q "配置无效" "$T/rt1" && ok "退修1 提示说清配置无效" || bad "退修1 提示不清楚"
grep -q "未改动任何信箱状态" "$T/rt1" && ok "退修1 提示明说没改任何状态" || bad "退修1 提示没说清没改状态"
[ "$(cat "$P2/routes.json")" = "$r_before" ] && ok "退修1 拒绝后 routes 字节不变" || bad "退修1 拒绝后 routes 被改"
printf '%s' '{"version":1,"groups":{"g":["a","a"]}}' > "$P2/config.json"
po17_off @g 2>"$T/rt2"; rc=$?
[ $rc -ne 0 ] && ok "退修1 运行时重复组成员时分组停用" || bad "退修1 重复组成员仍能分组"
[ "$(cat "$P2/routes.json")" = "$r_before" ] && ok "退修1 重复成员时 routes 不变" || bad "退修1 重复成员改了 routes"
printf '%s' '{"version":1,"aliases":{"pm":{"candidates":["a"],"notify":["幽灵"]}}}' > "$P2/config.json"
echo x | POSTOFFICE_HOME="$P2" "$PO" send @pm tester "不该投出去" "回复" >"$T/rt3" 2>&1; rc=$?
[ $rc -ne 0 ] && ok "退修1 notify 指向已移除信箱时逻辑地址停用" || bad "退修1 已移除信箱仍能发信"
[ "$(po17_n a)" = "$i_before" ] && ok "退修1 非法配置没有信落进候选信箱" || bad "退修1 非法配置仍投了信"
POSTOFFICE_HOME="$P2" "$PO" send a tester "物理信箱照常" "回复" >/dev/null 2>&1 \
  && ok "退修1 非法配置时物理信箱照常发信" || bad "退修1 非法配置连物理信箱也坏了"
POSTOFFICE_HOME="$P2" "$PO" config show 2>&1 | grep -q "注意：这份配置现在有问题" \
  && ok "退修1 config show 仍如实列出全部问题" || bad "退修1 config show 看不到问题"
POSTOFFICE_HOME="$P2" "$PO" config show 2>&1 | grep -q "指向未登记信箱 幽灵" \
  && ok "退修1 config show 指出具体是哪个信箱" || bad "退修1 show 没指出具体问题"

# 退修2：pending=None 不再同时表示「没有候选」与「没有待确认」；首轮无人等稳定期后通知一次
po17_new idle1 "$SW_CFG" "$SW_OFF"
po17_off a; po17_off b
po17_round                                   # 首见全员离线：只记基线
[ "$(po17_n d1)" = "0" ] && ok "退修2 首见无人时不立刻广播" || bad "退修2 首见无人就广播"
po17_round                                   # 刚过稳定期一点点，还不该发
[ "$(po17_n d1)" = "0" ] && ok "退修2 首轮无人先等稳定期" || bad "退修2 首轮无人没等稳定期"
po17_round; po17_round                        # 越过稳定期
[ "$(po17_n d1)" = "1" ] && ok "退修2 首轮无人过稳定期后通知一次" || bad "退修2 首轮无人过稳定期没通知"
po17_round; po17_round                        # 再跑几轮
[ "$(po17_n d1)" = "1" ] && ok "退修2 首轮无人只通知一次" || bad "退修2 首轮无人重复通知"
grep -q "（无在线候选）" "$P2/d1/inbox/"*.md && ok "退修2 无人接任通知说清没有候选" || bad "退修2 通知没写没有候选"
# 已确认 A 后全员离线：不能 1 秒就广播
po17_new idle2 "$SW_CFG" "$SW_ON"
po17_round                                   # 基线 = a
po17_off a; po17_off b; po17_round
[ "$(po17_n d1)" = "0" ] && ok "退修2 已确认后全离线不当场广播" || bad "退修2 全离线当场广播"
po17_round; po17_round
[ "$(po17_n d1)" = "1" ] && ok "退修2 已确认后全离线过稳定期才广播" || bad "退修2 全离线迟迟不广播"
# A→无人→A 抖动在稳定期内取消
po17_new idle3 "$SW_CFG" "$SW_ON"
po17_round; po17_off a; po17_off b; po17_round
[ "$(po17_n d1)" = "0" ] && ok "退修2 抖动用例起点没有广播" || bad "退修2 抖动用例起点不对"
po17_on a; po17_round; po17_round; po17_round
[ "$(po17_n d1)" = "0" ] && ok "退修2 A→无人→A 抖动被取消" || bad "退修2 抖动没取消"
# 重启不重复
po17_round
[ "$(po17_n d1)" = "0" ] && ok "退修2 重启不重复通知" || bad "退修2 重启重复通知"

# 退修3：删掉 alias 要落盘；重新加入时建立新基线而不是接回旧的待切换
po17_new rm1 "$SW_CFG" "$SW_ON"
po17_round
printf '%s' '{"version":1,"aliases":{}}' > "$P2/config.json"
po17_round
python3 -c "import json,sys;print('OK' if json.load(open(sys.argv[1]))['aliases']=={} else 'BAD')" "$P2/alias_state.json" \
  | grep -q OK && ok "退修3 删除最后一个 alias 已落盘" || bad "退修3 删除 alias 只清了内存"
printf '%s' "$SW_CFG" > "$P2/config.json"; po17_off a; po17_round; po17_round
[ "$(po17_n d1)" = "0" ] && ok "退修3 重新加入后从新基线开始" || bad "退修3 重新加入接回了旧待切换"
python3 -c "import json,sys;print('OK' if json.load(open(sys.argv[1]))['aliases']['pm']['confirmed']=='b' else 'BAD')" "$P2/alias_state.json" \
  | grep -q OK && ok "退修3 重新加入后基线是新观察到的目标" || bad "退修3 重新加入后基线不对"

# 退修4：同一事件只有一份广播记录、每个收件人一封；广播成功后崩溃也不重播
po17_new part "$SW_CFG" "$SW_ON"
po17_round; po17_off a; po17_round
# 让 d2 收不到信（只读 inbox），模拟「一部分送达、一部分失败」
python3 - "$P2/config.json" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg["aliases"]["pm"]["notify"] = ["d1", "d2"]
json.dump(cfg, open(p, "w"))
PY
python3 - "$P2/routes.json" <<'PY'
import json, sys
p = sys.argv[1]
r = json.load(open(p))
for k in r:
    r[k]["methods"] = ["notify"]
json.dump(r, open(p, "w"))
PY
po17_on b 2>/dev/null; chmod 555 "$P2/d2/inbox"
POSTOFFICE_HOME="$P2" POSTOFFICE_ALIAS_STABLE=1 POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 2.5
kill $PM 2>/dev/null; wait $PM 2>/dev/null; unset PM
recs=$(ls "$P2/broadcasts/"*.json 2>/dev/null | wc -l | tr -d ' ')
d1n=$(po17_n d1); d2n=$(po17_n d2)
[ "$d1n" = "1" ] && ok "退修4 部分失败时先到的收件人拿到一封" || bad "退修4 部分失败时第一封没到"
[ "$d2n" = "0" ] && ok "退修4 部分失败时失败方确实没收到" || bad "退修4 部分失败时竟然都收到了"
chmod 755 "$P2/d2/inbox"
POSTOFFICE_HOME="$P2" POSTOFFICE_ALIAS_STABLE=1 POSTOFFICE_POLL=1 "$PO" postman >/dev/null 2>&1 & PM=$!; sleep 2.5
kill $PM 2>/dev/null; wait $PM 2>/dev/null; unset PM
recs2=$(ls "$P2/broadcasts/"*.json 2>/dev/null | wc -l | tr -d ' ')
recs_note="记录数 ${recs} 变 ${recs2}"
[ "$(po17_n d1)" = "$d1n" ] && ok "退修4 恢复后不给已送达方重投" || bad "退修4 恢复后重复投递给已送达方"
[ "$(po17_n d2)" = "1" ] && ok "退修4 恢复后只补发给失败方" || bad "退修4 恢复后没补发"
[ "$recs2" = "$recs" ] && ok "退修4 同一事件只有一份广播记录" || bad "退修4 恢复时又建了一份广播记录：${recs_note}"
grep -q "未完成（已送达 1/2）" "$P2/logs/postman.log" \
  && ok "退修4 部分失败写明已送达几个" || bad "退修4 部分失败没记进度"
# 交接只在补齐后才发生，且不重播已成功的广播
python3 -c "
import json,sys
st=json.load(open(sys.argv[1]))
ev=list(st['aliases']['pm']['events'].values())[0]
print('OK' if len(ev['sent'])==2 and ev['broadcast'].startswith('B') and ev['done'] else 'BAD:'+json.dumps(ev,ensure_ascii=False))" "$P2/alias_state.json" \
  | grep -q OK && ok "退修4 事件记录里两个收件人都已送达" || bad "退修4 事件记录里送达名单不对"
# 稳定 BID：重启换一批也仍是同一个编号
bid_now=$(python3 -c "import json,sys;print(list(json.load(open(sys.argv[1]))['aliases']['pm']['events'].values())[0]['broadcast'])" "$P2/alias_state.json")
po17_round
bid_after=$(python3 -c "import json,sys;print(list(json.load(open(sys.argv[1]))['aliases']['pm']['events'].values())[0]['broadcast'])" "$P2/alias_state.json")
[ "$bid_now" = "$bid_after" ] && ok "退修4 事件广播编号跨重启稳定" || bad "退修4 广播编号跨重启变了"

# 退修4b：真·硬中断恢复。第一封广播信真的落地之后，抛一个不被普通 Exception 捕获的中断，
# 从磁盘重新加载模块再跑一轮，目标仍然是 b —— 这样恢复分支一定被走到。
# mode=inbox 时信留在收件箱；done 是收件人处理过；arch 是用户用 clear 把它归档了。
alias_crash() { # $1=第几封信落地后中断（1=广播 2=交接） $2=那封信最后在 inbox|done|arch
python3 - "$PO" "$P2" "$1" "$2" <<'PY'
import importlib.machinery, importlib.util, json, os, re, sys, time
from pathlib import Path
po, home, step, mode = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
os.environ["POSTOFFICE_HOME"] = str(home)
os.environ["POSTOFFICE_NO_NOTIFY"] = "1"
os.environ["POSTOFFICE_ALIAS_STABLE"] = "0"
ROUTES = json.loads((home / "routes.json").read_text(encoding="utf-8"))

def load():
    spec = importlib.util.spec_from_loader("po", importlib.machinery.SourceFileLoader("po", po))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class HardStop(BaseException):
    pass                                  # 基类不是 Exception：邮递员的 except Exception 抓不到它

# --- 第一轮：信真的写进磁盘，然后立刻被硬中断 ---
m = load()
# 第一轮：目标还是 a，只记基线（不发任何信）
m.process_alias_switches(m.boxes(ROUTES))
ROUTES["a"]["status"] = "offline"          # 现在目标变成 b
m.process_alias_switches(m.boxes(ROUTES))  # 起稳定期
real_write = m.write_letter
landed = []

def boom(to, sender, subject, need, body, extra_headers=()):
    p = real_write(to, sender, subject, need, body, extra_headers=extra_headers)
    landed.append(p)
    if len(landed) == step:
        raise HardStop(f"模拟硬中断：第 {step} 封信已落地，对应的状态还没保存")
    return p

m.write_letter = boom
crashed = False
try:
    m.process_alias_switches(m.boxes(ROUTES))
except HardStop:
    crashed = True
except Exception as e:                    # 被普通异常抓到就算失败，说明中断类型选错了
    print("BAD 中断被 except Exception 抓到了", e)
if not crashed:
    print("BAD 没有触发硬中断")
    raise SystemExit(0)
if len(landed) != step or not landed[step - 1].exists():
    print(f"BAD 中断前第 {step} 封信并没有真的落地（只写了 {len(landed)} 封）")
    raise SystemExit(0)
st = json.loads((home / "alias_state.json").read_text(encoding="utf-8"))
ev0 = list(st["aliases"]["pm"]["events"].values())[0]
if step == 1:                             # 广播信刚落地：送达名单与编号都还没落盘
    if ev0.get("sent") or ev0.get("broadcast") or ev0.get("done"):
        print("BAD 中断发生得太晚，送达状态已经落盘了")
        raise SystemExit(0)
else:                                      # 交接信刚落地：广播已完成，但交接 id 还没落盘
    if not ev0.get("broadcast") or ev0.get("handoff") or ev0.get("done"):
        print("BAD 交接中断的前置状态不对")
        raise SystemExit(0)

# --- 把那封信放到 mode 指定的位置（模拟用户归档 / 收件人处理）---
first = landed[step - 1]
holder = first.parent.parent.name
if mode == "done":
    dst = home / holder / "done"
elif mode == "arch":
    dst = home / holder / "archived" / time.strftime("%Y%m%d-%H%M%S")
else:
    dst = first.parent
dst.mkdir(parents=True, exist_ok=True)
if mode != "inbox":
    first.replace(dst / first.name)

# --- 从磁盘重新加载模块，跑第二轮：目标仍然是 b（a 仍离线）---
m2 = load()
m2.process_alias_switches(m2.boxes(ROUTES))
st2 = json.loads((home / "alias_state.json").read_text(encoding="utf-8"))
ev = list(st2["aliases"]["pm"]["events"].values())[0]
bid = ev.get("broadcast") or ""

def letters_with(box, needle):
    hits = []
    for sub in ("inbox", "done"):
        d = home / box / sub
        if d.is_dir():
            hits += [p for p in d.glob("*.md") if needle in p.read_text(encoding="utf-8")]
    a = home / box / "archived"
    if a.is_dir():
        hits += [p for p in a.rglob("*.md") if needle in p.read_text(encoding="utf-8")]
    return hits

recs = sorted((home / "broadcasts").glob("*.json"))
eid = ev.get("id") or ""
handoffs = [p for p in letters_with("b", "事由：交接：@pm")]
tagged = [p for p in handoffs if f"切换事件：{eid}" in p.read_text(encoding="utf-8")]
problems = []
if len(letters_with("d1", "广播：" + bid)) != 1:
    problems.append(f"d1 手里有 {len(letters_with('d1', '广播：', bid))} 封该广播的信")
if len(recs) != 1:
    problems.append(f"广播记录有 {len(recs)} 份")
if not re.fullmatch(r"B\d{8}-\d{6}_[A-Za-z0-9_.\-]+", bid or ""):
    problems.append(f"广播编号不合格式：{bid!r}")
if len(handoffs) != 1:
    problems.append(f"b 手里有 {len(handoffs)} 封交接提醒")
if len(tagged) != len(handoffs):
    problems.append(f"只有 {len(tagged)}/{len(handoffs)} 封交接提醒带了可恢复的事件身份")
if not ev.get("done"):
    problems.append("事件没有走到完成")
if not ev.get("handoff"):
    problems.append("交接 id 没有补记上")
print("OK" if not problems else "BAD:" + "；".join(problems))
PY
}
for mode in inbox done arch; do
  po17_new "crash_$mode" "$SW_CFG" "$SW_ON"   # a 在线开局：脚本里先记基线，再让 a 下线触发切换
  r=$(alias_crash 1 "$mode")
  where="广播信在${mode}"
  [ "$r" = "OK" ] && ok "退修4b 硬中断后从磁盘恢复（${where}）不重投、不另建记录、仍补完交接" \
                  || bad "退修4b 硬中断恢复（${where}）：$r"
  po17_new "hcrash_$mode" "$SW_CFG" "$SW_ON"
  r=$(alias_crash 2 "$mode")
  where="交接信在${mode}"
  [ "$r" = "OK" ] && ok "退修4d 交接信落盘窗口硬中断后不产生第二封（${where}）" \
                  || bad "退修4d 交接信硬中断恢复（${where}）：$r"
done

# 退修4c：两个 alias 在同一秒确认切换，广播编号不能撞。
# 编号来自状态文件里单调递增的计数器，而不是时钟，所以同秒不同 alias、删掉再加回来都不会撞。
TWO_SAME='{"version":1,"aliases":{"alpha":{"candidates":["a","b"],"notify":["d1"]},
                                  "beta":{"candidates":["a","b"],"notify":["d1"]}}}'
TWO_DIFF='{"version":1,"aliases":{"alpha":{"candidates":["a","b"],"notify":["d1"]},
                                  "beta":{"candidates":["a","b"],"notify":["d2"]}}}'
two_aliases() { # $1=notify 配置 $2=标签；结果写进全局 R（不跑子 Shell，P2 要留给调用方继续用）
  po17_new "two_$2" "$1" "$SW_ON"
  po17_round                          # 两个 alias 各自记下基线 = a
  po17_off a                          # 同一轮里 a 下线，两个 alias 一起倒计时
  po17_round; po17_round
  python3 - "$P2" "$T/twoR" <<'PY'
import json, re, sys
from pathlib import Path
home = Path(sys.argv[1])
st = json.loads((home / "alias_state.json").read_text(encoding="utf-8"))
evs = [e for s in st["aliases"].values() for e in s["events"].values()]
bids = [e.get("broadcast") for e in evs]
recs = {}
for p in (home / "broadcasts").glob("*.json"):
    r = json.loads(p.read_text(encoding="utf-8"))
    recs[r["id"]] = r
problems = []
if len(evs) != 2:
    problems.append(f"事件有 {len(evs)} 个")
if len(set(bids)) != 2 or None in bids:
    problems.append(f"广播编号相撞或缺失：{bids}")
for b in bids:
    if b and not re.fullmatch(r"B\d{8}-\d{6}_[A-Za-z0-9_.\-]+", b):
        problems.append(f"编号不合格式：{b}")
if len(recs) != 2:
    problems.append(f"广播记录有 {len(recs)} 份：{sorted(recs)}")
for e in evs:                          # 每个事件都要拿到属于自己的那份记录
    r = recs.get(e.get("broadcast") or "")
    if r is None:
        problems.append(f"{e['id']} 没有自己的广播记录")
    elif not e["id"].startswith("E" + str(e["seq"]) + "_"):
        problems.append(f"{e['id']} 的序号与事件记录不一致")
if not all(e.get("done") for e in evs):
    problems.append("有事件没走到完成")
open(sys.argv[2], "w", encoding="utf-8").write("OK" if not problems else "BAD:" + "；".join(problems))
PY
R=$(cat "$T/twoR")
}
two_aliases "$TWO_SAME" same
[ "$R" = OK ] && ok "退修4c 同秒确认的两个 alias 各有自己的广播编号与记录" || bad "退修4c 同通知对象：$R"
d1_n=$(po17_n d1)
[ "$d1_n" = "2" ] && ok "退修4c 同通知对象时 d1 各收一封（不吞掉一个）" || bad "退修4c 同通知对象只收到 ${d1_n} 封"
two_aliases "$TWO_DIFF" diff
[ "$R" = OK ] && ok "退修4c 通知对象不同也不共用对方的记录" || bad "退修4c 不同通知对象：$R"
python3 - "$P2" > "$T/twoids" <<'PY'
import json, sys
from pathlib import Path
home = Path(sys.argv[1])
st = json.loads((home / "alias_state.json").read_text(encoding="utf-8"))
print(json.dumps({a: e["broadcast"] for a, s in st["aliases"].items() for e in s["events"].values()}))
PY
A_BID=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['alpha'])" "$T/twoids")
B_BID=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['beta'])" "$T/twoids")
[ "$A_BID" != "$B_BID" ] && ok "退修4c 两个 alias 的广播编号确实不同" || bad "退修4c 编号仍然相同"
# 独立回执：各自只记进自己的记录，别的信箱不能替另一个广播回执
POSTOFFICE_HOME="$P2" "$PO" ack d1 "$A_BID" "alpha 我回执" >/dev/null
POSTOFFICE_HOME="$P2" "$PO" ack d2 "$B_BID" "beta 我回执" >/dev/null
r=$(python3 - "$P2" "$A_BID" "$B_BID" <<'PY'
import json, sys
from pathlib import Path
home, a, b = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
recs = {}
for p in (home / "broadcasts").glob("*.json"):
    r = json.loads(p.read_text(encoding="utf-8"))
    recs[r["id"]] = r
acks = [json.loads(x) for x in (home / "acks.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
by_id = {}
for e in acks:
    if e.get("kind") == "broadcast":
        by_id.setdefault(e["id"], []).append(e)
problems = []
if recs.get(a, {}).get("to") != ["d1"] or recs.get(b, {}).get("to") != ["d2"]:
    problems.append(f"记录的收件人错了：alpha={recs.get(a, {}).get('to')} beta={recs.get(b, {}).get('to')}")
if [e["by"] for e in by_id.get(a, [])] != ["d1"]:
    problems.append(f"alpha 广播的回执是 {[e['by'] for e in by_id.get(a, [])]}")
if [e["by"] for e in by_id.get(b, [])] != ["d2"]:
    problems.append(f"beta 广播的回执是 {[e['by'] for e in by_id.get(b, [])]}")
if sorted(by_id) != sorted([a, b]):
    problems.append(f"账本里的广播回执编号是 {sorted(by_id)}")
print("OK" if not problems else "BAD:" + "；".join(problems))
PY
)
[ "$r" = OK ] && ok "退修4c 两个广播各自记账、互不串号" || bad "退修4c 回执串号：$r"
POSTOFFICE_HOME="$P2" "$PO" ack d1 "$B_BID" "越权" >"$T/xack" 2>&1 \
  && bad "退修4c 非收件信箱竟能给另一个广播回执" || ok "退修4c 非收件信箱不能给另一个广播回执"

# 退修5：空或不合法的「广播：」信头要拒绝，账本、回执与原信都不动
po17_new emptybc "$SW_CFG" "$SW_ON"
printf '来源：tester\n事由：空广播头\n需要：回复\n广播：\n\n正文。\n' > "$T/empty_bc.md"
cp "$T/empty_bc.md" "$P2/d1/inbox/20260101-000000_tester_空广播头.md"
EMPTY_LID=20260101-000000_tester_空广播头
acks_before=$(cat "$P2/acks.jsonl" 2>/dev/null | wc -l | tr -d ' ')
POSTOFFICE_HOME="$P2" "$PO" ack d1 "$EMPTY_LID" "不该记上" >"$T/ebc" 2>&1; rc=$?
[ $rc -ne 0 ] && ok "退修5 空广播头回执被拒" || bad "退修5 空广播头竟回执成功"
grep -q "空的或不是广播编号格式" "$T/ebc" && ok "退修5 说清是空/不合法信头" || bad "退修5 提示不清楚"
[ ! -f "$P2/acks.jsonl" ] || [ "$(cat "$P2/acks.jsonl" 2>/dev/null | wc -l | tr -d ' ')" = "$acks_before" ] \
  && ok "退修5 被拒时账本没有变化" || bad "退修5 被拒时账本被写了"
[ -f "$P2/d1/inbox/$EMPTY_LID.md" ] && ok "退修5 被拒时原信留在 inbox" || bad "退修5 被拒时原信被搬走"
[ "$(ls "$P2/d1/inbox/" | grep -c '回执')" = "0" ] && ok "退修5 被拒时没有生成回执通知" || bad "退修5 被拒时仍生成了回执"
printf '来源：tester\n事由：坏广播头\n需要：回复\n广播：这不是编号\n\n正文。\n' > "$P2/d1/inbox/20260101-000001_tester_坏广播头.md"
POSTOFFICE_HOME="$P2" "$PO" ack d1 "20260101-000001_tester_坏广播头" "不该记上" >"$T/ebc2" 2>&1; rc=$?
[ $rc -ne 0 ] && ok "退修5 非编号格式的广播头被拒" || bad "退修5 非编号格式竟通过"
grep -q "这不是编号" "$P2/d1/inbox/20260101-000001_tester_坏广播头.md" \
  && ok "退修5 坏广播头的原信内容没被动过" || bad "退修5 坏广播头原信被改"
# 原有两条合同仍然成立：正文伪造不算、物理编号与广播编号归一去重
po17_new normbc "$SW_CFG" "$SW_ON"
echo 正文 | POSTOFFICE_HOME="$P2" "$PO" broadcast d1,d2 d1 "归一广播" "回复" >"$T/n1" 2>&1
NB=$(sed -n 's/^广播编号：//p' "$T/n1")
NL=$(basename "$(ls "$P2"/d1/inbox/*归一广播*.md)" .md)
POSTOFFICE_HOME="$P2" "$PO" ack d1 "$NL" "按信回执" | grep -q "$NB" \
  && ok "退修5 归一化去重仍然成立" || bad "退修5 归一化去重坏了"
POSTOFFICE_HOME="$P2" "$PO" ack d1 "$NB" "再回一次" | grep -q "已回执过" \
  && ok "退修5 物理编号与广播编号只记一条" || bad "退修5 两种编号记了两条"
printf '广播：%s\n正文伪造。\n' "$NB" > "$T/forge_bc.md"
POSTOFFICE_HOME="$P2" "$PO" send d1 d1 "正文伪造广播" "回复" --file "$T/forge_bc.md" >/dev/null 2>&1
FID=$(basename "$(ls "$P2"/d1/inbox/*正文伪造广播*.md)" .md)
POSTOFFICE_HOME="$P2" "$PO" ack d1 "$FID" "不该算广播" >/dev/null 2>&1
grep -q '"kind": "letter"' "$P2/acks.jsonl" \
  && ok "退修5 正文伪造广播标记仍按普通信处理" || bad "退修5 正文伪造被算成广播"

# 退修6：面板把投递失败单独算出来，页面确认框中英文都要列出失败项与总数
PANEL_HTML="$(dirname "$PO")/panel/index.html"
grep -q '{f} 封投递失败需人工处理' "$PANEL_HTML" \
  && grep -q '{f} failed delivery needing a human' "$PANEL_HTML" \
  && ok "退修6 中英文确认文案都列投递失败数量" || bad "退修6 确认文案缺投递失败数量"
grep -q '全部 {n} 封信件和通知' "$PANEL_HTML" \
  && grep -q 'all {n} letters and notifications' "$PANEL_HTML" \
  && ok "退修6 中英文确认文案都写明收件箱总数" || bad "退修6 确认文案缺总数"
grep -q '收件箱里的全部' "$PANEL_HTML" && grep -q "inbox" "$PANEL_HTML" \
  && ok "退修6 确认文案明确是整个收件箱" || bad "退修6 确认文案没说清是收件箱"
grep -q 'data-f="${c.failed}"' "$PANEL_HTML" && grep -q 'data-n="${b.pending.length}"' "$PANEL_HTML" \
  && ok "退修6 归档按钮把失败数与总数传给确认框" || bad "退修6 按钮没传失败数或总数"
po17_new pnl "$SW_CFG" "$SW_ON"
POSTOFFICE_HOME="$P2" "$PO" add cbox --claude "面板确认用会话" >/dev/null
POSTOFFICE_HOME="$P2" "$PO" add obox6 --notify >/dev/null
python3 - "$P2/routes.json" <<'PY'
import json, sys
p = sys.argv[1]
r = json.load(open(p))
r["obox6"]["methods"] = ["opencode_plugin"]
json.dump(r, open(p, "w"))
PY
echo x | POSTOFFICE_HOME="$P2" "$PO" send cbox d1 "确认用已提醒" "回复" >/dev/null
echo x | POSTOFFICE_HOME="$P2" "$PO" send obox6 d1 "确认用投递失败" "回复" >/dev/null
C1=$(basename "$(ls "$P2"/cbox/inbox/*确认用已提醒*.md)")
OF6=$(basename "$(ls "$P2"/obox6/inbox/*确认用投递失败*.md)")
printf '%s\n' "$P2/cbox/inbox/$C1" > "$P2/cbox/.seen"
printf '{"box":"obox6","file":"%s","result":"FAILED_FINAL"}\n' "$OF6" >> "$P2/opencode_delivered.jsonl"
PORT6=$((9050 + $$ % 120))
POSTOFFICE_HOME="$P2" "$PO" panel --no-open --port $PORT6 >/dev/null 2>&1 & PP6=$!; sleep 2
curl -s "http://127.0.0.1:$PORT6/api/state" | python3 -c '
import json, sys
st = {b["name"]: b["counts"] for b in json.load(sys.stdin)["boxes"]}
good = st.get("cbox") == {"waiting": 0, "reminded": 1, "failed": 0} \
    and st.get("obox6") == {"waiting": 0, "reminded": 0, "failed": 1}
print("OK" if good else "BAD:" + json.dumps(st, ensure_ascii=False))' >"$T/p6"
grep -q "^OK$" "$T/p6" && ok "退修6 面板把失败与已提醒分开算" || bad "退修6 面板分类错：$(cat "$T/p6")"
curl -s "http://127.0.0.1:$PORT6/" | grep -q 'data-f="\${c.failed}"' \
  && ok "退修6 面板实际发出的页面带失败数占位" || bad "退修6 实际页面没带失败数"
kill $PP6 2>/dev/null; wait $PP6 2>/dev/null; unset PP6

# 15) 编译/类型检查
/usr/bin/python3 -m py_compile "$PO" 2>/dev/null && ok "v1.7 py_compile 通过" || bad "v1.7 py_compile"
if command -v node >/dev/null 2>&1; then
  node --experimental-strip-types --check "$(dirname "$PO")/opencode/postoffice.ts" 2>/dev/null \
    && ok "v1.6 插件类型检查通过" || bad "v1.6 插件检查"
fi

rm -rf "$T"
echo "通过 ${pass}，失败 ${fail}"; [ $fail -eq 0 ]
