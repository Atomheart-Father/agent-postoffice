#!/usr/bin/env python3
"""POSTOFFICE_SLACK_WEBHOOK：人操作员信箱的对外告警（Red 先行，写于实现之前）。

冻结契约：
  「人操作员信箱」= routes 里 methods 恰为 ["notify"]、且被 config.json 的
  panel.operator 点名的信箱。当它收到的**正式信**被邮递员一轮处理时，邮递员对
  POSTOFFICE_SLACK_WEBHOOK 恰好发一次 POST（每轮批一次）：Content-Type
  application/json，JSON 体恰好是 {"text": "..."}。
  text 必须包含：来源信箱名 + "有新信"、"事由：" 与事由原文、"需要：" 与需要原文；
  永不包含信件正文。设置了 POSTOFFICE_PANEL_BASE_URL 时，text 里带
  <base>#/mail/<operator>/<完整编号> 的深链；未设置时 text 不许出现任何 "http"。
  同一轮投出的多封正式信合并为一次告警：text 提到 "2 封"，链接是收件箱根
  #/mail/<operator>，不带单封编号。
  webhook 未设置 → 一次请求都没有；webhook 返回 500 → 信照常投递入账
  （.delivered.json、认领照旧）、轮次照常完成，下一轮不重发同一封的告警；
  webhook 长时间不响应 → 投递不被拖住（尽力而为、短超时，本文件用「8 秒才应答的
  webhook 必须在 6.5 秒内先入账」来钉住"短"）；非操作员的 notify 信箱收信 → 0 请求；
  给操作员的回执通知 → 0 请求（只有正式信才告警）。
  机密卫生：webhook URL 里的密钥段不许出现在 <home>/logs/ 任何文件、/api/state 响应、
  面板 HTML 里。

只写测试不改实现，预期在当前树上失败（RED）：现在没有任何 webhook 逻辑——需要
≥1 次请求的用例全红；0 请求的用例是回归锁，现在就该通过。webhook 环境变量只发给
邮递员（及一台对照面板）进程。全部在 mktemp 的临时 POSTOFFICE_HOME 里跑。
"""
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_panelS_"))
SECRET = "TOPSECRET-XYZ"
SENTINEL = "SENTINEL-BODY-slack-9d31"


