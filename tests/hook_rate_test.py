#!/usr/bin/env python3
"""Claude 钩子的「限流 × 认领」不变式：任何唤醒都必须同时满足「限流允许」AND「认领到手」。

这一票（v1.9 / B）只做诊断与复现，所以本文件里的断言现在**全部是红的**：cmd_hook 在
`rate_ok()` 为假时并没有把 `new` 收窄，于是照旧写 `.seen`、照旧 `rate_mark()`、照旧
`return 2` 唤醒，而且一个认领都没抢（postoffice:2177-2189）。缺了认领这条腿，唤醒与撤回
之间就没有互斥，`postoffice retract` 会在「已经唤醒」之后仍然报成功。

每条断言都只钉住那条不变式，不钉住任何实现细节：
  1) 6 次 / 600 秒窗口用满之后，第 7 次唤醒必须被挡住；
  2) 被挡下的那一次不许留下任何送达痕迹（`.seen` / `.wake_times` / 认领）；
  3) 限流满了也不许绕过别人手里的认领（不许唤醒、不许把信记成已送达、不许改认领归属）；
  4) 唤醒与撤回真进程并发时只允许一个结果；
  5) 撤回先拿到认领 → 钩子不唤醒；
  6) 钩子先拿到认领 → 撤回明确被拒，且认领留在钩子名下。

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
BASE = Path(tempfile.mkdtemp(prefix="po_hookrate_"))
TITLE = "限流认领的标题"
BOX = "claude"


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    # 与运行环境的 Claude 桌面变量隔离：认人只认用例显式给的值，否则身份会被带偏。
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    env.update(extra)
    return env


def run_po(*args, home=None, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home or BASE), input=stdin, timeout=timeout)


class HookRateClaim(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        for box in ("boss", BOX):
            (self.home / box / "inbox").mkdir(parents=True)
            (self.home / box / "done").mkdir(parents=True)
        run_po("add", "boss", "--notify", home=self.home)
        run_po("add", BOX, "--claude", TITLE, home=self.home)
        self.set_methods(BOX, "claude_hook")
        self.hooks = []

    # -- helpers ---------------------------------------------------------
    def set_methods(self, box, method, **extra):
        p = self.home / "routes.json"
        r = json.loads(p.read_text())
        r[box]["methods"] = [method]
        r[box].update(extra)
        p.write_text(json.dumps(r, ensure_ascii=False))

    def send(self, to=BOX, sender="boss", subject="限流信", need="回复"):
        out = run_po("send", to, sender, subject, need, home=self.home, stdin="正文\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        line = [x for x in out.stdout.splitlines() if x.startswith("编号：")][0]
        return line.split("：", 1)[1].strip()

    def title_transcript(self, title=TITLE):
        tp = self.home / "t.jsonl"
        tp.write_text(json.dumps({"type": "custom-title", "customTitle": title}) + "\n")
        return tp

    def letter(self, lid, box=BOX):
        return self.home / box / "inbox" / f"{lid}.md"

    def spend_wakes(self, n, box=BOX, age=0.0):
        """往 `.wake_times` 写 n 条最近的时间戳：模拟这个信箱刚用掉的唤醒次数。"""
        now = time.time()
        (self.home / box / ".wake_times").write_text("".join(f"{now - age - i}\n" for i in range(n)))

    def wake_times(self, box=BOX):
        f = self.home / box / ".wake_times"
        return f.read_text().split() if f.exists() else []

    def seen(self, box=BOX):
        f = self.home / box / ".seen"
        return f.read_text().split() if f.exists() else []

    def claims(self, box=BOX):
        d = self.home / box / ".claims"
        return sorted(p.name for p in d.glob("*")) if d.is_dir() else []

    def start_hook(self, transcript=None, **extra):
        """起一个真的 `postoffice hook` 子进程，stderr 落到文件里：唤醒负载就写在那儿。"""
        err = self.home / f"hook_err_{len(self.hooks)}.txt"
        sink = err.open("w")
        self.addCleanup(sink.close)
        p = subprocess.Popen([sys.executable, PO, "hook"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=sink,
                             env=env_for(self.home, **extra), text=True)
        self.addCleanup(self.kill, p)
        p.stdin.write(json.dumps({"transcript_path": str(transcript or self.title_transcript())}))
        p.stdin.close()
        self.hooks.append((p, err))
        return p

    def kill(self, p):
        if p.poll() is None:
            p.kill()
            p.wait()

    def wait_hook(self, p, err, wait=10):
        """等钩子自己下台：交回 (退出码, 唤醒负载)。124 = 交班时它还在岗（没有可唤醒的东西）。

        交班即收工：还活着就杀掉，否则它在下一条用例里会跟新的钩子抢同一批信。
        """
        deadline = time.time() + wait
        while time.time() < deadline:
            if p.poll() is not None:
                return p.returncode, Path(err).read_text()
            time.sleep(0.05)
        self.kill(p)
        return 124, Path(err).read_text()

    def run_hook(self, wait=8, **extra):
        p = self.start_hook(**extra)
        return self.wait_hook(p, self.hooks[-1][1], wait=wait)

    def park_on_rate_decision(self, spent=6):
        """把钩子真进程停在「已经列出待投、还没决定要不要唤醒」的那个点上。

        `.wake_times` 换成 FIFO：`rate_ok()` 里的 `f.read_text()` 会一直等写端，于是钩子
        停在 postoffice:2177，既没抢认领也没写 `.seen` —— 正是「投递方手里已经有一份待投
        清单」的状态。撤回方此时跑完整流程（真进程、真抢认领、真归档），之后我们再把 6 条
        限流记录喂进去，钩子就带着「撤回已经完成」的现场去做唤醒决策。

        用例全程额外持有 FIFO 的读端，所以喂数据不必等钩子来读，钩子之后的 `rate_mark()`
        打开 FIFO 追加也不会卡住 —— 今天这个缺陷会一路顺畅地走到 `return 2`。
        """
        fifo = self.home / BOX / ".wake_times"
        self.spend_wakes(spent)
        parked = fifo.with_suffix(".fifo")
        os.mkfifo(parked)
        fifo.unlink()
        parked.rename(fifo)
        keeper = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)   # 读者常驻：喂数据不必等它读
        self.addCleanup(os.close, keeper)
        p = self.start_hook()
        deadline = time.time() + 10
        while time.time() < deadline and not (self.home / BOX / ".watch_alive").exists():
            time.sleep(0.02)
        self.assertTrue((self.home / BOX / ".watch_alive").exists(), "钩子没有上岗，夹具没搭起来")
        time.sleep(0.4)                                            # 让它走进 rate_ok 停住
        self.assertIsNone(p.poll(), "钩子不该在下台之前就退出了")
        return p, fifo

    def feed_rate(self, fifo, spent=6):
        """把限流记录喂进 FIFO，让钩子从 rate_ok 里醒过来继续做唤醒决策。

        写完必须关掉写端：read_text() 读到 EOF 才返回，光喂不关，钩子会一直等在限流判断里。
        """
        now = time.time()
        fd = os.open(str(fifo), os.O_WRONLY)
        try:
            os.write(fd, "".join(f"{now - i}\n" for i in range(spent)).encode())
        finally:
            os.close(fd)

    # -- 1) 第 7 次唤醒必须被挡住 -----------------------------------------
    def test_the_seventh_wake_is_blocked(self):
        """6 次 / 600 秒窗口用满之后，第 7 次不许唤醒（退出码 2）。"""
        lid = self.send()
        self.assertTrue(self.letter(lid).exists())
        self.spend_wakes(6)
        rc, err = self.run_hook()
        self.assertEqual(rc, 124, f"限流已满时钩子应继续在岗监视，实际退出码 {rc}")
        self.assertNotIn(str(self.letter(lid)), err, "限流已满时不得把信交给会话")

    # -- 2) 被挡下的那一次不留送达痕迹 ------------------------------------
    def test_a_blocked_wake_records_nothing(self):
        """没唤醒 = 没送达：`.seen` 不许写、限流计数不许再涨、认领一个都不许留。"""
        lid = self.send()
        self.spend_wakes(6)
        rc, _ = self.run_hook()
        self.assertNotEqual(rc, 2, "限流已满时不得唤醒")
        # Claude 钩子通道的「已送达」凭据就是 .seen（postoffice:2273），没有别的台账
        self.assertEqual(self.seen(), [], "被限流挡下的一轮不得写 .seen")
        self.assertEqual(len(self.wake_times()), 6, "没唤醒就不该再记一次限流（否则窗口一直被推后）")
        self.assertEqual(self.claims(), [], "限流路径不得留下任何认领")

    # -- 3) 限流满了也不许绕过别人手里的认领 -------------------------------
    def test_a_full_rate_limit_does_not_bypass_a_claim(self):
        """认领已被别人握在手里 + 限流也满了：这封信既不许被唤醒，也不许被记成已送达。

        认领归属也不许被旁路动过：放掉之后这封信必须还能被正常投出去（不许被吞掉）。
        """
        lid = self.send()
        claims = self.home / BOX / ".claims"
        claims.mkdir(parents=True)
        (claims / f"{lid}.md").write_text("99999\n")          # 别人（撤回方/另一个投递方）已经拿着
        self.spend_wakes(6)
        rc, _ = self.run_hook()
        self.assertEqual(rc, 124, f"限流已满时不得绕过认领唤醒，实际退出码 {rc}")
        self.assertEqual(self.seen(), [], "抢不到认领的信不得被记成已送达")
        self.assertEqual(self.claims(), [f"{lid}.md"], "认领必须仍在原主手里")
        (claims / f"{lid}.md").unlink()                       # 原主放手
        self.spend_wakes(0)
        rc2, _ = self.run_hook(wait=10)
        self.assertEqual(rc2, 2, "限流解除后这封信必须还能被正常投出去")
        self.assertEqual(self.seen(), [str(self.letter(lid))])

    # -- 4) 唤醒与撤回真并发：只允许一个结果 -------------------------------
    def test_a_wake_and_a_retraction_never_both_win(self):
        """两边真的同时起跑：不强制先后，只断言那条不变量。

        钩子被停在限流判断上（手里已有一份待投清单），撤回方跑真正的 CLI。绝不允许
        「撤回报成功」与「钩子把那封信交给会话」同时发生。
        """
        lid = self.send()
        p, fifo = self.park_on_rate_decision()
        r = run_po("retract", "boss", BOX, lid, home=self.home)     # 与停住的钩子并发
        self.feed_rate(fifo)
        rc, err = self.wait_hook(p, self.hooks[-1][1], wait=10)
        retract_ok = r.returncode == 0
        woke = str(self.letter(lid)) in err
        self.assertFalse(retract_ok and woke,
                         f"撤回与唤醒不能同时成立：撤回 rc={r.returncode}，钩子 rc={rc}，"
                         f"唤醒负载里含这封信={woke}")
        # 信只有一个下落：在 inbox 里，或者在 archived 里
        in_inbox = self.letter(lid).exists()
        archived = [p for p in (self.home / BOX / "archived").rglob(f"{lid}.md")]
        self.assertNotEqual(in_inbox, bool(archived), "信既没留在 inbox 也没进 archived：丢件了")
        if retract_ok:
            self.assertNotIn(str(self.letter(lid)), err, "撤回来了就不许再交给会话")
            self.assertNotIn(str(self.letter(lid)), self.seen(), "撤回来了就不许写 .seen")
        else:
            self.assertIn("无法撤回", r.stderr)

    # -- 5) 撤回先拿到认领 → 钩子不唤醒 ------------------------------------
    def test_retraction_that_wins_the_claim_means_no_wake(self):
        """撤回在钩子做决策之前完成 → 钩子不得唤醒，也不得把那封信写进 .seen。"""
        lid = self.send()
        p, fifo = self.park_on_rate_decision()
        r = run_po("retract", "boss", BOX, lid, home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.letter(lid).exists(), "撤回后原信应已归档")
        self.feed_rate(fifo)
        rc, err = self.wait_hook(p, self.hooks[-1][1], wait=10)
        self.assertNotEqual(rc, 2, "撤回已经完成，钩子不得唤醒")
        self.assertNotIn(str(self.letter(lid)), err, "撤回来了就不许出现在唤醒负载里")
        self.assertEqual(self.seen(), [], "撤回来了就不许写 .seen")
        self.assertEqual(self.claims(), [], "撤回放掉了自己的认领，钩子不该再留一个")

    # -- 对照：限流没满时，正常唤醒路径一个字节都不许变 ----------------------
    def test_a_wake_under_the_rate_limit_still_works(self):
        """对照组：限流空着（`.wake_times` 压根不存在）时，钩子照常唤醒。

        钉住修复不许碰到的那一面：退出码 2、唤醒负载里既有路径也有正文开头、`.seen` 照写、
        限流计数照记一次、认领照建（唤醒过的那封信必须有归属，撤回才拒得掉）。
        """
        lid = self.send()
        self.assertFalse((self.home / BOX / ".wake_times").exists(), "前置：限流计数是空的")
        rc, err = self.run_hook()
        self.assertEqual(rc, 2, f"限流没满时必须照常唤醒，实际退出码 {rc}")
        head = (self.letter(lid)).read_text().splitlines()[:1]
        self.assertIn(f"【联络总站新信｜{BOX}】", err, "唤醒负载的形状不许变")
        self.assertIn(str(self.letter(lid)), err, "唤醒负载必须给出信件路径")
        self.assertIn(head[0], err, "唤醒负载必须带信件开头")
        self.assertIn("按信件“需要”字段处理", err, "结尾那句不许变")
        self.assertEqual(self.seen(), [str(self.letter(lid))], "唤醒了就必须写 .seen")
        self.assertEqual(len(self.wake_times()), 1, "一次唤醒只记一次限流")
        self.assertEqual(self.claims(), [f"{lid}.md"], "唤醒过的那封信必须有认领归属")

    # -- 6) 钩子先拿到认领 → 撤回明确被拒 ----------------------------------
    def test_a_claude_wake_blocks_the_retraction_and_keeps_the_claim(self):
        """限流满着的那一轮不许唤醒、不许留认领；限流解除后钩子唤醒 → 认领在钩子名下 → 撤回被拒。"""
        lid = self.send()
        self.spend_wakes(6)
        rc, _ = self.run_hook()
        self.assertEqual(rc, 124, f"限流满时不得唤醒，实际退出码 {rc}")
        self.assertEqual(self.claims(), [], "限流路径不得留下任何认领")
        self.spend_wakes(0)                                   # 清空限流计数（等价于窗口过期）
        rc2, _ = self.run_hook(wait=10)
        self.assertEqual(rc2, 2, f"限流解除后钩子应当正常唤醒，实际退出码 {rc2}")
        self.assertEqual(self.claims(), [f"{lid}.md"], "唤醒过的那封信必须有认领归属")
        r = run_po("retract", "boss", BOX, lid, home=self.home)
        self.assertNotEqual(r.returncode, 0, "唤醒过的信不得再撤回")
        self.assertIn("投递进行中", r.stderr)
        self.assertEqual(len(list((self.home / BOX / "inbox").glob("*.md"))), 1, "原信必须留在原地")
        self.assertEqual(self.claims(), [f"{lid}.md"], "被拒之后认领归属不变")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)