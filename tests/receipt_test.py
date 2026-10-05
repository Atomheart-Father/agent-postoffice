"""Public CLI receipt flow (v1.4): reminders carry metadata only, `receipt` looks the body up by ID.

Everything runs in a temp dir against the lab `postoffice`; no real mailbox, agent or model is touched.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

PO = Path(__file__).resolve().parents[1] / 'postoffice'
SENTINEL = 'SENTINEL-RECEIPT-BODY-7f3a9c'


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
                              capture_output=True, env=env or self.env, check=check)

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
        self.assertIn('postoffice receipt alice ' + original.stem, result.stderr)
        self.assertIn('默认不答复', result.stderr)
        self.assertIn(str(receipt), result.stderr)
        self.assertIn('归档：mv', result.stderr)
        got = self.run_po('receipt', 'alice', original.stem)
        self.assertIn(SENTINEL, got.stdout)
        self.assertIn('来源：bob', got.stdout)

    def test_normal_letter_reminder_keeps_path_and_id(self):
        self.run_po('send', 'alice', 'bob', '待办事项', '回复', body='请处理')
        letter = next((self.home / 'alice/inbox').glob('*.md'))
        t = self.as_claude('alice', 'Normal Letter')
        result = self.hook(t)
        self.assertEqual(result.returncode, 2)
        self.assertIn('== ' + str(letter), result.stderr)
        self.assertIn('编号：' + letter.stem, result.stderr)

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
        self.run_postman()
        summaries = list((self.home / 'alice/inbox').glob('*.md'))
        self.assertEqual(len(summaries), 1, summaries)
        summary = summaries[0].read_text()
        self.assertNotIn('B-NOTE-1', summary)
        self.assertNotIn('B-NOTE-2', summary)
        self.assertIn('postoffice receipt alice ' + bid, summary)
        t = self.as_claude('alice', 'Broadcast Receipt')
        result = self.hook(t)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('B-NOTE-1', result.stderr)
        self.assertIn('postoffice receipt alice ' + bid, result.stderr)
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
        self.assertIn('postoffice receipt alice old-id-1', result.stderr)
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
        self.assertIn('postoffice receipt carol ' + original.stem, text)


if __name__ == '__main__':
    unittest.main()