# ---------------------------------------------------------------- 假 webhook 服务
class FakeSlack(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        srv = self.server
        with srv.lock:
            srv.requests.append({"path": self.path,
                                 "ctype": self.headers.get("Content-Type", ""),
                                 "body": body, "t": time.time()})
        try:
            if srv.mode == "sleep":
                time.sleep(srv.sleep_secs)
            if srv.mode == "fail500":
                self.send_response(500)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
        except (BrokenPipeError, ConnectionResetError):
            pass            # 调用方短超时掉头就走，属预期


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    env.pop("POSTOFFICE_SLACK_WEBHOOK", None)       # 缺省保证未设置；要测就显式传
    env.pop("POSTOFFICE_PANEL_BASE_URL", None)
    env.update(extra)
    return env


def run_po(*args, home, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home), input=stdin, timeout=timeout)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class SlackAlert(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSlack)
        srv.lock = threading.Lock()
        srv.requests = []
        srv.mode = "ok"
        srv.sleep_secs = 0.0
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.srv = srv
        cls.webhook_url = f"http://127.0.0.1:{srv.server_port}/hooks/{SECRET}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        # 每个用例全新临时总站：信箱、配置、台账互不串味，也躲开每箱限流
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ("chief", "helper", "coder"):
            out = run_po("add", box, "--notify", "--who", f"{box} 的说明", home=self.home)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.write_config()
        with self.srv.lock:
            self.srv.requests.clear()
        self.srv.mode = "ok"
        self.srv.sleep_secs = 0.0

    # -- helpers ---------------------------------------------------------
    def write_config(self):
        """operator 用 panel 段点名（实现还没接，先按冻结形状写进配置）。"""
        cfg = {"version": 1,
               "panel": {"label": "Desk", "operator": "chief",
                         "organization": {"mailbox": "chief", "label": "", "children": []}}}
        (self.home / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def send(self, to, sender="coder", subject="测试信", need="回复", body="正文\n"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return re.search(r"编号：(\S+)", out.stdout).group(1)

    def letter_path(self, box, lid):
        return self.home / box / "inbox" / f"{lid}.md"

    def delivered(self):
        f = self.home / ".delivered.json"
        return set(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else set()

    def round_(self, webhook=None, base_url=None, seconds=2.5):
        """跑一轮邮递员：独立进程，POLL=1，到点杀掉；webhook 只发给这个进程。"""
        extra = {}
        if webhook is not None:
            extra["POSTOFFICE_SLACK_WEBHOOK"] = webhook
        if base_url is not None:
            extra["POSTOFFICE_PANEL_BASE_URL"] = base_url
        p = subprocess.Popen([sys.executable, PO, "postman"], env=env_for(self.home, **extra),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(seconds)
        finally:
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def slack_requests(self):
        with self.srv.lock:
            return list(self.srv.requests)

    def slack_texts(self):
        return [json.loads(r["body"].decode("utf-8"))["text"] for r in self.slack_requests()]

    # -- 回归锁：现在就该通过 ----------------------------------------------
    def test_unset_webhook_produces_no_outbound_calls(self):
        lid = self.send("chief", "coder", "未设置对照")
        self.round_()                    # env_for 已保证 POSTOFFICE_SLACK_WEBHOOK 未设置
        self.assertEqual(self.slack_requests(), [], "webhook 未设置时不许有任何对外请求")
        self.assertIn(str(self.letter_path("chief", lid)), self.delivered(),
                      "正面对照：这封信必须照常投递入账")

    def test_non_operator_notify_box_gets_no_alert(self):
        lid = self.send("helper", "coder", "非操作员信")
        self.round_(webhook=self.webhook_url)
        self.assertEqual(self.slack_requests(), [], "非操作员信箱不许触发对外告警")
        self.assertIn(str(self.letter_path("helper", lid)), self.delivered(),
                      "正面对照：这封信必须照常投递入账")

    def test_receipt_notice_to_operator_gets_no_alert(self):
        rid = "20260101-000000_coder_回执"
        p = self.home / "chief" / "inbox" / f"{rid}.md"
        p.write_text("来源：coder\n事由：回执：深链告警\n需要：回执（默认不答复）\n"
                     "回执：20260101-000000_x\n原事由：深链告警\n\n回执正文也不外传\n",
                     encoding="utf-8")
        self.round_(webhook=self.webhook_url)
        self.assertEqual(self.slack_requests(), [], "回执通知不是正式信，不许触发告警")
        self.assertIn(str(p), self.delivered(), "正面对照：回执通知本身照常投递入账")

    def test_webhook_secret_never_leaks(self):
        lid = self.send("chief", "coder", "机密卫生")
        self.round_(webhook=self.webhook_url)
        self.assertEqual(len(self.slack_requests()), 1, "正面对照：告警要真的发过一次")
        port = free_port()
        startup = self.home / "panel_startup.log"
        with open(startup, "wb") as fh:
            proc = subprocess.Popen(
                [sys.executable, PO, "panel", "--no-open", "--port", str(port)],
                env=env_for(self.home, POSTOFFICE_SLACK_WEBHOOK=self.webhook_url),
                stdout=fh, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 15
            state_text = html_text = ""
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.fail("对照面板进程提前退出")
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    conn.request("GET", "/api/state")
                    resp = conn.getresponse()
                    state_text = resp.read().decode("utf-8", errors="replace")
                    conn.close()
                    if resp.status == 200:
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                        conn.request("GET", "/")
                        resp = conn.getresponse()
                        html_text = resp.read().decode("utf-8", errors="replace")
                        conn.close()
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                self.fail("对照面板没有在 15 秒内起来")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for f in sorted((self.home / "logs").rglob("*")):
            if f.is_file():
                self.assertNotIn(SECRET, f.read_text(encoding="utf-8", errors="replace"),
                                 f"日志不许泄漏 webhook 密钥：{f}")
        self.assertNotIn(SECRET, state_text, "/api/state 不许泄漏 webhook 密钥")
        self.assertNotIn(SECRET, html_text, "面板 HTML 不许泄漏 webhook 密钥")

    # -- RED：操作员告警主体 ------------------------------------------------
    def test_single_letter_alerts_once_with_required_facts_and_no_body(self):
        lid = self.send("chief", "coder", "发布检查", need="回复",
                        body=SENTINEL + "\n这一段是机密正文，绝不能出门\n")
        self.round_(webhook=self.webhook_url)
        reqs = self.slack_requests()
        self.assertEqual(len(reqs), 1, f"一轮恰好一次告警，实际 {len(reqs)} 次：{reqs}")
        req = reqs[0]
        self.assertEqual(req["path"], "/hooks/" + SECRET, req["path"])
        self.assertTrue(req["ctype"].startswith("application/json"), req["ctype"])
        payload = json.loads(req["body"].decode("utf-8"))
        self.assertEqual(set(payload), {"text"}, f"JSON 体必须是 {'{'}text{'}'}：{payload}")
        text = payload["text"]
        self.assertIn("coder", text, f"text 要带来源信箱名：{text!r}")
        self.assertIn("有新信", text)
        self.assertIn("事由：", text)
        self.assertIn("发布检查", text)
        self.assertIn("需要：", text)
        self.assertIn("回复", text)
        self.assertNotIn(SENTINEL, text, "告警永不带信件正文")
        self.assertNotIn("机密正文", text, "告警永不带信件正文")
        self.assertNotIn("http", text, "未设置 BASE_URL 时不许伪造链接")

    def test_base_url_adds_the_deep_link(self):
        lid = self.send("chief", "coder", "深链告警")
        self.round_(webhook=self.webhook_url, base_url="https://desk.example.com")
        reqs = self.slack_requests()
        self.assertEqual(len(reqs), 1, f"一轮恰好一次告警，实际 {len(reqs)} 次：{reqs}")
        text = json.loads(reqs[0]["body"].decode("utf-8"))["text"]
        self.assertIn(f"https://desk.example.com#/mail/chief/{lid}", text,
                      f"text 要带单封深链（编号 {lid}）：{text!r}")
        self.assertIn("有新信", text)

    def test_two_letters_same_round_alert_once_with_inbox_root(self):
        ids = [self.send("chief", "coder", f"批量告警{i}") for i in range(2)]
        self.round_(webhook=self.webhook_url)
        reqs = self.slack_requests()
        self.assertEqual(len(reqs), 1, f"两封同轮只许一次告警，实际 {len(reqs)} 次：{reqs}")
        text = json.loads(reqs[0]["body"].decode("utf-8"))["text"]
        self.assertIn("2 封", text, f"批量告警要说清封数：{text!r}")
        self.assertIn("有新信", text)
        self.assertIn("#/mail/chief", text, f"批量告警给收件箱根链接：{text!r}")
        for lid in ids:
            self.assertNotIn(f"#/mail/chief/{lid}", text,
                             f"批量形态不带单封编号链接：{text!r}")

    def test_failed_webhook_still_delivers_and_never_realerts(self):
        lid = self.send("chief", "coder", "失败也入账")
        self.srv.mode = "fail500"
        self.round_(webhook=self.webhook_url)
        self.assertEqual(len(self.slack_requests()), 1, "失败的那次告警确实发出过")
        letter = self.letter_path("chief", lid)
        self.assertTrue(letter.is_file(), "notify 信箱的信照常留在 inbox")
        self.assertIn(str(letter), self.delivered(), "告警失败不许拦住投递入账")
        claims = sorted(p.name for p in (self.home / "chief" / ".claims").glob("*"))
        self.assertIn(f"{lid}.md", claims, "投递认领照旧")
        self.srv.mode = "ok"
        self.round_(webhook=self.webhook_url)
        self.assertEqual(len(self.slack_requests()), 1, "下一轮不许重发同一封的告警")
        self.assertIn(str(letter), self.delivered())

    def test_slow_webhook_does_not_stall_the_round(self):
        lid = self.send("chief", "coder", "慢告警不挡路")
        self.srv.mode = "sleep"
        self.srv.sleep_secs = 8.0
        p = subprocess.Popen([sys.executable, PO, "postman"],
                             env=env_for(self.home, POSTOFFICE_SLACK_WEBHOOK=self.webhook_url),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marked = False
        try:
            deadline = time.time() + 6.5        # webhook 8 秒后才应答：入账必须先于它完成
            while time.time() < deadline:
                if str(self.letter_path("chief", lid)) in self.delivered():
                    marked = True
                    break
                if p.poll() is not None:
                    break
                time.sleep(0.2)
        finally:
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        self.assertTrue(marked, "投递入账必须在慢 webhook 应答之前完成（短超时、尽力而为）")
        self.assertEqual(len(self.slack_requests()), 1, "告警本身要发过一次")
        self.assertTrue(self.letter_path("chief", lid).is_file(), "信照常留在 inbox")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
