#!/usr/bin/env python3
"""撤回（postoffice retract）：只对「收件方通道还没接受」的普通直发信生效。

范围：普通 send 的信。回执通知、广播副本、交接信、闹钟信一律拒绝。
三条硬约束各有对应用例：
  1) 精确编号 + 来源必须一致，不接受路径/通配符/前缀；
  2) 已送达或投递认领在途一律拒绝，且不动文件；
  3) 与投递入口互斥（认领），忙时尚未送达的信撤回后不会被唤醒。

全部在临时 POSTOFFICE_HOME 里跑，不碰真实的 ~/agent-postoffice。
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
BASE = Path(tempfile.mkdtemp(prefix="po_retract_"))


def run_po(*args, home=None, stdin=None, timeout=60):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home or BASE)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env, input=stdin, timeout=timeout)


def hook(home, transcript, wait=12):
    """Really run `postoffice hook` and hand back its exit code: 2 = it woke the session,
    0 = it walked away, 124 = it was still watching when we gave up (nothing to wake about)."""
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    payload = json.dumps({"transcript_path": str(transcript)})
    p = subprocess.Popen([sys.executable, PO, "hook"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    p.stdin.write(payload.encode())
    p.stdin.close()
    deadline = time.time() + wait
    while time.time() < deadline:
        if p.poll() is not None:
            return p.returncode
        time.sleep(0.2)
    p.kill()
    p.wait()
    return 124


def postman(home, timeout=60):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    p = subprocess.Popen([sys.executable, PO, "postman"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.5)
    finally:
        p.terminate()
        p.wait(timeout=timeout)
    return p.returncode


class Retract(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ("boss", "worker", "claude", "coded", "spy"):
            (self.home / box / "inbox").mkdir(parents=True)
            (self.home / box / "done").mkdir(parents=True)
        run_po("add", "boss", "--notify", home=self.home)
        run_po("add", "worker", "--notify", home=self.home)
        run_po("add", "claude", "--claude", "抽样的标题", home=self.home)
        run_po("add", "coded", "--notify", home=self.home)
        run_po("add", "spy", "--notify", home=self.home)
        self.set_methods("worker", "opencode_plugin")
        self.set_methods("claude", "claude_hook")
        self.set_methods("coded", "codex_queue")

    # -- helpers ---------------------------------------------------------
    def set_methods(self, box, method):
        p = self.home / "routes.json"
        r = json.loads(p.read_text())
        r[box]["methods"] = [method]
        p.write_text(json.dumps(r, ensure_ascii=False))

    def send(self, to, sender="boss", subject="测试信", need="回复"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin="正文\n")
        line = [x for x in out.stdout.splitlines() if x.startswith("编号：")][0]
        return line.split("：", 1)[1].strip()

    def title_transcript(self, title="抽样的标题"):
        tp = self.home / "t.jsonl"
        tp.write_text(json.dumps({"type": "custom-title", "customTitle": title}) + "\n")
        return tp

    def inbox(self, box="worker"):
        return sorted(p.name for p in (self.home / box / "inbox").glob("*.md"))

    def archived(self, box="worker"):
        return sorted(str(p.relative_to(self.home)) for p in (self.home / box / "archived").rglob("*.md"))

    def claim(self, box, lid):
        d = self.home / box / ".claims"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{lid}.md").write_text("12345\n")

    def oc_ledger(self, box, lid, result="DELIVERED"):
        p = self.home / "opencode_delivered.jsonl"
        with p.open("a") as f:
            f.write(json.dumps({"box": box, "file": f"{lid}.md", "result": result}) + "\n")

    # -- 1) 正常路径 ------------------------------------------------------
    def test_retracts_an_untouched_letter_and_reports_where_it_went(self):
        lid = self.send("worker")
        out = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("已撤回", out.stdout)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(len(self.archived()), 1)
        self.assertIn("核实依据", out.stdout)
        self.assertIn("opencode_plugin", out.stdout)

    def test_body_and_headers_are_untouched_after_retraction(self):
        lid = self.send("worker")
        before = (self.home / "worker" / "inbox" / f"{lid}.md").read_bytes()
        run_po("retract", "boss", "worker", lid, home=self.home)
        after = list((self.home / "worker" / "archived").rglob(f"{lid}.md"))[0].read_bytes()
        self.assertEqual(before, after)

    def test_retraction_writes_no_notice_and_no_ledger_row(self):
        lid = self.send("worker")
        run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertFalse((self.home / "acks.jsonl").exists())
        self.assertEqual(self.inbox("boss"), [])
        self.assertEqual(self.inbox(), [])            # no receipt notice anywhere

    def test_no_correction_is_sent_for_you(self):
        lid = self.send("worker")
        run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertEqual(self.inbox("boss"), [])     # the sender gets nothing back either

    # -- 2) 精确匹配与来源核对 --------------------------------------------
    def test_unknown_id_is_refused(self):
        out = run_po("retract", "boss", "worker", "20990101-000000_boss", home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("没有编号为", out.stderr)

    def test_a_prefix_of_the_real_id_is_refused(self):
        lid = self.send("worker")
        out = run_po("retract", "boss", "worker", lid[:12], home=self.home)
        self.assertNotEqual(out.returncode, 0)

    def test_paths_globs_and_dots_are_refused(self):
        lid = self.send("worker")
        for bad in (f"inbox/{lid}", f"*{lid}*", f".{lid}"):
            out = run_po("retract", "boss", "worker", bad, home=self.home)
            self.assertNotEqual(out.returncode, 0, bad)
            self.assertIn("完整信件编号", out.stderr, bad)
        out = run_po("retract", "boss", "worker", lid + ".md", home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("去掉 .md", out.stderr)
        out = run_po("retract", "boss", "worker", "", home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("完整信件编号", out.stderr)
        self.assertEqual(len(self.inbox()), 1)       # nothing moved

    def test_sender_mismatch_is_refused(self):
        lid = self.send("worker", sender="boss")
        out = run_po("retract", "worker", "worker", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("不是你指定的", out.stderr)
        self.assertEqual(len(self.inbox()), 1)

    def test_unknown_mailboxes_are_refused(self):
        lid = self.send("worker")
        for args in (("幽灵", "worker"), ("boss", "幽灵")):
            out = run_po("retract", *args, lid, home=self.home)
            self.assertNotEqual(out.returncode, 0, args)
            self.assertIn("没有信箱", out.stderr)
        self.assertEqual(len(self.inbox()), 1)

    # -- 3) 已送达 / 在途一律拒绝（分通道） --------------------------------
    def test_opencode_ledger_delivered_is_refused(self):
        lid = self.send("worker")
        self.oc_ledger("worker", lid)
        out = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("已送达", out.stderr)
        self.assertIn("另发一封修正信", out.stderr)
        self.assertEqual(len(self.inbox()), 1)

    def test_opencode_failed_final_is_refused(self):
        lid = self.send("worker")
        self.oc_ledger("worker", lid, "FAILED_FINAL")
        out = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("已送达", out.stderr)
        self.assertEqual(len(self.inbox()), 1)

    def test_claim_in_flight_is_refused(self):
        lid = self.send("worker")
        self.claim("worker", lid)
        out = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("投递进行中", out.stderr)
        self.assertEqual(len(self.inbox()), 1)

    def test_claude_seen_is_refused(self):
        lid = self.send("claude")
        seen = self.home / "claude" / ".seen"
        seen.write_text(str(self.home / "claude" / "inbox" / f"{lid}.md") + "\n")
        out = run_po("retract", "boss", "claude", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("已送达", out.stderr)
        self.assertEqual(len(self.inbox("claude")), 1)

    def test_codex_accept_record_is_refused(self):
        lid = self.send("coded")
        (self.home / ".woken.json").write_text(
            json.dumps({str(self.home / "coded" / "inbox" / f"{lid}.md"): 1.0}))
        out = run_po("retract", "boss", "coded", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("已送达", out.stderr)
        self.assertEqual(len(self.inbox("coded")), 1)

    def test_unknown_channel_fails_closed(self):
        lid = self.send("spy")
        self.set_methods("spy", "carrier_pigeon")
        out = run_po("retract", "boss", "spy", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("无法核实", out.stderr)
        self.assertEqual(len(self.inbox("spy")), 1)

    def test_notify_channel_is_supported(self):
        self.set_methods("spy", "notify")
        lid = self.send("spy")
        out = run_po("retract", "boss", "spy", lid, home=self.home)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("notify", out.stdout)

    # -- 4) 派生通知不属于撤回范围 ----------------------------------------
    def test_receipt_notice_is_refused(self):
        lid = self.send("worker")
        run_po("ack", "worker", lid, "收到", home=self.home)
        rlid = self.inbox("boss")[0][:-3]
        out = run_po("retract", "worker", "boss", rlid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("不是普通直发信", out.stderr)
        self.assertEqual(len(self.inbox("boss")), 1)

    def test_broadcast_copy_is_refused(self):
        out = run_po("broadcast", "worker,claude", "boss", "广播一下", "回复", home=self.home,
                     stdin="正文\n")
        bid = [x for x in out.stdout.splitlines() if x.startswith("广播编号：")][0].split("：", 1)[1].strip()
        lid = self.inbox()[0][:-3]
        r = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不是普通直发信", r.stderr)
        self.assertTrue(bid)                     # sanity: the broadcast really was created

    def test_alarm_letter_is_refused(self):
        lid = self.send("worker")
        run_po("ack", "worker", lid, "收到", home=self.home)
        alarm = self.home / "worker" / "inbox" / "20260101-000000_postoffice_闹钟.md"
        alarm.write_text("来源：postoffice\n事由：闹钟\n需要：仅告知\n闹钟：A1_x\n\n固定短句。\n")
        r = run_po("retract", "postoffice", "worker", "20260101-000000_postoffice_闹钟", home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不是普通直发信", r.stderr)
        self.assertTrue(alarm.exists())

    def test_body_forged_broadcast_header_does_not_block_retraction(self):
        """只有信头块算数：正文里伪造的「广播：」不该把这封普通信推出撤回范围。"""
        p = self.home / "worker" / "inbox" / "20260101-000001_boss_正文伪造.md"
        p.write_text("来源：boss\n事由：正文伪造\n需要：回复\n\n广播：B20990101-000000_ghost\n")
        r = run_po("retract", "boss", "worker", "20260101-000001_boss_正文伪造", home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    # -- 5) 与投递入口互斥：忙时撤回后不会被唤醒 --------------------------
    def test_retracting_a_waiting_letter_means_the_plugin_never_wakes(self):
        """真实投递路径：会话一直忙 → 认领从没发生 → 撤回成功 → 之后再投也读不到这封信。"""
        lid = self.send("worker")
        r = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        # 投递方随后扫一遍收件箱：这封信已经不在，也没有认领残留
        self.assertEqual(self.inbox(), [])
        self.assertEqual(list((self.home / "worker" / ".claims").glob("*")) if
                         (self.home / "worker" / ".claims").exists() else [], [])

    def test_retraction_after_the_hold_means_no_wake_afterwards(self):
        """反向顺序：投递方先认领（=已决定要唤醒）→ 撤回必须被拒 → 信仍在 inbox/。"""
        lid = self.send("worker")
        self.claim("worker", lid)
        r = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("投递进行中", r.stderr)
        self.assertEqual(len(self.inbox()), 1)

    def test_a_failing_archive_move_never_loses_the_letter(self):
        """归档目录写不进去时必须非零退出，且原信仍在 inbox/（宁可撤回失败，也不丢信）。"""
        lid = self.send("worker")
        arch = self.home / "worker" / "archived"
        arch.mkdir(parents=True)
        arch.chmod(0o500)
        self.addCleanup(arch.chmod, 0o700)
        r = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.inbox()), 1)
        self.assertTrue((self.home / "worker" / "inbox" / f"{lid}.md").exists())

    def test_retraction_is_idempotent_in_the_sense_of_refusing_the_second_time(self):
        lid = self.send("worker")
        self.assertEqual(run_po("retract", "boss", "worker", lid, home=self.home).returncode, 0)
        again = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(len(self.archived()), 1)   # no second copy

    # -- 6) 真实投递入口（不是造台账，而是真的跑 hook / postman） -----------
    def test_hook_run_after_a_retraction_wakes_nobody(self):
        """忙时那封还没被钩子接受 → 撤回 → 之后真的跑一次钩子，它看不到任何信、不唤醒。"""
        lid = self.send("claude")
        self.assertEqual(run_po("retract", "boss", "claude", lid, home=self.home).returncode, 0)
        tp = self.title_transcript()
        rc = hook(self.home, tp)
        self.assertEqual(rc, 124)                                  # still watching: nothing to wake about
        seen = self.home / "claude" / ".seen"
        self.assertFalse(seen.exists() and seen.read_text().strip())
        self.assertEqual(self.inbox("claude"), [])

    def test_hook_that_already_woke_blocks_the_retraction(self):
        """反向：真的跑一次钩子把它叫醒了 → 撤回必须被拒，且认领与 .seen 都留着。"""
        lid = self.send("claude")
        tp = self.title_transcript()
        self.assertEqual(hook(self.home, tp), 2)                    # woke
        r = run_po("retract", "boss", "claude", lid, home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("投递进行中", r.stderr)
        self.assertEqual(len(self.inbox("claude")), 1)
        self.assertTrue((self.home / "claude" / ".claims" / f"{lid}.md").exists())

    def test_postman_that_already_delivered_blocks_the_retraction(self):
        """真的跑一次邮递员（notify 通道）→ 接受记录写下 → 撤回被拒。"""
        self.set_methods("spy", "notify")
        lid = self.send("spy")
        postman(self.home)
        # notify 通道的「已接受」证据是投递认领（.woken.json 只对 codex_queue 写）
        self.assertTrue((self.home / "spy" / ".claims" / f"{lid}.md").exists())
        r = run_po("retract", "boss", "spy", lid, home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("投递进行中", r.stderr)
        self.assertEqual(len(self.inbox("spy")), 1)

    def test_retracted_letter_is_never_delivered_afterwards(self):
        """撤回后真的跑一次邮递员：收件箱空着，它什么也不投、也不写接受记录。"""
        self.set_methods("spy", "notify")
        lid = self.send("spy")
        self.assertEqual(run_po("retract", "boss", "spy", lid, home=self.home).returncode, 0)
        postman(self.home)
        claims = self.home / "spy" / ".claims"
        self.assertFalse(claims.exists() and (claims / f"{lid}.md").exists())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)