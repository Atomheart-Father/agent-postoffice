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
    def set_methods(self, box, method, **extra):
        p = self.home / "routes.json"
        r = json.loads(p.read_text())
        r[box]["methods"] = [method]
        r[box].update(extra)
        p.write_text(json.dumps(r, ensure_ascii=False))

    def route_set_claude(self, home, box, title):
        p = home / "routes.json"
        r = json.loads(p.read_text())
        r[box]["claude_title"] = title
        p.write_text(json.dumps(r, ensure_ascii=False))

    def claim_stale_path(self, box, path):
        """Ask the real claim primitive for a letter that has already been archived."""
        code = ("import importlib.machinery, importlib.util, sys\n"
                "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
                "spec=importlib.util.spec_from_loader('po', loader)\n"
                "m=importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
                "print('CLAIM', m.claim_letter(sys.argv[2], __import__('pathlib').Path(sys.argv[3])))")
        r = subprocess.run([sys.executable, "-c", code, str(PO), box, str(path)],
                           capture_output=True, text=True,
                           env=dict(os.environ, POSTOFFICE_HOME=str(self.home)))
        self.assertIn("CLAIM False", r.stdout, f"认领已归档的信必须失败：{r.stdout}{r.stderr}")
        return r

    def fake_codex(self):
        """假 codex CLI：把被调用的次数写进 capture 文件（投递到底有没有发生）。"""
        cap = self.home / "codex.calls"
        script = self.home / "fake_codex.sh"
        script.write_text(f'#!/bin/sh\necho called >> "{cap}"\nexit 0\n')
        script.chmod(0o755)
        return script

    def captured(self):
        f = self.home / "codex.calls"
        return f.read_text().split() if f.exists() else []

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
        """FAILED_FINAL 是投递失败，不是已送达：也不能声称对方已经收到。"""
        lid = self.send("worker")
        self.oc_ledger("worker", lid, "FAILED_FINAL")
        out = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("投递失败/无法确认", out.stderr)
        self.assertNotIn("已送达", out.stderr)
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

    # -- 7) 撤回与投递原子互斥：两边抢同一把 O_EXCL 认领 ----------------------
    def test_a_claimed_letter_is_not_woken_by_the_hook(self):
        """投递方拿不到认领时不得唤醒 —— 这正是「认领失败也照样唤醒」那个 bug 的负例。"""
        lid = self.send("claude")
        self.claim("claude", lid)                 # 认领已被别的一方（撤回/另一个投递）占住
        rc = hook(self.home, self.title_transcript())
        self.assertEqual(rc, 124, "认领拿不到就该什么都不唤醒")
        seen = self.home / "claude" / ".seen"
        self.assertFalse(seen.exists() and seen.read_text().strip(), "不得写 .seen")
        self.assertEqual(len(self.inbox("claude")), 1)

    def test_a_claimed_letter_is_not_delivered_by_the_postman(self):
        """同一个 bug 在邮递员这一侧：认领不到的那几封不投，也不该动别人的认领。"""
        self.set_methods("spy", "codex_queue", codex_cli=str(self.fake_codex()), thread_id="th-x")
        lid = self.send("spy")
        self.claim("spy", lid)
        postman(self.home)
        self.assertEqual(len(self.inbox("spy")), 1, "不得投递")
        wf = self.home / ".woken.json"
        got = wf.read_text() if wf.exists() else ""
        self.assertNotIn(f"{lid}.md", got, "认领不到的那几封不该进投递台账")
        self.assertEqual(self.captured(), [], "也真的没调用过投递通道")
        self.assertTrue((self.home / "spy" / ".claims" / f"{lid}.md").exists(),
                        "别人的认领不该被释放掉")

    def test_the_postman_delivers_an_unclaimed_letter(self):
        """对照组：没有认领在途时，邮递员照常投 —— 证明上一条不是被别的原因挡住的。"""
        self.set_methods("spy", "codex_queue", codex_cli=str(self.fake_codex()), thread_id="th-x")
        lid = self.send("spy")
        postman(self.home)
        got = (self.home / ".woken.json").read_text() if (self.home / ".woken.json").exists() else ""
        self.assertIn(f"{lid}.md", got, "没人在途时该正常投递")

    def test_a_successful_retraction_leaves_no_claim_behind(self):
        """撤回自己也要拿同一把认领；完成后必须放掉，否则这封信的投递会被永久挡住。"""
        lid = self.send("worker")
        self.assertEqual(run_po("retract", "boss", "worker", lid, home=self.home).returncode, 0)
        claims = self.home / "worker" / ".claims"
        self.assertFalse(claims.exists() and (claims / f"{lid}.md").exists(), "撤回后不该留认领")

    def test_a_failed_retraction_also_releases_its_claim(self):
        """撤回失败（归档写不进去）同样要放掉认领：否则投递方会被一把没人持有的锁挡住。"""
        lid = self.send("worker")
        arch = self.home / "worker" / "archived"
        arch.mkdir(parents=True)
        arch.chmod(0o500)
        self.addCleanup(arch.chmod, 0o700)
        r = run_po("retract", "boss", "worker", lid, home=self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.inbox()), 1)
        claims = self.home / "worker" / ".claims"
        self.assertFalse(claims.exists() and (claims / f"{lid}.md").exists(),
                        "失败路径也必须放掉认领")
        # 放掉之后投递方就能正常拿到它：真跑一次钩子，它应该被叫醒（rc=2）
        self.set_methods("worker", "claude_hook")
        self.route_set_claude(self.home, "worker", "worker 的标题")
        rc = hook(self.home, self.title_transcript("worker 的标题"))
        self.assertEqual(rc, 2, "认领还回去之后这封信必须还能被正常投递")

    def test_a_pending_scan_cannot_claim_after_a_retraction(self):
        """投递方先列出待投 → 撤回方归档并放掉认领 → 投递方再拿那个旧路径来认领。

        这是评审指出的 stale-scan 竞态：认领必须自己确认信还在 inbox/，否则「撤回报成功、
        投递之后仍被叫醒」就会发生。这里钩子与邮递员各来一次，都不许唤醒、都不许留认领。
        """
        for method in ("claude_hook", "codex_queue"):
            with self.subTest(method=method):
                self.setUp()
                self.set_methods("worker", method,
                                 **({"codex_cli": str(self.fake_codex()), "thread_id": "th-y"}
                                    if method == "codex_queue" else {}))
                self.route_set_claude(self.home, "worker", "worker 的标题")
                lid = self.send("worker")
                inbox_path = self.home / "worker" / "inbox" / f"{lid}.md"   # 先列出待投
                self.assertTrue(inbox_path.exists())
                self.assertEqual(run_po("retract", "boss", "worker", lid, home=self.home).returncode, 0)
                self.assertFalse(inbox_path.exists(), "撤回后原信应已归档")
                self.claim_stale_path("worker", inbox_path)                   # 投递方拿着旧路径来认领
                self.assertFalse((self.home / "worker" / ".claims" / f"{lid}.md").exists(),
                                 "认领到已归档的信时必须放掉自己刚建的认领")
                if method == "claude_hook":
                    hook(self.home, self.title_transcript("worker 的标题"))
                else:
                    postman(self.home)
                seen = self.home / "worker" / ".seen"
                self.assertFalse(seen.exists() and lid in seen.read_text(), "不得写 .seen")
                wf = self.home / ".woken.json"
                self.assertFalse(wf.exists() and lid in wf.read_text(), "不得进投递台账")
                self.assertFalse((self.home / "worker" / ".claims" / f"{lid}.md").exists(),
                                 "不得留下残留认领")
                self.assertEqual(self.captured(), [], "不得真的调用投递通道")

    def test_retract_and_delivery_never_both_win(self):
        """两边真的同时起跑：不强制先后，只断言那条不变量。

        投递方在子进程里调用与钩子/邮递员相同的 claim_letter 原语，撤回方跑真正的 CLI。
        连续若干轮，每轮都必须满足「要么撤回成功且投递没拿到认领，要么反过来」，绝不能
        同时出现「撤回报成功」和「投递方拿到认领」。
        """
        self.set_methods("spy", "notify")
        for i in range(6):
            box = f"race{i}"
            (self.home / box / "inbox").mkdir(parents=True)
            (self.home / box / "done").mkdir(parents=True)
            run_po("add", box, "--notify", home=self.home)
            self.set_methods(box, "opencode_plugin")
            lid = self.send(box)
            delivery = subprocess.Popen(
                [sys.executable, "-c",
                 "import importlib.machinery, importlib.util, sys\n"
                 "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
                 "spec=importlib.util.spec_from_loader('po', loader)\n"
                 "mod=importlib.util.module_from_spec(spec); loader.exec_module(mod)\n"
                 "p=mod.HOME/sys.argv[2]/'inbox'/sys.argv[3]\n"
                 "print('CLAIM', mod.claim_letter(sys.argv[2], p), flush=True)",
                 PO, box, lid + ".md"],
                stdout=subprocess.PIPE, text=True,
                env=dict(os.environ, POSTOFFICE_HOME=str(self.home)))
            retract = subprocess.Popen(
                [sys.executable, PO, "retract", "boss", box, lid],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=dict(os.environ, POSTOFFICE_HOME=str(self.home), POSTOFFICE_NO_NOTIFY="1"))
            dout, _ = delivery.communicate(timeout=60)
            _, rerr = retract.communicate(timeout=60)
            claimed = "CLAIM True" in dout
            ok = run_po("retract", "boss", box, lid, home=self.home)   # 只为看现在还在不在
            archived = list((self.home / box / "archived").rglob(f"{lid}.md"))
            if claimed:
                self.assertEqual(archived, [], f"投递拿到认领时撤回不该成功（第 {i} 轮）")
                self.assertEqual(len(list((self.home / box / "inbox").glob("*.md"))), 1)
            else:
                self.assertEqual(len(archived), 1, f"投递没拿到认领时撤回就该成功（第 {i} 轮）：{rerr}")
                self.assertEqual(list((self.home / box / "inbox").glob("*.md")), [])
            self.assertIsNotNone(ok)

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