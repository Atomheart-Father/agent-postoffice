#!/usr/bin/env python3
"""$POSTOFFICE_HOME/.env：私有运行设置的最小 stdlib 读取器（冻结契约）。

契约（v1.12 gate fix）：
  - 路径固定 `$POSTOFFICE_HOME/.env`（默认 ~/agent-postoffice/.env）；POSTOFFICE_HOME
    本身只由 process env 决定，.env 不影响这个进程的 HOME 解析。
  - 启动顺序：先按 process env 定 HOME，再读 HOME/.env，然后把其中允许的
    POSTOFFICE_* 键补进 os.environ —— 所有后续常量（POLL/GRACE/PANEL_BASE_URL/
    SLACK_WEBHOOK/...）因此都能来自 .env。
  - precedence：已有 process env 最高优先；.env 只补 os.environ 里不存在的键。
  - 解析：普通 `POSTOFFICE_NAME=value`；空行与 `#` 注释跳过；值可被成对的单/双引号
    包裹（只去外层引号）；只加载 `^POSTOFFICE_[A-Z0-9_]+$`；其它键忽略。
    不做 shell 语法：无 source/eval/变量展开/命令替换，`$(...)`、反引号、$HOME 全部
    按普通字符串保存；值里的内容绝不会被执行。
  - malformed 行安全忽略，诊断只报行号、绝不回显该行内容；一行坏行不许破坏
    routing/delivery；secret 值不许出现在 stderr/logs/state/HTML。
  - launchd 语义：子进程环境只有 POSTOFFICE_HOME+PATH 时，webhook 与 base URL
    仅存在于 $POSTOFFICE_HOME/.env，邮递员必须照样读到并执行 FakeSlack 请求。

全部在 mktemp 的临时 POSTOFFICE_HOME 里跑；真实 ~/agent-postoffice/.env 绝不读取/覆盖。
"""
import contextlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import io
import itertools
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
BASE = Path(tempfile.mkdtemp(prefix="po_envf_"))
SECRET = "ENVSECRET-7f3a"
_ctr = itertools.count()

