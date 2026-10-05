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
import sqlite3
import threading
import subprocess
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
    return _locks().take_alarm_lock(Path(path))


def release_lock(path):
    _locks().release_alarm_lock(Path(path))

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
    # 插件工具的 cancel 和邮递员到期用的是同一把锁（<session>.json.lock，O_EXCL + 看门狗），
    # 所以「读完 pending → cancel 删记录 → 才把信投进 inbox」这个窗口不会发生。
    def lock_path(self, session='ses_live'):
        return self.alarm_path(session).with_suffix('.json.lock')

    def gate_path(self, session='ses_live'):
        return self.alarm_path(session).with_suffix('.json.lock.gate')

    def test_postman_skips_while_another_writer_holds_the_lock(self):
        rec = self.put_alarm()
        self.lock_path().write_text('99999 somebody\n', encoding='utf-8')
        self.run_postman()
        self.assertEqual(self.inbox(), [], '别人正在改这条记录时不得往 inbox 写信')
        self.assertEqual(self.read_alarm()['state'], 'pending', '也不得改状态')
        self.assertTrue(self.lock_path().exists(), '别人的锁不该被抢走或删除')

    def test_stale_lock_does_not_deadlock_or_reset_the_timer(self):
        rec = self.put_alarm(due=time.time() + 3600)
        lock = self.lock_path()
        lock.write_text('99999 crashed\n', encoding='utf-8')
        old = time.time() - 3600            # 远超看门狗窗口
        os.utime(lock, (old, old))
        self.run_postman()
        self.assertFalse(lock.exists(), '陈旧锁应被清掉，不该永久卡死')
        self.assertEqual(self.read_alarm()['due'], rec['due'], '不得悄悄重置计时')
        self.assertEqual(self.inbox(), [], '没到期就不该写信')

    def test_alarm_cancelled_before_due_is_never_delivered(self):
        """cancel 抢先删掉记录后，邮递员这一轮什么也不做（模拟同一条记录上 cancel 与 postman 交错）。"""
        rec = self.put_alarm()
        self.alarm_path().unlink()                     # cancel 已经把记录删了
        self.run_postman()
        self.assertEqual(self.inbox(), [], '取消之后不得再冒出提醒信')
        self.assertIsNone(self.read_alarm(), '也不得把记录写回来')

    def test_lock_is_released_so_the_next_round_still_works(self):
        self.put_alarm()
        self.run_postman()
        self.assertEqual(len(self.inbox()), 1)
        self.assertFalse(self.lock_path().exists(), '处理完必须释放锁')
        self.run_postman()
        self.assertEqual(len(self.inbox()), 1, '下一轮仍正常工作且不重复')

    # --- 锁回收的并发证据 -----------------------------------------------------
    # 两个进程可能同时看到同一把陈旧锁：回收本身必须互斥，而且只许删「被验证过的那一把」，
    # 否则后一个会把前一个刚建的新锁删掉，两边同时持锁。这里用多线程同抢一把锁来复现。
    def test_only_one_writer_holds_the_lock_when_it_is_fresh(self):
        self.put_alarm()
        p = self.alarm_path()
        winners = []
        barrier = threading.Barrier(8)

        def grab():
            barrier.wait()
            if take_lock(p):
                winners.append(1)

        ts = [threading.Thread(target=grab) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(len(winners), 1, f'同一时刻只应有一个持锁者（实际 {len(winners)}）')
        self.assertTrue(self.lock_path().exists(), '持锁者的锁文件必须还在')
        self.assertEqual(self.read_alarm()['state'], 'pending', '记录没被动')

    def test_reclaiming_a_stale_lock_never_deletes_a_fresh_one(self):
        """陈旧锁被回收后会有新锁建起来；后来的进程不得把这个新锁当成陈旧的删掉。"""
        for _ in range(12):
            self.put_alarm()
            p = self.alarm_path()
            lock = self.lock_path()
            lock.write_text('999999 已崩掉的进程\n', encoding='utf-8')
            old = time.time() - 3600                     # 远超硬超时，无条件可回收
            os.utime(lock, (old, old))
            winners = []
            barrier = threading.Barrier(8)

            def grab():
                barrier.wait()
                if take_lock(p):
                    winners.append(1)

            ts = [threading.Thread(target=grab) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
            self.assertEqual(len(winners), 1, f'回收竞争后仍只应有一个持锁者（实际 {len(winners)}）')
            self.assertTrue(lock.exists(), '持锁者的新锁不得被后来者删掉')
            release_lock(p)
        self.run_postman()
        self.assertEqual(len(self.inbox()), 1, '锁机制生效后闹钟照常只响一次')

    def test_no_other_writer_can_get_in_while_a_reclaim_decides(self):
        """回收者判定「这把锁崩了、可以删」的同一时刻，别人既不能建新锁也不能删锁。

        门（<lock>.gate）就是这个临界区：建锁、回收、释放都走它。所以把「判定」和「删除」
        之间的窗口打开时，别人的 take 必须失败——只可能有一个写者，那把锁也不该被删掉。
        """
        self.put_alarm()
        p = self.alarm_path()
        lock = self.lock_path()
        gate = self.gate_path()
        lock.write_text('999999 已崩掉的进程\n', encoding='utf-8')
        old = time.time() - 3600
        os.utime(lock, (old, old))
        mod = _locks()
        real = mod._lock_info
        tried = []

        def fake(q):
            info = real(q)
            # 回收者已经拿到门、也判定完，正要 unlink：这一刻别人来抢锁
            if info is not None and Path(q) == lock and not tried:
                tried.append(True)
                self.assertFalse(mod.take_alarm_lock(p), '门被占用时不得让第二个人以为自己也持锁')
                self.assertEqual(self.read_alarm()['state'], 'pending', '记录没被动')
            return info

        mod._lock_info = fake
        try:
            self.assertTrue(mod.try_reap_alarm_lock(p), '陈旧锁应当被回收')
        finally:
            mod._lock_info = real
        self.assertTrue(tried, '用例必须在回收窗口里真的试过一次抢锁')
        self.assertFalse(lock.exists(), '那把崩掉的锁已被回收')
        self.assertFalse(gate.exists(), '门用完要放掉，不能留成新的卡点')

    def test_a_held_gate_admits_only_one_reaper(self):
        """回收门自己也要互斥：门被一个活着的进程占着时，第二个回收者进不去。"""
        self.put_alarm()
        p = self.alarm_path()
        lock = self.lock_path()
        gate = self.gate_path()
        mod = _locks()
        gate.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(mod.take_alarm_gate(gate), '先拿到门')
        lock.write_text('999999 已崩掉的进程\n', encoding='utf-8')
        old = time.time() - 3600
        os.utime(lock, (old, old))
        self.assertFalse(mod.try_reap_alarm_lock(p), '门被占时第二个回收者必须让开')
        self.assertTrue(lock.exists(), '陈旧锁还在，等门空出来再回收')
        self.assertFalse(mod.take_alarm_gate(gate), '门也不会有两个持有者')
        mod.drop_alarm_gate(gate)
        self.assertFalse(gate.exists(), '门放掉了')
        self.assertTrue(mod.try_reap_alarm_lock(p), '门空出来之后才能回收')

    def test_release_never_removes_a_lock_it_does_not_hold(self):
        """释放只删「锁文件里写着自己 pid」的那一把，免得把别人的新锁顺手删掉。"""
        self.put_alarm()
        p = self.alarm_path()
        lock = self.lock_path()
        self.assertTrue(take_lock(p), '先拿到锁')
        lock.write_text(f'{os.getpid() + 1} {int(time.time())}\n', encoding='utf-8')  # 换成别人的 pid
        mod = _locks()
        mod.release_alarm_lock(p)
        self.assertTrue(lock.exists(), '不是自己的锁就不能删')
        lock.write_text(f'{os.getpid()} {int(time.time())}\n', encoding='utf-8')
        mod.release_alarm_lock(p)
        self.assertFalse(lock.exists(), '自己的锁才删')

    def test_soft_timeout_alone_does_not_break_a_live_lock(self):
        """单凭 mtime 超时不够：持锁进程还活着时不得回收（这是原来那个 TOCTOU 的另一半）。"""
        rec = self.put_alarm(due=time.time() + 3600)
        p = self.alarm_path()
        lock = self.lock_path()
        lock.write_text(f'{os.getpid()} {int(time.time())}\n', encoding='utf-8')  # 本进程持有
        old = time.time() - 120                            # 超了软超时（60s），没超硬超时（600s）
        os.utime(lock, (old, old))
        self.assertFalse(take_lock(p), '持锁进程还活着时不得回收')
        self.assertTrue(lock.exists(), '锁必须还在')
        self.assertEqual(self.read_alarm()['due'], rec['due'], '也不得重置计时')

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


if __name__ == '__main__':
    unittest.main()