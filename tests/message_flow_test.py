#!/usr/bin/env python3
"""消息流（message flow）行为测试：A 编辑/撤回、B 批量唤醒（hook / codex 邮递员）、C archive-current。

状态：本文件写于这三项能力尚未实现时，预期在当前代码上全部失败（RED）。每条断言只钉住冻结
的行为契约，不钉实现细节。跑测试不需要、也不许改生产代码。

冻结契约（摘要）：
  A) 稳定引用 ref = <recipient>/<message-id>；`send` 成功后除 `编号：` 外还要打印 `引用：<to>/<id>`；
     底层 UPDATE：`postoffice edit --box <sender> <to>/<id> [--subject S] [--need N] [--body B]`
     （至少一个字段，--body 是字面文本），成功提示含 `已更新` 与 `尚未收到`；REVOKE 复用
     `postoffice retract`，成功提示含 `已撤回` 与 `不会再`；已投递/认领在途/未知通道一律
     fail closed（非零、原信一字节不动，提示含「投递」「另发」）；来源不符、派生通知
     （回执/广播/闹钟/切换事件/来源 postoffice）拒绝；id 必须精确。
  B) MAX_FORMAL_BATCH = 20；普通正式信各自文件/认领/台账，一次唤醒一批（≤20），
     一批只记一次限流、每封分别记账；闹钟不混进普通批；回执仍是尾部合并块且正文永不注入；
     Claude hook 本来就同轮多封一次唤醒，这里只做 regression 断言（不要求换成别的形态）。
  C) `<box>/.presented.json` 是「已成功展示但未归档」的裸 id 集合（union）；唤醒被通道接受
     后才写；`postoffice archive-current --box <box> [--keep <id> ...]` 只处理 presented，
     presented 之外的（例如之后新到的信）原地不动。

全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，不碰真实的 ~/agent-postoffice；不写死绝对路径。
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_mflow_"))
TITLE = "消息流标题"
SENTINEL = "SENTINEL-BODY-mflow-9d31"


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    # wake-coalesce：旧夹具按「信随到随投」的旧世界书写；合批/静默窗本身由
    # tests/wake_coalesce_test.py 专测。这里关掉窗口（0/0），保持旧用例语义不变。
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_QUIET"] = "0"
    env["POSTOFFICE_MAX_HOLD"] = "0"
    env["POSTOFFICE_POLL"] = "1"
    # 与运行环境的 Claude 桌面变量隔离：钩子认人只认用例显式给的值
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    env.update(extra)
    return env


def run_po(*args, home=None, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home or BASE), input=stdin, timeout=timeout)


def load_po(name="po_mflow_mod"):
    """Import the postoffice CLI as a module to exercise its pure index helpers directly."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(name, PO)
    spec = importlib.util.spec_from_file_location(name, PO, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def hook(home, transcript, wait=12):
    """真跑一次 `postoffice hook`，交回 (退出码, stderr 唤醒负载)。

    2 = 唤醒了会话，0 = 转身走了，124 = 我们放弃时它还在监视（没有可唤醒的东西）。
    """
    p = subprocess.Popen([sys.executable, PO, "hook"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         env=env_for(home), text=True)
    p.stdin.write(json.dumps({"transcript_path": str(transcript)}))
    p.stdin.close()
    p.stdin = None          # communicate() 会去 flush 已关闭的 stdin，这里明确断掉
    deadline = time.time() + wait
    while time.time() < deadline:
        if p.poll() is not None:
            return p.returncode, p.communicate()[1]
        time.sleep(0.1)
    p.kill()
    return 124, p.communicate()[1]


def postman(home, seconds=2.5, timeout=60):
    p = subprocess.Popen([sys.executable, PO, "postman"], env=env_for(home),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(seconds)
    finally:
        p.terminate()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    return p.returncode


class MessageFlow(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ("boss", "worker", "claude", "coded", "spy"):
            (self.home / box / "inbox").mkdir(parents=True)
            (self.home / box / "done").mkdir(parents=True)
        run_po("add", "boss", "--notify", home=self.home)
        run_po("add", "worker", "--notify", home=self.home)
        run_po("add", "claude", "--claude", TITLE, home=self.home)
        run_po("add", "coded", "--notify", home=self.home)
        run_po("add", "spy", "--notify", home=self.home)
        self.set_methods("worker", "opencode_plugin")
        self.set_methods("claude", "claude_hook")
        self.fake = self.fake_codex()
        self.set_methods("coded", "codex_queue", codex_cli=str(self.fake), thread_id="th-x")

    # -- helpers ---------------------------------------------------------
    def set_methods(self, box, method, **extra):
        p = self.home / "routes.json"
        r = json.loads(p.read_text())
        r[box]["methods"] = [method]
        r[box].update(extra)
        p.write_text(json.dumps(r, ensure_ascii=False))

    def send(self, to, sender="boss", subject="测试信", need="回复", body="正文\n"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [x for x in out.stdout.splitlines() if x.startswith("编号：")][0].split("：", 1)[1].strip()

    def send_out(self, to, sender="boss", subject="测试信", need="回复", body="正文\n"):
        return run_po("send", to, sender, subject, need, home=self.home, stdin=body)

    def letter_path(self, box, lid):
        return self.home / box / "inbox" / f"{lid}.md"

    def inbox(self, box):
        return sorted(p.name for p in (self.home / box / "inbox").glob("*.md"))

    def done(self, box):
        return sorted(p.name for p in (self.home / box / "done").glob("*.md"))

    def archived(self, box):
        return sorted(str(p.relative_to(self.home)) for p in (self.home / box / "archived").rglob("*.md"))

    def claims(self, box):
        d = self.home / box / ".claims"
        return sorted(p.name for p in d.glob("*")) if d.is_dir() else []

    def claim(self, box, lid, pid="12345"):
        d = self.home / box / ".claims"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{lid}.md").write_text(pid + "\n")

    def oc_ledger(self, box, lid, result="DELIVERED"):
        with (self.home / "opencode_delivered.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"box": box, "file": f"{lid}.md", "result": result}) + "\n")

    def woken(self):
        p = self.home / ".woken.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def wake_times(self, box):
        f = self.home / box / ".wake_times"
        return f.read_text().split() if f.exists() else []

    def presented(self, box):
        p = self.home / box / ".presented.json"
        return set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()

    def write_presented(self, box, ids):
        (self.home / box / ".presented.json").write_text(json.dumps(list(ids), ensure_ascii=False))

    def title_transcript(self, title=TITLE):
        tp = self.home / "t.jsonl"
        tp.write_text(json.dumps({"type": "custom-title", "customTitle": title}) + "\n")
        return tp

    def fake_codex(self):
        """假 codex app-server：说 stdio JSON-RPC，维护一个持久 pending 队列并记录每次 add/update 的索引文本。

        忙时 add 留在 pending、update 就地替换；idle 时 add 立即被消费（不入 pending），模拟真实语义。
        状态与 capture 都存在脚本同目录，跨进程（每轮邮递员各起一次）持久。索引文本仍用哨兵包裹，
        所以 codex_calls() 与既有断言（稳定引用/批次形态）保持不变。
        """
        script = self.home / "fake_codex.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "D = os.path.dirname(os.path.abspath(__file__))\n"
            "STATE = os.path.join(D, 'fake_codex_state.json')\n"
            "CAP = os.path.join(D, 'codex.calls')\n"
            "def load():\n"
            "    try:\n"
            "        return json.load(open(STATE))\n"
            "    except Exception:\n"
            "        return {'busy': True, 'pending': [], 'seq': 0}\n"
            "def save(s):\n"
            "    open(STATE, 'w').write(json.dumps(s))\n"
            "def emit(o):\n"
            "    sys.stdout.write(json.dumps(o) + '\\n'); sys.stdout.flush()\n"
            "def text_of(p):\n"
            "    inp = p.get('input') or []\n"
            "    return '\\n'.join(x.get('text','') for x in inp if isinstance(x, dict) and x.get('type')=='text')\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line: continue\n"
            "    try: msg = json.loads(line)\n"
            "    except ValueError: continue\n"
            "    m, rid, p = msg.get('method'), msg.get('id'), (msg.get('params') or {})\n"
            "    s = load()\n"
            "    if m == 'initialize':\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'userAgent':'fake-codex','codexHome':D,'platformFamily':'unix','platformOs':'macos'}})\n"
            "    elif m == 'thread/queue/list':\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'data':s['pending'],'nextCursor':None}})\n"
            "    elif m == 'thread/queue/add':\n"
            "        open(CAP,'a').write('===8<===\\n' + text_of(p) + '\\n===>8===\\n')\n"
            "        if s['busy']:\n"
            "            s['seq'] += 1\n"
            "            s['pending'].append({'id':'q%d'%s['seq'],'input':p.get('input'),'clientUserMessageId':p.get('clientUserMessageId')})\n"
            "        save(s)\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'queuedSubmission':None}})\n"
            "    elif m == 'thread/queue/update':\n"
            "        open(CAP,'a').write('===8<===\\n' + text_of(p) + '\\n===>8===\\n')\n"
            "        for it in s['pending']:\n"
            "            if it['id'] == p.get('queuedSubmissionId'):\n"
            "                it['input'] = p.get('input')\n"
            "        save(s)\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'queuedSubmission':None}})\n"
            "    else:\n"
            "        emit({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'method not found'}})\n",
            encoding="utf-8")
        script.chmod(0o755)
        return script

    def codex_calls(self):
        f = self.home / "codex.calls"
        if not f.exists():
            return []
        return re.findall(r"===8<===\n(.*?)\n===>8===",
                          f.read_text(encoding="utf-8", errors="replace"), re.S)

    def fake_pending(self):
        f = self.home / "fake_codex_state.json"
        return json.loads(f.read_text(encoding="utf-8"))["pending"] if f.exists() else []

    def fake_pending_text(self):
        return "\n\n".join("".join(x.get("text", "") for x in (it.get("input") or [])
                                   if isinstance(x, dict) and x.get("type") == "text")
                           for it in self.fake_pending())

    def _pending_formal_count(self, it):
        text = "".join(x.get("text", "") for x in (it.get("input") or [])
                       if isinstance(x, dict) and x.get("type") == "text")
        return len(re.findall(r"^\d+\. \S+/", text, re.M))

    # ==================================================================== A
    # -- A1: send prints the stable ref -----------------------------------
    def test_send_prints_the_stable_ref_alongside_the_id(self):
        out = self.send_out("worker", subject="引用测试")
        self.assertEqual(out.returncode, 0, out.stderr)
        lid = [x for x in out.stdout.splitlines() if x.startswith("编号：")][0].split("：", 1)[1].strip()
        self.assertIn(f"引用：worker/{lid}", out.stdout,
                      "send 成功后必须打印可直接用于 edit/retract 的稳定引用：\n" + out.stdout)

    # -- A2: update in place ----------------------------------------------
    def test_edit_updates_only_subject_need_and_body_in_place(self):
        lid = self.send("worker", subject="旧事由", need="回复", body="旧正文，来自旧版本。\n")
        p = self.letter_path("worker", lid)
        self.assertTrue(p.exists())
        out = run_po("edit", "--box", "boss", f"worker/{lid}",
                     "--subject", "新事由", "--need", "仅告知",
                     "--body", "新正文：这一段是字面文本 --subject 不是参数。", home=self.home)
        combined = out.stdout + out.stderr
        self.assertEqual(out.returncode, 0, combined)
        self.assertIn("已更新", combined)
        self.assertIn("尚未收到", combined)
        self.assertTrue(p.exists(), "文件名/编号必须不变")
        text = p.read_text(encoding="utf-8")
        head = text.split("\n\n", 1)[0].splitlines()
        self.assertEqual([line.split("：", 1)[0] for line in head], ["来源", "事由", "需要"],
                         "系统标记不许增删，来源必须原样：\n" + "\n".join(head))
        self.assertTrue(head[0].startswith("来源：boss"))
        self.assertIn("事由：新事由", text)
        self.assertIn("需要：仅告知", text)
        self.assertIn("新正文：这一段是字面文本 --subject 不是参数。", text)
        self.assertNotIn("旧正文，来自旧版本。", text)
        self.assertNotIn("事由：旧事由", text)

    # -- A3: exact ref + at least one field --------------------------------
    def test_edit_requires_an_exact_stable_ref_and_at_least_one_field(self):
        lid = self.send("worker")
        p = self.letter_path("worker", lid)
        good = run_po("edit", "--box", "boss", f"worker/{lid}", "--need", "仅告知", home=self.home)
        self.assertEqual(good.returncode, 0, good.stderr)   # 正面对照：合法 edit 必须先成功
        snapshot = p.read_bytes()
        bad_refs = [f"worker/{lid}.md", f"worker/{lid[:8]}", f"inbox/{lid}",
                    f"worker/../worker/{lid}", lid, f"ghost/{lid}", "worker/*"]
        for ref in bad_refs:
            out = run_po("edit", "--box", "boss", ref, "--need", "仅告知", home=self.home)
            self.assertNotEqual(out.returncode, 0, f"必须拒绝的引用：{ref}")
            self.assertEqual(p.read_bytes(), snapshot, f"拒绝时原信必须一个字节不改：{ref}")
        no_field = run_po("edit", "--box", "boss", f"worker/{lid}", home=self.home)
        self.assertNotEqual(no_field.returncode, 0, "一个字段都不给必须拒绝")
        self.assertEqual(p.read_bytes(), snapshot)
        missing = run_po("edit", "--box", "boss", "worker/20990101-000000_boss_无此信",
                         "--need", "仅告知", home=self.home)
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(p.read_bytes(), snapshot)

    # -- A4: delivery already won -> update refused ------------------------
    def test_edit_refuses_after_the_plugin_ledger_shows_delivery(self):
        lid = self.send("worker")
        self.oc_ledger("worker", lid, "DELIVERED")
        self.edit_refused(f"worker/{lid}", "已送达（插件台账 DELIVERED）")

    def test_edit_refuses_while_a_delivery_claim_is_held(self):
        lid = self.send("worker")
        self.claim("worker", lid)
        self.edit_refused(f"worker/{lid}", "认领在途")

    def test_edit_refuses_after_the_claude_hook_has_seen_the_letter(self):
        lid = self.send("claude")
        seen = self.home / "claude" / ".seen"
        seen.write_text(str(self.letter_path("claude", lid)) + "\n")
        self.edit_refused(f"claude/{lid}", "Claude .seen")

    def test_edit_refuses_after_the_codex_accept_record(self):
        lid = self.send("coded")
        (self.home / ".woken.json").write_text(
            json.dumps({str(self.letter_path("coded", lid)): 1.0}))
        self.edit_refused(f"coded/{lid}", "codex 接受记录")

    def edit_refused(self, ref, why):
        box, lid = ref.split("/", 1)
        p = self.letter_path(box, lid)
        before = p.read_bytes()
        out = run_po("edit", "--box", "boss", ref, "--subject", "硬改", home=self.home)
        combined = out.stdout + out.stderr
        self.assertNotEqual(out.returncode, 0, f"{why} 之后必须拒绝：{combined}")
        self.assertIn("投递", combined, f"{why} 的拒绝提示要有「投递」：{combined}")
        self.assertIn("另发", combined, f"{why} 的拒绝提示要有「另发」：{combined}")
        self.assertEqual(p.read_bytes(), before, f"{why} 之后拒绝也不许动原信")
        self.assertIn(f"{lid}.md", self.inbox(box))

    def test_edit_fails_closed_on_an_unknown_channel(self):
        self.set_methods("spy", "carrier_pigeon")
        lid = self.send("spy", subject="未知通道")
        p = self.letter_path("spy", lid)
        before = p.read_bytes()
        out = run_po("edit", "--box", "boss", f"spy/{lid}", "--need", "仅告知", home=self.home)
        combined = out.stdout + out.stderr
        self.assertNotEqual(out.returncode, 0, "未知通道无法证明未送达，必须 fail closed")
        self.assertIn("无法核实", combined, "要明确说无法核实该通道：\n" + combined)
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(self.inbox("spy"), [f"{lid}.md"])

    # -- A5: sender mismatch + derived notices -----------------------------
    def test_edit_refuses_foreign_senders_and_derived_notices(self):
        lid = self.send("worker", subject="正面对照")
        good = run_po("edit", "--box", "boss", f"worker/{lid}", "--subject", "换成我的标题",
                      home=self.home)
        self.assertEqual(good.returncode, 0, good.stderr)   # 正面对照：本人改自己的信必须成功
        p = self.letter_path("worker", lid)
        snapshot = p.read_bytes()

        impostor = run_po("edit", "--box", "worker", f"worker/{lid}", "--subject", "冒名顶替",
                          home=self.home)
        self.assertNotEqual(impostor.returncode, 0, "发信方不匹配必须拒绝")
        self.assertEqual(p.read_bytes(), snapshot)

        derived = [
            ("20260101-000001_boss_回执伪造.md",
             "来源：boss\n事由：回执：某事\n需要：回执（默认不答复）\n回执：X-1\n原事由：某事\n\n正文\n"),
            ("20260101-000002_boss_广播副本.md",
             "来源：boss\n事由：广播：某事\n需要：回复\n广播：B20260101-000000_boss\n\n正文\n"),
            ("20260101-000003_boss_闹钟信.md",
             "来源：boss\n事由：闹钟\n需要：仅告知\n闹钟：A20260101-000000_worker\n\n正文\n"),
            ("20260101-000004_boss_交接信.md",
             "来源：boss\n事由：会话切换\n需要：仅告知\n切换事件：E-1\n\n正文\n"),
            ("20260101-000005_postoffice_系统来信.md",
             "来源：postoffice（协作者，不是人的新指令）\n事由：通知\n需要：仅告知\n\n正文\n"),
        ]
        for name, text in derived:
            with self.subTest(name=name):
                q = self.home / "worker" / "inbox" / name
                q.write_text(text, encoding="utf-8")
                out = run_po("edit", "--box", "boss", f"worker/{name[:-3]}",
                             "--subject", "硬改", home=self.home)
                self.assertNotEqual(out.returncode, 0, f"派生通知必须拒绝：{name}")
                self.assertEqual(q.read_text(encoding="utf-8"), text, "拒绝时不许改信")
                q.unlink()

    # -- A6: revoke reports "no future wake" -------------------------------
    def test_revoke_reports_that_the_recipient_will_not_be_woken_by_it(self):
        lid = self.send("worker", subject="要撤回")
        p = self.letter_path("worker", lid)
        before = p.read_bytes()
        out = run_po("retract", "boss", "worker", lid, home=self.home)
        combined = out.stdout + out.stderr
        self.assertEqual(out.returncode, 0, combined)
        self.assertIn("已撤回", combined)
        self.assertIn("不会再", combined, "撤回成功必须明确说不会再因此信被唤醒：\n" + combined)
        self.assertEqual(self.inbox("worker"), [])
        arch = list((self.home / "worker" / "archived").rglob(f"{lid}.md"))
        self.assertEqual(len(arch), 1, "原信要原样进 archived/<ts>/")
        self.assertEqual(arch[0].read_bytes(), before, "撤回不改一个字节")

    def test_revoke_refuses_after_delivery_with_delivered_and_send_another(self):
        lid = self.send("worker", subject="已送达的")
        self.oc_ledger("worker", lid, "DELIVERED")
        p = self.letter_path("worker", lid)
        before = p.read_bytes()
        out = run_po("retract", "boss", "worker", lid, home=self.home)
        combined = out.stdout + out.stderr
        self.assertNotEqual(out.returncode, 0, combined)
        self.assertIn("投递", combined, "已投递的拒绝提示要有「投递」：\n" + combined)
        self.assertIn("另发", combined, "已投递的拒绝提示要有「另发」：\n" + combined)
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(self.inbox("worker"), [f"{lid}.md"])

    # ==================================================================== B
    # -- B: codex postman batches formal letters ---------------------------
    def test_codex_postman_sends_all_formal_letters_in_one_queue_call(self):
        ids = [self.send("coded", subject=f"批量信{i}") for i in range(3)]
        postman(self.home)
        calls = self.codex_calls()
        self.assertEqual(len(calls), 1,
                         f"三封普通正式信应当同一轮只调一次 codex queue，实际 {len(calls)} 次")
        for lid in ids:
            self.assertIn(f"coded/{lid}", calls[0], "这一批要带上每封信的稳定引用")
            self.assertTrue(self.letter_path("coded", lid).exists(), "每封信仍各自留在 inbox")
            self.assertIn(f"{lid}.md", self.claims("coded"), "每封各自认领")
        self.assertEqual(len(self.wake_times("coded")), 1, "一批只记一次限流")
        woken = self.woken()
        for lid in ids:
            self.assertIn(str(self.letter_path("coded", lid)), woken, "每封各自进投递台账")

    def test_codex_batch_wake_text_shape_with_trailing_receipt_block(self):
        ids = [self.send("coded", subject=f"批形{i}") for i in range(2)]
        rcpt = self.home / "coded" / "inbox" / "20260101-000000_ghost_回执.md"
        rcpt.write_text("来源：ghost\n事由：回执：旧事\n需要：回执（默认不答复）\n"
                        "回执：orig-1\n原事由：旧事\n\n" + SENTINEL + "\n", encoding="utf-8")
        old = time.time() - 3600
        os.utime(rcpt, (old, old))
        postman(self.home)
        calls = self.codex_calls()
        self.assertEqual(len(calls), 1, f"两封信加回执应当只调一次，实际 {len(calls)} 次")
        text = calls[0]
        self.assertIn("【联络总站｜2 封新信】", text, "≥2 封用 batch 形态（第一行）：\n" + text)
        self.assertNotIn("【联络总站新信｜", text, "≥2 封不得退回单封形态：\n" + text)
        for i, lid in enumerate(ids, start=1):
            line = next((l for l in text.splitlines() if l.startswith(f"{i}. coded/{lid}")), None)
            self.assertIsNotNone(line, f"第 {i} 封要有编号行 `{i}. coded/{lid} …`：\n" + text)
            self.assertIn("来源：boss", line)
            self.assertIn(f"事由：批形{i - 1}", line)
            self.assertIn(str(self.letter_path("coded", lid)), line, "编号行要带该信绝对路径")
        self.assertEqual(text.count("按各信"), 1, "公共 tail 只出现一次")
        self.assertEqual(text.count("postoffice skill"), 1)
        self.assertIn("另有 1 条回执", text, "回执仍是尾部合并块")
        self.assertNotIn(SENTINEL, text, "回执正文永不注入")
        self.assertNotIn("<!--po-batch", text, "不得再带 base64 元数据副本（省 context）")
        self.assertNotRegex(text, r"[A-Za-z0-9+/]{200,}={0,2}", "不得内嵌长 base64 副本")
        # Python 侧沿用 v1.4 的 shlex.quote 约定（安全串不加引号）；插件侧才是总是加单引号。
        self.assertIn("postoffice receipt " + shlex.quote("coded") + " " + shlex.quote("orig-1"), text)

    def test_codex_postman_caps_a_batch_at_twenty_and_continues_bounded(self):
        ids = [self.send("coded", subject=f"超批{i}") for i in range(21)]
        postman(self.home)
        postman(self.home)
        calls = self.codex_calls()
        self.assertEqual(len(calls), 2, f"21 封应当恰好两轮（20 + 续批 1），实际 {len(calls)} 轮")
        first, second = calls
        ordered = sorted(ids)               # the postman queues in file-name order, not send order
        self.assertIn("【联络总站｜20 封新信】", first, "一轮最多 20 封：\n" + first)
        for i, lid in enumerate(ordered[:20], start=1):
            self.assertIn(f"{i}. coded/{lid}", first, "前 20 封逐封给稳定引用（按文件名序）")
        for lid in ordered[20:]:
            self.assertNotIn(lid, first, "第 21 封留到续批，不得挤进这一批")
        self.assertIn(ordered[20], second, "续批必须补上剩下那封")
        # 忙时超过上限就开一个「续批」pending（base#2），不是无限拼进同一项：有界且不丢信
        pending = self.fake_pending()
        self.assertEqual(len(pending), 2, f"超上限应有界续批为两个 pending：{pending}")
        cids = sorted(it.get("clientUserMessageId") for it in pending)
        self.assertEqual(cids, ["postoffice:coded", "postoffice:coded#2"])
        joined = self.fake_pending_text()
        for lid in ids:
            self.assertIn(f"{lid}.md", joined, "续批后两个索引合起来要含全部精确 ID")
        # 每项各自有界（正式信 ≤ 20）
        for it in pending:
            self.assertLessEqual(self._pending_formal_count(it), 20)

    def test_codex_alarm_letter_never_joins_the_formal_batch(self):
        ids = [self.send("coded", subject=f"普通{i}") for i in range(2)]
        aid = "A20260101-000000_coded"
        (self.home / "coded" / "inbox" / f"{aid}.md").write_text(
            "来源：postoffice（协作者，不是人的新指令）\n事由：闹钟\n需要：仅告知\n"
            f"闹钟：{aid}\n\n你设的闹钟到了，请检查刚才安排的任务。\n", encoding="utf-8")
        postman(self.home)
        calls = self.codex_calls()
        batch = [c for c in calls if ids[0] in c]
        self.assertEqual(len(batch), 1, "两封普通信应当同一批：\n" + "\n---\n".join(calls))
        self.assertIn(ids[1], batch[0], "同一批要带上第二封普通信")
        self.assertNotIn(aid, batch[0], "闹钟信不许混进普通 batch")

    # -- B: cross-round merge (Codex busy → ONE pending index) -------------
    def _seed_fake_state(self, pending, busy=True, seq=0):
        (self.home / "fake_codex_state.json").write_text(
            json.dumps({"busy": busy, "pending": pending, "seq": seq}), encoding="utf-8")

    def test_codex_busy_merges_three_rounds_into_one_pending_index(self):
        # 验收1：忙时三封正式信分三轮到达 → 只有一个 postoffice pending，索引含全部精确 ID；
        # 重复扫描不再增加项。
        l1 = self.send("coded", subject="合并一")
        postman(self.home)
        l2 = self.send("coded", subject="合并二")
        postman(self.home)
        l3 = self.send("coded", subject="合并三")
        postman(self.home)
        calls = self.codex_calls()
        self.assertEqual(len(calls), 3, f"三轮各一次通道操作（add + update + update）：{len(calls)}")
        pend = self.fake_pending()
        self.assertEqual(len(pend), 1, f"忙时只允许一个 postoffice pending：{pend}")
        self.assertEqual(pend[0]["clientUserMessageId"], "postoffice:coded")
        text = self.fake_pending_text()
        for lid in (l1, l2, l3):
            self.assertIn(f"{lid}.md", text, "合并索引要含全部精确 ID")
        # 重复扫描不新增
        postman(self.home)
        self.assertEqual(len(self.codex_calls()), 3, "没有新信时不许再产生通道操作")
        self.assertEqual(len(self.fake_pending()), 1)

    def test_codex_more_than_twenty_formals_all_queue_and_mark(self):
        # wake-coalesce 后回执不再单独成批（只回执=不出发，见 wake_coalesce_test）。
        # 20 项上限的批界与续批意图改由正式信承载：21 封正式信超 20 上限，第 21 封
        # 绝不许被悄悄丢掉——落进第二项，且每个 ID 都入索引、都记账为已接受。
        self._seed_fake_state([], busy=True)
        paths, lids = [], []
        for i in range(21):
            lid = f"20260101-0000{i:02d}_ghost_旧{i}"
            lids.append(lid)
            f = self.home / "coded" / "inbox" / f"{lid}.md"
            f.write_text(f"来源：ghost\n事由：旧{i}\n需要：回复\n\n正文{i}\n", encoding="utf-8")
            old = time.time() - 3600
            os.utime(f, (old, old))
            paths.append(str(f))
        postman(self.home)
        pend = self.fake_pending()
        self.assertEqual(len(pend), 2, f"21 封正式信应分两项（20+1），实际 {len(pend)}")
        text = self.fake_pending_text()
        for lid in lids:
            self.assertIn(lid, text, f"每封信的稳定引用都要在索引里：{lid}")
        delivered = set(json.loads((self.home / ".delivered.json").read_text(encoding="utf-8")))
        for p in paths:
            self.assertIn(p, delivered, f"每封信都要记账为已接受：{p}")

    def test_codex_full_formal_batch_then_one_more_across_rounds(self):
        # 满项（20 封正式信）后下一轮再来第 21 封：第 21 封必须落进第二项续批，
        # 绝不能被「已在其内」的合并路径丢掉（槽位已满）。回执版场景由新合同废除。
        self._seed_fake_state([], busy=True)
        lids = []

        def mk(i):
            lid = f"20260103-0000{i:02d}_ghost_跨轮{i}"
            f = self.home / "coded" / "inbox" / f"{lid}.md"
            f.write_text(f"来源：ghost\n事由：跨{i}\n需要：回复\n\n正文{i}\n", encoding="utf-8")
            old = time.time() - 3600
            os.utime(f, (old, old))
            lids.append(lid)
            return f

        for i in range(20):
            mk(i)
        postman(self.home)
        self.assertEqual(len(self.fake_pending()), 1, "20 封刚好一项")
        f21 = mk(20)
        postman(self.home)
        pend = self.fake_pending()
        self.assertEqual(len(pend), 2, f"第21封必须落进续批，不能丢：{pend}")
        text = self.fake_pending_text()
        for lid in lids:
            self.assertIn(lid, text, f"每封信的稳定引用都要在索引里：{lid}")
        delivered = set(json.loads((self.home / ".delivered.json").read_text(encoding="utf-8")))
        self.assertIn(str(f21), delivered, "第21封必须记账为已接受")

    def test_codex_receipt_query_survives_render_merge_render(self):
        # Render→parse→merge→render round-trip per the real _receipt_item shape: the old query
        # command must survive a later merge, and the bare stable id (unquoted) must round-trip so
        # dedupe keeps working even when the id carries blanks/punctuation.
        po = load_po()
        d = self.home / "coded" / "inbox"
        rid1 = "r 1：2(3)+4"
        p1 = d / "20260104-000000_ghost_旧回执.md"
        p1.write_text(f"来源：ghost\n事由：回执：旧\n需要：回执（默认不答复）\n"
                      f"回执：{rid1}\n原事由：旧\n\n正文\n", encoding="utf-8")
        e1 = po._codex_entry("coded", p1)
        self.assertEqual(e1["k"], "r")
        self.assertEqual(e1["ref"], rid1, "稳定引用是未加引号的裸 id")
        parsed = po._codex_parse_entries(po._codex_batch_text([e1]))
        self.assertEqual(len(parsed), 1, "单回执形态要能解析回来")
        self.assertEqual(parsed[0]["ref"], rid1, "解析要还原裸 id（去掉 shell 引号）")
        self.assertEqual(parsed[0]["query"], e1["query"], "旧查询命令必须原样保留")
        self.assertEqual(parsed[0]["subj"], e1["subj"])

        # dedupe is stable across the round trip
        merged0, overflow0 = po._codex_merge_entries(parsed, [e1], 20, 20)
        self.assertEqual(merged0, parsed)
        self.assertEqual(overflow0, [])

        # a new receipt arrives → merge → re-render keeps BOTH queries usable, bare ids stable
        rid2 = "r2"
        p2 = d / "20260104-000001_ghost_新回执.md"
        p2.write_text(f"来源：ghost\n事由：回执：新\n需要：回执（默认不答复）\n"
                      f"回执：{rid2}\n原事由：新\n\n正文\n", encoding="utf-8")
        e2 = po._codex_entry("coded", p2)
        merged, overflow = po._codex_merge_entries(parsed, [e2], 20, 20)
        self.assertEqual(overflow, [])
        t2 = po._codex_batch_text(merged)
        self.assertIn(e1["query"], t2, "合并后旧查询命令仍可用")
        self.assertIn(e2["query"], t2, "新查询命令可用")
        refs = sorted(x["ref"] for x in po._codex_parse_entries(t2))
        self.assertEqual(refs, sorted([rid1, rid2]), "裸稳定 ID 往返一致")

    def test_codex_old_single_format_letters_survive_merge_with_new_mail(self):
        # Backward compat: pending items written before the `引用：` line existed must still parse, so
        # merging a new letter never DROPS an old formal/receipt ref. Two shapes: old single formal,
        # and old single formal + receipt.
        old1 = "20260101-000000_ghost_旧单封"
        old2 = "20260101-000001_ghost_旧单封二"
        rid = "old-rid-1"
        old_single = (f"【联络总站新信｜coded】\n== /tmp/x/coded/inbox/{old1}.md\n"
                      "来源：ghost\n事由：旧单封\n需要：仅告知\n"
                      "按信件“需要”字段处理；回信/回执/归档规则见 postoffice skill。")
        old_single_receipt = (f"【联络总站新信｜coded】\n== /tmp/x/coded/inbox/{old2}.md\n"
                              "来源：ghost\n事由：旧单封二\n需要：仅告知\n"
                              "按信件“需要”字段处理；回信/回执/归档规则见 postoffice skill。\n"
                              "【联络总站回执｜coded】来自 ghost 的回执，原事由：旧回执\n"
                              f"查询：postoffice receipt coded {rid}\n默认不答复")
        # shape A: old single formal
        self._seed_fake_state([{"id": "q1", "input": [{"type": "text", "text": old_single}],
                                "clientUserMessageId": "postoffice:coded"}], busy=True)
        newA = self.send("coded", subject="新信A")
        postman(self.home)
        text = self.fake_pending_text()
        self.assertIn(f"{old1}.md", text, "旧单封的正式编号必须保全")
        self.assertIn(f"{newA}.md", text, "新正式编号要追加")
        self.assertEqual(len(self.fake_pending()), 1, "仍在同一 pending")
        # shape B: old single formal + receipt
        self._seed_fake_state([{"id": "q2", "input": [{"type": "text", "text": old_single_receipt}],
                                "clientUserMessageId": "postoffice:coded"}], busy=True)
        newB = self.send("coded", subject="新信B")
        postman(self.home)
        text = self.fake_pending_text()
        self.assertIn(f"{old2}.md", text, "旧单封+回执里的正式编号必须保全")
        self.assertIn(rid, text, "旧回执编号必须保全")
        self.assertIn(f"{newB}.md", text, "新正式编号要追加")

    def test_codex_never_touches_a_user_queued_message(self):
        # 只更新确证归属邮局、且尚未启动的项：用户自己排队的消息原样不动。
        self._seed_fake_state([{"id": "u1", "input": [{"type": "text", "text": "USER-MSG-KEEP",
                                                       "text_elements": []}],
                                "clientUserMessageId": None}], seq=1)
        l1 = self.send("coded", subject="自己的")
        postman(self.home)
        pend = self.fake_pending()
        users = [p for p in pend if p["clientUserMessageId"] is None]
        self.assertEqual(len(users), 1, "用户项必须还在")
        self.assertEqual(users[0]["input"][0]["text"], "USER-MSG-KEEP", "用户项内容一字不改")
        ours = [p for p in pend if p["clientUserMessageId"] == "postoffice:coded"]
        self.assertEqual(len(ours), 1, "邮局项另立一条，不并进用户项")
        self.assertIn(f"{l1}.md", "".join(x.get("text", "") for x in ours[0]["input"]))

        l2 = self.send("coded", subject="自己的二")
        postman(self.home)
        pend2 = self.fake_pending()
        self.assertEqual([p for p in pend2 if p["clientUserMessageId"] is None][0]["input"][0]["text"],
                         "USER-MSG-KEEP", "第二轮合并仍不许碰用户项")
        self.assertEqual(len([p for p in pend2 if p["clientUserMessageId"] == "postoffice:coded"]), 1,
                         "合并进邮局项，不新建")
        self.assertIn(f"{l2}.md", self.fake_pending_text())

    def test_codex_new_mail_after_pending_consumed_is_a_fresh_submission(self):
        # 验收3：pending 被消费（启动）后再到新信 → 不修改正在运行的内容，另起一条。
        l1 = self.send("coded", subject="先来")
        postman(self.home)
        self.assertEqual(len(self.fake_pending()), 1)
        # 模拟该 pending 已被会话消费（启动）：从队列消失
        self._seed_fake_state([], seq=1)
        l2 = self.send("coded", subject="后来")
        postman(self.home)
        pend = self.fake_pending()
        self.assertEqual(len(pend), 1, "消费后新信另起一条")
        text = self.fake_pending_text()
        self.assertIn(f"{l2}.md", text)
        self.assertNotIn(f"{l1}.md", text, "不得把已消费的旧内容重新并入")

    def test_codex_partial_claim_failure_wakes_the_rest_once_and_never_duplicates(self):
        ids = [self.send("coded", subject=f"部分{i}") for i in range(3)]
        self.claim("coded", ids[1], pid="99999")
        postman(self.home)
        calls = self.codex_calls()
        self.assertEqual(len(calls), 1, f"抢不到的那封不投，其余一次唤醒：{len(calls)} 次")
        self.assertIn(ids[0], calls[0])
        self.assertIn(ids[2], calls[0])
        self.assertNotIn(ids[1], calls[0], "被抢走的信不得出现在这一批里")
        self.assertEqual(len(self.wake_times("coded")), 1, "一批只记一次限流")
        woken = self.woken()
        self.assertIn(str(self.letter_path("coded", ids[0])), woken)
        self.assertIn(str(self.letter_path("coded", ids[2])), woken)
        self.assertNotIn(str(self.letter_path("coded", ids[1])), woken, "没投的不得记账")
        claim_file = self.home / "coded" / ".claims" / f"{ids[1]}.md"
        self.assertTrue(claim_file.exists(), "别人的认领不许被动")
        self.assertEqual(claim_file.read_text(), "99999\n")

        claim_file.unlink()
        postman(self.home)
        calls2 = self.codex_calls()
        self.assertEqual(len(calls2), 2, "下一轮应当只补抢不到的那封")
        self.assertIn(ids[1], calls2[1], "补投的那封要进合并索引")
        # 忙时是就地追加同一个 pending，不新建提交：合并后仍只有一个 postoffice pending
        self.assertEqual(len(self.fake_pending()), 1, f"合并后仍只有一个 postoffice pending：{self.fake_pending()}")
        self.assertIn(ids[0], self.fake_pending_text())
        self.assertIn(ids[1], self.fake_pending_text())
        self.assertIn(ids[2], self.fake_pending_text())
        woken = self.woken()
        for lid in ids:
            self.assertIn(str(self.letter_path("coded", lid)), woken, "三封都各记一次账")

        # 全部抢不到 → 0 wake、0 空 prompt、0 台账
        extra = [self.send("coded", subject=f"全占{i}") for i in range(2)]
        for lid in extra:
            self.claim("coded", lid)
        postman(self.home)
        calls3 = self.codex_calls()
        self.assertEqual(len(calls3), 2, "全部抢不到时不得再产生一次投递")
        self.assertEqual(len(self.wake_times("coded")), 2, "没有唤醒就不许再记限流")
        for lid in extra:
            self.assertNotIn(str(self.letter_path("coded", lid)), self.woken(), "抢不到就不许记账")

    # -- B: robust boundaries (dedupe / pagination / lost response) --------
    def test_codex_merge_dedupes_by_id_and_keeps_rules_once_formal_first(self):
        l1 = self.send("coded", subject="去重一")
        postman(self.home)
        # an aged receipt notification rides along with the next formal round
        rc = self.home / "coded" / "inbox" / "20260101-000000_ghost_回执.md"
        rc.write_text("来源：ghost\n事由：回执：旧事\n需要：回执（默认不答复）\n"
                      "回执：orig-9\n原事由：旧事\n\n正文\n", encoding="utf-8")
        old = time.time() - 3600
        os.utime(rc, (old, old))
        l2 = self.send("coded", subject="去重二")
        postman(self.home)
        self.assertEqual(len(self.fake_pending()), 1, "仍合并为一个 pending")
        text = self.fake_pending_text()
        self.assertEqual(text.count(f"{l1}.md"), 1, "稳定引用必须去重，不得把旧 reminder 全文再拼一遍")
        self.assertEqual(text.count("按各信"), 1, "公共规则只出现一次")
        self.assertIn(f"{l2}.md", text)
        self.assertIn("postoffice receipt coded orig-9", text, "回执仍按 ID 索引")
        self.assertLess(text.index(f"{l2}.md"), text.index("另有 1 条回执"),
                        "正式信在前、回执块在后")

    def _paging_codex(self):
        """Like fake_codex but returns ONE item per page (cursor = offset), to exercise pagination."""
        script = self.home / "fake_codex_paged.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "D = os.path.dirname(os.path.abspath(__file__))\n"
            "STATE = os.path.join(D, 'fake_codex_state.json')\n"
            "CAP = os.path.join(D, 'codex.calls')\n"
            "def load():\n"
            "    try:\n"
            "        return json.load(open(STATE))\n"
            "    except Exception:\n"
            "        return {'busy': True, 'pending': [], 'seq': 0}\n"
            "def save(s):\n"
            "    open(STATE, 'w').write(json.dumps(s))\n"
            "def emit(o):\n"
            "    sys.stdout.write(json.dumps(o) + '\\n'); sys.stdout.flush()\n"
            "def text_of(p):\n"
            "    return '\\n'.join(x.get('text','') for x in (p.get('input') or []) if isinstance(x, dict) and x.get('type')=='text')\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line: continue\n"
            "    try: msg = json.loads(line)\n"
            "    except ValueError: continue\n"
            "    m, rid, p = msg.get('method'), msg.get('id'), (msg.get('params') or {})\n"
            "    s = load()\n"
            "    if m == 'initialize':\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'userAgent':'fake','codexHome':D,'platformFamily':'unix','platformOs':'macos'}})\n"
            "    elif m == 'thread/queue/list':\n"
            "        off = int(p.get('cursor') or 0)\n"
            "        page = s['pending'][off:off+1]\n"
            "        nxt = str(off + 1) if off + 1 < len(s['pending']) else None\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'data':page,'nextCursor':nxt}})\n"
            "    elif m == 'thread/queue/add':\n"
            "        open(CAP,'a').write('===8<===\\n' + text_of(p) + '\\n===>8===\\n')\n"
            "        if s['busy']:\n"
            "            s['seq'] += 1\n"
            "            s['pending'].append({'id':'q%d'%s['seq'],'input':p.get('input'),'clientUserMessageId':p.get('clientUserMessageId')})\n"
            "        save(s)\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'queuedSubmission':None}})\n"
            "    elif m == 'thread/queue/update':\n"
            "        open(CAP,'a').write('===8<===\\n' + text_of(p) + '\\n===>8===\\n')\n"
            "        for it in s['pending']:\n"
            "            if it['id'] == p.get('queuedSubmissionId'):\n"
            "                it['input'] = p.get('input')\n"
            "        save(s)\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'queuedSubmission':None}})\n"
            "    else:\n"
            "        emit({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'method not found'}})\n",
            encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_codex_paginates_to_find_our_pending_beyond_the_first_page(self):
        # 用户自己排了很多队时邮局 pending 落在后页：必须翻页精确找到，不能只看第一页就当「不存在」再 add。
        self._seed_fake_state([{"id": "u1",
                                "input": [{"type": "text", "text": "USER-A", "text_elements": []}],
                                "clientUserMessageId": None}], seq=1)
        self.set_methods("coded", "codex_queue", codex_cli=str(self._paging_codex()), thread_id="th-x")
        l0 = self.send("coded", subject="翻页零")           # page2 empty → add our item
        postman(self.home)
        self.assertEqual(len(self.fake_pending()), 2, "用户项 + 邮局项")
        l1 = self.send("coded", subject="翻页一")           # our item now on page 2 → must be found
        postman(self.home)
        pend = self.fake_pending()
        self.assertEqual(len(pend), 2, f"翻页找到后是就地更新，不得新建第三项：{pend}")
        ours = [p for p in pend if p["clientUserMessageId"] == "postoffice:coded"]
        self.assertEqual(len(ours), 1)
        ours_text = "".join(x.get("text", "") for x in ours[0]["input"])
        self.assertIn(f"{l0}.md", ours_text)
        self.assertIn(f"{l1}.md", ours_text)
        users = [p for p in pend if p["clientUserMessageId"] is None]
        self.assertEqual(users[0]["input"][0]["text"], "USER-A", "翻页不得碰用户项")

    def _lossy_codex(self):
        """fake_codex that RECORDS the add then exits without replying (accepted-but-response-lost)."""
        script = self.home / "fake_codex_lossy.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "D = os.path.dirname(os.path.abspath(__file__))\n"
            "STATE = os.path.join(D, 'fake_codex_state.json')\n"
            "CAP = os.path.join(D, 'codex.calls')\n"
            "def load():\n"
            "    try:\n"
            "        return json.load(open(STATE))\n"
            "    except Exception:\n"
            "        return {'busy': True, 'pending': [], 'seq': 0}\n"
            "def save(s):\n"
            "    open(STATE, 'w').write(json.dumps(s))\n"
            "def emit(o):\n"
            "    sys.stdout.write(json.dumps(o) + '\\n'); sys.stdout.flush()\n"
            "def text_of(p):\n"
            "    return '\\n'.join(x.get('text','') for x in (p.get('input') or []) if isinstance(x, dict) and x.get('type')=='text')\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line: continue\n"
            "    try: msg = json.loads(line)\n"
            "    except ValueError: continue\n"
            "    m, rid, p = msg.get('method'), msg.get('id'), (msg.get('params') or {})\n"
            "    s = load()\n"
            "    if m == 'initialize':\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'userAgent':'fake','codexHome':D,'platformFamily':'unix','platformOs':'macos'}})\n"
            "    elif m == 'thread/queue/list':\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'data':s['pending'],'nextCursor':None}})\n"
            "    elif m == 'thread/queue/add':\n"
            "        open(CAP,'a').write('===8<===\\n' + text_of(p) + '\\n===>8===\\n')\n"
            "        if s['busy']:\n"
            "            s['seq'] += 1\n"
            "            s['pending'].append({'id':'q%d'%s['seq'],'input':p.get('input'),'clientUserMessageId':p.get('clientUserMessageId')})\n"
            "        save(s)\n"
            "        sys.exit(0)\n"          # accepted, but the reply is lost
            "    else:\n"
            "        emit({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'method not found'}})\n",
            encoding="utf-8")
        script.chmod(0o755)
        return script

    def _vanishing_codex(self):
        """fake_codex that records the add then exits without replying AND without persisting the
        item — the write is lost with no on-queue evidence it ever landed."""
        script = self.home / "fake_codex_vanishing.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "D = os.path.dirname(os.path.abspath(__file__))\n"
            "CAP = os.path.join(D, 'codex.calls')\n"
            "def emit(o):\n"
            "    sys.stdout.write(json.dumps(o) + '\\n'); sys.stdout.flush()\n"
            "def text_of(p):\n"
            "    return '\\n'.join(x.get('text','') for x in (p.get('input') or []) if isinstance(x, dict) and x.get('type')=='text')\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line: continue\n"
            "    try: msg = json.loads(line)\n"
            "    except ValueError: continue\n"
            "    m, rid, p = msg.get('method'), msg.get('id'), (msg.get('params') or {})\n"
            "    if m == 'initialize':\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'userAgent':'fake','codexHome':D,'platformFamily':'unix','platformOs':'macos'}})\n"
            "    elif m == 'thread/queue/list':\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'data':[],'nextCursor':None}})\n"
            "    elif m == 'thread/queue/add':\n"
            "        open(CAP,'a').write('===8<===\\n' + text_of(p) + '\\n===>8===\\n')\n"
            "        sys.exit(0)\n"          # accepted? unknown — no reply and nothing persisted
            "    else:\n"
            "        emit({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'method not found'}})\n",
            encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_codex_lost_response_that_landed_recovers_without_duplicate_add(self):
        # The add is accepted and persisted, but the reply is lost. The next round must find the
        # real item (not blindly re-add) and reconcile — exactly one add, one item, delivered.
        self.set_methods("coded", "codex_queue", codex_cli=str(self._lossy_codex()), thread_id="th-x")
        l1 = self.send("coded", subject="丢响应")
        postman(self.home)
        self.assertEqual(len(self.codex_calls()), 1, "丢响应后不得再 add 一次（不许盲重试）")
        self.assertEqual(len(self.fake_pending()), 1, "写入已落盘")
        self.assertIn(f"{l1}.md", self.fake_pending_text())
        self.assertFalse((self.home / "coded" / ".codex_uncertain.json").exists(),
                         "找到真实 pending 后要清掉未知标记（确认恢复）")
        self.assertIn(str(self.letter_path("coded", l1)), self.woken(), "恢复后记账为已投递")

    def test_codex_lost_response_with_no_visible_item_stays_unknown_without_resubmit(self):
        # The add's reply is lost AND no item is visible: the outcome is unknown, so the next round
        # must NOT resubmit (could double-deliver); keep a diagnosable marker and leave the letter.
        self.set_methods("coded", "codex_queue", codex_cli=str(self._vanishing_codex()), thread_id="th-x")
        l1 = self.send("coded", subject="丢响应无痕")
        postman(self.home)
        self.assertEqual(len(self.codex_calls()), 1, "结果未知时不得盲重试再 add")
        self.assertTrue((self.home / "coded" / ".codex_uncertain.json").exists(),
                        "结果未知要留下可诊断标记")
        self.assertEqual(self.woken(), {}, "unknown 不得记账为已投递")
        self.assertIn(f"{l1}.md", self.inbox("coded"), "原信保留在 inbox")

    # -- B: Claude hook regression (same round, multi-letter, one wake) ----
    def test_hook_wakes_once_for_several_letters_and_records_presented_union(self):
        ids = [self.send("claude", subject=f"钩子信{i}") for i in range(3)]
        self.write_presented("claude", ["stale-old"])
        rc, err = hook(self.home, self.title_transcript(), wait=12)
        self.assertEqual(rc, 2, f"同轮多封应当唤醒一次：{err}")
        for lid in ids:
            path = str(self.letter_path("claude", lid))
            self.assertIn(path, err, "唤醒负载要带上每一封的路径")
            self.assertIn(path, (self.home / "claude" / ".seen").read_text().split())
            self.assertIn(f"{lid}.md", self.claims("claude"))
        self.assertEqual(len(self.wake_times("claude")), 1, "一次唤醒只记一次限流")
        presented = json.loads((self.home / "claude" / ".presented.json").read_text(encoding="utf-8"))
        self.assertEqual(set(presented), {"stale-old", *ids},
                         "presented 必须是 union，不是覆盖：\n" + repr(presented))
        for x in presented:
            self.assertIsInstance(x, str)
            self.assertNotIn("/", x)
            self.assertFalse(x.endswith(".md"), "presented 只存裸 id")

        # 部分 claim 失败：被抢走的信不出现、不记 presented，其余照常一次唤醒
        pair = [self.send("claude", subject=f"部分{i}") for i in range(2)]
        self.claim("claude", pair[0])
        rc2, err2 = hook(self.home, self.title_transcript(), wait=8)
        self.assertEqual(rc2, 2, err2)
        self.assertIn(str(self.letter_path("claude", pair[1])), err2)
        self.assertNotIn(str(self.letter_path("claude", pair[0])), err2, "抢不到的信不出现")
        presented = json.loads((self.home / "claude" / ".presented.json").read_text(encoding="utf-8"))
        self.assertIn(pair[1], presented)
        self.assertNotIn(pair[0], presented, "没展示过的不得进 presented")

        # 全部抢不到 → 0 wake、不 rate_mark、presented 不涨
        more = [self.send("claude", subject=f"占住{i}") for i in range(2)]
        for lid in more:
            self.claim("claude", lid)
        before_presented = json.loads((self.home / "claude" / ".presented.json").read_text(encoding="utf-8"))
        before_rate = len(self.wake_times("claude"))
        rc3, err3 = hook(self.home, self.title_transcript(), wait=4)
        self.assertEqual(rc3, 124, f"全部抢不到时不得唤醒：{err3}")
        self.assertEqual(len(self.wake_times("claude")), before_rate, "没唤醒就不许记限流")
        self.assertEqual(
            json.loads((self.home / "claude" / ".presented.json").read_text(encoding="utf-8")),
            before_presented)

    def test_hook_single_letter_wake_text_stays_the_single_letter_shape(self):
        lid = self.send("claude", subject="单封形态")
        p = self.letter_path("claude", lid)
        rc, err = hook(self.home, self.title_transcript(), wait=8)
        self.assertEqual(rc, 2, err)
        self.assertIn("【联络总站新信｜claude】", err, "单封必须保持现有形态：\n" + err)
        self.assertIn(f"引用：claude/{lid}", err, "单封也要给可直接复制的稳定引用：\n" + err)
        self.assertIn(f"== {p}", err)
        # 单封摘要按解析出的 source/subject/need 生成中性元数据，不回抄信头原始行（§27-32 退修）
        self.assertIn("来源：boss", err)
        self.assertIn("事由：单封形态", err)
        self.assertIn("需要：回复", err)
        self.assertNotIn("邮局只确认来源信箱", err, "唤醒不得回抄信头括号")
        self.assertIn("按信件", err)
        self.assertEqual(self.presented("claude"), {lid},
                         "送达接受后要把这封记进 presented")

    # ==================================================================== C
    def test_archive_current_moves_only_the_presented_letters(self):
        ids = [self.send("worker", subject=f"已展示{i}") for i in range(3)]
        self.write_presented("worker", ids)
        fresh = self.send("worker", subject="之后新到")
        out = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("已归档 3 封", out.stdout, "成功归档要报告真实数量：\n" + out.stdout)
        self.assertEqual(self.inbox("worker"), [f"{fresh}.md"],
                         "presented 之外刚到的信必须原地不动")
        self.assertEqual(self.done("worker"), sorted(f"{i}.md" for i in ids))
        self.assertTrue(self.letter_path("worker", fresh).read_text(encoding="utf-8")
                        .splitlines()[0].startswith("来源：boss"))
        self.assertEqual(self.presented("worker") & set(ids), set(), "成功归档的要移出 presented")
        self.assertNotIn(fresh, self.presented("worker"))
        again = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.inbox("worker"), [f"{fresh}.md"], "重复调用幂等")
        self.assertEqual(self.done("worker"), sorted(f"{i}.md" for i in ids))

    def test_archive_current_keep_leaves_that_letter_in_inbox_and_presented(self):
        ids = [self.send("worker", subject=f"保留{i}") for i in range(3)]
        self.write_presented("worker", ids)
        out = run_po("archive-current", "--box", "worker", "--keep", ids[1], home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("已归档 2 封", out.stdout)
        self.assertIn("保留 1 封", out.stdout, "要如实报告保留数量：\n" + out.stdout)
        self.assertEqual(self.inbox("worker"), [f"{ids[1]}.md"], "--keep 的信留在 inbox")
        self.assertEqual(self.done("worker"), sorted([f"{ids[0]}.md", f"{ids[2]}.md"]))
        self.assertEqual(self.presented("worker"), {ids[1]},
                         "--keep 的信仍在 presented set")
        again = run_po("archive-current", "--box", "worker", "--keep", ids[1], home=self.home)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.inbox("worker"), [f"{ids[1]}.md"])
        self.assertEqual(self.presented("worker"), {ids[1]})

    def test_archive_current_cleans_stale_refs_and_touches_nothing_else(self):
        a = self.send("worker", subject="还在")
        b = self.send("worker", subject="已归档过")
        os.replace(self.letter_path("worker", b), self.home / "worker" / "done" / f"{b}.md")
        c = self.send("worker", subject="已撤回过")
        arch = self.home / "worker" / "archived" / "20260101-000000"
        arch.mkdir(parents=True, exist_ok=True)
        os.replace(self.letter_path("worker", c), arch / f"{c}.md")
        d = self.send("worker", subject="没展示过")
        ghost = "20990101-000000_boss_幽灵"
        self.write_presented("worker", [a, b, c, ghost])
        out = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertEqual(out.returncode, 0, "stale ref 要幂等清理，不许报错：\n" + out.stdout + out.stderr)
        self.assertEqual(self.inbox("worker"), [f"{d}.md"], "没展示过的信不许被动")
        self.assertEqual(self.done("worker"), sorted([f"{a}.md", f"{b}.md"]), "不得出现重复归档")
        self.assertIn(f"{c}.md", [Path(x).name for x in self.archived("worker")], "已撤回的留在 archived")
        self.assertEqual(self.presented("worker") & {a, b, c, ghost}, set(),
                         "stale 的成功清掉 presented")
        again = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.inbox("worker"), [f"{d}.md"])
        self.assertEqual(self.done("worker"), sorted([f"{a}.md", f"{b}.md"]))

    def test_archive_current_with_no_presented_set_is_a_clean_noop(self):
        d = self.send("worker", subject="没人展示过")
        out = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertEqual(out.returncode, 0, "没有 presented 就是无事可做，退出码为 0：\n" + out.stdout + out.stderr)
        self.assertIn("没有待归档", out.stdout, "空集合要明确说没有待归档：\n" + out.stdout)
        self.assertEqual(self.inbox("worker"), [f"{d}.md"])
        bad = run_po("archive-current", "--box", "ghost", home=self.home)
        self.assertNotEqual(bad.returncode, 0, "未知信箱必须拒绝")

    def test_archive_current_fails_closed_on_a_corrupt_presented_file(self):
        a = self.send("worker", subject="正常归档")
        self.write_presented("worker", [a])
        out = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertEqual(out.returncode, 0, "正常对照必须先成功：\n" + out.stdout + out.stderr)
        self.assertIn(f"{a}.md", self.done("worker"))
        b = self.send("worker", subject="presented 坏了")
        pf = self.home / "worker" / ".presented.json"
        pf.write_text("{not json", encoding="utf-8")
        before = self.inbox("worker")
        bad = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertNotEqual(bad.returncode, 0, "presented 读不出来必须 fail closed")
        self.assertEqual(self.inbox("worker"), before, "读不出 presented 时不许动任何信")
        self.assertEqual(pf.read_text(encoding="utf-8"), "{not json", "不许悄悄覆盖坏文件")

    def test_archive_current_fails_closed_on_non_bare_presented_ids(self):
        victim = self.home / "claude" / "inbox" / "victim.md"
        victim.write_text("来源：boss\n事由：别家的信\n需要：仅告知\n\n正文\n", encoding="utf-8")
        pf = self.home / "worker" / ".presented.json"
        pf.write_text(json.dumps(["../../claude/inbox/victim", "/tmp/po-evil"]), encoding="utf-8")
        out = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertNotEqual(out.returncode, 0, "非裸编号必须整单拒绝")
        self.assertIn("裸编号", out.stdout + out.stderr, "要说清拒绝原因：\n" + out.stdout + out.stderr)
        self.assertTrue(victim.exists(), "别的信箱的信一个字节都不许动")
        self.assertEqual(self.done("worker"), [], "拒绝路径不许归档任何东西")
        self.assertEqual(json.loads(pf.read_text(encoding="utf-8")),
                         ["../../claude/inbox/victim", "/tmp/po-evil"], "坏 presented 不许被改写")

    def test_archive_current_reports_a_done_collision_instead_of_dropping_the_ref(self):
        a = self.send("worker", subject="撞名")
        self.write_presented("worker", [a])
        clash = self.home / "worker" / "done" / f"{a}.md"
        clash.write_text("来源：boss\n事由：done 里已有同名\n需要：仅告知\n\n旧内容\n", encoding="utf-8")
        out = run_po("archive-current", "--box", "worker", home=self.home)
        self.assertNotEqual(out.returncode, 0, "同名冲突要显式报告，不许静默丢引用")
        self.assertIn("同名", out.stdout + out.stderr, out.stdout + out.stderr)
        self.assertIn("另有 1 封", out.stdout + out.stderr, "冲突要显式计数（且区分已归档/保留）：\n" + out.stdout + out.stderr)
        self.assertEqual(self.inbox("worker"), [f"{a}.md"], "冲突时 inbox 的信必须原地不动")
        self.assertIn("旧内容", clash.read_text(encoding="utf-8"), "done/ 里的同名文件不许被动")
        self.assertIn(a, self.presented("worker"), "冲突的引用必须留着，下次还能再来")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
