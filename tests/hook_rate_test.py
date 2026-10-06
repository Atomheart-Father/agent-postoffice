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

    def park_on_rate_decision(self, records=6):
        """把钩子真进程停在「已经列出待投、还没决定要不要唤醒」的那个点上。

        `.wake_times` 换成 FIFO：`rate_ok()` 里的 `f.read_text()` 会一直等写端，于是钩子
        停在 postoffice:2177，既没抢认领也没写 `.seen` —— 正是「投递方手里已经有一份待投
        清单」的状态。`records=6` 是「限流已经用满」那一组用例的夹具；`records=0` 则一个
        限流记录都不预填，`feed_rate(fifo, 0)` 之后 `rate_ok()` 放行，用来单独验认领仲裁。

        用例全程额外持有 FIFO 的读端，所以喂数据不必等钩子来读，钩子之后的 `rate_mark()`
        打开 FIFO 追加也不会卡住。
        """
        fifo = self.home / BOX / ".wake_times"
        if records:
            self.spend_wakes(records)
        parked = fifo.with_suffix(".fifo")
        os.mkfifo(parked)
        if fifo.exists():
            fifo.unlink()
        parked.rename(fifo)
        keeper = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)   # 读者常驻：喂数据不必等它读
        self.addCleanup(os.close, keeper)
        self.park_keeper, self.park_fifo = keeper, fifo
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
        `spent=0` 表示喂一份空记录：钩子读到 EOF、记录为空，`rate_ok()` 放行。
        """
        now = time.time()
        fd = os.open(str(fifo), os.O_WRONLY)
        try:
            if spent:
                os.write(fd, "".join(f"{now - i}\n" for i in range(spent)).encode())
        finally:
            os.close(fd)

    def unpark_rate_file(self):
        """把夹具用的 `.wake_times` FIFO 换回普通文件，内容就是钩子真的写进去的限流记录。

        FIFO 只是停靠用的机关：留着它的话，之后任何一次读它都会等一个永远不来的写端。
        常驻的读者 fd 让这里能把钩子写进管道的内容原样取出来，不会丢也不会编。
        """
        data = b""
        while True:
            chunk = os.read(self.park_keeper, 65536)
            if not chunk:
                break
            data += chunk
        real = self.park_fifo.with_suffix(".real")
        real.write_bytes(data)
        os.replace(real, self.park_fifo)
        return data

    def retract(self, lid):
        """跑真正的 `postoffice retract` CLI（不用任何包装），交回 (退出码, stderr)。"""
        env = env_for(self.home)
        p = subprocess.Popen([sys.executable, PO, "retract", "boss", BOX, lid],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, env=env)
        err = p.communicate(timeout=90)[1]
        return p.returncode, err

    # -- 认领仲裁（限流放行，不靠限流挡人）----------------------------------
    def test_a_live_race_lets_only_one_side_win_the_claim(self):
        """限流放行时，钩子与撤回真进程同时抢同一封信的认领：只能有一个赢家。

        这里一个限流记录都不预填（`.wake_times` 压根不存在），所以钩子一定会走到
        `claim_letter`：两边在同一个 O_EXCL 认领文件上真实竞争。20 轮够把两种赢家都逼出来
        （钩子到认领点之前要读的档案比撤回多，天然吃亏，所以轮数留够）。两种起跑顺序交替，
        避免固定的先后偏差；每轮 inbox 里只有这一封信，所以「钩子 rc=2」与「这封信被交付」
        是同一件事，不存在用别的信把赢家说过去的可能。逐轮断言互斥并留下各自的证据。
        """
        self.assertFalse((self.home / BOX / ".wake_times").exists(), "前置：一个限流记录都不许预填")
        wins = {"retract": 0, "hook": 0}
        for i in range(20):
            self.assertEqual(len(list((self.home / BOX / "inbox").glob("*.md"))), 0,
                             f"第 {i} 轮开始前 inbox 必须是空的：上一轮的信已由会话收走")
            lid = self.send(subject=f"竞态信{i}")
            self.assertEqual(len(list((self.home / BOX / "inbox").glob("*.md"))), 1, "本轮 inbox 只应有这一封信")
            cmd_hook = [sys.executable, PO, "hook"]
            cmd_retract = [sys.executable, PO, "retract", "boss", BOX, lid]
            if i % 2 == 0:
                h = self.start_hook()
                r = subprocess.Popen(cmd_retract, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, env=env_for(self.home))
                rerr = r.communicate(timeout=90)[1]
                rrc = r.returncode
            else:
                r = subprocess.Popen(cmd_retract, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, env=env_for(self.home))
                h = self.start_hook()
                rerr = r.communicate(timeout=90)[1]
                rrc = r.returncode
            hrc, herr = self.wait_hook(h, self.hooks[-1][1], wait=5)
            path = str(self.letter(lid))
            retract_ok = rrc == 0
            delivered = path in herr or path in self.seen()      # 这封信真的到了会话手里
            self.assertFalse(retract_ok and (hrc == 2 or delivered),
                             f"第 {i} 轮两边都赢了：撤回 rc={rrc}（{rerr.strip()[:120]}），"
                             f"钩子 rc={hrc}，交付={delivered}")
            if retract_ok:                                    # 撤回赢：认领归它，信进归档
                wins["retract"] += 1
                self.assertNotEqual(hrc, 2, f"第 {i} 轮撤回赢了，钩子不得唤醒")
                self.assertFalse(delivered, f"第 {i} 轮撤回来了，就不许再交付给会话")
                self.assertFalse(self.letter(lid).exists(), f"第 {i} 轮撤回来了，原信应已离开发件箱")
                self.assertEqual(len(list((self.home / BOX / "archived").rglob(f"{lid}.md"))), 1,
                                 f"第 {i} 轮撤回来了，归档里必须有它")
                self.assertNotIn(f"{lid}.md", self.claims(), f"第 {i} 轮撤回放掉了自己的认领")
                self.assertNotIn(path, self.seen(), f"第 {i} 轮撤回来了，不许写 .seen")
            else:                                             # 钩子赢：认领归它，撤回必须被拒
                wins["hook"] += 1
                self.assertEqual(hrc, 2, f"第 {i} 轮撤回没赢，钩子就必须已经唤醒：{rerr.strip()[:120]}")
                self.assertTrue(delivered, f"第 {i} 轮钩子赢了，这封信必须真的进了唤醒负载")
                self.assertIn("无法撤回", rerr, f"第 {i} 轮钩子已经唤醒，撤回必须被明确拒绝")
                self.assertTrue(self.letter(lid).exists(), f"第 {i} 轮信必须仍在 inbox/")
                self.assertIn(path, self.seen(), f"第 {i} 轮唤醒过就必须写 .seen")
                self.assertIn(f"{lid}.md", self.claims(), f"第 {i} 轮认领必须在钩子名下")
            leftover = self.letter(lid)
            if leftover.exists():                 # 会话把被唤醒的那封收进 done/，下一轮从干净 inbox 起跑
                os.replace(leftover, self.home / BOX / "done" / leftover.name)
        self.assertGreater(wins["retract"], 0, "撤回一次都没赢过：竞态没真的跑起来")
        self.assertGreater(wins["hook"], 0, "钩子一次都没赢过：竞态没真的跑起来（两种赢家都必须出现）")
        # 限流从头到尾放行的证据：每一次真唤醒恰好记一次限流，没有任何一轮是被限流挡下的
        self.assertEqual(len(self.wake_times()), wins["hook"],
                         "限流计数必须恰好等于真唤醒次数 —— 说明限流没有挡过任何一轮")

    def test_the_retraction_winning_the_claim_means_no_wake(self):
        """限流放行：撤回抢到认领 → 钩子随后抢不到认领 → 不唤醒、不写 .seen、不留认领。"""
        lid = self.send()
        p, fifo = self.park_on_rate_decision(records=0)       # 一个限流记录都不预填
        rrc, rerr = self.retract(lid)                          # 撤回在钩子排队等认领的时候赢
        self.assertEqual(rrc, 0, rerr)
        self.feed_rate(fifo, 0)                                # 放行：rate_ok() 读到空记录 → True
        hrc, herr = self.wait_hook(p, self.hooks[-1][1], wait=10)
        self.assertNotEqual(hrc, 2, "认领被撤回拿走了，钩子不得唤醒")
        self.assertNotIn(str(self.letter(lid)), herr, "撤回来了就不许出现在唤醒负载里")
        self.assertNotIn(str(self.letter(lid)), self.seen(), "撤回来了就不许写 .seen")
        self.assertNotIn(f"{lid}.md", self.claims(), "撤回放掉了认领，钩子不该再留一个")
        self.assertEqual(len(list((self.home / BOX / "archived").rglob(f"{lid}.md"))), 1, "原信应已归档")
        self.unpark_rate_file()      # FIFO 只是停靠机关，换回普通文件才能读
        self.assertEqual(self.wake_times(), [], "没唤醒就不该记限流（也说明限流确实放行了）")

    def test_the_hook_winning_the_claim_refuses_the_retraction(self):
        """限流放行：钩子抢到认领、唤醒还没落地 → 此刻真撤回必须被拒（投递进行中）。

        停靠点选在「认领已拿到、正在渲染唤醒负载」：认领是对整份待投清单做的（postoffice:2180），
        所以 inbox 里多一封名字排在最前的 FIFO 信就能把钩子卡在 reminder() 里，让「认领在钩子
        名下但还没唤醒」这个瞬间可以被观测，也可以往里插一个真的撤回进程。
        """
        lid = self.send()
        self.assertFalse((self.home / BOX / ".wake_times").exists(), "前置：一个限流记录都不许预填")
        park = self.home / BOX / "inbox" / "0000-park.md"
        os.mkfifo(park)
        self.addCleanup(park.unlink, True)
        reader = os.open(str(park), os.O_RDONLY | os.O_NONBLOCK)     # 读者常驻，钩子的 open 不会失败
        writer = os.open(str(park), os.O_WRONLY)                    # 写端先占住，等下关掉它放行
        self.addCleanup(reader and os.close, reader)
        h = self.start_hook()
        claim = self.home / BOX / ".claims" / f"{lid}.md"
        deadline = time.time() + 15
        while time.time() < deadline and not claim.exists():
            self.assertIsNone(h.poll(), "钩子不该在下台之前就退出")
            time.sleep(0.02)
        self.assertTrue(claim.exists(), "钩子没有拿到认领，夹具没搭起来")
        rrc, rerr = self.retract(lid)                               # 认领在钩子名下时插入真撤回
        self.assertNotEqual(rrc, 0, "钩子已经拿到认领，撤回必须被拒")
        self.assertIn("投递进行中", rerr)
        self.assertTrue(self.letter(lid).exists(), "被拒之后原信必须留在 inbox/")
        self.assertNotIn(str(self.letter(lid)), self.seen(), "唤醒还没落地，.seen 不该有它")
        # 放行钩子的渲染：把 FIFO 换成真信（后续按路径重读时就不再是 FIFO），再关掉写端给 EOF
        real = self.home / BOX / "inbox" / "0000-park.real.md"
        real.write_text(f"收件人：{BOX}\n发件人：boss\n事由：夹具信\n需要：仅告知\n")
        os.replace(real, park)
        os.close(writer)
        hrc, herr = self.wait_hook(h, self.hooks[-1][1], wait=15)
        self.assertEqual(hrc, 2, f"限流放行时钩子应当照常唤醒，实际退出码 {hrc}")
        # 夹具那封 FIFO 信也是真的被投递了，所以唤醒负载里有两封；但认领必须两封都在钩子名下
        self.assertIn(str(self.letter(lid)), self.seen(), "唤醒落地才写 .seen")
        self.assertEqual(sorted(self.claims()), sorted([f"{lid}.md", park.name]),
                         "认领必须留在钩子名下")

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

    # -- 4) 限流已满的一轮里，被撤回的信既不会被唤醒也不会写 .seen ---------------
    def test_a_rate_blocked_hook_leaves_a_retracted_letter_alone(self):
        """限流已满（预填 6 条）时钩子被停在限流判断上，此时撤回完成。

        这一轮赢的是限流、不是认领，所以它只钉限流那一侧的行为：被限流挡下的那一轮，
        不许因为撤回刚刚归档过那封信就去唤醒、更不许把它写进 `.seen`。
        认领仲裁由上面那三条（限流放行）用例负责。
        """
        lid = self.send()
        p, fifo = self.park_on_rate_decision()
        r = run_po("retract", "boss", BOX, lid, home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.letter(lid).exists(), "撤回后原信应已归档")
        self.feed_rate(fifo)
        rc, err = self.wait_hook(p, self.hooks[-1][1], wait=10)
        self.assertNotEqual(rc, 2, "限流已满的一轮不许唤醒")
        self.assertNotIn(str(self.letter(lid)), err, "撤回来了就不许出现在唤醒负载里")
        self.assertEqual(self.seen(), [], "撤回来了就不许写 .seen")
        self.assertEqual(self.claims(), [], "撤回放掉了认领，钩子不该再留一个")
        self.unpark_rate_file()
        # 夹具的 6 条记录是被 rate_ok 读掉的那 6 条；钩子没唤醒，所以一个字节都不该再添
        self.assertEqual(self.wake_times(), [], "没唤醒就不该再记一次限流")

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