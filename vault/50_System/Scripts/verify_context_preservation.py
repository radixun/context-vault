#!/usr/bin/env python3
"""Prove that Context Vault user Markdown survived a staged rollout.

The verifier is deliberately independent from ``vault-audit.py``.  It creates
a hash-only baseline outside the vault, then compares a later vault against
that baseline.  Unlisted deletion, addition, rename, or byte change fails.
Intentional changes must be individually allowlisted and pinned to exact
before/after SHA-256 values.

The default corpus includes every regular file in all seven user-content roots,
including Inbox, Archive, and attachments. Add --root 50_System explicitly for
system-file migrations. Schema-1 baselines retain their original Markdown scope.
The note count is discovered and frozen with the baseline; it is never assumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 2
ALLOWLIST_SCHEMA_VERSION = 1
DEFAULT_ROOTS = (
    "00_Inbox",
    "10_Projects",
    "20_Areas",
    "30_Resources",
    "40_Archive",
    "60_Sessions",
    "70_Decisions",
)
IGNORED_DIR_NAMES = {".git", ".stversions", ".trash", ".Trash", "__pycache__"}
SYNC_CONFLICT_MARKER = ".sync-conflict-"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?$")
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MARKDOWN_LINK_RE = re.compile(r"(!?)\[[^\]\n]*\]\(([^)\n]+)\)")
BLOCK_ID_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9_-]+)\s*$")
CALLOUT_RE = re.compile(r"^\s*>\s*\[!([^\]\n]+)\]", re.IGNORECASE)
TAG_RE = re.compile(r"(?<![\w/])#([\w][\w/-]*)", re.UNICODE)
TASK_RE = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+")


class PreservationError(RuntimeError):
    """Raised for an invalid or unsafe verifier input."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def failure(code: str, message: str, path: str | None = None) -> dict[str, str]:
    item = {"code": code, "message": message}
    if path is not None:
        item["path"] = path
    return item


def split_frontmatter(data: bytes) -> tuple[bytes, bytes, bool]:
    """Return raw frontmatter, raw body, and whether frontmatter exists.

    Delimiters and line endings remain in the frontmatter segment.  Therefore a
    body hash is byte-exact and can prove that frontmatter-only normalization
    did not touch note content.
    """

    lines = data.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        return b"", data, False
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip(b"\r\n") in {b"---", b"..."}:
            return b"".join(lines[: index + 1]), b"".join(lines[index + 1 :]), True
    raise PreservationError("opening YAML frontmatter delimiter has no closing delimiter")


def _sequence_digest(items: Iterable[Any]) -> str:
    return digest_json(list(items))


