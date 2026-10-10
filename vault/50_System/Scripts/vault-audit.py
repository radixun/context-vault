#!/usr/bin/env python3
"""Portable mechanical and ratchet audit for the Codex Context Vault."""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import posixpath
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_metadata import frontmatter_lines, frontmatter_syntax_problems, frontmatter


FALLBACK_REQUIRED_KEYS = ("type", "status", "created", "updated", "summary")
WIKILINK_RE = re.compile(r"!?\[\[([^\[\]\n]+?)\]\]")
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)([^`\n]+?)\1(?!`)")
EXCLUDED_PARTS = {".git", ".obsidian", ".stversions", ".trash", ".Trash"}
VALID_SEVERITIES = {"ERROR", "WARN", "INFO"}
REQUIRED_MANIFEST_KEYS = (
    "system",
    "schema_version",
    "canonical_policy",
    "project_routing",
    "note_types",
    "path_rules",
    "indexes",
    "validation",
)
ASSET_EXTENSIONS = {
    "base",
    "canvas",
    "excalidraw",
    "gif",
    "htm",
    "html",
    "jpeg",
    "jpg",
    "mov",
    "mp3",
    "mp4",
    "pdf",
    "png",
    "svg",
    "webp",
    "zip",
}


@dataclass
class Issue:
    severity: str
    check: str
    path: str
    message: str


def rel(vault: Path, path: Path) -> str:
    try:
        return path.relative_to(vault).as_posix()
    except ValueError:
        return path.as_posix()


def add_issue(
    issues: list[Issue], severity: str, check: str, path: str, message: str
) -> None:
    issues.append(Issue(severity, check, path, message))


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_excluded_path(vault: Path, path: Path) -> bool:
    """Return whether a path is outside the active synchronized-memory surface."""
    try:
        parts = path.relative_to(vault).parts
    except ValueError:
        return True
    return any(
        part in EXCLUDED_PARTS or ".sync-conflict-" in part for part in parts
    )


def safe_relative_config_path(value: Any) -> str | None:
    """Accept only portable vault-relative configuration paths."""
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        return None
    candidate = Path(value.strip())
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate.as_posix()


def nonempty_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return [item.strip() for item in value]


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def markdown_files(vault: Path) -> list[Path]:
    return sorted(
        path
        for path in vault.rglob("*.md")
        if not is_excluded_path(vault, path)
    )


def matches_any(relpath: str, patterns: Any) -> bool:
    values = nonempty_string_list(patterns)
    return bool(values) and any(fnmatch.fnmatchcase(relpath, pattern) for pattern in values)


def requires_frontmatter(
    vault: Path, path: Path, system_manifest: dict[str, Any]
) -> bool:
    relpath = rel(vault, path)
    validation = system_manifest.get("validation", {})
    ignored = validation.get("ignore_markdown", []) if isinstance(validation, dict) else []
    if matches_any(relpath, ignored):
        return False
    if path.name in {"AGENTS.md", "README.md", "Welcome.md"}:
        return False
    return not relpath.startswith(".obsidian/")


def matching_rule(relpath: str, system_manifest: dict[str, Any]) -> dict[str, Any] | None:
    rules = system_manifest.get("path_rules", [])
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if isinstance(rule, dict) and fnmatch.fnmatchcase(relpath, str(rule.get("glob", ""))):
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


def required_keys(rule: dict[str, Any] | None, system_manifest: dict[str, Any]) -> list[str]:
    note_types = system_manifest.get("note_types", {})
    if not isinstance(note_types, dict):
        return list(FALLBACK_REQUIRED_KEYS)
    common = note_types.get("common", {})
    common_required = (
        nonempty_string_list(common.get("required")) if isinstance(common, dict) else None
    )
    keys = common_required or list(FALLBACK_REQUIRED_KEYS)
    note_type = rule.get("note_type") if rule else None
    if note_type and note_type != "common":
        typed = note_types.get(note_type, {})
        if isinstance(typed, dict):
            typed_required = nonempty_string_list(typed.get("required"))
            if typed_required:
                keys.extend(typed_required)
    return list(dict.fromkeys(keys))


def oversized_class(
    vault: Path,
    path: Path,
    rule: dict[str, Any] | None,
) -> tuple[str, str]:
    if rule:
        severity = str(rule.get("size_severity", "INFO")).upper()
        if severity not in VALID_SEVERITIES:
            severity = "INFO"
        return (
            severity,
            "oversized-routing"
            if severity in {"ERROR", "WARN"}
            else "oversized-history",
        )
    relpath = rel(vault, path)
    parts = Path(relpath).parts
    if relpath.startswith(("40_Archive/", "60_Sessions/")):
        return "INFO", "oversized-history"
    if len(parts) >= 3 and parts[0] == "10_Projects":
        return "INFO", "oversized-project-detail"
    return "INFO", "oversized-reference"








def normalize_slug(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("_", "-")
    value = re.sub(r"[^\w]+", "-", value, flags=re.UNICODE)
    return re.sub(r"-+", "-", value).strip("-")


def scalar(value: str | None) -> str:
    return value.strip().strip("\"'") if value else ""


def strip_fenced_regions(content: str) -> str:
    output: list[str] = []
    in_fence = False
    marker = ""
    for line in content.splitlines():
        stripped = line.lstrip()
        if in_fence:
            if stripped.startswith(marker):
                in_fence = False
            continue
        if stripped.startswith(("```", "~~~")):
            in_fence = True
            marker = stripped[:3]
            continue
        output.append(line)
    return "\n".join(output)


def strip_code_regions(content: str) -> str:
    return re.sub(r"(`+)(?:(?!\1).)*?\1", "", strip_fenced_regions(content))


def wikilink_targets(text: str) -> list[str]:
    targets: list[str] = []
    for inner in WIKILINK_RE.findall(strip_code_regions(text)):
        normalized = inner.replace("\\|", "|").replace("&#124;", "|")
        target = normalized.split("|", 1)[0].split("#", 1)[0].strip()
        if not target or "{{" in target:
            continue
        extension = target.rsplit(".", 1)[-1].lower() if "." in target else ""
        if extension in ASSET_EXTENSIONS:
            continue
        targets.append(target)
    return targets


def normalized_reference(value: str) -> str:
    clean = value.replace("\\", "/").strip().lstrip("/")
    if clean.lower().endswith(".md"):
        clean = clean[:-3]
    while clean.startswith("./"):
        clean = clean[2:]
    return posixpath.normpath(clean) if clean else ""


def index_references(text: str) -> set[str]:
    """Extract exact note/root routes from wikilinks and inline-code paths."""
    references: set[str] = set()
    for target in wikilink_targets(text):
        candidate = normalized_reference(target)
        if candidate:
            references.add(candidate)
    for match in INLINE_CODE_RE.finditer(strip_fenced_regions(text)):
        candidate = normalized_reference(match.group(2))
        if candidate:
            references.add(candidate)
    return references


def generated_marker_text(text: str, marker: str) -> str | None:
    """Return one well-formed generated block, or None when it is unavailable.

    Generated indexes may contain manual policy examples that resemble routes.
    Membership checks must use the authoritative generated rows when markers
    exist, while old unmarked indexes retain their backward-compatible scan.
    Malformed/duplicate markers fail closed through an explicit audit issue.
    """

    begin = f"<!-- BEGIN GENERATED:{marker} -->"
    end = f"<!-- END GENERATED:{marker} -->"
    if text.count(begin) != 1 or text.count(end) != 1:
        return None
    start = text.find(begin) + len(begin)
    stop = text.find(end, start)
    if stop < start:
        return None
    return text[start:stop]


def generated_index_references(
    text: str,
    markers: tuple[str, ...],
) -> tuple[set[str], str | None]:
    """Extract references from generated blocks, falling back only if absent."""

    marker_token_present = "<!-- BEGIN GENERATED:" in text or "<!-- END GENERATED:" in text
    blocks: list[str] = []
    for marker in markers:
        block = generated_marker_text(text, marker)
        if block is None:
            if marker_token_present:
                return set(), f"missing, duplicate, or malformed generated marker {marker!r}"
            return index_references(text), None
        blocks.append(block)
    return index_references("\n".join(blocks)), None


def index_has_reference(text: str, expected: str) -> bool:
    return normalized_reference(expected) in index_references(text)


def frontmatter_aliases(text: str) -> list[str]:
    lines = frontmatter_lines(text)
    if lines is None:
        return []
    for index, line in enumerate(lines):
        if not line.startswith("aliases:"):
            continue
        tail = line.split(":", 1)[1].strip()
        if tail.startswith("[") and tail.endswith("]"):
            return [
                value.strip().strip("\"'")
                for value in tail[1:-1].split(",")
                if value.strip()
            ]
        aliases: list[str] = []
        for child in lines[index + 1 :]:
            match = re.match(r"^\s+-\s+(.+)$", child)
            if not match:
                break
            aliases.append(match.group(1).strip().strip("\"'"))
        return aliases
    return []


def note_type_config(
    system_manifest: dict[str, Any], note_type: str
) -> dict[str, Any]:
    note_types = system_manifest.get("note_types")
    if not isinstance(note_types, dict):
        return {}
    value = note_types.get(note_type)
    return value if isinstance(value, dict) else {}


def configured_fields(config: dict[str, Any], key: str) -> list[str]:
    return nonempty_string_list(config.get(key)) or []


def frontmatter_schema_findings(
    fm: dict[str, str],
    rule: dict[str, Any] | None,
    system_manifest: dict[str, Any],
    *,
    relpath: str | None = None,
) -> list[tuple[str, str, str]]:
    """Apply optional, backward-compatible note-type value constraints."""
    common = note_type_config(system_manifest, "common")
    note_type = rule.get("note_type") if isinstance(rule, dict) else None
    typed = note_type_config(system_manifest, note_type) if isinstance(note_type, str) else {}
    findings: list[tuple[str, str, str]] = []

    expected_type = typed.get("exact_type")
    actual_type = scalar(fm.get("type"))
    accepted_types = nonempty_string_list(typed.get("accepted_types"))
    if accepted_types:
        if actual_type not in accepted_types:
            findings.append(
                (
                    "ERROR",
                    "frontmatter-schema",
                    f"type must be one of {', '.join(accepted_types)}, got {actual_type!r}",
                )
            )
        elif (
            isinstance(expected_type, str)
            and expected_type
            and actual_type != expected_type
        ):
            findings.append(
                (
                    "INFO",
                    "legacy-type",
                    f"legacy type {actual_type!r} is accepted; canonical type is {expected_type!r}",
                )
            )
    elif isinstance(expected_type, str) and expected_type:
        if actual_type != expected_type:
            findings.append(
                (
                    "ERROR",
                    "frontmatter-schema",
                    f"type must be {expected_type!r}, got {actual_type!r}",
                )
            )

    status_values = nonempty_string_list(typed.get("status_values"))
    if status_values is None:
        status_values = nonempty_string_list(common.get("status_values"))
    common_aliases = common.get("status_aliases")
    typed_aliases = typed.get("status_aliases")
    status_aliases: dict[str, str] = {}
    if isinstance(common_aliases, dict):
        status_aliases.update(
            (key, value)
            for key, value in common_aliases.items()
            if isinstance(key, str) and isinstance(value, str)
        )
    if isinstance(typed_aliases, dict):
        status_aliases.update(
            (key, value)
            for key, value in typed_aliases.items()
            if isinstance(key, str) and isinstance(value, str)
        )
    actual_status = scalar(fm.get("status"))
    canonical_status = status_aliases.get(actual_status, actual_status)
    if canonical_status != actual_status:
        findings.append(
            (
                "INFO",
                "legacy-type",
                f"legacy status {actual_status!r} is accepted as {canonical_status!r}",
            )
        )
    if status_values:
        if canonical_status not in status_values:
            findings.append(
                (
                    "ERROR",
                    "frontmatter-schema",
                    f"status must be one of {', '.join(status_values)}, got {actual_status!r}",
                )
            )

    nonempty = list(
        dict.fromkeys(
            configured_fields(common, "nonempty")
            + configured_fields(typed, "nonempty")
        )
    )
    for field in nonempty:
        if not scalar(fm.get(field)):
            findings.append(
                ("ERROR", "frontmatter-schema", f"{field} must be non-empty")
            )

    date_fields = list(
        dict.fromkeys(
            configured_fields(common, "date_fields")
            + configured_fields(typed, "date_fields")
        )
    )
    for field in date_fields:
        if field not in fm:
            continue
        value = scalar(fm.get(field))
        if (
            value == "YYYY-MM-DD"
            and relpath is not None
            and relpath.startswith("50_System/Templates/")
        ):
            continue
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            dt.date.fromisoformat(value)
        except ValueError:
            findings.append(
                (
                    "ERROR",
                    "frontmatter-schema",
                    f"{field} must be an ISO date (YYYY-MM-DD)",
                )
            )
    return findings


def frontmatter_schema_problems(
    fm: dict[str, str],
    rule: dict[str, Any] | None,
    system_manifest: dict[str, Any],
    *,
    relpath: str | None = None,
) -> list[str]:
    """Backward-compatible view of only ratchet-relevant schema errors."""
    return [
        message
        for severity, _, message in frontmatter_schema_findings(
            fm, rule, system_manifest, relpath=relpath
        )
        if severity == "ERROR"
    ]


def wikilink_exists(
    vault: Path,
    source: Path,
    target: str,
    paths: set[str],
    leaves: set[str],
    aliases: set[str],
) -> bool:
    if target.strip().casefold().startswith(("http://", "https://")):
        return True
    clean = normalized_reference(target)
    lowered = clean.casefold()
    path_qualified = "/" in clean
    if not path_qualified and (lowered in aliases or Path(lowered).name in leaves):
        return True
    source_dir = Path(rel(vault, source)).parent
    candidates = (clean, posixpath.join(source_dir.as_posix(), clean))
    for candidate in candidates:
        normalized = posixpath.normpath(candidate).casefold()
        if normalized == ".." or normalized.startswith("../"):
            continue
        if normalized in paths:
            return True
    return False


def audit_system_manifest(
    vault: Path, system_manifest: dict[str, Any], issues: list[Issue]
) -> None:
    path = vault / "50_System/system-manifest.json"
    if not system_manifest:
        add_issue(
            issues,
            "ERROR",
            "system-manifest",
            rel(vault, path),
            "missing or invalid JSON system manifest",
        )
        return
    for key in REQUIRED_MANIFEST_KEYS:
        if key not in system_manifest:
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"missing key: {key}",
            )

    if system_manifest.get("system") != "context-vault":
        add_issue(
            issues,
            "ERROR",
            "system-manifest",
            rel(vault, path),
            "system must be 'context-vault'",
        )
    schema_version = system_manifest.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        add_issue(
            issues,
            "ERROR",
            "system-manifest",
            rel(vault, path),
            "schema_version must be 1 or 2",
        )

    for key in ("canonical_policy", "project_routing"):
        configured = safe_relative_config_path(system_manifest.get(key))
        if configured is None:
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"{key} must be a safe vault-relative path",
            )
        elif key == "canonical_policy" and not (vault / configured).is_file():
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"canonical_policy does not exist: {configured}",
            )

    note_types = system_manifest.get("note_types")
    if not isinstance(note_types, dict) or not note_types:
        add_issue(
            issues,
            "ERROR",
            "system-manifest",
            rel(vault, path),
            "note_types must be a non-empty object",
        )
        note_types = {}
    for name, config in note_types.items():
        if not isinstance(name, str) or not name or not isinstance(config, dict):
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                "each note type must have a non-empty name and object definition",
            )
            continue
        required = nonempty_string_list(config.get("required"))
        if required is None:
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"note_types.{name}.required must be an array of non-empty strings",
            )
        exact_type = config.get("exact_type")
        if exact_type is not None and (
            not isinstance(exact_type, str) or not exact_type.strip()
        ):
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"note_types.{name}.exact_type must be a non-empty string",
            )
        for optional in (
            "accepted_types",
            "status_values",
            "nonempty",
            "date_fields",
        ):
            if optional in config and nonempty_string_list(config.get(optional)) is None:
                add_issue(
                    issues,
                    "ERROR",
                    "system-manifest",
                    rel(vault, path),
                    f"note_types.{name}.{optional} must be an array of non-empty strings",
                )
        accepted_types = nonempty_string_list(config.get("accepted_types"))
        if accepted_types and not (
            isinstance(exact_type, str) and exact_type.strip()
        ):
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"note_types.{name}.accepted_types requires exact_type",
            )
        if (
            isinstance(exact_type, str)
            and exact_type.strip()
            and accepted_types
            and exact_type not in accepted_types
        ):
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"note_types.{name}.accepted_types must include exact_type",
            )
        status_aliases = config.get("status_aliases")
        if status_aliases is not None and (
            not isinstance(status_aliases, dict)
            or not all(
                isinstance(alias, str)
                and alias.strip()
                and isinstance(canonical, str)
                and canonical.strip()
                for alias, canonical in (
                    status_aliases.items()
                    if isinstance(status_aliases, dict)
                    else []
                )
            )
        ):
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"note_types.{name}.status_aliases must map non-empty strings to non-empty strings",
            )
        if isinstance(status_aliases, dict):
            local_statuses = nonempty_string_list(config.get("status_values"))
            common_config = note_types.get("common", {})
            common_statuses = (
                nonempty_string_list(common_config.get("status_values"))
                if isinstance(common_config, dict)
                else None
            )
            accepted_statuses = local_statuses or common_statuses
            if accepted_statuses:
                for canonical in status_aliases.values():
                    if isinstance(canonical, str) and canonical not in accepted_statuses:
                        add_issue(
                            issues,
                            "ERROR",
                            "system-manifest",
                            rel(vault, path),
                            f"note_types.{name}.status_aliases target {canonical!r} "
                            "is not present in status_values",
                        )

    rules = system_manifest.get("path_rules")
    if not isinstance(rules, list):
        add_issue(
            issues,
            "ERROR",
            "system-manifest",
            rel(vault, path),
            "path_rules must be an array",
        )
        rules = []
    for index, rule in enumerate(rules):
        prefix = f"path_rules[{index}]"
        if not isinstance(rule, dict):
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"{prefix} must be an object",
            )
            continue
        glob = rule.get("glob")
        if (
            not isinstance(glob, str)
            or not glob.strip()
            or "\\" in glob
            or Path(glob).is_absolute()
            or ".." in Path(glob).parts
        ):
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"{prefix}.glob must be a non-empty vault-relative glob",
            )
        configured_type = rule.get("note_type")
        if not isinstance(configured_type, str) or configured_type not in note_types:
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"{prefix}.note_type must name a configured note type",
            )
        if "max_chars" in rule and not is_positive_int(rule.get("max_chars")):
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"{prefix}.max_chars must be a positive integer",
            )
        severity = rule.get("size_severity", "INFO")
        if not isinstance(severity, str) or severity.upper() not in VALID_SEVERITIES:
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                f"{prefix}.size_severity must be ERROR, WARN, or INFO",
            )

    indexes = system_manifest.get("indexes")
    if not isinstance(indexes, dict):
        add_issue(
            issues,
            "ERROR",
            "system-manifest",
            rel(vault, path),
            "indexes must be an object",
        )
    else:
        for kind in ("project", "decision", "session", "global", "master", "generator"):
            if kind not in indexes and kind not in {"project", "decision", "session"}:
                continue
            configured = safe_relative_config_path(indexes.get(kind))
            if configured is None:
                add_issue(
                    issues,
                    "ERROR",
                    "system-manifest",
                    rel(vault, path),
                    f"indexes.{kind} must be a safe vault-relative path",
                )
            elif not (vault / configured).is_file():
                add_issue(
                    issues,
                    "ERROR",
                    "system-manifest",
                    rel(vault, path),
                    f"indexes.{kind} does not exist: {configured}",
                )

    validation = system_manifest.get("validation")
    if not isinstance(validation, dict):
        add_issue(
            issues,
            "ERROR",
            "system-manifest",
            rel(vault, path),
            "validation must be an object",
        )
    else:
        if nonempty_string_list(validation.get("ignore_markdown")) is None:
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                "validation.ignore_markdown must be an array of non-empty strings",
            )
        for optional in (
            "allowed_legacy_session_roots",
            "session_project_sentinels",
        ):
            if optional in validation and nonempty_string_list(
                validation.get(optional)
            ) is None:
                add_issue(
                    issues,
                    "ERROR",
                    "system-manifest",
                    rel(vault, path),
                    f"validation.{optional} must be an array of non-empty strings",
                )
        sentinels = nonempty_string_list(
            validation.get("session_project_sentinels")
        )
        if sentinels is not None and set(sentinels) != {"_general", "_unassigned"}:
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                "validation.session_project_sentinels must contain exactly "
                "'_general' and '_unassigned'",
            )
        if "baseline" in validation and safe_relative_config_path(
            validation.get("baseline")
        ) is None:
            add_issue(
                issues,
                "ERROR",
                "system-manifest",
                rel(vault, path),
                "validation.baseline must be a safe vault-relative path",
            )


