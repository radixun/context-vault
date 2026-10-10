#!/usr/bin/env python3
"""Check local Vault writes and request at most one missing-log correction."""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys


def respond(event: dict, config: dict) -> dict:
    vault = Path(config['vault']).expanduser().resolve()
    skill = Path(config['skill']).expanduser().resolve()
    event_name = event.get('hook_event_name')
    if event_name == 'PostToolUse':
        env = dict(os.environ, CONTEXT_VAULT_PATH=str(vault))
        result = subprocess.run([sys.executable, str(skill / 'scripts/validate_vault_write.py')],
                                input=json.dumps(event), capture_output=True, text=True,
                                timeout=12, env=env)
        if result.returncode:
            return {'systemMessage': 'Context Vault validation did not finish. Run the explicit changed-note check.'}
        return json.loads(result.stdout) if result.stdout.strip() else {}
    if event_name != 'Stop' or event.get('stop_hook_active'):
        return {}
    cwd = event.get('cwd')
    if not isinstance(cwd, str) or not cwd:
        return {}
    result = subprocess.run([sys.executable, str(skill / 'scripts/resolve_project_manifest.py'),
                             '--vault', str(vault), '--workspace', cwd],
                            capture_output=True, text=True, timeout=6)
    if result.returncode:
        return {}
    route = json.loads(result.stdout)
    if route.get('status') != 'active':
        return {}
    slug = route['slug']
    daily = vault / '60_Sessions' / slug / (date.today().isoformat() + '.md')
    if daily.is_file():
        return {}
    return {'decision': 'block', 'reason':
            'The active project has no daily log for today. Follow the Context Vault skill once: '
            'record a compact task result, validate it and refresh indexes. Do not store the prompt or transcript. '
            'If a write is blocked, report the limit. Do not repeat this check.'}


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError('Hook input must be a JSON object')
        config = json.loads(Path(__file__).with_name('config.json').read_text(encoding='utf-8'))
        response = respond(event, config)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        response = {'systemMessage': 'Context Vault hook could not run. Check its local configuration.'}
    print(json.dumps(response))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