def structure_fingerprint(body: bytes) -> dict[str, Any]:
    """Return content-free structural diagnostics for one Markdown body."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PreservationError(f"Markdown is not valid UTF-8: {exc}") from exc

    headings: list[tuple[int, str]] = []
    wikilinks: list[tuple[bool, str]] = []
    markdown_links: list[tuple[bool, str]] = []
    block_ids: list[str] = []
    callouts: list[str] = []
    tags: list[str] = []
    fence_languages: list[str] = []
    task_count = 0
    fenced_code_blocks = 0
    in_fence = False
    fence_char = ""
    fence_length = 0
    paragraph_count = 0
    in_paragraph = False
    non_empty_line_count = 0

    lines = text.splitlines()
    for line in lines:
        fence_match = FENCE_RE.match(line)
        if in_fence:
            if fence_match:
                marker = fence_match.group(1)
                if marker[0] == fence_char and len(marker) >= fence_length:
                    in_fence = False
            continue
        if fence_match:
            marker = fence_match.group(1)
            in_fence = True
            fence_char = marker[0]
            fence_length = len(marker)
            fenced_code_blocks += 1
            info = fence_match.group(2).strip()
            fence_languages.append(info.split(maxsplit=1)[0] if info else "")
            in_paragraph = False
            continue

        stripped = line.strip()
        if stripped:
            non_empty_line_count += 1
            if not in_paragraph:
                paragraph_count += 1
                in_paragraph = True
        else:
            in_paragraph = False

        heading_match = HEADING_RE.match(line)
        if heading_match:
            headings.append((len(heading_match.group(1)), heading_match.group(2)))
        wikilinks.extend(
            (bool(match.group(1)), match.group(2))
            for match in WIKILINK_RE.finditer(line)
        )
        markdown_links.extend(
            (bool(match.group(1)), match.group(2))
            for match in MARKDOWN_LINK_RE.finditer(line)
        )
        block_match = BLOCK_ID_RE.search(line)
        if block_match:
            block_ids.append(block_match.group(1))
        callout_match = CALLOUT_RE.match(line)
        if callout_match:
            callouts.append(callout_match.group(1))
        tags.extend(match.group(1) for match in TAG_RE.finditer(line))
        if TASK_RE.match(line):
            task_count += 1

    context_payload = {
        "headings": headings,
        "wikilinks": wikilinks,
        "markdown_links": markdown_links,
        "block_ids": block_ids,
        "callouts": callouts,
        "tags": tags,
        "fence_languages": fence_languages,
        "task_count": task_count,
        "fenced_code_blocks": fenced_code_blocks,
        "unclosed_fence": in_fence,
        "paragraph_count": paragraph_count,
        "line_count": len(lines),
        "non_empty_line_count": non_empty_line_count,
    }
    return {
        "context_sha256": digest_json(context_payload),
        "heading_count": len(headings),
        "headings_sha256": _sequence_digest(headings),
        "wikilink_count": len(wikilinks),
        "wikilinks_sha256": _sequence_digest(wikilinks),
        "markdown_link_count": len(markdown_links),
        "markdown_links_sha256": _sequence_digest(markdown_links),
        "block_id_count": len(block_ids),
        "callout_count": len(callouts),
        "tag_count": len(tags),
        "task_count": task_count,
        "fenced_code_blocks": fenced_code_blocks,
        "fence_languages_sha256": _sequence_digest(fence_languages),
        "unclosed_fence": in_fence,
        "paragraph_count": paragraph_count,
        "line_count": len(lines),
        "non_empty_line_count": non_empty_line_count,
    }


def inventory_record(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    frontmatter, body, has_frontmatter = split_frontmatter(data) if path.suffix.lower() == ".md" else (b"", data, False)
    return {
        "sha256": sha256_bytes(data),
        "size_bytes": len(data),
        "body_sha256": sha256_bytes(body),
        "body_size_bytes": len(body),
        "frontmatter_sha256": sha256_bytes(frontmatter) if has_frontmatter else None,
        "frontmatter_size_bytes": len(frontmatter),
        "has_frontmatter": has_frontmatter,
        "structure": structure_fingerprint(body) if path.suffix.lower() == ".md" else {"context_sha256": sha256_bytes(body), "kind": "binary"},
    }


def normalized_collision_key(relative_path: str) -> str:
    return unicodedata.normalize("NFC", relative_path).casefold()


def validate_roots(roots: Sequence[str]) -> tuple[str, ...]:
    if not roots:
        raise PreservationError("at least one preserved root is required")
    normalized: list[str] = []
    pure_roots: list[PurePosixPath] = []
    for root in roots:
        if not isinstance(root, str) or not root or "\\" in root:
            raise PreservationError("preserved roots must be non-empty POSIX paths")
        pure = PurePosixPath(root)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise PreservationError(f"unsafe preserved root: {root!r}")
        if len(pure.parts) != 1:
            raise PreservationError(
                f"preserved roots must be top-level vault directories: {root!r}"
            )
        if pure.as_posix() != root:
            raise PreservationError(f"non-canonical preserved root: {root!r}")
        if root in normalized:
            raise PreservationError(f"duplicate preserved root: {root!r}")
        normalized.append(root)
        pure_roots.append(pure)
    for index, left in enumerate(pure_roots):
        for right in pure_roots[index + 1 :]:
            shorter, longer = sorted((left.parts, right.parts), key=len)
            if longer[: len(shorter)] == shorter:
                raise PreservationError(
                    f"overlapping preserved roots: {left.as_posix()!r}, "
                    f"{right.as_posix()!r}"
                )
    return tuple(normalized)


def collect_inventory(
    vault: Path, roots: Sequence[str], *, schema_version: int = SCHEMA_VERSION
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    vault = vault.resolve()
    roots = validate_roots(roots)
    issues: list[dict[str, str]] = []
    files: dict[str, dict[str, Any]] = {}
    collision_keys: dict[str, str] = {}

    if not vault.is_dir():
        raise PreservationError(f"vault is not a directory: {vault}")

    def walk_error(error: OSError) -> None:
        issues.append(failure("walk_error", str(error)))

    for root in roots:
        root_path = vault / root
        try:
            root_path.resolve().relative_to(vault)
        except (OSError, ValueError) as exc:
            raise PreservationError(
                f"preserved root resolves outside the vault: {root!r}"
            ) from exc
        if not root_path.is_dir():
            issues.append(failure("missing_root", "required corpus root is missing", root))
            continue
        if root_path.is_symlink():
            issues.append(failure("symlink_root", "corpus root must not be a symlink", root))
            continue

        for directory, dirnames, filenames in os.walk(
            root_path, topdown=True, followlinks=False, onerror=walk_error
        ):
            directory_path = Path(directory)
            kept_dirs: list[str] = []
            for name in sorted(dirnames):
                candidate = directory_path / name
                relative = candidate.relative_to(vault).as_posix()
                if SYNC_CONFLICT_MARKER in name.lower():
                    issues.append(
                        failure(
                            "sync_conflict",
                            "Syncthing conflict artifact blocks preservation proof",
                            relative,
                        )
                    )
                    continue
                if name in IGNORED_DIR_NAMES:
                    continue
                if candidate.is_symlink():
                    issues.append(
                        failure(
                            "symlink_directory",
                            "symlinked directories are not trusted corpus inputs",
                            relative,
                        )
                    )
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs

            for name in sorted(filenames):
                candidate = directory_path / name
                relative = candidate.relative_to(vault).as_posix()
                if SYNC_CONFLICT_MARKER in name.lower():
                    issues.append(
                        failure(
                            "sync_conflict",
                            "Syncthing conflict artifact blocks preservation proof",
                            relative,
                        )
                    )
                    continue
                if schema_version == 1 and candidate.suffix.lower() != ".md":
                    continue
                if candidate.is_symlink():
                    issues.append(
                        failure(
                            "symlink_file",
                            "symlinked Markdown is not a trusted corpus input",
                            relative,
                        )
                    )
                    continue
                collision_key = normalized_collision_key(relative)
                prior = collision_keys.get(collision_key)
                if prior is not None and prior != relative:
                    issues.append(
                        failure(
                            "portable_path_collision",
                            f"path collides with {prior!r} after NFC/case folding",
                            relative,
                        )
                    )
                    continue
                collision_keys[collision_key] = relative
                try:
                    files[relative] = inventory_record(candidate)
                except (OSError, PreservationError) as exc:
                    issues.append(failure("unreadable_note", str(exc), relative))

    files = dict(sorted(files.items()))
    digest_payload = {
        "schema_version": schema_version,
        "roots": list(roots),
        "note_count": len(files),
        "files": files,
    }
    inventory = {
        **digest_payload,
        "inventory_sha256": digest_json(digest_payload),
    }
    return inventory, issues


def collect_stable_inventory(
    vault: Path, roots: Sequence[str], *, schema_version: int = SCHEMA_VERSION
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Scan twice so an actively changing corpus cannot become a baseline."""

    first, first_issues = collect_inventory(vault, roots, schema_version=schema_version)
    if first_issues:
        return first, first_issues
    second, second_issues = collect_inventory(vault, roots, schema_version=schema_version)
    if second_issues:
        return second, second_issues
    if first["inventory_sha256"] != second["inventory_sha256"]:
        return second, [
            failure(
                "unstable_corpus",
                "corpus changed between consecutive inventory passes; quiesce writers",
            )
        ]
    return second, []


