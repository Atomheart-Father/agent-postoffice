#!/usr/bin/env python3
"""组织/身份标准化 v1（PO-ORG-IDENTITY-V1）公开行为测试。

只测公开 seam：config import 校验、`identity` 导出、`add` 的「只改资料 / 显式改绑」、
CONTACT 名片与面板 /api/state 的一致性。全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，
只连 127.0.0.1，绝不碰真实数据目录。
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
BASE = Path(tempfile.mkdtemp(prefix="po_org_"))


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


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def routes_of(home):
    p = home / "routes.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


ORG2 = {
    "version": 2,
    "groups": {"team": ["dev", "reviewer"]},
    "aliases": {
        "proj.owner": {"candidates": ["dev", "backup"],
                       "role": {"scope": {"kind": "project", "id": "alpha"}, "title": "开发"}},
        "proj.review": {"candidates": ["reviewer"],
                        "role": {"scope": {"kind": "project", "id": "alpha"}, "title": "审核"}},
        "co.host": {"candidates": ["dev"],
                    "role": {"scope": {"kind": "company", "id": "boxz"}, "title": "主持"}},
    },
    "organization": {
        "companies": {"boxz": {"name": "盒子", "rules_file": "rules.md",
                               "members": ["dev", "reviewer", "backup"]}},
        "projects": {"alpha": {"company_id": "boxz", "name": "Alpha", "root": "/tmp/alpha",
                               "status_file": "STATUS.md",
                               "members": ["dev", "reviewer", "backup"]}},
    },
}


def import_config(home, cfg):
    src = home / "incoming.json"
    src.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return run_po("config", "import", str(src), home=home)


class OrgIdentity(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        for box in ("dev", "reviewer", "backup", "sbox", "boss"):
            out = run_po("add", box, "--notify", home=self.home)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def identity(self, box):
        out = run_po("identity", box, "--json", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout)

    # ---- config import validation ---------------------------------------
    def test_valid_v2_org_imports(self):
        out = import_config(self.home, ORG2)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("已导入配置", out.stdout)

    def test_bad_refs_rejected_and_old_config_untouched(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        before = (self.home / "config.json").read_bytes()
        bads = [
            # company_id points nowhere
            {**ORG2, "organization": {"companies": ORG2["organization"]["companies"],
                                      "projects": {"alpha": {**ORG2["organization"]["projects"]["alpha"],
                                                             "company_id": "nope"}}}},
            # a member is not a registered box
            {**ORG2, "organization": {"companies": {"boxz": {"name": "盒子", "rules_file": "rules.md",
                                                             "members": ["dev", "ghost"]}},
                                      "projects": ORG2["organization"]["projects"]}},
            # a project member is not in the owning company
            {**ORG2, "organization": {"companies": ORG2["organization"]["companies"],
                                      "projects": {"alpha": {**ORG2["organization"]["projects"]["alpha"],
                                                             "members": ["dev", "ghost"]}}}},
            # role scope points at an unknown project
            {**ORG2, "aliases": {**ORG2["aliases"],
                                 "proj.owner": {"candidates": ["dev"],
                                                "role": {"scope": {"kind": "project", "id": "ghost"},
                                                         "title": "开发"}}}},
            # project-scope role may not carry its own handoff_file
            {**ORG2, "aliases": {**ORG2["aliases"],
                                 "proj.owner": {"candidates": ["dev"],
                                                "role": {"scope": {"kind": "project", "id": "alpha"},
                                                         "title": "开发", "handoff_file": "h.md"}}}},
            # organization is not allowed in version 1
            {"version": 1, "groups": {}, "aliases": {},
             "organization": ORG2["organization"]},
        ]
        for bad in bads:
            out = import_config(self.home, bad)
            self.assertNotEqual(out.returncode, 0, f"应当拒绝：{bad}")
            self.assertIn("配置没通过校验", out.stdout + out.stderr)
            self.assertEqual((self.home / "config.json").read_bytes(), before,
                             "校验失败不许改动已发布的配置")

    def test_v1_config_without_org_still_valid(self):
        out = import_config(self.home, {"version": 1, "groups": {}, "aliases": {}})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    # ---- identity snapshot ----------------------------------------------
    def test_identity_active_vs_candidate(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        dev = self.identity("dev")
        self.assertTrue(dev["registered"])
        mem = {(m["kind"], m["id"]) for m in dev["memberships"]}
        self.assertIn(("company", "boxz"), mem)
        self.assertIn(("project", "alpha"), mem)
        act = {r["alias"]: r for r in dev["active_roles"]}
        self.assertIn("proj.owner", act)
        self.assertEqual(act["proj.owner"]["active_target"], "dev")
        self.assertEqual(act["proj.owner"]["title"], "开发")
        self.assertIn("co.host", act)
        self.assertEqual(dev["candidate_only_roles"], [])

        backup = self.identity("backup")
        self.assertEqual(backup["active_roles"], [])
        self.assertEqual({r["alias"] for r in backup["candidate_only_roles"]}, {"proj.owner"})

        ghost = self.identity("ghostish")
        self.assertFalse(ghost["registered"])

    def test_identity_org_invalid_is_diagnosable_not_fatal(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        cfg["organization"]["projects"]["alpha"]["company_id"] = "gone"
        (self.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        dev = self.identity("dev")
        self.assertFalse(dev["config_ok"])
        self.assertTrue(dev["error"])
        self.assertEqual(dev["active_roles"], [])   # no fabricated duties while org is invalid
        self.assertEqual(dev["binding"], dev["binding"])  # physical facts still present

    # ---- add: profile-only vs explicit rebind ---------------------------
    def test_profile_only_update_does_not_rebind(self):
        out = run_po("add", "dev", "--desc", "旧的", home=self.home)  # existing box, no channel
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("已更新资料", out.stdout)
        r = routes_of(self.home)["dev"]
        self.assertEqual(r["methods"], ["notify"])
        self.assertTrue((self.home / "dev" / "CONTACT.md").read_text(encoding="utf-8")
                        .find("按身份查") >= 0)

    def test_explicit_rebind_changes_binding_and_records_handoff(self):
        out = run_po("add", "dev", "--claude", "我的窗口", "--claude-session", "S2",
                     "--source", "user", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("已重绑", out.stdout)
        r = routes_of(self.home)["dev"]
        self.assertEqual(r["methods"], ["claude_hook"])
        self.assertEqual(r["claude_title"], "我的窗口")
        self.assertEqual(r["claude_session"], "S2")
        rec = json.loads((self.home / "logs" / "rebind.log").read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(rec["box"], "dev")
        self.assertEqual(rec["to"], "claude_hook:S2")
        self.assertEqual(rec["source"], "user")

    def test_new_box_requires_a_channel(self):
        out = run_po("add", "brandnew", home=self.home)
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("brandnew", routes_of(self.home))

    # ---- bad organization must not disable valid alias routing ----------
    def test_bad_org_does_not_disable_alias_routing(self):
        # a broken organization is rejected at IMPORT time (whole-file gate) ...
        bad = json.loads(json.dumps(ORG2))
        bad["organization"]["companies"]["boxz"]["members"] = ["dev", "ghost"]
        self.assertNotEqual(import_config(self.home, bad).returncode, 0)
        # ... but a hand-edited file at RUNTIME only disables the identity ornament
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        self.assertTrue(self.identity("dev")["config_ok"])
        (self.home / "config.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        out = run_po("send", "@proj.owner", "sbox", "路由仍然有效", "仅告知", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(list((self.home / "dev" / "inbox").glob("*.md")),
                        "坏 organization 不许拖停合法 @alias 的路由")
        self.assertEqual(run_po("list", home=self.home).returncode, 0)   # physical mail unaffected
        dev = self.identity("dev")
        self.assertFalse(dev["config_ok"])
        self.assertTrue(dev["error"])
        self.assertEqual(dev["active_roles"], [])                       # no fabricated duties
        # fixing the organization restores identity
        (self.home / "config.json").write_text(json.dumps(ORG2, ensure_ascii=False), encoding="utf-8")
        fixed = self.identity("dev")
        self.assertTrue(fixed["config_ok"])
        self.assertIn("proj.owner", [r["alias"] for r in fixed["active_roles"]])

    # ---- CONTACT must not persist a dynamic duty / current-owner copy -----
    def test_contact_has_no_dynamic_duty_copy(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        run_po("add", "dev", "--desc", "regenerate card", home=self.home)
        contact = (self.home / "dev" / "CONTACT.md").read_text(encoding="utf-8")
        for banned in ("现职", "可候选", "受理", "当前由"):
            self.assertNotIn(banned, contact, f"名片不许留动态任职副本：{banned}")
        self.assertIn("postoffice identity dev", contact)   # query entry stays

    # ---- a non-member fallback successor still gets scope pointers --------
    def test_fallback_successor_gets_scope_pointers_without_membership(self):
        fb = {
            "version": 2, "groups": {},
            "aliases": {"p.review": {"candidates": ["boss", "dev"],
                                     "role": {"scope": {"kind": "project", "id": "alpha"},
                                              "title": "审核"}}},
            "organization": {
                "companies": {"boxz": {"name": "盒子", "rules_file": "/tmp/rules.md", "members": ["dev"]}},
                "projects": {"alpha": {"company_id": "boxz", "name": "Alpha", "root": "/tmp/alpha",
                                       "status_file": "STATUS.md", "members": ["dev"]}},
            },
        }
        self.assertEqual(import_config(self.home, fb).returncode, 0)
        boss = self.identity("boss")
        self.assertEqual([r["alias"] for r in boss["active_roles"]], ["p.review"])
        self.assertEqual(boss["memberships"], [], "fallback 接任不许被自动算成成员")
        self.assertEqual([c["path"] for c in boss["company_rules"]], ["/tmp/rules.md"])
        self.assertEqual([c["path"] for c in boss["project_status"]], ["/tmp/alpha/STATUS.md"])

    # ---- role switch handoff derives from the scope, not spec.handoff ------
    def test_role_alias_handoff_derives_from_scope(self):
        fb = json.loads(json.dumps(ORG2))
        fb["organization"]["projects"]["alpha"]["root"] = "/tmp/alpha"
        self.assertEqual(import_config(self.home, fb).returncode, 0)
        out = run_po("config", "show", home=self.home)
        # a valid project role carries BOTH the owning company's rules and the project STATUS
        self.assertIn("交接路径：rules.md、/tmp/alpha/STATUS.md", out.stdout)

    # ---- 修复1：稳定切换也必须校验组织；坏组织不写 STATUS，合法路由照常 ------
    def _run_rounds(self, rounds=4, seconds=1.6, stable=1):
        """跑若干轮邮递员（稳定期 1 秒），让别名切换真正确认并走完交接流程。"""
        env = env_for(self.home, POSTOFFICE_ALIAS_STABLE=str(stable))
        for _ in range(rounds):
            p = subprocess.Popen([sys.executable, PO, "postman"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(seconds)
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def _handoff_notes(self):
        out = []
        for box in ("dev", "backup", "reviewer"):
            for n in (self.home / box / "inbox").glob("*.md"):
                t = n.read_text(encoding="utf-8")
                if "交接：" in t:
                    out.append(t)
        return out

    def test_role_switch_handoff_valid_carries_rules_and_status_broken_org_carries_neither(self):
        fb = json.loads(json.dumps(ORG2))
        fb["organization"]["projects"]["alpha"]["root"] = "/tmp/alpha"
        self.assertEqual(import_config(self.home, fb).returncode, 0)
        self._run_rounds()                      # 基线：@proj.owner → dev
        run_po("offline", "dev", home=self.home)
        self._run_rounds()                      # 目标换成 backup → 确认切换 + 交接
        notes = self._handoff_notes()
        self.assertTrue(notes, "有效组织下角色切换应写出交接提醒")
        self.assertTrue(any("rules.md" in t and "STATUS.md" in t for t in notes),
                        f"有效角色交接要带 rules 与 STATUS：{notes}")
        # 现在破坏组织（project.company_id 指向不存在的公司），再切一次
        for box in ("dev", "backup", "reviewer"):
            for n in (self.home / box / "inbox").glob("*.md"):
                if "交接：" in n.read_text(encoding="utf-8"):
                    n.unlink()
        cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        cfg["organization"]["projects"]["alpha"]["company_id"] = "gone"
        (self.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        run_po("online", "dev", home=self.home)      # 目标从 backup 变回 dev → 第二次切换
        self._run_rounds()
        # 逻辑地址路由照常（坏组织不污染 routing）
        st = run_po("config", "show", home=self.home)
        self.assertEqual(st.returncode, 0, st.stdout + st.stderr)
        self.assertIn("组织身份已停用", st.stdout)
        broken_notes = self._handoff_notes()
        self.assertTrue(broken_notes, "坏组织下路由仍照常切换（切换本身必须继续）")
        for t in broken_notes:
            self.assertIn("组织身份已停用", t, "坏组织的交接要说明为什么留空")
            self.assertNotIn("STATUS.md", t, "坏组织绝不许写出已停用的 STATUS 引用")

    def test_role_plus_alias_handoff_is_rejected(self):
        conflict = json.loads(json.dumps(ORG2))
        conflict["aliases"]["proj.owner"]["handoff"] = "/tmp/other.md"
        out = import_config(self.home, conflict)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("role", out.stdout + out.stderr)

    # ---- rebind must not record a wrong STATUS from a broken org ---------
    def test_rebind_handoff_empty_and_explained_when_org_invalid(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        cfg["organization"]["projects"]["alpha"]["company_id"] = "gone"
        (self.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        out = run_po("rebind", "dev", "--claude", "窗口", "--claude-session", "S9", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("组织身份已停用", out.stdout)
        rec = json.loads((self.home / "logs" / "rebind.log").read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(rec["handoff"], "")

    # ---- harness seams: registration hint, resume hint, first-read convention ----
    def test_registration_prints_identity_hint_once(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        out = run_po("add", "dev", "--claude", "窗口", "--claude-session", "S1", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("身份（dev）", out.stdout)
        self.assertIn("现职", out.stdout)
        self.assertIn("proj.owner", out.stdout)
        self.assertIn("/tmp/alpha/STATUS.md", out.stdout)

    def test_identity_hint_resolves_from_claude_payload(self):
        cfg = json.loads(json.dumps(ORG2))
        cfg["aliases"]["c.owner"] = {"candidates": ["cbox"],
                                     "role": {"scope": {"kind": "company", "id": "boxz"}, "title": "主持"}}
        cfg["organization"]["companies"]["boxz"]["members"].append("cbox")
        run_po("add", "cbox", "--claude", "T", "--claude-session", "CS1", home=self.home)
        self.assertEqual(import_config(self.home, cfg).returncode, 0)
        out = run_po("identity", "--hint", home=self.home, stdin='{"session_id":"CS1"}')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("身份（cbox）", out.stdout)
        self.assertIn("c.owner", out.stdout)
        # an unresolvable payload prints nothing at all (fail-soft, never guesses a box)
        blank = run_po("identity", "--hint", home=self.home, stdin='{"session_id":"nope"}')
        self.assertEqual(blank.returncode, 0)
        self.assertEqual(blank.stdout.strip(), "")

    def test_claude_install_adds_resume_hint_hook(self):
        home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        fake_home = home / "fakehome"
        (fake_home / ".claude").mkdir(parents=True)
        env = env_for(self.home, HOME=str(fake_home))
        out = subprocess.run([sys.executable, PO, "install", "claude"], capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        cmds = [h.get("command", "") for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
        self.assertTrue(any("identity --hint" in c for c in cmds), cmds)
        self.assertTrue(any(c.rstrip().endswith(" hook") for c in cmds), cmds)

    def test_ordinary_letter_carries_no_rules_or_roles(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        run_po("send", "sbox", "dev", "普通事由", "仅告知", stdin="正文正文\n", home=self.home)
        letter = next((self.home / "sbox" / "inbox").glob("*.md")).read_text(encoding="utf-8")
        for banned in ("公司规章", "现职", "po.owner", "/tmp/alpha/STATUS.md", "rules.md"):
            self.assertNotIn(banned, letter, f"普通信不许夹带身份/规则：{banned}")

    # ---- 修复3：hint 与面板都要说清「坏组织为什么停用」，并区分未配置 ------
    def test_identity_hint_explains_broken_org_and_stays_silent_when_unconfigured(self):
        # 未配置 organization → hint 沉默（普通安装行为不变）
        self.assertEqual(import_config(self.home, {"version": 1, "groups": {}, "aliases": {}}).returncode, 0)
        silent = run_po("identity", "dev", "--hint", home=self.home)
        self.assertEqual(silent.returncode, 0, silent.stdout + silent.stderr)
        self.assertEqual(silent.stdout.strip(), "", f"没配组织就不该有提示：{silent.stdout!r}")
        # 坏组织 → hint 明确报停用原因，且原因非空、不是「组织身份已停用（）」
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        cfg["organization"]["projects"]["alpha"]["company_id"] = "gone"
        (self.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        broken = run_po("identity", "dev", "--hint", home=self.home)
        self.assertEqual(broken.returncode, 0, broken.stdout + broken.stderr)
        self.assertIn("组织配置已停用", broken.stdout)
        self.assertNotIn("组织配置已停用（）", broken.stdout)
        self.assertRegex(broken.stdout.strip(), r"组织配置已停用（.+）", "停用必须带真实原因")

    def _panel_state(self):
        """Boot a real panel in this temp home, read /api/state once, stop it again."""
        port = free_port()
        proc = subprocess.Popen([sys.executable, PO, "panel", "--no-open", "--port", str(port)],
                                env=env_for(self.home), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                self.assertIsNone(proc.poll(), "面板进程提前退出")
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/api/state")
                    resp = conn.getresponse()
                    body = resp.read()
                    conn.close()
                    if resp.status == 200:
                        return json.loads(body.decode("utf-8"))
                except OSError:
                    pass
                time.sleep(0.2)
            self.fail("面板没有在 15 秒内起来")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    # ---- 修复3：面板也要说清「坏组织为什么停用」，并区分未配置 ------
    def test_panel_reports_org_disabled_reason_and_unconfigured_is_silent(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        cfg["organization"]["projects"]["alpha"]["company_id"] = "gone"
        (self.home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        ident = next(b for b in self._panel_state()["boxes"] if b["name"] == "dev")["identity"]
        self.assertTrue(ident["org_present"], "配了组织就要露出来，哪怕它是坏的")
        self.assertFalse(ident["config_ok"])
        self.assertTrue(ident["error"].strip(), "面板必须带真实停用原因")
        self.assertEqual(ident["active_roles"], [], "坏组织不许编出职责")
        # 普通投递完全不受影响
        out = run_po("send", "dev", "backup", "普通信", "仅告知", stdin="正文\n", home=self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(list((self.home / "dev" / "inbox").glob("*.md")))
        # 完全没配组织 → 不出现组织呈现
        self.assertEqual(import_config(self.home, {"version": 1, "groups": {}, "aliases": {}}).returncode, 0)
        row = next(b for b in self._panel_state()["boxes"] if b["name"] == "dev")
        self.assertFalse(row["identity"]["org_present"], "没配组织就不该显示组织身份")

    # ---- panel + CONTACT share the one identity -------------------------
    def test_panel_and_contact_match_identity(self):
        self.assertEqual(import_config(self.home, ORG2).returncode, 0)
        run_po("add", "dev", "--display-name", "开发者", home=self.home)
        state = self._panel_state()
        row = next(b for b in state["boxes"] if b["name"] == "dev")
        self.assertEqual(row["who"], "开发者")
        self.assertIn("identity", row)
        self.assertEqual({r["alias"] for r in row["identity"]["active_roles"]},
                         {r["alias"] for r in self.identity("dev")["active_roles"]})
        contact = (self.home / "dev" / "CONTACT.md").read_text(encoding="utf-8")
        self.assertIn("开发者", contact)
        self.assertIn("postoffice identity dev", contact)


if __name__ == "__main__":
    unittest.main(verbosity=2)
