#!/usr/bin/env python3
"""v1.13 runtime activity 竞态负例（§13-15 退修）。

读改写必须是互斥的：os.replace 只让「写」原子，两个写者仍能交错 read/compute/write
（旧心跳快照把刚写的 working 按回 idle）。旧绑定的观察者也绝不许发布当前信箱的 activity：
watcher 熬过一次改绑之后，它的心跳不得把新绑定写的 working 改回 idle、也不许当空记录初始化。

用 importlib 直接把 postoffice 载进内存（HOME 指向临时目录），在函数层做确定性的并发/跨绑定负例。
构造这些负例时**故意不经过锁之外的状态**：如果读改写不是互斥，或旧绑定心跳会回落 activity_write，
下面的断言就会失败（RED-capable）。
"""
import importlib.machinery
import importlib.util
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
import os

BASE = Path(tempfile.mkdtemp(prefix="po_activity_race_"))
HOME = BASE / "home"
HOME.mkdir(parents=True, exist_ok=True)
os.environ["POSTOFFICE_HOME"] = str(HOME)
os.environ["POSTOFFICE_NO_NOTIFY"] = "1"
os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)

_loader = importlib.machinery.SourceFileLoader("po_race", PO)
_spec = importlib.util.spec_from_file_location("po_race", PO, loader=_loader)
po = importlib.util.module_from_spec(_spec)
_loader.exec_module(po)

BOX = "racebox"


class ActivityRace(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(HOME / "runtime", ignore_errors=True)

    def test_touch_never_clobbers_a_concurrent_working_write(self):
        """心跳与一次真实 working 写入并发：最终的 working 绝不能被旧快照覆盖回 idle。"""
        for _ in range(60):
            po.activity_write(BOX, "idle", "claude_hook", "s1")
            t = threading.Thread(target=lambda: po.activity_write(BOX, "working", "claude_hook", "s1"))
            t.start()
            po.activity_touch(BOX, "claude_hook", "s1")
            t.join()
            rec = po.activity_read(BOX)
            self.assertEqual(rec.get("state"), "working",
                             "并发写入的 working 不得被旧心跳快照覆盖回 idle")

    def test_old_binding_watcher_never_overwrites_a_rebound_box(self):
        """旧绑定（s1）watcher 的心跳不得碰新绑定（s2）写的记录。"""
        po.activity_write(BOX, "working", "claude_hook", "s2")
        po.activity_touch(BOX, "claude_hook", "s1")
        rec = po.activity_read(BOX)
        self.assertEqual(rec.get("binding"), "s2", "旧绑定心跳不得清空/初始化新绑定的记录")
        self.assertEqual(rec.get("state"), "working", "旧绑定心跳不得把新绑定的 working 改回 idle")

    def test_touch_with_no_record_writes_nothing(self):
        """无记录时旧心跳也不许凭绑定不符去初始化。"""
        po.activity_touch(BOX, "claude_hook", "s1")
        self.assertFalse((HOME / "runtime" / "activity" / f"{BOX}.json").exists())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
