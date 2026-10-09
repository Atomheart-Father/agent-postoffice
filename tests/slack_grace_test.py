#!/usr/bin/env python3
"""Slack 同步：20 分钟未确认成功唤醒/投递的超时提醒（小票，Red 先行，写于实现之前）。

冻结契约（用户追加小票，转述）：
  「现在 xxx 20 分钟没看信会弹窗，希望这条邮局给老板的提醒也送到 Slack。」
  - 复用 POSTOFFICE_SLACK_WEBHOOK / POSTOFFICE_PANEL_BASE_URL，不新增 webhook 配置。
  - 接在现有 claude_hook / opencode_plugin 的 GRACE 到期通知分支：原桌面弹窗保留，向同一
    已配置 Slack 追加一条短告警；含目标信箱、事由、面板信件链接，**不含正文**。
  - 措辞如实写「20 分钟未确认成功唤醒/投递」——不得宣称没读或模型忽略（本系统没有已读检测）。
  - 沿用同一超时告警去重依据（同一 mark），不写第二套投递真相，不把告警记成已送达。
  - GRACE 未到 → 无请求；一轮一条；下一轮 / 正常重启不重复。
  - 已接受（.seen）/ 离线 / 闹钟信 → 无新增告警。
  - 失败或慢响应：短超时，不影响桌面弹窗与投递记账，不循环重试，不回显 webhook 密钥。
  - Slack 未配置 → 零请求。

只写测试不改实现，预期在当前树上失败（RED）：现在 GRACE 分支只弹桌面窗、从不发 Slack。
全部在 mktemp 的临时 POSTOFFICE_HOME 里跑；webhook 只发给邮递员进程。
"""
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
BASE = Path(tempfile.mkdtemp(prefix="po_slackgrace_"))
SECRET = "GRACE-SECRET-777"
SENTINEL = "GRACE-BODY-8a1f"


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
            pass


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    # wake-coalesce：夹具信按「随到随投」书写；静默窗/合批由 wake_coalesce_test 专测，这里关窗。
    env["POSTOFFICE_QUIET"] = "0"
    env["POSTOFFICE_MAX_HOLD"] = "0"
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    env.pop("POSTOFFICE_SLACK_WEBHOOK", None)
    env.pop("POSTOFFICE_PANEL_BASE_URL", None)
    env.update(extra)
    return env


def run_po(*args, home, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home), input=stdin, timeout=timeout)