def audit_project_routing(
    vault: Path, system_manifest: dict[str, Any], issues: list[Issue]
) -> None:
    configured = system_manifest.get(
        "project_routing", "50_System/project-routing.json"
    )
    relative = safe_relative_config_path(configured)
    if relative is None:
        add_issue(
            issues,
            "ERROR",
            "project-routing",
            "50_System/system-manifest.json",
            "project_routing must be a safe vault-relative path",
        )
        return
    path = vault / relative
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        add_issue(
            issues,
            "ERROR",
            "project-routing",
            rel(vault, path),
            f"missing or invalid JSON registry: {exc}",
        )
        return
    if not isinstance(registry, dict):
        add_issue(
            issues,
            "ERROR",
            "project-routing",
            rel(vault, path),
            "registry root must be an object",
        )
        return
    schema_version = registry.get("schema_version", 1)
    if type(schema_version) is not int or schema_version not in {1, 2}:
        add_issue(
            issues,
            "ERROR",
            "project-routing",
            rel(vault, path),
            "schema_version must be 1 or 2",
        )
    raw_aliases = registry.get("workspace_aliases", {})
    if not isinstance(raw_aliases, dict):
        add_issue(
            issues,
            "ERROR",
            "project-routing",
            rel(vault, path),
            "workspace_aliases must be an object",
        )
        return

    projects_root = vault / "10_Projects"
    manifests = {
        candidate.parent.name: candidate
        for candidate in projects_root.glob("*/Memory_Manifest.md")
        if not is_excluded_path(vault, candidate)
    }
    normalized_projects: dict[str, list[str]] = {}
    for slug in manifests:
        normalized_projects.setdefault(normalize_slug(slug), []).append(slug)
    for normalized, slugs in sorted(normalized_projects.items()):
        if not normalized or len(slugs) > 1:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                f"canonical projects {sorted(slugs)!r} collide as {normalized!r}",
            )

    normalized_sources: dict[str, str] = {}
    for alias, target in raw_aliases.items():
        if not isinstance(alias, str) or not isinstance(target, str) or not target:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                "aliases and target slugs must be non-empty strings",
            )
            continue
        normalized = normalize_slug(alias)
        previous = normalized_sources.get(normalized)
        if not normalized:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                f"alias {alias!r} normalizes to an empty value",
            )
        elif previous is not None:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                f"aliases {previous!r} and {alias!r} normalize to {normalized!r}",
            )
        else:
            normalized_sources[normalized] = alias

        safe_target = target if normalize_slug(target) == target else None
        if safe_target is None:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                f"alias {alias!r} target must be one canonical project slug",
            )
            continue
        if safe_target not in manifests:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                f"alias {alias!r} targets missing manifest {target!r}",
            )
        canonical_matches = normalized_projects.get(normalized, [])
        if canonical_matches and (
            len(canonical_matches) != 1 or canonical_matches[0] != safe_target
        ):
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                f"alias {alias!r} is shadowed by canonical project "
                f"{canonical_matches[0]!r} before it can target {safe_target!r}",
            )

    raw_roots = registry.get("workspace_roots")
    if raw_roots is not None:
        roots = nonempty_string_list(raw_roots)
        if roots is None:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                "workspace_roots must be an array of non-empty strings",
            )
        else:
            for root in roots:
                if not (root == "~" or root.startswith("~/") or Path(root).is_absolute()):
                    add_issue(
                        issues,
                        "ERROR",
                        "project-routing",
                        rel(vault, path),
                        f"workspace root {root!r} must be absolute or use ~/",
                    )

    raw_bindings = registry.get("workspace_bindings", {})
    if not isinstance(raw_bindings, dict):
        add_issue(
            issues,
            "ERROR",
            "project-routing",
            rel(vault, path),
            "workspace_bindings must be an object",
        )
        return
    expanded_bindings: dict[str, tuple[str, str]] = {}
    for raw_workspace, target in raw_bindings.items():
        valid_workspace = (
            isinstance(raw_workspace, str)
            and bool(raw_workspace)
            and (
                raw_workspace == "~"
                or raw_workspace.startswith("~/")
                or Path(raw_workspace).is_absolute()
            )
        )
        if not valid_workspace:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                "workspace binding paths must be absolute or use ~/",
            )
        if (
            not isinstance(target, str)
            or not target
            or normalize_slug(target) != target
        ):
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                f"workspace binding {raw_workspace!r} has a non-canonical target slug",
            )
            continue
        if target not in manifests:
            add_issue(
                issues,
                "ERROR",
                "project-routing",
                rel(vault, path),
                f"workspace binding {raw_workspace!r} targets missing manifest {target!r}",
            )
        if valid_workspace:
            assert isinstance(raw_workspace, str)
            expanded = str(
                (Path.home() if raw_workspace == "~" else
                 Path.home() / raw_workspace[2:] if raw_workspace.startswith("~/") else
                 Path(raw_workspace)).resolve(strict=False)
            )
            previous = expanded_bindings.get(expanded)
            if previous is not None and previous[1] != target:
                add_issue(
                    issues,
                    "ERROR",
                    "project-routing",
                    rel(vault, path),
                    f"workspace bindings {previous[0]!r} and {raw_workspace!r} "
                    "resolve to the same path with different targets",
                )
            else:
                expanded_bindings[expanded] = (raw_workspace, target)


