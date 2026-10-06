#!/usr/bin/env python3
"""层级路由（hierarchical escalation）回归锁：@alias 候选顺序、下线回退、恢复、切换公告去重。

这些契约在当前代码（v1.10 的 alias 引擎）上已经成立：candidates 就是有序候选数组，
alias_target 取第一个「已登记且在线」的候选；offline/online 只改变解析结果；已经写进
inbox 的信永不被搬走；切换广播/交接提醒由 process_alias_switches 的 alias_state 去重，
重启不重播。本文件把它们钉成回归测试——预期在当前树上直接通过；如果失败，那是引擎真 bug，
不许把断言改软。

约定：每个用例都在 mktemp 出来的临时 POSTOFFICE_HOME 里跑（绝不碰 ~/agent-postoffice），
全程 POSTOFFICE_NO_NOTIFY=1。邮递员每轮一个独立进程（POSTOFFICE_POLL=1、
POSTOFFICE_ALIAS_STABLE=1），睡 ~1.8s 后杀掉；等待有界，最多 8 轮。
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

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_hier_"))
ZI = "仅告知"

ALL_BOXES = ["a", "b", "q", "audit", "owner",
             "backend-gpt", "backend-claude",
             "frontend-gpt", "frontend-claude",
             "postoffice-gpt", "postoffice-claude",
             "m1", "m2"]

HIERARCHY_ALIASES = {
    "backend.q-supervisor": {
        "candidates": ["q", "backend-gpt", "backend-claude", "owner"],
        "notify": ["audit"],
        "handoff": "docs/boxz/backend.md",
    },
    "backend.supervisor": {"candidates": ["backend-gpt", "backend-claude", "owner"]},
    "frontend.supervisor": {"candidates": ["frontend-gpt", "frontend-claude", "owner"]},
    "postoffice.supervisor": {"candidates": ["postoffice-gpt", "postoffice-claude", "owner"]},
}


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    # 与运行环境的 Claude 桌面变量隔离，测试里没有人需要认身份
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    env.update(extra)
    return env


def run_po(*args, home, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home), input=stdin, timeout=timeout)


class HierarchyRouting(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ALL_BOXES:
            out = run_po("add", box, "--notify", "--who", f"{box} 的说明", home=self.home)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.write_config(HIERARCHY_ALIASES)

    # -- helpers ---------------------------------------------------------
    def write_config(self, aliases):
        (self.home / "config.json").write_text(
            json.dumps({"version": 1, "aliases": aliases}, ensure_ascii=False), encoding="utf-8")

    def send_to(self, alias, sender, subject, body="正文\n"):
        """发一封到 @alias，返回落盘路径（路径必须由发送方自己报告，别靠猜）。"""
        out = run_po("send", alias, sender, subject, ZI, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        line = next((l for l in out.stdout.splitlines() if l.startswith("已投递：")), None)
        self.assertIsNotNone(line, out.stdout)
        return Path(line.split("：", 1)[1].strip())

    def send_fails(self, alias, sender, subject, body="正文\n"):
        return run_po("send", alias, sender, subject, ZI, home=self.home, stdin=body)

    def target_of(self, path):
        return Path(path).parent.parent.name

    def offline(self, name):
        out = run_po("offline", name, home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def online(self, name):
        out = run_po("online", name, home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def inbox(self, box):
        return sorted(p.name for p in (self.home / box / "inbox").glob("*.md"))

    def done(self, box):
        return sorted(p.name for p in (self.home / box / "done").glob("*.md"))

    def all_letters(self):
        return {box: self.inbox(box) for box in ALL_BOXES}

    def header_field(self, path, key):
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        for line in text.split("\n\n", 1)[0].splitlines():
            if line.startswith(key):
                return line[len(key):].strip()
        return ""

    def letters_with(self, box, key):
        return [p for p in sorted((self.home / box / "inbox").glob("*.md"))
                if self.header_field(p, key)]

    def audit_broadcasts(self):
        return self.letters_with("audit", "广播：")

    def handoffs(self, box):
        return self.letters_with(box, "切换事件：")

    def alias_state(self):
        p = self.home / "alias_state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"aliases": {}}

    def alias_events(self, name):
        node = (self.alias_state().get("aliases") or {}).get(name) or {}
        return list((node.get("events") or {}).values())

    def alias_confirmed(self, name):
        return ((self.alias_state().get("aliases") or {}).get(name) or {}).get("confirmed")

    def switch_log_text(self):
        f = self.home / "logs" / "alias_switch.log"
        return f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""

    def round_(self, seconds=1.8, stable=1):
        """跑一轮邮递员：独立进程，POLL=1，稳定期 1 秒，到点杀掉。"""
        p = subprocess.Popen([sys.executable, PO, "postman"],
                             env=env_for(self.home, POSTOFFICE_ALIAS_STABLE=str(stable)),
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

    def wait_until(self, pred, what, max_rounds=8, seconds=1.8, stable=1):
        for i in range(max_rounds):
            self.round_(seconds=seconds, stable=stable)
            if pred():
                return i + 1
        self.fail(f"等了 {max_rounds} 轮仍未满足：{what}")

    # ==================================================================== 1
    def test_01_no_skip_all_five_letters_land_in_the_top_candidate(self):
        for i in range(5):
            sender = "a" if i % 2 == 0 else "b"
            path = self.send_to("@backend.q-supervisor", sender, f"高层信{i}")
            self.assertEqual(self.target_of(path), "q", f"第 {i} 封必须直达 q：{path}")
        self.assertEqual(len(self.inbox("q")), 5, "五封信必须全部落在 q/inbox")
        for box in ("backend-gpt", "backend-claude", "owner", "audit"):
            self.assertEqual(self.inbox(box), [], f"第一候选在线时 {box} 不许被跳过性投递")

    # ==================================================================== 2
    def test_02_fallback_chain_walks_down_and_fails_when_everyone_is_offline(self):
        self.offline("q")
        p = self.send_to("@backend.q-supervisor", "a", "一层回退")
        self.assertEqual(self.target_of(p), "backend-gpt")
        self.offline("backend-gpt")
        p = self.send_to("@backend.q-supervisor", "a", "二层回退")
        self.assertEqual(self.target_of(p), "backend-claude")
        self.offline("backend-claude")
        p = self.send_to("@backend.q-supervisor", "a", "三层回退")
        self.assertEqual(self.target_of(p), "owner")
        self.offline("owner")

        before = self.all_letters()
        out = self.send_fails("@backend.q-supervisor", "a", "全离线")
        self.assertNotEqual(out.returncode, 0, "全部候选离线时发送必须失败")
        self.assertIn("没有在线信箱", out.stderr, f"失败原因要说清没有在线信箱：{out.stderr!r}")
        self.assertEqual(self.all_letters(), before, "失败的发送不许在任何信箱留下信")

    # ==================================================================== 3
    def test_03_recovery_goes_back_to_the_top_candidate(self):
        self.offline("q")
        self.offline("backend-gpt")
        self.offline("backend-claude")
        p = self.send_to("@backend.q-supervisor", "a", "只有 owner 在线")
        self.assertEqual(self.target_of(p), "owner")
        self.online("q")
        p = self.send_to("@backend.q-supervisor", "a", "q 恢复后")
        self.assertEqual(self.target_of(p), "q", "q 一恢复，下一封就必须回到最高优先级候选")
        self.assertEqual(len(self.inbox("owner")), 1, "回退到 owner 的旧信不许被搬走")

    # ==================================================================== 4
    def test_04_q_supervisor_layer_never_defaults_to_owner_while_t1_is_online(self):
        p = self.send_to("@backend.supervisor", "a", "T1 全在线")
        self.assertEqual(self.target_of(p), "backend-gpt")
        self.offline("backend-gpt")
        p = self.send_to("@backend.supervisor", "a", "gpt 离线")
        self.assertEqual(self.target_of(p), "backend-claude")
        self.offline("backend-claude")
        p = self.send_to("@backend.supervisor", "a", "T1 全离线")
        self.assertEqual(self.target_of(p), "owner")
        self.online("backend-gpt")           # claude 仍离线
        p = self.send_to("@backend.supervisor", "a", "gpt 回来")
        self.assertEqual(self.target_of(p), "backend-gpt",
                         "T1 里只要有人在，就必须用它，不许默认落到 owner")
        self.assertEqual(len(self.inbox("owner")), 1, "owner 只该收到 T1 全离线的那一封")

    # ==================================================================== 5
    def test_05_frontend_and_postoffice_supervisors_never_skip_to_owner(self):
        owner_before = len(self.inbox("owner"))
        for alias, t1, t2 in [("@frontend.supervisor", "frontend-gpt", "frontend-claude"),
                              ("@postoffice.supervisor", "postoffice-gpt", "postoffice-claude")]:
            with self.subTest(alias=alias):
                self.offline(t2)                      # 只剩第一候选在线
                p = self.send_to(alias, "a", f"{alias} 第一候选")
                self.assertEqual(self.target_of(p), t1)
                self.offline(t1)
                self.online(t2)                       # 只剩第二候选在线
                p = self.send_to(alias, "a", f"{alias} 第二候选")
                self.assertEqual(self.target_of(p), t2, "还在线的 T1 候选必须接手，不许跳过 owner")
                self.offline(t2)                      # 全离线
                p = self.send_to(alias, "a", f"{alias} 全离线")
                self.assertEqual(self.target_of(p), "owner")
                self.online(t1)
                self.online(t2)
        self.assertEqual(len(self.inbox("owner")), owner_before + 2,
                         "owner 只该收到每个 alias 全离线的那一封")

    # ==================================================================== 6
    def test_06_old_mail_never_moves_when_targets_change(self):
        a = self.send_to("@backend.q-supervisor", "a", "旧信A")
        self.offline("q")
        b = self.send_to("@backend.q-supervisor", "b", "旧信B")
        self.offline("backend-gpt")
        c = self.send_to("@backend.q-supervisor", "a", "旧信C")
        self.offline("backend-claude")
        d = self.send_to("@backend.q-supervisor", "b", "旧信D")
        self.assertEqual([self.target_of(x) for x in (a, b, c, d)],
                         ["q", "backend-gpt", "backend-claude", "owner"])
        payloads = {x: Path(x).read_bytes() for x in (a, b, c, d)}

        self.online("q")
        self.online("backend-gpt")
        self.online("backend-claude")
        for _ in range(4):
            self.round_()

        for x, expected_box in zip((a, b, c, d),
                                   ("q", "backend-gpt", "backend-claude", "owner")):
            self.assertTrue(Path(x).exists(), f"{x} 必须还在原信箱")
            self.assertEqual(Path(x).read_bytes(), payloads[x], f"{x} 的字节不许被改动")
            self.assertEqual(self.target_of(x), expected_box)
            self.assertEqual(self.done(expected_box), [],
                             "邮递员不许把信挪到 done/，旧信原地不动")
        self.assertEqual(sum(len(self.inbox(box)) for box in ALL_BOXES), 4,
                         "切换目标不许触发任何搬信/重投")

    # ==================================================================== 7
    def test_07_switch_broadcast_and_handoff_are_announced_exactly_once_each(self):
        self.round_()                                        # 首轮只记基线
        self.assertEqual(self.audit_broadcasts(), [], "首轮只记基线，不发任何切换广播")

        self.offline("q")
        self.wait_until(lambda: len(self.audit_broadcasts()) >= 1, "q 下线后的切换广播")
        self.assertEqual(len(self.audit_broadcasts()), 1, "同一次切换只许一封广播给 audit")
        self.assertEqual(len(self.handoffs("backend-gpt")), 1, "新目标只许一封交接提醒")
        events = self.alias_events("backend.q-supervisor")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from"], "q")
        self.assertEqual(events[0]["to"], "backend-gpt")
        self.assertEqual(self.header_field(self.audit_broadcasts()[0], "广播："),
                         events[0]["broadcast"], "广播信要带这次事件自己的编号")
        self.assertIn("原目标下线", self.switch_log_text(), "切换原因要落进 alias_switch.log")

        for _ in range(2):                                   # 多跑几轮不重复
            self.round_()
        self.round_()                                        # 重启邮递员也不重复
        self.assertEqual(len(self.audit_broadcasts()), 1, "多轮/重启后广播仍只有一封")
        self.assertEqual(len(self.handoffs("backend-gpt")), 1, "多轮/重启后交接仍只有一封")

        self.online("q")                                     # 恢复同样只公告一次
        self.wait_until(lambda: len(self.audit_broadcasts()) >= 2, "q 恢复后的切换广播")
        self.assertEqual(len(self.audit_broadcasts()), 2, "总共恰好两封广播：下场一次、恢复一次")
        self.assertEqual(len(self.handoffs("q")), 1, "恢复后的新目标 q 只许一封交接提醒")
        self.assertEqual(len(self.handoffs("backend-gpt")), 1, "旧目标的交接不许重发")
        self.assertIn("高优先级候选恢复", self.switch_log_text())
        bids = [self.header_field(p, "广播：") for p in self.audit_broadcasts()]
        self.assertEqual(len(set(bids)), 2, "两次事件必须各有各的广播编号")
        events = self.alias_events("backend.q-supervisor")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["done"] for e in events), "两个事件都要走到完成")
        self.assertEqual([e["to"] for e in sorted(events, key=lambda e: e["seq"])],
                         ["backend-gpt", "q"])

    # ==================================================================== 8
    def test_08_all_offline_notice_lands_once_and_never_repeats(self):
        self.round_()                                        # 基线 = q
        for box in ("q", "backend-gpt", "backend-claude", "owner"):
            self.offline(box)
        self.wait_until(lambda: len(self.audit_broadcasts()) >= 1, "全员下线的公告")
        self.assertEqual(len(self.audit_broadcasts()), 1, "全离线只许公告一次")
        events = self.alias_events("backend.q-supervisor")
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["to"], "全员离线事件的目标必须是 none")
        self.assertEqual(events[0]["handoff"], "-", "没有接手人时交接必须是 '-'")
        letter = self.audit_broadcasts()[0].read_text(encoding="utf-8", errors="replace")
        self.assertIn("无在线候选", letter, "公告信要说清没有在线候选")
        self.assertIsNone(self.alias_confirmed("backend.q-supervisor"))

        for _ in range(2):
            self.round_()
        self.round_()                                        # 重启
        self.assertEqual(len(self.audit_broadcasts()), 1, "多轮/重启后全离线公告仍只有一封")
        self.assertEqual(len(self.alias_events("backend.q-supervisor")), 1)

    # ==================================================================== 9
    def test_09_old_two_candidate_array_form_still_resolves_and_switches(self):
        self.write_config({"mgr": ["m1", "m2"]})             # 旧式纯数组 alias（无 notify/handoff）
        p = self.send_to("@mgr", "a", "数组第一封")
        self.assertEqual(self.target_of(p), "m1")

        self.round_()                                        # 基线 = m1
        self.assertEqual(self.alias_confirmed("mgr"), "m1")
        self.offline("m1")
        self.wait_until(lambda: self.alias_confirmed("mgr") == "m2", "数组 alias 切换到 m2")
        p = self.send_to("@mgr", "a", "数组第二封")
        self.assertEqual(self.target_of(p), "m2", "m1 离线后旧式 alias 必须回退到 m2")

        self.online("m1")
        self.wait_until(lambda: self.alias_confirmed("mgr") == "m1", "数组 alias 恢复到 m1")
        p = self.send_to("@mgr", "a", "数组第三封")
        self.assertEqual(self.target_of(p), "m1", "m1 恢复后旧式 alias 必须回到 m1")

        events = sorted(self.alias_events("mgr"), key=lambda e: e["seq"])
        self.assertEqual(len(events), 2, "基线→切换→恢复恰好两个事件")
        self.assertTrue(all(e["done"] for e in events))
        self.assertEqual([e["to"] for e in events], ["m2", "m1"], "两个事件的去向要对")
        self.assertTrue(all(not e["broadcast"] for e in events),
                        "旧式 alias 没有 notify，就不该有广播编号")
        self.assertEqual(self.inbox("audit"), [], "没有配 notify 就不许给 audit 发信")
        for box in ALL_BOXES:
            self.assertEqual(self.letters_with(box, "广播："), [],
                             f"旧式 alias 不许在任何信箱留下广播信（{box}）")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