class SlackGrace(unittest.TestCase):
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
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ("chief", "hookbox", "h2"):
            out = run_po("add", box, "--notify", home=self.home)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.write_config()
        self.write_routes()
        with self.srv.lock:
            self.srv.requests.clear()
        self.srv.mode = "ok"
        self.srv.sleep_secs = 0.0

    # -- helpers ---------------------------------------------------------
    def write_config(self):
        cfg = {"version": 1,
               "panel": {"label": "Desk", "operator": "chief",
                         "organization": {"mailbox": "chief", "label": "", "children": []}}}
        (self.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def write_routes(self, hook_status="online", h2_status="online"):
        routes = {
            "chief": {"methods": ["notify"], "status": "online"},
            "hookbox": {"methods": ["claude_hook"], "status": hook_status,
                        "claude_title": "Hook", "claude_session": "s_hook"},
            "h2": {"methods": ["claude_hook"], "status": h2_status,
                   "claude_title": "H2", "claude_session": "s_h2"},
        }
        (self.home / "routes.json").write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")

    def send(self, to, sender="coder", subject="测试信", need="回复", body="正文\n"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return re.search(r"编号：(\S+)", out.stdout).group(1)

    def letter_path(self, box, lid):
        return self.home / box / "inbox" / f"{lid}.md"

    def delivered(self):
        f = self.home / ".delivered.json"
        return set(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else set()

    def round_(self, webhook=None, base_url=None, grace=0, seconds=2.5):
        """跑一轮邮递员：独立进程，POLL=1，到点杀掉；grace 默认 0（立刻到期）。"""
        extra = {"POSTOFFICE_GRACE": str(grace)}
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

    def notify_log(self):
        f = self.home / "logs" / "notify.log"
        return f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""

    # -- 回归锁 ------------------------------------------------------------
    def test_unset_webhook_produces_no_outbound_calls(self):
        lid = self.send("hookbox", "coder", "未设置对照")
        self.round_()                       # env_for 保证 webhook 未设置
        self.assertEqual(self.slack_requests(), [], "webhook 未设置时不许有任何对外请求")
        self.assertIn("20 分钟未被唤醒", self.notify_log(), "桌面弹窗照旧")

    def test_notify_path_does_not_use_the_grace_wording(self):
        # 对照：操作员的普通正式信走既有「投递告警」，不得混入超时措辞
        self.send("chief", "coder", "普通正式信", need="回复")
        self.round_(webhook=self.webhook_url)
        reqs = self.slack_requests()
        self.assertEqual(len(reqs), 1, "操作员正式信走既有投递告警，恰好一条")
        text = json.loads(reqs[0]["body"].decode("utf-8"))["text"]
        self.assertIn("有新信", text)
        self.assertNotIn("20 分钟未确认成功唤醒/投递", text, "notify 路径不许用超时措辞")

    def test_no_alert_before_grace(self):
        self.send("hookbox", "coder", "还没到点")
        self.round_(webhook=self.webhook_url, grace=1200)
        self.assertEqual(self.slack_requests(), [], "GRACE 未到不许发 Slack")

    # -- 主体 --------------------------------------------------------------
    def test_grace_timeout_alerts_slack_with_metadata_only(self):
        lid = self.send("hookbox", "coder", "卡住的信", need="回复", body=SENTINEL + "\n这一步是机密正文\n")
        self.round_(webhook=self.webhook_url, base_url="https://desk.example.com")
        reqs = self.slack_requests()
        self.assertEqual(len(reqs), 1, f"一轮恰好一次超时告警，实际 {len(reqs)}：{reqs}")
        req = reqs[0]
        self.assertEqual(req["path"], "/hooks/" + SECRET, req["path"])
        self.assertTrue(req["ctype"].startswith("application/json"), req["ctype"])
        payload = json.loads(req["body"].decode("utf-8"))
        self.assertEqual(set(payload), {"text"}, f"JSON 体必须是 {{text}}：{payload}")
        text = payload["text"]
        self.assertIn("hookbox", text, f"要带目标信箱：{text!r}")
        self.assertIn("20 分钟未确认成功唤醒/投递", text, f"措辞要如实：{text!r}")
        self.assertIn("事由：", text)
        self.assertIn("卡住的信", text)
        self.assertIn(f"https://desk.example.com#/mail/hookbox/{lid}", text, f"要带面板信件链接：{text!r}")
        self.assertNotIn(SENTINEL, text, "超时告警永不带正文")
        self.assertNotIn("机密正文", text, "超时告警永不带正文")
        self.assertIn("20 分钟未被唤醒", self.notify_log(), "桌面弹窗必须保留")

    def test_no_base_url_no_fake_link(self):
        self.send("hookbox", "coder", "无深链")
        self.round_(webhook=self.webhook_url)
        text = json.loads(self.slack_requests()[0]["body"].decode("utf-8"))["text"]
        self.assertNotIn("http", text, "未设置 BASE_URL 时不许出现任何 http")
        self.assertIn("#/mail/hookbox/", text, "退回相对链接")

    def test_next_round_never_repeats(self):
        self.send("hookbox", "coder", "只提醒一次")
        self.round_(webhook=self.webhook_url)
        self.assertEqual(len(self.slack_requests()), 1, "第一轮一条")
        self.round_(webhook=self.webhook_url)
        self.assertEqual(len(self.slack_requests()), 1, "下一轮不许重发同一封的超时告警")

    def test_accepted_letter_gets_no_alert(self):
        lid = self.send("hookbox", "coder", "已经被接受")
        (self.home / "hookbox" / ".seen").write_text(str(self.letter_path("hookbox", lid)) + "\n",
                                                     encoding="utf-8")
        self.round_(webhook=self.webhook_url)
        self.assertEqual(self.slack_requests(), [], "通道已接受的信不许再告警")

    def test_offline_box_gets_no_alert(self):
        self.send("hookbox", "coder", "离线不催")
        self.write_routes(hook_status="offline")
        self.round_(webhook=self.webhook_url)
        self.assertEqual(self.slack_requests(), [], "离线信箱不告警（计时也不走）")

    def test_alarm_letter_gets_no_alert(self):
        rid = "20260101-000000_coder_闹钟"
        p = self.home / "hookbox" / "inbox" / f"{rid}.md"
        p.write_text("来源：coder\n事由：到点提醒\n需要：仅告知\n闹钟：2026-01-01T00:00\n\n闹钟正文\n",
                     encoding="utf-8")
        self.round_(webhook=self.webhook_url)
        self.assertEqual(self.slack_requests(), [], "闹钟信不催人，也不该同步 Slack")

    def test_failed_webhook_still_notifies_desktop_and_keeps_bookkeeping(self):
        lid = self.send("hookbox", "coder", "失败也记账")
        self.srv.mode = "fail500"
        self.round_(webhook=self.webhook_url)
        self.assertEqual(len(self.slack_requests()), 1, "失败的那次确实发出过")
        self.assertIn(str(self.letter_path("hookbox", lid)), self.delivered(),
                      "告警失败不许拦住超时记账（不改投递真相）")
        self.assertTrue(self.letter_path("hookbox", lid).is_file(), "信照常留在 inbox")
        self.assertIn("20 分钟未被唤醒", self.notify_log(), "桌面弹窗照旧")

    def test_slow_webhook_does_not_block_the_next_box(self):
        self.send("hookbox", "coder", "先卡住")
        l2 = self.send("h2", "coder", "后一个信箱")
        self.srv.mode = "sleep"
        self.srv.sleep_secs = 8.0
        p = subprocess.Popen([sys.executable, PO, "postman"],
                             env=env_for(self.home, POSTOFFICE_SLACK_WEBHOOK=self.webhook_url,
                                         POSTOFFICE_GRACE="0"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marked = False
        try:
            deadline = time.time() + 6.5
            while time.time() < deadline:
                if str(self.letter_path("h2", l2)) in self.delivered():
                    marked = True
                    break
                if p.poll() is not None:
                    break
                time.sleep(0.1)
        finally:
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        self.assertTrue(marked, "挂起的 webhook 不得把下一个信箱的超时记账拖过 6.5s（timeout 必须秒级）")

    def test_secret_never_leaks_in_logs(self):
        self.send("hookbox", "coder", "机密卫生")
        self.srv.mode = "fail500"
        self.round_(webhook=self.webhook_url)
        for f in sorted((self.home / "logs").rglob("*")):
            if f.is_file():
                self.assertNotIn(SECRET, f.read_text(encoding="utf-8", errors="replace"),
                                 f"日志不许泄漏 webhook 密钥：{f}")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
