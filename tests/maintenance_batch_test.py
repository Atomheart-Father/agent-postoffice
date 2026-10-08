#!/usr/bin/env python3
"""邮局维护批次 2026-10-08 的公开行为测试（A T2替补/升级、B STATUS新鲜度、C 文件名/引用、D 精确归档）。

只走公共 seam：CLI send / identity / archive / archive-current / config import，以及邮递员轮次。
全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，只连本机，绝不碰真实 ~/agent-postoffice。

约定：升级语义只对显式配置了 escalation_role 的 alias 生效；没有该字段的旧 alias 行为不变
（现有 hierarchy_test.py 继续钉住这一点）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_maint_"))
ZI = "仅告知"

ALL_BOXES = ["impl", "impl2", "sup", "sup2", "boss", "sbox", "notifybox", "audit"]


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    for k in ("POSTOFFICE_PANEL_BASE_URL", "POSTOFFICE_SLACK_WEBHOOK",
              "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_HOST_SESSION_ID"):
        env.pop(k, None)
    env.update(extra)
    return env


def run_po(*args, home, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home), input=stdin, timeout=timeout)


def anon_config(home):
    rules = home / "rules.md"
    rules.write_text("# 规章\nAS_OF: 2020-01-01\nSOURCE: evidence.md\n", encoding="utf-8")
    (home / "evidence.md").write_text("证据\n", encoding="utf-8")
    return {
        "version": 2,
        "groups": {},
        "aliases": {
            "p.impl": {"candidates": ["impl", "impl2"], "escalation_role": "p.design",
                       "notify": ["notifybox"],
                       "role": {"scope": {"kind": "project", "id": "pj"}, "title": "IMPLEMENT"}},
            "p.design": {"candidates": ["sup", "sup2", "boss"],
                         "role": {"scope": {"kind": "project", "id": "pj"}, "title": "DESIGN"}},
            "p.plain": {"candidates": ["sup", "boss"]},
        },
        "organization": {
            "companies": {"co": {"name": "Co", "rules_file": str(rules),
                                 "members": list(ALL_BOXES)}},
            "projects": {"pj": {"company_id": "co", "name": "Pj", "root": str(home),
                                "status_file": "STATUS.md", "members": list(ALL_BOXES)}},
        },
    }


class MaintenanceBatch(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ALL_BOXES:
            out = run_po("add", box, "--notify", home=self.home)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    # -- helpers ---------------------------------------------------------
    def import_config(self, cfg):
        src = self.home / "incoming.json"
        src.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return run_po("config", "import", str(src), home=self.home)

    def offline(self, name):
        self.assertEqual(run_po("offline", name, home=self.home).returncode, 0)

    def online(self, name):
        self.assertEqual(run_po("online", name, home=self.home).returncode, 0)

    def send(self, alias, sender, subject, body="正文\n"):
        return run_po("send", alias, sender, subject, ZI, home=self.home, stdin=body)

    def target_of(self, path):
        return Path(path).parent.parent.name

    def delivered(self, out):
        line = next((l for l in out.stdout.splitlines() if l.startswith("已投递：")), None)
        self.assertIsNotNone(line, out.stdout + out.stderr)
        return Path(line.split("：", 1)[1].strip())

    def ident(self, box):
        out = run_po("identity", box, "--json", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout)

    def header_field(self, path, key):
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        for line in text.split("\n\n", 1)[0].splitlines():
            if line.startswith(key):
                return line[len(key):].strip()
        return ""

    def round_(self, seconds=1.8, stable=1):
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

    def wait_until(self, pred, what, max_rounds=8):
        for i in range(max_rounds):
            self.round_()
            if pred():
                return i + 1
        self.fail(f"等了 {max_rounds} 轮仍未满足：{what}")

    def letters_with(self, box, key):
        d = self.home / box / "inbox"
        out = []
        for p in sorted(d.glob("*.md")) if d.is_dir() else []:
            if self.header_field(p, key):
                out.append(p)
        return out

    # ==================================================================== A
    def test_a_qualified_execution_then_single_upgrade(self):
        self.assertEqual(self.import_config(anon_config(self.home)).returncode, 0)

        p = self.delivered(self.send("@p.impl", "sbox", "全员在线"))
        self.assertEqual(self.target_of(p), "impl")
        self.assertEqual(self.header_field(p, "升级受理："), "", "有执行者时不许出现升级头")

        self.offline("impl")
        p = self.delivered(self.send("@p.impl", "sbox", "首选离线走次选"))
        self.assertEqual(self.target_of(p), "impl2", "同组显式合格替补优先，不升级")

        self.offline("impl2")
        p = self.delivered(self.send("@p.impl", "sbox", "执行组全离线→升级"))
        self.assertEqual(self.target_of(p), "sup", "执行候选全离线才升级到 design 的首选")
        self.assertIn("@p.design", self.header_field(p, "升级受理："))
        # 升级受理者获得的是自己（DESIGN）的现职，绝不是原 T2 的 IMPLEMENT
        sup = self.ident("sup")
        self.assertEqual([r["alias"] for r in sup["active_roles"]], ["p.design"])
        self.assertNotIn("p.impl", [r["alias"] for r in sup["active_roles"] + sup["candidate_only_roles"]])

        self.offline("sup")
        p = self.delivered(self.send("@p.impl", "sbox", "design 首选离线"))
        self.assertEqual(self.target_of(p), "sup2", "上级按自己的候选接任")

        self.offline("sup2")
        p = self.delivered(self.send("@p.impl", "sbox", "design 仅剩兜底"))
        self.assertEqual(self.target_of(p), "boss", "上级候选兜底到 boss")

        # 兜底到 boss 也只给 DESIGN，不给原 T2 岗位
        self.assertEqual([r["alias"] for r in self.ident("boss")["active_roles"]], ["p.design"])
        impl = self.ident("impl")
        self.assertEqual(impl["active_roles"], [])
        self.assertEqual([r["alias"] for r in impl["candidate_only_roles"]], ["p.impl"])
        self.assertIn("escalation", impl["candidate_only_roles"][0])

        # 执行与上级全离线 → 发送失败，不投给任何人
        self.offline("boss")
        before = {b: sorted(x.name for x in (self.home / b / "inbox").glob("*.md")) for b in ALL_BOXES}
        out = self.send("@p.impl", "sbox", "全离线")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("没有在线信箱", out.stderr)
        after = {b: sorted(x.name for x in (self.home / b / "inbox").glob("*.md")) for b in ALL_BOXES}
        self.assertEqual(before, after, "失败发送不许留下任何信")

    def test_a_old_alias_without_escalation_keeps_walking_candidates(self):
        self.assertEqual(self.import_config(anon_config(self.home)).returncode, 0)
        self.offline("sup")                      # p.plain candidates=[sup,boss]
        p = self.delivered(self.send("@p.plain", "sbox", "旧语义"))
        self.assertEqual(self.target_of(p), "boss")

    def test_a_org_role_error_disables_identity_not_old_routing(self):
        # A bad role DEFINITION on the escalation target is org-level: it must disable the identity
        # presentation only, never the already-legal old routing/groups. (Runtime load gates on
        # config_errors; config import gates on core+org, so a hand-edited config.json models a
        # config that was valid at import and later edited.)
        cfg = anon_config(self.home)
        self.assertEqual(self.import_config(cfg).returncode, 0)
        bad = json.loads(json.dumps(cfg))
        bad["aliases"]["p.design"]["role"]["title"] = 17          # org error, not core
        (self.home / "config.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")

        # old core routing keeps working: an independent plain alias still delivers
        p = self.delivered(self.send("@p.plain", "sbox", "组织坏但旧路由继续"))
        self.assertEqual(self.target_of(p), "sup")

        # identity presentation is disabled with a diagnosable reason (not silently "all stopped")
        ident = self.ident("impl")
        self.assertFalse(ident["config_ok"])
        self.assertIn("role", ident["error"])

    def test_a_invalid_escalation_rejected_and_old_config_untouched(self):
        self.assertEqual(self.import_config(anon_config(self.home)).returncode, 0)
        before = (self.home / "config.json").read_bytes()
        good = anon_config(self.home)

        def with_impl(esc):
            cfg = json.loads(json.dumps(good))
            if esc is None:
                cfg["aliases"]["p.impl"].pop("escalation_role", None)
            else:
                cfg["aliases"]["p.impl"]["escalation_role"] = esc
            return cfg

        bads = [
            with_impl("nope"),                    # unknown alias
            with_impl("p.impl"),                  # self
            with_impl("p.plain"),                 # not a role alias
            with_impl(123),                       # not a string
        ]
        chain = json.loads(json.dumps(good))
        chain["aliases"]["p.design"]["escalation_role"] = "p.impl"   # p.impl already escalates → chain
        bads.append(chain)
        for bad in bads:
            out = self.import_config(bad)
            self.assertNotEqual(out.returncode, 0, f"应当拒绝：{bad}")
            self.assertEqual((self.home / "config.json").read_bytes(), before,
                             "非法升级配置不许改动旧字节")

    def test_a_upgrade_switch_broadcast_and_handoff_use_upgrade_wording(self):
        self.assertEqual(self.import_config(anon_config(self.home)).returncode, 0)
        self.round_()                                          # 基线，不广播

        self.offline("impl")
        self.offline("impl2")
        self.wait_until(lambda: len(self.letters_with("notifybox", "广播：")) >= 1, "升级切换广播")
        bcasts = self.letters_with("notifybox", "广播：")
        self.assertEqual(len(bcasts), 1, "一次切换只许一封广播")
        self.assertIn("升级", bcasts[0].read_text(encoding="utf-8"))

        notes = self.letters_with("sup", "切换事件：")
        self.assertEqual(len(notes), 1, "升级目标只许一封交接提醒")
        note = notes[0].read_text(encoding="utf-8")
        self.assertIn("执行岗位无人可接", note)
        self.assertNotIn("现在由你受理", note, "升级受理不是授予执行岗位")

        # 执行者恢复 → 回落到执行目标；升级前的交接提醒不重投
        self.online("impl")
        self.wait_until(lambda: len(self.letters_with("impl", "切换事件：")) >= 1, "恢复切换交接")
        self.assertEqual(len(self.letters_with("sup", "切换事件：")), 1, "恢复不许重送已投的升级信")
        self.assertIn("现在由你受理", self.letters_with("impl", "切换事件：")[0].read_text(encoding="utf-8"))

    # ==================================================================== B
    def test_b_status_freshness_is_a_hint_from_as_of_and_source(self):
        self.assertEqual(self.import_config(anon_config(self.home)).returncode, 0)
        # rules.md: AS_OF 2020 + SOURCE evidence.md（mtime 现在）→ 来源晚于摘要，提示待刷新
        ident = self.ident("impl")
        rules = ident["company_rules"][0]
        self.assertEqual(rules["freshness"]["state"], "ok")
        self.assertTrue(rules["freshness"]["stale"])
        self.assertEqual(rules["freshness"]["as_of_text"], "2020-01-01")
        human = run_po("identity", "impl", home=self.home).stdout
        self.assertIn("来源晚于摘要", human)

        # AS_OF 改成未来 → 不再提示
        (self.home / "rules.md").write_text("# 规章\nAS_OF: 2999-01-01\nSOURCE: evidence.md\n",
                                            encoding="utf-8")
        self.assertFalse(self.ident("impl")["company_rules"][0]["freshness"]["stale"])

        # 缺文件的来源指针要如实标注
        st = self.ident("impl")["project_status"][0]
        self.assertEqual(st["freshness"]["state"], "missing")
        self.assertIn("缺文件", run_po("identity", "impl", home=self.home).stdout)

        # 没有 AS_OF 的 STATUS：可读但不判过期
        (self.home / "STATUS.md").write_text("# 进度\nCURRENT: x\n", encoding="utf-8")
        st = self.ident("impl")["project_status"][0]
        self.assertEqual(st["freshness"]["state"], "ok")
        self.assertFalse(st["freshness"]["stale"])

    def test_b_unreadable_pointer_is_a_diagnosable_hint_not_a_crash(self):
        self.assertEqual(self.import_config(anon_config(self.home)).returncode, 0)
        # non-UTF-8 STATUS bytes: the freshness hint must fail soft, never break the identity query
        (self.home / "STATUS.md").write_bytes(b"\xff\xfe bad status")
        out = run_po("identity", "impl", "--json", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)     # no traceback
        ident = json.loads(out.stdout)
        st = ident["project_status"][0]
        self.assertEqual(st["freshness"]["state"], "unavailable")
        self.assertIn("无法读取", run_po("identity", "impl", home=self.home).stdout)
        # the rest of the identity is intact (other pointer still fresh/evaluated)
        self.assertEqual(ident["company_rules"][0]["freshness"]["state"], "ok")

    # ==================================================================== C
    def test_c_new_reference_is_copy_safe(self):
        out = self.send("impl", "sbox", "LONGRUN01裁决分发①报告订正(+补给)")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lid = next(l.split("：", 1)[1].strip() for l in out.stdout.splitlines() if l.startswith("编号："))
        ref = next(l.split("：", 1)[1].strip() for l in out.stdout.splitlines() if l.startswith("引用："))
        self.assertEqual(ref, f"impl/{lid}")
        self.assertNotRegex(lid, r"[+/()×\s]", f"编号不许含难复制的标点：{lid!r}")
        self.assertRegex(lid, r"^[0-9]{8}-[0-9]{6}_sbox_\S+$")
        self.assertTrue(re.fullmatch(r"[^\s]+", ref))

    def test_c_old_punctuation_ids_still_resolve(self):
        # 旧编号（含 + ( )）仍可按精确编号归档：不批量改名、引用不退化
        old = self.home / "impl" / "inbox" / "20260101-000000_sbox_OLD+ID(x).md"
        old.write_text("来源：sbox\n事由：旧格式\n需要：仅告知\n\n老信\n", encoding="utf-8")
        out = run_po("archive", "impl", old.stem, home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue((self.home / "impl" / "done" / old.name).is_file())

    # ==================================================================== D
    def test_d_explicit_archive_by_exact_id_and_idempotent(self):
        p = self.delivered(self.send("impl", "sbox", "归档我"))
        lid = p.stem
        out = run_po("archive", "impl", lid, home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(p.exists())
        self.assertTrue((self.home / "impl" / "done" / f"{lid}.md").is_file())
        again = run_po("archive", "impl", lid, home=self.home)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("已在 done/", again.stdout)
        # 归档不是回执：不产生回执账、不发新信
        acks = self.home / "acks.jsonl"
        self.assertFalse(acks.exists() and "impl" in acks.read_text(encoding="utf-8"))

    def test_d_archive_rejects_paths_globs_crossbox_and_conflict(self):
        p = self.delivered(self.send("impl2", "sbox", "冲突"))
        lid = p.stem
        self.assertNotEqual(run_po("archive", "impl2", "../impl2/inbox/x", home=self.home).returncode, 0)
        self.assertNotEqual(run_po("archive", "impl2", "*.md", home=self.home).returncode, 0)
        self.assertNotEqual(run_po("archive", "sbox", lid, home=self.home).returncode, 0,
                            "不许把别的箱的信按本箱编号归档")
        # 冲突不覆盖
        done = self.home / "impl2" / "done" / f"{lid}.md"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("已有的同名文件\n", encoding="utf-8")
        out = run_po("archive", "impl2", lid, home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertTrue(p.exists(), "冲突时原信必须还在 inbox")
        self.assertEqual(done.read_text(encoding="utf-8"), "已有的同名文件\n", "冲突不许覆盖")

    def test_d_archive_current_does_not_touch_unpresented_new_mail(self):
        a = self.delivered(self.send("impl", "sbox", "新信一"))
        b = self.delivered(self.send("impl", "sbox", "新信二"))
        out = run_po("archive-current", "--box", "impl", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("没有待归档", out.stdout)
        self.assertTrue(a.exists() and b.exists(), "presented 为空时不许碰 inbox 里新到的信")

    def test_d_rebind_keeps_presented_set_per_box_for_successor(self):
        # Ground-truth for the "rebind orphans the presented set" feedback: the set is per-BOX
        # (impl/.presented.json), not per-session, so a real rebind does not clear it and the
        # successor's archive-current still files the old letters. Pin the actual behaviour; the
        # explicit `archive <box> <id>` entry stays available as the successor's per-letter tool.
        p = self.delivered(self.send("impl", "sbox", "重绑前的信"))
        lid = p.stem
        (self.home / "impl" / ".presented.json").write_text(json.dumps([lid]), encoding="utf-8")
        r = run_po("rebind", "impl", "--claude", "old_title", "--claude-session", "old_sid",
                   "--source", "anon-test", home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads((self.home / "routes.json").read_text())["impl"]["claude_session"],
                         "old_sid")
        self.assertEqual(json.loads((self.home / "impl" / ".presented.json").read_text()), [lid],
                         "presented 是按箱的，重绑不许清空它")
        out = run_po("archive-current", "--box", "impl", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(p.exists())
        self.assertTrue((self.home / "impl" / "done" / f"{lid}.md").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
