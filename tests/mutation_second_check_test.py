#!/usr/bin/env python3
"""§11 认领后「第二次检查」的针对性验证（RED-capable，白盒注入）。

apply_mutation 的顺序是：第一遍判定 → claim_letter → 第二遍判定(skip_claim) → 变更。
outbox_test 里的 held-claim 用例只能证明「认领前已有占用」会被第一遍挡住；本用例专门证明
第二遍：在 claim_letter 成功的同一窗口注入一条投递台账（模拟投递方刚写下 DELIVERED），
第二遍必须 fail closed 拒绝变更。若有人删掉第二遍检查，本用例会 RED —— 信会被改/被撤。

只在 mktemp 的临时 POSTOFFICE_HOME 里跑，不碰真实邮局。
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

PO = Path(__file__).resolve().parent.parent / "postoffice"


def load_module(home):
    os.environ["POSTOFFICE_HOME"] = str(home)
    os.environ["POSTOFFICE_NO_NOTIFY"] = "1"
    spec = importlib.util.spec_from_loader("po_mod", SourceFileLoader("po_mod", str(PO)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SecondCheck(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="po_second_"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        (self.home / "boss").mkdir()
        (self.home / "lead" / "inbox").mkdir(parents=True)
        (self.home / "lead" / "done").mkdir(parents=True)
        (self.home / "routes.json").write_text(json.dumps({
            "boss": {"methods": ["notify"], "status": "online"},
            "lead": {"methods": ["opencode_plugin"], "session_id": "ses_x", "status": "online"},
        }), encoding="utf-8")
        self.lid = "20260101-000000_boss_hi"
        self.letter = self.home / "lead" / "inbox" / f"{self.lid}.md"
        self.letter.write_text("来源：boss\n事由：hi\n需要：回复\n\n正文\n", encoding="utf-8")
        self.mod = load_module(self.home)

    def _race_claim(self):
        """替换 claim_letter：抢到认领的同一窗口里，投递方写下 DELIVERED 台账。"""
        ledger = self.home / "opencode_delivered.jsonl"
        original = self.mod.claim_letter

        def racing_claim(name, path):
            ledger.write_text(json.dumps(
                {"box": name, "file": path.name, "result": "DELIVERED"}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            return True

        self.mod.claim_letter = racing_claim
        self.addCleanup(lambda: setattr(self.mod, "claim_letter", original))

    def test_second_check_refuses_retract_when_delivery_lands_in_window(self):
        # 第一遍判定（认领前）：没有 claim、没有台账 → 放行（证明我们确实走到了第二遍）
        self.assertEqual(self.mod.refusal_for_methods("lead", self.letter, ["opencode_plugin"]), "")
        self._race_claim()
        with self.assertRaises(SystemExit) as cm:
            self.mod.apply_mutation("retract", "lead", self.letter, ["opencode_plugin"])
        self.assertIn("已送达", str(cm.exception), "第二遍必须按台账拒绝")
        self.assertTrue(self.letter.is_file(), "第二遍拦住：原信仍在 inbox，没被撤走")
        archived = self.home / "lead" / "archived"
        self.assertFalse(archived.exists() and any(archived.glob("**/*.md")), "不得产生任何归档")

    def test_second_check_refuses_edit_when_delivery_lands_in_window(self):
        before = self.letter.read_text(encoding="utf-8")
        self._race_claim()
        with self.assertRaises(SystemExit):
            self.mod.apply_mutation("edit", "lead", self.letter, ["opencode_plugin"], subject="改过的事由")
        self.assertEqual(self.letter.read_text(encoding="utf-8"), before, "第二遍拦住：正文逐字节不变")


if __name__ == "__main__":
    unittest.main(verbosity=2)
