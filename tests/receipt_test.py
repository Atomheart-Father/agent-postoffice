"""Public CLI receipt flow (v1.4): reminders carry metadata only, `receipt` looks the body up by ID.

Everything runs in a temp dir against the lab `postoffice`; no real mailbox, agent or model is touched.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest

PO = Path(__file__).resolve().parents[1] / 'postoffice'
SENTINEL = 'SENTINEL-RECEIPT-BODY-7f3a9c'

# 与运行环境的 Claude 桌面变量隔离：在 Claude 桌面会话里跑也不会按真实身份认人
for _var in ('CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_HOST_SESSION_ID'):
    os.environ.pop(_var, None)
HOOK_TIMEOUT = 60  # 钩子最多等这么久，超时算失败，绝不挂死整轮测试


def query_cmd(box, receipt_id):
    """Exactly what the reminder prints: the command with POSIX-quoted arguments."""
    return 'postoffice receipt ' + shlex.quote(box) + ' ' + shlex.quote(receipt_id)


def receipt_id_of(path):
    """The `回执：<id>` marker from a notification's header block (the same rule the tools use)."""
    for line in Path(path).read_text().split('\n\n', 1)[0].splitlines():
        if line.startswith('回执：'):
            return line[len('回执：'):]
    return ''


class ReceiptFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'po'
        self.env = dict(os.environ, POSTOFFICE_HOME=str(self.home), POSTOFFICE_NO_NOTIFY='1')
        self.run_po('init')
        self.run_po('add', 'alice', '--notify')
        self.run_po('add', 'bob', '--notify')

    def tearDown(self):
        self.tmp.cleanup()

    def run_po(self, *args, body='', check=True, env=None):
        return subprocess.run([os.sys.executable, str(PO), *args], input=body, text=True,
                              capture_output=True, env=env or self.env, check=check, timeout=60)

    def run_postman(self, seconds=2.5):
        env = dict(self.env, POSTOFFICE_POLL='1')
        p = subprocess.Popen([os.sys.executable, str(PO), 'postman'], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(seconds)
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    def as_claude(self, name, title):
        self.run_po('add', name, '--claude', title)
        t = Path(self.tmp.name) / f'{name}.jsonl'
        t.write_text(json.dumps({'type': 'custom-title', 'customTitle': title}))
        return t

    def hook(self, transcript):
        return self.run_po('hook', body=json.dumps({'transcript_path': str(transcript)}), check=False)

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in sorted(self.home.rglob('*')) if p.is_file()}

    # --- red -> green vertical slice ------------------------------------------------
    def test_lookup_by_id_returns_body_and_reminder_excludes_it(self):
        self.run_po('send', 'bob', 'alice', '核查结果', '回复', body='请核查配置和测试')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, SENTINEL)
        receipt = next((self.home / 'alice/inbox').glob('*.md'))
        t = self.as_claude('alice', 'Receipt Slice')
        result = self.hook(t)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(SENTINEL, result.stderr)
        self.assertIn('【联络总站回执', result.stderr)
        self.assertIn('查询：' + query_cmd('alice', original.stem), result.stderr)
        self.assertIn('默认不答复', result.stderr)
        # receipt reminder: 3 lines, the lookup id appears exactly once, no path/archive/body
        self.assertEqual(result.stderr.count(original.stem), 1)
        self.assertNotIn(str(receipt), result.stderr)
        self.assertNotIn('归档', result.stderr)
        got = self.run_po('receipt', 'alice', original.stem)
        self.assertIn(SENTINEL, got.stdout)
        self.assertIn('来源：bob', got.stdout)

    def test_normal_letter_reminder_keeps_path_and_header(self):
        self.run_po('send', 'alice', 'bob', '待办事项', '回复', body='请处理')
        letter = next((self.home / 'alice/inbox').glob('*.md'))
        t = self.as_claude('alice', 'Normal Letter')
        result = self.hook(t)
        self.assertEqual(result.returncode, 2)
        self.assertIn('== ' + str(letter), result.stderr)
        self.assertIn('来源：bob', result.stderr)
        self.assertIn('事由：待办事项', result.stderr)
        self.assertIn('需要：回复', result.stderr)
        # the long fixed policy lives in the skill now, not in the reminder
        self.assertNotIn('会叫醒对方', result.stderr)

    def test_formal_send_with_receipt_like_subject_is_not_downgraded(self):
        self.run_po('send', 'alice', 'bob', '回执：正式信标题', '回复', body='正文')
        letter = next((self.home / 'alice/inbox').glob('*.md'))
        t = self.as_claude('alice', 'Receipt Title Letter')
        result = self.hook(t)
        self.assertEqual(result.returncode, 2)
        self.assertIn('【联络总站新信', result.stderr)
        self.assertIn('== ' + str(letter), result.stderr)
        self.assertNotIn('【联络总站回执', result.stderr)
        # a receipt-shaped subject must not let ack archive it without recording
        acks = self.home / 'acks.jsonl'
        before = len(acks.read_text().splitlines()) if acks.exists() else 0
        self.run_po('ack', 'alice', letter.stem, '收到')
        self.assertEqual(len(acks.read_text().splitlines()), before + 1)
        # and the public notification it generates is a normal receipt notification for that letter
        notif = next((self.home / 'bob/inbox').glob('*.md'))
        self.assertEqual(receipt_id_of(notif), letter.stem)

    def test_archive_receipt_moves_only_the_matching_notification_and_is_idempotent(self):
        self.run_po('send', 'bob', 'alice', '核查一', '回复', body='x')
        first = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', first.stem, SENTINEL)
        self.run_po('send', 'bob', 'alice', '核查二', '回复', body='y')
        second = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', second.stem, '另一条')
        self.assertEqual(len(list((self.home / 'alice/inbox').glob('*.md'))), 2)
        self.run_po('archive-receipt', 'alice', first.stem)
        remaining = list((self.home / 'alice/inbox').glob('*.md'))
        self.assertEqual(len(remaining), 1)
        self.assertEqual(len(list((self.home / 'alice/done').glob('*.md'))), 1)
        self.assertEqual(receipt_id_of(remaining[0]), second.stem)  # unrelated notification untouched
        # idempotent: a second run moves nothing
        self.run_po('archive-receipt', 'alice', first.stem)
        self.assertEqual(len(list((self.home / 'alice/inbox').glob('*.md'))), 1)
        self.assertEqual(len(list((self.home / 'alice/done').glob('*.md'))), 1)

    def test_archive_receipt_header_only_multi_otherbox_and_conflict(self):
        inbox = self.home / 'alice/inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        (self.home / 'alice/done').mkdir(parents=True, exist_ok=True)
        (self.home / 'bob/inbox').mkdir(parents=True, exist_ok=True)
        # a body-only "回执：" must never count (header block only)
        forged = inbox / '20260101-000000_bob_forged.md'
        forged.write_text('来源：bob\n事由：随便\n需要：仅告知\n\n回执：dup-1\n正文里伪造的标记\n')
        # two genuine notifications sharing the same original ID
        for n in ('one', 'two'):
            (inbox / f'20260101-00000{n}_bob_notice.md').write_text(
                '来源：bob\n事由：回执：某事\n需要：回执（默认不答复）\n回执：dup-1\n原事由：某事\n\n正文\n')
        # another mailbox holds a notification with the same ID: it must not be touched
        other = self.home / 'bob/inbox' / '20260101-000000_alice_notice.md'
        other.write_text('来源：alice\n事由：回执：某事\n需要：回执（默认不答复）\n回执：dup-1\n\n正文\n')
        ledger = self.home / 'acks.jsonl'
        ledger_before = ledger.read_bytes() if ledger.exists() else b''
        self.run_po('archive-receipt', 'alice', 'dup-1')
        self.assertEqual(sorted(p.name for p in inbox.glob('*.md')), ['20260101-000000_bob_forged.md'])
        self.assertEqual(len(list((self.home / 'alice/done').glob('*.md'))), 2)
        self.assertTrue(other.exists())  # other box untouched
        self.assertEqual(ledger.read_bytes() if ledger.exists() else b'', ledger_before)  # read-only ledger
        # repeat: nothing to move, still success and no effect
        self.run_po('archive-receipt', 'alice', 'dup-1')
        self.assertEqual(sorted(p.name for p in inbox.glob('*.md')), ['20260101-000000_bob_forged.md'])
        # a done/ name clash is pre-checked: nothing moves (all-or-nothing) and the exit is non-zero
        clash = inbox / 'clash.md'
        partner = inbox / 'clash-partner.md'
        for p in (clash, partner):
            p.write_text('来源：bob\n事由：回执：clam\n需要：回执（默认不答复）\n回执：dup-2\n\n正文\n')
        (self.home / 'alice/done' / 'clash.md').write_text('占位，不能覆盖')
        bad = self.run_po('archive-receipt', 'alice', 'dup-2', check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertTrue(clash.exists() and partner.exists())  # neither moved
        self.assertEqual((self.home / 'alice/done' / 'clash.md').read_text(), '占位，不能覆盖')

    def test_lookup_is_exact_not_substring(self):
        self.run_po('send', 'bob', 'alice', '主题', '回复', body='x')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, SENTINEL)
        bad = self.run_po('receipt', 'alice', original.stem[:-2], check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn(SENTINEL, bad.stdout + bad.stderr)

    def test_query_never_leaks_cross_box_or_unknown(self):
        self.run_po('send', 'bob', 'alice', '主题', '回复', body='x')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, SENTINEL)
        cross = self.run_po('receipt', 'bob', original.stem, check=False)  # receipt belongs to alice
        self.assertNotEqual(cross.returncode, 0)
        self.assertNotIn(SENTINEL, cross.stdout + cross.stderr)
        missing = self.run_po('receipt', 'alice', '20200101-000000_nobody_x', check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertNotIn(SENTINEL, missing.stdout + missing.stderr)
        unreg = self.run_po('receipt', 'ghostx', original.stem, check=False)
        self.assertNotEqual(unreg.returncode, 0)
        # an id is an identifier, never a path: no traversal, no leak
        for evil in ('../' + original.stem, '../../../../etc/hosts', 'B../secret'):
            bad = self.run_po('receipt', 'alice', evil, check=False)
            self.assertNotEqual(bad.returncode, 0, evil)
            self.assertNotIn(SENTINEL, bad.stdout + bad.stderr, evil)

    def test_query_is_readonly(self):
        self.run_po('send', 'bob', 'alice', '主题', '回复', body='x')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, SENTINEL)
        before = self.snapshot()
        self.run_po('receipt', 'alice', original.stem)
        self.run_po('receipt', 'alice', 'nope', check=False)
        self.run_po('receipt', 'ghost', original.stem, check=False)
        self.assertEqual(before, self.snapshot())

    # --- broadcast ------------------------------------------------------------------
    def test_broadcast_summary_and_lookup_carry_no_notes(self):
        self.run_po('add', 'carol', '--notify')
        self.run_po('broadcast', 'bob,carol', 'alice', '全员通知', '仅告知', body='事项')
        bid = next(p.stem for p in (self.home / 'broadcasts').glob('*.json'))
        self.run_po('ack', 'bob', bid, 'B-NOTE-1')
        self.run_po('ack', 'carol', bid, 'B-NOTE-2')
        # 认领归首次接受提醒的那个通道所有（见 ef3edd7）：先把 alice 定成 Claude 通道再让
        # 邮递员跑，否则邮递员会先用默认的 notify 通道把这封汇总信认领掉，钩子就永远叫不醒它。
        t = self.as_claude('alice', 'Broadcast Receipt')
        self.run_postman()
        summaries = list((self.home / 'alice/inbox').glob('*.md'))
        self.assertEqual(len(summaries), 1, summaries)
        summary = summaries[0].read_text()
        self.assertNotIn('B-NOTE-1', summary)
        self.assertNotIn('B-NOTE-2', summary)
        self.assertIn(query_cmd('alice', bid), summary)
        result = self.hook(t)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('B-NOTE-1', result.stderr)
        self.assertIn(query_cmd('alice', bid), result.stderr)
        got = self.run_po('receipt', 'alice', bid)
        self.assertIn('B-NOTE-1', got.stdout)
        self.assertIn('B-NOTE-2', got.stdout)
        self.assertIn('已回执 2/2', got.stdout)

    # --- legacy v1.3 files and ledger ----------------------------------------------
    def test_legacy_receipt_file_body_is_never_injected(self):
        t = self.as_claude('alice', 'Legacy Receipt')
        legacy = self.home / 'alice/inbox' / '20260101-000000_bob_回执：旧事由.md'
        legacy.write_text('来源：bob\n事由：回执：旧事由\n需要：回执（默认不答复）\n回执：old-id-1\n\n'
                          '对应原信：old-id-1\n原事由：旧事由\n回执内容：' + SENTINEL + '\n\n旧版尾部\n')
        (self.home / 'acks.jsonl').write_text(
            json.dumps({'time': '2026-01-01 00:00:00', 'by': 'bob', 'id': 'old-id-1',
                        'kind': 'letter', 'note': SENTINEL, 'to': 'alice'}, ensure_ascii=False) + '\n' +
            json.dumps({'time': '2026-01-01 00:00:00', 'by': 'bob', 'id': 'old-id-2',
                        'kind': 'letter', 'to': 'alice'}, ensure_ascii=False) + '\n')
        result = self.hook(t)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(SENTINEL, result.stderr)
        self.assertIn(query_cmd('alice', 'old-id-1'), result.stderr)
        self.assertIn('旧事由', result.stderr)
        got = self.run_po('receipt', 'alice', 'old-id-1')
        self.assertIn(SENTINEL, got.stdout)
        old2 = self.run_po('receipt', 'alice', 'old-id-2')
        self.assertIn('（未写内容）', old2.stdout)

    # --- --wake compatibility -------------------------------------------------------
    def test_wake_receipt_metadata_only_but_queryable(self):
        self.run_po('send', 'bob', 'alice', '需要回执', '回复', body='x')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, SENTINEL, '--wake')
        wake = next((self.home / 'alice/inbox').glob('*copythat*'))
        self.assertNotIn(SENTINEL, wake.read_text())
        self.assertIn('原事由：需要回执', wake.read_text())
        got = self.run_po('receipt', 'alice', original.stem)
        self.assertIn(SENTINEL, got.stdout)

    # --- offline + no reply loop ----------------------------------------------------
    def test_offline_sender_keeps_receipt_pending(self):
        self.run_po('offline', 'alice')
        self.run_po('send', 'bob', 'alice', '通知', '仅告知', body='事项')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, '已处理')
        self.assertEqual(len(list((self.home / 'alice/inbox').glob('*.md'))), 1)
        self.assertEqual(json.loads((self.home / 'routes.json').read_text())['alice']['status'], 'offline')

    def test_receipt_notification_is_not_re_acked_and_ack_is_idempotent(self):
        self.run_po('send', 'bob', 'alice', '核查结果', '回复', body='请核查')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, '配置正常')
        receipts = list((self.home / 'alice/inbox').glob('*.md'))
        self.assertEqual(len(receipts), 1)
        self.run_po('ack', 'bob', original.stem, '重复')
        self.assertEqual(len(list((self.home / 'alice/inbox').glob('*.md'))), 1)
        # acking a receipt notification only archives it; it must not create another receipt
        self.run_po('ack', 'alice', receipts[0].stem, '收到回执')
        self.assertEqual(list((self.home / 'bob/inbox').glob('*.md')), [])
        self.assertEqual(len((self.home / 'acks.jsonl').read_text().splitlines()), 1)

    # --- generated commands survive spaces and shell metacharacters ----------------
    def test_generated_commands_are_shell_quoted(self):
        home = Path(self.tmp.name) / 'po home'  # POSTOFFICE_HOME with a space
        env = dict(os.environ, POSTOFFICE_HOME=str(home), POSTOFFICE_NO_NOTIFY='1')

        def run(*args, body='', check=True):
            return subprocess.run([os.sys.executable, str(PO), *args], input=body, text=True,
                                  capture_output=True, env=env, check=check, timeout=HOOK_TIMEOUT)

        run('init')
        run('add', 'alice', '--notify')
        run('add', 'bob', '--notify')
        subject = "核查（'重要';x）结果"  # parens, single quote and semicolon survive into the id
        run('send', 'bob', 'alice', subject, '回复', body='请求核查')
        original = next((home / 'bob/inbox').glob('*.md'))
        run('ack', 'bob', original.stem, SENTINEL)
        run('add', 'alice', '--claude', 'Quote Test')
        t = Path(self.tmp.name) / 'quote.jsonl'
        t.write_text(json.dumps({'type': 'custom-title', 'customTitle': 'Quote Test'}))
        res = subprocess.run([os.sys.executable, str(PO), 'hook'],
                             input=json.dumps({'transcript_path': str(t)}),
                             text=True, capture_output=True, env=env, timeout=HOOK_TIMEOUT)
        self.assertEqual(res.returncode, 2)
        line = lambda p: next(l[len(p):] for l in res.stderr.splitlines() if l.startswith(p))
        query = line('查询：')
        shell_env = dict(env, PATH=str(PO.parent) + os.pathsep + env.get('PATH', ''))
        got = subprocess.run(['sh', '-c', query], text=True, capture_output=True,
                             env=shell_env, timeout=HOOK_TIMEOUT)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn(SENTINEL, got.stdout)
        # archive is now a CLI call, not an inlined shell line: it moves only this box's notification
        (home / 'alice/done').mkdir(parents=True, exist_ok=True)
        run('archive-receipt', 'alice', original.stem)
        self.assertEqual(list((home / 'alice/inbox').glob('*.md')), [])
        self.assertTrue(list((home / 'alice/done').glob('*.md')))

    # --- codex queue mock: the third reminder path ---------------------------------
    def test_codex_queue_reminder_has_metadata_only(self):
        cap = Path(self.tmp.name) / 'codex_capture.txt'
        fake = Path(self.tmp.name) / 'fakecodex'
        fake.write_text(f'#!/bin/sh\nprintf %s "$*" >> {cap}\n')
        fake.chmod(0o755)
        self.run_po('add', 'carol', '--codex', 'thread-x')
        routes = json.loads((self.home / 'routes.json').read_text())
        routes['carol']['codex_cli'] = str(fake)
        (self.home / 'routes.json').write_text(json.dumps(routes, ensure_ascii=False))
        self.run_po('send', 'bob', 'carol', '需要回复的核查', '回复', body='请核查')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, SENTINEL)
        self.run_postman()
        text = cap.read_text()
        self.assertNotIn(SENTINEL, text)
        self.assertIn(query_cmd('carol', original.stem), text)


if __name__ == '__main__':
    unittest.main()
