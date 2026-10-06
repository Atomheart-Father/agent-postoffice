"""Session self-set one-shot alarm: the local postman scheduler side (OpenCode delivery channel).

An alarm is created by the OpenCode plugin tool from the trusted tool context, lands in
`<HOME>/alarms/<session>.json`, and the resident postman turns it into ONE fixed-sentence letter in
that same physical mailbox once it is due. Identity is re-verified here before anything is written.

Everything runs in a temp dir against the lab `postoffice`; no real mailbox, agent or model is used.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import threading
import subprocess
import sys
import tempfile
import time
import unittest

PO = Path(__file__).resolve().parents[1] / 'postoffice'
_LOCKS = None


def _locks():
    """锁原语直接在本进程里调用（用多线程制造确定的并发），不用起子进程。"""
    global _LOCKS
    if _LOCKS is None:
        import importlib.machinery
        import importlib.util
        # 导入时会算出 HOME；指到本次测试自己的临时目录，免得在真实邮局里留下任何东西
        os.environ['POSTOFFICE_HOME'] = os.environ.get('ALARM_TEST_HOME') or tempfile.mkdtemp(
            prefix='po-alarm-lock-')
        loader = importlib.machinery.SourceFileLoader('po_alarm', str(PO))
        spec = importlib.util.spec_from_loader('po_alarm', loader)
        _LOCKS = importlib.util.module_from_spec(spec)
        loader.exec_module(_LOCKS)
    return _LOCKS


def take_lock(path):
    """Take the kernel lock; returns the held fd (release it) or None when somebody holds it."""
    return _locks().take_alarm_lock(Path(path))


def release_lock(fd):
    _locks().release_alarm_lock(fd)

FIXED = '你设的闹钟到了，请检查刚才安排的任务。'

for _var in ('CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_HOST_SESSION_ID'):
    os.environ.pop(_var, None)


def mkdb(path, sessions):
    """A stand-in for the OpenCode session database: (id, title, directory) triples."""
    con = sqlite3.connect(path)
    con.execute('create table session (id text, title text, directory text, '
                'parent_id text, time_updated integer)')
    for sid, title, directory in sessions:
        con.execute('insert into session values (?,?,?,null,0)', (sid, title, directory))
    con.commit()
    con.close()


class AlarmScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'po'
        self.db = Path(self.tmp.name) / 'opencode.db'
        mkdb(self.db, [('ses_live', '邮局实验', '/tmp'), ('ses_other', '别的会话', '/tmp')])
        self.env = dict(os.environ, POSTOFFICE_HOME=str(self.home), POSTOFFICE_NO_NOTIFY='1',
                        OPENCODE_DB=str(self.db), POSTOFFICE_POLL='1', ALARM_TEST_HOME=str(self.home))
        os.environ['ALARM_TEST_HOME'] = str(self.home)   # 供 _locks() 导入模块时用同一个临时 HOME
        self.run_po('init')
        self.run_po('add', 'lab', '--notify')
        self.routes = json.loads((self.home / 'routes.json').read_text())
        self.routes['lab'].update(methods=['opencode_plugin'], session_id='ses_live', status='online')
        self.routes['ses_other'] = {'methods': ['opencode_plugin'], 'session_id': 'ses_live',
                                    'status': 'online'}   # a second box on the same session
        self.write_routes()

    def tearDown(self):
        self.tmp.cleanup()

    def write_routes(self):
        (self.home / 'routes.json').write_text(json.dumps(self.routes, ensure_ascii=False))

    def run_po(self, *args, body='', check=True, env=None):
        return subprocess.run([os.sys.executable, str(PO), *args], input=body, text=True,
                              capture_output=True, env=env or self.env, check=check, timeout=60)

    def run_postman(self, seconds=2.0, env=None):
        p = subprocess.Popen([os.sys.executable, str(PO), 'postman'],
                             env=env or dict(self.env, POSTOFFICE_POLL='1'),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(seconds)
        finally:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def alarm_path(self, session='ses_live'):
        return self.home / 'alarms' / f'{session}.json'

    def put_alarm(self, due=None, session='ses_live', box='lab', state='pending',
                  letter=None, aid=None, **extra):
        due = time.time() - 1 if due is None else due
        rec = {'id': aid or f'A20260101-000000_{box}', 'box': box, 'session': session,
               'due': due, 'created': time.time() - 600, 'state': state}
        if letter:
            rec['letter'] = letter
        rec.update(extra)
        p = self.alarm_path(session)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, ensure_ascii=False), encoding='utf-8')
        return rec

    def read_alarm(self, session='ses_live'):
        p = self.alarm_path(session)
        return json.loads(p.read_text()) if p.exists() else None

    def inbox(self, box='lab'):
        return sorted(p.name for p in (self.home / box / 'inbox').glob('*.md'))

    def ledger(self, box='lab', file=None):
        f = self.home / 'opencode_delivered.jsonl'
        if not f.exists():
            return []
        out = []
        for line in f.read_text(encoding='utf-8').splitlines():
            if not line:
                continue
            e = json.loads(line)
            if e.get('box') == box and (file is None or e.get('file') == file):
                out.append(e)
        return out

    # --- 到期写入 -------------------------------------------------------------
    def test_not_due_writes_nothing(self):
        self.put_alarm(due=time.time() + 3600)
        self.run_postman()
        self.assertEqual(self.inbox(), [], '未到点不应写任何信')
        self.assertEqual(self.read_alarm()['state'], 'pending', '未到点状态不变')

    def test_due_writes_one_alarm_letter(self):
        rec = self.put_alarm()
        self.run_postman()
        self.assertEqual(len(self.inbox()), 1, '到点应写一封提醒信')
        name = self.inbox()[0]
        self.assertEqual(name, rec['id'] + '.md', '文件名就是闹钟 id（确定性，重启不会写第二封）')
        text = (self.home / 'lab' / 'inbox' / name).read_text(encoding='utf-8')
        head, body = text.split('\n\n', 1)
        self.assertIn(f'闹钟：{rec["id"]}', head.split('\n'), '信头要有闹钟标记')
        self.assertIn('需要：仅告知', head, '闹钟不需要任何人回答')
        self.assertEqual(body.strip(), FIXED, '正文就是那句固定短句')
        self.assertNotIn(f'来源：{rec["box"]}', head, '不要把发信方伪造成收信方自己')

    def test_state_becomes_lettered(self):
        rec = self.put_alarm()
        self.run_postman()
        got = self.read_alarm()
        self.assertEqual(got['state'], 'lettered')
        self.assertEqual(got['letter'], rec['id'])

    def test_letter_present_but_state_pending_is_not_duplicated(self):
        """Crash window: the letter landed, the state save did not."""
        rec = self.put_alarm(state='pending')
        self.write_letter(rec['id'])
        self.run_postman()
        self.assertEqual(len(self.inbox()), 1, '信已存在时不得再写一封')
        self.assertEqual(self.read_alarm()['state'], 'lettered', '应补记为已投递信')

    def test_two_rounds_do_not_duplicate(self):
        self.put_alarm()
        self.run_postman()
        self.run_postman()
        self.assertEqual(len(self.inbox()), 1, 'postman 重启后不得写第二封')

    def mark_delivered_for(self, lid, result='DELIVERED'):
        with (self.home / 'opencode_delivered.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': 'now', 'box': 'lab', 'file': lid + '.md',
                                'session': 'ses_live', 'result': result}) + '\n')

    def test_lettered_record_without_its_letter_is_not_trusted(self):
        """lettered 记录缺 letter 字段（或指错了信）时不拿它判定送达 —— 插件侧同一个口径。

        少写一个字段就让「已送达」成立，等于把活跃记录留在盘上；判定只认 letter == id。
        """
        for extra, why in (({}, '缺 letter'), ({'letter': 'A20260101-000000_other'}, 'letter 指错了信')):
            with self.subTest(why=why):
                self.setUp()
                rec = self.put_alarm(state='lettered', **extra)
                self.write_letter(rec['id'])
                self.mark_delivered_for(rec['id'])
                self.run_postman()
                self.assertIsNotNone(self.read_alarm(),
                                    f'{why} 时不得清掉活跃记录（当成了已送达）')

    def test_lettered_record_whose_letter_matched_is_cleared(self):
        """对照组：letter 字段正确时，记录照常在送达后被清掉。"""
        rec = self.put_alarm(state='lettered', letter='A20260101-000000_lab')
        self.write_letter(rec['id'])
        self.mark_delivered_for(rec['id'])
        self.run_postman()
        self.assertIsNone(self.read_alarm(), 'letter 正确且台账已送达时应清掉记录')

    def test_letter_already_filed_is_not_written_again(self):
        """状态没落盘、而信已被收信方挪走或归档：也必须认得出那封信，不得再响一次。"""
        for sub in ('done', 'archived/20260101-000000'):
            with self.subTest(sub=sub):
                self.setUp()
                rec = self.put_alarm(state='pending')
                src = self.write_letter(rec['id'])
                dst = self.home / 'lab' / sub
                dst.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst / src.name))
                self.run_postman()
                self.assertEqual(self.inbox(), [], f'信已在 {sub} 时不得再往 inbox 写一封')
                self.assertEqual(self.read_alarm()['state'], 'lettered',
                                 f'信已在 {sub} 时也应补记为已投递信')

    # --- 跨进程互斥：邮递员 vs 插件的取消 ---------------------------------------
    # 插件工具（转给 Python 的 alarm-set / alarm-cancel）和邮递员到期用的是同一把内核锁
    # （<session>.json.lock，flock），所以「读完 pending → cancel 删记录 → 才把信投进 inbox」
    # 这个窗口不会发生。
    def lock_path(self, session='ses_live'):
        return self.alarm_path(session).with_suffix('.json.lock')

    def test_alarm_cancelled_before_due_is_never_delivered(self):
        """cancel 抢先删掉记录后，邮递员这一轮什么也不做（模拟同一条记录上 cancel 与 postman 交错）。"""
        rec = self.put_alarm()
        self.alarm_path().unlink()                     # cancel 已经把记录删了
        self.run_postman()
        self.assertEqual(self.inbox(), [], '取消之后不得再冒出提醒信')
        self.assertIsNone(self.read_alarm(), '也不得把记录写回来')

    def test_only_one_writer_holds_the_lock_at_a_time(self):
        self.put_alarm()
        p = self.alarm_path()
        winners = []
        barrier = threading.Barrier(8)

        def grab():
            barrier.wait()
            fd = take_lock(p)
            if fd is not None:
                winners.append(fd)

        ts = [threading.Thread(target=grab) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(len(winners), 1, f'同一时刻只应有一个持锁者（实际 {len(winners)}）')
        release_lock(winners[0])
        self.assertEqual(self.read_alarm()['state'], 'pending', '记录没被动')

    def test_the_kernel_hands_the_lock_over_after_the_holder_is_killed(self):
        """崩溃后接管：持有者被 kill -9，内核放锁，等锁方在有界时间内拿到。

        旧实现靠 mtime 看门狗猜「持有者大概死了」，这里不用猜 —— 内核说它死了就是死了。
        """
        self.put_alarm()
        p = self.alarm_path()
        holder = subprocess.Popen(
            [sys.executable, '-c',
             "import importlib.machinery, importlib.util, sys, os, time\n"
             "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
             "spec=importlib.util.spec_from_loader('po', loader)\n"
             "m=importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
             "fd=m.take_alarm_lock(m.alarm_file('ses_live'))\n"
             "print('HELD' if fd is not None else 'BUSY', flush=True)\n"
             "while not os.path.exists(sys.argv[2]): time.sleep(0.05)", str(PO), str(self.home / 'stop')],
            stdout=subprocess.PIPE, text=True,
            env=dict(os.environ, POSTOFFICE_HOME=str(self.home)))
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'HELD', '持有者应拿到锁')
        self.assertIsNone(take_lock(p), '别人拿不到')
        (self.home / 'stop').write_text('x')
        holder.wait(15)
        deadline = time.time() + 10
        fd = None
        while fd is None and time.time() < deadline:
            fd = take_lock(p)
            time.sleep(0.05)
        self.assertIsNotNone(fd, '持有者被 kill 后锁必须能被拿到')
        release_lock(fd)

    def test_the_kernel_releases_a_lock_whose_holder_was_sigkilled(self):
        """崩溃（无善后）的接管：kill -9 之后内核立刻放锁，不靠任何看门狗去猜。"""
        self.put_alarm()
        p = self.alarm_path()
        holder = subprocess.Popen(
            [sys.executable, '-c',
             "import importlib.machinery, importlib.util, sys, os, time\n"
             "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
             "spec=importlib.util.spec_from_loader('po', loader)\n"
             "m=importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
             "fd=m.take_alarm_lock(m.alarm_file('ses_live'))\n"
             "print('HELD' if fd is not None else 'BUSY', flush=True)\n"
             "while True: time.sleep(0.05)", str(PO)],
            stdout=subprocess.PIPE, text=True,
            env=dict(os.environ, POSTOFFICE_HOME=str(self.home)))
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'HELD')
        self.assertIsNone(take_lock(p))
        holder.kill()
        holder.wait(10)
        deadline = time.time() + 10
        fd = None
        while fd is None and time.time() < deadline:
            fd = take_lock(p)
            time.sleep(0.05)
        self.assertIsNotNone(fd, '被 kill -9 的持有者留下的锁必须立刻能拿到')
        release_lock(fd)

    def test_a_paused_holder_keeps_the_lock_past_the_old_hard_timeout(self):
        """暂停恢复：旧实现在 600 秒硬超时后会无视 pid 回收，于是暂停的持有者恢复时会删掉新锁。

        这里把持有者 SIGSTOP 住，等它「暂停」的时间远超旧硬超时，另一方仍必须拿不到锁；
        再 SIGCONT 放行，等锁方才拿到。这正是旧实现出问题的那个场景。
        """
        self.put_alarm()
        p = self.alarm_path()
        holder = subprocess.Popen(
            [sys.executable, '-c',
             "import importlib.machinery, importlib.util, sys, os, time\n"
             "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
             "spec=importlib.util.spec_from_loader('po', loader)\n"
             "m=importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
             "fd=m.take_alarm_lock(m.alarm_file('ses_live'))\n"
             "print('HELD' if fd is not None else 'BUSY', flush=True)\n"
             "while not os.path.exists(sys.argv[2]): time.sleep(0.05)", str(PO), str(self.home / 'stop')],
            stdout=subprocess.PIPE, text=True,
            env=dict(os.environ, POSTOFFICE_HOME=str(self.home)))
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'HELD')
        holder.send_signal(signal.SIGSTOP)
        time.sleep(1.5)                       # 这里不真等 600s：flock 没有硬超时，一秒就够证明没有超时逻辑
        self.assertIsNone(take_lock(p), '持有者只是被暂停（还活着），锁不能被别人抢走')
        holder.send_signal(signal.SIGCONT)
        (self.home / 'stop').write_text('x')
        deadline = time.time() + 10
        fd = None
        while fd is None and time.time() < deadline:
            fd = take_lock(p)
            time.sleep(0.05)
        self.assertIsNotNone(fd, '持有者恢复并退出后，锁必须能被拿到')
        release_lock(fd)

    def test_releasing_only_touches_our_own_descriptor(self):
        """释放不可能删掉别人的锁：释放路径上根本没有 unlink，只有 flock(LOCK_UN) + close。"""
        self.put_alarm()
        p = self.alarm_path()
        fd = take_lock(p)
        self.assertIsNotNone(fd)
        lock = self.lock_path()
        self.assertTrue(lock.exists(), '锁文件只建不删')
        before = lock.stat().st_ino
        release_lock(fd)
        self.assertEqual(lock.stat().st_ino, before, '释放不换文件')
        fd2 = take_lock(p)
        self.assertIsNotNone(fd2)
        release_lock(fd2)


    # --- 身份重新核证 ---------------------------------------------------------
    def test_refused_when_box_not_registered(self):
        self.put_alarm(box='幽灵')
        self.run_postman()
        self.assertEqual(self.inbox(), [], '信箱不存在时不得写任何信')
        self.assertEqual(self.read_alarm()['state'], 'refused')

    def test_refused_when_session_rebound(self):
        self.put_alarm()
        self.routes['lab']['session_id'] = 'ses_elsewhere'
        self.write_routes()
        self.run_postman()
        self.assertEqual(self.inbox(), [], '信箱改绑别的会话后不得投递')
        self.assertEqual(self.read_alarm()['state'], 'refused')

    def test_refused_when_method_lost(self):
        self.put_alarm()
        self.routes['lab']['methods'] = ['notify']
        self.write_routes()
        self.run_postman()
        self.assertEqual(self.inbox(), [], '信箱不再走插件通道时不得投递')
        self.assertEqual(self.read_alarm()['state'], 'refused')

    def test_refused_when_session_absent_from_db(self):
        self.put_alarm(session='ses_never_existed')
        self.run_postman()
        self.assertEqual(self.inbox('lab'), [], 'OpenCode 里没有这个会话时不得投递')
        self.assertEqual(self.read_alarm('ses_never_existed')['state'], 'refused')

    def test_missing_db_does_not_refuse(self):
        """No database to verify against is not the same as a session that does not exist."""
        self.put_alarm()
        env = dict(self.env, OPENCODE_DB=str(Path(self.tmp.name) / 'missing.db'))
        p = subprocess.Popen([os.sys.executable, str(PO), 'postman'], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2.0)
        finally:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        self.assertEqual(len(self.inbox()), 1, '没有数据库可核证时不应误判为身份不明')

    def test_refusal_is_logged_with_reason(self):
        self.put_alarm(box='幽灵')
        self.run_postman()
        text = (self.home / 'logs' / 'postman.log').read_text(encoding='utf-8')
        self.assertIn('闹钟', text)
        self.assertIn('未登记', text, '日志要写明拒绝原因')

    # --- 离线与未送达 ---------------------------------------------------------
    def test_offline_box_keeps_the_letter_until_recovery(self):
        """离线时提醒留在 inbox 等恢复，不假报成功、也不丢。"""
        self.routes['lab']['status'] = 'offline'
        self.write_routes()
        self.put_alarm()
        self.run_postman()
        self.assertEqual(len(self.inbox()), 1, '离线时提醒要留在 inbox 等恢复')
        self.assertEqual(self.read_alarm()['state'], 'lettered', '提醒已排队，等上线后送')
        self.assertEqual(self.ledger(), [], '离线时不得有任何送达记账')

    def test_grace_monitor_ignores_alarm_letters(self):
        """会话只是忙（例如正在跑长任务）时闹钟才响，这是正常情况，不该为此骚扰人。

        对照组：同一次运行里普通信确实会触发这页提醒，所以断言的是"只多了一页"。
        """
        self.put_alarm()
        self.run_po('send', 'lab', 'someone', '普通信对照', '回复', body='正文\n')
        fast = dict(self.env, POSTOFFICE_GRACE='0')
        self.run_postman(env=fast)
        self.assertEqual(len(self.inbox()), 2, '闹钟提醒与普通信都在 inbox')
        blob = (self.home / 'logs' / 'notify.log').read_text(encoding='utf-8')
        self.assertEqual(blob.count('20 分钟未被唤醒'), 1,
                         '只有普通信该触发"20 分钟未被唤醒"，闹钟信不得触发')

    def test_alarm_letter_is_not_a_nudge(self):
        """The 30-minute nudge only targets letters that ask for something."""
        self.put_alarm()
        self.run_postman()
        name = self.inbox()[0]
        rc = self.run_po('hook', check=False, body='{"transcript_path":"/nonexistent.jsonl"}')
        self.assertIn(rc.returncode, (0, 2), 'hook 不该因闹钟信崩掉')
        text = (self.home / 'logs' / 'notify.log')
        blob = text.read_text(encoding='utf-8') if text.exists() else ''
        self.assertNotIn(name, blob, '仅告知的闹钟信不该进 30 分钟催办')

    # --- 送达后清理 -----------------------------------------------------------
    def mark_delivered(self, result='DELIVERED'):
        rec = self.read_alarm()
        with (self.home / 'opencode_delivered.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': 'now', 'box': 'lab', 'file': rec['letter'] + '.md',
                                'session': 'ses_live', 'result': result}) + '\n')

    def test_delivered_clears_the_alarm(self):
        self.put_alarm()
        self.run_postman()
        self.mark_delivered()
        self.run_postman()
        self.assertIsNone(self.read_alarm(), '送达后应清掉活跃闹钟记录')

    def test_failed_final_also_clears_the_alarm(self):
        self.put_alarm()
        self.run_postman()
        self.mark_delivered(result='FAILED_FINAL')
        self.run_postman()
        self.assertIsNone(self.read_alarm(), '投递失败到上限也算这次闹钟结束')

    def alarm_cli(self, *args):
        return subprocess.run([sys.executable, str(PO), *args], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def test_cancel_of_a_failed_delivery_does_not_say_delivered(self):
        """FAILED_FINAL 是失败且收件状态不确定，取消时不得说「已经送达过了」。"""
        rec = self.put_alarm()
        self.run_postman()
        self.mark_delivered(result='FAILED_FINAL')
        r = self.alarm_cli('alarm-cancel', '--box', 'lab', '--session', 'ses_live')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('无法确认收件方是否收到', r.stdout)
        self.assertNotIn('已经送达过了', r.stdout)
        self.assertIsNone(self.read_alarm(), '记录仍要清掉')

    def test_cancel_of_a_delivered_alarm_says_delivered(self):
        """对照组：真送达时仍然说已送达。"""
        rec = self.put_alarm()
        self.run_postman()
        self.mark_delivered(result='DELIVERED')
        r = self.alarm_cli('alarm-cancel', '--box', 'lab', '--session', 'ses_live')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('已经送达过了', r.stdout)

    def test_alarm_set_refuses_when_the_route_changed_while_waiting(self):
        """锁内重核路由：等到锁的这段时间里信箱被改绑，就不能落记录。"""
        rec = {'box': 'lab', 'session': 'ses_live'}
        routes = json.loads((self.home / 'routes.json').read_text())
        routes['lab']['session_id'] = 'ses_someone_else'
        (self.home / 'routes.json').write_text(json.dumps(routes))
        r = self.alarm_cli('alarm-set', '--box', 'lab', '--session', 'ses_live', '--delay', '10')
        self.assertNotEqual(r.returncode, 0, '改绑之后必须拒绝')
        self.assertIn('绑的是别的会话', r.stderr)
        self.assertIsNone(self.read_alarm(), '不得留下任何记录')

    def test_alarm_cancel_refuses_when_the_route_changed_while_waiting(self):
        """同样地：改绑之后 cancel 不得动别人的记录。"""
        self.put_alarm()
        routes = json.loads((self.home / 'routes.json').read_text())
        routes['lab']['session_id'] = 'ses_someone_else'
        (self.home / 'routes.json').write_text(json.dumps(routes))
        r = self.alarm_cli('alarm-cancel', '--box', 'lab', '--session', 'ses_live')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('绑的是别的会话', r.stderr)
        self.assertIsNotNone(self.read_alarm(), '记录必须留着')
        self.assertEqual(len(self.inbox()), 0, '不得动信箱里的任何文件')

    def test_alarm_cancel_still_works_for_a_box_that_went_offline(self):
        """对照组：信箱只是离线（身份仍唯一）时 cancel 仍允许，只清定时器不碰 inbox。"""
        rec = self.put_alarm()
        routes = json.loads((self.home / 'routes.json').read_text())
        routes['lab']['status'] = 'offline'
        (self.home / 'routes.json').write_text(json.dumps(routes))
        r = self.alarm_cli('alarm-cancel', '--box', 'lab', '--session', 'ses_live')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('离线', r.stdout)
        self.assertIsNone(self.read_alarm())

        # --- 真正的锁等待：命令必须是在排队期间路由才被改的 -----------------------
    # 前两条负例只是「先改路由再跑命令」，锁外核验的旧实现同样会拒绝，所以它们证明不了
    # 「核验搬进锁内」这件事。这里让子进程真握住同一把 <session>.json.lock，命令确实
    # 排在 flock 上，等它进入等待之后才改路由，再放锁。
    def hold_alarm_lock(self):
        """Child that takes the same per-session flock and holds it until `stop` appears."""
        holder = subprocess.Popen(
            [sys.executable, '-c',
             "import importlib.machinery, importlib.util, sys, os, time\n"
             "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
             "spec=importlib.util.spec_from_loader('po', loader)\n"
             "m=importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
             "fd=m.take_alarm_lock(m.alarm_file(sys.argv[2]))\n"
             "open(sys.argv[3], 'w').write('held')\n"
             "while not os.path.exists(sys.argv[4]): time.sleep(0.02)",
             str(PO), 'ses_live', str(self.home / 'holding'), str(self.home / 'stop')],
            env=dict(os.environ, POSTOFFICE_HOME=str(self.home)))
        self.addCleanup(holder.kill)
        for _ in range(400):
            if (self.home / 'holding').exists():
                break
            time.sleep(0.05)
        else:
            self.fail('持锁子进程没能在限定时间内拿到锁')
        self.assertIsNone(take_lock(self.alarm_path()), '锁确实被那个子进程握着')
        return holder

    def release_alarm_lock(self, holder):
        (self.home / 'stop').write_text('go')
        holder.wait(20)
        for _ in range(400):
            fd = take_lock(self.alarm_path())
            if fd is not None:
                release_lock(fd)
                return
            time.sleep(0.05)
        self.fail('放锁后仍拿不到锁')

    def set_route(self, **changes):
        p = self.home / 'routes.json'
        r = json.loads(p.read_text())
        r['lab'].update(changes)
        p.write_text(json.dumps(r, ensure_ascii=False))

    def test_alarm_set_refuses_when_the_route_is_rebound_while_it_waits(self):
        holder = self.hold_alarm_lock()
        cmd = subprocess.Popen([sys.executable, str(PO), 'alarm-set', '--box', 'lab',
                                '--session', 'ses_live', '--delay', '10'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=self.env)
        self.addCleanup(cmd.kill)
        time.sleep(1.0)                      # 让它真的排在 flock 上
        self.assertIsNone(cmd.poll(), '命令应当在等锁而不是已经退出')
        self.set_route(session_id='ses_someone_else')      # 等锁期间改绑
        self.release_alarm_lock(holder)
        _, err = cmd.communicate(timeout=30)
        self.assertNotEqual(cmd.returncode, 0)
        self.assertIn('绑的是别的会话', err)
        self.assertIsNone(self.read_alarm(), '拒绝时不得留下任何记录')

    def test_alarm_set_still_works_when_the_route_is_unchanged_while_it_waits(self):
        """对照组：锁被握着、但路由没变时，命令等到锁之后应当成功。"""
        holder = self.hold_alarm_lock()
        cmd = subprocess.Popen([sys.executable, str(PO), 'alarm-set', '--box', 'lab',
                                '--session', 'ses_live', '--delay', '10'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=self.env)
        self.addCleanup(cmd.kill)
        time.sleep(1.0)
        self.assertIsNone(cmd.poll(), '命令应当在等锁')
        self.release_alarm_lock(holder)
        out, err = cmd.communicate(timeout=30)
        self.assertEqual(cmd.returncode, 0, err)
        self.assertIn('已设闹钟', out)
        self.assertIsNotNone(self.read_alarm(), '成功时必须落下记录')

    def test_alarm_cancel_refuses_when_the_route_is_rebound_while_it_waits(self):
        self.put_alarm()
        holder = self.hold_alarm_lock()
        cmd = subprocess.Popen([sys.executable, str(PO), 'alarm-cancel', '--box', 'lab',
                                '--session', 'ses_live'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=self.env)
        self.addCleanup(cmd.kill)
        time.sleep(1.0)
        self.assertIsNone(cmd.poll(), '命令应当在等锁')
        self.set_route(session_id='ses_someone_else')      # 等锁期间改绑
        self.release_alarm_lock(holder)
        _, err = cmd.communicate(timeout=30)
        self.assertNotEqual(cmd.returncode, 0)
        self.assertIn('绑的是别的会话', err)
        self.assertIsNotNone(self.read_alarm(), '拒绝时记录必须留着')
        self.assertEqual(self.inbox(), [], '拒绝时不得动信箱里的任何文件')

    def test_alarm_cancel_refuses_a_rebound_session_even_while_the_box_is_offline(self):
        """离线不是身份豁免：信箱标着 offline，但 session 已改绑给别人时不得取消。"""
        self.put_alarm()
        self.set_route(status='offline', session_id='ses_someone_else')
        r = self.alarm_cli('alarm-cancel', '--box', 'lab', '--session', 'ses_live')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('绑的是别的会话', r.stderr)
        self.assertIsNotNone(self.read_alarm(), '记录必须留着')
        self.assertEqual(self.inbox(), [], '不得动信箱里的任何文件')

    def test_alarm_cancel_refuses_a_disabled_channel_even_while_the_box_is_offline(self):
        """同上，但失效的是 methods：信箱不再走插件通道时也不许按旧归属取消。"""
        self.put_alarm()
        self.set_route(status='offline', methods=['notify'])
        r = self.alarm_cli('alarm-cancel', '--box', 'lab', '--session', 'ses_live')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('不再由 OpenCode 插件投递', r.stderr)
        self.assertIsNotNone(self.read_alarm())
        self.assertEqual(self.inbox(), [])

    def test_undelivered_alarm_stays(self):
        self.put_alarm()
        self.run_postman()
        self.run_postman()
        self.assertIsNotNone(self.read_alarm(), '还没送达时记录要留着，排队等下一次')

    # --- 坏数据不拖垮邮递员 ---------------------------------------------------
    def test_broken_alarm_file_does_not_crash_the_postman(self):
        (self.home / 'alarms').mkdir(parents=True, exist_ok=True)
        (self.home / 'alarms' / 'ses_broken.json').write_text('{ 这不是 JSON')
        (self.home / 'alarms' / 'no_fields.json').write_text('{"hello": 1}')
        self.put_alarm(session='ses_live')       # 身份可核证的那条
        self.run_postman()
        self.assertEqual(len(self.inbox()), 1, '坏文件不能挡住其它闹钟')
        self.assertEqual(self.read_alarm()['state'], 'lettered')

    def write_letter(self, aid, box='lab'):
        d = self.home / box / 'inbox'
        d.mkdir(parents=True, exist_ok=True)
        p = d / f'{aid}.md'
        p.write_text(f'来源：postoffice（协作者）\n事由：闹钟\n需要：仅告知\n闹钟：{aid}\n\n{FIXED}\n',
                     encoding='utf-8')
        return p

    # --- 取消 vs 投递：完整并发语义 -------------------------------------------
    # 到期写成信（state=lettered）之后，取消必须同时负责三件事：把那封还没人收的提醒从
    # 投递队列里收走、清掉活动记录、并且**如实**说明到底能不能保证不再响。
    # 这一节的每一条都是先复现后落成用例的；本阶段只写测试，不改实现。
    def cancel_cli(self, box='lab', session='ses_live'):
        return subprocess.run([sys.executable, str(PO), 'alarm-cancel', '--box', box,
                               '--session', session], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def add_box(self, name, status='online'):
        """A mailbox of its own: one rate-limit window and one .claims dir per case."""
        self.run_po('add', name, '--notify')
        p = self.home / 'routes.json'
        r = json.loads(p.read_text())
        r[name].update(methods=['opencode_plugin'], session_id='ses_live', status=status)
        p.write_text(json.dumps(r, ensure_ascii=False))
        self.routes = r
        return name

    def archived(self, box='lab'):
        return sorted(p.name for p in (self.home / box / 'archived').rglob('*.md'))

    def claims(self, box='lab'):
        d = self.home / box / '.claims'
        return sorted(p.name for p in d.glob('*')) if d.is_dir() else []

    def delivery_side(self, box, lid, marker):
        """A child that does exactly what the plugin does per letter: take the shared claim, and
        only if it won, "prompt" (the marker) — then keep the claim, like a delivery that is on its
        way. The ledger row is deliberately NOT written: that is the race window we care about
        (claim held, wake-up in flight, nothing recorded yet)."""
        code = ("import importlib.machinery, importlib.util, sys\n"
                "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
                "spec=importlib.util.spec_from_loader('po', loader)\n"
                "mod=importlib.util.module_from_spec(spec); loader.exec_module(mod)\n"
                "p=mod.HOME/sys.argv[2]/'inbox'/sys.argv[3]\n"
                "won=mod.claim_letter(sys.argv[2], p)\n"
                "print('CLAIM', won, flush=True)\n"
                "if won:\n"
                "    open(sys.argv[4], 'a').write('prompted')\n")
        return subprocess.Popen([sys.executable, '-c', code, PO, box, lid + '.md', str(marker)],
                                stdout=subprocess.PIPE, text=True,
                                env=dict(os.environ, POSTOFFICE_HOME=str(self.home)))

    # C-3 (1) offline + lettered：提醒必须被收走
    def test_cancel_files_the_reminder_even_while_the_box_is_offline(self):
        """信已经落进 inbox（lettered）之后信箱才下线，cancel 仍必须把那封提醒收走。

        旧实现只清掉活动记录就返回，还顺手说一句「没有待送达的提醒需要撤回」——而 inbox 里
        明明就躺着那封没人收的提醒。记录没了之后没有任何人再管它：收信方上线时它会按普通信
        投递（还会展开信头和路径），并且永远不会被归档，cancel 也再也够不着它。
        """
        self.put_alarm()
        self.run_postman()                              # 到期 → lettered，提醒进了 inbox
        self.set_route(status='offline')               # 之后才下线
        r = self.cancel_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('没有待送达的提醒', r.stdout,
                         'inbox 里明明有那封提醒，不得说「没有待送达的提醒」：' + r.stdout)
        self.assertEqual(self.inbox(), [], '离线时那封没人收的提醒也必须被收走')
        self.assertEqual(self.archived(), ['A20260101-000000_lab.md'],
                         '提醒要存档而不是删掉：' + str(self.archived()))
        self.assertIsNone(self.read_alarm(), '活动记录照旧清掉')

    # C-3 (3) 投递方拿着认领：cancel 不得声称保证成功
    def test_cancel_does_not_guarantee_while_the_delivery_side_holds_the_claim(self):
        """插件已经抢到 .claims/<letter>.md（马上要 promptAsync）时，取消无法保证成功。

        认领是投递方和撤回方共用的那把锁（postoffice retract 就靠它判胜负）。cancel 从来没碰
        过它，所以现在会照旧把信从 inbox 移走并回一句「不会再响」——而那一轮唤醒照样发生。
        """
        self.put_alarm()
        self.run_postman()
        lid = self.read_alarm()['letter']
        (self.home / 'lab' / '.claims').mkdir(parents=True, exist_ok=True)
        (self.home / 'lab' / '.claims' / f'{lid}.md').write_text('4242\n')
        r = self.cancel_cli()
        said = r.stdout + r.stderr
        self.assertRegex(said, r'无法保证|不能保证|已进入投递|投递进行中',
                         '认领已被投递方抢到时必须明说无法保证取消：' + said)
        self.assertNotIn('不会再响', said,
                         '投递已经在路上时不得声称「不会再响」：' + said)

    # C-3 (4) cancel 赢：投递方拿着旧路径来认领必须失败，且不留残留认领
    def test_cancel_wins_so_a_stale_delivery_claim_cannot_wake_anybody(self):
        """对照组：cancel 成功之后，投递方拿着删除前那份路径来认领必须失败。

        这是「cancel 赢 → 不会再响」那一半的护栏。现在它靠的是投递侧 claim_letter 自己的
        stat 复查（认领之后确认信还在 inbox），所以这条今天就是绿的：改坏任一边都会红。
        """
        self.put_alarm()
        self.run_postman()
        lid = self.read_alarm()['letter']
        stale = self.home / 'lab' / 'inbox' / f'{lid}.md'
        self.assertTrue(stale.exists())
        self.assertEqual(self.cancel_cli().returncode, 0)
        self.assertEqual(self.claims(), [], 'cancel 不得留下自己的认领')
        p = subprocess.run(
            [sys.executable, '-c',
             "import importlib.machinery, importlib.util, sys\n"
             "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
             "spec=importlib.util.spec_from_loader('po', loader)\n"
             "mod=importlib.util.module_from_spec(spec); loader.exec_module(mod)\n"
             "print('CLAIM', mod.claim_letter('lab', mod.HOME/'lab'/'inbox'/sys.argv[2]))",
             PO, f'{lid}.md'], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, POSTOFFICE_HOME=str(self.home)))
        self.assertIn('CLAIM False', p.stdout, '投递方拿着旧路径不得再拿到认领：' + p.stdout)
        self.assertEqual(self.claims(), [], '也不得留下残留认领')

    # C-3 (5) 真进程多轮 race：cancel 与投递只能有一个赢
    def test_cancel_and_delivery_never_both_win(self):
        """两边真的同时起跑，只断言那条不变量：绝不允许「投递已经唤醒 + cancel 声称不会再响」。

        投递方在子进程里用与插件同一个 claim_letter 原语抢认领，抢到就写一个 marker 当作
        「已经 prompt 了」；取消方跑真正的 CLI。两边抢的是同一个 .claims 文件，所以每一轮
        必然有一方赢 —— 断言的是「谁赢谁说了算」，不规定哪一方赢。偶数轮让投递方先起跑 0.2s、
        奇数轮让 cancel 先起跑 0.2s，两个方向都要被真实走到。注意在当前实现下**每一轮都是
        投递赢**：cancel 根本不参与抢认领，先起跑也没用，那正是这条用例要抓的根因。
        """
        for i in range(6):
            box = self.add_box(f'racelab{i}')
            self.put_alarm(box=box)
            self.run_postman()                      # 那条闹钟到期，提醒进这个信箱
            rec = self.read_alarm()
            self.assertEqual(rec['box'], box)
            lid = rec['letter']
            marker = self.home / f'prompted{i}'

            def start_cancel():
                return subprocess.Popen(
                    [sys.executable, str(PO), 'alarm-cancel', '--box', box,
                     '--session', 'ses_live'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)

            if i % 2 == 0:                          # 投递方先起跑，晚到的 cancel 该抢不到认领
                delivery = self.delivery_side(box, lid, marker)
                self.addCleanup(delivery.kill)
                time.sleep(0.2)
                cancel = start_cancel()
            else:                                   # cancel 先起跑，晚到的投递方该抢不到
                cancel = start_cancel()
                time.sleep(0.2)
                delivery = self.delivery_side(box, lid, marker)
                self.addCleanup(delivery.kill)
            self.addCleanup(cancel.kill)
            dout, _ = delivery.communicate(timeout=60)
            cout, cerr = cancel.communicate(timeout=60)
            said = (cout or '') + (cerr or '')
            if marker.exists():                    # 投递赢：它已经 prompt 了
                self.assertNotIn('不会再响', said,
                                 f'第 {i} 轮投递已经抢到认领并唤醒，cancel 不得说不会再响：{said}')
                self.assertRegex(said, r'无法保证|不能保证|已进入投递|投递进行中',
                                 f'第 {i} 轮投递赢时必须明说无法保证取消：{said}')
            else:                                  # cancel 赢：提醒必须被收走，且不留认领
                self.assertEqual(cancel.returncode, 0, f'第 {i} 轮：{said}')
                self.assertIn('不会再响', said, f'第 {i} 轮 cancel 赢时该说不会再响：{said}')
                self.assertEqual(sorted(p.name for p in (self.home / box / 'inbox').glob('*.md')),
                                 [], f'第 {i} 轮 cancel 赢时提醒必须被收走：{said}')
                self.assertEqual(self.claims(box), [],
                                 f'第 {i} 轮 cancel 赢时不得留下认领：{self.claims(box)}')
            self.assertIn('CLAIM', dout, '投递方子进程应当报告自己有没有抢到认领：' + dout)

    # C-3 (6) 台账 fail closed：未知结果既不当送达、也不当未送达，而且整条拒绝
    def test_an_unknown_ledger_result_is_not_read_as_undelivered(self):
        """result=FAILED_UNKNOWN 必须整条拒绝：记录原地不动、信原地不动、非零退出。

        上一轮这里只把措辞改成「因此不能保证取消」，随后照样走 alarm_file(sid).unlink()
        并返回 0 —— 那不是 fail closed：台账说结果未知时，唯一诚实的动作是什么都不做，
        并且把「没有取消」「哪里都没改」说出来，让调用方（插件工具 → 模型）能转述。
        """
        self.put_alarm()
        self.run_postman()
        before = self.read_alarm()
        lid = before['letter']
        self.mark_delivered(result='FAILED_UNKNOWN')
        r = self.cancel_cli()
        said = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, '结果未知时必须非零退出：' + said)
        self.assertNotIn('已取消闹钟', said, '不得以成功前缀开头：' + said)
        self.assertRegex(said, r'未知|无法判定|没有取消',
                         '必须明说结果未知、没有取消：' + said)
        self.assertRegex(said, r'都没有改动|原样',
                         '必须明说活动记录与那封信都没有改动：' + said)
        self.assertEqual(self.read_alarm(), before, '活动记录必须原地保留、内容一个字节不变')
        self.assertEqual(self.inbox(), [lid + '.md'], '那封提醒必须原地留在 inbox')
        self.assertEqual(self.archived(), [], '不得归档、不得移走那封提醒')
        self.assertEqual(self.claims(), [], '拒绝路径不得留下认领')

    def test_failed_retryable_still_counts_as_not_delivered(self):
        """对照组，必须保持不变：FAILED_RETRYABLE 是「已知没送到、还会重试」，所以收得走。

        它和上面那条成对使用：fail closed 只针对**认不出来**的结果值。已知未送达不能一起改成
        不敢收，否则用户永远撤不回一个还在重试队列里的提醒。
        """
        self.put_alarm()
        self.run_postman()
        self.mark_delivered(result='FAILED_RETRYABLE')
        r = self.cancel_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.inbox(), [], '已知未送达的提醒应当被收走：' + r.stdout)
        self.assertEqual(self.archived(), ['A20260101-000000_lab.md'])
        self.assertIsNone(self.read_alarm())

    # --- precheck 与 claim 之间的窗口：台账变了也必须完整重判 --------------------
    # cancel 先在锁内读一次台账（precheck），再去抢认领；这中间投递方可能刚好写完结局。
    # 窗口用受控停靠造出来：子进程 import 单文件后把 claim_letter 包一层，先落下 `paused`
    # 文件、等测试放行，再调用真正的 claim_letter。停在哪一步由测试决定、由文件交接驱动，
    # 与调度/概率无关，重复多少次结果都一样。
    def cancel_with_claim_paused(self, mutate):
        paused, resume = self.home / 'paused', self.home / 'resume'
        code = (
            "import importlib.machinery, importlib.util, sys, os, time\n"
            "loader=importlib.machinery.SourceFileLoader('po', sys.argv[1])\n"
            "spec=importlib.util.spec_from_loader('po', loader)\n"
            "mod=importlib.util.module_from_spec(spec); loader.exec_module(mod)\n"
            "real=mod.claim_letter\n"
            "def paused(box, path):\n"
            "    open(os.environ['PO_PAUSED'], 'w').write('at-claim')\n"
            "    while not os.path.exists(os.environ['PO_RESUME']): time.sleep(0.02)\n"
            "    return real(box, path)\n"
            "mod.claim_letter=paused\n"
            "sys.argv=[sys.argv[0], 'alarm-cancel', '--box', 'lab', '--session', 'ses_live']\n"
            "sys.exit(mod.main())\n")
        cmd = subprocess.Popen(
            [sys.executable, '-c', code, str(PO)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(self.env, PO_PAUSED=str(paused), PO_RESUME=str(resume)))
        self.addCleanup(cmd.kill)
        for _ in range(600):
            if paused.exists():
                break
            if cmd.poll() is not None:
                out, err = cmd.communicate()
                self.fail(f'取消进程没停在 claim 之前就退出了（rc={cmd.returncode}）：{out}{err}')
            time.sleep(0.05)
        else:
            self.fail('取消进程没有停在 claim 之前（超时）')
        mutate()                       # 就在这一刻改台账：precheck 已经读过，claim 还没发生
        resume.write_text('go')
        out, err = cmd.communicate(timeout=60)
        return subprocess.CompletedProcess(cmd.args, cmd.returncode, out, err)

    def test_delivered_written_between_precheck_and_claim_is_still_read_as_delivered(self):
        """窗口里出现的 DELIVERED 必须认出来：不归档，只说已送达。"""
        self.put_alarm()
        self.run_postman()
        lid = self.read_alarm()['letter']
        r = self.cancel_with_claim_paused(lambda: self.mark_delivered(result='DELIVERED'))
        said = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, said)
        self.assertIn('已经送达过了', said, '抢到认领后复查到 DELIVERED 要如实说：' + said)
        self.assertNotIn('不会再响', said, '已送达的提醒不能归档、也就不能说不会再响：' + said)
        self.assertEqual(self.inbox(), [lid + '.md'], '已送达的提醒原地不动')
        self.assertEqual(self.archived(), [], '不归档')
        self.assertEqual(self.claims(), [], '认领要放回去')
        self.assertIsNone(self.read_alarm(), '记录照旧清掉')

    def test_unknown_written_between_precheck_and_claim_fails_closed(self):
        """窗口里出现的未知结果必须 fail closed：不删记录、不动信、非零退出。"""
        self.put_alarm()
        self.run_postman()
        before = self.read_alarm()
        lid = before['letter']
        r = self.cancel_with_claim_paused(lambda: self.mark_delivered(result='FAILED_UNKNOWN'))
        said = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, '结果未知时必须非零退出：' + said)
        self.assertNotIn('已取消闹钟', said, '不得以成功前缀开头：' + said)
        self.assertRegex(said, r'未知|无法判定|没有取消', '必须明说结果未知、没有取消：' + said)
        self.assertRegex(said, r'都没有改动|原样', '必须明说哪里都没动：' + said)
        self.assertEqual(self.read_alarm(), before, '活动记录必须原地保留、内容不变')
        self.assertEqual(self.inbox(), [lid + '.md'], '那封提醒必须原地留在 inbox')
        self.assertEqual(self.archived(), [], '不得归档、不得移走那封提醒')
        self.assertEqual(self.claims(), [], '拒绝路径也必须把刚抢到的认领放回去')

    def test_failed_final_written_between_precheck_and_claim_keeps_the_letter(self):
        """窗口里出现的 FAILED_FINAL 按既定语义处理：信留 inbox、如实说无法确认。"""
        self.put_alarm()
        self.run_postman()
        lid = self.read_alarm()['letter']
        r = self.cancel_with_claim_paused(lambda: self.mark_delivered(result='FAILED_FINAL'))
        said = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, 'FAILED_FINAL 的既定语义是清记录、正常退出：' + said)
        self.assertIn('无法确认收件方是否收到', said, '失败不等于送达，文案要诚实：' + said)
        self.assertNotIn('已经送达过了', said, '失败不等于送达：' + said)
        self.assertNotIn('不会再响', said, '没归档就不该说不会再响：' + said)
        self.assertEqual(self.inbox(), [lid + '.md'], '既定语义：那封信仍在信箱里')
        self.assertEqual(self.archived(), [], '不归档')
        self.assertEqual(self.claims(), [], '认领要放回去')
        self.assertIsNone(self.read_alarm(), '记录照旧清掉')


if __name__ == '__main__':
    unittest.main()