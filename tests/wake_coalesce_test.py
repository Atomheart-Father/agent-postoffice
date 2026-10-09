#!/usr/bin/env python3
"""wake-coalesce 票：回执不单独唤醒 + 新正式信静默窗合批 + WakePlan 单一选择器。

公共入口：postman 真实轮、`postoffice wake-plan` 只读 CLI、进程内纯函数 wake_plan。
时钟全部用 os.utime 控制，绝不真等 45s/150s。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
PO = os.path.join(ROOT, "postoffice")


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    env.update(extra)
    return env


def run_po(*args, home=None, stdin=None, timeout=60, **extra):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home or BASE, **extra), input=stdin, timeout=timeout)


def postman(home, seconds=2.5, timeout=60, **extra):
    p = subprocess.Popen([sys.executable, PO, "postman"], env=env_for(home, **extra),
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


def load_po(name="po_wake_mod"):
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(name, PO)
    spec = importlib.util.spec_from_file_location(name, PO, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def age(home, box, name, seconds_ago):
    p = Path(home) / box / "inbox" / name
    t = time.time() - seconds_ago
    os.utime(p, (t, t))


def wake_count(home, box):
    f = Path(home) / box / ".wake_times"
    return len(f.read_text().splitlines()) if f.exists() else 0


def delivered_keys(home):
    f = Path(home) / ".delivered.json"
    return set(json.loads(f.read_text())) if f.exists() else set()


def is_delivered(home, box, name):
    return str(Path(home) / box / "inbox" / name) in delivered_keys(home)


def postman_round(home, seconds=3.0, **extra):
    """单轮 postman（POLL 调大 → 只扫一次），便于精确数唤醒次数。seconds 要盖过解释器冷启动。"""
    return postman(home, seconds=seconds, POSTOFFICE_POLL="100", **extra)


def log_lines(home, name):
    f = Path(home) / "logs" / name
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


class WakeCoalesce(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="wake_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ("nb", "peer"):
            (self.home / box / "inbox").mkdir(parents=True)
            (self.home / box / "done").mkdir(parents=True)
        run_po("add", "nb", "--notify", home=self.home)
        run_po("add", "peer", "--notify", home=self.home)

    def tearDown(self):
        pass

    def send(self, to, sender="peer", subject="测试信", need="回复", body="正文\n"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [x for x in out.stdout.splitlines() if x.startswith("编号：")][0].split("：", 1)[1].strip()

    def receipt_into_nb(self, subject="需要回执的信"):
        """nb 发信给 peer，peer ack → 一封真实回执通知落进 nb/inbox。"""
        lid = self.send("peer", sender="nb", subject=subject)
        r = run_po("ack", "peer", lid, "copy that", home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        inbox = sorted((self.home / "nb" / "inbox").glob("*.md"))
        self.assertTrue(inbox, "回执通知应落进 nb/inbox")
        return inbox

    def alarm_letter(self, name="alarmtest.md"):
        p = self.home / "nb" / "inbox" / name
        p.write_text("闹钟：alarm-id-1\n来源：postoffice\n事由：闹钟\n需要：仅告知\n\n正文\n",
                     encoding="utf-8")
        return p

    # ---- 合同1：普通回执不单独唤醒，也不设长兜底 ----
    def test_receipt_alone_never_wakes_even_when_very_old(self):
        self.receipt_into_nb()
        for p in (self.home / "nb" / "inbox").glob("*.md"):
            age(self.home, "nb", p.name, 10_000)   # 远超旧的 RECEIPT_WAIT(600s)
        postman(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 0, "只回执→0 次唤醒")
        self.assertTrue(sorted((self.home / "nb" / "inbox").glob("*.md")),
                        "回执留 inbox：不投递、不丢失、不假记已展示")
        self.assertFalse(any("notify nb" in ln for ln in log_lines(self.home, "notify.log")),
                         "不得为回执叫醒")
        for p in (self.home / "nb" / "inbox").glob("*.md"):
            self.assertFalse(is_delivered(self.home, "nb", p.name), "回执不得假记已投递")

    def test_receipt_never_pages_the_human_after_grace(self):
        self.receipt_into_nb()
        for p in (self.home / "nb" / "inbox").glob("*.md"):
            age(self.home, "nb", p.name, 30)
        postman(self.home, seconds=3, POSTOFFICE_GRACE="2")
        self.assertFalse(any("20 分钟未被唤醒" in ln or "超 20 分钟" in ln
                             for ln in log_lines(self.home, "notify.log")),
                         "回执超 GRACE 也不得惊动人")

    def test_receipts_ride_along_with_a_departing_formal_letter(self):
        lid = self.send("nb", subject="正式信")
        self.receipt_into_nb(subject="无关回执")
        age(self.home, "nb", lid + ".md", 160)
        for p in (self.home / "nb" / "inbox").glob("*.md"):
            if p.name != lid + ".md":
                age(self.home, "nb", p.name, 10_000)
        postman_round(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 1, "正式信出发，回执搭车，一次唤醒")
        self.assertTrue(is_delivered(self.home, "nb", lid + ".md"))
        for p in (self.home / "nb" / "inbox").glob("*.md"):
            if p.name != lid + ".md":
                self.assertTrue(is_delivered(self.home, "nb", p.name), "回执搭车同轮记已投递")

    # ---- 合同2：新正式信静默窗合批（QUIET=45 / MAX_HOLD=150，可调） ----
    def test_young_formal_letter_holds_and_three_coalesce_into_one_wake(self):
        l1 = self.send("nb", subject="第一批")
        age(self.home, "nb", l1 + ".md", 0)
        postman_round(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 0, "最新一封静默不足 → 不出发")
        self.assertTrue((self.home / "nb" / "inbox" / (l1 + ".md")).exists())
        l2 = self.send("nb", subject="第二批")
        l3 = self.send("nb", subject="第三批")
        postman_round(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 0, "仍在静默窗内")
        age(self.home, "nb", l1 + ".md", 160)          # 最老已等 ≥ MAX_HOLD
        postman_round(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 1, "三封合批只叫醒一次")
        for x in (l1, l2, l3):
            self.assertTrue(is_delivered(self.home, "nb", x + ".md"), f"{x} 应同批投出")
        wakes = [ln for ln in log_lines(self.home, "postman.log") if "notify nb" in ln]
        self.assertEqual(len(wakes), 1)

    def test_quiet_window_honours_env_override(self):
        l1 = self.send("nb", subject="环境变量窗")
        age(self.home, "nb", l1 + ".md", 6)
        postman_round(self.home, POSTOFFICE_QUIET="5", POSTOFFICE_MAX_HOLD="9")
        self.assertEqual(wake_count(self.home, "nb"), 1, "QUIET/MAX_HOLD 是可调初始值")

    def test_max_hold_departs_even_when_new_letters_keep_arriving(self):
        l1 = self.send("nb", subject="老信")
        age(self.home, "nb", l1 + ".md", 160)
        l2 = self.send("nb", subject="新信")
        age(self.home, "nb", l2 + ".md", 10)
        postman_round(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 1, "最老已等 ≥ MAX_HOLD → 出发")
        self.assertTrue(is_delivered(self.home, "nb", l1 + ".md"))
        self.assertTrue(is_delivered(self.home, "nb", l2 + ".md"))

    def test_batch_cap_overflow_keeps_the_rest_for_next_round(self):
        for i in range(25):
            lid = self.send("nb", subject=f"信{i:02d}")
            age(self.home, "nb", lid + ".md", 160)
        postman_round(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 1)
        done = [p for p in (self.home / "nb" / "inbox").glob("*.md")
                if is_delivered(self.home, "nb", p.name)]
        self.assertEqual(len(done), 20, "一批至多 20 封")
        rest = [p for p in (self.home / "nb" / "inbox").glob("*.md")
                if not is_delivered(self.home, "nb", p.name)]
        self.assertEqual(len(rest), 5, "超批留余，不丢")

    # ---- 合同4：闹钟不受窗口拖延，但绝不与正式信同轮 ----
    def test_due_alarm_departs_alone_while_formals_hold(self):
        lid = self.send("nb", subject="年轻信")
        age(self.home, "nb", lid + ".md", 0)
        alarm = self.alarm_letter()
        age(self.home, "nb", alarm.name, 0)
        postman_round(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 1, "到期闹钟不受静默窗拖延")
        self.assertTrue(is_delivered(self.home, "nb", alarm.name), "闹钟单独出发")
        self.assertFalse(is_delivered(self.home, "nb", lid + ".md"), "正式信仍在窗内不搭闹钟轮")

    def test_alarm_never_rides_with_departing_formals(self):
        lid = self.send("nb", subject="到点信")
        age(self.home, "nb", lid + ".md", 160)
        alarm = self.alarm_letter()
        postman_round(self.home)
        self.assertEqual(wake_count(self.home, "nb"), 1)
        self.assertTrue(is_delivered(self.home, "nb", lid + ".md"))
        self.assertFalse(is_delivered(self.home, "nb", alarm.name), "闹钟不与正式信同轮")
        self.assertTrue(alarm.exists(), "闹钟留下轮独占")

    # ---- 第0步：信头分类只认信头块，正文伪造「闹钟」无效 ----
    def test_forged_alarm_in_body_is_formal(self):
        m = load_po()
        p = self.home / "nb" / "inbox" / "forged.md"
        p.write_text("来源：peer\n事由：普通信\n需要：回复\n\n闹钟：forged-id\n正文提到闹钟\n",
                     encoding="utf-8")
        self.assertEqual(m.letter_kind(p), "formal", "正文里的「闹钟：」不算闹钟信")


    # ---- 补3（T0 20261009）：岗位切换纯告知=notice，不单独唤醒、随工作信有界搭车 ----
    def letter_path(self, box, lid):
        return self.home / box / "inbox" / f"{lid}.md"

    def notice_letter(self, name="switch_notice.md", subject="逻辑地址 @dev 改由 w2 受理"):
        p = self.home / "nb" / "inbox" / name
        p.write_text(f"来源：postoffice\n事由：{subject}\n需要：仅告知\n切换事件：SW1\n广播：B1_sw1_dev\n"
                     "\n这封信只报告路由变化。\n", encoding="utf-8")
        return p

    def escalation_letter(self, name="escalation.md"):
        p = self.home / "nb" / "inbox" / name
        p.write_text("来源：postoffice\n事由：升级：@dev 执行岗位无人可接，请你处理\n需要：仅告知\n"
                     "切换事件：SW1\n升级求助：SW1\n\n这是升级求助，不是纯告知。\n", encoding="utf-8")
        return p

    def test_notice_classification_is_header_only(self):
        """分类只看信头：切换事件：→notice；升级求助：优先→formal；标题写「岗位/交接」的普通信仍是 formal。"""
        m = load_po()
        self.assertEqual(m.letter_kind(self.notice_letter()), "notice")
        self.assertEqual(m.letter_kind(self.escalation_letter()), "formal",
                         "升级求助是真正请行动的票，不得被压成 notice")
        p = self.send("nb", subject="交接：关于岗位的普通工作信")
        self.assertEqual(m.letter_kind(self.letter_path("nb", p)), "formal",
                         "标题含交接/岗位不构成分类依据（伪造负例）")

    def test_notice_alone_never_wakes_and_has_no_fallback_timer(self):
        p = self.notice_letter()
        age(self.home, "nb", p.name, 10_000)
        postman_round(self.home)
        self.assertFalse(is_delivered(self.home, "nb", p.name), "只有纯告知→0 次唤醒、不投递")
        self.assertTrue(p.exists(), "notice 留在 inbox")
        self.assertEqual(wake_count(self.home, "nb"), 0, "notice 不设超时兜底，不惊动人")

    def test_notice_rides_with_a_departing_formal_letter(self):
        p = self.notice_letter()
        age(self.home, "nb", p.name, 10_000)
        lid = self.send("nb", subject="正式工作信")
        postman_round(self.home, POSTOFFICE_QUIET="0", POSTOFFICE_MAX_HOLD="0")
        self.assertTrue(is_delivered(self.home, "nb", f"{lid}.md"), "正式信照常出发（不被 notice 压住）")
        self.assertTrue(is_delivered(self.home, "nb", p.name), "notice 有界搭车随同批送达")
        self.assertEqual(wake_count(self.home, "nb"), 1, "一批只唤醒一次")

    def test_escalation_handoff_is_a_real_action_letter(self):
        """③升级求助（切换事件：+升级求助：）保持 formal：单独即唤醒。"""
        p = self.escalation_letter()
        age(self.home, "nb", p.name, 10_000)
        postman_round(self.home, POSTOFFICE_QUIET="0", POSTOFFICE_MAX_HOLD="0")
        self.assertTrue(is_delivered(self.home, "nb", p.name), "升级求助不被 notice 规则压住")
        self.assertEqual(wake_count(self.home, "nb"), 1)

    def test_notice_never_pages_the_human(self):
        """GRACE 兜底只对 formal：纯告知躺在 inbox 也不弹 20 分钟提醒。"""
        p = self.notice_letter()
        age(self.home, "nb", p.name, 10_000)
        postman(self.home, seconds=3.0, POSTOFFICE_POLL="1", POSTOFFICE_GRACE="1")
        self.assertNotIn("20 分钟", log_lines(self.home, "postman.log"), "notice 不进人侧兜底")


    # ---- WakePlan：纯函数 + 只读 CLI，同一黄金表 ----
    GOLDEN = [
        ({"candidates": [
            {"id": "a.md", "kind": "formal", "mtime": 1000.0},
            {"id": "b.md", "kind": "formal", "mtime": 1030.0},
            {"id": "r.md", "kind": "receipt", "mtime": 900.0}],
            "now": 1046.0}, "quiet_hold"),
        ({"candidates": [
            {"id": "a.md", "kind": "formal", "mtime": 1000.0},
            {"id": "b.md", "kind": "formal", "mtime": 1030.0},
            {"id": "r.md", "kind": "receipt", "mtime": 900.0}],
            "now": 1076.0}, "quiet_elapsed"),
        ({"candidates": [{"id": "old.md", "kind": "formal", "mtime": 900.0},
                         {"id": "new.md", "kind": "formal", "mtime": 1090.0}],
            "now": 1060.0}, "max_hold"),
        ({"candidates": [{"id": "a.md", "kind": "formal", "mtime": 1090.0}],
            "now": 1091.0, "pending_exists": True}, "pending_merge"),
        ({"candidates": [{"id": "al.md", "kind": "alarm", "mtime": 1090.0},
                         {"id": "a.md", "kind": "formal", "mtime": 1090.0}],
            "now": 1091.0}, "alarm"),
        ({"candidates": [{"id": "r.md", "kind": "receipt", "mtime": 900.0}],
            "now": 1091.0}, "receipts_wait"),
        ({"candidates": [{"id": "n.md", "kind": "notice", "mtime": 900.0}],
            "now": 1091.0}, "notice_wait"),
        ({"candidates": [{"id": "a.md", "kind": "formal", "mtime": 900.0},
                         {"id": "n.md", "kind": "notice", "mtime": 950.0},
                         {"id": "r.md", "kind": "receipt", "mtime": 960.0}],
            "now": 1091.0}, "formal_depart_notice_receipts_ride"),
    ]

    def test_cli_matches_function_on_golden_table(self):
        m = load_po()
        for payload, tag in self.GOLDEN:
            fn = m.wake_plan(payload["candidates"], payload["now"],
                             pending_exists=bool(payload.get("pending_exists")))
            r = run_po("wake-plan", "nb", home=self.home, stdin=json.dumps(payload))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout), fn, f"黄金样例不一致：{tag}")

    def test_wake_plan_cli_is_read_only(self):
        before = sorted(str(p) for p in self.home.rglob("*"))
        r = run_po("wake-plan", "nb", home=self.home, stdin=json.dumps(
            {"now": time.time(),
             "candidates": [{"id": "a.md", "kind": "formal", "mtime": time.time()}]}))
        self.assertEqual(r.returncode, 0, r.stderr)
        after = sorted(str(p) for p in self.home.rglob("*"))
        self.assertEqual(before, after, "wake-plan 不得初始化/写入任何文件")

    def test_wake_plan_rejects_bad_shapes(self):
        r = run_po("wake-plan", "nb", home=self.home, stdin='{"now": 1, "candidates": "x"}')
        self.assertNotEqual(r.returncode, 0, "candidates 非数组必须拒绝")
        r = run_po("wake-plan", "nb", home=self.home, stdin='{"candidates": [{"id": "a"}]}')
        self.assertNotEqual(r.returncode, 0, "缺 kind/mtime 必须拒绝")
        r = run_po("wake-plan", "nb", home=self.home, stdin='{"now": 1, "candidates": []}')
        self.assertEqual(r.returncode, 0, "空候选集合法（无事可做也是一种决策）")

    def test_wake_plan_is_deterministic_across_restart(self):
        m = load_po()
        items = [{"id": "a.md", "kind": "formal", "mtime": 100.0}]
        self.assertEqual(m.wake_plan(items, 101), m.wake_plan(items, 101))


if __name__ == "__main__":
    unittest.main()