def audit_canonical_project_routing(
    vault: Path,
    issues: list[Issue],
    system_manifest: dict[str, Any] | None = None,
) -> None:
    if system_manifest is None:
        system_manifest = load_json(vault / "50_System/system-manifest.json")
    projects_root = vault / "10_Projects"
    project_dirs = sorted(
        path
        for path in projects_root.iterdir()
        if path.is_dir() and not is_excluded_path(vault, path)
    ) if projects_root.is_dir() else []
    manifests: list[Path] = []
    for project_dir in project_dirs:
        manifest = project_dir / "Memory_Manifest.md"
        if not manifest.is_file():
            add_issue(
                issues,
                "ERROR",
                "project-manifest",
                rel(vault, project_dir),
                "project directory is missing Memory_Manifest.md",
            )
            continue
        manifests.append(manifest)
    project_slugs = {path.parent.name for path in manifests}

    normalized_projects: dict[str, list[str]] = {}
    for slug in project_slugs:
        normalized_projects.setdefault(normalize_slug(slug), []).append(slug)
    for normalized, slugs in sorted(normalized_projects.items()):
        if not normalized:
            add_issue(
                issues,
                "ERROR",
                "project-slug",
                "10_Projects",
                f"canonical folder {slugs[0]!r} normalizes to an empty slug",
            )
        elif len(slugs) > 1:
            add_issue(
                issues,
                "ERROR",
                "project-slug",
                "10_Projects",
                f"canonical folders {sorted(slugs)!r} normalize to {normalized!r}",
            )
        elif slugs[0] != normalized:
            add_issue(
                issues,
                "ERROR",
                "project-slug",
                "10_Projects",
                f"canonical folder {slugs[0]!r} must use normalized slug {normalized!r}",
            )

    for path in manifests:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            add_issue(
                issues,
                "ERROR",
                "file-read",
                rel(vault, path),
                f"could not read manifest: {exc}",
            )
            continue
        fm = frontmatter(text)
        if fm is None:
            continue
        declared = scalar(fm.get("project"))
        if not declared:
            add_issue(
                issues,
                "ERROR",
                "project-slug",
                rel(vault, path),
                "frontmatter project must be non-empty",
            )
        elif declared != path.parent.name:
            add_issue(
                issues,
                "ERROR",
                "project-slug",
                rel(vault, path),
                f"frontmatter project {declared!r} does not match folder {path.parent.name!r}",
            )

    session_root = vault / "60_Sessions"
    if not session_root.exists():
        return
    validation = system_manifest.get("validation", {})
    if not isinstance(validation, dict):
        validation = {}
    legacy_roots = set(
        nonempty_string_list(validation.get("allowed_legacy_session_roots")) or []
    )
    sentinels = set(
        nonempty_string_list(validation.get("session_project_sentinels"))
        or ["_general", "_unassigned"]
    )
    for path in sorted(session_root.glob("*/*.md")):
        if is_excluded_path(vault, path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            add_issue(
                issues,
                "ERROR",
                "file-read",
                rel(vault, path),
                f"could not read session: {exc}",
            )
            continue
        fm = frontmatter(text)
        if fm is None:
            continue
        declared = scalar(fm.get("project"))
        root_name = path.parent.name
        problem: str | None = None
        if root_name == "_general":
            if "_general" not in sentinels or declared != "_general":
                problem = "_general sessions must declare project '_general'"
        elif root_name in project_slugs:
            if declared != root_name:
                problem = f"project must be {root_name!r} for this canonical session root"
        elif root_name not in legacy_roots:
            problem = "unknown session root is not allowlisted as reviewed legacy history"
        elif declared not in {root_name, "_unassigned"} or "_unassigned" not in sentinels:
            problem = (
                "allowlisted legacy sessions must declare '_unassigned' "
                "or their exact session-root name"
            )
        if problem:
            add_issue(
                issues,
                "ERROR",
                "session-root",
                rel(vault, path),
                problem,
            )


def audit_markdown(
    vault: Path,
    files: list[Path],
    default_oversized_chars: int,
    system_manifest: dict[str, Any],
    issues: list[Issue],
) -> None:
    paths: set[str] = set()
    leaves: set[str] = set()
    aliases: set[str] = set()
    contents: dict[Path, str] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            add_issue(
                issues,
                "ERROR",
                "file-read",
                rel(vault, path),
                f"could not read Markdown: {exc}",
            )
            continue
        contents[path] = text
        relative = rel(vault, path).removesuffix(".md").casefold()
        paths.add(relative)
        leaves.add(path.stem.casefold())
        aliases.update(alias.casefold() for alias in frontmatter_aliases(text))

    for path, text in contents.items():
        try:
            data = path.read_bytes()
        except OSError as exc:
            add_issue(
                issues,
                "ERROR",
                "file-read",
                rel(vault, path),
                f"could not reread Markdown: {exc}",
            )
            continue
        relpath = rel(vault, path)
        rule = matching_rule(relpath, system_manifest)

        if data.startswith(b"\xef\xbb\xbf"):
            add_issue(issues, "ERROR", "bom", relpath, "UTF-8 BOM is present")

        if requires_frontmatter(vault, path, system_manifest):
            fm = frontmatter(text)
            if fm is None:
                add_issue(
                    issues, "ERROR", "frontmatter", relpath, "missing YAML frontmatter"
                )
            else:
                for problem in frontmatter_syntax_problems(text):
                    add_issue(
                        issues,
                        "ERROR",
                        "frontmatter-syntax",
                        relpath,
                        problem,
                    )
                missing = [
                    key for key in required_keys(rule, system_manifest) if key not in fm
                ]
                if missing:
                    add_issue(
                        issues,
                        "ERROR",
                        "frontmatter",
                        relpath,
                        f"missing keys: {', '.join(missing)}",
                    )
                for severity, check, message in frontmatter_schema_findings(
                    fm, rule, system_manifest, relpath=relpath
                ):
                    add_issue(
                        issues,
                        severity,
                        check,
                        relpath,
                        message,
                    )

        limit = (
            int(rule["max_chars"])
            if rule and is_positive_int(rule.get("max_chars"))
            else default_oversized_chars
        )
        if len(text) > limit:
            severity, check = oversized_class(vault, path, rule)
            add_issue(
                issues, severity, check, relpath, f"{len(text)} chars exceeds {limit}"
            )

        for target in wikilink_targets(text):
            if not wikilink_exists(vault, path, target, paths, leaves, aliases):
                add_issue(
                    issues,
                    "ERROR",
                    "wikilink",
                    relpath,
                    f"unresolved link: [[{target}]]",
                )


def audit_indexes(
    vault: Path, system_manifest: dict[str, Any], issues: list[Issue]
) -> None:
    indexes = system_manifest.get("indexes", {})
    if not isinstance(indexes, dict):
        indexes = {}
    defaults = {
        "project": "50_System/Indexes/Project_Index.md",
        "decision": "50_System/Indexes/Decision_Index.md",
        "session": "50_System/Indexes/Session_Index.md",
    }

    def configured_index(kind: str) -> Path:
        relative = safe_relative_config_path(indexes.get(kind)) or defaults[kind]
        return vault / relative

    def read_index(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            add_issue(
                issues,
                "ERROR",
                "index-read",
                rel(vault, path),
                f"index is missing or unreadable: {exc}",
            )
            return ""

    project_index = configured_index("project")
    decision_index = configured_index("decision")
    session_index = configured_index("session")

    project_text = read_index(project_index)
    project_references, project_marker_error = generated_index_references(
        project_text, ("project-active-projects", "project-paused-projects")
    )
    if project_marker_error:
        add_issue(
            issues,
            "ERROR",
            "project-index",
            rel(vault, project_index),
            project_marker_error,
        )
    expected_project_references: set[str] = set()
    for manifest in sorted((vault / "10_Projects").glob("*/Memory_Manifest.md")):
        if is_excluded_path(vault, manifest):
            continue
        slug = manifest.parent.name
        expected = f"10_Projects/{slug}/Memory_Manifest"
        expected_project_references.add(normalized_reference(expected))
        if normalized_reference(expected) not in project_references:
            add_issue(
                issues,
                "ERROR",
                "project-index",
                rel(vault, manifest),
                "project missing from Project_Index",
            )
    indexed_project_references = {
        reference
        for reference in project_references
        if reference.startswith("10_Projects/")
        and reference.endswith("/Memory_Manifest")
    }
    for stale in sorted(indexed_project_references - expected_project_references):
        add_issue(
            issues,
            "ERROR",
            "project-index",
            rel(vault, project_index),
            f"stale or noncanonical project route in index: {stale}",
        )

    decision_text = read_index(decision_index)
    decision_references, decision_marker_error = generated_index_references(
        decision_text,
        (
            "decision-active-accepted",
            "decision-done",
            "decision-superseded-archived",
        ),
    )
    if decision_marker_error:
        add_issue(
            issues,
            "ERROR",
            "decision-index",
            rel(vault, decision_index),
            decision_marker_error,
        )
    expected_decision_references: set[str] = set()
    for decision in sorted((vault / "70_Decisions").glob("*.md")):
        if decision.name == "README.md" or is_excluded_path(vault, decision):
            continue
        expected = f"70_Decisions/{decision.stem}"
        expected_decision_references.add(normalized_reference(expected))
        if normalized_reference(expected) not in decision_references:
            add_issue(
                issues,
                "ERROR",
                "decision-index",
                rel(vault, decision),
                "decision missing from Decision_Index",
            )
    indexed_decision_references = {
        reference
        for reference in decision_references
        if reference.startswith("70_Decisions/") and reference.count("/") == 1
    }
    for stale in sorted(indexed_decision_references - expected_decision_references):
        add_issue(
            issues,
            "ERROR",
            "decision-index",
            rel(vault, decision_index),
            f"stale or noncanonical decision route in index: {stale}",
        )

    session_text = read_index(session_index)
    session_references, session_marker_error = generated_index_references(
        session_text, ("session-canonical-roots", "session-unassigned-roots")
    )
    if session_marker_error:
        add_issue(
            issues,
            "ERROR",
            "session-index",
            rel(vault, session_index),
            session_marker_error,
        )
    session_root = vault / "60_Sessions"
    expected_session_references: set[str] = set()
    if session_root.exists():
        for child in sorted(
            path
            for path in session_root.iterdir()
            if path.is_dir() and not is_excluded_path(vault, path)
        ):
            expected = normalized_reference(f"60_Sessions/{child.name}")
            expected_session_references.add(expected)
            if expected not in session_references:
                add_issue(
                    issues,
                    "ERROR",
                    "session-index",
                    rel(vault, child),
                    "session root missing from Session_Index",
                )
        indexed_session_references = {
            reference
            for reference in session_references
            if reference.startswith("60_Sessions/") and reference.count("/") == 1
        }
        for stale in sorted(indexed_session_references - expected_session_references):
            add_issue(
                issues,
                "ERROR",
                "session-index",
                rel(vault, session_index),
                f"stale or noncanonical session-root route in index: {stale}",
            )
        for root_log in sorted(
            path for path in session_root.glob("*.md")
            if path.name != "README.md" and not is_excluded_path(vault, path)
        ):
            add_issue(
                issues,
                "ERROR",
                "root-session-log",
                rel(vault, root_log),
                "daily log is directly under 60_Sessions",
            )


def audit_session_filenames(vault: Path, issues: list[Issue]) -> None:
    """Validate host-aware daily logs without rejecting dated handoff notes."""

    root = vault / "60_Sessions"
    for path in sorted(root.glob("*/*.md")):
        if is_excluded_path(vault, path):
            continue
        name = path.name
        match = re.match(r"^(\d{4}-\d{2}-\d{2})(.*)\.md$", name)
        problem: str | None = None
        if match is None:
            problem = "session filename must begin with an ISO date"
        else:
            try:
                dt.date.fromisoformat(match.group(1))
            except ValueError:
                problem = "session filename contains an invalid ISO date"
            suffix = match.group(2)
            if problem is None and suffix.startswith("--") and suffix not in {
                "--linux",
                "--local",
                "--windows",
            }:
                problem = "host daily suffix must be linux, local or windows"
            elif problem is None and suffix and not suffix.startswith(("-", "--")):
                problem = "unexpected text after session date"
        if problem:
            add_issue(
                issues,
                "ERROR",
                "session-filename",
                rel(vault, path),
                problem,
            )


def audit_telemetry(vault: Path, issues: list[Issue]) -> None:
    root = vault / "50_System/Telemetry"
    for path in sorted(
        candidate
        for candidate in root.glob("codex-memory-events*.jsonl")
        if not is_excluded_path(vault, candidate)
    ):
        invalid = 0
        missing_event = 0
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            add_issue(
                issues,
                "ERROR",
                "telemetry-json",
                rel(vault, path),
                f"could not read telemetry ledger: {exc}",
            )
            continue
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                add_issue(
                    issues,
                    "ERROR",
                    "telemetry-json",
                    f"{rel(vault, path)}:{line_no}",
                    "invalid JSONL event",
                )
                continue
            if not isinstance(event, dict):
                invalid += 1
                add_issue(
                    issues,
                    "ERROR",
                    "telemetry-schema",
                    f"{rel(vault, path)}:{line_no}",
                    "JSONL event must be an object",
                )
                continue
            if "event" not in event and "kind" not in event:
                missing_event += 1
        if missing_event:
            add_issue(
                issues,
                "WARN",
                "telemetry-schema",
                rel(vault, path),
                f"{missing_event} events have neither event nor kind",
            )
        if invalid == 0:
            add_issue(
                issues, "INFO", "telemetry-json", rel(vault, path), "JSONL parse ok"
            )


def audit_ds_store(vault: Path, issues: list[Issue]) -> None:
    for path in sorted(vault.rglob(".DS_Store")):
        if is_excluded_path(vault, path):
            continue
        add_issue(
            issues,
            "WARN",
            "ds-store",
            rel(vault, path),
            "macOS metadata file should not be stored",
        )


def audit_sync_conflicts(vault: Path, issues: list[Issue]) -> None:
    """Report host-local conflict artifacts without treating them as note truth."""
    for path in sorted(
        candidate
        for candidate in vault.rglob("*")
        if candidate.is_file()
        and any(".sync-conflict-" in part for part in candidate.relative_to(vault).parts)
    ):
        add_issue(
            issues,
            "ERROR",
            "sync-conflict",
            rel(vault, path),
            "Syncthing conflict artifact requires manual recovery",
        )


def issue_counts(issues: list[Issue]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in issues:
        key = f"{item.severity}:{item.check}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def baseline_schema_problems(baseline: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if baseline.get("system") != "context-vault":
        problems.append("system must be 'context-vault'")
    schema_version = baseline.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        problems.append("schema_version must be integer 1")
    if baseline.get("mode") != "legacy-debt-ratchet":
        problems.append("mode must be 'legacy-debt-ratchet'")
    captured = baseline.get("captured")
    try:
        if not isinstance(captured, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", captured
        ):
            raise ValueError
        dt.date.fromisoformat(captured)
    except ValueError:
        problems.append("captured must be an ISO date (YYYY-MM-DD)")
    counts = baseline.get("counts")
    if not isinstance(counts, dict):
        problems.append("counts must be an object")
        return problems
    for key, value in counts.items():
        if (
            not isinstance(key, str)
            or not key.startswith(("ERROR:", "WARN:"))
            or not key.partition(":")[2]
        ):
            problems.append("count keys must start with ERROR: or WARN:")
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append(f"count {key!r} must be a non-negative integer")
    return problems


def valid_baseline_counts(baseline: dict[str, Any]) -> dict[str, int]:
    if baseline_schema_problems(baseline):
        return {}
    counts = baseline.get("counts")
    if not isinstance(counts, dict):
        return {}
    return {
        key: value
        for key, value in counts.items()
        if isinstance(key, str)
        and key.startswith(("ERROR:", "WARN:"))
        and bool(key.partition(":")[2])
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }


def ratchet_regressions(
    issues: list[Issue], baseline: dict[str, Any]
) -> dict[str, tuple[int, int]]:
    allowed = valid_baseline_counts(baseline)
    current = issue_counts(issues)
    regressions: dict[str, tuple[int, int]] = {}
    for key, count in current.items():
        if not key.startswith(("ERROR:", "WARN:")):
            continue
        limit = allowed.get(key, 0)
        if count > limit:
            regressions[key] = (count, limit)
    return regressions


def print_text(
    issues: list[Issue],
    summary: bool,
    regressions: dict[str, tuple[int, int]] | None = None,
) -> None:
    severity_counts = {
        severity: sum(1 for item in issues if item.severity == severity)
        for severity in ("ERROR", "WARN", "INFO")
    }
    print(
        "Summary: "
        + ", ".join(f"{key}={severity_counts[key]}" for key in ("ERROR", "WARN", "INFO"))
    )
    if regressions is not None:
        if regressions:
            print(
                "Ratchet: REGRESSION — "
                + ", ".join(
                    f"{key} {current}>{limit}"
                    for key, (current, limit) in regressions.items()
                )
            )
        else:
            print("Ratchet: OK — no ERROR/WARN category exceeds the reviewed baseline")
    if summary:
        return
    for item in issues:
        print(f"[{item.severity}] {item.check}: {item.path} - {item.message}")


def main() -> int:
    default_vault = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-path", default=str(default_vault))
    parser.add_argument("--oversized-chars", type=int, default=12000)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--ratchet", action="store_true")
    parser.add_argument("--baseline")
    parser.add_argument("--write-baseline")
    args = parser.parse_args()

    if args.ratchet and args.write_baseline:
        parser.error("--write-baseline cannot be combined with --ratchet")
    if args.oversized_chars <= 0:
        parser.error("--oversized-chars must be a positive integer")

    vault = Path(args.vault_path).expanduser().resolve()
    if not vault.exists():
        print(f"Vault path does not exist: {vault}", file=sys.stderr)
        return 2

    manifest_path = vault / "50_System/system-manifest.json"
    system_manifest = load_json(manifest_path)
    issues: list[Issue] = []
    audit_system_manifest(vault, system_manifest, issues)
    audit_project_routing(vault, system_manifest, issues)
    files = markdown_files(vault)
    audit_markdown(vault, files, args.oversized_chars, system_manifest, issues)
    audit_canonical_project_routing(vault, issues, system_manifest)
    audit_indexes(vault, system_manifest, issues)
    audit_session_filenames(vault, issues)
    audit_telemetry(vault, issues)
    audit_ds_store(vault, issues)
    audit_sync_conflicts(vault, issues)

    if args.write_baseline:
        target = Path(args.write_baseline).expanduser().resolve()
        document = {
            "system": "context-vault",
            "schema_version": 1,
            "captured": dt.date.today().isoformat(),
            "mode": "audit-baseline-proposal",
            "review_required": True,
            "counts": {
                key: count
                for key, count in issue_counts(issues).items()
                if key.startswith(("ERROR:", "WARN:"))
            },
        }
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        except FileExistsError:
            print(
                f"Refusing to overwrite existing baseline or proposal: {target}",
                file=sys.stderr,
            )
            return 2

    regressions: dict[str, tuple[int, int]] | None = None
    if args.ratchet:
        baseline_path = (
            Path(args.baseline).expanduser().resolve()
            if args.baseline
            else vault / "50_System/Audit_Baseline.json"
        )
        baseline = load_json(baseline_path)
        if not baseline:
            add_issue(
                issues,
                "ERROR",
                "audit-baseline",
                rel(vault, baseline_path),
                "missing or invalid ratchet baseline",
            )
        else:
            problems = baseline_schema_problems(baseline)
            if problems:
                add_issue(
                    issues,
                    "ERROR",
                    "audit-baseline",
                    rel(vault, baseline_path),
                    "invalid ratchet baseline: " + "; ".join(problems),
                )
        regressions = ratchet_regressions(issues, baseline)

    if args.json:
        result: dict[str, Any] = {
            "issues": [asdict(item) for item in issues],
            "counts": issue_counts(issues),
        }
        if regressions is not None:
            result["ratchet_regressions"] = {
                key: {"current": current, "baseline": limit}
                for key, (current, limit) in regressions.items()
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_text(issues, args.summary, regressions)

    if args.ratchet:
        return 1 if regressions else 0
    return 1 if any(item.severity == "ERROR" for item in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
