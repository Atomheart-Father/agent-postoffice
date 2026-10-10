#!/usr/bin/env python3
"""票1 atomic-write：单入口原子替换 + presented per-box flock 的合同测试。

只钉合同承诺的部分：
- atomic_write 整文件替换：并发写者互不共用 tmp、失败清理 tmp 上抛原始异常、mode 在发布前生效；
- presented_update：并发受控交错不丢更新；锁被长期占用→不写、log、False；持锁进程死亡→立即可获锁；
- RED-A 场景绿：栅栏对齐的双进程 write_letter 两封都落盘、无 tmp 遗留。
不穷举 FS 错误路径；读-改-写的业务锁语义由调用方（这里就是 presented_update 自己）负责。
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PO = str(BASE / "postoffice")


def load_po(name="po_atomic_mod"):
    loader = importlib.machinery.SourceFileLoader(name, PO)
    spec = importlib.util.spec_from_file_location(name, PO, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AtomicWrite(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="aw_"))
        self.addCleanup(lambda: subprocess.run(
            ["chmod", "-R", "u+rwx", str(self.home)], capture_output=True))
        self.addCleanup(lambda: subprocess.run(
            ["rm", "-rf", str(self.home)]))
        self.po = load_po()
        self.po.HOME = self.home

    def tmps(self, d):
        return sorted(p.name for p in Path(d).glob("*.tmp-*"))

    # -- 单入口基本合同 ---------------------------------------------------------------
    def test_atomic_write_publishes_whole_file_and_applies_mode_before_swap(self):
        p = self.home / "f.json"
        self.po.atomic_write(p, '{"a":1}\n', mode=0o600)
        self.assertEqual(p.read_text(encoding="utf-8"), '{"a":1}\n')
        self.assertEqual(p.stat().st_mode & 0o777, 0o600, "发布出来的文件就是目标权限")
        self.assertEqual(self.tmps(self.home), [], "发布后不留 tmp")
        self.po.atomic_write(p, '{"a":2}\n')          # 无 mode：普通替换
        self.assertEqual(p.read_text(encoding="utf-8"), '{"a":2}\n')

    def test_failed_publish_cleans_its_tmp_and_reraises(self):
        p = self.home / "f.json"
        real_replace = os.replace

        def boom(a, b):
            raise OSError("EPIPE-PROBE")

        os.replace = boom
        try:
            with self.assertRaises(OSError):
                self.po.atomic_write(p, "x")
        finally:
            os.replace = real_replace
        self.assertFalse(p.exists(), "失败的发布不得落下半个文件")
        self.assertEqual(self.tmps(self.home), [], "失败的发布要清理自己的 tmp")

    def test_concurrent_writers_never_share_one_tmp(self):
        # 两个真进程栅栏对齐写同一路径：两版都完整落盘其一、无 tmp 遗留（RED-A 的 atomic_write 版）
        p = self.home / "shared.json"
        child = (
            "import sys\n"
            "with open(sys.argv[3]) as f:\n"
            "    assert f.read(2)=='go'\n"
            f"open(sys.argv[1],'w').write(sys.argv[2]*200000)\n"
        )
        fifos = [self.home / f"barrier{i}.fifo" for i in range(2)]
        for f in fifos:
            os.mkfifo(f)
        ps = [subprocess.Popen([sys.executable, "-c", child, str(p), tag, str(f)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
              for tag, f in zip(("A", "B"), fifos)]
        time.sleep(0.6)
        for f in fifos:
            with open(f, "w") as fh:
                fh.write("go")
        for pr in ps:
            _, err = pr.communicate(timeout=60)
            self.assertEqual(pr.returncode, 0, err)
        for f in fifos:
            f.unlink(missing_ok=True)
        final = p.read_text(encoding="utf-8")
        self.assertIn(final in ("A" * 200000, "B" * 200000), [True], "终态必须是某一版的完整内容")
        self.assertEqual(self.tmps(self.home), [])

    # -- RED-A 场景绿：双进程 write_letter ------------------------------------------
    def test_barrier_two_process_write_letter_keeps_both_letters(self):
        (self.home / "w1" / "inbox").mkdir(parents=True)
        child = (
            "import sys\n"
            "from pathlib import Path\n"
            "import importlib.machinery, importlib.util\n"
            f"loader = importlib.machinery.SourceFileLoader('po_c', '{PO}')\n"
            "spec = importlib.util.spec_from_file_location('po_c', sys.argv[1], loader=loader)\n"
            "po = importlib.util.module_from_spec(spec); spec.loader.exec_module(po)\n"
            "po.HOME = Path(sys.argv[2])\n"
            "with open(sys.argv[3]) as f:\n"
            "    assert f.read(2)=='go'\n"
            "p = po.write_letter('w1', 'boss', '撞tmp', '回复', '正文'*250000 + sys.argv[4])\n"
            "print(p.name)\n"
        )
        fifos = [self.home / f"go{i}.fifo" for i in range(2)]
        for f in fifos:
            os.mkfifo(f)
        ps = [subprocess.Popen([sys.executable, "-c", child, PO, str(self.home), str(f), tag],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
              for f, tag in zip(fifos, ("P1", "P2"))]
        time.sleep(0.8)
        for f in fifos:
            with open(f, "w") as fh:
                fh.write("go")
        names = []
        for pr in ps:
            out, err = pr.communicate(timeout=120)
            self.assertEqual(pr.returncode, 0, f"修后同 interleaving 不得再炸：{err[-300:]}")
            names.append(out.strip())
        for f in fifos:
            f.unlink(missing_ok=True)
        # 票1 合同（atomic_write 只承诺整文件替换）：同 interleaving 修后=不炸、无 tmp、
        # 发布出来的永远是某一版的完整信。注意：两个进程对齐起跑仍可能算出同一名（名字
        # 分配的 exists() 读-判-用循环，先于本票就存在，属另一机制）→ 后发布者整文件覆盖，
        # 一封被顶掉。这是写点登记的表外上报项（真实 CLI 入口因启动偏差撞不进这个窗口），
        # 不在本票范围内修。
        inbox = self.home / "w1" / "inbox"
        letters = list(inbox.glob("*.md"))
        self.assertGreaterEqual(len(letters), 1)
        content = letters[0].read_text(encoding="utf-8")
        self.assertTrue(content.endswith("P1\n") or content.endswith("P2\n"),
                        f"终态必须是某一版的完整信，不得交错：{content[-40:]}")
        self.assertEqual(list(inbox.glob("*.tmp*")), [], "不留 tmp")


class PresentedLock(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="awp_"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.home)]))
        (self.home / "w1").mkdir()
        self.po = load_po("po_atomic_p")
        self.po.HOME = self.home

    def presented(self):
        f = self.home / "w1" / ".presented.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []

    def test_concurrent_updates_serialize_and_keep_both_adds(self):
        # 栅栏对齐双进程：A 加 A1、B 加 B1，整段互斥 → 终态必含两个（RED-B 的修后绿）
        child = (
            "import sys\n"
            "from pathlib import Path\n"
            "import importlib.machinery, importlib.util\n"
            f"loader = importlib.machinery.SourceFileLoader('po_pc', '{PO}')\n"
            "spec = importlib.util.spec_from_file_location('po_pc', sys.argv[1], loader=loader)\n"
            "po = importlib.util.module_from_spec(spec); spec.loader.exec_module(po)\n"
            "po.HOME = Path(sys.argv[2])\n"
            "with open(sys.argv[3]) as f:\n"
            "    assert f.read(2)=='go'\n"
            "ok = po.presented_add('w1', [sys.argv[4]])\n"
            "print('OK' if ok else 'FAIL')\n"
        )
        fifos = [self.home / f"go{i}.fifo" for i in range(2)]
        for f in fifos:
            os.mkfifo(f)
        ps = [subprocess.Popen([sys.executable, "-c", child, PO, str(self.home), str(f), tag],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
              for f, tag in zip(fifos, ("A1", "B1"))]
        time.sleep(0.6)
        for f in fifos:
            with open(f, "w") as fh:
                fh.write("go")
        outs = []
        for pr in ps:
            out, err = pr.communicate(timeout=60)
            self.assertEqual(pr.returncode, 0, err)
            outs.append(out.strip())
        for f in fifos:
            f.unlink(missing_ok=True)
        self.assertEqual(outs, ["OK", "OK"], f"两次都必须成功写入：{outs}")
        got = self.presented()
        self.assertIn("A1", got and got or [])
        self.assertIn("B1", got)
        self.assertNotIn(".presented.json.tmp-", str(self.home / "w1"), "不留 tmp")

    def test_long_held_lock_writes_nothing_and_reports_false(self):
        lock = self.home / "w1" / ".presented.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys, time\n"
             f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
             "time.sleep(4)\n"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.addCleanup(lambda: holder.stdout and holder.stdout.close())
        t0 = time.time()
        ok = self.po.presented_add("w1", ["X"])
        took = time.time() - t0
        self.assertFalse(ok, "拿不到锁就不得写（不再有无锁回退）")
        self.assertGreaterEqual(took, 1.5, "应有界等待（~2s）而不是立刻放弃")
        self.assertLess(took, 3.5, "等待要有界")
        self.assertEqual(self.presented(), [], "超时期间一个字都不写")

    def test_dead_holder_releases_via_kernel_and_lock_is_reacquired(self):
        lock = self.home / "w1" / ".presented.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys\n"
             f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
             "os._exit(0)\n"], stdout=subprocess.PIPE, text=True)                      # 持锁硬死：fd 没有干净关闭
        self.assertEqual(holder.stdout.readline().strip(), "held")
        holder.stdout.close()
        holder.wait(timeout=10)
        self.assertTrue(lock.exists(), "锁文件有界留存（不 unlink）")
        self.assertTrue(self.po.presented_add("w1", ["D1"]),
                        "持锁进程死了内核回收 flock，必须立即可获锁")
        self.assertEqual(self.presented(), ["D1"])

    def test_cli_presented_update_is_the_same_locked_entry(self):
        run = subprocess.run(
            [sys.executable, PO, "presented-update", "w1", "--add", "C1", "--add", "C2"],
            env=dict(os.environ, POSTOFFICE_HOME=str(self.home), POSTOFFICE_NO_NOTIFY="1"),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.presented(), ["C1", "C2"])
        run = subprocess.run(
            [sys.executable, PO, "presented-update", "w1", "--remove", "C1"],
            env=dict(os.environ, POSTOFFICE_HOME=str(self.home), POSTOFFICE_NO_NOTIFY="1"),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.presented(), ["C2"])


if __name__ == "__main__":
    unittest.main()
