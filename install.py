#!/usr/bin/env python3
"""Install a new private local Vault and a Context Vault skill."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def render(text: str, vault: Path, skill: Path) -> str:
    return (text.replace('@VAULT_PATH@', f'`{vault}`')
            .replace('@VAULT_SHELL@', shlex.quote(str(vault)))
            .replace('@RESOLVER@', shlex.quote(str(skill / 'scripts/resolve_project_manifest.py')))
            .replace('@TOOLS@', shlex.quote(str(vault / '50_System/Scripts'))))


def install(vault: Path, harness: Path) -> dict:
    vault = vault.expanduser().absolute()
    harness = harness.expanduser().absolute()
    skill = harness / 'skills/context-vault'
    integration = harness / 'context-vault-integration'
    for target in (vault, skill, integration):
        if target.exists() or target.is_symlink():
            raise ValueError(f'Destination already exists: {target}')
        for parent in target.parents:
            if parent.is_symlink():
                raise ValueError(f'Destination parent is a symlink: {parent}')
        if target.is_relative_to(ROOT):
            raise ValueError('Install outside the distribution checkout')
    destinations = (vault, skill, integration)
    for i, left in enumerate(destinations):
        for right in destinations[i + 1:]:
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError('Install destinations must be separate directories')
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store', 'test_*.py', '.gitkeep')
    shutil.copytree(ROOT / 'vault', vault, ignore=ignore)
    shutil.copytree(ROOT / 'skill', skill, ignore=ignore)
    integration.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(render((ROOT / 'skill/SKILL.md').read_text(), vault, skill), encoding='utf-8')
    (integration / 'AGENTS.fragment.md').write_text(render((ROOT / 'harness/AGENTS.fragment.md').read_text(), vault, skill), encoding='utf-8')
    shutil.copyfile(ROOT / 'hooks/context_vault_hook.py', integration / 'context_vault_hook.py')
    config = {'vault': str(vault), 'skill': str(skill)}
    (integration / 'config.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(integration / "context_vault_hook.py"))}'
    hooks = {'description': 'Local Context Vault checks.', 'hooks': {
        'PostToolUse': [{'matcher': 'apply_patch|Edit|Write', 'hooks': [
            {'type': 'command', 'command': command, 'timeout': 15,
             'statusMessage': 'Checking changed Vault notes'}]}],
        'Stop': [{'hooks': [{'type': 'command', 'command': command, 'timeout': 10,
                            'statusMessage': 'Checking the project daily log'}]}]}}
    (integration / 'hooks.json').write_text(json.dumps(hooks, indent=2) + '\n', encoding='utf-8')
    subprocess.run([sys.executable, str(vault / '50_System/Scripts/generate_indexes.py'),
                    '--vault', str(vault), '--write'], check=True, stdout=subprocess.PIPE, text=True)
    return {'status': 'installed', 'vault': str(vault), 'skill': str(skill),
            'integration': str(integration), 'next': 'Merge the generated AGENTS fragment and hooks. Review hook trust in Codex.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, default=Path.home() / 'Documents/context_vault')
    parser.add_argument('--harness', type=Path, default=Path.home() / '.codex')
    args = parser.parse_args()
    if os.name == 'nt':
        parser.error('This installer supports macOS and Linux. Windows installation is not supported in this release.')
    try:
        result = install(args.vault, args.harness)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