# ---------------------------------------------------------------- 假 webhook 服务
class FakeSlack(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        with self.server.lock:
            self.server.requests.append({"path": self.path, "body": body, "t": time.time()})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def env_base(home, **extra):
    """普通测试进程环境：.env 键一律先剔掉，要测就显式传或写进 .env。"""
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    env.pop("POSTOFFICE_SLACK_WEBHOOK", None)
    env.pop("POSTOFFICE_PANEL_BASE_URL", None)
    env.update(extra)
    return env


def run_po(*args, home, env=None, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env if env is not None else env_base(home),
                          input=stdin, timeout=timeout)


def import_postoffice(home):
    """白盒：在指定 POSTOFFICE_HOME 下真的 import 一次 postoffice（会执行 .env loader）。
    返回 (loader 运行后的 os.environ 快照, stderr 文本)；无论成败都完整还原本进程环境。"""
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(saved)
        os.environ["POSTOFFICE_HOME"] = str(home)
        os.environ.pop("POSTOFFICE_SLACK_WEBHOOK", None)
        os.environ.pop("POSTOFFICE_PANEL_BASE_URL", None)
        err = io.StringIO()
        name = "po_env_under_test_%d" % next(_ctr)
        with contextlib.redirect_stderr(err):
            loader = importlib.machinery.SourceFileLoader(name, PO)
            spec = importlib.util.spec_from_loader(name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
        assert mod.VERSION  # 模块真的执行到了
        return dict(os.environ), err.getvalue()
    finally:
        os.environ.clear()
        os.environ.update(saved)


class WhiteBox(unittest.TestCase):
    """解析器机制：precedence / 白名单 / 注释 / 引号 / malformed / 无展开。"""

    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def write_env(self, text):
        (self.home / ".env").write_text(text, encoding="utf-8")

    def test_dotenv_fills_missing_keys(self):
        self.write_env("POSTOFFICE_SLACK_WEBHOOK=http://127.0.0.1:9/hooks/x\n"
                       "POSTOFFICE_POLL=3\n")
        env, err = import_postoffice(self.home)
        self.assertEqual(env.get("POSTOFFICE_SLACK_WEBHOOK"), "http://127.0.0.1:9/hooks/x")
        self.assertEqual(env.get("POSTOFFICE_POLL"), "3")
        self.assertEqual(err, "", "合法文件不该有任何诊断输出")

    def test_process_env_wins_over_dotenv(self):
        self.write_env("POSTOFFICE_SLACK_WEBHOOK=file-value\n")
        saved = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(saved)
            os.environ["POSTOFFICE_SLACK_WEBHOOK"] = "proc-value"
            os.environ["POSTOFFICE_HOME"] = str(self.home)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                loader = importlib.machinery.SourceFileLoader("po_env_prec_%d" % next(_ctr), PO)
                mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
                loader.exec_module(mod)
            self.assertEqual(os.environ["POSTOFFICE_SLACK_WEBHOOK"], "proc-value",
                             "process env 必须赢过 .env")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_non_postoffice_keys_ignored(self):
        self.write_env("FOO=bar\nPATH=/evil\npostoffice_lower=1\n"
                       "POSTOFFICE_Mixed=1\nPOSTOFFICE_OK=1\n")
        saved_path = os.environ.get("PATH")
        env, _ = import_postoffice(self.home)
        self.assertNotIn("FOO", env, "非 POSTOFFICE_* 键不许加载")
        self.assertEqual(env.get("PATH"), saved_path, "PATH 必须原样")
        self.assertNotIn("postoffice_lower", env, "小写键忽略")
        self.assertNotIn("POSTOFFICE_Mixed", env, "键必须全大写（[A-Z0-9_]）")
        self.assertEqual(env.get("POSTOFFICE_OK"), "1")

    def test_comments_blanks_and_simple_quotes(self):
        self.write_env("# 注释\n\n   # 缩进注释\n"
                       "POSTOFFICE_A=\"v v\"\nPOSTOFFICE_B='x'\nPOSTOFFICE_C=plain  \n")
        env, err = import_postoffice(self.home)
        self.assertEqual(env.get("POSTOFFICE_A"), "v v")
        self.assertEqual(env.get("POSTOFFICE_B"), "x")
        self.assertEqual(env.get("POSTOFFICE_C"), "plain", "值两端空白去掉")
        self.assertEqual(err, "")

    def test_malformed_lines_ignored_with_value_free_diagnostic(self):
        self.write_env("not a line at all\n"
                       "=oops\n"
                       "POSTOFFICE_BAD KEY=1\n"
                       "POSTOFFICE_Q=\"unclosed\n"
                       "POSTOFFICE_GOOD=1\n")
        env, err = import_postoffice(self.home)   # 不许抛异常
        self.assertEqual(env.get("POSTOFFICE_GOOD"), "1",
                         "相邻合法行必须照常加载（坏行不破坏整个文件）")
        self.assertNotIn("POSTOFFICE_Q", env)
        self.assertNotIn("POSTOFFICE_BAD KEY", env)
        self.assertTrue(err.strip(), "malformed 行应有不含 value 的诊断")
        for payload in ("not a line at all", "oops", "unclosed"):
            self.assertNotIn(payload, err, "诊断不许回显行内容（可能是 secret）")

    def test_no_shell_expansion_or_command_substitution(self):
        marker1 = self.home / "pwn1"
        marker2 = self.home / "pwn2"
        self.write_env(f"POSTOFFICE_X=$(touch {marker1})\n"
                       f"POSTOFFICE_Y=`touch {marker2}`\n"
                       "POSTOFFICE_Z=$HOME\n")
        env, _ = import_postoffice(self.home)
        self.assertEqual(env.get("POSTOFFICE_X"), f"$(touch {marker1})", "命令替换必须是字面串")
        self.assertEqual(env.get("POSTOFFICE_Y"), f"`touch {marker2}`")
        self.assertEqual(env.get("POSTOFFICE_Z"), "$HOME", "变量展开必须不发生")
        self.assertFalse(marker1.exists(), "命令不被执行")
        self.assertFalse(marker2.exists(), "命令不被执行")

    def test_dotenv_as_directory_is_ignored(self):
        (self.home / ".env").mkdir()
        env, err = import_postoffice(self.home)   # 不许抛
        self.assertNotIn("POSTOFFICE_SLACK_WEBHOOK", env)
        self.assertEqual(err, "")


class Behavior(unittest.TestCase):
    """真实进程行为：.env 里的 webhook/base URL 生效；launchd 风格自证。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSlack)
        srv.lock = threading.Lock()
        srv.requests = []
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
        for box in ("chief", "coder"):
            out = run_po("add", box, "--notify", "--who", f"{box} 的说明", home=self.home)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        cfg = {"version": 1,
               "panel": {"label": "Desk", "operator": "chief",
                         "organization": {"mailbox": "chief", "label": "", "children": []}}}
        (self.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                               encoding="utf-8")
        with self.srv.lock:
            self.srv.requests.clear()

    # -- helpers ---------------------------------------------------------
    def send(self, to, sender="coder", subject="测试信", need="回复", body="正文\n"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lid = re.search(r"编号：(\S+)", out.stdout).group(1)
        # wake-coalesce 合同：夹具信按「会话离开期间到达」老化；静默窗本身由 wake_coalesce_test 专测
        fp = self.home / to / "inbox" / f"{lid}.md"
        old = fp.stat().st_mtime - 1000
        os.utime(fp, (old, old))
        return lid

    def write_dotenv(self, text):
        (self.home / ".env").write_text(text, encoding="utf-8")

    def round_(self, seconds=2.5, env=None, stderr_path=None):
        err = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
        p = subprocess.Popen([sys.executable, PO, "postman"],
                             env=env if env is not None else env_base(self.home),
                             stdout=subprocess.DEVNULL, stderr=err)
        try:
            time.sleep(seconds)
        finally:
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            if stderr_path:
                err.close()

    def slack_requests(self):
        with self.srv.lock:
            return list(self.srv.requests)

    @staticmethod
    def stop(proc):
        proc.kill()
        proc.wait()

    def start_panel(self, env):
        port = free_port()
        proc = subprocess.Popen([sys.executable, PO, "panel", "--no-open", "--port", str(port)],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/state")
                r = c.getresponse()
                r.read()
                c.close()
                if r.status == 200:
                    return proc, port
            except OSError:
                pass
            time.sleep(0.25)
        proc.kill()
        self.fail("面板 15 秒内没起来")

    def jpost(self, port, path, payload, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        c.request("POST", path, json.dumps(payload), headers)
        r = c.getresponse()
        body = json.loads(r.read().decode("utf-8"))
        c.close()
        return r.status, body

    def jget(self, port, path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    # -- 行为用例 ---------------------------------------------------------
    def test_dotenv_webhook_reaches_fake_slack(self):
        self.write_dotenv(f"POSTOFFICE_SLACK_WEBHOOK={self.webhook_url}\n")
        lid = self.send("chief", "coder", "env 信箱告警")
        self.round_()                    # env_base 里没有 webhook；只有 .env 有
        reqs = self.slack_requests()
        self.assertEqual(len(reqs), 1, ".env 里的 webhook 必须被邮递员读到并发请求")
        text = json.loads(reqs[0]["body"].decode("utf-8"))["text"]
        self.assertIn("coder 有新信", text)
        self.assertIn("env 信箱告警", text)
        self.assertIn(str(self.home / "chief" / "inbox" / f"{lid}.md"),
                      (self.home / ".delivered.json").read_text(encoding="utf-8"),
                      "正面对照：信照常投递入账")

    def test_dotenv_base_url_trusts_exact_origin(self):
        self.write_dotenv("POSTOFFICE_PANEL_BASE_URL=https://example.ts.net\n")
        proc, port = self.start_panel(env_base(self.home))
        self.addCleanup(self.stop, proc)
        st, _ = self.jpost(port, "/api/status", {"name": "coder", "status": "online"},
                           origin="https://example.ts.net")
        self.assertEqual(st, 200, ".env 的 BASE_URL 必须是 trusted exact Origin")
        st2, _ = self.jpost(port, "/api/status", {"name": "coder", "status": "online"},
                            origin="https://evil-ts.net")
        self.assertEqual(st2, 403, "不在 trusted 列表的 Origin 照旧 403")

    def test_process_env_base_url_beats_dotenv(self):
        self.write_dotenv("POSTOFFICE_PANEL_BASE_URL=https://file.example.ts.net\n")
        env = env_base(self.home, POSTOFFICE_PANEL_BASE_URL="https://proc.example.ts.net")
        proc, port = self.start_panel(env)
        self.addCleanup(self.stop, proc)
        st, _ = self.jpost(port, "/api/status", {"name": "coder", "status": "online"},
                           origin="https://proc.example.ts.net")
        self.assertEqual(st, 200, "process env 的 BASE_URL 生效")
        st2, _ = self.jpost(port, "/api/status", {"name": "coder", "status": "online"},
                            origin="https://file.example.ts.net")
        self.assertEqual(st2, 403, ".env 的同名值不得覆盖 process env")

    def test_launchd_style_process_reads_only_dotenv(self):
        """子进程环境只有 POSTOFFICE_HOME + PATH（模拟 launchd）；webhook、base URL、
        NO_NOTIFY、POLL 全部只放 .env —— 必须照样出现 FakeSlack 请求和正确深链。"""
        self.write_dotenv(f"# launchd 风格\nPOSTOFFICE_SLACK_WEBHOOK={self.webhook_url}\n"
                          "POSTOFFICE_PANEL_BASE_URL=https://machine.example.ts.net\n"
                          "POSTOFFICE_NO_NOTIFY=1\nPOSTOFFICE_POLL=1\n"
                          "TOTALLY-BROKEN-LINE\n")
        lid = self.send("chief", "coder", "launchd 自证")
        stderr_path = self.home / "postman_stderr.log"
        launchd_env = {"POSTOFFICE_HOME": str(self.home), "PATH": os.environ.get("PATH", "")}
        self.round_(seconds=3.0, env=launchd_env, stderr_path=stderr_path)
        reqs = self.slack_requests()
        self.assertEqual(len(reqs), 1, "launchd 风格（无 export）也必须读到 .env 的 webhook")
        text = json.loads(reqs[0]["body"].decode("utf-8"))["text"]
        self.assertIn(f"https://machine.example.ts.net#/mail/chief/{lid}", text,
                      "深链必须用 .env 里的 BASE_URL")
        delivered = (self.home / ".delivered.json").read_text(encoding="utf-8")
        self.assertIn(lid, delivered, "信照常投递入账")
        self.assertTrue((self.home / "logs" / "notify.log").exists(),
                        ".env 的 NO_NOTIFY 生效（通知落 notify.log，不弹系统通知）")
        err = stderr_path.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("TOTALLY-BROKEN-LINE", err, "诊断不许回显坏行内容")
        self.assertNotIn(SECRET, err, "secret 不许进 stderr")

    def test_secret_from_dotenv_never_leaks(self):
        dead = free_port()   # 没人监听：成功路径 + 失败日志路径一次覆盖
        self.write_dotenv(f"POSTOFFICE_SLACK_WEBHOOK=http://127.0.0.1:{dead}/hooks/{SECRET}\n")
        self.send("chief", "coder", "失败日志卫生")
        self.round_(seconds=3.0)
        proc, port = self.start_panel(env_base(self.home))
        self.addCleanup(self.stop, proc)
        _, state = self.jget(port, "/api/state")
        _, html = self.jget(port, "/")
        for label, blob in (("/api/state", state), ("HTML", html)):
            self.assertNotIn(SECRET.encode(), blob, f"secret 不许出现在 {label}")
        for f in (self.home / "logs").glob("*"):
            self.assertNotIn(SECRET, f.read_text(encoding="utf-8", errors="replace"),
                             f"secret 不许出现在 {f.name}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
