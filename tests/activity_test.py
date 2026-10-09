#!/usr/bin/env python3
"""v1.13 runtime activity（会话活性，纯presentation）后端 seam（Red 先行）。

冻结契约：
  存储文件 <POSTOFFICE_HOME>/runtime/activity/<box>.json，键：
    state   "working"|"idle"
    since   float（epoch）
    observed float（epoch）
    source  "opencode_plugin"|"claude_hook"
    binding 字符串：该观测归属的 routes ident
  写/读：CLI `postoffice activity --state working|idle`，stdin 是 Claude 钩子负载（含 session_id，
    与 cmd_hook 读法一致），routes.json 里某信箱 methods 含 "claude_hook" 且 claude_session ==
    该 session id → 文件出现，source=="claude_hook"，binding==session id。
    身份解析不到 → 退出码 0、无文件、stdout 无错误输出（fail-soft，绝不打断钩子）。
    同状态再来一次（更晚）→ since 不变、observed 前进；状态改变 → since 重置为更晚的值。
  删掉整个 runtime/ 目录 → send + 一轮邮递员 + GET /api/state 照常（核心不受影响）。
  /api/state：每个信箱行新增 "activity": {"state":"working"|"idle"|"unknown","since":<number|null>}。
    规则：无文件 → unknown/since null；binding != 当前 routes ident → unknown；observed 早于
    1800 秒 → unknown；否则就是存的 state 与它的 since。
  不参与路由：给离线箱写一个 working 文件，不得改变 online、逻辑地址解析或任何投递结果。
  Claude 钩子集成：临时家里跑 `postoffice hook`（POSTOFFICE_POLL=1），watcher 空闲时 activity 是
    "idle"；被叫醒（退出 2，即来信）之后 activity 是 "working"。
    若真跑钩子太抖，可给该子测试标一个清楚的原因并保留其余。

全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，只连 127.0.0.1，不碰真实邮局。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_activity_"))
TITLE = "活性测试标题"
SESSION = "loc_activity_session"
SESSION2 = "rebind_session_2"
CLAUDE_BOX = "cbox"

ACTIVITY_KEYS = {"state", "since", "observed", "source", "binding"}


def env_for(home, **extra):
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


class Activity(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ("pbox", "aliasa", "aliasb"):
            out = run_po("add", box, "--notify", home=self.home)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        out = run_po("add", CLAUDE_BOX, "--claude", TITLE, home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        r = load_routes(self.home)
        r[CLAUDE_BOX]["methods"] = ["claude_hook"]
        r[CLAUDE_BOX]["claude_session"] = SESSION
        save_routes(self.home, r)
        (self.home / "config.json").write_text(json.dumps(
            {"version": 1, "groups": {}, "aliases": {"al": ["aliasa", "aliasb"]}},
            ensure_ascii=False), encoding="utf-8")
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, PO, "panel", "--no-open", "--port", str(self.port)],
            env=env_for(self.home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_panel)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("面板进程提前退出")
            try:
                if self.raw("GET", "/api/state")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("面板没有在 15 秒内起来")

    def stop_panel(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def kill_hook(self, p):
        """Cleanup for a hook subprocess: kill, reap, and close its pipes (no ResourceWarning)."""
        if p.poll() is None:
            p.kill()
        try:
            p.wait(timeout=10)
        except Exception:
            pass
        for stream in (p.stdin, p.stdout, p.stderr):
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass

    # -- helpers ----------------------------------------------------------
    def raw(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def state(self):
        status, data = self.raw("GET", "/api/state")
        self.assertEqual(status, 200)
        return json.loads(data.decode("utf-8"))

    def box_row(self, name):
        rows = [b for b in self.state()["boxes"] if b["name"] == name]
        self.assertEqual(len(rows), 1, f"找不到信箱行 {name}")
        return rows[0]

    def activity_path(self, box=CLAUDE_BOX):
        return self.home / "runtime" / "activity" / f"{box}.json"

    def read_activity(self, box=CLAUDE_BOX):
        p = self.activity_path(box)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def run_activity(self, state, session=SESSION, home=None):
        payload = json.dumps({"session_id": session, "transcript_path": "", "cwd": "/tmp"})
        return run_po("activity", "--state", state, home=home or self.home, stdin=payload)

    def write_activity(self, box=CLAUDE_BOX, *, state="working", since=None, observed=None,
                       source="claude_hook", binding=SESSION):
        d = self.home / "runtime" / "activity"
        d.mkdir(parents=True, exist_ok=True)
        now = time.time()
        (d / f"{box}.json").write_text(json.dumps({
            "state": state,
            "since": now - 100 if since is None else since,
            "observed": now if observed is None else observed,
            "source": source,
            "binding": binding,
        }), encoding="utf-8")

    def send(self, to=CLAUDE_BOX, sender="pbox", subject="活性信", need="回复", body="正文\n"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return next(l.split("：", 1)[1].strip() for l in out.stdout.splitlines()
                    if l.startswith("编号："))

    # ==================================================== CLI writer/reader
    def test_activity_cli_writes_file_for_resolved_claude_identity(self):
        out = self.run_activity("working")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        act = self.read_activity()
        self.assertIsNotNone(act, "解析得到身份时必须写出 runtime/activity/<box>.json")
        self.assertEqual(set(act), ACTIVITY_KEYS, "文件键集合被冻结")
        self.assertEqual(act["state"], "working")
        self.assertEqual(act["source"], "claude_hook")
        self.assertEqual(act["binding"], SESSION, "binding 必须是归属的 routes ident（session id）")
        self.assertIsInstance(act["since"], (int, float))
        self.assertIsInstance(act["observed"], (int, float))
        self.assertLessEqual(act["since"], act["observed"])

    def test_activity_cli_unresolvable_identity_is_fail_soft(self):
        out = self.run_activity("working", session="session-that-nobody-owns")
        self.assertEqual(out.returncode, 0, "身份解析不到必须退出码 0：\n" + out.stdout + out.stderr)
        self.assertEqual(out.stdout, "", "fail-soft 不许在 stdout 输出错误")
        self.assertFalse(self.activity_path().exists(), "认不出人就不许写文件")
        # 连 stdin 都没有也不许炸
        out2 = run_po("activity", "--state", "idle", home=self.home)
        self.assertEqual(out2.returncode, 0, out2.stdout + out2.stderr)
        self.assertFalse(self.activity_path().exists())

    def test_activity_same_state_keeps_since_and_advances_observed(self):
        self.assertEqual(self.run_activity("working").returncode, 0)
        a = self.read_activity()
        time.sleep(0.05)
        self.assertEqual(self.run_activity("working").returncode, 0)
        b = self.read_activity()
        self.assertEqual(b["state"], "working")
        self.assertEqual(b["since"], a["since"], "同状态再来一次：since 不许动")
        self.assertGreater(b["observed"], a["observed"], "observed 必须前进")

    def test_activity_state_change_resets_since(self):
        self.assertEqual(self.run_activity("working").returncode, 0)
        a = self.read_activity()
        time.sleep(0.05)
        self.assertEqual(self.run_activity("idle").returncode, 0)
        b = self.read_activity()
        self.assertEqual(b["state"], "idle")
        self.assertGreater(b["since"], a["since"], "状态改变：since 必须重置为更晚的值")

    def test_activity_binding_change_resets_since(self):
        old = time.time() - 100
        self.write_activity(state="working", since=old, observed=time.time(), binding="old-session")
        self.assertEqual(self.run_activity("working").returncode, 0)   # 本会话 = SESSION
        b = self.read_activity()
        self.assertEqual(b["binding"], SESSION)
        self.assertGreater(b["since"], old, "换了绑定：不许继承旧绑定的 since（否则时长会假）")

    # ========================================================= core survives
    def test_deleting_runtime_dir_does_not_break_core(self):
        self.write_activity()
        shutil.rmtree(self.home / "runtime", ignore_errors=True)
        out = run_po("send", "pbox", "cbox", "删 runtime 后的信", "回复",
                     home=self.home, stdin="核心照常。\n")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        p = subprocess.Popen([sys.executable, PO, "postman"], env=env_for(self.home),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2.5)
        finally:
            p.terminate()
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        self.assertEqual(self.raw("GET", "/api/state")[0], 200, "删 runtime 后面板照常")
        # 再跑一次 writer 也应当从头恢复，而不是报错
        self.assertEqual(self.run_activity("idle").returncode, 0)

    # ====================================================== /api/state rules
    def test_state_exposure_activity_rules(self):
        row = self.box_row(CLAUDE_BOX)
        self.assertIn("activity", row, "每个信箱行必须新增 activity")
        self.assertEqual(row["activity"], {"state": "unknown", "since": None},
                         "没有文件 → unknown / since null")

        since = time.time() - 100
        self.write_activity(state="working", since=since, observed=time.time(), binding=SESSION)
        a = self.box_row(CLAUDE_BOX)["activity"]
        self.assertEqual(a["state"], "working")
        self.assertEqual(a["since"], since, "有效文件要回它存的 state 与 since")

        self.write_activity(state="idle", since=since, observed=time.time(), binding="someone-else")
        self.assertEqual(self.box_row(CLAUDE_BOX)["activity"],
                         {"state": "unknown", "since": None}, "binding 对不上 → unknown")

        self.write_activity(state="working", since=since, observed=time.time() - 1801, binding=SESSION)
        self.assertEqual(self.box_row(CLAUDE_BOX)["activity"],
                         {"state": "unknown", "since": None}, "observed 早于 1800 秒 → unknown")

        self.write_activity(state="idle", since=since, observed=time.time(), binding=SESSION)
        self.assertEqual(self.box_row(CLAUDE_BOX)["activity"], {"state": "idle", "since": since})

    # ======================================================= not routing
    def test_activity_never_routes(self):
        self.assertEqual(run_po("offline", "aliasb", home=self.home).returncode, 0)
        self.write_activity(box="aliasb", state="working", source="claude_hook", binding=SESSION)
        row = self.box_row("aliasb")
        self.assertFalse(row["online"], "写 activity 不许把离线箱变在线")
        # 逻辑地址仍只按 routes 解析：第一候选在线 → 投到它，不管 activity
        out = run_po("send", "@al", "pbox", "活性不影响路由", "回复", home=self.home, stdin="x\n")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("解析 @al → aliasa", out.stdout, "逻辑地址只认 routes 在线状态")
        self.assertTrue(any((self.home / "aliasa" / "inbox").glob("*.md")), "信要投到第一候选")

    # ==================================================== Claude hook 集成
    def test_claude_hook_sets_activity_idle_then_working(self):
        t = self.home / "t.jsonl"
        t.write_text(json.dumps({"type": "custom-title", "customTitle": TITLE}) + "\n",
                     encoding="utf-8")
        p = subprocess.Popen([sys.executable, PO, "hook"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env=env_for(self.home), text=True)
        self.addCleanup(self.kill_hook, p)
        p.stdin.write(json.dumps({"transcript_path": str(t), "session_id": SESSION}))
        p.stdin.close()
        p.stdin = None
        alive = self.home / CLAUDE_BOX / ".watch_alive"
        deadline = time.time() + 15
        while time.time() < deadline and not alive.exists():
            self.assertIsNone(p.poll(), "钩子不该在上岗前退出")
            time.sleep(0.05)
        self.assertTrue(alive.exists(), "钩子没有上岗，夹具没搭起来")

        def wait_activity(want, timeout=15):
            end = time.time() + timeout
            last = None
            while time.time() < end:
                last = self.read_activity()
                if last and last.get("state") == want:
                    return last
                time.sleep(0.05)
            return last

        idle = wait_activity("idle", timeout=15)
        self.assertIsNotNone(idle, "watcher 空闲时 activity 文件必须存在且为 idle")
        self.assertEqual(idle.get("state"), "idle", f"空闲时应为 idle：{idle}")

        self.send(CLAUDE_BOX, sender="pbox", subject="叫醒信")
        deadline = time.time() + 20
        while time.time() < deadline and p.poll() is None:
            time.sleep(0.1)
        self.assertEqual(p.returncode, 2, f"来信必须把钩子叫醒：rc={p.returncode}")
        working = wait_activity("working", timeout=5)
        self.assertIsNotNone(working, "唤醒后必须写出 working")
        self.assertEqual(working.get("state"), "working", f"唤醒后应为 working：{working}")

    def test_live_watcher_heartbeat_never_overwrites_working(self):
        """存活 watcher 的心跳只保活：不得把 UserPromptSubmit 刚写的 working 按回 idle。"""
        t = self.home / "t.jsonl"
        t.write_text(json.dumps({"type": "custom-title", "customTitle": TITLE}) + "\n",
                     encoding="utf-8")
        p = subprocess.Popen([sys.executable, PO, "hook"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env=env_for(self.home), text=True)
        self.addCleanup(self.kill_hook, p)
        p.stdin.write(json.dumps({"transcript_path": str(t), "session_id": SESSION}))
        p.stdin.close()
        p.stdin = None
        alive = self.home / CLAUDE_BOX / ".watch_alive"
        deadline = time.time() + 15
        while time.time() < deadline and not alive.exists():
            self.assertIsNone(p.poll(), "钩子不该在上岗前退出")
            time.sleep(0.05)
        self.assertTrue(alive.exists(), "钩子没有上岗，夹具没搭起来")
        # 等 watcher 自己写出 idle（上岗即空闲）
        deadline = time.time() + 10
        while time.time() < deadline and (self.read_activity() or {}).get("state") != "idle":
            time.sleep(0.05)
        # UserPromptSubmit：watcher 仍存活时写 working
        self.assertEqual(self.run_activity("working").returncode, 0)
        self.assertEqual((self.read_activity() or {}).get("state"), "working")
        # 等过至少两个心跳周期（POSTOFFICE_POLL=1），working 必须还在
        time.sleep(2.5)
        self.assertIsNone(p.poll(), "夹具前提：watcher 此时应仍存活")
        self.assertEqual((self.read_activity() or {}).get("state"), "working",
                         "存活 watcher 的心跳不许把 working 覆盖成 idle")

    def test_live_watcher_rebind_never_publishes_the_old_identity(self):
        """watcher 存活时 routes 改绑新 session：旧 watcher 心跳不得污染新绑定的 activity。"""
        t = self.home / "t.jsonl"
        t.write_text(json.dumps({"type": "custom-title", "customTitle": TITLE}) + "\n",
                     encoding="utf-8")
        p = subprocess.Popen([sys.executable, PO, "hook"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env=env_for(self.home), text=True)
        self.addCleanup(self.kill_hook, p)
        p.stdin.write(json.dumps({"transcript_path": str(t), "session_id": SESSION}))
        p.stdin.close()
        p.stdin = None
        alive = self.home / CLAUDE_BOX / ".watch_alive"
        deadline = time.time() + 15
        while time.time() < deadline and not alive.exists():
            self.assertIsNone(p.poll(), "钩子不该在上岗前退出")
            time.sleep(0.05)
        self.assertTrue(alive.exists(), "钩子没有上岗，夹具没搭起来")
        deadline = time.time() + 10
        while time.time() < deadline and (self.read_activity() or {}).get("state") != "idle":
            time.sleep(0.05)
        # routes 改绑到新 session，新 session 写 working
        r = load_routes(self.home)
        r[CLAUDE_BOX]["claude_session"] = SESSION2
        save_routes(self.home, r)
        self.assertEqual(self.run_activity("working", session=SESSION2).returncode, 0)
        rec = self.read_activity()
        self.assertEqual(rec.get("binding"), SESSION2, "新 session 写入必须绑定新身份")
        self.assertEqual(rec.get("state"), "working")
        time.sleep(2.5)                     # 让旧 watcher 至少跑两个心跳周期（POSTOFFICE_POLL=1）
        self.assertIsNone(p.poll(), "夹具前提：watcher 此时应仍存活")
        rec = self.read_activity()
        self.assertEqual(rec.get("binding"), SESSION2, "旧 watcher 心跳不得把 binding 改回旧身份")
        self.assertEqual(rec.get("state"), "working", "旧 watcher 心跳不得把新绑定的 working 改回 idle")
        self.assertEqual(self.box_row(CLAUDE_BOX)["activity"], {"state": "working", "since": rec["since"]},
                         "面板不得因旧 watcher 心跳变 UNKNOWN")

    def test_live_watcher_rebind_wake_branch_never_publishes_old_binding(self):
        """旧 watcher 存活、改绑新会话后再收到普通信：退出 2 的 wake 分支也不得把 binding 写回旧身份。"""
        t = self.home / "t.jsonl"
        t.write_text(json.dumps({"type": "custom-title", "customTitle": TITLE}) + "\n",
                     encoding="utf-8")
        p = subprocess.Popen([sys.executable, PO, "hook"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env=env_for(self.home), text=True)
        self.addCleanup(self.kill_hook, p)
        p.stdin.write(json.dumps({"transcript_path": str(t), "session_id": SESSION}))
        p.stdin.close()
        p.stdin = None
        alive = self.home / CLAUDE_BOX / ".watch_alive"
        deadline = time.time() + 15
        while time.time() < deadline and not alive.exists():
            self.assertIsNone(p.poll(), "钩子不该在上岗前退出")
            time.sleep(0.05)
        self.assertTrue(alive.exists(), "钩子没有上岗，夹具没搭起来")
        deadline = time.time() + 10
        while time.time() < deadline and (self.read_activity() or {}).get("state") != "idle":
            time.sleep(0.05)
        # 改绑新 session 并由新 session 写 working
        r = load_routes(self.home)
        r[CLAUDE_BOX]["claude_session"] = SESSION2
        save_routes(self.home, r)
        self.assertEqual(self.run_activity("working", session=SESSION2).returncode, 0)
        # 给信箱一封普通信 → 旧 watcher 退出 2（wake 分支）
        self.send(CLAUDE_BOX, sender="pbox", subject="改绑后的普通信")
        deadline = time.time() + 20
        while time.time() < deadline and p.poll() is None:
            time.sleep(0.1)
        self.assertEqual(p.returncode, 2, f"旧 watcher 应被叫醒：rc={p.returncode}")
        time.sleep(0.3)                       # 让任何（错误的）wake 写落定
        rec = self.read_activity()
        self.assertEqual(rec.get("binding"), SESSION2, "wake 分支不得把 binding 写回旧身份")
        self.assertNotEqual(self.box_row(CLAUDE_BOX)["activity"]["state"], "unknown",
                            "面板不得因旧 watcher 的 wake 变 UNKNOWN")


def load_routes(home):
    return json.loads((home / "routes.json").read_text(encoding="utf-8"))


def save_routes(home, routes):
    (home / "routes.json").write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
