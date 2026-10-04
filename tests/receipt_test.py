"""Public CLI receipt flow; isolated from real mailboxes and agents."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

PO = Path(__file__).resolve().parents[1] / 'postoffice'

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

    def run_po(self, *args, body='', check=True):
        return subprocess.run([os.sys.executable, str(PO), *args], input=body,
                              text=True, capture_output=True, env=self.env, check=check)

    def test_receipt_content_delivered_once_without_reply_loop(self):
        self.run_po('send', 'bob', 'alice', '核查结果', '回复', body='请核查配置和测试')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, '配置正常，测试未检查')
        receipts = list((self.home / 'alice/inbox').glob('*.md'))
        self.assertEqual(len(receipts), 1)
        text = receipts[0].read_text()
        self.assertIn('回执：' + original.stem, text)
        self.assertIn('配置正常，测试未检查', text)
        self.assertIn('默认不答复', text)
        self.run_po('ack', 'bob', original.stem, '重复')
        self.assertEqual(len(list((self.home / 'alice/inbox').glob('*.md'))), 1)
        self.run_po('ack', 'alice', receipts[0].stem, '收到回执')
        self.assertEqual(list((self.home / 'bob/inbox').glob('*.md')), [])
        self.assertEqual(len((self.home / 'acks.jsonl').read_text().splitlines()), 1)

    def test_offline_sender_keeps_receipt_pending(self):
        self.run_po('offline', 'alice')
        self.run_po('send', 'bob', 'alice', '通知', '仅告知', body='事项')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, '已处理')
        self.assertEqual(len(list((self.home / 'alice/inbox').glob('*.md'))), 1)
        self.assertEqual(json.loads((self.home / 'routes.json').read_text())['alice']['status'], 'offline')

    def test_claude_hook_receipt_content_and_no_reply_instruction(self):
        self.run_po('add', 'alice', '--claude', 'Receipt Test')
        transcript = Path(self.tmp.name) / 'session.jsonl'
        transcript.write_text(json.dumps({'type': 'custom-title', 'customTitle': 'Receipt Test'}))
        self.run_po('send', 'bob', 'alice', '核查结果', '回复', body='请核查配置和测试')
        original = next((self.home / 'bob/inbox').glob('*.md'))
        self.run_po('ack', 'bob', original.stem, '配置正常，测试未检查')
        result = self.run_po('hook', body=json.dumps({'transcript_path': str(transcript)}), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('配置正常，测试未检查', result.stderr)
        self.assertIn('默认不答复、不再 ack', result.stderr)
        self.assertNotIn('原信仍在等待答复', result.stderr)

if __name__ == '__main__':
    unittest.main()
