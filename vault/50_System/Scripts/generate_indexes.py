#!/usr/bin/env python3
"""Generate the small, canonical routing sections in Context Vault indexes.

Only content enclosed by ``BEGIN/END GENERATED`` markers is rewritten after
the initial migration.  The first write replaces the legacy route lists while
leaving the surrounding prose, policy, frontmatter, and instructions intact.
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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_metadata import strict_frontmatter
from vault_write import replace_checked


INDEX_ROOT = Path("50_System/Indexes")
INDEX_FILES = (
    "Global_Memory_Manifest.md",
    "Project_Index.md",
    "Session_Index.md",
    "Decision_Index.md",
    "Master_Index.md",
)
CANONICAL_INPUT_ROOTS = (
    Path("10_Projects"),
    Path("60_Sessions"),
    Path("70_Decisions"),
    INDEX_ROOT,
)
IGNORED_NOTE_NAMES = {"AGENTS.md", "README.md", "Welcome.md"}
SESSION_TYPES = {"daily-log", "session", "session-log"}
PROJECT_STATUSES = {"active", "paused"}
DECISION_GROUPS = {
    "active": {"active", "accepted"},
    "done": {"done", "complete", "completed"},
    "archived": {"superseded", "archived"},
}


class GenerationError(RuntimeError):
    """Raised when canonical source data or generated markers are unsafe."""


@dataclass(frozen=True)
class Note:
    path: Path
    relative: str
    frontmatter: dict[str, str]
    text: str


@dataclass(frozen=True)
class Project:
    slug: str
    status: str
    summary: str
    manifest_route: str
    home_route: str
    title: str


@dataclass(frozen=True)
class SessionRoot:
    slug: str
    count: int
    first: date | None
    last: date | None
    canonical: bool


@dataclass(frozen=True)
class Decision:
    route: str
    status: str
    project: str
    summary: str
    title: str


def _absolute(path: Path) -> Path:
    """Return an absolute path without hiding a symlink at the supplied leaf."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def validate_generation_inputs(vault: Path) -> Path:
    """Fail closed on symlinks and Syncthing conflict copies in routed inputs."""

    supplied = _absolute(vault)
    if supplied.is_symlink():
        raise GenerationError(f"vault root must not be a symlink: {supplied}")
    if not supplied.is_dir():
        raise GenerationError(f"vault path is not a directory: {supplied}")
    vault = supplied.resolve()

    for relative_root in CANONICAL_INPUT_ROOTS:
        root = vault / relative_root
        if root.is_symlink():
            raise GenerationError(
                f"canonical input root must not be a symlink: {relative_root.as_posix()}"
            )
        if not root.is_dir():
            raise GenerationError(f"missing canonical input root: {root}")
        for directory, names, filenames in os.walk(root, followlinks=False):
            parent = Path(directory)
            for name in sorted((*names, *filenames), key=_sort_key):
                candidate = parent / name
                relative = candidate.relative_to(vault).as_posix()
                if ".sync-conflict-" in name:
                    raise GenerationError(
                        f"Syncthing conflict artifact blocks index generation: {relative}"
                    )
                if candidate.is_symlink():
                    raise GenerationError(
                        f"symlink is not allowed in canonical index inputs: {relative}"
                    )
    return vault


def _sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else str(decoded)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def parse_frontmatter(text: str, path: Path) -> dict[str, str]:
    values = strict_frontmatter(text)
    if values is None:
        raise GenerationError(f"missing or invalid YAML frontmatter: {path}")
    return {key: _scalar(value) for key, value in values.items()}


