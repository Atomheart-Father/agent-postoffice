#!/usr/bin/env python3
"""v1.11 gate：POST /api/ack-one 与 POST /api/send 两个后端 seam（本票无 UI）。

冻结契约（必须与 CLI 共用同一核心，不许分叉）：
  /api/ack-one {"box","id","note"?}
    - 与 CLI `ack` 同一核心：回执入账（acks.jsonl）、原信 inbox→done、发件人收到既有回执通知。
    - 重复调用幂等：state=already，不产生第二份回执、不产生第二份通知。
    - 把「回执通知」再 ack：state=receipt_notice，只归档通知、不再产生回执。
    - 未知信箱→404；查无此信或非法编号→400 refused（fail closed，什么都不动）；守卫保持（403）。
  /api/send {"from","to","subject","need","body"}
    - 与 CLI `send` 同一核心：@逻辑地址 first-online、绝不跳级、只有靠前的候选离线才 fallback、
      全离线按既有「发送失败：@x 当前没有在线信箱」文案拒绝。
    - 成功 200 {"ok":true,"id","ref":"<resolved>/<id>"}；生成的信与 CLI `send` 逐字节一致。
    - 未知发信方→404；未登记物理目标→400 refused（文案同 CLI「没有信箱 x」）；非法形状→404/400；守卫保持（403）。
  全部在临时 POSTOFFICE_HOME 里跑，不碰真实邮局。
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_panelSA_"))

BOXES = ["alice", "bob", "q", "gpt", "bot"]


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
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


class SendAckAPI(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        for box in BOXES:
            out = run_po("add", box, "--notify", "--who", "seam 测试", home=cls.home)
            if out.returncode != 0:
                raise RuntimeError(out.stdout + out.stderr)
        cfg = {"version": 1, "groups": {}, "aliases": {"escalate": ["q", "gpt", "bot"]}}
        (cls.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, PO, "panel", "--no-open", "--port", str(cls.port)],
            env=env_for(cls.home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError("面板进程提前退出")
            try:
                status, _ = cls.raw("GET", "/api/state")
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("面板没有在 15 秒内起来")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait()
        shutil.rmtree(cls.home, ignore_errors=True)

    # -- HTTP helpers -----------------------------------------------------
    @classmethod
    def raw(cls, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    @classmethod
    def jpost(cls, path, obj, extra_headers=None):
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers or {})
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        status, data = cls.raw("POST", path, body, headers)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data.decode("utf-8", errors="replace")

    # -- filesystem helpers ----------------------------------------------
    def inbox(self, box):
        return sorted(p.name for p in (self.home / box / "inbox").glob("*.md"))

    def acks(self):
        p = self.home / "acks.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def send_cli(self, to, sender, subject, need="仅告知", body="正文\n"):
        r = run_po("send", to, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = re.search(r"编号：(\S+)", r.stdout)
        self.assertIsNotNone(m, r.stdout)
        lid = m.group(1)
        return lid, self.home / to / "inbox" / f"{lid}.md"

    def notification(self, box, bid):
        """本箱 inbox 里信头带 `回执：<bid>` 的通知文件（只看信头，正文里的字样不算）。"""
        for f in sorted((self.home / box / "inbox").glob("*.md")):
            head = f.read_text(encoding="utf-8").split("\n\n", 1)[0]
            if f"回执：{bid}" in head:
                return f
        self.fail(f"{box}/inbox 里没有回执 {bid} 的通知")

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob("*") if p.is_file()}

    # -- /api/ack-one -----------------------------------------------------
    def test_ack_one_fail_closed_touches_nothing(self):
        before = self.snapshot()
        status, d = self.jpost("/api/ack-one", {"box": "ghost", "id": "20200101-000000_alice_none"})
        self.assertEqual((status, d), (404, {"ok": False, "error": "not_found"}))
        status, d = self.jpost("/api/ack-one", {"box": "bob", "id": "20200101-000000_alice_none"})
        self.assertEqual((status, d["ok"], d["error"]), (400, False, "refused"))
        self.assertTrue(d.get("message"), "被拒时必须说明原因")
        for bad in ("../x", "x/y", "x.md", "*", "/etc/passwd"):
            status, d = self.jpost("/api/ack-one", {"box": "bob", "id": bad})
            self.assertEqual((status, d), (400, {"ok": False, "error": "bad_request"}), bad)
        status, d = self.jpost("/api/ack-one", {"box": "bob", "id": "anything", "note": 5})
        self.assertEqual((status, d), (400, {"ok": False, "error": "bad_request"}))
        self.assertEqual(self.snapshot(), before, "fail closed 的请求不许动任何文件")

    def test_ack_one_guards_still_apply(self):
        status, _ = self.raw("POST", "/api/ack-one", b'{"box":"bob","id":"x"}',
                             {"Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        status, _ = self.raw("POST", "/api/ack-one", b'{"box":"bob","id":"x"}',
                             {"Content-Type": "application/json", "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.raw("POST", "/api/other", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, d = self.raw("POST", "/api/ack-one", b"{not json",
                             {"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_ack_one_is_idempotent(self):
        lid, _ = self.send_cli("bob", "alice", "幂等信")
        status, d = self.jpost("/api/ack-one", {"box": "bob", "id": lid, "note": "第一次"})
        self.assertEqual((status, d["ok"], d["state"]), (200, True, "recorded"))
        notices = len(self.inbox("alice"))
        status, d = self.jpost("/api/ack-one", {"box": "bob", "id": lid, "note": "第二次"})
        self.assertEqual((status, d["ok"], d["state"]), (200, True, "already"))
        rows = [e for e in self.acks() if e["id"] == lid]
        self.assertEqual(len(rows), 1, "重复 ack 不许产生第二份回执")
        self.assertEqual(rows[0]["note"], "第一次", "重复调用不许改写第一次的回执")
        self.assertEqual(len(self.inbox("alice")), notices, "重复 ack 不许再发一份通知")
        self.assertTrue((self.home / "bob" / "done" / f"{lid}.md").is_file())

    def test_ack_one_matches_cli_ack(self):
        lid1, _ = self.send_cli("bob", "alice", "等价比对", body="同一正文\n")
        time.sleep(1.1)                     # 换个时间戳，避免同一秒撞文件名
        lid2, _ = self.send_cli("bob", "alice", "等价比对", body="同一正文\n")
        r = run_po("ack", "bob", lid1, "同一条备注", home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"已回执：{lid1}（空闲时通知 alice，默认不答复）", r.stdout)
        status, d = self.jpost("/api/ack-one", {"box": "bob", "id": lid2, "note": "同一条备注"})
        self.assertEqual((status, d["ok"], d["state"]), (200, True, "recorded"))
        rows = {e["id"]: e for e in self.acks() if e["id"] in (lid1, lid2)}
        self.assertEqual(set(rows), {lid1, lid2})
        strip = lambda e: {k: v for k, v in e.items() if k not in ("id", "time")}
        self.assertEqual(strip(rows[lid1]), strip(rows[lid2]), "CLI 与面板的账本条目必须一致")
        n1 = self.notification("alice", lid1).read_text(encoding="utf-8")
        n2 = self.notification("alice", lid2).read_text(encoding="utf-8")
        self.assertEqual(n1.replace(lid1, "<ID>"), n2.replace(lid2, "<ID>"),
                         "CLI ack 与面板 ack 的通知信必须逐字节等价（除编号）")

    def test_ack_one_receipt_notification_only_files(self):
        lid, _ = self.send_cli("bob", "alice", "通知归档信")
        r = run_po("ack", "bob", lid, "收到", home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        notif = self.notification("alice", lid)
        before_inbox = len(self.inbox("alice"))
        before_acks = len(self.acks())
        status, d = self.jpost("/api/ack-one", {"box": "alice", "id": notif.stem})
        self.assertEqual((status, d["ok"], d["state"]), (200, True, "receipt_notice"))
        self.assertFalse(notif.exists())
        self.assertTrue((self.home / "alice" / "done" / notif.name).is_file())
        self.assertEqual(len(self.acks()), before_acks, "归档回执通知不许再记一条回执")
        self.assertEqual(len(self.inbox("alice")), before_inbox - 1, "不许再产生新通知")

    def test_ack_system_notification_ledgers_without_backsend(self):
        # 系统通知（发信方 postoffice）只留知悉账：不回送、不注册假信箱、不报“未登记”
        lid = "20260101-120000_postoffice_系统通知"
        (self.home / "bob" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.home / "bob" / "inbox" / f"{lid}.md").write_text(
            "来源：postoffice\n事由：系统通知\n需要：仅告知\n\n正文\n", encoding="utf-8")
        before_acks = len(self.acks())
        r = run_po("ack", "bob", lid, "知悉", home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已记账（知悉系统通知）", r.stdout)
        self.assertNotIn("未登记", r.stdout, "不得报假“未登记”")
        self.assertNotIn("已投递回执通知", r.stdout, "不得回送系统")
        self.assertTrue((self.home / "bob" / "done" / f"{lid}.md").is_file(), "原信进 done")
        rows = [e for e in self.acks() if e["id"] == lid]
        self.assertEqual(len(rows), 1, "记一条知悉账")
        self.assertEqual(rows[0]["to"], "postoffice")
        self.assertEqual(len(self.acks()), before_acks + 1)
        # 普通 ack 行为不变
        lid2, _ = self.send_cli("bob", "alice", "普通信")
        r2 = run_po("ack", "bob", lid2, "收到", home=self.home)
        self.assertIn("已回执：", r2.stdout)
        self.assertIn("空闲时通知 alice", r2.stdout)
        self.assertTrue(self.notification("alice", lid2).is_file(), "普通 ack 照常通知")

    def test_ack_one_records_files_and_notifies(self):
        lid, path = self.send_cli("bob", "alice", "甲信", body="第一行\n第二行\n")
        raw = path.read_bytes()
        status, d = self.jpost("/api/ack-one", {"box": "bob", "id": lid, "note": "收到，已阅"})
        self.assertEqual((status, d), (200, {"ok": True, "state": "recorded", "id": lid, "to": "alice"}))
        self.assertFalse(path.exists(), "原信必须移出 inbox")
        filed = self.home / "bob" / "done" / f"{lid}.md"
        self.assertEqual(filed.read_bytes(), raw, "inbox→done 必须一字节不改")
        rows = [e for e in self.acks() if e["id"] == lid]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["by"], rows[0]["to"], rows[0]["kind"], rows[0]["note"]),
                         ("bob", "alice", "letter", "收到，已阅"))
        text = self.notification("alice", lid).read_text(encoding="utf-8")
        self.assertIn("事由：回执：甲信", text)
        self.assertIn(f"对应原信：{lid}", text)
        self.assertIn(f"postoffice receipt alice '{lid}'", text)
        r = run_po("receipt", "alice", lid, home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("来源：bob", r.stdout)
        self.assertIn("收到，已阅", r.stdout)

    # -- /api/send --------------------------------------------------------
    def test_send_alias_all_offline_refused(self):
        for box in ("q", "gpt", "bot"):
            self.assertEqual(run_po("offline", box, home=self.home).returncode, 0)
        try:
            before = self.snapshot()
            status, d = self.jpost("/api/send", {"from": "alice", "to": "@escalate",
                                                 "subject": "全军离线", "need": "请处理", "body": "x\n"})
            self.assertEqual((status, d["ok"], d["error"]), (400, False, "refused"))
            self.assertIn("发送失败：@escalate 当前没有在线信箱", d["message"])
            self.assertIn("候选：", d["message"])
            self.assertIn("- gpt：离线", d["message"])
            self.assertEqual(self.snapshot(), before, "全离线拒绝时不许投出任何信")
        finally:
            for box in ("q", "gpt", "bot"):
                run_po("online", box, home=self.home)

    def test_send_alias_fallback_and_old_mail_stays(self):
        status, d1 = self.jpost("/api/send", {"from": "alice", "to": "@escalate",
                                              "subject": "回退甲", "need": "请处理", "body": "1\n"})
        self.assertEqual((status, d1["ok"]), (200, True))
        self.assertEqual(d1["ref"], f"q/{d1['id']}")
        self.assertEqual(run_po("offline", "q", home=self.home).returncode, 0)
        try:
            status, d2 = self.jpost("/api/send", {"from": "alice", "to": "@escalate",
                                                  "subject": "回退乙", "need": "请处理", "body": "2\n"})
            self.assertEqual((status, d2["ok"]), (200, True))
            self.assertEqual(d2["ref"], f"gpt/{d2['id']}", "靠前的候选离线才 fallback 到下一个")
            self.assertTrue((self.home / "q" / "inbox" / f"{d1['id']}.md").is_file(),
                            "旧信不许跟着目标切换搬家")
        finally:
            run_po("online", "q", home=self.home)
        status, d3 = self.jpost("/api/send", {"from": "alice", "to": "@escalate",
                                              "subject": "回退丙", "need": "请处理", "body": "3\n"})
        self.assertEqual(d3["ref"], f"q/{d3['id']}", "候选恢复后新信回第一候选")
        self.assertTrue((self.home / "q" / "inbox" / f"{d1['id']}.md").is_file())
        self.assertTrue((self.home / "gpt" / "inbox" / f"{d2['id']}.md").is_file())

    def test_send_alias_first_online_never_skips(self):
        before = {b: set(self.inbox(b)) for b in ("q", "gpt", "bot")}
        status, d = self.jpost("/api/send", {"from": "alice", "to": "@escalate",
                                             "subject": "不跳级", "need": "请处理", "body": "x\n"})
        self.assertEqual((status, d["ok"]), (200, True))
        self.assertTrue(d["ref"].startswith("q/"), f"第一候选在线时必须进 q：{d['ref']}")
        self.assertEqual(set(self.inbox("q")) - before["q"], {f"{d['id']}.md"},
                         "q 收件箱应恰好新增这封")
        for b in ("gpt", "bot"):
            self.assertEqual(set(self.inbox(b)), before[b], f"{b} 不许收到（跳级）")
        text = (self.home / "q" / "inbox" / f"{d['id']}.md").read_text(encoding="utf-8")
        self.assertIn("逻辑地址：@escalate", text, "逻辑地址信头必须与 CLI 一致")
        self.assertIn("来源：alice", text)

    def test_send_guards_still_apply(self):
        status, _ = self.raw("POST", "/api/send", b'{"from":"alice","to":"bob"}',
                             {"Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        status, _ = self.raw("POST", "/api/send", b'{"from":"alice","to":"bob"}',
                             {"Content-Type": "application/json", "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.raw("POST", "/api/nope", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, d = self.raw("POST", "/api/send", b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_send_physical_target_and_bytes_match_cli(self):
        body = "第一行\n第二行 <script>alert(1)</script> 🎯\n"
        status, d = self.jpost("/api/send", {"from": "alice", "to": "bob",
                                             "subject": "面板写信", "need": "仅告知", "body": body})
        self.assertEqual((status, d["ok"]), (200, True))
        self.assertEqual(d["ref"], f"bob/{d['id']}")
        panel_path = self.home / "bob" / "inbox" / f"{d['id']}.md"
        panel_bytes = panel_path.read_bytes()
        time.sleep(1.1)                     # 换个时间戳，避免同一秒撞文件名
        r = run_po("send", "bob", "alice", "面板写信", "仅告知", home=self.home, stdin=body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cli_lid = re.search(r"编号：(\S+)", r.stdout).group(1)
        cli_bytes = (self.home / "bob" / "inbox" / f"{cli_lid}.md").read_bytes()
        self.assertNotEqual(cli_lid, d["id"])
        self.assertEqual(panel_bytes, cli_bytes, "面板与 CLI 生成的信必须逐字节一致")
        text = panel_bytes.decode("utf-8")
        self.assertIn("来源：alice", text)
        self.assertIn("事由：面板写信", text)
        self.assertIn("需要：仅告知", text)
        self.assertIn(body.split("\n")[1], text)

    def test_cli_send_refuses_missing_target_before_reading_body(self):
        # 目标不存在时先拒绝、再读正文：不许先碰 --file（也不许为它吞 stdin）
        r = subprocess.run([sys.executable, PO, "send", "nobody", "alice", "s", "仅告知",
                            "--file", "/nonexistent-xyz-po-seam"],
                           capture_output=True, text=True, env=env_for(self.home), timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("没有信箱 nobody", r.stdout + r.stderr)
        self.assertNotIn("FileNotFoundError", r.stderr)

    def test_send_unknown_sender_and_target_refused(self):
        status, d = self.jpost("/api/send", {"from": "ghost", "to": "bob", "subject": "s",
                                             "need": "仅告知", "body": "x\n"})
        self.assertEqual((status, d), (404, {"ok": False, "error": "not_found"}))
        status, d = self.jpost("/api/send", {"from": "alice", "to": "nobody", "subject": "s",
                                             "need": "仅告知", "body": "x\n"})
        self.assertEqual((status, d["ok"], d["error"]), (400, False, "refused"))
        self.assertIn("没有信箱 nobody", d["message"], "未知目标沿用 CLI send 的同一句拒绝")
        status, d = self.jpost("/api/send", {"from": "alice", "to": "../bob", "subject": "s",
                                             "need": "仅告知", "body": "x\n"})
        self.assertEqual((status, d), (404, {"ok": False, "error": "not_found"}))
        status, d = self.jpost("/api/send", {"from": "alice", "to": "@nope", "subject": "s",
                                             "need": "仅告知", "body": "x\n"})
        self.assertEqual((status, d["ok"], d["error"]), (400, False, "refused"))
        self.assertIn("没有逻辑地址", d["message"])
        status, d = self.jpost("/api/send", {"from": "alice", "subject": "s",
                                             "need": "仅告知", "body": "x\n"})
        self.assertEqual((status, d), (400, {"ok": False, "error": "bad_request"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
