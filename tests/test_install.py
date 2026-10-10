import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
HOOK_SPEC = importlib.util.spec_from_file_location('hook', ROOT / 'hooks/context_vault_hook.py')
hook = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(hook)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vault test ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.vault = self.base / 'private vault'
        self.harness = self.base / 'harness'

    def run_tool(self, name, *args, ok=True):
        result = subprocess.run([sys.executable, str(self.vault / '50_System/Scripts' / name),
                                 *map(str, args)], capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_install_activate_log_hook_and_conflict(self):
        self.harness.mkdir()
        existing = self.harness / 'AGENTS.md'
        existing.write_text('Keep existing instructions.\n')
        installer.install(self.vault, self.harness)
        self.assertEqual(existing.read_text(), 'Keep existing instructions.\n')
        with self.assertRaises(ValueError):
            installer.install(self.vault, self.harness)
        self.assertNotIn('@VAULT', (self.harness / 'skills/context-vault/SKILL.md').read_text())
        self.run_tool('vault-audit.py', '--vault-path', self.vault, '--ratchet', '--summary')
        workspace = self.base / 'sample-app'
        workspace.mkdir()
        self.run_tool('activate_project.py', '--vault', self.vault, '--workspace', workspace, '--slug', 'sample-app')
        self.run_tool('activate_project.py', '--vault', self.vault, '--workspace', workspace, '--slug', 'sample-app', ok=False)
        config = {'vault': str(self.vault), 'skill': str(self.harness / 'skills/context-vault')}
        event = {'hook_event_name': 'Stop', 'cwd': str(workspace)}
        self.assertEqual(hook.respond(event, config)['decision'], 'block')
        self.assertEqual(hook.respond(dict(event, stop_hook_active=True), config), {})
        self.assertEqual(hook.respond(dict(event, cwd=str(self.base)), config), {})
        entry = self.base / 'entry.md'
        entry.write_text('## Result\n\n- Checked the synthetic project.\n')
        for status in ('written', 'unchanged'):
            result = self.run_tool('append_vault_session.py', '--vault', self.vault,
                                  '--project', 'sample-app', '--host', 'local', '--entry-file', entry)
            self.assertEqual(json.loads(result.stdout)['status'], status)
        self.assertEqual(hook.respond(event, config), {})
        manifest = self.vault / '10_Projects/sample-app/Memory_Manifest.md'
        receipt = self.base / 'receipt.json'
        proposal = self.base / 'proposal.md'
        self.run_tool('guard_vault_write.py', 'snapshot', '--vault', self.vault, '--path', manifest, '--receipt', receipt)
        proposal.write_text(manifest.read_text() + '\n- New proposed fact.\n')
        manifest.write_text(manifest.read_text() + '\n- Concurrent fact.\n')
        self.run_tool('guard_vault_write.py', 'commit', '--vault', self.vault,
                      '--receipt', receipt, '--content-file', proposal, ok=False)
        self.assertIn('Concurrent fact', manifest.read_text())
        self.run_tool('generate_indexes.py', '--vault', self.vault, '--write')
        self.run_tool('vault-audit.py', '--vault-path', self.vault, '--ratchet', '--summary')
        bad = self.vault / '00_Inbox/bad.md'
        bad.write_text('Missing metadata.\n')
        response = hook.respond({'hook_event_name': 'PostToolUse', 'cwd': str(workspace),
                                 'tool_name': 'Write', 'tool_input': {'file_path': str(bad)}}, config)
        self.assertTrue(response)

    def test_overlap_is_rejected_before_writes(self):
        with self.assertRaises(ValueError):
            installer.install(self.base / 'harness/skills', self.harness)
        self.assertFalse(self.harness.exists())

    def test_symlink_is_rejected(self):
        target = self.base / 'real'
        target.mkdir()
        link = self.base / 'link'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.install(link / 'vault', self.harness)
        self.assertEqual(list(target.iterdir()), [])
