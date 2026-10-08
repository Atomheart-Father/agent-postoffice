#!/usr/bin/env python3
"""v1.13 outbox（人控制台「发件箱」）后端 seam（Red 先行，本文件写于实现之前）。

冻结契约：
  GET /api/outbox（纯读）
    200 {"ok":true,"outbox":[row,...]}；row 的键恰好是
    {to,id,ref,subject,need,body,alias,mtime,status,reason}；ref == "<to>/<id>"；
    alias 是冻结的 `逻辑地址：@x` 信头值（没有则 ""）；status ∈ {pending,locked,unknown}；
    status != pending 时 reason 是非空中文说明，pending 时 reason == ""。
    发现规则：只收「来源：== operator」的普通直发信（信头没有 回执：/广播：/闹钟：/切换事件：
    字段，且来源 != "postoffice"）。别的发信方寄到任何信箱的信都不出现；派生通知不出现。
    alias 不二次解析：行里的 to 就是发信当时解析出的物理信箱，之后候选改线/离线都不变。
    状态分类（与收件方在线/离线无关）：
      (a) 无认领、无台账 → pending
      (b) opencode_plugin 收件方 + opencode_delivered.jsonl 有 DELIVERED → locked
      (c) claude_hook 收件方 + <box>/.seen 里有该信路径 → locked
      (d) codex_queue/notify 收件方 + <home>/.woken.json 里有该信路径 → locked
      (e) <box>/.claims/ 里有认领文件（命名同 claim_letter：<id>.md）→ locked
      (f) opencode_plugin FAILED_FINAL 台账 → unknown
      (g) 手改 routes 成未知 method 字符串 → unknown
    纯读：连续两次 GET 后整个 home（除 logs/）逐字节不变，且不新建文件。
  POST /api/edit-one {"to","id","subject"?,"need"?,"body"?}
    至少一个 subject/need/body 是字符串，否则 400 bad_request；坏 id（../x、x/y、x.md、*、绝对路径）
    400 bad_request；未知/未登记 to → 404 not_found；查无此信 → 404 not_found；
    成功 200 {"ok":true,"state":"updated","id","to"}，文件保持 id/文件名/来源/收件人，
    subject/need/body 改成新值（读回核对）。多余字段 from/sender 一律忽略：信的真实来源 != operator
    → 400 refused + 与 CLI 同一句 message，文件一字节不动。
  POST /api/retract-one {"to","id"}
    成功 200 {"ok":true,"state":"retracted","id","to"}，原信移到 <to>/archived/<ts>/<id>.md，
    不产生任何通知信/第二封信，acks.jsonl 不变；查无此信 → 404；已送达/有认领 → 400 refused + CLI 文案。
    竞态：行还显示 pending 后按住认领，再 retract/edit 必须 400 refused 且原信不动。
  守卫：与其他写接口同一套 403 矩阵；JSON 坏 → 400。
  CLI/面板等价：同一封（除编号外字节相同的两封）分别用 CLI edit/retract 与 /api/edit-one、
    /api/retract-one 处理，结束时文件逐字节一致（归档里也一致）。
  隔离：panel 段坏掉（operator 缺失/无效）→ /api/outbox 返回 {"ok":true,"outbox":[]}，
    物理发信/逻辑地址/分组/邮递员照常（至少一次 send 成功）。

全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，只连 127.0.0.1，不碰真实邮局。
"""
import http.client
import hashlib
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
BASE = Path(tempfile.mkdtemp(prefix="po_outbox_"))

OPBOX = "opbox"
OTHER = "otherbox"
BOXES = ["opbox", "otherbox", "plainbox", "plugbox", "hookbox", "codexbox",
         "notifyb", "claimbox", "weirdbox", "aliasa", "aliasb", "grp1"]

ROW_KEYS = {"to", "id", "ref", "subject", "need", "body", "alias", "mtime", "status", "reason"}
CJK = re.compile(r"[\u4e00-\u9fff]")


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