def read_note(path: Path, vault: Path) -> Note:
    if path.is_symlink():
        raise GenerationError(
            f"symlink is not allowed for canonical source note: "
            f"{path.relative_to(vault).as_posix()}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GenerationError(f"could not read {path}: {exc}") from exc
    return Note(
        path=path,
        relative=path.relative_to(vault).as_posix(),
        frontmatter=parse_frontmatter(text, path),
        text=text,
    )


def _required(note: Note, key: str) -> str:
    value = note.frontmatter.get(key, "").strip()
    if not value:
        raise GenerationError(f"missing frontmatter {key!r}: {note.relative}")
    return value


def _h1(text: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _inline(value: str) -> str:
    return " ".join(value.replace("|", " / ").replace("]]", "] ]").split())


def _sentence(value: str) -> str:
    value = _inline(value)
    if not value:
        return "No summary provided."
    return value if value.endswith((".", "!", "?", "…")) else f"{value}."


def _route(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix().removesuffix(".md")


def _resolve_home_link(vault: Path, manifest: Note) -> Path | None:
    """Resolve an explicitly declared Project home wikilink, if present."""

    match = re.search(
        r"(?im)^\s*-\s*Project home:\s*\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]",
        manifest.text,
    )
    if not match:
        return None
    target = match.group(1).strip().replace("\\", "/").removesuffix(".md")
    candidate = Path(target)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise GenerationError(
            f"unsafe Project home link in {manifest.relative}: {target!r}"
        )
    if "/" in target:
        resolved = vault / f"{target}.md"
    else:
        resolved = manifest.path.parent / f"{target}.md"
    try:
        resolved.relative_to(vault)
    except ValueError as exc:
        raise GenerationError(
            f"Project home escapes vault in {manifest.relative}: {target!r}"
        ) from exc
    if not resolved.is_file():
        raise GenerationError(
            f"Project home link does not resolve in {manifest.relative}: {target!r}"
        )
    return resolved


def _project_home(vault: Path, manifest: Note, slug: str) -> Note:
    explicit = _resolve_home_link(vault, manifest)
    conventional = manifest.path.parent / f"{slug}.md"
    if explicit is not None:
        candidates = [explicit]
    elif conventional.is_file():
        candidates = [conventional]
    else:
        candidates = []
        for candidate in sorted(manifest.path.parent.glob("*.md")):
            if candidate == manifest.path or candidate.name in IGNORED_NOTE_NAMES:
                continue
            note = read_note(candidate, vault)
            if (
                note.frontmatter.get("type") == "project"
                and note.frontmatter.get("project") == slug
            ):
                candidates.append(candidate)
        if len(candidates) != 1:
            raise GenerationError(
                f"expected exactly one project home for {slug!r}; found {len(candidates)}"
            )

    home = read_note(candidates[0], vault)
    if home.frontmatter.get("type") != "project":
        raise GenerationError(f"project home is not type 'project': {home.relative}")
    if _required(home, "project") != slug:
        raise GenerationError(
            f"project home slug does not match folder {slug!r}: {home.relative}"
        )
    return home


def discover_projects(vault: Path) -> list[Project]:
    root = vault / "10_Projects"
    if not root.is_dir():
        raise GenerationError(f"missing project root: {root}")
    projects: list[Project] = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: _sort_key(p.name)):
        manifest_path = folder / "Memory_Manifest.md"
        if not manifest_path.is_file():
            raise GenerationError(f"project folder is missing Memory_Manifest.md: {folder}")
        manifest = read_note(manifest_path, vault)
        slug = _required(manifest, "project")
        if slug != folder.name:
            raise GenerationError(
                f"manifest project {slug!r} does not match folder {folder.name!r}: "
                f"{manifest.relative}"
            )
        status = _required(manifest, "status").casefold()
        if status not in PROJECT_STATUSES:
            raise GenerationError(
                f"unsupported project status {status!r}: {manifest.relative}"
            )
        home = _project_home(vault, manifest, slug)
        projects.append(
            Project(
                slug=slug,
                status=status,
                summary=_sentence(_required(manifest, "summary")),
                manifest_route=_route(vault, manifest.path),
                home_route=_route(vault, home.path),
                title=_inline(_h1(home.text, slug)),
            )
        )
    return projects


def _iso_date(value: str, relative: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GenerationError(
            f"invalid frontmatter created date {value!r}: {relative}"
        ) from exc


def discover_sessions(vault: Path, project_slugs: set[str]) -> list[SessionRoot]:
    root = vault / "60_Sessions"
    if not root.is_dir():
        raise GenerationError(f"missing session root: {root}")
    result: list[SessionRoot] = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: _sort_key(p.name)):
        dates: list[date] = []
        notes = [
            p
            for p in sorted(folder.rglob("*.md"))
            if p.name not in IGNORED_NOTE_NAMES
        ]
        for path in notes:
            note = read_note(path, vault)
            note_type = _required(note, "type").casefold()
            if note_type not in SESSION_TYPES:
                raise GenerationError(
                    f"unsupported session note type {note_type!r}: {note.relative}"
                )
            dates.append(_iso_date(_required(note, "created"), note.relative))
        result.append(
            SessionRoot(
                slug=folder.name,
                count=len(notes),
                first=min(dates) if dates else None,
                last=max(dates) if dates else None,
                canonical=folder.name == "_general" or folder.name in project_slugs,
            )
        )
    return result


def discover_decisions(vault: Path) -> list[Decision]:
    root = vault / "70_Decisions"
    if not root.is_dir():
        raise GenerationError(f"missing decision root: {root}")
    supported = set().union(*DECISION_GROUPS.values())
    result: list[Decision] = []
    for path in sorted(root.glob("*.md"), key=lambda p: _sort_key(p.name)):
        if path.name in IGNORED_NOTE_NAMES:
            continue
        note = read_note(path, vault)
        if _required(note, "type").casefold() != "decision":
            raise GenerationError(f"non-decision note in decision root: {note.relative}")
        status = _required(note, "status").casefold()
        if status not in supported:
            raise GenerationError(
                f"unsupported decision status {status!r}: {note.relative}"
            )
        title = re.sub(r"^ADR:\s*", "", _h1(note.text, path.stem), flags=re.I)
        result.append(
            Decision(
                route=_route(vault, path),
                status=status,
                project=_inline(_required(note, "project")),
                summary=_sentence(_required(note, "summary")),
                title=_inline(title),
            )
        )
    return result


def marker_block(marker: str, body: str) -> str:
    return (
        f"<!-- BEGIN GENERATED:{marker} -->\n"
        f"{body.rstrip()}\n"
        f"<!-- END GENERATED:{marker} -->"
    )


def _marker_tokens(marker: str) -> tuple[str, str]:
    return (
        f"<!-- BEGIN GENERATED:{marker} -->",
        f"<!-- END GENERATED:{marker} -->",
    )


def _replace_existing_marker(text: str, marker: str, block: str) -> str | None:
    begin, end = _marker_tokens(marker)
    begin_count, end_count = text.count(begin), text.count(end)
    if begin_count == end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise GenerationError(
            f"marker {marker!r} must have exactly one BEGIN and one END"
        )
    start = text.index(begin)
    finish = text.index(end)
    if finish < start:
        raise GenerationError(f"END precedes BEGIN for marker {marker!r}")
    finish += len(end)
    return text[:start] + block + text[finish:]


def _heading_match(text: str, headings: Sequence[str]) -> re.Match[str] | None:
    alternatives = "|".join(re.escape(value) for value in headings)
    return re.search(rf"^##\s+(?:{alternatives})\s*$", text, re.MULTILINE)


def _next_h2(text: str, offset: int) -> int:
    match = re.search(r"^##\s+", text[offset:], re.MULTILINE)
    return offset + match.start() if match else len(text)


def _insert_section(text: str, heading: str, block: str, before: str | None) -> str:
    insertion = f"## {heading}\n\n{block}\n\n"
    if before:
        match = re.search(rf"^##\s+{re.escape(before)}\s*$", text, re.MULTILINE)
        if match:
            prefix = text[: match.start()]
            if prefix and not prefix.endswith("\n\n"):
                prefix = prefix.rstrip("\n") + "\n\n"
            return prefix + insertion + text[match.start() :]
    prefix = text.rstrip("\n")
    return f"{prefix}\n\n{insertion.rstrip()}\n"


def generated_section(
    text: str,
    *,
    marker: str,
    body: str,
    headings: Sequence[str],
    canonical_heading: str,
    legacy_bullet: str,
    insert_before: str | None,
) -> str:
    """Replace a marker or safely migrate one legacy list section."""

    block = marker_block(marker, body)
    replaced = _replace_existing_marker(text, marker, block)
    if replaced is not None:
        return replaced

    heading = _heading_match(text, headings)
    if heading is None:
        return _insert_section(text, canonical_heading, block, insert_before)

    section_start = heading.end()
    section_end = _next_h2(text, section_start)
    section = text[section_start:section_end]
    lines = section.splitlines(keepends=True)
    bullet_re = re.compile(legacy_bullet)
    first: int | None = None
    for index, line in enumerate(lines):
        if bullet_re.match(line.rstrip("\r\n")):
            first = index
            break
    if first is not None:
        last = first
        while last < len(lines):
            stripped = lines[last].rstrip("\r\n")
            if bullet_re.match(stripped) or (last > first and stripped.startswith(("  ", "\t"))):
                last += 1
                continue
            break
        lines[first:last] = [block + "\n"]
        migrated = "".join(lines)
    else:
        core = section.rstrip()
        tail = section[len(core) :]
        if core.strip():
            migrated = core.rstrip("\r\n") + "\n\n" + block + "\n" + tail
        else:
            migrated = "\n\n" + block + "\n\n"
    return text[:section_start] + migrated + text[section_end:]


def _remove_placeholder(text: str, placeholder: str) -> str:
    if "<!-- BEGIN GENERATED:" in text:
        return text
    return re.sub(
        rf"(?m)^{re.escape(placeholder)}[ \t]*\r?\n?",
        "",
        text,
        count=1,
    )


def _rows(items: Iterable[str], empty: str) -> str:
    values = list(items)
    return "\n".join(values) if values else f"_{empty}_"


def _global_project_rows(projects: Iterable[Project]) -> str:
    return _rows(
        (
            f"- [[{project.manifest_route}|{_inline(project.slug)}]] — {project.summary}"
            for project in projects
        ),
        "No projects in this state.",
    )


def _project_rows(projects: Iterable[Project]) -> str:
    return _rows(
        (
            f"- [[{project.home_route}|{project.title}]] · "
            f"[[{project.manifest_route}|manifest]]"
            for project in projects
        ),
        "No projects in this state.",
    )


def _session_rows(roots: Iterable[SessionRoot]) -> str:
    def row(item: SessionRoot) -> str:
        noun = "file" if item.count == 1 else "files"
        if item.first is None or item.last is None:
            span = "no dated sessions"
        elif item.first == item.last:
            span = item.first.isoformat()
        else:
            span = f"{item.first.isoformat()} to {item.last.isoformat()}"
        return f"- `60_Sessions/{item.slug}` - {item.count} {noun}, {span}."

    return _rows((row(item) for item in roots), "No session roots in this group.")


def _decision_rows(decisions: Iterable[Decision]) -> str:
    return _rows(
        (
            f"- [[{item.route}|{item.title}]] — {item.project}: {item.summary}"
            for item in decisions
        ),
        "No decisions in this group.",
    )


def render_global(text: str, projects: Sequence[Project]) -> str:
    active = [item for item in projects if item.status == "active"]
    paused = [item for item in projects if item.status == "paused"]
    text = generated_section(
        text,
        marker="global-active-projects",
        body=_global_project_rows(active),
        headings=("Active Projects",),
        canonical_heading="Active Projects",
        legacy_bullet=r"^-\s+\[\[10_Projects/",
        insert_before="Read Rule",
    )
    return generated_section(
        text,
        marker="global-paused-projects",
        body=_global_project_rows(paused),
        headings=("Paused Projects",),
        canonical_heading="Paused Projects",
        legacy_bullet=r"^-\s+\[\[10_Projects/",
        insert_before="Read Rule",
    )


def render_project_index(text: str, projects: Sequence[Project]) -> str:
    active = [item for item in projects if item.status == "active"]
    paused = [item for item in projects if item.status == "paused"]
    text = generated_section(
        text,
        marker="project-active-projects",
        body=_project_rows(active),
        headings=("Active Projects",),
        canonical_heading="Active Projects",
        legacy_bullet=r"^-\s+\[\[10_Projects/",
        insert_before="Project Note Requirements",
    )
    return generated_section(
        text,
        marker="project-paused-projects",
        body=_project_rows(paused),
        headings=("Paused Projects",),
        canonical_heading="Paused Projects",
        legacy_bullet=r"^-\s+\[\[10_Projects/",
        insert_before="Project Note Requirements",
    )


def render_session_index(text: str, roots: Sequence[SessionRoot]) -> str:
    text = _remove_placeholder(text, "No project session roots yet.")
    canonical = [item for item in roots if item.canonical]
    unassigned = [item for item in roots if not item.canonical]
    text = generated_section(
        text,
        marker="session-canonical-roots",
        body=_session_rows(canonical),
        headings=("Canonical Project Session Roots",),
        canonical_heading="Canonical Project Session Roots",
        legacy_bullet=r"^-\s+`60_Sessions/",
        insert_before="Template",
    )
    return generated_section(
        text,
        marker="session-unassigned-roots",
        body=_session_rows(unassigned),
        headings=("Unassigned Session Roots",),
        canonical_heading="Unassigned Session Roots",
        legacy_bullet=r"^-\s+`60_Sessions/",
        insert_before="Template",
    )


def render_decision_index(text: str, decisions: Sequence[Decision]) -> str:
    text = _remove_placeholder(text, "No decisions yet.")
    active = [item for item in decisions if item.status in DECISION_GROUPS["active"]]
    done = [item for item in decisions if item.status in DECISION_GROUPS["done"]]
    archived = [item for item in decisions if item.status in DECISION_GROUPS["archived"]]
    text = generated_section(
        text,
        marker="decision-active-accepted",
        body=_decision_rows(active),
        headings=("Active Or Accepted",),
        canonical_heading="Active Or Accepted",
        legacy_bullet=r"^-\s+\[\[70_Decisions/",
        insert_before="Decision Threshold",
    )
    text = generated_section(
        text,
        marker="decision-done",
        body=_decision_rows(done),
        headings=("Done", "Done Or Historical"),
        canonical_heading="Done Or Historical",
        legacy_bullet=r"^-\s+\[\[70_Decisions/",
        insert_before="Decision Threshold",
    )
    return generated_section(
        text,
        marker="decision-superseded-archived",
        body=_decision_rows(archived),
        headings=("Superseded", "Superseded Or Archived"),
        canonical_heading="Superseded Or Archived",
        legacy_bullet=r"^-\s+\[\[70_Decisions/",
        insert_before="Decision Threshold",
    )


def render_master_index(text: str) -> str:
    return generated_section(
        text,
        marker="master-project-index-pointer",
        body="- [[Project_Index|Project Index]]",
        headings=("Active Projects",),
        canonical_heading="Active Projects",
        legacy_bullet=r"^-\s+\[\[(?:10_Projects/|40_Archive/Projects/)",
        insert_before="High-Value Resources",
    )


def build_updates(vault: Path, originals_out: dict[Path, str] | None = None) -> dict[Path, str]:
    vault = validate_generation_inputs(vault)
    projects = discover_projects(vault)
    sessions = discover_sessions(vault, {item.slug for item in projects})
    decisions = discover_decisions(vault)
    paths = {name: vault / INDEX_ROOT / name for name in INDEX_FILES}
    for path in paths.values():
        if not path.is_file():
            raise GenerationError(f"missing index file: {path}")
    originals: dict[str, str] = {}
    for name in INDEX_FILES:
        path = paths[name]
        if path.is_symlink():
            raise GenerationError(
                f"symlink is not allowed for generated index: "
                f"{path.relative_to(vault).as_posix()}"
            )
        raw = path.read_bytes().decode("utf-8")
        originals[name] = raw.replace("\r\n", "\n")
        if originals_out is not None:
            originals_out[path] = raw
    return {
        paths["Global_Memory_Manifest.md"]: render_global(
            originals["Global_Memory_Manifest.md"], projects
        ),
        paths["Project_Index.md"]: render_project_index(
            originals["Project_Index.md"], projects
        ),
        paths["Session_Index.md"]: render_session_index(
            originals["Session_Index.md"], sessions
        ),
        paths["Decision_Index.md"]: render_decision_index(
            originals["Decision_Index.md"], decisions
        ),
        paths["Master_Index.md"]: render_master_index(originals["Master_Index.md"]),
    }


def changed_updates(updates: dict[Path, str]) -> dict[Path, str]:
    return {
        path: content
        for path, content in updates.items()
        if path.read_bytes().decode("utf-8") != content
    }


def atomic_write(path: Path, content: str, *, expected: str | None = None) -> None:
    if path.is_symlink():
        raise GenerationError(f"refusing atomic write through symlink: {path}")
    original = path.read_bytes() if expected is None else expected.encode("utf-8")
    try:
        replace_checked(path, content.encode("utf-8"), hashlib.sha256(original).hexdigest())
    except ValueError as exc:
        raise GenerationError(str(exc)) from exc


def _report(vault: Path, action: str, changed: Sequence[Path]) -> dict[str, object]:
    relative = [path.relative_to(vault).as_posix() for path in sorted(changed)]
    if action == "check":
        status = "drift" if relative else "clean"
    else:
        status = "written" if relative else "clean"
    return {
        "action": action,
        "changed": relative,
        "changed_count": len(relative),
        "status": status,
        "vault": str(vault),
    }


def _print_summary(report: dict[str, object]) -> None:
    print(
        f"Index generation: {report['status']} "
        f"({report['changed_count']} file(s); action={report['action']})"
    )
    for path in report["changed"]:
        print(f"- {path}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="vault root (defaults to the root containing this script)",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="report drift without writes")
    action.add_argument("--write", action="store_true", help="atomically update drifted indexes")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit one JSON object")
    output.add_argument("--summary", action="store_true", help="emit a concise text summary")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    vault = _absolute(args.vault)
    action = "check" if args.check else "write"
    try:
        originals: dict[Path, str] = {}
        updates = build_updates(vault, originals)
        changed = changed_updates(updates)
        if args.write:
            for path in changed:
                if path.read_bytes().decode("utf-8") != originals[path]:
                    raise GenerationError(f"concurrent index edit; re-read and reconcile: {path}")
            for path in sorted(changed):
                atomic_write(path, changed[path], expected=originals[path])
        report = _report(vault.resolve(), action, list(changed))
    except (GenerationError, OSError, UnicodeError) as exc:
        if args.json:
            print(
                json.dumps(
                    {"action": action, "error": str(exc), "status": "error", "vault": str(vault)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(f"Index generation error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_summary(report)
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
