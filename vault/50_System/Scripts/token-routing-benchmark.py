#!/usr/bin/env python3
"""Read-only token-routing benchmark for a Context Vault.

The benchmark deliberately uses only the Python standard library. It prints to
stdout, never mutates the vault, never calls a network service, and never uses
embeddings. Token estimates follow the telemetry protocol: ceil(chars / 4).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_metadata import strict_frontmatter


SCHEMA_VERSION = 1
TOKEN_ESTIMATOR = "ceil(chars/4)"
DEFAULT_STALE_DAYS = 30
DEFAULT_USER_CONTENT_ROOTS = (
    "00_Inbox",
    "10_Projects",
    "20_Areas",
    "30_Resources",
    "40_Archive",
    "60_Sessions",
    "70_Decisions",
)
IGNORED_PARTS = {
    ".git",
    ".obsidian",
    ".stversions",
    ".trash",
    ".Trash",
    "__pycache__",
}
NON_MARKDOWN_EXTENSIONS = {
    ".base",
    ".canvas",
    ".excalidraw",
    ".gif",
    ".htm",
    ".html",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
    ".zip",
}
WIKILINK_RE = re.compile(r"!?\[\[([^\[\]\n]+?)\]\]")
READ_FIRST_RE = re.compile(
    r"^##\s+Read First\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
SESSION_INDEX_ROW_RE = re.compile(
    r"^- `60_Sessions/([^`]+)`\s+-\s+(\d+)\s+files?\b", re.MULTILINE
)
MASTER_POINTER_MARKER = "master-project-index-pointer"
PROJECT_INDEX_POINTER_TARGETS = {
    "Project_Index",
    "50_System/Indexes/Project_Index",
}


class BenchmarkError(RuntimeError):
    """Raised when the requested vault cannot be benchmarked safely."""


def relative(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def frontmatter(text: str) -> dict[str, str]:
    return {key: value.strip().strip("\"'") for key, value in (strict_frontmatter(text) or {}).items()}


def strip_code_regions(content: str) -> str:
    output: list[str] = []
    in_fence = False
    fence = ""
    for line in content.splitlines():
        stripped = line.lstrip()
        if in_fence:
            if stripped.startswith(fence):
                in_fence = False
            continue
        if stripped.startswith(("```", "~~~")):
            in_fence = True
            fence = stripped[:3]
            continue
        output.append(re.sub(r"(`+)(?:(?!\1).)*?\1", "", line))
    return "\n".join(output)


def wikilink_targets(text: str) -> list[str]:
    targets: list[str] = []
    for inner in WIKILINK_RE.findall(strip_code_regions(text)):
        normalized = inner.replace("\\|", "|").replace("&#124;", "|")
        target = normalized.split("|", 1)[0].split("#", 1)[0].strip()
        if target and "{{" not in target:
            targets.append(target)
    return targets


def read_first_targets(text: str) -> list[str]:
    match = READ_FIRST_RE.search(text)
    return wikilink_targets(match.group("body")) if match else []


def estimated_tokens(chars: int) -> int:
    return math.ceil(chars / 4)


def percentile(values: Sequence[int], fraction: float) -> float | int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * (
        position - lower
    )
    return round(result, 1)


def distribution(values: Iterable[int]) -> dict[str, float | int | None]:
    items = list(values)
    if not items:
        return {
            "count": 0,
            "total": 0,
            "min": None,
            "median": None,
            "p75": None,
            "p90": None,
            "max": None,
            "mean": None,
        }
    return {
        "count": len(items),
        "total": sum(items),
        "min": min(items),
        "median": percentile(items, 0.5),
        "p75": percentile(items, 0.75),
        "p90": percentile(items, 0.9),
        "max": max(items),
        "mean": round(sum(items) / len(items), 1),
    }


def ignored(path: Path) -> bool:
    return (
        path.is_symlink()
        or any(part in IGNORED_PARTS for part in path.parts)
        or ".sync-conflict-" in path.name
    )


def markdown_files(vault: Path) -> list[Path]:
    return sorted(
        path
        for path in vault.rglob("*.md")
        if path.is_file() and not ignored(path)
    )


def build_markdown_catalog(
    vault: Path,
) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    by_relative: dict[str, Path] = {}
    by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in markdown_files(vault):
        key = relative(vault, path).removesuffix(".md").casefold()
        by_relative[key] = path
        by_stem[path.stem.casefold()].append(path)
    return by_relative, dict(by_stem)


def within(vault: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(vault.resolve())
    except ValueError:
        return False
    return True


def resolve_wikilink(
    vault: Path,
    source: Path,
    target: str,
    by_relative: dict[str, Path],
    by_stem: dict[str, list[Path]],
) -> tuple[str, Path | None]:
    clean = target.replace("\\", "/").strip()
    lowered = clean.casefold()
    if lowered.startswith(("http://", "https://")):
        return "external", None

    suffix = Path(clean).suffix.casefold()
    if suffix in NON_MARKDOWN_EXTENSIONS:
        return "non_markdown", None

    without_suffix = clean[:-3] if lowered.endswith(".md") else clean
    candidates = (vault / without_suffix, source.parent / without_suffix)
    for candidate in candidates:
        markdown_candidate = candidate.parent / f"{candidate.name}.md"
        if within(vault, markdown_candidate) and markdown_candidate.is_file():
            return "resolved", markdown_candidate.resolve()

    direct = by_relative.get(without_suffix.casefold())
    if direct is not None:
        return "resolved", direct.resolve()

    leaf_matches = by_stem.get(Path(without_suffix).name.casefold(), [])
    if len(leaf_matches) == 1:
        return "resolved", leaf_matches[0].resolve()
    return ("ambiguous" if leaf_matches else "unresolved"), None


def canonical_home(
    project_dir: Path,
    slug: str,
    resolved_read_first: Sequence[Path],
) -> tuple[str, Path | None]:
    exact = project_dir / f"{slug}.md"
    if exact.is_file() and not ignored(exact) and within(project_dir, exact):
        return "canonical", exact.resolve()

    linked = [
        path
        for path in resolved_read_first
        if path.parent == project_dir.resolve() and path.name != "Memory_Manifest.md"
    ]
    if linked:
        return "read_first", linked[0]

    typed: list[Path] = []
    for candidate in sorted(project_dir.glob("*.md")):
        if (
            candidate.name == "Memory_Manifest.md"
            or ignored(candidate)
            or not within(project_dir, candidate)
        ):
            continue
        metadata = frontmatter(read_text(candidate))
        if metadata.get("type") == "project" and metadata.get("project") == slug:
            typed.append(candidate.resolve())
    if len(typed) == 1:
        return "typed", typed[0]
    return "missing", None


def parse_declared_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def freshness_measurement(
    metadata: dict[str, str],
    *,
    as_of: date,
    stale_days: int,
    active: bool,
) -> dict[str, Any]:
    raw_last_verified = metadata.get("last_verified")
    raw_updated = metadata.get("updated")
    last_verified = parse_declared_date(raw_last_verified)
    updated = parse_declared_date(raw_updated)

    basis: str | None
    selected: date | None
    fallback_used: bool
    fallback_reason: str | None
    if last_verified is not None:
        basis = "frontmatter.last_verified"
        selected = last_verified
        fallback_used = False
        fallback_reason = None
    elif updated is not None:
        basis = "frontmatter.updated"
        selected = updated
        fallback_used = True
        fallback_reason = (
            "missing_last_verified"
            if not raw_last_verified
            else "invalid_last_verified"
        )
    else:
        basis = None
        selected = None
        fallback_used = True
        if not raw_last_verified and not raw_updated:
            fallback_reason = "missing_last_verified_and_updated"
        elif raw_last_verified and raw_updated:
            fallback_reason = "invalid_last_verified_and_updated"
        elif raw_last_verified:
            fallback_reason = "invalid_last_verified_missing_updated"
        else:
            fallback_reason = "missing_last_verified_invalid_updated"

    age_days = (as_of - selected).days if selected is not None else None
    return {
        "preferred_field": "frontmatter.last_verified",
        "fallback_field": "frontmatter.updated",
        "basis": basis,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "date": selected.isoformat() if selected is not None else None,
        "age_days": age_days,
        "stale": active and age_days is not None and age_days > stale_days,
        "last_verified": (
            last_verified.isoformat() if last_verified is not None else None
        ),
        "declared_updated": updated.isoformat() if updated is not None else None,
    }


def project_measurements(
    vault: Path,
    as_of: date,
    stale_days: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_relative, by_stem = build_markdown_catalog(vault)
    rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()

    manifests = sorted(
        path
        for path in (vault / "10_Projects").glob("*/Memory_Manifest.md")
        if path.is_file() and not ignored(path)
    )
    for manifest in manifests:
        text = read_text(manifest)
        metadata = frontmatter(text)
        slug = manifest.parent.name
        status = metadata.get("status", "missing")
        status_counts[status] += 1
        freshness = freshness_measurement(
            metadata,
            as_of=as_of,
            stale_days=stale_days,
            active=status == "active",
        )

        targets = read_first_targets(text)
        resolved: list[Path] = []
        resolution_counts: Counter[str] = Counter()
        for target in targets:
            resolution, path = resolve_wikilink(
                vault, manifest, target, by_relative, by_stem
            )
            resolution_counts[resolution] += 1
            if path is not None and path != manifest.resolve() and path not in resolved:
                resolved.append(path)

        home_resolution, home = canonical_home(manifest.parent, slug, resolved)
        manifest_chars = len(text)
        home_chars = len(read_text(home)) if home is not None else 0
        read_first_chars = sum(len(read_text(path)) for path in resolved)

        rows.append(
            {
                "project": slug,
                "status": status,
                "last_verified": freshness["last_verified"],
                "declared_updated": freshness["declared_updated"],
                "freshness": freshness,
                "stale": freshness["stale"],
                "manifest": {
                    "path": relative(vault, manifest),
                    "chars": manifest_chars,
                    "estimated_tokens": estimated_tokens(manifest_chars),
                },
                "home": {
                    "found": home is not None,
                    "resolution": home_resolution,
                    "path": relative(vault, home) if home is not None else None,
                    "chars": home_chars,
                    "estimated_tokens": estimated_tokens(home_chars),
                },
                "manifest_plus_home": {
                    "chars": manifest_chars + home_chars,
                    "estimated_tokens": estimated_tokens(
                        manifest_chars + home_chars
                    ),
                },
                "read_first": {
                    "declared_links": len(targets),
                    "resolved_unique_notes": len(resolved),
                    "resolution_counts": dict(sorted(resolution_counts.items())),
                    "paths": [relative(vault, path) for path in resolved],
                    "incremental_chars": read_first_chars,
                    "incremental_estimated_tokens": estimated_tokens(
                        read_first_chars
                    ),
                    "total_chars_with_manifest": manifest_chars + read_first_chars,
                    "total_estimated_tokens_with_manifest": estimated_tokens(
                        manifest_chars + read_first_chars
                    ),
                },
            }
        )

    active = [row for row in rows if row["status"] == "active"]
    stale = [row for row in active if row["stale"]]
    unavailable_dates = [
        row["project"]
        for row in rows
        if row["freshness"]["date"] is None
    ]
    basis_counts = Counter(
        row["freshness"]["basis"] or "unavailable" for row in active
    )
    fallback_reasons = Counter(
        row["freshness"]["fallback_reason"]
        for row in active
        if row["freshness"]["fallback_reason"] is not None
    )
    age_buckets: Counter[str] = Counter()
    for row in active:
        age = row["freshness"]["age_days"]
        if age is None:
            age_buckets["missing_or_invalid"] += 1
        elif age < 0:
            age_buckets["future"] += 1
        elif age <= 7:
            age_buckets["0_7"] += 1
        elif age <= 30:
            age_buckets["8_30"] += 1
        elif age <= 60:
            age_buckets["stale_31_60"] += 1
        elif age <= 90:
            age_buckets["stale_61_90"] += 1
        else:
            age_buckets["stale_91_plus"] += 1

    costs = {
        "active_project_count": len(active),
        "manifest_only_estimated_tokens": distribution(
            row["manifest"]["estimated_tokens"] for row in active
        ),
        "manifest_plus_home_estimated_tokens": distribution(
            row["manifest_plus_home"]["estimated_tokens"] for row in active
        ),
        "manifest_plus_read_first_estimated_tokens": distribution(
            row["read_first"]["total_estimated_tokens_with_manifest"]
            for row in active
        ),
        "read_first_declared_links": distribution(
            row["read_first"]["declared_links"] for row in active
        ),
    }
    staleness = {
        "preferred_basis": "frontmatter.last_verified",
        "fallback_basis": "frontmatter.updated",
        "filesystem_mtime_used": False,
        "as_of": as_of.isoformat(),
        "threshold_days": stale_days,
        "active_project_count": len(active),
        "stale_count": len(stale),
        "stale_projects": [row["project"] for row in stale],
        "basis_counts": dict(sorted(basis_counts.items())),
        "fallback_count": sum(
            1 for row in active if row["freshness"]["fallback_used"]
        ),
        "fallback_reasons": dict(sorted(fallback_reasons.items())),
        "missing_or_invalid_freshness_date": unavailable_dates,
        "age_buckets": dict(sorted(age_buckets.items())),
        "freshness_age_days": distribution(
            row["freshness"]["age_days"]
            for row in active
            if row["freshness"]["age_days"] is not None
        ),
    }
    overview = {
        "routable_project_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "costs": costs,
        "staleness": staleness,
    }
    return rows, overview


def project_routes(text: str) -> set[str]:
    routes: set[str] = set()
    for target in wikilink_targets(text):
        clean = target.replace("\\", "/").removesuffix(".md").strip("/")
        match = re.fullmatch(
            r"10_Projects/([^/]+)/Memory_Manifest", clean, re.IGNORECASE
        )
        if match:
            routes.add(match.group(1))
    return routes


def decision_routes(text: str) -> set[str]:
    routes: set[str] = set()
    for target in wikilink_targets(text):
        clean = target.replace("\\", "/").removesuffix(".md").strip("/")
        if clean.startswith("70_Decisions/"):
            routes.add(clean)
    return routes


def generated_region(text: str, marker: str) -> str | None:
    begin = f"<!-- BEGIN GENERATED:{marker} -->"
    end = f"<!-- END GENERATED:{marker} -->"
    if text.count(begin) != 1 or text.count(end) != 1:
        return None
    start = text.find(begin) + len(begin)
    finish = text.find(end, start)
    return text[start:finish] if finish >= start else None


def project_index_pointer_targets(text: str) -> list[str]:
    targets: list[str] = []
    for target in wikilink_targets(text):
        clean = target.replace("\\", "/").removesuffix(".md").strip("/")
        targets.append(clean)
    return targets


def configured_index_paths(vault: Path) -> dict[str, Path]:
    system = load_json(vault / "50_System" / "system-manifest.json")
    indexes = system.get("indexes", {})
    if not isinstance(indexes, dict):
        indexes = {}
    defaults = {
        "project": "50_System/Indexes/Project_Index.md",
        "decision": "50_System/Indexes/Decision_Index.md",
        "session": "50_System/Indexes/Session_Index.md",
    }
    result: dict[str, Path] = {}
    for name, default in defaults.items():
        fallback = vault / default
        candidate = vault / str(indexes.get(name, default))
        result[name] = candidate if within(vault, candidate) else fallback
    return result


def catalog_drift(
    vault: Path,
    path: Path,
    all_projects: set[str],
    active_projects: set[str],
) -> dict[str, Any]:
    if not path.is_file() or ignored(path) or not within(vault, path):
        return {
            "path": relative(vault, path),
            "exists": False,
            "route_count": 0,
            "missing_all_projects": sorted(all_projects),
            "missing_active_projects": sorted(active_projects),
            "extra_projects": [],
        }
    routes = project_routes(read_text(path))
    return {
        "path": relative(vault, path),
        "exists": True,
        "route_count": len(routes),
        "missing_all_projects": sorted(all_projects - routes),
        "missing_active_projects": sorted(active_projects - routes),
        "extra_projects": sorted(routes - all_projects),
    }


def master_catalog_drift(
    vault: Path,
    path: Path,
    all_projects: set[str],
    active_projects: set[str],
) -> dict[str, Any]:
    """Validate generated pointer semantics, otherwise retain legacy catalog mode."""

    if not path.is_file() or ignored(path) or not within(vault, path):
        result = catalog_drift(
            vault, path, all_projects=all_projects, active_projects=active_projects
        )
        result.update(
            {
                "mode": "missing",
                "expected_scope": "active_projects",
                "pointer_target": "Project_Index",
                "pointer_targets": [],
                "pointer_exact": False,
                "pointer_issues": ["missing_master_index"],
            }
        )
        return result

    text = read_text(path)
    region = generated_region(text, MASTER_POINTER_MARKER)
    if region is None:
        result = catalog_drift(
            vault, path, all_projects=all_projects, active_projects=active_projects
        )
        result.update(
            {
                "mode": "legacy_catalog",
                "expected_scope": "active_projects",
                "pointer_target": "Project_Index",
                "pointer_targets": [],
                "pointer_exact": False,
                "pointer_issues": [],
            }
        )
        return result

    targets = project_index_pointer_targets(region)
    exact = len(targets) == 1 and targets[0] in PROJECT_INDEX_POINTER_TARGETS
    issues: list[str] = []
    if not exact:
        if not any(target in PROJECT_INDEX_POINTER_TARGETS for target in targets):
            issues.append("missing_project_index_pointer")
        if len(targets) != 1:
            issues.append("generated_region_must_contain_one_pointer")
        elif targets[0] not in PROJECT_INDEX_POINTER_TARGETS:
            issues.append("wrong_pointer_target")
    return {
        "path": relative(vault, path),
        "exists": True,
        "mode": "generated_pointer",
        "expected_scope": "project_index_pointer",
        "route_count": 0,
        "missing_all_projects": [],
        "missing_active_projects": [],
        "extra_projects": [],
        "pointer_target": "Project_Index",
        "pointer_targets": targets,
        "pointer_exact": exact,
        "pointer_issues": issues,
    }


def index_drift(vault: Path, project_rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    all_projects = {str(row["project"]) for row in project_rows}
    active_projects = {
        str(row["project"]) for row in project_rows if row["status"] == "active"
    }
    configured = configured_index_paths(vault)
    catalog_paths = {
        "project_index": configured["project"],
        "master_index": vault / "50_System/Indexes/Master_Index.md",
        "global_memory_manifest": (
            vault / "50_System/Indexes/Global_Memory_Manifest.md"
        ),
    }
    catalogs = {
        name: (
            master_catalog_drift(
                vault,
                path,
                all_projects=all_projects,
                active_projects=active_projects,
            )
            if name == "master_index"
            else catalog_drift(
                vault,
                path,
                all_projects=all_projects,
                active_projects=active_projects,
            )
        )
        for name, path in catalog_paths.items()
    }
    catalogs["project_index"]["expected_scope"] = "all_projects"
    catalogs["global_memory_manifest"]["expected_scope"] = "active_projects"

    decision_path = configured["decision"]
    actual_decisions = {
        relative(vault, path).removesuffix(".md")
        for path in sorted((vault / "70_Decisions").glob("*.md"))
        if path.is_file()
        and not ignored(path)
        and path.name not in {"README.md", "AGENTS.md", "Welcome.md"}
    }
    decision_exists = (
        decision_path.is_file()
        and not ignored(decision_path)
        and within(vault, decision_path)
    )
    indexed_decisions = (
        decision_routes(read_text(decision_path)) if decision_exists else set()
    )
    decisions = {
        "path": relative(vault, decision_path),
        "exists": decision_exists,
        "actual_count": len(actual_decisions),
        "route_count": len(indexed_decisions),
        "missing": sorted(actual_decisions - indexed_decisions),
        "extra": sorted(indexed_decisions - actual_decisions),
    }

    session_path = configured["session"]
    session_root = vault / "60_Sessions"
    actual_session_counts = {
        path.name: sum(
            1
            for note in path.rglob("*.md")
            if note.is_file() and not ignored(note)
        )
        for path in sorted(session_root.iterdir())
        if path.is_dir() and not ignored(path)
    } if session_root.is_dir() else {}
    session_index_exists = (
        session_path.is_file()
        and not ignored(session_path)
        and within(vault, session_path)
    )
    claimed_session_counts = (
        {
            match.group(1): int(match.group(2))
            for match in SESSION_INDEX_ROW_RE.finditer(read_text(session_path))
        }
        if session_index_exists
        else {}
    )
    mismatches = [
        {
            "root": root,
            "claimed": claimed_session_counts[root],
            "actual": actual_session_counts[root],
            "delta": actual_session_counts[root] - claimed_session_counts[root],
        }
        for root in sorted(actual_session_counts.keys() & claimed_session_counts.keys())
        if actual_session_counts[root] != claimed_session_counts[root]
    ]
    sessions = {
        "path": relative(vault, session_path),
        "exists": session_index_exists,
        "actual_root_count": len(actual_session_counts),
        "claimed_root_count": len(claimed_session_counts),
        "missing_roots": sorted(actual_session_counts.keys() - claimed_session_counts.keys()),
        "extra_roots": sorted(claimed_session_counts.keys() - actual_session_counts.keys()),
        "count_mismatches": mismatches,
        "net_file_delta": sum(item["delta"] for item in mismatches),
    }

    drift_items = 0
    for value in catalogs.values():
        if value["expected_scope"] == "project_index_pointer":
            drift_items += len(value["pointer_issues"])
            continue
        missing_key = (
            "missing_all_projects"
            if value["expected_scope"] == "all_projects"
            else "missing_active_projects"
        )
        drift_items += len(value[missing_key]) + len(value["extra_projects"])
    drift_items += len(decisions["missing"]) + len(decisions["extra"])
    drift_items += (
        len(sessions["missing_roots"])
        + len(sessions["extra_roots"])
        + len(sessions["count_mismatches"])
    )
    return {
        "drift_item_count": drift_items,
        "project_catalogs": catalogs,
        "decisions": decisions,
        "sessions": sessions,
    }


def safe_scope_root(vault: Path, value: str) -> Path | None:
    candidate = vault / value.strip().strip("/")
    return candidate if within(vault, candidate) else None


def user_content_roots(vault: Path) -> list[Path]:
    system = load_json(vault / "50_System" / "system-manifest.json")
    configured = system.get("user_content_roots", DEFAULT_USER_CONTENT_ROOTS)
    if not isinstance(configured, list):
        configured = list(DEFAULT_USER_CONTENT_ROOTS)
    roots = [
        root
        for value in configured
        if isinstance(value, str)
        for root in [safe_scope_root(vault, value)]
        if root is not None and root.is_dir()
    ]
    return roots or [
        vault / value
        for value in DEFAULT_USER_CONTENT_ROOTS
        if (vault / value).is_dir()
    ]


def files_under(roots: Iterable[Path]) -> list[Path]:
    files: set[Path] = set()
    for root in roots:
        if root.is_file() and not ignored(root):
            files.add(root)
        elif root.is_dir():
            files.update(
                path
                for path in root.rglob("*")
                if path.is_file() and not ignored(path)
            )
    return sorted(files)


def hash_scope(vault: Path, paths: Iterable[Path]) -> dict[str, Any]:
    selected = sorted(set(path for path in paths if path.is_file() and not ignored(path)))
    digest = hashlib.sha256()
    digest.update(b"sha256-path-content-v1\0")
    total_bytes = 0
    markdown_count = 0
    for path in selected:
        path_bytes = relative(vault, path).encode("utf-8")
        content = path.read_bytes()
        content_digest = hashlib.sha256(content).digest()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content_digest)
        total_bytes += len(content)
        markdown_count += path.suffix.casefold() == ".md"
    return {
        "algorithm": "sha256-path-content-v1",
        "sha256": digest.hexdigest(),
        "file_count": len(selected),
        "markdown_file_count": markdown_count,
        "bytes": total_bytes,
    }


def content_preservation(vault: Path) -> dict[str, Any]:
    roots = user_content_roots(vault)
    all_user_files = files_under(roots)
    project_root = vault / "10_Projects"
    project_files = files_under([project_root]) if project_root.is_dir() else []
    manifest_files = sorted(project_root.glob("*/Memory_Manifest.md"))
    manifest_set = set(manifest_files)
    project_detail_files = [path for path in project_files if path not in manifest_set]

    routing_paths: list[Path] = list(manifest_files)
    indexes = vault / "50_System/Indexes"
    if indexes.is_dir():
        routing_paths.extend(files_under([indexes]))
    routing_paths.extend(
        path
        for path in (
            vault / "50_System/Codex_Instructions.md",
            vault / "50_System/project-routing.json",
            vault / "50_System/system-manifest.json",
        )
        if path.is_file()
    )

    per_root = {
        relative(vault, root): hash_scope(vault, files_under([root]))
        for root in roots
    }
    return {
        "purpose": "compare before/after runs without storing note contents",
        "all_user_content": hash_scope(vault, all_user_files),
        "project_content_excluding_manifests": hash_scope(
            vault, project_detail_files
        ),
        "project_manifests": hash_scope(vault, manifest_files),
        "routing_surfaces": hash_scope(vault, routing_paths),
        "user_roots": per_root,
    }


def analyze_vault(
    vault: Path,
    *,
    as_of: date | None = None,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> dict[str, Any]:
    vault = vault.resolve()
    if not vault.is_dir():
        raise BenchmarkError(f"vault is not a directory: {vault}")
    if stale_days < 0:
        raise BenchmarkError("stale-days must be non-negative")
    benchmark_date = as_of or date.today()
    project_rows, overview = project_measurements(
        vault, as_of=benchmark_date, stale_days=stale_days
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "context-vault-token-routing",
        "read_only": True,
        "vault": str(vault),
        "token_estimator": TOKEN_ESTIMATOR,
        "as_of": benchmark_date.isoformat(),
        "projects": {
            "routable_count": overview["routable_project_count"],
            "status_counts": overview["status_counts"],
            "costs": overview["costs"],
            "items": project_rows,
        },
        "staleness": overview["staleness"],
        "index_drift": index_drift(vault, project_rows),
        "content_preservation": content_preservation(vault),
    }


def scalar_text(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def cost_line(label: str, values: dict[str, Any]) -> str:
    return (
        f"{label}: median={scalar_text(values['median'])}, "
        f"p90={scalar_text(values['p90'])}, max={scalar_text(values['max'])}"
    )


def summary(report: dict[str, Any]) -> str:
    projects = report["projects"]
    costs = projects["costs"]
    stale = report["staleness"]
    drift = report["index_drift"]
    hashes = report["content_preservation"]
    lines = [
        "Context Vault token routing benchmark",
        f"Vault: {report['vault']}",
        f"As of: {report['as_of']}; estimator: {report['token_estimator']}",
        (
            f"Projects: {projects['routable_count']} routable, "
            f"{costs['active_project_count']} active"
        ),
        cost_line(
            "Manifest only", costs["manifest_only_estimated_tokens"]
        ),
        cost_line(
            "Manifest + home", costs["manifest_plus_home_estimated_tokens"]
        ),
        cost_line(
            "Manifest + Read First",
            costs["manifest_plus_read_first_estimated_tokens"],
        ),
        (
            f"Freshness: {stale['stale_count']}/"
            f"{stale['active_project_count']} active projects exceed "
            f"{stale['threshold_days']} days; "
            f"basis={stale['preferred_basis']}, "
            f"fallback={stale['fallback_basis']} "
            f"({stale['fallback_count']} active fallbacks), mtime=unused"
        ),
        f"Index drift items: {drift['drift_item_count']}",
    ]
    for name, item in drift["project_catalogs"].items():
        if item["expected_scope"] == "project_index_pointer":
            lines.append(
                f"  {name}: mode={item['mode']}, "
                f"pointer_exact={str(item['pointer_exact']).lower()}, "
                f"pointer_issues={len(item['pointer_issues'])}"
            )
            continue
        lines.append(
            f"  {name}: missing_active={len(item['missing_active_projects'])}, "
            f"missing_all={len(item['missing_all_projects'])}, "
            f"extra={len(item['extra_projects'])}"
        )
    lines.extend(
        [
            (
                "  decisions: "
                f"missing={len(drift['decisions']['missing'])}, "
                f"extra={len(drift['decisions']['extra'])}"
            ),
            (
                "  sessions: "
                f"missing_roots={len(drift['sessions']['missing_roots'])}, "
                f"extra_roots={len(drift['sessions']['extra_roots'])}, "
                f"count_mismatches={len(drift['sessions']['count_mismatches'])}"
            ),
            "Content-preservation SHA-256:",
            (
                "  all_user_content="
                f"{hashes['all_user_content']['sha256']}"
            ),
            (
                "  project_content_excluding_manifests="
                f"{hashes['project_content_excluding_manifests']['sha256']}"
            ),
            f"  project_manifests={hashes['project_manifests']['sha256']}",
            f"  routing_surfaces={hashes['routing_surfaces']['sha256']}",
        ]
    )
    return "\n".join(lines)


def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault",
        type=Path,
        default=Path.cwd(),
        help="Context Vault root (default: current directory)",
    )
    parser.add_argument(
        "--as-of",
        type=iso_date,
        default=None,
        help="Reproducible staleness date in YYYY-MM-DD form",
    )
    parser.add_argument(
        "--stale-days",
        type=non_negative_int,
        default=DEFAULT_STALE_DAYS,
        help=f"Freshness staleness threshold (default: {DEFAULT_STALE_DAYS})",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Print JSON")
    output.add_argument(
        "--summary", action="store_true", help="Print the human summary (default)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = analyze_vault(
            args.vault, as_of=args.as_of, stale_days=args.stale_days
        )
    except (BenchmarkError, OSError) as exc:
        print(f"token-routing-benchmark: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