class OutboxAPI(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in BOXES:
            out = run_po("add", box, "--notify", "--who", "outbox 测试", home=self.home)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.set_methods("plugbox", "opencode_plugin", session_id="ses_plug")
        self.set_methods("hookbox", "claude_hook", claude_title="钩子箱", claude_session="loc_hook")
        self.set_methods("codexbox", "codex_queue", thread_id="th_codex")
        self.set_methods("weirdbox", "carrier_pigeon")
        self.write_config()
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
                status, _ = self.raw("GET", "/api/state")
                if status == 200:
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

    # -- fixtures ---------------------------------------------------------
    def write_config(self, panel=None):
        cfg = {"version": 1, "groups": {"grp": ["grp1"]},
               "aliases": {"al": ["aliasa", "aliasb"]},
               "panel": panel if panel is not None else
               {"label": "T", "operator": OPBOX, "organization": {"mailbox": OPBOX}}}
        (self.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def set_methods(self, box, method, **extra):
        p = self.home / "routes.json"
        r = json.loads(p.read_text(encoding="utf-8"))
        r[box]["methods"] = [method]
        r[box].update(extra)
        p.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")

    def send(self, to, subject, sender=OPBOX, need="回复", body="正文内容。\n"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lid = next(l.split("：", 1)[1].strip() for l in out.stdout.splitlines()
                   if l.startswith("编号："))
        return lid

    def put(self, box, lid, header, body="正文\n"):
        p = self.home / box / "inbox" / f"{lid}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(header) + "\n\n" + body, encoding="utf-8")
        return p

    # -- HTTP helpers -----------------------------------------------------
    def raw(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def jget(self, path):
        status, data = self.raw("GET", path)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data.decode("utf-8", errors="replace")

    def jpost(self, path, obj, extra_headers=None):
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers or {})
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        status, data = self.raw("POST", path, body, headers)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data.decode("utf-8", errors="replace")

    def outbox(self):
        status, d = self.jget("/api/outbox")
        self.assertEqual(status, 200, d)
        self.assertIs(d.get("ok"), True, d)
        return d["outbox"]

    def row_for(self, to, lid):
        rows = [r for r in self.outbox() if r["to"] == to and r["id"] == lid]
        self.assertEqual(len(rows), 1, f"{to}/{lid} 应当恰好一行：{rows}")
        return rows[0]

    def snapshot(self):
        """整个临时邮局（除 logs/）的 路径→(大小, sha256)，供纯读写断言。"""
        out = {}
        for f in sorted(self.home.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(self.home)
            if rel.parts and rel.parts[0] == "logs":
                continue
            out[str(rel)] = (f.stat().st_size, hashlib.sha256(f.read_bytes()).hexdigest())
        return out

    def cli_message(self, *args):
        out = run_po(*args, home=self.home)
        self.assertNotEqual(out.returncode, 0, f"CLI 对照应当失败：{out.stdout}{out.stderr}")
        return (out.stdout + out.stderr).strip()

    # ================================================================ shape
    def test_outbox_rows_have_the_frozen_shape(self):
        lid = self.send("plainbox", "形状信", need="仅告知", body="形状正文。\n")
        rows = self.outbox()
        self.assertTrue(any(r["to"] == "plainbox" and r["id"] == lid for r in rows), rows)
        row = self.row_for("plainbox", lid)
        self.assertEqual(set(row), ROW_KEYS, "行的键集合被冻结")
        self.assertEqual(row["id"], lid)
        self.assertEqual(row["to"], "plainbox")
        self.assertEqual(row["ref"], f"plainbox/{lid}")
        self.assertEqual(row["subject"], "形状信")
        self.assertEqual(row["need"], "仅告知")
        self.assertEqual(row["alias"], "", "没有逻辑地址信头时 alias 必须是空串")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["reason"], "", "pending 的 reason 必须是空串")
        self.assertIsInstance(row["mtime"], (int, float))
        stat_m = (self.home / "plainbox" / "inbox" / f"{lid}.md").stat().st_mtime
        self.assertLess(abs(row["mtime"] - stat_m), 2, "mtime 应是该信文件的修改时间")
        self.assertIsInstance(row["body"], str)
        self.assertIn("形状正文", row["body"], "body 应带正文")
        self.assertNotIn("来源：", row["body"], "body 不该带上信头")
        self.assertNotIn("事由：", row["body"])

    # ============================================================ discovery
    def test_outbox_discovery_only_operator_plain_direct_mail(self):
        good = self.send("plainbox", "正常发件箱信", body="正常。\n")
        foreign = self.send("plainbox", "别人的信", sender=OTHER, body="别人。\n")
        system = self.put("plainbox", "20260101-000001_postoffice_系统",
                          ["来源：postoffice", "事由：系统通知", "需要：仅告知"])
        derived = {
            "20260101-000002_opbox_回执": ["来源：" + OPBOX, "事由：回执：某事",
                                          "需要：回执（默认不答复）", "回执：X-1", "原事由：某事"],
            "20260101-000003_opbox_广播": ["来源：" + OPBOX, "事由：广播：某事",
                                          "需要：回复", "广播：B20260101-000000_opbox"],
            "20260101-000004_opbox_闹钟": ["来源：" + OPBOX, "事由：闹钟",
                                          "需要：仅告知", "闹钟：A20260101-000000_plainbox"],
            "20260101-000005_opbox_切换": ["来源：" + OPBOX, "事由：会话切换",
                                          "需要：仅告知", "切换事件：E-1"],
        }
        for lid, header in derived.items():
            self.put("plainbox", lid, header)
        rows = self.outbox()
        refs = {r["ref"] for r in rows}
        self.assertIn(f"plainbox/{good}", refs, "operator 的普通直发信必须出现")
        self.assertNotIn(f"plainbox/{foreign}", refs, "别的发信方的信不许出现")
        self.assertNotIn(f"plainbox/{system}", refs, "来源 postoffice 的来信不许出现")
        for lid in derived:
            self.assertNotIn(f"plainbox/{lid}", refs, f"派生通知不许出现：{lid}")

    # ================================================================= alias
    def test_outbox_alias_is_frozen_and_not_resolved_again(self):
        out = run_po("send", "@al", OPBOX, "逻辑地址信", "回复", home=self.home, stdin="逻辑正文。\n")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lid = next(l.split("：", 1)[1].strip() for l in out.stdout.splitlines() if l.startswith("编号："))
        row = self.row_for("aliasa", lid)
        self.assertEqual(row["alias"], "@al", "alias 必须是冻结的 逻辑地址：@x 信头值")
        self.assertEqual(row["to"], "aliasa", "to 必须是发信当时解析出的物理信箱")
        self.assertEqual(row["ref"], f"aliasa/{lid}")

        # 之后把第一候选下线（alias 现在应解析到 aliasb），行里的 to 不许被二次解析改变
        self.assertEqual(run_po("offline", "aliasa", home=self.home).returncode, 0)
        self.assertEqual(run_po("offline", "aliasb", home=self.home).returncode, 0)
        self.assertEqual(self.row_for("aliasa", lid)["to"], "aliasa",
                         "outbox 不许重新解析逻辑地址")
        self.assertEqual(self.row_for("aliasa", lid)["alias"], "@al")

    # ================================================================ status
    def test_outbox_status_classes_and_reasons(self):
        pend = self.send("plainbox", "无认领无台账", body="待发。\n")
        plugok = self.put("plugbox", "20260101-100001_opbox_已送",
                          ["来源：" + OPBOX, "事由：插件送达", "需要：回复"])
        plugin_failed = self.put("plugbox", "20260101-100002_opbox_失败终",
                                 ["来源：" + OPBOX, "事由：插件失败", "需要：回复"])
        hookseen = self.put("hookbox", "20260101-100003_opbox_钩子见",
                            ["来源：" + OPBOX, "事由：钩子已见", "需要：回复"])
        codexwoke = self.put("codexbox", "20260101-100004_opbox_邮差",
                             ["来源：" + OPBOX, "事由：邮差接受", "需要：回复"])
        notifywoke = self.put("notifyb", "20260101-100005_opbox_通知",
                              ["来源：" + OPBOX, "事由：通知接受", "需要：回复"])
        claimed = self.put("claimbox", "20260101-100006_opbox_认领",
                           ["来源：" + OPBOX, "事由：有人认领", "需要：回复"])
        weird = self.put("weirdbox", "20260101-100007_opbox_未知",
                         ["来源：" + OPBOX, "事由：未知通道", "需要：回复"])

        with (self.home / "opencode_delivered.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"box": "plugbox", "file": f"{plugok.stem}.md",
                                "result": "DELIVERED"}) + "\n")
            f.write(json.dumps({"box": "plugbox", "file": f"{plugin_failed.stem}.md",
                                "result": "FAILED_FINAL"}) + "\n")
        (self.home / "hookbox" / ".seen").write_text(str(hookseen) + "\n", encoding="utf-8")
        (self.home / ".woken.json").write_text(
            json.dumps({str(codexwoke): 1.0, str(notifywoke): 1.0}), encoding="utf-8")
        cdir = self.home / "claimbox" / ".claims"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / f"{claimed.stem}.md").write_text("99999\n", encoding="utf-8")
        # 与在线/离线无关：把 pending 的收件方下线，仍然 pending
        self.assertEqual(run_po("offline", "plainbox", home=self.home).returncode, 0)

        cases = {
            ("plainbox", pend): "pending",
            ("plugbox", plugok.stem): "locked",
            ("plugbox", plugin_failed.stem): "unknown",
            ("hookbox", hookseen.stem): "locked",
            ("codexbox", codexwoke.stem): "locked",
            ("notifyb", notifywoke.stem): "locked",
            ("claimbox", claimed.stem): "locked",
            ("weirdbox", weird.stem): "unknown",
        }
        for (box, lid), want in cases.items():
            with self.subTest(box=box, lid=lid):
                row = self.row_for(box, lid)
                self.assertEqual(row["status"], want, row)
                if want == "pending":
                    self.assertEqual(row["reason"], "", "pending 的 reason 必须为空")
                else:
                    self.assertTrue(row["reason"], f"{want} 必须有非空 reason")
                    self.assertTrue(CJK.search(row["reason"]),
                                    f"reason 必须是中文说明：{row['reason']!r}")

    # ================================================================= purity
    def test_outbox_is_a_pure_read(self):
        self.send("plainbox", "纯读正面对照", body="纯读。\n")
        self.assertEqual(self.jget("/api/outbox")[0], 200)
        before = self.snapshot()
        for _ in range(2):
            status, d = self.jget("/api/outbox")
            self.assertEqual(status, 200, d)
        self.assertEqual(self.snapshot(), before, "outbox 必须纯读：连一个字节都不许写")
        for rel in ("runtime", "plainbox/.claims", "opencode_delivered.jsonl", ".woken.json"):
            self.assertFalse((self.home / rel).exists(), f"纯读不许产生 {rel}")

    # ============================================================ edit-one 400
    def test_edit_one_rejects_bad_requests_touching_nothing(self):
        lid = self.send("plainbox", "非法编辑对照", body="别动。\n")
        path = self.home / "plainbox" / "inbox" / f"{lid}.md"
        before = self.snapshot()
        bad = [
            {"to": "plainbox", "id": lid},                                   # 一个字段都不给
            {"to": "plainbox", "id": lid, "subject": 5},                     # 字段不是字符串
            {"to": "plainbox", "id": lid, "body": None},
            {"to": "plainbox", "id": "../x", "subject": "s"},
            {"to": "plainbox", "id": "x/y", "subject": "s"},
            {"to": "plainbox", "id": f"{lid}.md", "subject": "s"},
            {"to": "plainbox", "id": "*", "subject": "s"},
            {"to": "plainbox", "id": "/etc/passwd", "subject": "s"},
            {"to": "", "id": lid, "subject": "s"},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                status, d = self.jpost("/api/edit-one", payload)
                self.assertEqual(status, 400, f"{payload}: {d}")
                self.assertEqual(d, {"ok": False, "error": "bad_request"}, payload)
        # 未知但格式合法的信箱 / 查无此信 → 404
        status, d = self.jpost("/api/edit-one", {"to": "nosuch", "id": lid, "subject": "s"})
        self.assertEqual((status, d), (404, {"ok": False, "error": "not_found"}))
        status, d = self.jpost("/api/edit-one",
                               {"to": "plainbox", "id": "20990101-000000_opbox_无此信", "subject": "s"})
        self.assertEqual((status, d), (404, {"ok": False, "error": "not_found"}))
        self.assertTrue(path.is_file(), "被拒的编辑必须把原信留在 inbox")
        self.assertEqual(self.snapshot(), before, "被拒的编辑不许动任何文件")

    # ========================================================== edit-one 200
    def test_edit_one_updates_in_place_keeping_identity(self):
        lid = self.send("plainbox", "旧事由", need="回复", body="旧正文，来自旧版本。\n")
        path = self.home / "plainbox" / "inbox" / f"{lid}.md"
        status, d = self.jpost("/api/edit-one", {"to": "plainbox", "id": lid,
                                                 "subject": "新事由", "need": "仅告知",
                                                 "body": "新正文，逐字替换 --subject 不是参数。"})
        self.assertEqual(status, 200, d)
        self.assertEqual(d, {"ok": True, "state": "updated", "id": lid, "to": "plainbox"})
        self.assertTrue(path.is_file(), "edit 必须原地改，文件名/编号不变")
        text = path.read_text(encoding="utf-8")
        head = text.split("\n\n", 1)[0].splitlines()
        self.assertEqual([l.split("：", 1)[0] for l in head], ["来源", "事由", "需要"],
                         "系统标记不许增删：\n" + "\n".join(head))
        self.assertTrue(head[0].startswith(f"来源：{OPBOX}"), "来源必须原样保留")
        self.assertIn("事由：新事由", text)
        self.assertIn("需要：仅告知", text)
        self.assertIn("新正文，逐字替换 --subject 不是参数。", text)
        self.assertNotIn("旧正文，来自旧版本。", text)
        self.assertNotIn("事由：旧事由", text)
        self.assertTrue((self.home / "plainbox" / "inbox" / f"{lid}.md").is_file(),
                        "收件人/文件位置不变")

    # ================================================== edit-one spoof/refuse
    def test_edit_one_ignores_spoof_fields_and_refuses_foreign_sender(self):
        lid = self.send("plainbox", "别人的信", sender=OTHER, body="不许动。\n")
        path = self.home / "plainbox" / "inbox" / f"{lid}.md"
        before = path.read_bytes()
        cli_msg = self.cli_message("edit", "--box", OPBOX, f"plainbox/{lid}", "--subject", "硬改")
        status, d = self.jpost("/api/edit-one", {"to": "plainbox", "id": lid, "subject": "硬改",
                                                 "from": OPBOX, "sender": OPBOX})
        self.assertEqual((status, d.get("ok"), d.get("error")), (400, False, "refused"), d)
        self.assertEqual(d.get("message"), cli_msg, "必须沿用 CLI 的同一句拒绝文案")
        self.assertEqual(path.read_bytes(), before, "冒名编辑不许动别人的信")

    # ====================================================== retract-one 200
    def test_retract_one_moves_original_and_writes_nothing_else(self):
        lid = self.send("plainbox", "要撤回", body="撤回我。\n")
        path = self.home / "plainbox" / "inbox" / f"{lid}.md"
        before = path.read_bytes()
        acks = (self.home / "acks.jsonl")
        acks_before = acks.read_bytes() if acks.exists() else None
        letters_before = sorted(p.name for p in self.home.rglob("*.md"))

        status, d = self.jpost("/api/retract-one", {"to": "plainbox", "id": lid})
        self.assertEqual(status, 200, d)
        self.assertEqual(d, {"ok": True, "state": "retracted", "id": lid, "to": "plainbox"})
        self.assertFalse(path.exists(), "原信必须离开 inbox")
        arch = list((self.home / "plainbox" / "archived").rglob(f"{lid}.md"))
        self.assertEqual(len(arch), 1, "原信要原样进 archived/<ts>/")
        self.assertEqual(arch[0].read_bytes(), before, "撤回不改一个字节")
        letters_after = sorted(p.name for p in self.home.rglob("*.md"))
        self.assertEqual(letters_after, letters_before,
                         "撤回只搬一封信，不许产生通知信/第二封信")
        self.assertEqual(acks.read_bytes() if acks.exists() else None, acks_before,
                         "撤回不许改 acks.jsonl")

    # ============================================ retract-one 404/400 refuse
    def test_retract_one_missing_and_delivered_and_claimed(self):
        status, d = self.jpost("/api/retract-one", {"to": "plainbox", "id": "ghost"})
        self.assertEqual((status, d), (404, {"ok": False, "error": "not_found"}))

        delivered = self.send("plugbox", "已送达的")
        with (self.home / "opencode_delivered.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"box": "plugbox", "file": f"{delivered}.md",
                                "result": "DELIVERED"}) + "\n")
        cli_msg = self.cli_message("retract", OPBOX, "plugbox", delivered)
        status, d = self.jpost("/api/retract-one", {"to": "plugbox", "id": delivered})
        self.assertEqual((status, d.get("ok"), d.get("error")), (400, False, "refused"), d)
        self.assertEqual(d.get("message"), cli_msg)
        self.assertTrue((self.home / "plugbox" / "inbox" / f"{delivered}.md").is_file())

        claimed = self.send("claimbox", "被认领的")
        cdir2 = self.home / "claimbox" / ".claims"
        cdir2.mkdir(parents=True, exist_ok=True)
        (cdir2 / f"{claimed}.md").write_text("99999\n", encoding="utf-8")
        cli_msg2 = self.cli_message("retract", OPBOX, "claimbox", claimed)
        status, d = self.jpost("/api/retract-one", {"to": "claimbox", "id": claimed})
        self.assertEqual((status, d.get("ok"), d.get("error")), (400, False, "refused"), d)
        self.assertEqual(d.get("message"), cli_msg2)
        self.assertTrue((self.home / "claimbox" / "inbox" / f"{claimed}.md").is_file())

    # ================================================================ race
    def test_outbox_edit_retract_race_with_held_claim(self):
        lid = self.send("claimbox", "竞态信", body="原样。\n")
        path = self.home / "claimbox" / "inbox" / f"{lid}.md"
        before = path.read_bytes()
        self.assertEqual(self.row_for("claimbox", lid)["status"], "pending")
        cdir = self.home / "claimbox" / ".claims"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / f"{lid}.md").write_text("99999\n", encoding="utf-8")
        status, d = self.jpost("/api/retract-one", {"to": "claimbox", "id": lid})
        self.assertEqual((status, d.get("ok"), d.get("error")), (400, False, "refused"), d)
        status, d = self.jpost("/api/edit-one", {"to": "claimbox", "id": lid, "subject": "硬改"})
        self.assertEqual((status, d.get("ok"), d.get("error")), (400, False, "refused"), d)
        self.assertEqual(path.read_bytes(), before, "认领在途时 edit/retract 都不许动原信")
        self.assertEqual((cdir / f"{lid}.md").read_text(encoding="utf-8"), "99999\n",
                         "别人的认领不许被动")

    # =============================================================== guards
    def test_outbox_edit_retract_guards(self):
        path = "/api/edit-one"
        body = b'{"to":"plainbox","id":"x","subject":"s"}'
        status, _ = self.raw("POST", path, body, {"Content-Type": "text/plain"})
        self.assertEqual(status, 403, "没有 application/json 必须拒绝")
        status, _ = self.raw("POST", path, body,
                             {"Content-Type": "application/json", "Origin": "http://evil.example"})
        self.assertEqual(status, 403, "外部 Origin 必须拒绝")
        status, _ = self.raw("POST", "/api/nope", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403, "未知写路径必须拒绝")
        status, d = self.raw("POST", path, b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400, "坏 JSON 必须 400")

        path = "/api/retract-one"
        body = b'{"to":"plainbox","id":"x"}'
        status, _ = self.raw("POST", path, body, {"Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        status, _ = self.raw("POST", path, body,
                             {"Content-Type": "application/json", "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, d = self.raw("POST", path, b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    # ========================================================= CLI parity
    def test_cli_and_panel_edit_parity(self):
        hdr = ["来源：" + OPBOX, "事由：等价比对", "需要：回复"]
        body = "同一正文\n"
        for lid in ("20260101-200001_opbox_等甲", "20260101-200002_opbox_等乙"):
            self.put("plainbox", lid, hdr, body=body)
        r = run_po("edit", "--box", OPBOX, "plainbox/20260101-200001_opbox_等甲",
                   "--subject", "新事由", "--need", "仅告知", "--body", "新正文", home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        status, d = self.jpost("/api/edit-one", {"to": "plainbox", "id": "20260101-200002_opbox_等乙",
                                                 "subject": "新事由", "need": "仅告知", "body": "新正文"})
        self.assertEqual(status, 200, d)
        a = (self.home / "plainbox" / "inbox" / "20260101-200001_opbox_等甲.md").read_bytes()
        b = (self.home / "plainbox" / "inbox" / "20260101-200002_opbox_等乙.md").read_bytes()
        self.assertEqual(a, b, "CLI edit 与面板 edit-one 必须产出逐字节一致的信")

    def test_cli_and_panel_retract_parity(self):
        hdr = ["来源：" + OPBOX, "事由：撤回比对", "需要：回复"]
        body = "同一正文\n"
        for lid in ("20260101-210001_opbox_撤甲", "20260101-210002_opbox_撤乙"):
            self.put("plainbox", lid, hdr, body=body)
        r = run_po("retract", OPBOX, "plainbox", "20260101-210001_opbox_撤甲", home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        status, d = self.jpost("/api/retract-one", {"to": "plainbox", "id": "20260101-210002_opbox_撤乙"})
        self.assertEqual(status, 200, d)
        a = list((self.home / "plainbox" / "archived").rglob("20260101-210001_opbox_撤甲.md"))
        b = list((self.home / "plainbox" / "archived").rglob("20260101-210002_opbox_撤乙.md"))
        self.assertEqual(len(a), 1, "CLI 撤回的必须已归档")
        self.assertEqual(len(b), 1, "面板撤回的必须已归档")
        self.assertEqual(a[0].read_bytes(), b[0].read_bytes(), "两条撤回路径必须产出逐字节一致的归档信")

    # ============================================================ isolation
    def test_broken_panel_section_disables_outbox_only(self):
        good = self.send("plainbox", "隔离正面对照", body="隔离。\n")
        self.assertTrue(any(r["id"] == good for r in self.outbox()), "前置：正常 panel 下 outbox 可用")
        original = (self.home / "config.json").read_bytes()
        try:
            for panel in ({"label": "T", "organization": {"mailbox": OPBOX}},   # 缺 operator
                          {"label": "T", "operator": "ghost", "organization": {"mailbox": OPBOX}}):
                with self.subTest(panel=panel):
                    self.write_config(panel=panel)
                    status, d = self.jget("/api/outbox")
                    self.assertEqual((status, d), (200, {"ok": True, "outbox": []}),
                                     "panel 段坏掉时 outbox 必须空表且 ok")
        finally:
            (self.home / "config.json").write_bytes(original)
        # 物理发信 / 逻辑地址 / 分组 / 邮递员照常
        out = run_po("send", "plainbox", OPBOX, "坏 panel 下的物理信", "回复",
                     home=self.home, stdin="物理。\n")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        # 用坏 panel（但 aliases/groups 有效）再试一次 alias 与 group
        self.write_config(panel={"label": "T", "operator": "ghost", "organization": {"mailbox": OPBOX}})
        out = run_po("send", "@al", OPBOX, "坏 panel 下的逻辑信", "回复",
                     home=self.home, stdin="逻辑。\n")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(run_po("online", "@grp", home=self.home).returncode, 0, "分组开关照常")
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
        (self.home / "config.json").write_bytes(original)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
