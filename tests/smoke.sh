#!/bin/bash
# 冒烟测试：全部在临时目录里跑，不碰真实的 ~/.claude、~/agent-postoffice。
set -u
PO="$(cd "$(dirname "$0")/.." && pwd)/postoffice"
T=$(mktemp -d); export HOME="$T" POSTOFFICE_HOME="$T/po" POSTOFFICE_NO_NOTIFY=1
pass=0; fail=0
ok()  { echo "✅ $1"; pass=$((pass+1)); }
bad() { echo "❌ $1"; fail=$((fail+1)); }
hook_in() { echo "{\"transcript_path\":\"$T/$1.jsonl\"}"; }
hook_cli() { printf '{"transcript_path":"%s/%s.jsonl","session_id":"%s"}' "$T" "$1" "$2"; }
route_field() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]].get(sys.argv[3],''))" "$POSTOFFICE_HOME/routes.json" "$1" "$2"; }
route_set() { python3 -c "import json,sys;p=sys.argv[1];r=json.load(open(p));r[sys.argv[2]][sys.argv[3]]=sys.argv[4];json.dump(r,open(p,'w'))" "$POSTOFFICE_HOME/routes.json" "$1" "$2" "$3"; }
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

# ---------- v1.6：稳定认人 + 只为真阻塞提醒 + 回执合并排后（docs/SPEC_v1.6.md rev2） ----------
mkcap() { printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\n' "$1" > "$2"; chmod +x "$2"; }
mkfail() { printf '#!/bin/sh\nexit 1\n' > "$1"; chmod +x "$1"; }
clog() { printf '%s Mapping internal session %s to CLI session %s\n' "$1" "$2" "$3" >> "$T/main.log"; }
titleonly() { printf '{"transcript_path":"%s/%s.jsonl"}' "$T" "$1"; }
DHD() { env CLAUDE_CODE_ENTRYPOINT=claude-desktop POSTOFFICE_CLAUDE_MAINLOG="$T/main.log" "$@"; }

# 1) 桌面版无 HOST：按 main.log 反查身份；首次标题匹配自动绑定；改名后仍认人并更新标题
: > "$T/main.log"
clog "2026-10-05 02:00:01" local_L1 cli-L1
printf '{"type":"custom-title","customTitle":"稳定会话"}\n' > "$T/s1.jsonl"
"$PO" add sbox --claude "稳定会话" >/dev/null
echo "正文" | "$PO" send sbox tester "首信" "回复" >/dev/null
out=$(hook_cli s1 cli-L1 | DHD "$PO" hook 2>&1); rc=$?
[ $rc -eq 2 ] && printf '%s' "$out" | grep -q "【联络总站新信" && ok "v1.6 桌面日志反查身份并唤醒" || bad "v1.6 日志认人 rc=$rc"
[ "$(route_field sbox claude_session)" = "local_L1" ] && ok "v1.6 首次标题匹配自动绑定身份" || bad "v1.6 自动绑定"
printf '{"type":"custom-title","customTitle":"改过的名字"}\n' > "$T/s1.jsonl"
echo "正文" | "$PO" send sbox tester "改名后" "回复" >/dev/null
out=$(hook_cli s1 cli-L1 | DHD "$PO" hook 2>&1); rc=$?
[ $rc -eq 2 ] && ok "v1.6 会话改名后仍按身份认人" || bad "v1.6 改名后认人 rc=$rc"
[ "$(route_field sbox claude_title)" = "改过的名字" ] && ok "v1.6 认人时顺手更新标题" || bad "v1.6 更新标题"

# 2) 回退：同一身份换了新的 CLI id 照样认人；重名映射按行内时间戳取最新
clog "2026-10-05 02:05:00" local_L1 cli-L2
printf '{"type":"custom-title","customTitle":"回退后"}\n' > "$T/s1.jsonl"
echo "正文" | "$PO" send sbox tester "回退后" "回复" >/dev/null
out=$(hook_cli s1 cli-L2 | DHD "$PO" hook 2>&1); rc=$?
[ $rc -eq 2 ] && ok "v1.6 回退换了 CLI id 照样认人" || bad "v1.6 回退认人 rc=$rc"
clog "2026-10-05 01:00:00" local_OLD cli-L3
clog "2026-10-05 03:00:00" local_NEW cli-L3
printf '{"type":"custom-title","customTitle":"时间戳会话"}\n' > "$T/s3.jsonl"
"$PO" add tbox --claude "时间戳会话" >/dev/null
echo "正文" | "$PO" send tbox tester "时间戳" "回复" >/dev/null
out=$(hook_cli s3 cli-L3 | DHD "$PO" hook 2>&1); rc=$?
[ $rc -eq 2 ] && [ "$(route_field tbox claude_session)" = "local_NEW" ] \
  && ok "v1.6 多个映射取最新行内时间戳" || bad "v1.6 时间戳映射=$(route_field tbox claude_session)"

# 3) 首选 HOST_SESSION_ID（桌面版），改名不影响
printf '{"type":"custom-title","customTitle":"主机会话"}\n' > "$T/h.jsonl"
"$PO" add hostbox --claude "主机会话" >/dev/null
echo "正文" | "$PO" send hostbox tester "主机信" "回复" >/dev/null
out=$(hook_cli h cli-ignored | env CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_HOST "$PO" hook 2>&1); rc=$?
[ $rc -eq 2 ] && [ "$(route_field hostbox claude_session)" = "local_HOST" ] \
  && ok "v1.6 优先用 HOST_SESSION_ID 绑定" || bad "v1.6 HOST 绑定 rc=$rc"
printf '{"type":"custom-title","customTitle":"改个名"}\n' > "$T/h.jsonl"
echo "正文" | "$PO" send hostbox tester "主机信2" "回复" >/dev/null
out=$(hook_cli h whatever | env CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_HOST "$PO" hook 2>&1); rc=$?
[ $rc -eq 2 ] && ok "v1.6 HOST 身份改名后仍认人" || bad "v1.6 HOST 改名 rc=$rc"

# 4) 无映射：桌面版退回标题匹配（未绑定信箱），不臆造绑定，钩子不报错
printf '{"type":"custom-title","customTitle":"无日志会话"}\n' > "$T/nl.jsonl"
"$PO" add nolog --claude "无日志会话" >/dev/null
echo "正文" | "$PO" send nolog tester "无日志" "回复" >/dev/null
out=$(hook_cli nl cli-NL | env CLAUDE_CODE_ENTRYPOINT=claude-desktop POSTOFFICE_CLAUDE_MAINLOG="$T/nope.log" "$PO" hook 2>&1); rc=$?
[ $rc -eq 2 ] && ok "v1.6 日志缺失退回标题匹配" || bad "v1.6 日志缺失 rc=$rc"
[ -z "$(route_field nolog claude_session)" ] && ok "v1.6 退回标题匹配不臆造绑定" || bad "v1.6 误绑定"

# 5) 认人负例：同名不接管 / 多重绑定 / 未解析撞已绑定 / 命令行用 CLI id；每种诊断只通知一次
rm -f "$POSTOFFICE_HOME/logs/notify.log"
"$PO" add twin --claude "撞名标题" >/dev/null; route_set twin claude_session local_TAKEN
printf '{"type":"custom-title","customTitle":"撞名标题"}\n' > "$T/tw.jsonl"
echo "正文" | "$PO" send twin tester "撞名信" "回复" >/dev/null
out=$(hook_cli tw x | env CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_FRESH "$PO" hook 2>&1); rc=$?
[ $rc -eq 0 ] && ok "v1.6 同名不同身份不接管" || bad "v1.6 撞名接管 rc=$rc"
hook_cli tw x | env CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_FRESH "$PO" hook >/dev/null 2>&1
[ "$(grep -c "会话认人诊断" "$POSTOFFICE_HOME/logs/notify.log" 2>/dev/null)" = "1" ] && ok "v1.6 同名只诊断一次" || bad "v1.6 同名重复诊断"
"$PO" add dupa --claude "绑定甲" >/dev/null; route_set dupa claude_session local_DUP
"$PO" add dupb --claude "绑定乙" >/dev/null; route_set dupb claude_session local_DUP
echo "正文" | "$PO" send dupa tester "多重绑定信" "回复" >/dev/null
out=$(hook_cli tw x | env CLAUDE_CODE_ENTRYPOINT=claude-desktop CLAUDE_CODE_HOST_SESSION_ID=local_DUP "$PO" hook 2>&1); rc=$?
[ $rc -eq 0 ] && ok "v1.6 身份被多重绑定则不认任何一个" || bad "v1.6 多重绑定认了 rc=$rc"
"$PO" add n2 --claude "N2" >/dev/null; route_set n2 claude_session local_X
printf '{"type":"custom-title","customTitle":"N2"}\n' > "$T/n2.jsonl"
echo "正文" | "$PO" send n2 tester "无身份信" "回复" >/dev/null
out=$(titleonly n2 | "$PO" hook 2>&1); rc=$?
[ $rc -eq 0 ] && ok "v1.6 身份缺失撞已绑定信箱则不认领" || bad "v1.6 未解析却认领 rc=$rc"
"$PO" add clibox --claude "命令行会话" >/dev/null
printf '{"type":"custom-title","customTitle":"命令行会话"}\n' > "$T/cb.jsonl"
echo "正文" | "$PO" send clibox tester "命令行信" "回复" >/dev/null
out=$(hook_cli cb cli-CMD | env -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_HOST_SESSION_ID "$PO" hook 2>&1); rc=$?
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
[ "$(grep -c "【联络总站新信" "$T/merge_cap")" = "3" ] && ok "v1.6 正式信各自先送" || bad "v1.6 正式信"
fm=$(grep -n "另有 3 条回执" "$T/merge_cap" | cut -d: -f1); lf=$(grep -n "【联络总站新信" "$T/merge_cap" | tail -1 | cut -d: -f1)
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

# 11) 编译/类型检查
/usr/bin/python3 -m py_compile "$PO" 2>/dev/null && ok "v1.6 py_compile 通过" || bad "v1.6 py_compile"
if command -v node >/dev/null 2>&1; then
  node --experimental-strip-types --check "$(dirname "$PO")/opencode/postoffice.ts" 2>/dev/null \
    && ok "v1.6 插件类型检查通过" || bad "v1.6 插件检查"
fi

rm -rf "$T"
echo "通过 ${pass}，失败 ${fail}"; [ $fail -eq 0 ]
