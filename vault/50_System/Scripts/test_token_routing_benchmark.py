import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("token-routing-benchmark.py")
SPEC = importlib.util.spec_from_file_location("token_routing_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


def manifest_text(
    slug: str,
    *,
    status: str = "active",
    updated: str = "2026-01-01",
    last_verified: str | None = None,
    read_first: tuple[str, ...] = (),
) -> str:
    links = "\n".join(f"- [[{target}]]" for target in read_first)
    section = f"\n## Read First\n\n{links}\n" if read_first else ""
    return (
        "---\n"
        "type: manifest\n"
        f"status: {status}\n"
        "created: 2026-01-01\n"
        f"updated: {updated}\n"
        + (f"last_verified: {last_verified}\n" if last_verified is not None else "")
        + (
        f"project: {slug}\n"
        "tags: []\n"
        f"summary: Routing manifest for {slug}.\n"
        "---\n\n"
        f"# Memory Manifest: {slug}\n"
        f"{section}"
        )
    )


def project_text(slug: str, body: str = "Current project state.") -> str:
    return (
        "---\n"
        "type: project\n"
        "status: active\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        f"project: {slug}\n"
        "tags: []\n"
        f"summary: Project home for {slug}.\n"
        "---\n\n"
        f"# {slug}\n\n{body}\n"
    )


class VaultFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        for root in (
            "10_Projects",
            "50_System/Indexes",
            "60_Sessions",
            "70_Decisions",
        ):
            (self.vault / root).mkdir(parents=True)
        self.write(
            "50_System/system-manifest.json",
            json.dumps(
                {
                    "user_content_roots": [
                        "10_Projects/",
                        "60_Sessions/",
                        "70_Decisions/",
                    ],
                    "indexes": {
                        "project": "50_System/Indexes/Project_Index.md",
                        "decision": "50_System/Indexes/Decision_Index.md",
                        "session": "50_System/Indexes/Session_Index.md",
                    },
                }
            ),
        )
        self.write("50_System/project-routing.json", '{"workspace_aliases": {}}')
        self.write("50_System/Codex_Instructions.md", "# Instructions\n")
        self.write("50_System/Indexes/Project_Index.md", "# Project Index\n")
        self.write("50_System/Indexes/Master_Index.md", "# Master Index\n")
        self.write(
            "50_System/Indexes/Global_Memory_Manifest.md",
            "# Global Memory Manifest\n",
        )
        self.write("50_System/Indexes/Decision_Index.md", "# Decision Index\n")
        self.write("50_System/Indexes/Session_Index.md", "# Session Index\n")

    def close(self):
        self.temp.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def add_project(
        self,
        slug: str,
        *,
        status: str = "active",
        updated: str = "2026-01-01",
        last_verified: str | None = None,
        read_first: tuple[str, ...] = (),
        with_home: bool = True,
    ) -> tuple[Path, str, str | None]:
        manifest = manifest_text(
            slug,
            status=status,
            updated=updated,
            last_verified=last_verified,
            read_first=read_first,
        )
        manifest_path = self.write(
            f"10_Projects/{slug}/Memory_Manifest.md", manifest
        )
        home = None
        if with_home:
            home = project_text(slug)
            self.write(f"10_Projects/{slug}/{slug}.md", home)
        return manifest_path, manifest, home

    def snapshot(self) -> dict[str, tuple[str, int]]:
        result = {}
        for path in sorted(item for item in self.vault.rglob("*") if item.is_file()):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[path.relative_to(self.vault).as_posix()] = (
                digest,
                path.stat().st_mtime_ns,
            )
        return result


class TokenRoutingBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.fixture = VaultFixture()

    def tearDown(self):
        self.fixture.close()

    def test_manifest_home_read_first_and_declared_staleness(self):
        decision = "# Decision\n\nA durable decision.\n"
        self.fixture.write("70_Decisions/alpha-choice.md", decision)
        manifest_path, manifest, home = self.fixture.add_project(
            "alpha",
            updated="2026-01-01",
            read_first=(
                "10_Projects/alpha/alpha|Alpha home",
                "70_Decisions/alpha-choice|Decision",
            ),
        )
        self.fixture.add_project(
            "paused", status="paused", updated="2025-01-01"
        )

        report = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 2, 15), stale_days=30
        )
        alpha = next(
            item for item in report["projects"]["items"] if item["project"] == "alpha"
        )

        self.assertEqual(
            alpha["manifest"]["path"],
            manifest_path.relative_to(self.fixture.vault).as_posix(),
        )
        self.assertEqual(
            alpha["manifest"]["estimated_tokens"],
            benchmark.estimated_tokens(len(manifest)),
        )
        self.assertEqual(
            alpha["manifest_plus_home"]["estimated_tokens"],
            benchmark.estimated_tokens(len(manifest) + len(home)),
        )
        self.assertEqual(
            alpha["read_first"]["total_estimated_tokens_with_manifest"],
            benchmark.estimated_tokens(len(manifest) + len(home) + len(decision)),
        )
        self.assertEqual(alpha["read_first"]["resolved_unique_notes"], 2)
        self.assertEqual(report["projects"]["costs"]["active_project_count"], 1)
        self.assertEqual(report["staleness"]["stale_projects"], ["alpha"])
        self.assertEqual(report["staleness"]["stale_count"], 1)
        self.assertEqual(alpha["freshness"]["basis"], "frontmatter.updated")
        self.assertTrue(alpha["freshness"]["fallback_used"])
        self.assertEqual(
            report["staleness"]["fallback_reasons"],
            {"missing_last_verified": 1},
        )
        self.assertFalse(report["staleness"]["filesystem_mtime_used"])

    def test_valid_last_verified_wins_and_invalid_value_falls_back(self):
        self.fixture.add_project(
            "verified",
            updated="2025-01-01",
            last_verified="2026-02-10",
            with_home=False,
        )
        self.fixture.add_project(
            "fallback",
            updated="2026-02-01",
            last_verified="not-a-date",
            with_home=False,
        )

        report = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 2, 15), stale_days=30
        )
        items = {item["project"]: item for item in report["projects"]["items"]}
        verified = items["verified"]["freshness"]
        fallback = items["fallback"]["freshness"]

        self.assertEqual(verified["basis"], "frontmatter.last_verified")
        self.assertEqual(verified["date"], "2026-02-10")
        self.assertEqual(verified["age_days"], 5)
        self.assertFalse(verified["fallback_used"])
        self.assertFalse(verified["stale"])

        self.assertEqual(fallback["basis"], "frontmatter.updated")
        self.assertEqual(fallback["date"], "2026-02-01")
        self.assertTrue(fallback["fallback_used"])
        self.assertEqual(fallback["fallback_reason"], "invalid_last_verified")
        self.assertEqual(
            report["staleness"]["basis_counts"],
            {"frontmatter.last_verified": 1, "frontmatter.updated": 1},
        )
        self.assertEqual(report["staleness"]["fallback_count"], 1)

    def test_relative_read_first_link_resolves_inside_vault(self):
        session = "# Session\n\nCompact handoff.\n"
        self.fixture.write("60_Sessions/alpha/handoff.v1.md", session)
        _, manifest, _ = self.fixture.add_project(
            "alpha",
            read_first=("../../60_Sessions/alpha/handoff.v1|Handoff",),
        )

        report = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )
        alpha = report["projects"]["items"][0]
        self.assertEqual(alpha["read_first"]["resolved_unique_notes"], 1)
        self.assertEqual(
            alpha["read_first"]["total_estimated_tokens_with_manifest"],
            benchmark.estimated_tokens(len(manifest) + len(session)),
        )

    def test_index_drift_detects_catalog_decision_and_session_gaps(self):
        self.fixture.add_project("alpha", with_home=False)
        self.fixture.add_project("beta", with_home=False)
        self.fixture.write(
            "50_System/Indexes/Project_Index.md",
            "\n".join(
                (
                    "[[10_Projects/alpha/Memory_Manifest|Alpha]]",
                    "[[10_Projects/ghost/Memory_Manifest|Ghost]]",
                )
            ),
        )
        self.fixture.write("70_Decisions/d1.md", "# D1\n")
        self.fixture.write("70_Decisions/d2.md", "# D2\n")
        self.fixture.write("70_Decisions/README.md", "# Infrastructure note\n")
        self.fixture.write(
            "50_System/Indexes/Decision_Index.md",
            "[[70_Decisions/d1|D1]]\n[[70_Decisions/ghost|Ghost]]\n",
        )
        self.fixture.write("60_Sessions/alpha/2026-01-01.md", "# One\n")
        self.fixture.write("60_Sessions/alpha/2026-01-02.md", "# Two\n")
        self.fixture.write("60_Sessions/beta/2026-01-01.md", "# Three\n")
        self.fixture.write(
            "50_System/Indexes/Session_Index.md",
            "- `60_Sessions/alpha` - 1 file.\n"
            "- `60_Sessions/ghost` - 5 files.\n",
        )

        report = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )
        drift = report["index_drift"]
        projects = drift["project_catalogs"]["project_index"]
        self.assertEqual(projects["missing_all_projects"], ["beta"])
        self.assertEqual(projects["extra_projects"], ["ghost"])
        self.assertEqual(drift["decisions"]["missing"], ["70_Decisions/d2"])
        self.assertEqual(drift["decisions"]["extra"], ["70_Decisions/ghost"])
        self.assertEqual(drift["sessions"]["missing_roots"], ["beta"])
        self.assertEqual(drift["sessions"]["extra_roots"], ["ghost"])
        self.assertEqual(
            drift["sessions"]["count_mismatches"],
            [{"root": "alpha", "claimed": 1, "actual": 2, "delta": 1}],
        )

    def test_master_generated_pointer_and_legacy_catalog_semantics(self):
        self.fixture.add_project("alpha", with_home=False)
        self.fixture.add_project("beta", with_home=False)
        self.fixture.write(
            "50_System/Indexes/Master_Index.md",
            "# Master Index\n\n## Active Projects\n\n"
            "<!-- BEGIN GENERATED:master-project-index-pointer -->\n"
            "- [[Project_Index|Project Index]]\n"
            "<!-- END GENERATED:master-project-index-pointer -->\n",
        )
        generated = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )["index_drift"]["project_catalogs"]["master_index"]
        self.assertEqual(generated["mode"], "generated_pointer")
        self.assertEqual(generated["expected_scope"], "project_index_pointer")
        self.assertTrue(generated["pointer_exact"])
        self.assertEqual(generated["pointer_issues"], [])
        self.assertEqual(generated["missing_active_projects"], [])

        self.fixture.write(
            "50_System/Indexes/Master_Index.md",
            "# Legacy Master Index\n\n"
            "[[10_Projects/alpha/Memory_Manifest|Alpha]]\n",
        )
        legacy = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )["index_drift"]["project_catalogs"]["master_index"]
        self.assertEqual(legacy["mode"], "legacy_catalog")
        self.assertEqual(legacy["expected_scope"], "active_projects")
        self.assertEqual(legacy["missing_active_projects"], ["beta"])

        self.fixture.write(
            "50_System/Indexes/Master_Index.md",
            "# Broken Master Index\n\n"
            "<!-- BEGIN GENERATED:master-project-index-pointer -->\n"
            "- [[Decision_Index|Wrong]]\n"
            "<!-- END GENERATED:master-project-index-pointer -->\n",
        )
        broken = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )["index_drift"]
        master = broken["project_catalogs"]["master_index"]
        self.assertFalse(master["pointer_exact"])
        self.assertEqual(
            master["pointer_issues"],
            ["missing_project_index_pointer", "wrong_pointer_target"],
        )
        self.assertGreaterEqual(broken["drift_item_count"], 1)

    def test_hashes_are_stable_and_analysis_does_not_write(self):
        self.fixture.add_project("alpha")
        before = self.fixture.snapshot()
        first = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )
        after = self.fixture.snapshot()
        second = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )

        self.assertEqual(before, after)
        self.assertEqual(
            first["content_preservation"], second["content_preservation"]
        )

        original = first["content_preservation"]
        self.fixture.write(
            "10_Projects/alpha/alpha.md", project_text("alpha", "Changed detail.")
        )
        changed = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )["content_preservation"]
        self.assertNotEqual(
            original["all_user_content"]["sha256"],
            changed["all_user_content"]["sha256"],
        )
        self.assertNotEqual(
            original["project_content_excluding_manifests"]["sha256"],
            changed["project_content_excluding_manifests"]["sha256"],
        )
        self.assertEqual(
            original["project_manifests"]["sha256"],
            changed["project_manifests"]["sha256"],
        )
        self.assertEqual(
            original["routing_surfaces"]["sha256"],
            changed["routing_surfaces"]["sha256"],
        )

    def test_configured_index_path_cannot_escape_vault(self):
        self.fixture.add_project("alpha", with_home=False)
        self.fixture.write(
            "50_System/system-manifest.json",
            json.dumps(
                {
                    "user_content_roots": ["10_Projects/"],
                    "indexes": {
                        "project": "/etc/passwd",
                        "decision": "../../outside-decisions.md",
                        "session": "50_System/Indexes/Session_Index.md",
                    },
                }
            ),
        )

        report = benchmark.analyze_vault(
            self.fixture.vault, as_of=date(2026, 1, 2)
        )
        drift = report["index_drift"]
        self.assertEqual(
            drift["project_catalogs"]["project_index"]["path"],
            "50_System/Indexes/Project_Index.md",
        )
        self.assertEqual(
            drift["decisions"]["path"],
            "50_System/Indexes/Decision_Index.md",
        )

    def test_cli_json_and_summary_are_read_only(self):
        self.fixture.add_project("alpha")
        before = self.fixture.snapshot()
        json_run = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--vault",
                str(self.fixture.vault),
                "--as-of",
                "2026-01-02",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        parsed = json.loads(json_run.stdout)
        self.assertTrue(parsed["read_only"])
        self.assertEqual(parsed["token_estimator"], "ceil(chars/4)")

        summary_run = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--vault",
                str(self.fixture.vault),
                "--as-of",
                "2026-01-02",
                "--summary",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Context Vault token routing benchmark", summary_run.stdout)
        self.assertIn("basis=frontmatter.last_verified", summary_run.stdout)
        self.assertIn("fallback=frontmatter.updated", summary_run.stdout)
        self.assertIn("mtime=unused", summary_run.stdout)
        self.assertIn("Content-preservation SHA-256", summary_run.stdout)
        self.assertEqual(before, self.fixture.snapshot())


if __name__ == "__main__":
    unittest.main()
