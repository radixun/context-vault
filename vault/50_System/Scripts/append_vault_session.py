#!/usr/bin/env python3
"""Create or append a validated daily log without losing concurrent edits."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from validate_vault_write import load_validation_schema, scalar, validate_content
from vault_metadata import strict_frontmatter
from vault_write import replace_checked, sync_directory


def append_entry(vault: Path, project: str, day: str, host: str, entry: str) -> dict:
    vault = vault.expanduser().resolve(strict=True)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError("project must be an existing canonical slug")
    date.fromisoformat(day)
    if host not in {"mac", "local", "linux", "windows"}:
        raise ValueError("host must be mac, local, linux or windows")
    manifest = vault / "10_Projects" / project / "Memory_Manifest.md"
    fm = strict_frontmatter(manifest.read_text(encoding="utf-8")) or {}
    if scalar(fm.get("status")) != "active" or scalar(fm.get("project")) != project:
        raise ValueError("daily logging requires an active matching manifest")
    entry = entry.strip()
    if not entry:
        raise ValueError("entry must not be empty")
    if entry.startswith("---"):
        raise ValueError("entry must contain Markdown body, not frontmatter")
    # Reject symlinked parents instead of redirecting a write to another project.
    folder = vault
    for part in ("60_Sessions", project):
        folder = folder / part
        if folder.is_symlink():
            raise ValueError("session parents must not be symlinks")
        folder.mkdir(exist_ok=True)
    filename = day + ("" if host in {"local", "mac"} else "--" + host) + ".md"
    target = folder / filename
    if target.is_symlink():
        raise ValueError("session target must not be a symlink")
    before = target.read_bytes() if target.exists() else None
    if before is None:
        text = (f"---\ntype: session\nstatus: active\ncreated: {day}\nupdated: {day}\n"
                f"project: {project}\ntags:\n  - session\n  - daily-log\n"
                "summary: Project daily ledger of results and follow-up.\n---\n\n"
                f"# Daily Log: {project} / {day}\n")
    else:
        text = before.decode("utf-8")
    schema = load_validation_schema(vault)
    problems = validate_content(text, target, vault, schema)
    if problems:
        raise ValueError("; ".join(problems))
    block = "\n\n" + entry + "\n"
    if block in text:
        return {"status": "unchanged", "path": target.relative_to(vault).as_posix()}
    proposed = text + block
    problems = validate_content(proposed, target, vault, schema)
    if problems:
        raise ValueError("; ".join(problems))
    data = proposed.encode("utf-8")
    if before is not None:
        replace_checked(target, data, hashlib.sha256(before).hexdigest())
    else:
        # Hard-link publication is atomic and never replaces an existing writer.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, prefix=".session-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        sync_directory(folder)
    if target.read_bytes() != data:
        raise ValueError("session changed after write; re-read and reconcile")
    return {"status": "written", "path": target.relative_to(vault).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--host", choices=["mac", "local", "linux", "windows"], required=True)
    parser.add_argument("--entry-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = append_entry(args.vault, args.project, args.date, args.host,
                              args.entry_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
