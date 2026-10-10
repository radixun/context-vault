#!/usr/bin/env python3
"""Optimistic-concurrency guard for mutable Context Vault canonical notes.

Only the explicit ``commit`` command edits a note.  ``snapshot`` records an exact byte fingerprint
in a receipt outside the synchronized vault, ``verify`` checks that fingerprint
immediately before a write, and ``hash`` emits the same stable fingerprint
without creating a receipt. ``commit`` verifies the receipt and atomically
replaces the note under a cooperating local-writer lock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_write import replace_checked, sync_directory
from vault_metadata import strict_frontmatter
from validate_vault_write import load_validation_schema, validate_content

SCHEMA_VERSION = 1
SYNC_CONFLICT_MARKER = ".sync-conflict-"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RECEIPT_BYTES = 64 * 1024
IDENTITY_FIELDS = ("mtime_ns", "device", "inode", "ctime_ns")


class GuardError(RuntimeError):
    """A safe, user-facing guard failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep CLI usage failures machine-readable like all other failures."""

    def error(self, message: str) -> None:
        raise GuardError("usage", message)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def emit_json(value: Any) -> None:
    sys.stdout.write(canonical_json(value))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_vault(raw_vault: str | os.PathLike[str]) -> Path:
    supplied = Path(raw_vault).expanduser()
    if supplied.is_symlink():
        raise GuardError("unsafe_vault", "vault path must not be a symbolic link")
    try:
        vault = supplied.resolve(strict=True)
    except FileNotFoundError as exc:
        raise GuardError("missing_vault", f"vault does not exist: {supplied}") from exc
    except OSError as exc:
        raise GuardError("unsafe_vault", f"cannot resolve vault: {exc}") from exc
    if not vault.is_dir():
        raise GuardError("unsafe_vault", f"vault is not a directory: {vault}")
    return vault


def _validate_relative_note_path(relative: str) -> PurePosixPath:
    if not relative or "\\" in relative:
        raise GuardError("unsafe_path", "note path must be a non-empty POSIX path")
    if SYNC_CONFLICT_MARKER in relative.casefold():
        raise GuardError("sync_conflict", "sync-conflict artifacts are never canonical")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or pure.as_posix() != relative:
        raise GuardError("unsafe_path", f"note path is not canonical: {relative!r}")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise GuardError("unsafe_path", f"note path contains traversal: {relative!r}")

    parts = pure.parts
    is_manifest = (
        len(parts) == 3
        and parts[0] == "10_Projects"
        and parts[2] == "Memory_Manifest.md"
    )
    is_project_home = (
        len(parts) == 3
        and parts[0] == "10_Projects"
        and parts[2] == f"{parts[1]}.md"
    )
    is_decision = (
        len(parts) == 2
        and parts[0] == "70_Decisions"
        and parts[1].endswith(".md")
        and parts[1] != "README.md"
        and not parts[1].startswith(".")
    )
    is_session = (
        len(parts) == 3 and parts[0] == "60_Sessions"
        and parts[2].endswith(".md") and not parts[2].startswith(".")
    )
    if not (is_manifest or is_project_home or is_decision or is_session):
        raise GuardError(
            "unsupported_path",
            "guard accepts only a project Memory_Manifest.md, canonical project "
            "home, project session, or 70_Decisions Markdown note",
        )
    return pure


def _requested_relative_path(vault: Path, raw_path: str | os.PathLike[str]) -> str:
    raw = os.fspath(raw_path)
    if os.name == "nt":
        raw = raw.replace("\\", "/")
    if not raw or "\\" in raw:
        raise GuardError("unsafe_path", "note path must be a non-empty POSIX path")
    segments = raw.split("/")
    if Path(raw).is_absolute():
        segments = segments[1:]
    if any(segment in {"", ".", ".."} for segment in segments):
        raise GuardError("unsafe_path", f"note path contains traversal: {raw!r}")

    supplied = Path(raw).expanduser()
    requested = supplied if supplied.is_absolute() else vault / supplied
    requested = Path(os.path.abspath(os.fspath(requested)))
    try:
        relative = requested.relative_to(vault).as_posix()
    except ValueError as exc:
        raise GuardError("outside_vault", "note path resolves outside the vault") from exc
    _validate_relative_note_path(relative)
    return relative


def resolve_note(
    vault: Path, raw_path: str | os.PathLike[str]
) -> tuple[Path, str]:
    """Resolve one canonical note and reject symlinks in its vault-local path."""

    relative = _requested_relative_path(vault, raw_path)
    current = vault
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            item_stat = os.lstat(current)
        except FileNotFoundError as exc:
            code = "missing_note" if index == len(parts) - 1 else "missing_parent"
            raise GuardError(code, f"note path is missing: {relative}") from exc
        except OSError as exc:
            raise GuardError("unreadable_note", f"cannot inspect {relative}: {exc}") from exc
        if stat.S_ISLNK(item_stat.st_mode):
            raise GuardError("unsafe_symlink", f"note path contains a symlink: {relative}")
        if index < len(parts) - 1 and not stat.S_ISDIR(item_stat.st_mode):
            raise GuardError("unsafe_path", f"note parent is not a directory: {relative}")

    if not stat.S_ISREG(item_stat.st_mode):
        raise GuardError("unsafe_path", f"note is not a regular file: {relative}")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise GuardError("unreadable_note", f"cannot resolve {relative}: {exc}") from exc
    if not _is_relative_to(resolved, vault) or resolved.relative_to(vault).as_posix() != relative:
        raise GuardError("unsafe_path", "resolved note path does not match its canonical path")
    return resolved, relative


def _same_open_file(left: os.stat_result, right: os.stat_result) -> bool:
    # Windows stat and fstat can give different legacy ctime values after replace.
    # Use the explicit creation time there. Keep the POSIX change time elsewhere.
    time_field = "st_birthtime_ns" if os.name == "nt" else "st_ctime_ns"
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", time_field)
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def fingerprint(vault: Path, raw_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return a race-detecting byte and filesystem fingerprint for one note."""

    path, relative = resolve_note(vault, raw_path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GuardError("unreadable_note", f"cannot open {relative}: {exc}") from exc

    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise GuardError("unreadable_note", f"cannot hash {relative}: {exc}") from exc
    finally:
        os.close(descriptor)

    try:
        named = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GuardError("concurrent_change", f"note changed while hashing: {relative}") from exc
    except OSError as exc:
        raise GuardError("unreadable_note", f"cannot recheck {relative}: {exc}") from exc
    if not (_same_open_file(before, after) and _same_open_file(after, named)):
        raise GuardError("concurrent_change", f"note changed while hashing: {relative}")

    return {
        "schema_version": SCHEMA_VERSION,
        "path": relative,
        "sha256": digest.hexdigest(),
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "device": after.st_dev,
        "inode": after.st_ino,
        "ctime_ns": after.st_birthtime_ns if os.name == "nt" else after.st_ctime_ns,
    }


def resolve_receipt(
    vault: Path,
    raw_receipt: str | os.PathLike[str],
    *,
    for_write: bool,
) -> Path:
    supplied = Path(raw_receipt).expanduser()
    if not supplied.is_absolute():
        supplied = Path.cwd() / supplied

    try:
        parent = supplied.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        code = "missing_receipt_parent" if for_write else "missing_receipt"
        raise GuardError(code, f"receipt parent does not exist: {supplied.parent}") from exc
    if not parent.is_dir():
        raise GuardError("unsafe_receipt", "receipt parent is not a directory")
    receipt = parent / supplied.name

    if _is_relative_to(receipt, vault):
        raise GuardError("receipt_inside_vault", "receipt must be outside the vault")
    try:
        receipt_stat = os.lstat(receipt)
    except FileNotFoundError as exc:
        if not for_write:
            raise GuardError("missing_receipt", f"receipt does not exist: {receipt}") from exc
    else:
        if stat.S_ISLNK(receipt_stat.st_mode):
            raise GuardError("unsafe_receipt", "receipt path must not be a symbolic link")
        if not stat.S_ISREG(receipt_stat.st_mode):
            raise GuardError("unsafe_receipt", "receipt path is not a regular file")
    return receipt


def atomic_write_json(path: Path, value: Any) -> None:
    """Durably replace ``path`` with JSON using a same-directory temporary."""

    encoded = canonical_json(value).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def snapshot_note(
    vault: Path,
    raw_path: str | os.PathLike[str],
    raw_receipt: str | os.PathLike[str],
) -> tuple[dict[str, Any], Path]:
    receipt_path = resolve_receipt(vault, raw_receipt, for_write=True)
    receipt = fingerprint(vault, raw_path)
    receipt["timestamp"] = utc_now()
    atomic_write_json(receipt_path, receipt)
    return receipt, receipt_path


def _required_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if type(value) is not int or value < 0:
        raise GuardError("malformed_receipt", f"receipt `{key}` must be a non-negative integer")
    return value


def validate_receipt(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise GuardError("malformed_receipt", "receipt JSON root must be an object")
    expected_fields = {
        "schema_version",
        "path",
        "sha256",
        "size",
        "mtime_ns",
        "device",
        "inode",
        "ctime_ns",
        "timestamp",
    }
    actual_fields = set(document)
    if actual_fields != expected_fields:
        missing = sorted(expected_fields - actual_fields)
        unknown = sorted(actual_fields - expected_fields)
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        raise GuardError("malformed_receipt", "; ".join(details))
    if document.get("schema_version") != SCHEMA_VERSION:
        raise GuardError("malformed_receipt", "receipt schema_version is unsupported")

    path = document.get("path")
    if not isinstance(path, str):
        raise GuardError("malformed_receipt", "receipt `path` must be a string")
    _validate_relative_note_path(path)

    sha256 = document.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise GuardError("malformed_receipt", "receipt `sha256` must be lowercase SHA-256")
    for key in ("size", *IDENTITY_FIELDS):
        _required_int(document, key)

    timestamp = document.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise GuardError("malformed_receipt", "receipt `timestamp` must be UTC RFC 3339")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as exc:
        raise GuardError("malformed_receipt", "receipt `timestamp` is invalid") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise GuardError("malformed_receipt", "receipt `timestamp` must be UTC")
    return document


def load_receipt(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GuardError("unreadable_receipt", f"cannot open receipt: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GuardError("unsafe_receipt", "receipt path is not a regular file")
        if before.st_size > MAX_RECEIPT_BYTES:
            raise GuardError("malformed_receipt", "receipt is unreasonably large")
        chunks: list[bytes] = []
        remaining = MAX_RECEIPT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    except GuardError:
        raise
    except OSError as exc:
        raise GuardError("unreadable_receipt", f"cannot read receipt: {exc}") from exc
    finally:
        os.close(descriptor)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise GuardError("malformed_receipt", "receipt is unreasonably large")
    try:
        named = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise GuardError("concurrent_receipt_change", "receipt changed while reading") from exc
    if not (_same_open_file(before, after) and _same_open_file(after, named)):
        raise GuardError("concurrent_receipt_change", "receipt changed while reading")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise GuardError("malformed_receipt", f"duplicate receipt field: {key}")
            value[key] = item
        return value

    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except GuardError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError("malformed_receipt", f"receipt is not valid UTF-8 JSON: {exc}") from exc
    return validate_receipt(document)


def _failure_report(path: str, code: str, message: str) -> dict[str, Any]:
    return {
        "status": "drift",
        "path": path,
        "content_match": False,
        "identity_match": False,
        "failures": [{"code": code, "message": message}],
        "warnings": [],
        "summary": f"verification failed for {path}: {message}",
    }


def verify_receipt(
    vault: Path,
    raw_receipt: str | os.PathLike[str],
    *,
    strict_identity: bool = False,
) -> tuple[dict[str, Any], int]:
    receipt_path = resolve_receipt(vault, raw_receipt, for_write=False)
    expected = load_receipt(receipt_path)
    path = expected["path"]
    try:
        current = fingerprint(vault, path)
    except GuardError as exc:
        if exc.code in {
            "missing_note",
            "missing_parent",
            "unsafe_symlink",
            "unsafe_path",
            "concurrent_change",
        }:
            return _failure_report(path, exc.code, str(exc)), 1
        raise

    failures: list[dict[str, Any]] = []
    if current["path"] != path:
        failures.append({"code": "path_drift", "message": "canonical path changed"})
    if current["sha256"] != expected["sha256"]:
        failures.append({"code": "sha256_drift", "message": "note bytes changed"})
    if current["size"] != expected["size"]:
        failures.append({"code": "size_drift", "message": "note size changed"})

    identity_fields = [
        field for field in IDENTITY_FIELDS if current[field] != expected[field]
    ]
    identity_drift = {
        "code": "identity_drift",
        "fields": identity_fields,
        "message": "filesystem identity changed while canonical bytes were checked",
    }
    warnings: list[dict[str, Any]] = []
    if identity_fields:
        if strict_identity:
            failures.append(identity_drift)
        else:
            warnings.append(identity_drift)

    content_match = not any(
        failure["code"] in {"path_drift", "sha256_drift", "size_drift"}
        for failure in failures
    )
    if failures:
        report = {
            "status": "drift",
            "path": path,
            "content_match": content_match,
            "identity_match": not identity_fields,
            "strict_identity": strict_identity,
            "failures": failures,
            "warnings": warnings,
            "summary": f"verification detected drift for {path}",
        }
        return report, 1

    summary = f"unchanged canonical bytes for {path}"
    if warnings:
        summary += "; filesystem identity drift is advisory"
    return {
        "status": "ok",
        "path": path,
        "content_match": True,
        "identity_match": not identity_fields,
        "strict_identity": strict_identity,
        "failures": [],
        "warnings": warnings,
        "summary": summary,
    }, 0


def commit_note(vault: Path, receipt_path: str, content_file: str) -> dict[str, Any]:
    receipt = load_receipt(resolve_receipt(vault, receipt_path, for_write=False))
    source = Path(content_file).expanduser().resolve()
    if _is_relative_to(source, vault):
        raise GuardError("unsafe_content", "replacement must be staged outside the vault")
    data = source.read_bytes()
    try:
        fm = strict_frontmatter(data.decode("utf-8"))
    except UnicodeError as exc:
        raise GuardError("invalid_content", "replacement must be UTF-8 Markdown") from exc
    if fm is None or not data.strip():
        raise GuardError("invalid_content", "replacement must have valid frontmatter")
    path, relative = resolve_note(vault, receipt["path"])
    if relative.startswith("10_Projects/") and fm.get("project", "").strip("\"'") != path.parent.name:
        raise GuardError("invalid_content", "replacement project must match the canonical folder")
    try:
        schema = load_validation_schema(vault)
        problems = validate_content(data.decode("utf-8"), path, vault, schema)
    except ValueError as exc:
        raise GuardError("invalid_schema", str(exc)) from exc
    if problems:
        raise GuardError("invalid_content", "; ".join(problems))
    try:
        replace_checked(path, data, receipt["sha256"])
    except ValueError as exc:
        raise GuardError("concurrent_change", str(exc)) from exc
    return {"status": "written", "path": relative, "sha256": hashlib.sha256(data).hexdigest()}


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Optimistic-concurrency guard for Context Vault canonical notes"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="atomically write a receipt")
    snapshot.add_argument("--vault", required=True)
    snapshot.add_argument("--path", required=True)
    snapshot.add_argument("--receipt", required=True)

    verify = subparsers.add_parser("verify", help="verify a receipt against the note")
    verify.add_argument("--vault", required=True)
    verify.add_argument("--receipt", required=True)
    verify.add_argument(
        "--strict-identity",
        action="store_true",
        help="treat mtime/device/inode/ctime changes as blocking drift",
    )

    commit = subparsers.add_parser("commit", help="verify receipt and replace under a local writer lock")
    commit.add_argument("--vault", required=True)
    commit.add_argument("--receipt", required=True)
    commit.add_argument("--content-file", required=True)

    hash_command = subparsers.add_parser("hash", help="print a fingerprint only")
    hash_command.add_argument("--vault", required=True)
    hash_command.add_argument("--path", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        vault = resolve_vault(arguments.vault)
        if arguments.command == "commit":
            emit_json(commit_note(vault, arguments.receipt, arguments.content_file))
            return 0
        if arguments.command == "snapshot":
            receipt, receipt_path = snapshot_note(
                vault, arguments.path, arguments.receipt
            )
            emit_json(
                {
                    **receipt,
                    "status": "ok",
                    "receipt": str(receipt_path),
                    "summary": f"snapshot recorded for {receipt['path']}",
                }
            )
            return 0
        if arguments.command == "verify":
            report, exit_code = verify_receipt(
                vault,
                arguments.receipt,
                strict_identity=arguments.strict_identity,
            )
            emit_json(report)
            return exit_code

        record = fingerprint(vault, arguments.path)
        emit_json(
            {
                **record,
                "status": "ok",
                "summary": f"hash captured for {record['path']}",
            }
        )
        return 0
    except GuardError as exc:
        emit_json({"status": "error", "code": exc.code, "summary": str(exc)})
        return 2
    except OSError as exc:
        emit_json({"status": "error", "code": "io_error", "summary": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
