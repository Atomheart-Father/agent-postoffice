#!/usr/bin/env python3
"""面板信件详情 + 单封归档 API（ticket 2，Red 先行）。

冻结契约：
  GET /api/letter?box=<box>&id=<id>
    200 {"ok":true,"id","box","sender","subject","need","body"}
      - id 是裸编号（不含 .md）；sender 是「来源」信头的短格式（去掉（…）括注，等价 letter_sender()）；
        subject/need 是信头原值；body 是文件里存的正文字节，去掉最末一个换行，内部换行原样。
      - 只读 <box>/inbox/<id>.md，绝不读 done/ 或 archived/。
    非法输入（缺参/空参、id 不合裸编号规则：../x、x/y、x.md、*、绝对路径、%2e%2e%2fx 等）
      → 400 {"ok":false,"error":"bad_request"}；未知信箱 / 查无此信 → 404 {"ok":false,"error":"not_found"}。
    纯读：不写任何 .presented.json / .seen / 台账 / 认领 / 唤醒文件。
  POST /api/archive-one {"box","id"}
    inbox→done 一字节不改；重复调用幂等 already；done/ 同名异内容 → 409 conflict（两边都不动）；
    两边都没有 → 404；非法 id → 400，未知信箱 → 404；不动别的信。
    仍保留守卫：无 application/json → 403；外部 Origin → 403；未知路径 → 403。
  /api/clear 仍是整箱 inbox→archived（不是 done）。

本文件写于实现之前，预期在当前树上失败（RED）；所有断言只钉冻结行为，不钉实现。
全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，只连 127.0.0.1，不碰真实邮局。
"""
import hashlib
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
import urllib.parse
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_panelL_"))
ZI = "仅告知"

BOXES = ["pbox", "arcbox", "arc2", "arc3", "guardbox", "clearbox"]


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


