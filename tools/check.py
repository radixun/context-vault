#!/usr/bin/env python3
"""Run the local test suites and check distribution consistency."""
from pathlib import Path
import os
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
for filename in ('vault_metadata.py', 'validate_vault_write.py'):
    if (root / 'skill/scripts' / filename).read_bytes() != (root / 'vault/50_System/Scripts' / filename).read_bytes():
        raise SystemExit(f'Skill copy differs: {filename}')
for folder in ('tests', 'vault/50_System/Scripts'):
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(root / folder),
                    '-p', 'test_*.py'], cwd=root, env=env, check=True)
subprocess.run([sys.executable, str(root / 'vault/50_System/Scripts/generate_indexes.py'),
                '--vault', str(root / 'vault'), '--check'], env=env, check=True)
subprocess.run([sys.executable, str(root / 'vault/50_System/Scripts/vault-audit.py'),
                '--vault-path', str(root / 'vault'), '--ratchet', '--summary'], env=env, check=True)
print('All checks passed.')
