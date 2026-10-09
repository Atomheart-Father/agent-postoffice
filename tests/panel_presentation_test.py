#!/usr/bin/env python3
"""config.json 可选 "panel" 段 = 面板呈现真相（Red 先行，写于实现之前）。

冻结契约：
  config.json（version 1，整文件替换写入）新增可选顶层键 "panel"，只定义面板顶部的
  呈现结构；缺段时一切照旧。
  panel 结构（任何一层都只许这些键，出现未知字段即呈现错误）：
    panel        必须是对象；键恰好是 label(str)、operator(str)、organization(node)，
                 三件都必填
    node         对象：mailbox(str) 与 members(非空 str 数组) 二选一（两者都有、
                 都没有都算错）；可选 label(str)、children([node])
    名字规则     mailbox/members 里的名字必须是 routes.json 里已登记的物理信箱；
                 填了配置里的逻辑地址（alias）名 → 呈现错误；
                 同一个物理信箱在整棵树里只许出现一次（operator 若被 organization
                 引用，也按同一规则只算一次引用，不额外占位）
    operator     必须是已登记信箱
    panel 不是对象 / organization 不是对象 → 错误
  /api/state 顶层新增 "panel" 键：
    段合法    {"ok":true,"label":<str>,"operator":<str>,
               "organization":<归一化树：节点 = mailbox|members 二选一 +
                              label(str，缺省"") + children(list，缺省[])>}
    段缺省    {"ok":true,"label":"","operator":"","organization":null}
    任何错误  {"ok":false,"error":"<以 `panel 配置：` 开头的中文短句>",
               "label":"","operator":"","organization":null}
  隔离契约（最重要）：panel 段坏掉时其余一切照常——物理发信、@逻辑地址发信、面板分组
  开关、邮递员整轮、GET /api/state 全部不受影响，routes.json 在面板读接口前后逐字节
  不变；反过来，groups/aliases 坏掉时按既有行为停用分组与逻辑地址，而合法的 panel
  仍然照常暴露。

本文件只写测试不改实现，预期在当前树上失败（RED）：现在的配置校验忽略未知键
"panel"，/api/state 里没有 "panel" 键；纯回归锁除外（它们现在就该通过）。
全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，只连 127.0.0.1，绝不碰真实数据目录。
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
import urllib.parse
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_panelP_"))
ZI = "仅告知"

BOXES = ["director", "research-a", "research-b", "analyst", "ops", "runner"]
ALIASES = {"escalate": ["research-a", "research-b"]}
GROUPS = {"team": ["research-a", "research-b"]}

PANEL_KEYS = {"ok", "label", "operator", "organization"}
ERR_PREFIX = "panel 配置："


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
    env.pop("POSTOFFICE_PANEL_BASE_URL", None)      # 本文件不测它，也不许被外部环境带进来
    env.pop("POSTOFFICE_SLACK_WEBHOOK", None)
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


def mnode(name, label="", children=()):
    """归一化形态的单信箱节点（期望值也用它构造，做精确相等断言）。"""
    return {"mailbox": name, "label": label, "children": list(children)}


def gnode(names, label="", children=()):
    """归一化形态的同伴成员组节点。"""
    return {"members": list(names), "label": label, "children": list(children)}


ACME_ORG = mnode("director", children=[
    gnode(["research-a", "research-b"], "Research", [mnode("analyst")]),
    gnode(["ops"], "Operations", [mnode("runner")]),
])
ACME_PANEL = {"label": "ACME", "operator": "director", "organization": ACME_ORG}


class PanelPresentation(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        for box in BOXES:
            out = run_po("add", box, "--notify", "--who", "面板呈现测试", home=cls.home)
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

    # -- fixtures & assertions --------------------------------------------
    def write_config(self, panel=None, groups=None, aliases=None):
        cfg = {"version": 1,
               "groups": GROUPS if groups is None else groups,
               "aliases": ALIASES if aliases is None else aliases}
        if panel is not None:
            cfg["panel"] = panel
        (self.home / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def assert_panel_ok(self, state, label, operator, organization, msg=""):
        p = state.get("panel")
        self.assertIsInstance(p, dict, msg or f"/api/state 必须带 panel 键：{p!r}")
        self.assertEqual(set(p), PANEL_KEYS, p)
        self.assertIs(p["ok"], True, p)
        self.assertEqual(p["label"], label, p)
        self.assertEqual(p["operator"], operator, p)
        self.assertEqual(p["organization"], organization, p)

    def assert_panel_error(self, state, msg=""):
        p = state.get("panel")
        self.assertIsInstance(p, dict, msg or f"/api/state 必须带 panel 键：{p!r}")
        self.assertEqual(set(p), PANEL_KEYS | {"error"}, p)
        self.assertIs(p["ok"], False, p)
        err = p["error"]
        self.assertIsInstance(err, str, p)
        self.assertTrue(err.startswith(ERR_PREFIX), f"错误必须以 {ERR_PREFIX!r} 开头：{err!r}")
        self.assertGreater(len(err), len(ERR_PREFIX), f"错误必须带具体原因：{err!r}")
        self.assertRegex(err, r"[\u4e00-\u9fff]", f"错误必须是中文短句：{err!r}")
        self.assertEqual(p["label"], "", p)
        self.assertEqual(p["operator"], "", p)
        self.assertIsNone(p["organization"], p)

    def bad_panel(self, organization=None, **panel):
        p = {"label": "X", "operator": "director",
             "organization": mnode("director")}
        p.update(panel)
        if organization is not None:
            p["organization"] = organization
        return p

    def round_(self, seconds=1.8):
        """跑一轮邮递员：独立进程，POLL=1，到点杀掉。"""
        p = subprocess.Popen([sys.executable, PO, "postman"], env=env_for(self.home),
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

    def delivered(self):
        f = self.home / ".delivered.json"
        return set(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else set()

    # ================================================== (1) 段缺省
    def test_absent_panel_section_exposes_empty_and_everything_stays_normal(self):
        self.write_config()
        status, state = self.jget("/api/state")
        self.assertEqual(status, 200, state)
        self.assertEqual(state.get("panel"),
                         {"ok": True, "label": "", "operator": "", "organization": None},
                         "缺段时必须暴露 ok/空串/null 的 panel 键")
        names = {b["name"] for b in state.get("boxes", [])}
        self.assertTrue(set(BOXES) <= names, "其余状态照旧：信箱都要在")
        self.assertEqual(state.get("config_error"), "")

    # ================================================== (2) 单信箱根 + 嵌套子节点
    def test_valid_single_mailbox_root_with_nested_children(self):
        org = mnode("director", children=[mnode("analyst"), mnode("runner")])
        self.write_config(panel=self.bad_panel(organization=org, label="ACME"))
        status, state = self.jget("/api/state")
        self.assertEqual(status, 200, state)
        self.assert_panel_ok(state, "ACME", "director", org)

    # ================================================== (3) 同级成员组 + 子节点
    def test_peer_members_group_with_children(self):
        org = mnode("director", children=[
            gnode(["research-a", "research-b"], "Research", [mnode("analyst")])])
        self.write_config(panel=self.bad_panel(organization=org, label="ACME"))
        status, state = self.jget("/api/state")
        self.assertEqual(status, 200, state)
        self.assert_panel_ok(state, "ACME", "director", org)
        self.assertEqual(state["panel"]["organization"]["children"][0],
                         gnode(["research-a", "research-b"], "Research", [mnode("analyst")]),
                         "members 组要保持成员顺序与自己的子树")

    # ================================================== (4) 独立 T1 节点（无 children）
    def test_standalone_t1_node_without_children(self):
        org = mnode("analyst")
        self.write_config(panel=self.bad_panel(organization=org, label="Solo"))
        status, state = self.jget("/api/state")
        self.assertEqual(status, 200, state)
        self.assert_panel_ok(state, "Solo", "director",
                             {"mailbox": "analyst", "label": "", "children": []})

    # ================================================== (5) 树内重复物理信箱
    def test_duplicate_physical_mailbox_anywhere_in_the_tree_is_an_error(self):
        cases = {
            "同一叶子出现两次": mnode("director", children=[
                mnode("analyst"),
                gnode(["research-a"], children=[mnode("analyst")])]),
            "根信箱在下面再出现": mnode("director", children=[mnode("director")]),
            "同一成员组内重复": gnode(["research-a", "research-a"]),
        }
        for name, org in cases.items():
            with self.subTest(case=name):
                self.write_config(panel=self.bad_panel(organization=org))
                status, state = self.jget("/api/state")
                self.assertEqual(status, 200, state)
                self.assert_panel_error(state)

    # ================================================== (6) 未登记信箱
    def test_unknown_mailbox_in_the_tree_is_an_error(self):
        cases = {
            "mailbox 未登记": mnode("ghost"),
            "members 未登记": gnode(["research-a", "ghost"]),
        }
        for name, org in cases.items():
            with self.subTest(case=name):
                self.write_config(panel=self.bad_panel(organization=org))
                status, state = self.jget("/api/state")
                self.assertEqual(status, 200, state)
                self.assert_panel_error(state)

    # ================================================== (7) 逻辑地址名当信箱
    def test_alias_name_used_as_a_mailbox_is_an_error(self):
        cases = {
            "mailbox 用逻辑地址": mnode("escalate"),
            "members 用逻辑地址": gnode(["escalate"]),
        }
        for name, org in cases.items():
            with self.subTest(case=name):
                self.write_config(panel=self.bad_panel(organization=org))
                status, state = self.jget("/api/state")
                self.assertEqual(status, 200, state)
                self.assert_panel_error(state)

    # ================================================== (8) 各种坏形状
    def test_malformed_shapes_are_each_a_presentation_error(self):
        cases = {
            "members 空数组": self.bad_panel({"members": []}),
            "mailbox 与 members 并存": self.bad_panel({"mailbox": "analyst", "members": ["ops"]}),
            "mailbox 与 members 都没有": self.bad_panel({"label": "组"}),
            "children 不是数组": self.bad_panel(mnode("analyst", children="ops")),
            "children 元素不是对象": self.bad_panel(mnode("director", children=["analyst"])),
            "节点未知字段": self.bad_panel({"mailbox": "analyst", "zone": "x"}),
            "节点 label 不是字符串": self.bad_panel({"mailbox": "analyst", "label": 5}),
            "mailbox 不是字符串": self.bad_panel({"mailbox": 7}),
            "members 不是数组": self.bad_panel({"members": "analyst"}),
            "members 元素不是字符串": self.bad_panel({"members": [7]}),
            "panel 未知字段": dict(self.bad_panel(), extra=1),
            "panel label 不是字符串": self.bad_panel(label=5),
            "operator 未登记": self.bad_panel(operator="ghost"),
            "panel 不是对象（字符串）": "ACME",
            "panel 不是对象（数组）": [],
            "organization 不是对象（字符串）": self.bad_panel("director"),
            "organization 不是对象（数组）": self.bad_panel([]),
            "panel 空对象": {},
            "panel 缺 operator": {"label": "X", "organization": mnode("director")},
            "panel 缺 organization": {"label": "X", "operator": "director"},
            "panel 缺 label": {"operator": "director", "organization": mnode("director")},
        }
        for name, panel in cases.items():
            with self.subTest(case=name):
                self.write_config(panel=panel)
                status, state = self.jget("/api/state")
                self.assertEqual(status, 200, f"{name}: {state}")
                self.assert_panel_error(state, msg=name)

    # ================================================== (9) 通用证明夹具
    def test_generic_proof_acme_fixture_round_trips_exactly(self):
        self.write_config(panel=ACME_PANEL)
        status, state = self.jget("/api/state")
        self.assertEqual(status, 200, state)
        self.assert_panel_ok(state, "ACME", "director", ACME_ORG)

    # ================================================== (10) 隔离契约
    def test_invalid_panel_section_disables_nothing_else(self):
        self.write_config(groups=GROUPS, aliases=ALIASES,
                          panel=self.bad_panel({"mailbox": "ghost"}))
        # 物理发信照常
        out = run_po("send", "research-b", "analyst", "物理直发", ZI,
                     home=self.home, stdin="正文甲\n")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("已投递：", out.stdout)
        lid1 = re.search(r"编号：(\S+)", out.stdout).group(1)
        # @逻辑地址发信照常
        out2 = run_po("send", "@escalate", "analyst", "逻辑直发", ZI,
                      home=self.home, stdin="正文乙\n")
        self.assertEqual(out2.returncode, 0, out2.stdout + out2.stderr)
        self.assertIn("已投递：", out2.stdout)
        lid2 = re.search(r"编号：(\S+)", out2.stdout).group(1)
        # 面板分组开关照常
        status, d = self.jpost("/api/status", {"group": "team", "status": "offline"})
        self.assertEqual((status, d), (200, {"ok": True, "error": ""}), d)
        routes = json.loads((self.home / "routes.json").read_text(encoding="utf-8"))
        self.assertEqual(routes["research-a"]["status"], "offline")
        status, d = self.jpost("/api/status", {"group": "team", "status": "online"})
        self.assertEqual((status, d), (200, {"ok": True, "error": ""}), d)
        routes = json.loads((self.home / "routes.json").read_text(encoding="utf-8"))
        self.assertEqual(routes["research-a"]["status"], "online")
        # 邮递员整轮照常完成、无异常
        self.round_()
        self.assertIn(str(self.home / "research-b" / "inbox" / f"{lid1}.md"), self.delivered())
        self.assertIn(str(self.home / "research-a" / "inbox" / f"{lid2}.md"), self.delivered())
        logf = self.home / "logs" / "postman.log"
        self.assertTrue(logf.is_file(), "邮递员日志必须存在")
        logtext = logf.read_text(encoding="utf-8", errors="replace")
        self.assertIn("邮递员上岗", logtext)
        self.assertNotIn("异常", logtext, f"panel 段坏不许让邮递员报异常：\n{logtext}")
        self.assertNotIn("Traceback", logtext)
        # GET /api/state 照常 200，且 panel 错误在场；分组与逻辑地址不许被连坐
        status, state = self.jget("/api/state")
        self.assertEqual(status, 200)
        self.assert_panel_error(state)
        self.assertEqual(state.get("config_error"), "",
                         "panel 段坏不许写进 config_error：分组与逻辑地址必须照常")
        # 面板读接口前后 routes.json 逐字节不变
        before = (self.home / "routes.json").read_bytes()
        self.jget("/api/state")
        status, d = self.jget("/api/letter?box=research-b&id="
                              + urllib.parse.quote(lid1, safe=""))
        self.assertEqual(status, 200, d)
        self.assertEqual((self.home / "routes.json").read_bytes(), before,
                         "面板读接口不许改动 routes.json")

    def test_broken_groups_config_still_exposes_valid_panel(self):
        self.write_config(groups={"team": ["ghost"]}, aliases={"escalate": ["ghost"]},
                          panel=ACME_PANEL)
        status, state = self.jget("/api/state")
        self.assertEqual(status, 200, state)
        self.assertTrue(state.get("config_error"),
                        "groups 坏掉时必须照旧报告 config_error（既有行为）")
        self.assert_panel_ok(state, "ACME", "director", ACME_ORG,
                             msg="分组坏掉时合法的 panel 仍要照常暴露")
        # 分组与逻辑地址按既有行为停用
        out = run_po("send", "@escalate", "analyst", "停用对照", ZI,
                     home=self.home, stdin="x\n")
        self.assertNotEqual(out.returncode, 0, "groups 坏掉时逻辑地址必须照旧停用")
        status, d = self.jpost("/api/status", {"group": "team", "status": "online"})
        self.assertEqual(status, 400, d)
        self.assertIs(d.get("ok"), False, "groups 坏掉时面板分组开关必须照旧失败")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