class PanelLetterAPI(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        for box in BOXES:
            out = run_po("add", box, "--notify", "--who", "面板 API 测试", home=cls.home)
            if out.returncode != 0:
                raise RuntimeError(out.stdout + out.stderr)
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
    def jget(cls, path):
        status, data = cls.raw("GET", path)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data.decode("utf-8", errors="replace")

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

    # -- fixtures ---------------------------------------------------------
    def send(self, box, subject, need=ZI, body="正文内容。\n", sender="boss"):
        out = run_po("send", box, sender, subject, need, home=self.home, stdin=body)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lid = next(l.split("：", 1)[1].strip() for l in out.stdout.splitlines()
                   if l.startswith("编号："))
        path = self.home / box / "inbox" / f"{lid}.md"
        self.assertTrue(path.is_file(), f"信没有落盘：{path}")
        return lid, path

    def detail_url(self, box, lid):
        return (f"/api/letter?box={urllib.parse.quote(box)}"
                f"&id={urllib.parse.quote(lid, safe='')}")

    def stored_body(self, path):
        raw = Path(path).read_text(encoding="utf-8")
        self.assertIn("\n\n", raw, "信件必须由信头块 + 空行 + 正文组成")
        stored = raw.split("\n\n", 1)[1]
        self.assertTrue(stored.endswith("\n"), "write_letter 总以换行收尾")
        return stored[:-1]

    def inbox(self, box):
        return sorted(p.name for p in (self.home / box / "inbox").glob("*.md"))

    def done(self, box):
        return sorted(p.name for p in (self.home / box / "done").glob("*.md"))

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

    # ==================================================================== 详情
    def test_detail_returns_the_stored_letter_exactly_and_raw(self):
        cases = [
            ("plain", "只有一行正文", "请回复"),
            ("multiline", "第一行\n第二行\n\n空行后是第四行", ZI),
            ("chinese", "你好，世界。这是中文正文。", ZI),
            ("english", "Hello, this is a plain English body.", ZI),
            ("emoji", "火箭 🚀 和派对 🎉", ZI),
            ("html", "<b>粗体</b> & '单引号' \"双引号\"", ZI),
            ("script", "<script>alert(1)</script>", ZI),
        ]
        for name, body, need in cases:
            with self.subTest(case=name):
                lid, path = self.send("pbox", f"详情往返 {name}", need=need, body=body + "\n")
                status, d = self.jget(self.detail_url("pbox", lid))
                self.assertEqual(status, 200, f"{name}: {d}")
                self.assertEqual(set(d), {"ok", "id", "box", "sender", "subject", "need", "body"},
                                 "200 响应必须恰好带这七个字段")
                self.assertIs(d["ok"], True)
                self.assertEqual(d["id"], lid, "id 必须是裸编号（不含 .md）")
                self.assertEqual(d["box"], "pbox")
                self.assertEqual(d["sender"], "boss", "sender 必须是去掉（…）括注的短格式")
                self.assertEqual(d["subject"], f"详情往返 {name}", "subject 是信头原值")
                self.assertEqual(d["need"], need, "need 是信头原值")
                self.assertEqual(d["body"], self.stored_body(path),
                                 "body 必须是文件正文去掉最末一个换行，内部换行原样")
                if name == "script":
                    self.assertEqual(d["body"], "<script>alert(1)</script>",
                                     "API 层不许转义 HTML")
                if name == "html":
                    self.assertIn("&", d["body"], "API 层不许转义 &")

    def test_detail_rejects_illegal_ids_and_bad_params_as_bad_request(self):
        lid, path = self.send("pbox", "非法参数对照")
        victim = self.home / "pbox" / "secret.md"          # 不在 inbox/ 里，绝不该被读到
        victim.write_text("SECRET-OUTSIDE-INBOX-7f3a\n", encoding="utf-8")
        victim_bytes = victim.read_bytes()
        cases = [
            "/api/letter",                                                # 两个参数都缺
            "/api/letter?box=pbox",                                       # 缺 id
            "/api/letter?box=pbox&id=",                                   # 空 id
            "/api/letter?id=" + urllib.parse.quote(lid, safe=""),         # 缺 box
            "/api/letter?box=&id=" + urllib.parse.quote(lid, safe=""),    # 空 box
            f"/api/letter?box=pbox&id={urllib.parse.quote('../x', safe='')}",
            f"/api/letter?box=pbox&id={urllib.parse.quote('x/y', safe='')}",
            f"/api/letter?box=pbox&id={urllib.parse.quote('../secret', safe='')}",
            "/api/letter?box=pbox&id=x.md",
            "/api/letter?box=pbox&id=*",
            f"/api/letter?box=pbox&id={urllib.parse.quote('/etc/passwd', safe='')}",
            "/api/letter?box=pbox&id=%2e%2e%2fx",
            "/api/letter?box=pbox&id=%2e%2e%2fsecret",
        ]
        for path_q in cases:
            with self.subTest(qs=path_q):
                status, d = self.jget(path_q)
                self.assertEqual(status, 400, f"{path_q}: {d}")
                self.assertEqual(d, {"ok": False, "error": "bad_request"}, path_q)
                self.assertNotIn("SECRET-OUTSIDE-INBOX", json.dumps(d),
                                 "被拒的读取不许泄露 inbox 之外的文件")
        self.assertEqual(victim.read_bytes(), victim_bytes, "被拒的读取不许改文件")

    def test_detail_unknown_box_and_missing_letter_are_not_found(self):
        lid, _ = self.send("pbox", "存在对照")
        queries = [
            f"/api/letter?box=ghost&id={urllib.parse.quote(lid, safe='')}",
            f"/api/letter?box=pbox&id={urllib.parse.quote('20990101-000000_boss_无此信', safe='')}",
            f"/api/letter?box={urllib.parse.quote('../pbox', safe='')}&id={urllib.parse.quote(lid, safe='')}",
        ]
        for path_q in queries:
            with self.subTest(qs=path_q):
                status, d = self.jget(path_q)
                self.assertEqual(status, 404, f"{path_q}: {d}")
                self.assertEqual(d, {"ok": False, "error": "not_found"}, path_q)

    def test_detail_reads_only_inbox_not_done_or_archived(self):
        lid, path = self.send("pbox", "只看 inbox")
        status, d = self.jget(self.detail_url("pbox", lid))
        self.assertEqual(status, 200, d)

        done_dir = self.home / "pbox" / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, done_dir / f"{lid}.md")
        status, d = self.jget(self.detail_url("pbox", lid))
        self.assertEqual(status, 404, "done/ 里的信不许被详情接口读到")
        self.assertEqual(d, {"ok": False, "error": "not_found"})

        arch = self.home / "pbox" / "archived" / "20260101-000000"
        arch.mkdir(parents=True, exist_ok=True)
        os.replace(done_dir / f"{lid}.md", arch / f"{lid}.md")
        status, d = self.jget(self.detail_url("pbox", lid))
        self.assertEqual(status, 404, "archived/ 里的信不许被详情接口读到")
        self.assertEqual(d, {"ok": False, "error": "not_found"})

    def test_detail_is_a_pure_read(self):
        lid, _ = self.send("pbox", "纯读快照")
        status, d = self.jget(self.detail_url("pbox", lid))
        self.assertEqual(status, 200, f"正面对照必须先成功：{d}")   # 否则纯读断言是空转
        before = self.snapshot()
        for path_q in [self.detail_url("pbox", lid),
                       "/api/letter?box=ghost&id=nope",
                       "/api/letter?box=pbox&id=%2e%2e%2fx",
                       "/api/letter"]:
            self.jget(path_q)
        self.assertEqual(self.snapshot(), before, "详情接口必须纯读，连一个字节都不许写")
        for rel in ("pbox/.presented.json", "pbox/.seen", "pbox/.claims",
                    "pbox/.wake_times", "opencode_delivered.jsonl", ".woken.json",
                    ".delivered.json"):
            self.assertFalse((self.home / rel).exists(), f"纯读不许产生唤醒/认领/台账文件 {rel}")

    # ==================================================================== 单封归档
    def test_archive_one_moves_then_reports_already(self):
        ids = [self.send("arcbox", f"归档信{i}")[0] for i in range(3)]
        a = ids[0]
        src = self.home / "arcbox" / "inbox" / f"{a}.md"
        before = src.read_bytes()

        status, d = self.jpost("/api/archive-one", {"box": "arcbox", "id": a})
        self.assertEqual(status, 200, d)
        self.assertEqual(d, {"ok": True, "state": "moved"})
        dst = self.home / "arcbox" / "done" / f"{a}.md"
        self.assertTrue(dst.is_file(), "归档后信必须在 done/")
        self.assertEqual(dst.read_bytes(), before, "归档必须一字节不改")
        self.assertEqual(self.inbox("arcbox"), sorted(f"{x}.md" for x in ids[1:]),
                         "另外两封必须原地不动")

        status, d = self.jpost("/api/archive-one", {"box": "arcbox", "id": a})
        self.assertEqual(status, 200, d)
        self.assertEqual(d, {"ok": True, "state": "already"}, "重复归档必须幂等")
        self.assertEqual(self.inbox("arcbox"), sorted(f"{x}.md" for x in ids[1:]))
        self.assertEqual(self.done("arcbox"), [f"{a}.md"], "幂等调用不许产生第二份")

    def test_archive_one_conflict_keeps_both_sides_untouched(self):
        lid, path = self.send("arc2", "撞名信")
        inbox_before = path.read_bytes()
        done = self.home / "arc2" / "done" / f"{lid}.md"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("done 里已有同名但内容不同\n", encoding="utf-8")
        done_before = done.read_bytes()

        status, d = self.jpost("/api/archive-one", {"box": "arc2", "id": lid})
        self.assertEqual(status, 409, d)
        self.assertEqual(d, {"ok": False, "error": "conflict"})
        self.assertEqual(path.read_bytes(), inbox_before, "冲突时 inbox 不许被动")
        self.assertEqual(done.read_bytes(), done_before, "冲突时 done 里的同名不许被覆盖")

        # 同名且内容完全相同也是冲突：两边都有就 fail closed，绝不覆盖（票面冻结）
        lid2, path2 = self.send("arc2", "撞名信二")
        before2 = path2.read_bytes()
        done2 = self.home / "arc2" / "done" / f"{lid2}.md"
        done2.write_bytes(before2)
        status, d = self.jpost("/api/archive-one", {"box": "arc2", "id": lid2})
        self.assertEqual(status, 409, d)
        self.assertEqual(d, {"ok": False, "error": "conflict"})
        self.assertEqual(path2.read_bytes(), before2, "同内容冲突时 inbox 不许被动")
        self.assertEqual(done2.read_bytes(), before2, "同内容冲突时 done 不许被重写")

    def test_archive_one_missing_and_invalid_requests_mutate_nothing(self):
        lid, path = self.send("arc3", "非法归档对照")
        before = self.snapshot()
        cases = [
            ({}, 400),
            ({"box": "arc3"}, 400),
            ({"id": lid}, 400),
            ({"box": "", "id": lid}, 400),
            ({"box": "arc3", "id": ""}, 400),
            ({"box": "arc3", "id": "xx/../y"}, 400),
            ({"box": "arc3", "id": "../x"}, 400),
            ({"box": "arc3", "id": f"{lid}.md"}, 400),
            ({"box": "ghost", "id": lid}, 404),
            ({"box": "arc3", "id": "20990101-000000_boss_无此信"}, 404),
        ]
        for payload, want in cases:
            with self.subTest(payload=payload):
                status, d = self.jpost("/api/archive-one", payload)
                self.assertEqual(status, want, f"{payload}: {d}")
                self.assertEqual(d, {"ok": False,
                                     "error": "bad_request" if want == 400 else "not_found"}, payload)
        self.assertEqual(self.snapshot(), before, "被拒的归档请求不许动任何文件")

    def test_archive_one_keeps_the_panel_guards(self):
        good, _ = self.send("guardbox", "守卫正面对照")
        status, d = self.jpost("/api/archive-one", {"box": "guardbox", "id": good})
        self.assertEqual(status, 200, f"正面对照必须先成功：{d}")

        keep, keep_path = self.send("guardbox", "守卫测试组")
        keep_before = keep_path.read_bytes()
        body = json.dumps({"box": "guardbox", "id": keep}, ensure_ascii=False).encode("utf-8")
        status, _ = self.raw("POST", "/api/archive-one", body, {})
        self.assertEqual(status, 403, "没有 application/json 必须拒绝")
        status, _ = self.raw("POST", "/api/archive-one", body,
                             {"Content-Type": "application/json", "Origin": "http://evil.example"})
        self.assertEqual(status, 403, "外部 Origin 必须拒绝")
        status, _ = self.raw("POST", "/api/nope", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403, "未知写路径必须拒绝")
        self.assertTrue(keep_path.is_file(), "被守卫拒绝时信必须留在 inbox")
        self.assertEqual(keep_path.read_bytes(), keep_before)

    def test_malformed_box_names_fail_closed_even_if_routes_was_hand_edited(self):
        """routes.json 被手改出 `../victim` 这类键时，新接口也必须 fail closed、不碰箱外文件。"""
        routes = self.home / "routes.json"
        original = routes.read_text(encoding="utf-8")
        victim_dir = BASE / "victim" / "inbox"
        victim_dir.mkdir(parents=True, exist_ok=True)
        victim = victim_dir / "whatever.md"
        victim.write_text("SECRET-VICTIM-CONTENT\n", encoding="utf-8")
        data = json.loads(original)
        data["../victim"] = {"status": "online", "methods": ["notify"]}
        routes.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            status, d = self.jget("/api/letter?box=..%2Fvictim&id=whatever")
            self.assertEqual(status, 404, f"手改 routes 里的非法信箱名必须 fail closed：{d}")
            self.assertEqual(d, {"ok": False, "error": "not_found"})
            self.assertNotIn("SECRET-VICTIM-CONTENT", json.dumps(d))
            status, d = self.jpost("/api/archive-one", {"box": "../victim", "id": "whatever"})
            self.assertEqual(status, 404, f"单封归档同样必须拒绝：{d}")
            self.assertEqual(d, {"ok": False, "error": "not_found"})
            self.assertTrue(victim.is_file(), "被拒的归档不许动到邮局之外的文件")
            self.assertEqual(victim.read_text(encoding="utf-8"), "SECRET-VICTIM-CONTENT\n")
        finally:
            routes.write_text(original, encoding="utf-8")

    def test_clear_still_archives_the_whole_inbox_not_done(self):
        ids = [self.send("clearbox", f"清空信{i}")[0] for i in range(2)]
        status, _ = self.jpost("/api/clear", {"name": "clearbox"})
        self.assertEqual(status, 200)
        self.assertEqual(self.inbox("clearbox"), [])
        self.assertEqual(self.done("clearbox"), [], "clear 走 archived/，不是 done/")
        arch = self.home / "clearbox" / "archived"
        self.assertEqual(sorted(p.name for p in arch.rglob("*.md")),
                         sorted(f"{x}.md" for x in ids), "clear 必须把整箱存档")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