def snapshot_document(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        **inventory,
        "created_at_utc": utc_now(),
        "tool": "verify_context_preservation.py",
    }


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise PreservationError("baseline snapshot must be a JSON object")
    if snapshot.get("schema_version") not in {1, SCHEMA_VERSION}:
        raise PreservationError(
            f"unsupported baseline schema_version: {snapshot.get('schema_version')!r}"
        )
    roots = snapshot.get("roots")
    files = snapshot.get("files")
    note_count = snapshot.get("note_count")
    if not isinstance(roots, list) or not roots or not all(
        isinstance(root, str) and root for root in roots
    ):
        raise PreservationError("baseline roots must be a non-empty string list")
    validated_roots = validate_roots(roots)
    if not isinstance(files, dict):
        raise PreservationError("baseline files must be an object")
    if not _is_nonnegative_int(note_count) or note_count != len(files):
        raise PreservationError("baseline note_count does not match files")

    required_record_keys = {
        "sha256",
        "size_bytes",
        "body_sha256",
        "body_size_bytes",
        "frontmatter_sha256",
        "frontmatter_size_bytes",
        "has_frontmatter",
        "structure",
    }
    collision_keys: dict[str, str] = {}
    for relative, record in files.items():
        validate_corpus_path(relative, validated_roots, "baseline path")
        if snapshot["schema_version"] == 1 and Path(relative).suffix.lower() != ".md":
            raise PreservationError(f"schema-1 baseline requires Markdown: {relative}")
        collision_key = normalized_collision_key(relative)
        prior = collision_keys.get(collision_key)
        if prior is not None and prior != relative:
            raise PreservationError(
                f"baseline paths collide after NFC/case folding: {prior!r}, {relative!r}"
            )
        collision_keys[collision_key] = relative
        if not isinstance(record, dict) or not required_record_keys.issubset(record):
            raise PreservationError(f"malformed baseline record: {relative}")
        if not SHA256_RE.fullmatch(str(record.get("sha256", ""))):
            raise PreservationError(f"invalid full SHA-256 in baseline: {relative}")
        if not SHA256_RE.fullmatch(str(record.get("body_sha256", ""))):
            raise PreservationError(f"invalid body SHA-256 in baseline: {relative}")
        if (
            not _is_nonnegative_int(record.get("size_bytes"))
            or not _is_nonnegative_int(record.get("body_size_bytes"))
            or not _is_nonnegative_int(record.get("frontmatter_size_bytes"))
        ):
            raise PreservationError(f"invalid sizes in baseline: {relative}")
        if (
            record["body_size_bytes"] + record["frontmatter_size_bytes"]
            != record["size_bytes"]
        ):
            raise PreservationError(f"segment sizes do not add up in baseline: {relative}")
        if not isinstance(record.get("has_frontmatter"), bool):
            raise PreservationError(f"invalid frontmatter flag in baseline: {relative}")
        if record["has_frontmatter"]:
            if not SHA256_RE.fullmatch(str(record.get("frontmatter_sha256", ""))):
                raise PreservationError(
                    f"invalid frontmatter SHA-256 in baseline: {relative}"
                )
        elif (
            record.get("frontmatter_sha256") is not None
            or record["frontmatter_size_bytes"] != 0
        ):
            raise PreservationError(
                f"frontmatter metadata is inconsistent in baseline: {relative}"
            )
        if not isinstance(record.get("structure"), dict) or not SHA256_RE.fullmatch(
            str(record["structure"].get("context_sha256", ""))
        ):
            raise PreservationError(f"invalid structure fingerprint: {relative}")

    payload = {
        "schema_version": snapshot["schema_version"],
        "roots": list(validated_roots),
        "note_count": note_count,
        "files": files,
    }
    expected_digest = digest_json(payload)
    if snapshot.get("inventory_sha256") != expected_digest:
        raise PreservationError("baseline inventory_sha256 does not match its contents")
    return snapshot


