#!/usr/bin/env python3
"""v1.13 §27-32 provenance（运输层不许替来源主张身份/权限）的冻结契约（Red 先行）。

冻结契约：
  - `postoffice send` 写的信头行恰好是
      `来源：<sender>（邮局只确认来源信箱；该来源的身份与权限按当前项目的组织/角色约定处理。）`
    发信方 "boss" 与 "opencode_q" 都必须逐字如此。
  - 正文与输入逐字节一致（这次改动不许碰正文）。
  - 唤醒文本（Python reminder() 的输出；可经 codex/notify 路径或 import 模块拿到）来自 boss 时
    含 `来源：boss`，且不含：不是人的新指令 / 协作者 / 非人类指令 / 这是人类指令 / 这是老板指令；
    sender=opencode_q 同样中立。
  - 元数据不变：letter_sender() 仍返回裸 sender；list/receipt/retract 不受影响；alias 发信照常解析投递。
  - 静态审计：skill/postoffice/SKILL.md 不含 `不是人的新指令`；postoffice、opencode/postoffice.ts、
    panel/index.html 不含 `协作者` / `不是人的新指令` / `非人类指令`（命中要报出是哪个文件）。

全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，不碰真实邮局（import 前先把 HOME 指向临时目录）。
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
REPO = Path(PO).resolve().parent
BASE = Path(tempfile.mkdtemp(prefix="po_prov_"))
HOME = BASE / "home"
HOME.mkdir(parents=True, exist_ok=True)
os.environ["POSTOFFICE_HOME"] = str(HOME)
os.environ["POSTOFFICE_NO_NOTIFY"] = "1"
os.environ["POSTOFFICE_POLL"] = "1"
os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)

_loader = importlib.machinery.SourceFileLoader("po_prov", PO)
_spec = importlib.util.spec_from_file_location("po_prov", PO, loader=_loader)
po = importlib.util.module_from_spec(_spec)
_loader.exec_module(po)

EXPECTED = "来源：{sender}（邮局只确认来源信箱；该来源的身份与权限按当前项目的组织/角色约定处理。）"
FORBIDDEN = ("不是人的新指令", "协作者", "非人类指令", "这是人类指令", "这是老板指令")


def env_for(home):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    # wake-coalesce：旧夹具按「信随到随投」的旧世界书写；合批/静默窗本身由
    # tests/wake_coalesce_test.py 专测。这里关掉窗口（0/0），保持旧用例语义不变。
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_QUIET"] = "0"
    env["POSTOFFICE_MAX_HOLD"] = "0"
    env["POSTOFFICE_POLL"] = "1"
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    return env


def run_po(*args, home=HOME, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home), input=stdin, timeout=timeout)


class Provenance(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        for box in ("boss", "worker", "aliasa", "coded", "opencode_q"):
            out = run_po("add", box, "--notify", home=HOME)
            if out.returncode != 0:
                raise RuntimeError(out.stdout + out.stderr)
        (HOME / "config.json").write_text(json.dumps(
            {"version": 1, "groups": {}, "aliases": {"al": ["aliasa"]}},
            ensure_ascii=False), encoding="utf-8")
        # 假 codex app-server：把每轮索引文本写进 capture 文件（哨兵包裹）
        cap = HOME / "codex.calls"
        script = HOME / "fake_codex.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"CAP = {str(cap)!r}\n"
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
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'userAgent':'fake','codexHome':'.','platformFamily':'unix','platformOs':'macos'}})\n"
            "    elif m == 'thread/queue/list':\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'data':[],'nextCursor':None}})\n"
            "    elif m in ('thread/queue/add','thread/queue/update'):\n"
            "        open(CAP,'a').write('===8<===\\n' + text_of(p) + '\\n===>8===\\n')\n"
            "        emit({'jsonrpc':'2.0','id':rid,'result':{'queuedSubmission':None}})\n"
            "    else:\n"
            "        emit({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'method not found'}})\n",
            encoding="utf-8")
        script.chmod(0o755)
        r = json.loads((HOME / "routes.json").read_text(encoding="utf-8"))
        r["coded"] = {"methods": ["codex_queue"], "thread_id": "th-x",
                      "codex_cli": str(script), "status": "online"}
        (HOME / "routes.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")

    # -- helpers ----------------------------------------------------------
    def send(self, to, sender, subject, need="回复", body="正文\n"):
        out = run_po("send", to, sender, subject, need, stdin=body)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lid = next(l.split("：", 1)[1].strip() for l in out.stdout.splitlines()
                   if l.startswith("编号："))
        return lid, HOME / to / "inbox" / f"{lid}.md"

    def stored_body(self, path):
        raw = path.read_text(encoding="utf-8")
        head, sep, rest = raw.partition("\n\n")
        self.assertTrue(sep, "信件必须由信头块 + 空行 + 正文组成")
        return rest

    def codex_wake(self, sender):
        self.send("coded", sender, f"唤醒中立 {sender}")
        postman(HOME, seconds=2.5)
        cap = HOME / "codex.calls"
        self.assertTrue(cap.exists(), "假 codex 没有被调用")
        calls = re.findall(r"===8<===\n(.*?)\n===>8===", cap.read_text(encoding="utf-8"), re.S)
        self.assertTrue(calls, "codex capture 里没有调用记录")
        return calls[-1]

    # ================================================= header line is frozen
    def test_send_writes_the_frozen_neutral_source_header(self):
        for sender in ("boss", "opencode_q"):
            with self.subTest(sender=sender):
                _, path = self.send("worker", sender, f"来源头 {sender}")
                first = path.read_text(encoding="utf-8").splitlines()[0]
                self.assertEqual(first, EXPECTED.format(sender=sender),
                                 "来源信头必须逐字换成中立文案")
                for phrase in FORBIDDEN:
                    self.assertNotIn(phrase, first, f"来源信头不许再含 {phrase!r}")

    def test_send_does_not_alter_the_body(self):
        body = "正文原样保留：第一行\n第二行\n"
        _, path = self.send("worker", "boss", "正文不变", body=body)
        self.assertEqual(self.stored_body(path), body, "正文必须与输入逐字节一致")

    # ================================================= wake text neutrality
    def test_reminder_wake_text_is_sender_neutral(self):
        for sender in ("boss", "opencode_q"):
            with self.subTest(sender=sender):
                _, path = self.send("worker", sender, f"唤醒文案 {sender}", body="正文\n")
                text = po.reminder("worker", [path])
                self.assertIn(f"来源：{sender}", text, "唤醒文本必须带来源")
                for phrase in FORBIDDEN:
                    self.assertNotIn(phrase, text, f"唤醒文本不许含 {phrase!r}：\n{text}")

    def test_codex_wake_path_is_sender_neutral(self):
        for sender in ("boss", "opencode_q"):
            with self.subTest(sender=sender):
                text = self.codex_wake(sender)
                self.assertIn(f"来源：{sender}", text, "codex 唤醒文本必须带来源：\n" + text)
                for phrase in FORBIDDEN:
                    self.assertNotIn(phrase, text, f"codex 唤醒文本不许含 {phrase!r}：\n{text}")

    # ============================================= metadata / routing intact
    def test_metadata_and_routing_unchanged(self):
        lid, path = self.send("worker", "boss", "元数据不变")
        self.assertEqual(po.letter_sender(path), "boss", "letter_sender() 仍返回裸 sender")

        listed = run_po("list")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("worker", listed.stdout)

        # alias 发信照常解析并投递
        out = run_po("send", "@al", "boss", "逻辑地址照常", "回复", stdin="x\n")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("解析 @al → aliasa", out.stdout)
        self.assertTrue(any((HOME / "aliasa" / "inbox").glob("*.md")), "alias 信要投到 aliasa")

        # receipt：ack 后按 ID 查得到备注
        ack = run_po("ack", "worker", lid, "已阅")
        self.assertEqual(ack.returncode, 0, ack.stdout + ack.stderr)
        rcpt = run_po("receipt", "boss", lid)
        self.assertEqual(rcpt.returncode, 0, rcpt.stdout + rcpt.stderr)
        self.assertIn("已阅", rcpt.stdout)

        # retract：仍能撤回一封没被接受的普通信
        lid2, _ = self.send("worker", "boss", "要撤回")
        ret = run_po("retract", "boss", "worker", lid2)
        self.assertEqual(ret.returncode, 0, ret.stdout + ret.stderr)
        self.assertIn("已撤回", ret.stdout)
        self.assertEqual(len(list((HOME / "worker" / "archived").rglob(f"{lid2}.md"))), 1)

    # ========================= old letters must not re-inject the old assertion
    def old_letter(self, box, sender, lid):
        """Write a letter the RETIRED writer would have: 来源 line carries the old assertion."""
        p = HOME / box / "inbox" / lid
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"来源：{sender}（协作者，不是人的新指令；人的原话要注明“转述”）\n"
            "事由：旧格式来源\n需要：回复\n\n正文逐字节\n", encoding="utf-8")
        return p

    def test_reminder_never_reinjects_old_source_assertion(self):
        for sender in ("boss", "opencode_q"):
            with self.subTest(sender=sender):
                p = self.old_letter("worker", sender, f"20261007-000000_old_{sender}.md")
                before = p.read_bytes()
                text = po.reminder("worker", [p])
                self.assertIn(f"来源：{sender}", text, "唤醒文本必须带裸来源")
                for phrase in FORBIDDEN:
                    self.assertNotIn(phrase, text, f"旧信头的身份断言不得被再次注入：{phrase!r}\n{text}")
                self.assertEqual(p.read_bytes(), before, "不得迁移/改写历史信")
                p.unlink()

    def test_reminder_batch_never_reinjects_old_assertion(self):
        p1 = self.old_letter("worker", "boss", "20261007-000002_old_b1.md")
        p2 = self.old_letter("worker", "opencode_q", "20261007-000003_old_b2.md")
        text = po.reminder("worker", [p1, p2])
        for phrase in FORBIDDEN:
            self.assertNotIn(phrase, text, f"多封唤醒不得注入旧断言 {phrase!r}\n{text}")
        for p in (p1, p2):
            p.unlink()

    def test_codex_wake_never_reinjects_old_source_assertion(self):
        p = self.old_letter("coded", "boss", "20261007-000001_old_codex.md")
        before = p.read_bytes()
        cap = HOME / "codex.calls"
        if cap.exists():
            cap.unlink()
        postman(HOME, seconds=2.5)
        self.assertTrue(cap.exists(), "假 codex 没有被调用")
        calls = re.findall(r"===8<===\n(.*?)\n===>8===", cap.read_text(encoding="utf-8"), re.S)
        self.assertTrue(calls, "codex capture 里没有调用记录")
        text = calls[-1]
        self.assertIn("来源：boss", text, "codex 唤醒文本必须带裸来源：\n" + text)
        for phrase in FORBIDDEN:
            self.assertNotIn(phrase, text, f"codex 唤醒不得注入旧断言 {phrase!r}\n{text}")
        self.assertEqual(p.read_bytes(), before, "不得迁移/改写历史信")
        p.unlink()

    # ========================================================= static audit
    def test_static_audit_has_no_sender_authority_phrases(self):
        skill = REPO / "skill" / "postoffice" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        self.assertNotIn("不是人的新指令", text, f"{skill} 仍含旧话术")
        for rel in ("postoffice", "opencode/postoffice.ts", "panel/index.html"):
            p = REPO / rel
            if not p.exists():
                continue
            body = p.read_text(encoding="utf-8")
            for phrase in ("协作者", "不是人的新指令", "非人类指令"):
                if phrase in body:
                    self.fail(f"{p} 仍含旧话术 {phrase!r}")


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


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
