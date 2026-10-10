#!/usr/bin/env python3
"""Fail-loud, warning-only PostToolUse validator for Context Vault writes."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Iterable, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_metadata import strict_frontmatter, frontmatter_syntax_problems


DEFAULT_VAULT = Path(os.environ.get("CONTEXT_VAULT_PATH", Path.home() / "Documents" / "context_vault")).expanduser()
PATCH_OP_RE = re.compile(
    r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", re.MULTILINE
)
PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+?)\s*$", re.MULTILINE)
DIFF_OLD_RE = re.compile(r"^--- (.+?)\s*$")
DIFF_NEW_RE = re.compile(r"^\+\+\+ (.+?)\s*$")
FALLBACK_CONTROL_PLANE = (
    ".obsidian/**",
    "50_System/**",
    "AGENTS.md",
    "README.md",
    "Welcome.md",
)
EMPTY_SCALARS = {"", "''", '""', "[]", "{}", "null", "none", "~"}


class Touch(NamedTuple):
    path: Path
    action: str = "write"
    related: Path | None = None


def canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def load_json_detailed(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "file is missing"
    if not path.is_file():
        return None, "path is not a file"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot read valid JSON: {exc}"
    if not isinstance(value, dict):
        return None, "JSON root must be an object"
    return value, None


def load_json(path: Path) -> dict[str, Any]:
    value, _ = load_json_detailed(path)
    return value or {}


def frontmatter(text: str) -> dict[str, str] | None:
    return strict_frontmatter(text)


def scalar(value: str | None) -> str:
    return value.strip().strip("\"'") if value else ""


def walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_strings(child)


def normalize_candidate(raw: str, cwd: Path) -> Path | None:
    candidate = raw.strip().strip("\"'")
    if not candidate or candidate == "/dev/null":
        return None
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return canonical_path(path)


def _diff_candidate(raw: str, cwd: Path) -> Path | None:
    raw = raw.split("\t", 1)[0].strip()
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return normalize_candidate(raw, cwd)


def _patch_touches(text: str, cwd: Path) -> list[Touch]:
    touches: list[Touch] = []
    matches = list(PATCH_OP_RE.finditer(text))
    for index, match in enumerate(matches):
        operation, raw_path = match.groups()
        path = normalize_candidate(raw_path, cwd)
        if path is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        move = PATCH_MOVE_RE.search(block) if operation == "Update" else None
        if move:
            destination = normalize_candidate(move.group(1), cwd)
            if destination is not None:
                touches.append(Touch(path, "move_from", destination))
                touches.append(Touch(destination, "move_to", path))
            else:
                touches.append(Touch(path, "write"))
        elif operation == "Delete":
            touches.append(Touch(path, "delete"))
        else:
            touches.append(Touch(path, "write"))
    return touches


def _diff_touches(text: str, cwd: Path) -> list[Touch]:
    touches: list[Touch] = []
    old_raw: str | None = None
    for line in text.splitlines():
        old_match = DIFF_OLD_RE.match(line)
        if old_match:
            old_raw = old_match.group(1)
            continue
        new_match = DIFF_NEW_RE.match(line)
        if not new_match or old_raw is None:
            continue
        old_path = _diff_candidate(old_raw, cwd)
        new_path = _diff_candidate(new_match.group(1), cwd)
        old_raw = None
        if old_path is None and new_path is not None:
            touches.append(Touch(new_path, "write"))
        elif old_path is not None and new_path is None:
            touches.append(Touch(old_path, "delete"))
        elif old_path is not None and new_path is not None:
            if old_path == new_path:
                touches.append(Touch(new_path, "write"))
            else:
                touches.append(Touch(old_path, "move_from", new_path))
                touches.append(Touch(new_path, "move_to", old_path))
    return touches


def discover_touches(payload: dict[str, Any], cwd: Path) -> list[Touch]:
    found: list[Touch] = []
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        move_pairs = (
            ("source_path", "destination_path"),
            ("old_path", "new_path"),
            ("from_path", "to_path"),
        )
        consumed: set[str] = set()
        for source_key, destination_key in move_pairs:
            source_raw = tool_input.get(source_key)
            destination_raw = tool_input.get(destination_key)
            if isinstance(source_raw, str) and isinstance(destination_raw, str):
                source = normalize_candidate(source_raw, cwd)
                destination = normalize_candidate(destination_raw, cwd)
                if source is not None and destination is not None:
                    found.append(Touch(source, "move_from", destination))
                    found.append(Touch(destination, "move_to", source))
                    consumed.update({source_key, destination_key})
        for key in ("file_path", "path", "target_path"):
            if key in consumed:
                continue
            raw = tool_input.get(key)
            if isinstance(raw, str):
                candidate = normalize_candidate(raw, cwd)
                if candidate is not None:
                    found.append(Touch(candidate, "write"))

    for text in walk_strings(tool_input):
        found.extend(_patch_touches(text, cwd))
        found.extend(_diff_touches(text, cwd))

    unique: dict[tuple[str, str, str], Touch] = {}
    for touch in found:
        key = (
            str(touch.path),
            touch.action,
            str(touch.related) if touch.related is not None else "",
        )
        unique[key] = touch
    return sorted(
        unique.values(),
        key=lambda item: (
            str(item.path),
            item.action,
            str(item.related) if item.related is not None else "",
        ),
    )


def discover_paths(payload: dict[str, Any], cwd: Path) -> list[Path]:
    """Compatibility wrapper used by existing hook tests and callers."""

    return sorted({touch.path for touch in discover_touches(payload, cwd)})


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def rule_for(relpath: str, manifest: dict[str, Any]) -> dict[str, Any] | None:
    rules = manifest.get("path_rules", [])
    if isinstance(rules, list):
        for rule in rules:
            if isinstance(rule, dict) and fnmatch.fnmatchcase(
                relpath, str(rule.get("glob", ""))
            ):
                return rule
    parts = Path(relpath).parts
    if (
        len(parts) == 3
        and parts[0] == "10_Projects"
        and Path(parts[2]).stem == parts[1]
    ):
        return {
            "note_type": "project",
            "max_chars": 12000,
            "size_severity": "WARN",
        }
    return None


def _schema_for(
    rule: dict[str, Any] | None, manifest: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    note_types = manifest.get("note_types", {})
    if not isinstance(note_types, dict):
        return {}, {}, None
    common = note_types.get("common", {})
    if not isinstance(common, dict):
        common = {}
    note_type = rule.get("note_type") if isinstance(rule, dict) else None
    typed = note_types.get(note_type, {}) if isinstance(note_type, str) else {}
    if not isinstance(typed, dict):
        typed = {}
    return common, typed, note_type if isinstance(note_type, str) else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def required_keys(rule: dict[str, Any] | None, manifest: dict[str, Any]) -> list[str]:
    common, typed, _ = _schema_for(rule, manifest)
    keys = _string_list(common.get("required")) + _string_list(typed.get("required"))
    return list(dict.fromkeys(keys))


def frontmatter_constraint_warnings(
    relpath: str,
    fm: dict[str, str],
    rule: dict[str, Any] | None,
    manifest: dict[str, Any],
) -> list[str]:
    common, typed, note_type = _schema_for(rule, manifest)
    warnings: list[str] = []

    exact_type = typed.get("exact_type", common.get("exact_type"))
    expected_type: str | None = None
    if isinstance(exact_type, str) and exact_type:
        expected_type = exact_type
    elif exact_type is True and note_type:
        expected_type = note_type
    accepted_types = _string_list(common.get("accepted_types")) + _string_list(
        typed.get("accepted_types")
    )
    allowed_types = set(accepted_types)
    if expected_type is not None:
        allowed_types.add(expected_type)
    actual_type = scalar(fm.get("type"))
    if allowed_types and actual_type not in allowed_types:
        expected_text = expected_type or ", ".join(sorted(allowed_types))
        warnings.append(
            f"{relpath}: frontmatter type must be `{expected_text}` or an accepted legacy type"
        )

    allowed_statuses = typed.get("status_values")
    if allowed_statuses is None:
        allowed_statuses = common.get("status_values")
    status_values = _string_list(allowed_statuses)
    aliases: dict[str, str] = {}
    for raw_aliases in (common.get("status_aliases"), typed.get("status_aliases")):
        if isinstance(raw_aliases, dict):
            aliases.update(
                {
                    key: value
                    for key, value in raw_aliases.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
            )
    actual_status = scalar(fm.get("status"))
    canonical_status = aliases.get(actual_status, actual_status)
    if status_values and canonical_status not in status_values:
        warnings.append(
            f"{relpath}: status must be one of: {', '.join(status_values)}"
        )

    nonempty = _string_list(common.get("nonempty")) + _string_list(
        typed.get("nonempty")
    )
    for field in dict.fromkeys(nonempty):
        if field in fm and fm[field].strip().casefold() in EMPTY_SCALARS:
            warnings.append(f"{relpath}: frontmatter `{field}` must not be empty")

    date_fields = _string_list(common.get("date_fields")) + _string_list(
        typed.get("date_fields")
    )
    for field in dict.fromkeys(date_fields):
        if field not in fm:
            continue
        value = scalar(fm.get(field))
        if relpath.startswith("50_System/Templates/") and value == "YYYY-MM-DD":
            continue
        try:
            date.fromisoformat(value)
        except ValueError:
            warnings.append(
                f"{relpath}: frontmatter `{field}` must be an ISO date (YYYY-MM-DD)"
            )
    return warnings


def ignored(relpath: str, manifest: dict[str, Any]) -> bool:
    validation = manifest.get("validation", {})
    patterns = validation.get("ignore_markdown", []) if isinstance(validation, dict) else []
    if not isinstance(patterns, list):
        return False
    return any(fnmatch.fnmatchcase(relpath, str(pattern)) for pattern in patterns)


def control_plane(relpath: str, manifest: dict[str, Any]) -> bool:
    patterns = manifest.get("infrastructure", FALLBACK_CONTROL_PLANE)
    if not isinstance(patterns, list):
        patterns = list(FALLBACK_CONTROL_PLANE)
    return any(fnmatch.fnmatchcase(relpath, str(pattern)) for pattern in patterns)


def index_warning(relpath: str, vault: Path, manifest: dict[str, Any]) -> str | None:
    indexes = manifest.get("indexes", {})
    if not isinstance(indexes, dict):
        return None

    expected: tuple[str, str] | None = None
    parts = Path(relpath).parts
    if len(parts) == 3 and parts[0] == "10_Projects" and parts[2] == "Memory_Manifest.md":
        expected = ("project", parts[1])
    elif len(parts) == 3 and parts[0] == "60_Sessions":
        expected = ("session", parts[1])
    elif len(parts) == 2 and parts[0] == "70_Decisions":
        expected = ("decision", Path(parts[1]).stem)

    if expected is None:
        return None
    kind, needle = expected
    index_rel = indexes.get(kind)
    if not isinstance(index_rel, str):
        return None
    try:
        index_text = (vault / index_rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"{relpath}: index `{index_rel}` is missing or unreadable"
    if needle not in index_text:
        return f"{relpath}: `{needle}` is not represented in `{index_rel}`"
    return None


def routing_warning(
    relpath: str,
    vault: Path,
    fm: dict[str, str],
    manifest: dict[str, Any] | None = None,
) -> str | None:
    parts = Path(relpath).parts
    declared = scalar(fm.get("project"))
    if (
        len(parts) == 3
        and parts[0] == "10_Projects"
        and parts[2] == "Memory_Manifest.md"
        and declared
        and declared != parts[1]
    ):
        return (
            f"{relpath}: frontmatter project `{declared}` must match folder `{parts[1]}`"
        )
    if len(parts) == 3 and parts[0] == "60_Sessions":
        validation = manifest.get("validation", {}) if isinstance(manifest, dict) else {}
        strict = isinstance(validation, dict) and any(
            key in validation
            for key in ("allowed_legacy_session_roots", "session_project_sentinels")
        )
        root = parts[1]
        if strict:
            legacy = set(_string_list(validation.get("allowed_legacy_session_roots")))
            configured_sentinels = _string_list(
                validation.get("session_project_sentinels")
            )
            sentinels = set(configured_sentinels or ["_general", "_unassigned"])
            canonical_roots = {
                path.parent.name
                for path in (vault / "10_Projects").glob("*/Memory_Manifest.md")
                if path.is_file() and ".sync-conflict-" not in path.name
            }
            if root in canonical_roots:
                if declared != root:
                    return f"{relpath}: canonical session root requires project `{root}`"
            elif root == "_general":
                if declared != "_general":
                    return f"{relpath}: `_general` session root requires project `_general`"
            elif root in legacy:
                allowed = {root}
                if "_unassigned" in sentinels:
                    allowed.add("_unassigned")
                if declared not in allowed:
                    return (
                        f"{relpath}: legacy session root requires project `{root}` "
                        "or `_unassigned`"
                    )
            else:
                return (
                    f"{relpath}: session root `{root}` is neither a canonical project "
                    "nor an allowed legacy root"
                )
        elif declared:
            canonical = vault / "10_Projects" / declared / "Memory_Manifest.md"
            if canonical.is_file() and declared != root:
                return f"{relpath}: project `{declared}` must use `60_Sessions/{declared}`"
    return None


def validate_content(text: str, path: Path, vault: Path, manifest: dict[str, Any]) -> list[str]:
    """Validate proposed Markdown at its destination without writing it.

    Index presence is checked after a batch; new routes need not exist yet.
    INFO size signals never block ordinary historical/session notes.
    """
    path, vault = canonical_path(path), canonical_path(vault)
    if not is_relative_to(path, vault) or path.suffix.lower() != ".md":
        return ["target must be a Markdown note inside the vault"]
    relpath = path.relative_to(vault).as_posix()
    if ignored(relpath, manifest):
        return []
    warnings: list[str] = []
    if text.startswith("\ufeff"):
        warnings.append(f"{relpath}: UTF-8 BOM is not allowed")
    rule = rule_for(relpath, manifest)
    fm = frontmatter(text)
    if fm is None:
        warnings.append(f"{relpath}: missing YAML frontmatter or invalid YAML syntax")
        warnings.extend(f"{relpath}: {problem}" for problem in frontmatter_syntax_problems(text))
    else:
        missing = [key for key in required_keys(rule, manifest) if key not in fm]
        if missing:
            warnings.append(f"{relpath}: missing frontmatter keys: {', '.join(missing)}")
        warnings.extend(frontmatter_constraint_warnings(relpath, fm, rule, manifest))
        route_problem = routing_warning(relpath, vault, fm, manifest)
        if route_problem:
            warnings.append(route_problem)

    if rule and rule.get("size_severity", "WARN") != "INFO" and type(rule.get("max_chars")) is int:
        limit = int(rule["max_chars"])
        if len(text) > limit:
            warnings.append(
                f"{relpath}: routing surface is {len(text)} chars; configured limit is {limit}"
            )

    return warnings


def validate_path(path: Path, vault: Path, manifest: dict[str, Any]) -> list[str]:
    path, vault = canonical_path(path), canonical_path(vault)
    if path.suffix.lower() != ".md" or not is_relative_to(path, vault):
        return []
    relpath = path.relative_to(vault).as_posix()
    if ignored(relpath, manifest):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [f"{relpath}: could not read valid UTF-8 after write"]
    warnings = validate_content(text, path, vault, manifest)
    problem = index_warning(relpath, vault, manifest)
    if problem:
        warnings.append(problem)
    return warnings


def load_validation_schema(vault: Path) -> dict[str, Any]:
    manifest, error = load_json_detailed(vault / "50_System/system-manifest.json")
    if error:
        raise ValueError(f"system-manifest.json: {error}")
    problems = validate_system_manifest_shape(manifest)
    if problems:
        raise ValueError("; ".join(problems))
    return manifest


def check_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Strict targeted Markdown validation; no writes")
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--path", action="append", required=True, help="Vault-relative or absolute Markdown path; repeatable")
    parser.add_argument("--content-file", type=Path, help="Check proposed UTF-8 content for one destination")
    args = parser.parse_args(argv)
    problems = []
    try:
        vault = args.vault.expanduser().resolve(strict=True)
        manifest = load_validation_schema(vault)
        if args.content_file and len(args.path) != 1:
            raise ValueError("--content-file requires exactly one --path")
        for raw in args.path:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = vault / path
            path = canonical_path(path)
            if not is_relative_to(path, vault) or path.suffix.lower() != ".md":
                raise ValueError("target must be a Markdown note inside the vault")
            if args.content_file:
                content = args.content_file.read_text(encoding="utf-8")
                problems.extend(validate_content(content, path, vault, manifest))
            else:
                if not path.is_file():
                    raise ValueError(f"missing target: {path}")
                problems.extend(validate_path(path, vault, manifest))
    except (OSError, ValueError, UnicodeError) as exc:
        problems.append(str(exc))
    print(json.dumps({"status": "invalid" if problems else "ok", "paths": args.path,
                      "problems": _unique(problems)}, ensure_ascii=False))
    return 1 if problems else 0


def _valid_string_array(value: Any, *, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and bool(item) for item in value)
    )


def validate_system_manifest_shape(manifest: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for key in ("system", "schema_version", "canonical_policy", "note_types", "path_rules"):
        if key not in manifest:
            warnings.append(f"50_System/system-manifest.json: missing key `{key}`")

    if "schema_version" in manifest and (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] not in {1, 2}
    ):
        warnings.append(
            "50_System/system-manifest.json: schema_version must be 1 or 2"
        )

    note_types = manifest.get("note_types")
    if not isinstance(note_types, dict):
        warnings.append("50_System/system-manifest.json: `note_types` must be an object")
    else:
        for name, schema in note_types.items():
            if not isinstance(name, str) or not isinstance(schema, dict):
                warnings.append(
                    "50_System/system-manifest.json: every note type must map to an object"
                )
                continue
            for field in (
                "required",
                "status_values",
                "nonempty",
                "date_fields",
                "accepted_types",
            ):
                if field in schema and not _valid_string_array(schema[field]):
                    warnings.append(
                        f"50_System/system-manifest.json: note_types.{name}.{field} "
                        "must be an array of non-empty strings"
                    )
            exact_type = schema.get("exact_type")
            if exact_type is not None and not (
                exact_type is True or isinstance(exact_type, str) and bool(exact_type)
            ):
                warnings.append(
                    f"50_System/system-manifest.json: note_types.{name}.exact_type "
                    "must be a non-empty string or true"
                )
            status_aliases = schema.get("status_aliases")
            if status_aliases is not None and not (
                isinstance(status_aliases, dict)
                and all(
                    isinstance(alias, str)
                    and bool(alias)
                    and isinstance(target, str)
                    and bool(target)
                    for alias, target in status_aliases.items()
                )
            ):
                warnings.append(
                    f"50_System/system-manifest.json: note_types.{name}.status_aliases "
                    "must be an object of non-empty alias-to-status strings"
                )
            if (
                isinstance(status_aliases, dict)
                and _valid_string_array(schema.get("status_values"))
            ):
                allowed = set(schema["status_values"])
                for alias, target in status_aliases.items():
                    if isinstance(alias, str) and isinstance(target, str) and target not in allowed:
                        warnings.append(
                            f"50_System/system-manifest.json: note_types.{name}.status_aliases "
                            f"target {target!r} is not in status_values"
                        )

    rules = manifest.get("path_rules")
    if not isinstance(rules, list):
        warnings.append("50_System/system-manifest.json: `path_rules` must be an array")
    else:
        for index, rule in enumerate(rules):
            prefix = f"50_System/system-manifest.json: path_rules[{index}]"
            if not isinstance(rule, dict):
                warnings.append(f"{prefix} must be an object")
                continue
            if not isinstance(rule.get("glob"), str) or not rule.get("glob"):
                warnings.append(f"{prefix}.glob must be a non-empty string")
            note_type = rule.get("note_type")
            if not isinstance(note_type, str) or not note_type:
                warnings.append(f"{prefix}.note_type must be a non-empty string")
            elif isinstance(note_types, dict) and note_type not in note_types:
                warnings.append(f"{prefix}.note_type references unknown type `{note_type}`")
            if "max_chars" in rule and (
                type(rule["max_chars"]) is not int or rule["max_chars"] <= 0
            ):
                warnings.append(f"{prefix}.max_chars must be a positive integer")

    for key in ("indexes", "validation"):
        if key in manifest and not isinstance(manifest[key], dict):
            warnings.append(f"50_System/system-manifest.json: `{key}` must be an object")
    if "infrastructure" in manifest and not _valid_string_array(
        manifest["infrastructure"]
    ):
        warnings.append(
            "50_System/system-manifest.json: `infrastructure` must be an array of non-empty strings"
        )
    validation = manifest.get("validation")
    if isinstance(validation, dict):
        for field in ("allowed_legacy_session_roots", "session_project_sentinels"):
            if field in validation and not _valid_string_array(validation[field]):
                warnings.append(
                    f"50_System/system-manifest.json: validation.{field} "
                    "must be an array of non-empty strings"
                )
    return warnings


def normalize_slug(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("_", "-")
    value = re.sub(r"[^\w]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def safe_slug(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value in {".", ".."} or Path(value).is_absolute() or Path(value).name != value:
        return None
    return value if normalize_slug(value) == value else None


def validate_registry_shape(registry: dict[str, Any], vault: Path) -> list[str]:
    warnings: list[str] = []
    raw_version = registry.get("schema_version", 1)
    if type(raw_version) is not int or raw_version not in {1, 2}:
        warnings.append("50_System/project-routing.json: schema_version must be 1 or 2")

    aliases = registry.get("workspace_aliases", {})
    if not isinstance(aliases, dict):
        warnings.append("50_System/project-routing.json: workspace_aliases must be an object")
        aliases = {}
    normalized_sources: dict[str, str] = {}
    normalized_targets: dict[str, str] = {}
    for alias, raw_target in aliases.items():
        if not isinstance(alias, str):
            warnings.append("50_System/project-routing.json: alias names must be strings")
            continue
        target = safe_slug(raw_target)
        if target is None:
            warnings.append(
                f"50_System/project-routing.json: alias {alias!r} has a non-canonical target slug"
            )
            continue
        normalized = normalize_slug(alias)
        previous = normalized_sources.get(normalized)
        if not normalized:
            warnings.append(
                f"50_System/project-routing.json: alias {alias!r} normalizes to empty"
            )
        elif previous is not None:
            warnings.append(
                f"50_System/project-routing.json: aliases {previous!r} and {alias!r} collide"
            )
        else:
            normalized_sources[normalized] = alias
            normalized_targets[normalized] = target
        if not (vault / "10_Projects" / target / "Memory_Manifest.md").is_file():
            warnings.append(
                f"50_System/project-routing.json: alias {alias!r} targets missing manifest {target!r}"
            )

    canonical_by_normalized: dict[str, list[str]] = {}
    projects_root = vault / "10_Projects"
    if projects_root.is_dir():
        for manifest_path in projects_root.glob("*/Memory_Manifest.md"):
            if manifest_path.is_file() and ".sync-conflict-" not in manifest_path.name:
                slug = manifest_path.parent.name
                canonical_by_normalized.setdefault(normalize_slug(slug), []).append(slug)
    for normalized, target in normalized_targets.items():
        canonical = canonical_by_normalized.get(normalized, [])
        if canonical and (len(canonical) != 1 or canonical[0] != target):
            warnings.append(
                "50_System/project-routing.json: alias "
                f"{normalized_sources[normalized]!r} conflicts with canonical project routing"
            )

    roots = registry.get("workspace_roots")
    if roots is not None:
        if not _valid_string_array(roots):
            warnings.append(
                "50_System/project-routing.json: workspace_roots must be an array of non-empty strings"
            )
        else:
            for root in roots:
                if not (
                    root == "~" or root.startswith("~/") or Path(root).is_absolute()
                ):
                    warnings.append(
                        "50_System/project-routing.json: workspace roots must be "
                        "absolute or use ~/"
                    )
    bindings = registry.get("workspace_bindings", {})
    if not isinstance(bindings, dict):
        warnings.append(
            "50_System/project-routing.json: workspace_bindings must be an object"
        )
    else:
        for path, raw_target in bindings.items():
            if not isinstance(path, str) or not path or not (
                path == "~" or path.startswith("~/") or Path(path).is_absolute()
            ):
                warnings.append(
                    "50_System/project-routing.json: binding paths must be absolute or use ~/"
                )
            target = safe_slug(raw_target)
            if target is None:
                warnings.append(
                    f"50_System/project-routing.json: binding {path!r} has a non-canonical target slug"
                )
            elif not (vault / "10_Projects" / target / "Memory_Manifest.md").is_file():
                warnings.append(
                    f"50_System/project-routing.json: binding {path!r} targets missing manifest {target!r}"
                )
    return warnings


def _label(path: Path, vault: Path) -> str:
    return path.relative_to(vault).as_posix() if is_relative_to(path, vault) else str(path)


def deletion_warnings(relpath: str, *, moved: bool = False) -> list[str]:
    verb = "moved" if moved else "deleted"
    parts = Path(relpath).parts
    warnings = [f"{relpath}: vault path was {verb}; verify inbound links and indexes"]
    if len(parts) == 3 and parts[0] == "10_Projects" and parts[2] == "Memory_Manifest.md":
        warnings.append(
            f"{relpath}: canonical manifest was {verb}; project memory routing is now changed"
        )
    elif relpath in {
        "50_System/system-manifest.json",
        "50_System/project-routing.json",
    }:
        warnings.append(f"{relpath}: critical control-plane file was {verb}")
    elif relpath.startswith("50_System/Indexes/"):
        warnings.append(f"{relpath}: routing index was {verb}")
    return warnings


def validate_control_plane_file(
    path: Path, relpath: str, vault: Path, manifest: dict[str, Any]
) -> list[str]:
    warnings = [
        f"{relpath}: control-plane path changed; run its targeted checks before completion"
    ]
    if relpath == "50_System/system-manifest.json":
        value, error = load_json_detailed(path)
        if error:
            warnings.append(f"{relpath}: {error}")
        else:
            assert value is not None
            warnings.extend(validate_system_manifest_shape(value))
    elif relpath == "50_System/project-routing.json":
        value, error = load_json_detailed(path)
        if error:
            warnings.append(f"{relpath}: {error}")
        else:
            assert value is not None
            warnings.extend(validate_registry_shape(value, vault))
    elif path.suffix.lower() == ".json":
        _, error = load_json_detailed(path)
        if error:
            warnings.append(f"{relpath}: {error}")
    return warnings


def validate_touch(
    touch: Touch, vault: Path, manifest: dict[str, Any]
) -> list[str]:
    vault = canonical_path(vault)
    path = canonical_path(touch.path)
    related = canonical_path(touch.related) if touch.related is not None else None
    path_inside = is_relative_to(path, vault)
    related_inside = related is not None and is_relative_to(related, vault)
    if not path_inside and not related_inside:
        return []

    warnings: list[str] = []
    if touch.action == "move_from" and related is not None:
        if path_inside and related_inside:
            warnings.append(
                f"{_label(path, vault)}: moved within vault to `{_label(related, vault)}`"
            )
        elif path_inside:
            warnings.append(
                f"{_label(path, vault)}: moved out of vault to `{related}`"
            )
        else:
            warnings.append(
                f"{_label(related, vault)}: moved into vault from `{path}`"
            )
        if path_inside:
            relpath = path.relative_to(vault).as_posix()
            if not ignored(relpath, manifest) or control_plane(relpath, manifest):
                warnings.extend(deletion_warnings(relpath, moved=True))
            if path.exists():
                warnings.append(f"{relpath}: move source still exists after the write tool")
        return warnings

    if not path_inside:
        return warnings
    relpath = path.relative_to(vault).as_posix()
    is_control = control_plane(relpath, manifest)
    if ignored(relpath, manifest) and not is_control:
        return warnings

    if touch.action == "delete":
        warnings.extend(deletion_warnings(relpath))
        if path.exists():
            warnings.append(f"{relpath}: delete was requested but the path still exists")
        return warnings

    if not path.exists():
        warnings.append(f"{relpath}: touched path is missing after the write tool")
        return warnings
    if touch.action == "move_to" and related is not None:
        warnings.append(f"{relpath}: move destination created from `{_label(related, vault)}`")

    if path.suffix.lower() == ".md":
        warnings.extend(validate_path(path, vault, manifest))
    if is_control:
        warnings.extend(validate_control_plane_file(path, relpath, vault, manifest))
    return warnings


def _unique(warnings: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(warnings))


def emit_warnings(warnings: Iterable[str]) -> int:
    items = _unique(warnings)
    if not items:
        return 0
    visible = items[:20]
    suffix = f"\n- {len(items) - 20} additional warning(s) omitted" if len(items) > 20 else ""
    message = (
        "Context Vault write validation (warning-only):\n- "
        + "\n- ".join(visible)
        + suffix
        + "\nThe validator never blocks the tool result; resolve relevant warnings before completion."
    )
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    return 0


def _main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        return emit_warnings([f"validator input is not valid JSON: {exc}"])
    if not isinstance(payload, dict):
        return emit_warnings(["validator input must be a JSON object"])

    cwd_raw = payload.get("cwd")
    try:
        cwd = canonical_path(Path(cwd_raw)) if isinstance(cwd_raw, str) else Path.cwd()
    except (OSError, ValueError) as exc:
        return emit_warnings([f"validator could not resolve payload cwd: {exc}"])

    touches = discover_touches(payload, cwd)
    if not touches:
        return emit_warnings(
            ["supported write hook payload contained no discoverable touched path"]
        )

    vault = canonical_path(
        Path(os.environ.get("CODEX_CONTEXT_VAULT", str(DEFAULT_VAULT)))
    )
    relevant = [
        touch
        for touch in touches
        if is_relative_to(touch.path, vault)
        or touch.related is not None
        and is_relative_to(touch.related, vault)
    ]
    if not relevant:
        return 0
    if not vault.exists():
        return emit_warnings([f"configured Context Vault is missing: {vault}"])
    if not vault.is_dir():
        return emit_warnings([f"configured Context Vault is not a directory: {vault}"])

    system_path = vault / "50_System" / "system-manifest.json"
    manifest, manifest_error = load_json_detailed(system_path)
    if manifest_error:
        warnings = [f"50_System/system-manifest.json: {manifest_error}"]
        manifest = {}
    else:
        assert manifest is not None
        warnings = validate_system_manifest_shape(manifest)

    registry_relative = manifest.get(
        "project_routing", "50_System/project-routing.json"
    )
    if not isinstance(registry_relative, str) or not registry_relative:
        warnings.append(
            "50_System/system-manifest.json: project_routing must be a non-empty string"
        )
    else:
        registry_path_value = vault / registry_relative
        registry, registry_error = load_json_detailed(registry_path_value)
        if registry_error:
            warnings.append(f"{registry_relative}: {registry_error}")
        else:
            assert registry is not None
            warnings.extend(validate_registry_shape(registry, vault))

    for touch in relevant:
        warnings.extend(validate_touch(touch, vault, manifest))
    return emit_warnings(warnings)


def main() -> int:
    try:
        return _main()
    except Exception as exc:  # PostToolUse must fail loud without blocking the write.
        return emit_warnings(
            [f"validator internal error: {type(exc).__name__}: {exc}"]
        )


if __name__ == "__main__":
    raise SystemExit(check_cli(sys.argv[1:]) if len(sys.argv) > 1 else main())
