#!/usr/bin/env python3
"""Create project memory after a direct user request."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from vault_write import replace_checked
from validate_vault_write import load_validation_schema, validate_content, validate_registry_shape


def activate(vault: Path, workspace: Path, slug: str) -> dict:
    vault = vault.expanduser().resolve(strict=True)
    workspace = workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir() or workspace.is_relative_to(vault):
        raise ValueError('Workspace must be a directory outside the Vault')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        raise ValueError('Use a lowercase project slug with optional hyphens')
    root = vault / '10_Projects'
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Project root must be a regular directory')
    folder = root / slug
    if folder.exists() or folder.is_symlink():
        raise ValueError('Project memory already exists; do not replace it')
    registry_path = vault / '50_System/project-routing.json'
    if registry_path.is_symlink() or registry_path.parent.is_symlink():
        raise ValueError('Registry path must not be a symlink')
    original = registry_path.read_bytes()
    registry = json.loads(original)
    problems = validate_registry_shape(registry, vault)
    if problems:
        raise ValueError('; '.join(problems))
    schema = load_validation_schema(vault)
    bindings = registry.setdefault('workspace_bindings', {})
    for raw in bindings:
        bound = Path(raw).expanduser().resolve()
        if workspace.is_relative_to(bound) or bound.is_relative_to(workspace):
            raise ValueError('Workspace overlaps an existing binding')
    if str(workspace) in bindings:
        raise ValueError('Workspace already has a binding')
    day = date.today().isoformat()
    candidates = {}
    for template, filename in [('Memory_Manifest.md', 'Memory_Manifest.md'), ('Project.md', slug + '.md')]:
        content = (vault / '50_System/Templates' / template).read_text(encoding='utf-8')
        content = content.replace('YYYY-MM-DD', day).replace('project-slug', slug).replace('PROJECT_HOME_LINK', f'[[{slug}|Project home]]')
        problems = validate_content(content, folder / filename, vault, schema)
        if problems:
            raise ValueError('; '.join(problems))
        candidates[filename] = content
    folder.mkdir()
    for filename, content in candidates.items():
        (folder / filename).write_text(content, encoding='utf-8')
    bindings[str(workspace)] = slug
    replace_checked(registry_path, (json.dumps(registry, indent=2) + '\n').encode(),
                    hashlib.sha256(original).hexdigest())
    scripts = Path(__file__).resolve().parent
    for filename in ('Memory_Manifest.md', slug + '.md'):
        subprocess.run([sys.executable, str(scripts / 'validate_vault_write.py'), '--vault', str(vault),
                        '--path', str(folder / filename), '--content-file', str(folder / filename)],
                       check=True, stdout=subprocess.PIPE, text=True)
    subprocess.run([sys.executable, str(scripts / 'generate_indexes.py'), '--vault', str(vault), '--write'],
                   check=True, stdout=subprocess.PIPE, text=True)
    return {'status': 'active', 'slug': slug, 'manifest': str(folder / 'Memory_Manifest.md')}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--slug', required=True)
    args = parser.parse_args()
    try:
        result = activate(args.vault, args.workspace, args.slug)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
