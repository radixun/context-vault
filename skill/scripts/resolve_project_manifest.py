#!/usr/bin/env python3
"""Resolve a workspace to one canonical Context Vault project manifest."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_metadata import strict_frontmatter


EXIT_ACTIVE = 0
EXIT_INACTIVE = 1
EXIT_AMBIGUOUS = 2
EXIT_INVALID_REGISTRY = 3
EXIT_INVALID_VAULT = 4
EXIT_MISSING_REGISTRY = 5


class RoutingConfig(NamedTuple):
    """Validated routing controls loaded from the routing registry."""

    schema_version: int
    aliases: dict[str, str]
    workspace_roots: tuple[Path, ...]
    workspace_bindings: dict[Path, str]


class ManifestRecord(NamedTuple):
    path: Path
    slug: str
    project: str
    status: str
    error: str | None = None


def normalize(value: str) -> str:
    """Return the canonical Unicode/case/separator routing key."""

    value = unicodedata.normalize("NFKC", value).casefold()
    # ``_`` belongs to ``\w`` in Python, so normalize it before the generic
    # non-word pass. Doing this after trimming would leave edge/repeated
    # underscores as leading or duplicate hyphens (for example ``__foo__``).
    value = value.replace("_", "-")
    value = re.sub(r"[^\w]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def canonical_path(path: Path) -> Path:
    """Resolve existing parents and symlinks without requiring the leaf to exist."""

    return path.expanduser().resolve(strict=False)


def result(status: str, workspace: Path, **extra: object) -> dict[str, object]:
    return {"status": status, "workspace": str(workspace), **extra}


def safe_slug(value: object) -> str | None:
    """Accept only one normalized project-folder component."""

    if not isinstance(value, str) or not value:
        return None
    if value in {".", ".."} or Path(value).is_absolute() or Path(value).name != value:
        return None
    return value if normalize(value) == value else None


def scalar(value: str | None) -> str:
    return value.strip().strip("\"'") if value else ""


def read_frontmatter(path: Path) -> dict[str, str] | None:
    try:
        return strict_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def manifest_record(path: Path) -> ManifestRecord:
    slug = path.parent.name
    fm = read_frontmatter(path)
    if fm is None:
        return ManifestRecord(path, slug, "", "", "missing or invalid YAML frontmatter")
    project = scalar(fm.get("project"))
    status = scalar(fm.get("status"))
    if not project:
        return ManifestRecord(path, slug, project, status, "frontmatter project is empty")
    if project != slug:
        return ManifestRecord(
            path,
            slug,
            project,
            status,
            f"frontmatter project {project!r} does not match folder {slug!r}",
        )
    if not status:
        return ManifestRecord(path, slug, project, status, "frontmatter status is empty")
    return ManifestRecord(path, slug, project, status)


def expand_registry_path(raw: str, home: Path) -> Path | None:
    """Expand the portable ``~/`` form used by the routing registry."""

    if raw == "~":
        candidate = home
    elif raw.startswith("~/"):
        candidate = home / raw[2:]
    else:
        candidate = Path(raw)
    if not candidate.is_absolute():
        return None
    return canonical_path(candidate)


def _load_registry_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot read valid JSON: {exc}"
    if not isinstance(value, dict):
        return None, "registry root must be an object"
    return value, None


def load_routing_config(
    registry_path: Path,
    manifests: dict[str, Path],
    *,
    home: Path,
) -> tuple[RoutingConfig | None, str | None]:
    """Load schema-v1 routing plus optional backward-compatible v2 controls.

    Schema v1 remains valid. When ``workspace_roots`` is absent, routing is
    limited to ``~/Projects`` and the vault itself. Schema v2 may add:

    - ``workspace_roots``: absolute or ``~/`` directory roots;
    - ``workspace_bindings``: absolute or ``~/`` workspace subtree -> project slug.

    Explicit bindings authorize their bound root and descendants outside the
    trusted roots; the deepest matching binding wins.
    """

    registry, error = _load_registry_json(registry_path)
    if error:
        return None, error
    assert registry is not None

    raw_version = registry.get("schema_version", 1)
    if type(raw_version) is not int or raw_version not in {1, 2}:
        return None, "schema_version must be 1 or 2"

    raw_aliases = registry.get("workspace_aliases", {})
    if not isinstance(raw_aliases, dict):
        return None, "workspace_aliases must be an object"

    aliases: dict[str, str] = {}
    alias_sources: dict[str, str] = {}
    for alias, raw_target in raw_aliases.items():
        if not isinstance(alias, str):
            return None, "workspace alias names must be strings"
        target = safe_slug(raw_target)
        if target is None:
            return None, f"alias {alias!r} has a non-canonical target slug"
        normalized = normalize(alias)
        if not normalized:
            return None, f"alias {alias!r} normalizes to an empty value"
        if normalized in aliases:
            return None, (
                f"aliases {alias_sources[normalized]!r} and {alias!r} normalize to "
                f"the same value {normalized!r}"
            )
        if target not in manifests:
            return None, f"alias {alias!r} targets missing manifest {target!r}"
        aliases[normalized] = target
        alias_sources[normalized] = alias

    canonical_by_normalized: dict[str, list[str]] = {}
    for slug in manifests:
        canonical_by_normalized.setdefault(normalize(slug), []).append(slug)
    for key, target in aliases.items():
        canonical = canonical_by_normalized.get(key, [])
        if canonical and (len(canonical) != 1 or canonical[0] != target):
            return None, (
                f"alias {alias_sources[key]!r} conflicts with canonical project "
                f"routing for {key!r}"
            )

    raw_roots = registry.get("workspace_roots")
    if raw_roots is None:
        roots = (canonical_path(home / "Projects"),)
    else:
        if not isinstance(raw_roots, list):
            return None, "workspace_roots must be an array"
        parsed_roots: list[Path] = []
        for raw_root in raw_roots:
            if not isinstance(raw_root, str) or not raw_root:
                return None, "workspace_roots entries must be non-empty strings"
            parsed = expand_registry_path(raw_root, home)
            if parsed is None:
                return None, f"workspace root {raw_root!r} must be absolute or use ~/"
            if parsed not in parsed_roots:
                parsed_roots.append(parsed)
        roots = tuple(parsed_roots)

    raw_bindings = registry.get("workspace_bindings", {})
    if not isinstance(raw_bindings, dict):
        return None, "workspace_bindings must be an object"
    bindings: dict[Path, str] = {}
    binding_sources: dict[Path, str] = {}
    for raw_path, raw_target in raw_bindings.items():
        if not isinstance(raw_path, str) or not raw_path:
            return None, "workspace binding paths must be non-empty strings"
        path = expand_registry_path(raw_path, home)
        if path is None:
            return None, f"workspace binding {raw_path!r} must be absolute or use ~/"
        target = safe_slug(raw_target)
        if target is None:
            return None, f"workspace binding {raw_path!r} has a non-canonical target slug"
        if target not in manifests:
            return None, f"workspace binding {raw_path!r} targets missing manifest {target!r}"
        previous = bindings.get(path)
        if previous is not None and previous != target:
            return None, (
                f"workspace bindings {binding_sources[path]!r} and {raw_path!r} "
                "resolve to the same path with different targets"
            )
        bindings[path] = target
        binding_sources[path] = raw_path

    return RoutingConfig(raw_version, aliases, roots, bindings), None


def load_aliases(registry_path: Path) -> tuple[dict[str, str] | None, str | None]:
    """Compatibility helper retained for callers that only inspect aliases."""

    registry, error = _load_registry_json(registry_path)
    if error:
        return None, error
    assert registry is not None
    raw_aliases = registry.get("workspace_aliases", {})
    if not isinstance(raw_aliases, dict):
        return None, "workspace_aliases must be an object"
    aliases: dict[str, str] = {}
    sources: dict[str, str] = {}
    for alias, raw_target in raw_aliases.items():
        if not isinstance(alias, str):
            return None, "workspace alias names must be strings"
        target = safe_slug(raw_target)
        if target is None:
            return None, f"alias {alias!r} has a non-canonical target slug"
        normalized = normalize(alias)
        if not normalized:
            return None, f"alias {alias!r} normalizes to an empty value"
        if normalized in aliases:
            return None, (
                f"aliases {sources[normalized]!r} and {alias!r} normalize to "
                f"the same value {normalized!r}"
            )
        aliases[normalized] = target
        sources[normalized] = alias
    return aliases, None


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def longest_containing_prefix(path: Path, candidates: Iterable[Path]) -> Path | None:
    """Return the deepest canonical candidate containing ``path``."""

    matches = [candidate for candidate in candidates if is_within(path, candidate)]
    if not matches:
        return None
    return max(matches, key=lambda candidate: len(candidate.parts))


def routed_manifest_result(
    workspace: Path,
    record: ManifestRecord,
    *,
    source: str,
) -> tuple[dict[str, object], int]:
    """Activate only a structurally valid manifest whose status is active."""

    common = {
        "slug": record.slug,
        "manifest": str(record.path),
        "source": source,
    }
    if record.error is not None:
        return result(
            "inactive",
            workspace,
            reason="invalid-manifest",
            error=record.error,
            **common,
        ), EXIT_INACTIVE
    if record.status != "active":
        return result(
            "inactive",
            workspace,
            reason="manifest-not-active",
            manifest_status=record.status,
            **common,
        ), EXIT_INACTIVE
    return result("active", workspace, **common), EXIT_ACTIVE


def resolve(
    vault: Path,
    workspace: Path,
    *,
    home: Path | None = None,
) -> tuple[dict[str, object], int]:
    vault = canonical_path(vault)
    workspace = canonical_path(workspace)
    home = canonical_path(home or Path.home())

    if not vault.exists():
        return result("missing-vault", workspace, vault=str(vault)), EXIT_INVALID_VAULT
    if not vault.is_dir():
        return result(
            "invalid-vault", workspace, vault=str(vault), error="vault path is not a directory"
        ), EXIT_INVALID_VAULT

    projects_root = vault / "10_Projects"
    if not projects_root.is_dir():
        return result(
            "invalid-vault",
            workspace,
            vault=str(vault),
            error="10_Projects directory is missing",
        ), EXIT_INVALID_VAULT

    registry_path = vault / "50_System" / "project-routing.json"
    if not registry_path.exists():
        return result(
            "missing-registry", workspace, registry=str(registry_path)
        ), EXIT_MISSING_REGISTRY
    if not registry_path.is_file():
        return result(
            "invalid-registry",
            workspace,
            registry=str(registry_path),
            error="registry path is not a file",
        ), EXIT_INVALID_REGISTRY

    manifests = {
        path.parent.name: path
        for path in projects_root.glob("*/Memory_Manifest.md")
        if path.is_file() and ".sync-conflict-" not in path.name
    }
    records = {slug: manifest_record(path) for slug, path in manifests.items()}
    config, registry_error = load_routing_config(
        registry_path, manifests, home=home
    )
    if registry_error:
        return result(
            "invalid-registry",
            workspace,
            registry=str(registry_path),
            error=registry_error,
        ), EXIT_INVALID_REGISTRY
    assert config is not None

    if not workspace.exists():
        return result(
            "inactive",
            workspace,
            reason="workspace-missing",
            registry=str(registry_path),
        ), EXIT_INACTIVE
    if not workspace.is_dir():
        return result(
            "inactive",
            workspace,
            reason="workspace-not-directory",
            registry=str(registry_path),
        ), EXIT_INACTIVE

    binding_root = longest_containing_prefix(
        workspace, config.workspace_bindings.keys()
    )
    if binding_root is not None:
        bound_slug = config.workspace_bindings[binding_root]
        return routed_manifest_result(
            workspace, records[bound_slug], source="binding"
        )

    if workspace == vault:
        route_component = workspace.name
    else:
        workspace_root = longest_containing_prefix(workspace, config.workspace_roots)
        if workspace_root is None:
            return result(
                "inactive",
                workspace,
                normalized=normalize(workspace.name),
                reason="untrusted-workspace-root",
                registry=str(registry_path),
            ), EXIT_INACTIVE
        relative_workspace = workspace.relative_to(workspace_root)
        if not relative_workspace.parts:
            return result(
                "inactive",
                workspace,
                reason="workspace-root",
                registry=str(registry_path),
            ), EXIT_INACTIVE
        # A Codex workspace can be any descendant of the project checkout. The
        # first child below the most-specific trusted root is the stable project
        # identity; a transient leaf such as ``docs`` or ``src`` is not.
        route_component = relative_workspace.parts[0]

    normalized = normalize(route_component)
    if route_component in manifests:
        return routed_manifest_result(workspace, records[route_component], source="exact")

    normalized_matches = [
        (slug, path) for slug, path in manifests.items() if normalize(slug) == normalized
    ]
    if len(normalized_matches) == 1:
        slug, _ = normalized_matches[0]
        return routed_manifest_result(workspace, records[slug], source="normalized")
    if len(normalized_matches) > 1:
        return result(
            "ambiguous",
            workspace,
            candidates=sorted(slug for slug, _ in normalized_matches),
            source="normalized",
        ), EXIT_AMBIGUOUS

    target = config.aliases.get(normalized)
    if target is not None:
        return routed_manifest_result(workspace, records[target], source="registry")

    return result(
        "inactive",
        workspace,
        normalized=normalized,
        registry=str(registry_path),
    ), EXIT_INACTIVE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--workspace", default=Path.cwd(), type=Path)
    parser.add_argument("--format", choices=("json", "path"), default="json")
    args = parser.parse_args()

    payload, code = resolve(args.vault, args.workspace)
    if args.format == "path" and payload["status"] == "active":
        print(payload["manifest"])
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
