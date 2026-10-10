import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("vault-audit.py")
SPEC = importlib.util.spec_from_file_location("vault_audit", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class VaultAuditRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        (self.vault / "10_Projects" / "alpha-project").mkdir(parents=True)
        (self.vault / "10_Projects" / "alpha-project" / "Memory_Manifest.md").write_text(
            "---\nproject: alpha-project\n---\n", encoding="utf-8"
        )
        (self.vault / "50_System").mkdir()
        self.manifest = {"project_routing": "50_System/project-routing.json"}

    def tearDown(self):
        self.temp.cleanup()

    def write_registry(self, aliases):
        (self.vault / "50_System" / "project-routing.json").write_text(
            json.dumps({"workspace_aliases": aliases}), encoding="utf-8"
        )

    def write_indexes(self, project="", decision="", session=""):
        root = self.vault / "50_System/Indexes"
        root.mkdir(parents=True, exist_ok=True)
        (root / "Project_Index.md").write_text(project, encoding="utf-8")
        (root / "Decision_Index.md").write_text(decision, encoding="utf-8")
        (root / "Session_Index.md").write_text(session, encoding="utf-8")

    def test_valid_registry_has_no_issues(self):
        self.write_registry({"Alpha Workspace": "alpha-project"})
        issues = []
        audit.audit_project_routing(self.vault, self.manifest, issues)
        self.assertEqual(issues, [])

    def test_registry_rejects_collision_and_missing_target(self):
        self.write_registry(
            {"Alpha Workspace": "alpha-project", "alpha_workspace": "missing-project"}
        )
        issues = []
        audit.audit_project_routing(self.vault, self.manifest, issues)
        messages = "\n".join(item.message for item in issues)
        self.assertIn("normalize to", messages)
        self.assertIn("missing manifest", messages)

    def test_registry_rejects_escaping_and_shadowed_targets(self):
        external = self.vault / "external-target"
        external.mkdir()
        (external / "Memory_Manifest.md").write_text("ok", encoding="utf-8")
        other = self.vault / "10_Projects/other-project"
        other.mkdir()
        (other / "Memory_Manifest.md").write_text(
            "---\nproject: other-project\n---\n", encoding="utf-8"
        )
        self.write_registry(
            {
                "External": str(external),
                "alpha_project": "other-project",
            }
        )
        issues = []
        audit.audit_project_routing(self.vault, self.manifest, issues)
        messages = "\n".join(item.message for item in issues)
        self.assertIn("one canonical project slug", messages)
        self.assertIn("shadowed by canonical project", messages)

    def test_registry_v2_controls_match_resolver_contract(self):
        self.write_registry({"Alpha": "alpha-project"})
        registry_path = self.vault / "50_System/project-routing.json"
        registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "workspace_roots": ["~/Projects", "relative/root"],
                    "workspace_bindings": {
                        "~/bound": "alpha-project",
                        "relative": "missing_project",
                    },
                    "workspace_aliases": {"Alpha": "alpha-project"},
                }
            ),
            encoding="utf-8",
        )
        issues = []
        audit.audit_project_routing(self.vault, self.manifest, issues)
        messages = "\n".join(item.message for item in issues)
        self.assertIn("workspace root", messages)
        self.assertIn("binding paths", messages)
        self.assertIn("non-canonical target", messages)

    def test_repeated_separator_normalization_matches_resolver(self):
        self.assertEqual(audit.normalize_slug("__My__Codex___Bar__"), "my-codex-bar")
        self.assertEqual(audit.normalize_slug("___"), "")

    def test_malformed_registry_is_an_error(self):
        (self.vault / "50_System" / "project-routing.json").write_text(
            "{", encoding="utf-8"
        )
        issues = []
        audit.audit_project_routing(self.vault, self.manifest, issues)
        self.assertEqual([item.check for item in issues], ["project-routing"])

    def test_manifest_project_must_match_folder(self):
        manifest = self.vault / "10_Projects" / "alpha-project" / "Memory_Manifest.md"
        manifest.write_text("---\nproject: wrong\n---\n", encoding="utf-8")
        issues = []
        audit.audit_canonical_project_routing(self.vault, issues)
        self.assertEqual([item.check for item in issues], ["project-slug"])

    def test_missing_manifest_and_normalized_project_collision_are_errors(self):
        orphan = self.vault / "10_Projects/orphan"
        orphan.mkdir()
        (orphan / "orphan.md").write_text("---\nproject: orphan\n---\n")
        colliding = self.vault / "10_Projects/alpha_project"
        colliding.mkdir()
        (colliding / "Memory_Manifest.md").write_text(
            "---\nproject: alpha_project\n---\n", encoding="utf-8"
        )
        issues = []
        audit.audit_canonical_project_routing(self.vault, issues)
        checks = [item.check for item in issues]
        self.assertIn("project-manifest", checks)
        self.assertIn("project-slug", checks)

    def test_session_uses_declared_project_root(self):
        wrong_root = self.vault / "60_Sessions" / "alpha_project"
        wrong_root.mkdir(parents=True)
        (wrong_root / "2026-08-09.md").write_text(
            "---\nproject: alpha-project\n---\n", encoding="utf-8"
        )
        issues = []
        audit.audit_canonical_project_routing(self.vault, issues)
        self.assertEqual([item.check for item in issues], ["session-root"])

    def test_unknown_session_fails_closed_but_allowlisted_legacy_is_allowed(self):
        unknown = self.vault / "60_Sessions/typo"
        unknown.mkdir(parents=True)
        (unknown / "2026-08-12.md").write_text(
            "---\nproject: typo\n---\n", encoding="utf-8"
        )
        registered = self.vault / "60_Sessions/legacy-root"
        registered.mkdir()
        (registered / "2026-08-12.md").write_text(
            "---\nproject: _unassigned\n---\n", encoding="utf-8"
        )
        self.write_indexes(session="- `60_Sessions/legacy-root` - reviewed\n")
        issues = []
        manifest = {
            "validation": {
                "allowed_legacy_session_roots": ["legacy-root"],
                "session_project_sentinels": ["_general", "_unassigned"],
            }
        }
        audit.audit_canonical_project_routing(self.vault, issues, manifest)
        session_paths = [item.path for item in issues if item.check == "session-root"]
        self.assertTrue(any("60_Sessions/typo" in path for path in session_paths))
        self.assertFalse(any("legacy-root" in path for path in session_paths))

    def test_index_membership_does_not_allow_unknown_session_root(self):
        root = self.vault / "60_Sessions/indexed-typo"
        root.mkdir(parents=True)
        (root / "2026-08-12.md").write_text(
            "---\nproject: indexed-typo\n---\n", encoding="utf-8"
        )
        self.write_indexes(session="- `60_Sessions/indexed-typo` - typo\n")
        issues = []
        audit.audit_canonical_project_routing(
            self.vault,
            issues,
            {"validation": {"allowed_legacy_session_roots": []}},
        )
        self.assertIn("session-root", [item.check for item in issues])

    def test_general_session_requires_explicit_general_sentinel(self):
        root = self.vault / "60_Sessions/_general"
        root.mkdir(parents=True)
        (root / "2026-08-12.md").write_text(
            "---\nproject:\n---\n", encoding="utf-8"
        )
        issues = []
        audit.audit_canonical_project_routing(
            self.vault,
            issues,
            {
                "validation": {
                    "session_project_sentinels": ["_general", "_unassigned"]
                }
            },
        )
        self.assertIn("session-root", [item.check for item in issues])

    def test_blank_project_in_canonical_session_root_is_an_error(self):
        root = self.vault / "60_Sessions/alpha-project"
        root.mkdir(parents=True)
        (root / "2026-08-12.md").write_text(
            "---\nproject:\n---\n", encoding="utf-8"
        )
        issues = []
        audit.audit_canonical_project_routing(self.vault, issues)
        self.assertIn("session-root", [item.check for item in issues])

    def test_host_specific_root_daily_logs_are_rejected(self):
        root = self.vault / "60_Sessions"
        root.mkdir()
        (root / "README.md").write_text("documentation", encoding="utf-8")
        for name in (
            "2026-08-12.md",
            "2026-08-12--linux.md",
            "2026-08-12--local.md",
            "other-root-note.md",
        ):
            (root / name).write_text("history", encoding="utf-8")
        self.write_indexes(
            project="[[10_Projects/alpha-project/Memory_Manifest]]\n"
        )
        issues = []
        audit.audit_indexes(self.vault, {}, issues)
        self.assertEqual(
            4, sum(item.check == "root-session-log" for item in issues)
        )
        root_paths = {
            item.path for item in issues if item.check == "root-session-log"
        }
        self.assertNotIn("60_Sessions/README.md", root_paths)
        self.assertIn("60_Sessions/other-root-note.md", root_paths)

    def test_project_session_host_suffixes_and_handoffs_are_valid(self):
        root = self.vault / "60_Sessions/alpha-project"
        root.mkdir(parents=True)
        for name in (
            "2026-08-12.md",
            "2026-08-12--linux.md",
            "2026-08-12--local.md",
            "2026-08-12--windows.md",
            "2026-08-12-1418-handoff.md",
        ):
            (root / name).write_text("history", encoding="utf-8")
        issues = []
        audit.audit_session_filenames(self.vault, issues)
        self.assertEqual(issues, [])

        (root / "2026-08-12--unknown-host.md").write_text(
            "history", encoding="utf-8"
        )
        audit.audit_session_filenames(self.vault, issues)
        self.assertEqual([item.check for item in issues], ["session-filename"])

    def test_indexes_require_exact_routes(self):
        self.write_indexes(project="mapping alpha-projectish\n")
        issues = []
        audit.audit_indexes(self.vault, {}, issues)
        self.assertIn("project-index", [item.check for item in issues])

        self.write_indexes(
            project="[[10_Projects/alpha-project/Memory_Manifest|manifest]]\n"
        )
        issues = []
        audit.audit_indexes(self.vault, {}, issues)
        self.assertNotIn("project-index", [item.check for item in issues])

    def test_indexes_require_general_and_reject_stale_exact_routes(self):
        general = self.vault / "60_Sessions/_general"
        general.mkdir(parents=True)
        (general / "2026-08-12.md").write_text(
            "---\nproject: _general\n---\n", encoding="utf-8"
        )
        self.write_indexes(
            project=(
                "[[10_Projects/alpha-project/Memory_Manifest]]\n"
                "[[10_Projects/stale/Memory_Manifest]]\n"
            ),
            session="`60_Sessions/stale`\n",
        )
        issues = []
        audit.audit_indexes(self.vault, {}, issues)
        messages = "\n".join(item.message for item in issues)
        self.assertIn("60_Sessions/_general", "\n".join(item.path for item in issues))
        self.assertIn("stale or noncanonical project route", messages)
        self.assertIn("stale or noncanonical session-root route", messages)

    def test_generated_session_membership_ignores_manual_policy_examples(self):
        general = self.vault / "60_Sessions/_general"
        general.mkdir(parents=True)
        (general / "2026-08-12.md").write_text(
            "---\nproject: _general\n---\n", encoding="utf-8"
        )
        session = (
            "Policy example: `60_Sessions/YYYY-MM-DD.md`.\n"
            "<!-- BEGIN GENERATED:session-canonical-roots -->\n"
            "- `60_Sessions/_general` - current.\n"
            "<!-- END GENERATED:session-canonical-roots -->\n"
            "<!-- BEGIN GENERATED:session-unassigned-roots -->\n"
            "<!-- END GENERATED:session-unassigned-roots -->\n"
        )
        self.write_indexes(
            project="[[10_Projects/alpha-project/Memory_Manifest]]\n",
            session=session,
        )
        issues = []
        audit.audit_indexes(self.vault, {}, issues)
        messages = "\n".join(item.message for item in issues)
        self.assertNotIn("YYYY-MM-DD", messages)
        self.assertNotIn("session-index", [item.check for item in issues])

    def test_generated_index_markers_fail_closed_when_incomplete(self):
        self.write_indexes(
            project="[[10_Projects/alpha-project/Memory_Manifest]]\n",
            session=(
                "<!-- BEGIN GENERATED:session-canonical-roots -->\n"
                "- `60_Sessions/fake`\n"
                "<!-- END GENERATED:session-canonical-roots -->\n"
            ),
        )
        issues = []
        audit.audit_indexes(self.vault, {}, issues)
        marker_issues = [
            item for item in issues
            if item.check == "session-index" and "marker" in item.message
        ]
        self.assertEqual(len(marker_issues), 1)

    def test_path_qualified_wikilink_cannot_fall_back_to_unrelated_leaf(self):
        source = self.vault / "source.md"
        paths = {"elsewhere/foo"}
        leaves = {"foo"}
        self.assertFalse(
            audit.wikilink_exists(
                self.vault, source, "missing/path/foo", paths, leaves, set()
            )
        )
        self.assertTrue(
            audit.wikilink_exists(self.vault, source, "foo", paths, leaves, set())
        )

    def test_non_object_telemetry_is_reported_without_crashing(self):
        root = self.vault / "50_System/Telemetry"
        root.mkdir()
        (root / "codex-memory-events.jsonl").write_text(
            'null\n[]\n{"event":"codex_read"}\n', encoding="utf-8"
        )
        issues = []
        audit.audit_telemetry(self.vault, issues)
        self.assertEqual(
            2,
            sum(
                item.check == "telemetry-schema" and item.severity == "ERROR"
                for item in issues
            ),
        )

    def test_manifest_shape_errors_are_reported_and_downstream_is_safe(self):
        policy = self.vault / "policy.md"
        policy.write_text("policy", encoding="utf-8")
        self.write_indexes()
        manifest = {
            "system": "context-vault",
            "schema_version": 1,
            "canonical_policy": "policy.md",
            "project_routing": "50_System/project-routing.json",
            "note_types": {
                "common": {"required": []},
                "session": {"required": [], "exact_type": []},
                "decision": {
                    "required": [],
                    "accepted_types": ["decision", "adr"],
                    "status_aliases": {"old": 1},
                },
            },
            "path_rules": None,
            "indexes": {
                "project": "50_System/Indexes/Project_Index.md",
                "decision": "50_System/Indexes/Decision_Index.md",
                "session": "50_System/Indexes/Session_Index.md",
            },
            "validation": {"ignore_markdown": []},
        }
        issues = []
        audit.audit_system_manifest(self.vault, manifest, issues)
        messages = "\n".join(item.message for item in issues)
        self.assertIn("path_rules must be an array", messages)
        self.assertIn("exact_type must be a non-empty string", messages)
        self.assertIn("accepted_types requires exact_type", messages)
        self.assertIn("status_aliases must map", messages)
        self.assertIsNone(audit.matching_rule("note.md", manifest))

    def test_optional_note_type_constraints_are_applied_only_when_configured(self):
        configured = {
            "note_types": {
                "common": {
                    "required": [],
                    "nonempty": ["summary", "tags"],
                    "date_fields": ["created"],
                },
                "session": {
                    "required": [],
                    "exact_type": "session",
                    "status_values": ["active", "done"],
                },
            }
        }
        rule = {"note_type": "session"}
        fm = audit.frontmatter(
            "---\ntype: decision\nstatus: invalid\ncreated: tomorrow\n"
            "summary:\ntags:\n  - daily-log\n---\n"
        )
        assert fm is not None
        problems = audit.frontmatter_schema_problems(fm, rule, configured)
        joined = "\n".join(problems)
        self.assertIn("type must be", joined)
        self.assertIn("status must be", joined)
        self.assertIn("summary must be non-empty", joined)
        self.assertIn("created must be an ISO date", joined)
        self.assertNotIn("tags must be non-empty", joined)
        self.assertEqual(
            audit.frontmatter_schema_problems(fm, rule, {"note_types": {}}), []
        )

    def test_optional_last_verified_date_is_checked_only_when_present(self):
        configured = {
            "note_types": {
                "common": {
                    "required": ["created", "updated"],
                    "date_fields": ["created", "updated", "last_verified"],
                }
            }
        }
        missing = audit.frontmatter(
            "---\ncreated: 2026-08-12\nupdated: 2026-08-12\n---\n"
        )
        assert missing is not None
        self.assertEqual(
            audit.frontmatter_schema_problems(missing, None, configured), []
        )
        invalid = audit.frontmatter(
            "---\ncreated: 2026-08-12\nupdated: 2026-08-12\n"
            "last_verified: someday\n---\n"
        )
        assert invalid is not None
        self.assertIn(
            "last_verified must be an ISO date",
            "\n".join(audit.frontmatter_schema_problems(invalid, None, configured)),
        )

    def test_date_placeholder_is_allowed_only_in_template_paths(self):
        configured = {
            "note_types": {
                "common": {
                    "required": ["created", "updated"],
                    "date_fields": ["created", "updated", "last_verified"],
                }
            }
        }
        fm = audit.frontmatter(
            "---\ncreated: YYYY-MM-DD\nupdated: YYYY-MM-DD\n"
            "last_verified: YYYY-MM-DD\n---\n"
        )
        assert fm is not None
        self.assertEqual(
            audit.frontmatter_schema_problems(
                fm,
                None,
                configured,
                relpath="50_System/Templates/Memory_Manifest.md",
            ),
            [],
        )
        for relpath in (
            "10_Projects/demo/Memory_Manifest.md",
            "50_System/Templates-copy/Memory_Manifest.md",
        ):
            with self.subTest(relpath=relpath):
                problems = audit.frontmatter_schema_problems(
                    fm, None, configured, relpath=relpath
                )
                self.assertEqual(len(problems), 3)
                self.assertTrue(
                    all("must be an ISO date" in problem for problem in problems)
                )

    def test_template_placeholder_does_not_disable_required_or_syntax_checks(self):
        template_root = self.vault / "50_System/Templates"
        template_root.mkdir(parents=True)
        valid = template_root / "Resource.md"
        valid.write_text(
            "---\ntype: resource\nstatus: active\ncreated: YYYY-MM-DD\n"
            "updated: YYYY-MM-DD\nsummary: Template\n---\n",
            encoding="utf-8",
        )
        broken = template_root / "Broken.md"
        broken.write_text(
            "---\ntype: resource\nstatus: active\ncreated: YYYY-MM-DD\n"
            "summary: ambiguous: mapping\n---\n",
            encoding="utf-8",
        )
        real = self.vault / "30_Resources/real-note.md"
        real.parent.mkdir(parents=True)
        real.write_text(valid.read_text(encoding="utf-8"), encoding="utf-8")
        configured = {
            "note_types": {
                "common": {
                    "required": ["type", "status", "created", "updated", "summary"],
                    "date_fields": ["created", "updated"],
                }
            },
            "validation": {"ignore_markdown": []},
        }
        issues = []
        audit.audit_markdown(
            self.vault, [valid, broken, real], 12000, configured, issues
        )
        valid_issues = [item for item in issues if item.path.endswith("Resource.md")]
        self.assertEqual(valid_issues, [])
        broken_checks = {
            item.check for item in issues if item.path.endswith("Broken.md")
        }
        self.assertIn("frontmatter", broken_checks)
        self.assertIn("frontmatter-syntax", broken_checks)
        real_schema = [
            item
            for item in issues
            if item.path == "30_Resources/real-note.md"
            and item.check == "frontmatter-schema"
        ]
        self.assertEqual(len(real_schema), 2)

    def test_frontmatter_strict_subset_rejects_ambiguous_shapes(self):
        text = (
            "---\nsummary: unquoted: mapping\nsummary: duplicate\n"
            "owner:\n  nested: mapping\n---\n"
        )
        problems = "\n".join(audit.frontmatter_syntax_problems(text))
        self.assertIn("colon-space", problems)
        self.assertIn("duplicate", problems)
        self.assertIn("only string-list", problems)

    def test_accepted_legacy_type_and_status_alias_are_info_only(self):
        configured = {
            "note_types": {
                "common": {"required": []},
                "session": {
                    "required": [],
                    "exact_type": "session",
                    "accepted_types": ["session", "daily-log"],
                    "status_values": ["active", "done"],
                    "status_aliases": {"complete": "done"},
                },
            }
        }
        fm = audit.frontmatter(
            "---\ntype: daily-log\nstatus: complete\n---\n"
        )
        assert fm is not None
        findings = audit.frontmatter_schema_findings(
            fm, {"note_type": "session"}, configured
        )
        self.assertEqual(
            [(severity, check) for severity, check, _ in findings],
            [("INFO", "legacy-type"), ("INFO", "legacy-type")],
        )
        self.assertEqual(
            audit.frontmatter_schema_problems(
                fm, {"note_type": "session"}, configured
            ),
            [],
        )

    def test_unaccepted_type_and_status_remain_errors(self):
        configured = {
            "note_types": {
                "common": {"required": []},
                "session": {
                    "required": [],
                    "exact_type": "session",
                    "accepted_types": ["session", "daily-log"],
                    "status_values": ["active", "done"],
                    "status_aliases": {"complete": "done"},
                },
            }
        }
        fm = audit.frontmatter("---\ntype: decision\nstatus: broken\n---\n")
        assert fm is not None
        findings = audit.frontmatter_schema_findings(
            fm, {"note_type": "session"}, configured
        )
        self.assertEqual(
            2, sum(severity == "ERROR" for severity, _, _ in findings)
        )

    def test_excluded_artifacts_stay_out_of_scans_but_conflicts_are_visible(self):
        versions = self.vault / ".stversions"
        versions.mkdir()
        conflict = versions / "note.sync-conflict-host.bin"
        conflict.write_bytes(b"conflict")
        (versions / ".DS_Store").write_bytes(b"metadata")
        self.assertNotIn(conflict, audit.markdown_files(self.vault))

        issues = []
        audit.audit_ds_store(self.vault, issues)
        self.assertNotIn("ds-store", [item.check for item in issues])
        audit.audit_sync_conflicts(self.vault, issues)
        conflicts = [item for item in issues if item.check == "sync-conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].severity, "ERROR")

    def test_invalid_baseline_is_safe_and_does_not_allow_debt(self):
        baseline = {
            "system": "wrong",
            "schema_version": 1,
            "mode": "legacy-debt-ratchet",
            "counts": {"ERROR:frontmatter": "many"},
        }
        self.assertTrue(audit.baseline_schema_problems(baseline))
        issue = audit.Issue("ERROR", "frontmatter", "note.md", "missing")
        self.assertEqual(
            audit.ratchet_regressions([issue], baseline),
            {"ERROR:frontmatter": (1, 0)},
        )
        forged = {
            "system": "wrong",
            "schema_version": 1,
            "captured": "2026-08-12",
            "mode": "legacy-debt-ratchet",
            "counts": {"ERROR:frontmatter": 999},
        }
        self.assertEqual(
            audit.ratchet_regressions([issue], forged),
            {"ERROR:frontmatter": (1, 0)},
        )

    def test_valid_baseline_requires_reviewed_mode_and_capture_date(self):
        baseline = {
            "system": "context-vault",
            "schema_version": 1,
            "captured": "2026-08-12",
            "mode": "legacy-debt-ratchet",
            "counts": {},
        }
        self.assertEqual(audit.baseline_schema_problems(baseline), [])

    def test_write_baseline_produces_non_approvable_proposal_and_no_overwrite(self):
        proposal = self.vault / "proposal.json"
        first = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--vault-path",
                str(self.vault),
                "--write-baseline",
                str(proposal),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertIn(first.returncode, {0, 1})
        document = json.loads(proposal.read_text(encoding="utf-8"))
        self.assertEqual(document["mode"], "audit-baseline-proposal")
        self.assertTrue(document["review_required"])
        self.assertTrue(audit.baseline_schema_problems(document))
        second = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--vault-path",
                str(self.vault),
                "--write-baseline",
                str(proposal),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(second.returncode, 2)
        self.assertIn("Refusing to overwrite", second.stderr)

    def test_write_baseline_cannot_self_approve_ratchet(self):
        baseline = self.vault / "baseline.json"
        baseline.write_text("sentinel", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--vault-path",
                str(self.vault),
                "--ratchet",
                "--write-baseline",
                str(baseline),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("cannot be combined", completed.stderr)
        self.assertEqual(baseline.read_text(encoding="utf-8"), "sentinel")

    def test_zero_baseline_rejects_new_debt(self):
        issue = audit.Issue("ERROR", "frontmatter", "note.md", "missing")
        self.assertEqual(
            audit.ratchet_regressions([issue], {"counts": {}}),
            {"ERROR:frontmatter": (1, 0)},
        )


if __name__ == "__main__":
    unittest.main()