def validate_corpus_path(path: Any, roots: Sequence[str], label: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path:
        raise PreservationError(f"{label} must be a non-empty POSIX path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PreservationError(f"unsafe {label}: {path!r}")
    if pure.as_posix() != path:
        raise PreservationError(f"invalid file {label}: {path!r}")
    if not pure.parts or pure.parts[0] not in roots:
        raise PreservationError(f"{label} is outside the preserved roots: {path!r}")
    return path


def _require_exact_keys(item: dict[str, Any], keys: set[str], label: str) -> None:
    if set(item) != keys:
        missing = sorted(keys - set(item))
        extra = sorted(set(item) - keys)
        raise PreservationError(
            f"{label} has wrong keys; missing={missing!r}, extra={extra!r}"
        )


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PreservationError(f"{label} must be a lowercase SHA-256")
    return value


def _require_reason(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreservationError(f"{label} requires a non-empty reason")
    return value.strip()


def empty_allowlist() -> dict[str, Any]:
    return {
        "schema_version": ALLOWLIST_SCHEMA_VERSION,
        "changes": [],
        "additions": [],
        "renames": [],
    }


def validate_allowlist(value: Any, roots: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreservationError("allowlist must be a JSON object")
    _require_exact_keys(
        value,
        {"schema_version", "changes", "additions", "renames"},
        "allowlist",
    )
    if value.get("schema_version") != ALLOWLIST_SCHEMA_VERSION:
        raise PreservationError(
            f"unsupported allowlist schema_version: {value.get('schema_version')!r}"
        )
    for key in ("changes", "additions", "renames"):
        if not isinstance(value.get(key), list):
            raise PreservationError(f"allowlist {key} must be a list")

    used_sources: set[str] = set()
    used_targets: set[str] = set()
    normalized: dict[str, Any] = empty_allowlist()

    for index, item in enumerate(value["changes"]):
        label = f"changes[{index}]"
        if not isinstance(item, dict):
            raise PreservationError(f"{label} must be an object")
        _require_exact_keys(
            item,
            {"path", "mode", "before_sha256", "after_sha256", "reason"},
            label,
        )
        path = validate_corpus_path(item["path"], roots, f"{label}.path")
        if item["mode"] not in {"frontmatter_only", "append_only", "replace"}:
            raise PreservationError(f"unsupported {label}.mode: {item['mode']!r}")
        if path in used_sources or path in used_targets:
            raise PreservationError(f"allowlist path is reused: {path}")
        used_sources.add(path)
        used_targets.add(path)
        normalized["changes"].append(
            {
                **item,
                "before_sha256": _require_hash(
                    item["before_sha256"], f"{label}.before_sha256"
                ),
                "after_sha256": _require_hash(
                    item["after_sha256"], f"{label}.after_sha256"
                ),
                "reason": _require_reason(item["reason"], label),
            }
        )

    for index, item in enumerate(value["additions"]):
        label = f"additions[{index}]"
        if not isinstance(item, dict):
            raise PreservationError(f"{label} must be an object")
        _require_exact_keys(item, {"path", "after_sha256", "reason"}, label)
        path = validate_corpus_path(item["path"], roots, f"{label}.path")
        if path in used_sources or path in used_targets:
            raise PreservationError(f"allowlist path is reused: {path}")
        used_targets.add(path)
        normalized["additions"].append(
            {
                **item,
                "after_sha256": _require_hash(
                    item["after_sha256"], f"{label}.after_sha256"
                ),
                "reason": _require_reason(item["reason"], label),
            }
        )

    for index, item in enumerate(value["renames"]):
        label = f"renames[{index}]"
        if not isinstance(item, dict):
            raise PreservationError(f"{label} must be an object")
        _require_exact_keys(
            item,
            {"from", "to", "mode", "before_sha256", "after_sha256", "reason"},
            label,
        )
        source = validate_corpus_path(item["from"], roots, f"{label}.from")
        target = validate_corpus_path(item["to"], roots, f"{label}.to")
        if source == target:
            raise PreservationError(f"{label} source and target are identical")
        if item["mode"] not in {"exact_file", "exact_body", "replace"}:
            raise PreservationError(f"unsupported {label}.mode: {item['mode']!r}")
        if (
            source in used_sources
            or source in used_targets
            or target in used_sources
            or target in used_targets
        ):
            raise PreservationError(
                f"allowlist source/target is reused: {source} -> {target}"
            )
        used_sources.add(source)
        used_targets.add(target)
        normalized["renames"].append(
            {
                **item,
                "before_sha256": _require_hash(
                    item["before_sha256"], f"{label}.before_sha256"
                ),
                "after_sha256": _require_hash(
                    item["after_sha256"], f"{label}.after_sha256"
                ),
                "reason": _require_reason(item["reason"], label),
            }
        )

    return normalized


def read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PreservationError(f"cannot read {label} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PreservationError(f"invalid JSON in {label} {path}: {exc}") from exc


def _check_pins(
    item: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    path: str,
    failures: list[dict[str, str]],
) -> None:
    if before["sha256"] != item["before_sha256"]:
        failures.append(
            failure(
                "before_hash_mismatch",
                "allowlist before_sha256 does not match the frozen baseline",
                path,
            )
        )
    if after["sha256"] != item["after_sha256"]:
        failures.append(
            failure(
                "after_hash_mismatch",
                "allowlist after_sha256 does not match the observed file",
                path,
            )
        )


def verify_preservation(
    baseline: dict[str, Any],
    after_vault: Path,
    allowlist: dict[str, Any],
    expected_baseline_sha256: str,
    expected_baseline_count: int | None = None,
    expected_allowlist_sha256: str | None = None,
) -> dict[str, Any]:
    baseline = validate_snapshot(baseline)
    expected_baseline_sha256 = _require_hash(
        expected_baseline_sha256, "expected baseline inventory SHA-256"
    )
    if baseline["inventory_sha256"] != expected_baseline_sha256:
        raise PreservationError(
            "baseline inventory SHA-256 does not match the externally pinned digest"
        )
    allowlist = validate_allowlist(allowlist, baseline["roots"])
    allowlist_sha256 = digest_json(allowlist)
    if expected_allowlist_sha256 is not None:
        expected_allowlist_sha256 = _require_hash(
            expected_allowlist_sha256, "expected allowlist SHA-256"
        )
        if allowlist_sha256 != expected_allowlist_sha256:
            raise PreservationError(
                "allowlist SHA-256 does not match the externally pinned digest"
            )
    after_vault = after_vault.resolve()
    after, collection_issues = collect_stable_inventory(
        after_vault, baseline["roots"], schema_version=baseline["schema_version"]
    )
    failures = list(collection_issues)
    before_files = baseline["files"]
    after_files = after["files"]
    before_paths = set(before_files)
    after_paths = set(after_files)

    if expected_baseline_count is not None:
        if not _is_nonnegative_int(expected_baseline_count):
            raise PreservationError("expected baseline count must be a non-negative integer")
        if baseline["note_count"] != expected_baseline_count:
            failures.append(
                failure(
                    "baseline_count_mismatch",
                    f"expected {expected_baseline_count}, found {baseline['note_count']}",
                )
            )

    change_by_path = {item["path"]: item for item in allowlist["changes"]}
    addition_by_path = {item["path"]: item for item in allowlist["additions"]}
    rename_by_source = {item["from"]: item for item in allowlist["renames"]}
    rename_by_target = {item["to"]: item for item in allowlist["renames"]}

    expected_after_count = baseline["note_count"] + len(addition_by_path)
    if after["note_count"] != expected_after_count:
        failures.append(
            failure(
                "after_count_mismatch",
                f"expected {expected_after_count}, found {after['note_count']}",
            )
        )

    unchanged = 0
    approved_changes = 0
    approved_additions = 0
    approved_renames = 0

    for path in sorted(before_paths & after_paths):
        before_record = before_files[path]
        after_record = after_files[path]
        item = change_by_path.get(path)
        if item is None:
            if before_record["sha256"] != after_record["sha256"]:
                failures.append(
                    failure(
                        "unapproved_modification",
                        "full-file SHA-256 changed without an allowlist entry",
                        path,
                    )
                )
            else:
                unchanged += 1
            continue

        if before_record["sha256"] == after_record["sha256"]:
            failures.append(
                failure(
                    "unused_change",
                    "allowlisted change did not occur; remove or refresh the entry",
                    path,
                )
            )
            continue
        _check_pins(item, before_record, after_record, path, failures)
        mode = item["mode"]
        if mode == "frontmatter_only":
            if before_record["body_sha256"] != after_record["body_sha256"]:
                failures.append(
                    failure(
                        "body_changed",
                        "frontmatter_only entry changed body bytes",
                        path,
                    )
                )
            if (
                before_record["structure"]["context_sha256"]
                != after_record["structure"]["context_sha256"]
            ):
                failures.append(
                    failure(
                        "context_changed",
                        "frontmatter_only entry changed structural context",
                        path,
                    )
                )
        elif mode == "append_only":
            try:
                after_data = (after_vault / PurePosixPath(path)).read_bytes()
            except OSError as exc:
                failures.append(failure("read_error", str(exc), path))
            else:
                before_size = before_record["size_bytes"]
                if len(after_data) <= before_size or sha256_bytes(
                    after_data[:before_size]
                ) != before_record["sha256"]:
                    failures.append(
                        failure(
                            "not_append_only",
                            "append_only entry did not preserve the complete old byte prefix",
                            path,
                        )
                    )
        approved_changes += 1

    disappeared = before_paths - after_paths
    appeared = after_paths - before_paths

    for source in sorted(disappeared):
        item = rename_by_source.get(source)
        if item is None:
            failures.append(
                failure("unapproved_deletion", "baseline note disappeared", source)
            )
            continue
        target = item["to"]
        if target not in appeared:
            failures.append(
                failure(
                    "missing_rename_target",
                    f"allowlisted rename target is absent: {target}",
                    source,
                )
            )
            continue
        before_record = before_files[source]
        after_record = after_files[target]
        _check_pins(item, before_record, after_record, target, failures)
        if item["mode"] == "exact_file" and before_record["sha256"] != after_record[
            "sha256"
        ]:
            failures.append(
                failure(
                    "rename_content_changed",
                    "exact_file rename changed file bytes",
                    target,
                )
            )
        elif item["mode"] == "exact_body":
            if before_record["body_sha256"] != after_record["body_sha256"]:
                failures.append(
                    failure(
                        "rename_body_changed",
                        "exact_body rename changed body bytes",
                        target,
                    )
                )
            if (
                before_record["structure"]["context_sha256"]
                != after_record["structure"]["context_sha256"]
            ):
                failures.append(
                    failure(
                        "rename_context_changed",
                        "exact_body rename changed structural context",
                        target,
                    )
                )
        approved_renames += 1

    for target in sorted(appeared):
        if target in rename_by_target:
            if rename_by_target[target]["from"] not in disappeared:
                failures.append(
                    failure(
                        "unused_rename",
                        "allowlisted rename source did not disappear",
                        target,
                    )
                )
            continue
        item = addition_by_path.get(target)
        if item is None:
            failures.append(
                failure("unapproved_addition", "new note is not allowlisted", target)
            )
            continue
        if after_files[target]["sha256"] != item["after_sha256"]:
            failures.append(
                failure(
                    "addition_hash_mismatch",
                    "allowlist after_sha256 does not match the added note",
                    target,
                )
            )
        approved_additions += 1

    for path in sorted(change_by_path):
        if path not in before_paths or path not in after_paths:
            failures.append(
                failure(
                    "invalid_change_target",
                    "allowlisted change must exist before and after",
                    path,
                )
            )
    for path in sorted(addition_by_path):
        if path in before_paths or path not in after_paths:
            failures.append(
                failure(
                    "invalid_addition_target",
                    "allowlisted addition must be absent before and present after",
                    path,
                )
            )
    for source, item in sorted(rename_by_source.items()):
        if source not in before_paths or source in after_paths:
            failures.append(
                failure(
                    "invalid_rename_source",
                    "allowlisted rename source must exist before and be absent after",
                    source,
                )
            )
        if item["to"] in before_paths or item["to"] not in after_paths:
            failures.append(
                failure(
                    "invalid_rename_target",
                    "allowlisted rename target must be absent before and present after",
                    item["to"],
                )
            )

    failures.sort(key=lambda item: (item.get("path", ""), item["code"], item["message"]))
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if not failures else "fail",
        "created_at_utc": utc_now(),
        "baseline_inventory_sha256": baseline["inventory_sha256"],
        "expected_baseline_inventory_sha256": expected_baseline_sha256,
        "after_inventory_sha256": after["inventory_sha256"],
        "allowlist_sha256": allowlist_sha256,
        "expected_allowlist_sha256": expected_allowlist_sha256,
        "baseline_note_count": baseline["note_count"],
        "after_note_count": after["note_count"],
        "expected_after_note_count": expected_after_count,
        "summary": {
            "unchanged": unchanged,
            "approved_changes": approved_changes,
            "approved_additions": approved_additions,
            "approved_renames": approved_renames,
            "failures": len(failures),
        },
        "failures": failures,
    }
    return report


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def write_json_outside_vault(path: Path, value: Any, vault: Path) -> None:
    if path_is_within(path, vault):
        raise PreservationError(
            f"evidence output must be outside the vault being measured: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    temporary_name = ""
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary_name = handle.name
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temporary_name, path)
    except OSError as exc:
        raise PreservationError(f"cannot write evidence {path}: {exc}") from exc
    finally:
        if handle is not None:
            handle.close()
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser(
        "snapshot", help="write a hash-only baseline outside the vault"
    )
    snapshot.add_argument("--vault", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="preserved root; repeat to override the seven default user roots",
    )
    snapshot.add_argument("--expected-count", type=int)

    verify = subparsers.add_parser(
        "verify", help="compare a later vault with a frozen baseline"
    )
    verify.add_argument("--baseline", type=Path, required=True)
    verify.add_argument("--after-vault", type=Path, required=True)
    verify.add_argument("--allowlist", type=Path)
    verify.add_argument("--report", type=Path)
    verify.add_argument(
        "--expected-baseline-sha256",
        required=True,
        help="digest printed by snapshot and frozen outside the baseline JSON",
    )
    verify.add_argument(
        "--expected-allowlist-sha256",
        help="digest printed by allowlist-digest; required with --allowlist",
    )
    verify.add_argument("--expected-baseline-count", type=int)

    allowlist_digest = subparsers.add_parser(
        "allowlist-digest", help="validate and print the canonical allowlist digest"
    )
    allowlist_digest.add_argument("--baseline", type=Path, required=True)
    allowlist_digest.add_argument("--allowlist", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            roots = tuple(args.roots or DEFAULT_ROOTS)
            if len(set(roots)) != len(roots):
                raise PreservationError("snapshot roots contain duplicates")
            inventory, issues = collect_stable_inventory(args.vault, roots)
            if issues:
                for issue in issues:
                    print(json.dumps(issue, ensure_ascii=False), file=sys.stderr)
                return 1
            if args.expected_count is not None and inventory["note_count"] != args.expected_count:
                print(
                    f"FAIL: expected {args.expected_count} notes, found {inventory['note_count']}",
                    file=sys.stderr,
                )
                return 1
            document = snapshot_document(inventory)
            write_json_outside_vault(args.output, document, args.vault)
            print(
                f"PASS snapshot: notes={inventory['note_count']} "
                f"inventory_sha256={inventory['inventory_sha256']} output={args.output}"
            )
            return 0

        baseline = validate_snapshot(read_json(args.baseline, "baseline"))
        if args.allowlist is None:
            allowlist = empty_allowlist()
        else:
            allowlist = validate_allowlist(
                read_json(args.allowlist, "allowlist"), baseline["roots"]
            )
        if args.command == "allowlist-digest":
            print(f"allowlist_sha256={digest_json(allowlist)}")
            return 0
        if args.allowlist is not None and args.expected_allowlist_sha256 is None:
            raise PreservationError(
                "--expected-allowlist-sha256 is required when --allowlist is used"
            )
        report = verify_preservation(
            baseline,
            args.after_vault,
            allowlist,
            args.expected_baseline_sha256,
            args.expected_baseline_count,
            args.expected_allowlist_sha256,
        )
        if args.report is not None:
            write_json_outside_vault(args.report, report, args.after_vault)
        print(
            f"{report['status'].upper()} preservation: "
            f"before={report['baseline_note_count']} "
            f"after={report['after_note_count']} "
            f"failures={len(report['failures'])}"
        )
        for item in report["failures"]:
            location = f" [{item['path']}]" if "path" in item else ""
            print(f"- {item['code']}{location}: {item['message']}")
        return 0 if report["status"] == "pass" else 1
    except PreservationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
