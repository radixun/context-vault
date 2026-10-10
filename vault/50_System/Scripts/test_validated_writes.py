from __future__ import annotations
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import append_vault_session as session
import guard_vault_write as guard
import validate_vault_write as validator


class ValidatedWritesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / 'vault'
        self.path = self.vault / '10_Projects/demo/Memory_Manifest.md'
        self.path.parent.mkdir(parents=True)
        system = self.vault / '50_System'
        (system / 'Indexes').mkdir(parents=True)
        self.schema = json.loads((Path(__file__).parent.parent / 'system-manifest.json').read_text())
        (system / 'system-manifest.json').write_text(json.dumps(self.schema))
        for name in ['Project_Index', 'Session_Index']:
            (system / 'Indexes' / (name + '.md')).write_text('demo\n')
        self.good = ('---\ntype: manifest\nstatus: active\nproject: demo\n'
                     'created: 2026-09-19\nupdated: 2026-09-19\ntags: [codex]\n'
                     'summary: Test fixture.\n---\n# Demo\n')
        self.path.write_text(self.good)
        self.receipt = self.root / 'receipt.json'
        self.proposal = self.root / 'proposal.md'
        self.env = patch.dict(os.environ, {'CONTEXT_VAULT_LOCK_DIR': str(self.root / 'locks')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def commit(self, content):
        guard.snapshot_note(self.vault, str(self.path), str(self.receipt))
        self.proposal.write_text(content, encoding='utf-8')
        return guard.commit_note(self.vault, str(self.receipt), str(self.proposal))

    def test_invalid_proposals_do_not_replace_original(self):
        cases = [self.good.replace('tags: [codex]\n', ''),
                 self.good.replace('summary: Test fixture.\n', ''),
                 self.good.replace('status: active', 'status: invalid'),
                 self.good.replace('2026-09-19', '2026-02-30'),
                 self.good.replace('project: demo', 'project: other'),
                 self.good + 'x' * 16000,
                 '\ufeff' + self.good]
        for content in cases:
            with self.subTest(content=content[:80]):
                with self.assertRaises(guard.GuardError):
                    self.commit(content)
                self.assertEqual(self.path.read_text(), self.good)
                self.assertEqual(self.proposal.read_text(encoding='utf-8'), content)

    def test_valid_commit_and_stale_receipt(self):
        self.assertEqual(self.commit(self.good + 'New fact\n')['status'], 'written')
        current = self.path.read_bytes()
        with self.assertRaises(guard.GuardError):
            guard.commit_note(self.vault, str(self.receipt), str(self.proposal))
        self.assertEqual(self.path.read_bytes(), current)

    def test_missing_schema_fails_closed(self):
        (self.vault / '50_System/system-manifest.json').unlink()
        with self.assertRaises(guard.GuardError):
            self.commit(self.good + 'New fact\n')
        self.assertEqual(self.path.read_text(), self.good)

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            status = validator.check_cli(['--vault', str(self.vault), *args])
        return status, json.loads(out.getvalue())

    def test_cli_checks_proposal_at_destination_without_write(self):
        self.proposal.write_text(self.good.replace('tags: [codex]\n', ''))
        code, report = self.cli('--path', str(self.path), '--content-file', str(self.proposal))
        self.assertEqual(code, 1)
        self.assertTrue(any('tags' in s for s in report['problems']))
        self.assertEqual(self.path.read_text(), self.good)
        self.proposal.write_text(self.good)
        self.assertEqual(self.cli('--path', str(self.path), '--content-file', str(self.proposal))[0], 0)

    def test_cli_missing_outside_and_invalid_utf8_fail(self):
        self.assertEqual(self.cli('--path', 'missing.md')[0], 1)
        self.assertEqual(self.cli('--path', str(self.root / 'outside.md'))[0], 1)
        self.path.write_bytes(b'\xff')
        self.assertEqual(self.cli('--path', str(self.path))[0], 1)

    def test_daily_log_create_append_and_repeat_preserve_content(self):
        entry = '## First\n\n- Fact and next step.'
        report = session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', entry)
        path = self.vault / report['path']
        original = path.read_bytes()
        self.assertEqual(validator.validate_path(path, self.vault, self.schema), [])
        self.assertEqual(session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', entry)['status'], 'unchanged')
        self.assertEqual(path.read_bytes(), original)
        session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## Second\n\n- Result.')
        self.assertTrue(path.read_bytes().startswith(original))
        path.write_bytes(path.read_bytes() + b'\n\n  \n')
        original = path.read_bytes()
        session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## Third')
        self.assertTrue(path.read_bytes().startswith(original))

    def test_daily_log_info_size_does_not_block_useful_history(self):
        report = session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## History\n' + 'x' * 12500)
        self.assertEqual(report['status'], 'written')

    def test_daily_log_invalid_existing_note_is_not_silently_repaired(self):
        report = session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## First')
        path = self.vault / report['path']
        invalid = path.read_text().replace('tags:\n  - session\n  - daily-log\n', '')
        path.write_text(invalid)
        with self.assertRaises(ValueError):
            session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## Second')
        self.assertEqual(path.read_text(), invalid)

    def test_daily_log_concurrent_append_preserved(self):
        report = session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## First')
        target = self.vault / report['path']
        replace = session.replace_checked
        def concurrent(path, data, sha):
            path.write_text(path.read_text() + '\nOther writer\n')
            return replace(path, data, sha)
        with patch.object(session, 'replace_checked', concurrent), self.assertRaises(ValueError):
            session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## Second')
        self.assertIn('Other writer', target.read_text())
        self.assertNotIn('## Second', target.read_text())

    def test_daily_log_concurrent_creation_never_overwrites(self):
        link = os.link
        def concurrent(source, target):
            Path(target).write_text('Other writer\n')
            return link(source, target)
        with patch.object(session.os, 'link', concurrent), self.assertRaises(FileExistsError):
            session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## First')
        self.assertEqual((self.vault / '60_Sessions/demo/2026-09-19.md').read_text(), 'Other writer\n')

    def test_dev_log_has_a_separate_file(self):
        mac = session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## Mac entry')
        dev = session.append_entry(self.vault, 'demo', '2026-09-19', 'linux', '## DEV entry')
        self.assertEqual(dev['path'], '60_Sessions/demo/2026-09-19--linux.md')
        self.assertNotIn('## DEV entry', (self.vault / mac['path']).read_text())
        self.assertEqual(session.append_entry(self.vault, 'demo', '2026-09-19', 'linux', '## DEV entry')['status'], 'unchanged')

    def test_windows_log_has_a_separate_file(self):
        mac = session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## Mac entry')
        pc = session.append_entry(self.vault, 'demo', '2026-09-19', 'windows', '## PC entry')
        self.assertEqual(pc['path'], '60_Sessions/demo/2026-09-19--windows.md')
        self.assertNotIn('## PC entry', (self.vault / mac['path']).read_text())
        original = (self.vault / pc['path']).read_bytes()
        self.assertEqual(session.append_entry(self.vault, 'demo', '2026-09-19', 'windows', '## PC entry')['status'], 'unchanged')
        self.assertEqual((self.vault / pc['path']).read_bytes(), original)

    def test_inactive_project_and_unknown_host_are_rejected(self):
        with self.assertRaises(ValueError):
            session.append_entry(self.vault, 'demo', '2026-09-19', 'unknown-host', '## First')
        self.path.write_text(self.good.replace('status: active', 'status: paused'))
        with self.assertRaises(ValueError):
            session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## First')

    def test_daily_log_symlink_cannot_redirect_another_project(self):
        (self.vault / '60_Sessions').mkdir()
        (self.root / 'other').mkdir()
        (self.vault / '60_Sessions/demo').symlink_to(self.root / 'other')
        with self.assertRaises(ValueError):
            session.append_entry(self.vault, 'demo', '2026-09-19', 'mac', '## First')
        self.assertEqual(list((self.root / 'other').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
