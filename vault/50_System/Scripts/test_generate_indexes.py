import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("generate_indexes.py")
SPEC = importlib.util.spec_from_file_location("generate_indexes", SCRIPT)
generator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


def frontmatter(kind, status, project, summary, body, created="2026-01-01"):
    return (
        "---\n"
        f"type: {kind}\n"
        f"status: {status}\n"
        f"created: {created}\n"
        f"updated: {created}\n"
        f"project: {project}\n"
        "tags: []\n"
        f"summary: {summary}\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


class VaultFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        for relative in (
            "10_Projects",
            "50_System/Indexes",
            "60_Sessions",
            "70_Decisions",
        ):
            (self.vault / relative).mkdir(parents=True)
        self._seed_indexes()

    def close(self):
        self.temp.cleanup()

    def write(self, relative, content):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read_index(self, name):
        return (self.vault / "50_System/Indexes" / name).read_text(encoding="utf-8")

    def _seed_indexes(self):
        common = (
            "---\n"
            "type: system\n"
            "status: active\n"
            "created: 2026-01-01\n"
            "updated: 2026-01-01\n"
            "tags: []\n"
            "summary: Test index.\n"
            "---\n\n"
        )
        self.write(
            "50_System/Indexes/Global_Memory_Manifest.md",
            common
            + "# Global Memory Manifest\n\n"
            + "GLOBAL PRELUDE MUST STAY.\n\n"
            + "## Active Projects\n\n"
            + "- [[10_Projects/stale/Memory_Manifest|stale]] - stale route.\n\n"
            + "## Read Rule\n\nGLOBAL READ POLICY MUST STAY.\n",
        )
        self.write(
            "50_System/Indexes/Project_Index.md",
            common
            + "# Project Index\n\n"
            + "## Active Projects\n\n"
            + "- [[10_Projects/stale/stale|Stale]] - stale.\n"
            + "- [[10_Projects/stale/Memory_Manifest|Stale Manifest]]\n\n"
            + "## Project Note Requirements\n\nPROJECT POLICY MUST STAY.\n",
        )
        self.write(
            "50_System/Indexes/Session_Index.md",
            common
            + "# Session Index\n\nSESSION PRELUDE MUST STAY.\n\n"
            + "## Canonical Project Session Roots\n\n"
            + "- `60_Sessions/stale` - 99 files, 1999-01-01.\n\n"
            + "## Unassigned Session Roots\n\n"
            + "UNASSIGNED POLICY MUST STAY.\n\n"
            + "- `60_Sessions/old-root` - 2 files, 1999-01-01.\n\n"
            + "## Template\n\nSESSION TEMPLATE MUST STAY.\n",
        )
        self.write(
            "50_System/Indexes/Decision_Index.md",
            common
            + "# Decision Index\n\n"
            + "## Active Or Accepted\n\n"
            + "- [[70_Decisions/stale|Stale]] - stale.\n\n"
            + "## Done Or Historical\n\n"
            + "- [[70_Decisions/stale-done|Stale done]] - stale.\n\n"
            + "## Superseded\n\n"
            + "- [[70_Decisions/stale-old|Stale old]] - stale.\n\n"
            + "## Decision Threshold\n\nDECISION POLICY MUST STAY.\n",
        )
        self.write(
            "50_System/Indexes/Master_Index.md",
            common
            + "# Master Index\n\nMASTER PRELUDE MUST STAY.\n\n"
            + "## Active Projects\n\n"
            + "- [[10_Projects/stale/stale|Stale]]\n"
            + "- [[10_Projects/stale/Memory_Manifest|Stale Manifest]]\n\n"
            + "When another project starts, follow the project policy.\n\n"
            + "## High-Value Resources\n\nMASTER RESOURCES MUST STAY.\n",
        )

    def add_project(
        self,
        slug,
        *,
        status="active",
        summary=None,
        title=None,
        home_name=None,
        explicit_home=False,
    ):
        summary = summary or f"Manifest summary for {slug}"
        title = title or slug.title()
        home_name = home_name or slug
        home_route = f"10_Projects/{slug}/{home_name}"
        read_first = (
            f"\n## Read First\n\n- Project home: [[{home_route}|{title}]]\n"
            if explicit_home
            else ""
        )
        self.write(
            f"10_Projects/{slug}/Memory_Manifest.md",
            frontmatter(
                "manifest",
                status,
                slug,
                summary,
                f"# Memory Manifest: {slug}{read_first}",
            ),
        )
        self.write(
            f"10_Projects/{slug}/{home_name}.md",
            frontmatter(
                "project",
                status,
                slug,
                f"HOME SUMMARY MUST NOT WIN for {slug}",
                f"# {title}\n\nProject body.",
            ),
        )

    def add_session(self, root, filename, created, project=None):
        project = project if project is not None else root
        self.write(
            f"60_Sessions/{root}/{filename}",
            frontmatter(
                "session",
                "done",
                project,
                f"Session for {root}",
                f"# Session {filename}",
                created=created,
            ),
        )

    def add_decision(self, stem, status, project="alpha"):
        self.write(
            f"70_Decisions/{stem}.md",
            frontmatter(
                "decision",
                status,
                project,
                f"Canonical summary for {stem}",
                f"# ADR: Title {stem}",
            ),
        )

    def populate(self):
        self.add_project(
            "alpha",
            summary="Alpha manifest truth",
            title="Alpha Exact Home",
            home_name="Alpha_Home",
            explicit_home=True,
        )
        self.add_project("beta", summary="Beta manifest truth")
        self.add_project("paused-one", status="paused", summary="Paused manifest truth")
        self.add_session("alpha", "2099-name.md", "2026-02-03")
        self.add_session("alpha", "1900-name.md", "2026-01-02")
        self.add_session("_general", "general.md", "2026-01-05", project="_general")
        self.add_session("legacy-root", "legacy.md", "2025-12-31", project="_unassigned")
        for stem, status in (
            ("active-choice", "active"),
            ("accepted-choice", "accepted"),
            ("done-choice", "done"),
            ("superseded-choice", "superseded"),
            ("archived-choice", "archived"),
        ):
            self.add_decision(stem, status)

    def write_generated(self):
        for path, content in generator.build_updates(self.vault).items():
            generator.atomic_write(path, content)

    def index_snapshot(self):
        result = {}
        for path in sorted((self.vault / "50_System/Indexes").glob("*.md")):
            result[path.name] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
        return result


def marked(text, marker):
    begin = f"<!-- BEGIN GENERATED:{marker} -->"
    end = f"<!-- END GENERATED:{marker} -->"
    return text.split(begin, 1)[1].split(end, 1)[0]


class GenerateIndexesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = VaultFixture()
        self.fixture.populate()

    def tearDown(self):
        self.fixture.close()

    def test_first_migration_builds_exact_routes_and_preserves_manual_text(self):
        self.fixture.write_generated()

        global_index = self.fixture.read_index("Global_Memory_Manifest.md")
        global_active = marked(global_index, "global-active-projects")
        global_paused = marked(global_index, "global-paused-projects")
        self.assertIn("[[10_Projects/alpha/Memory_Manifest|alpha]]", global_active)
        self.assertIn("Alpha manifest truth.", global_active)
        self.assertNotIn("paused-one", global_active)
        self.assertIn("[[10_Projects/paused-one/Memory_Manifest|paused-one]]", global_paused)
        self.assertIn("GLOBAL PRELUDE MUST STAY.", global_index)
        self.assertIn("GLOBAL READ POLICY MUST STAY.", global_index)
        self.assertNotIn("10_Projects/stale", global_index)

        project_index = self.fixture.read_index("Project_Index.md")
        active = marked(project_index, "project-active-projects")
        paused = marked(project_index, "project-paused-projects")
        self.assertIn(
            "[[10_Projects/alpha/Alpha_Home|Alpha Exact Home]] · "
            "[[10_Projects/alpha/Memory_Manifest|manifest]]",
            active,
        )
        self.assertNotIn("Alpha manifest truth", active)
        self.assertNotIn("HOME SUMMARY MUST NOT WIN", project_index)
        self.assertNotIn("paused-one", active)
        self.assertIn("10_Projects/paused-one/paused-one", paused)
        self.assertIn("PROJECT POLICY MUST STAY.", project_index)

        session_index = self.fixture.read_index("Session_Index.md")
        canonical = marked(session_index, "session-canonical-roots")
        unassigned = marked(session_index, "session-unassigned-roots")
        self.assertIn(
            "- `60_Sessions/alpha` - 2 files, 2026-01-02 to 2026-02-03.",
            canonical,
        )
        self.assertIn("- `60_Sessions/_general` - 1 file, 2026-01-05.", canonical)
        self.assertIn("- `60_Sessions/legacy-root` - 1 file, 2025-12-31.", unassigned)
        self.assertIn("UNASSIGNED POLICY MUST STAY.", session_index)
        self.assertIn("SESSION TEMPLATE MUST STAY.", session_index)
        self.assertNotIn("60_Sessions/stale", session_index)

        decision_index = self.fixture.read_index("Decision_Index.md")
        active_decisions = marked(decision_index, "decision-active-accepted")
        done_decisions = marked(decision_index, "decision-done")
        archived_decisions = marked(decision_index, "decision-superseded-archived")
        self.assertIn("[[70_Decisions/active-choice|Title active-choice]]", active_decisions)
        self.assertIn("[[70_Decisions/accepted-choice|Title accepted-choice]]", active_decisions)
        self.assertIn("[[70_Decisions/done-choice|Title done-choice]]", done_decisions)
        self.assertIn("70_Decisions/superseded-choice", archived_decisions)
        self.assertIn("70_Decisions/archived-choice", archived_decisions)
        self.assertIn("DECISION POLICY MUST STAY.", decision_index)

        master = self.fixture.read_index("Master_Index.md")
        master_projects = marked(master, "master-project-index-pointer")
        self.assertEqual(master_projects.strip(), "- [[Project_Index|Project Index]]")
        self.assertNotIn("10_Projects/", master_projects)
        self.assertIn("When another project starts, follow the project policy.", master)
        self.assertIn("MASTER RESOURCES MUST STAY.", master)

    def test_write_is_idempotent(self):
        self.fixture.write_generated()
        before = self.fixture.index_snapshot()
        updates = generator.build_updates(self.fixture.vault)
        self.assertEqual(generator.changed_updates(updates), {})
        for path, content in updates.items():
            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertEqual(before, self.fixture.index_snapshot())

    def test_existing_markers_limit_followup_changes_to_generated_blocks(self):
        self.fixture.write_generated()
        project_index = self.fixture.vault / "50_System/Indexes/Project_Index.md"
        manual = project_index.read_text(encoding="utf-8").replace(
            "PROJECT POLICY MUST STAY.", "PROJECT POLICY EDITED BY A HUMAN."
        )
        project_index.write_text(manual, encoding="utf-8")
        home = self.fixture.vault / "10_Projects/alpha/Alpha_Home.md"
        home.write_text(
            home.read_text(encoding="utf-8").replace(
                "# Alpha Exact Home", "# Updated Exact Home"
            ),
            encoding="utf-8",
        )
        update = generator.build_updates(self.fixture.vault)[project_index.resolve()]
        self.assertIn("PROJECT POLICY EDITED BY A HUMAN.", update)
        self.assertIn("Updated Exact Home", marked(update, "project-active-projects"))
        self.assertNotIn("Alpha Exact Home", marked(update, "project-active-projects"))

    def test_check_is_read_only_and_returns_nonzero_on_drift(self):
        before = self.fixture.index_snapshot()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--vault",
                str(self.fixture.vault),
                "--check",
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["changed_count"], 5)
        self.assertEqual(before, self.fixture.index_snapshot())

    def test_cli_write_then_check_and_summary(self):
        write = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--vault",
                str(self.fixture.vault),
                "--write",
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(write.returncode, 0, write.stderr)
        self.assertEqual(json.loads(write.stdout)["status"], "written")
        check = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--vault",
                str(self.fixture.vault),
                "--check",
                "--summary",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("Index generation: clean", check.stdout)

    def test_atomic_write_uses_same_directory_replace_and_preserves_mode(self):
        target = self.fixture.vault / "50_System/Indexes/Project_Index.md"
        os.chmod(target, 0o640)
        original_mode = stat.S_IMODE(target.stat().st_mode)
        real_replace = generator.os.replace
        calls = []

        def recording_replace(source, destination):
            calls.append((Path(source), Path(destination)))
            return real_replace(source, destination)

        with mock.patch.object(generator.os, "replace", side_effect=recording_replace):
            generator.atomic_write(target, "replacement\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "replacement\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), original_mode)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].parent, target.parent)
        self.assertEqual(calls[0][1], target)
        self.assertFalse(calls[0][0].exists())

    def test_template_style_placeholders_are_migrated_without_losing_prose(self):
        session = self.fixture.vault / "50_System/Indexes/Session_Index.md"
        session.write_text(
            "# Session Index\n\nNo project session roots yet.\n\n"
            "Portable session instructions.\n\n## Template\n\nTemplate text.\n",
            encoding="utf-8",
        )
        decision = self.fixture.vault / "50_System/Indexes/Decision_Index.md"
        decision.write_text(
            "# Decision Index\n\nNo decisions yet.\n\n"
            "## Decision Threshold\n\nThreshold text.\n",
            encoding="utf-8",
        )
        updates = generator.build_updates(self.fixture.vault)
        session_update = updates[session.resolve()]
        decision_update = updates[decision.resolve()]
        self.assertNotIn("No project session roots yet.", session_update)
        self.assertIn("Portable session instructions.", session_update)
        self.assertIn("session-canonical-roots", session_update)
        self.assertNotIn("No decisions yet.", decision_update)
        self.assertIn("Threshold text.", decision_update)
        self.assertIn("decision-superseded-archived", decision_update)

    def test_malformed_marker_fails_closed(self):
        path = self.fixture.vault / "50_System/Indexes/Project_Index.md"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n<!-- BEGIN GENERATED:project-active-projects -->\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(generator.GenerationError, "exactly one BEGIN"):
            generator.build_updates(self.fixture.vault)

    def test_unsupported_canonical_status_fails_instead_of_omitting_route(self):
        decision = self.fixture.vault / "70_Decisions/active-choice.md"
        decision.write_text(
            decision.read_text(encoding="utf-8").replace("status: active", "status: draft"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(generator.GenerationError, "unsupported decision status"):
            generator.build_updates(self.fixture.vault)

    def test_symlink_index_escape_is_rejected_without_touching_target(self):
        index = self.fixture.vault / "50_System/Indexes/Project_Index.md"
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "outside.md"
            outside.write_text("OUTSIDE MUST STAY\n", encoding="utf-8")
            index.unlink()
            index.symlink_to(outside)
            with self.assertRaisesRegex(
                generator.GenerationError, "symlink is not allowed"
            ):
                generator.build_updates(self.fixture.vault)
            self.assertEqual(outside.read_text(encoding="utf-8"), "OUTSIDE MUST STAY\n")

    def test_sync_conflict_artifact_blocks_generation(self):
        self.fixture.write(
            "10_Projects/alpha/Memory_Manifest.sync-conflict-20260812.md",
            "conflicting copy\n",
        )
        with self.assertRaisesRegex(
            generator.GenerationError, "Syncthing conflict artifact"
        ):
            generator.build_updates(self.fixture.vault)

    def test_symlink_vault_and_canonical_source_are_rejected(self):
        alias = self.fixture.vault.parent / f"{self.fixture.vault.name}-alias"
        alias.symlink_to(self.fixture.vault, target_is_directory=True)
        try:
            with self.assertRaisesRegex(generator.GenerationError, "vault root.*symlink"):
                generator.build_updates(alias)
        finally:
            alias.unlink()

        session = self.fixture.vault / "60_Sessions/alpha/2099-name.md"
        target = self.fixture.vault / "outside-session.md"
        target.write_text(session.read_text(encoding="utf-8"), encoding="utf-8")
        session.unlink()
        session.symlink_to(target)
        with self.assertRaisesRegex(generator.GenerationError, "symlink is not allowed"):
            generator.build_updates(self.fixture.vault)


if __name__ == "__main__":
    unittest.main()
