from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skill"
MODULE_PATH = SKILL_ROOT / "validate_vault_write.py"
if not MODULE_PATH.exists():
    MODULE_PATH = SKILL_ROOT / "scripts/validate_vault_write.py"
SPEC = importlib.util.spec_from_file_location("validate_vault_write", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "50_System/Indexes").mkdir(parents=True)
        (self.root / "50_System/Indexes/Project_Index.md").write_text(
            "demo\n", encoding="utf-8"
        )
        (self.root / "50_System/Indexes/Session_Index.md").write_text(
            "demo\n", encoding="utf-8"
        )
        self.manifest = {
            "system": "context-vault",
            "schema_version": 1,
            "canonical_policy": "50_System/Codex_Instructions.md",
            "project_routing": "50_System/project-routing.json",
            "infrastructure": [
                ".obsidian/**",
                "50_System/**",
                "AGENTS.md",
                "README.md",
                "Welcome.md",
            ],
            "note_types": {
                "common": {"required": ["type", "status", "created", "updated", "tags", "summary"]},
                "manifest": {"required": ["project"]},
                "session": {"required": ["project"]},
            },
            "path_rules": [
                {
                    "glob": "10_Projects/*/Memory_Manifest.md",
                    "note_type": "manifest",
                    "max_chars": 12000,
                },
                {
                    "glob": "60_Sessions/*/*.md",
                    "note_type": "session",
                    "max_chars": 12000,
                },
            ],
            "indexes": {
                "project": "50_System/Indexes/Project_Index.md",
                "session": "50_System/Indexes/Session_Index.md",
            },
            "validation": {"ignore_markdown": ["README.md"]},
        }
        (self.root / "50_System/system-manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )
        (self.root / "50_System/project-routing.json").write_text(
            json.dumps({"schema_version": 1, "workspace_aliases": {}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_main(
        self,
        payload: object | None = None,
        *,
        raw: str | None = None,
        vault: Path | None = None,
    ) -> tuple[int, str]:
        stdin = io.StringIO(raw if raw is not None else json.dumps(payload))
        stdout = io.StringIO()
        with patch.object(MODULE.sys, "stdin", stdin), patch.dict(
            MODULE.os.environ,
            {"CODEX_CONTEXT_VAULT": str(vault or self.root)},
            clear=False,
        ), redirect_stdout(stdout):
            code = MODULE.main()
        return code, stdout.getvalue()

    def test_discovers_apply_patch_absolute_path(self) -> None:
        target = self.root / "10_Projects/demo/Memory_Manifest.md"
        payload = {
            "tool_input": (
                "*** Begin Patch\n"
                f"*** Update File: {target}\n"
                "*** End Patch\n"
            )
        }
        self.assertEqual(MODULE.discover_paths(payload, self.root), [target.resolve()])

    def test_valid_manifest_is_clean(self) -> None:
        target = self.root / "10_Projects/demo/Memory_Manifest.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "---\n"
            "type: manifest\nstatus: active\ncreated: 2026-07-23\n"
            "updated: 2026-07-23\nproject: demo\ntags: []\nsummary: Demo\n"
            "---\n# Demo\n",
            encoding="utf-8",
        )
        self.assertEqual(MODULE.validate_path(target, self.root, self.manifest), [])

    def test_missing_fields_and_index_are_reported(self) -> None:
        target = self.root / "10_Projects/missing/Memory_Manifest.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\ntype: manifest\n---\n# Missing\n", encoding="utf-8")
        warnings = MODULE.validate_path(target, self.root, self.manifest)
        joined = "\n".join(warnings)
        self.assertIn("missing frontmatter keys", joined)
        self.assertIn("not represented", joined)

    def test_outside_vault_is_ignored(self) -> None:
        outside = Path(tempfile.gettempdir()) / "outside-note.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        try:
            self.assertEqual(MODULE.validate_path(outside, self.root, self.manifest), [])
        finally:
            outside.unlink(missing_ok=True)

    def test_host_daily_log_names_are_valid_history(self) -> None:
        session_root = self.root / "60_Sessions/demo"
        session_root.mkdir(parents=True)
        body = (
            "---\n"
            "type: session\nstatus: active\ncreated: 2026-07-23\n"
            "updated: 2026-07-23\nproject: demo\ntags: [daily-log]\n"
            "summary: Demo daily history\n"
            "---\n# Daily history\n"
        )
        for name in (
            "2026-07-23.md",
            "2026-07-23--linux.md",
            "2026-07-23--local.md",
        ):
            target = session_root / name
            target.write_text(body, encoding="utf-8")
            self.assertEqual(
                MODULE.validate_path(target, self.root, self.manifest),
                [],
            )

    def test_session_project_must_use_canonical_root(self) -> None:
        manifest = self.root / "10_Projects/demo/Memory_Manifest.md"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("---\nproject: demo\n---\n", encoding="utf-8")
        target = self.root / "60_Sessions/demo_alias/2026-07-23.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "---\n"
            "type: session\nstatus: active\ncreated: 2026-07-23\n"
            "updated: 2026-07-23\nproject: demo\ntags: []\nsummary: Demo\n"
            "---\n",
            encoding="utf-8",
        )
        warnings = MODULE.validate_path(target, self.root, self.manifest)
        self.assertTrue(any("must use `60_Sessions/demo`" in item for item in warnings))

    def test_strict_session_routing_handles_canonical_general_and_legacy_roots(self) -> None:
        manifest = json.loads(json.dumps(self.manifest))
        manifest["validation"].update(
            {
                "allowed_legacy_session_roots": ["Old_Project"],
                "session_project_sentinels": ["_general", "_unassigned"],
            }
        )
        (self.root / "50_System/Indexes/Session_Index.md").write_text(
            "demo\n_general\nOld_Project\n", encoding="utf-8"
        )
        canonical = self.root / "10_Projects/demo/Memory_Manifest.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("---\nproject: demo\n---\n", encoding="utf-8")

        def write_session(root: str, project: str) -> list[str]:
            target = self.root / "60_Sessions" / root / "2026-07-23.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "---\n"
                "type: session\nstatus: active\ncreated: 2026-07-23\n"
                f"updated: 2026-07-23\nproject: {project}\ntags: []\nsummary: Demo\n"
                "---\n",
                encoding="utf-8",
            )
            return MODULE.validate_path(target, self.root, manifest)

        self.assertEqual(write_session("demo", "demo"), [])
        self.assertTrue(
            any(
                "canonical session root requires project `demo`" in item
                for item in write_session("demo", "typo")
            )
        )
        self.assertEqual(write_session("_general", "_general"), [])
        self.assertTrue(
            any(
                "requires project `_general`" in item
                for item in write_session("_general", "demo")
            )
        )
        self.assertEqual(write_session("Old_Project", "Old_Project"), [])
        self.assertEqual(write_session("Old_Project", "_unassigned"), [])
        self.assertTrue(
            any(
                "legacy session root requires" in item
                for item in write_session("Old_Project", "typo")
            )
        )
        self.assertTrue(
            any(
                "neither a canonical project nor an allowed legacy root" in item
                for item in write_session("unknown-typo", "unknown-typo")
            )
        )

    def test_optional_schema_constraints_are_enforced_when_present(self) -> None:
        manifest = json.loads(json.dumps(self.manifest))
        manifest["note_types"]["common"].update(
            {
                "nonempty": ["type", "status", "created", "updated", "summary"],
                "date_fields": ["created", "updated"],
            }
        )
        manifest["note_types"]["manifest"].update(
            {
                "exact_type": "manifest",
                "status_values": ["active", "paused"],
                "nonempty": ["project"],
            }
        )
        target = self.root / "10_Projects/demo/Memory_Manifest.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "---\n"
            "type: project\nstatus: mystery\ncreated: YYYY-MM-DD\n"
            "updated: 2026-99-99\nproject:\ntags: []\nsummary:\n"
            "---\n# Demo\n",
            encoding="utf-8",
        )
        joined = "\n".join(MODULE.validate_path(target, self.root, manifest))
        self.assertIn("type must be `manifest`", joined)
        self.assertIn("status must be one of", joined)
        self.assertIn("`project` must not be empty", joined)
        self.assertIn("`summary` must not be empty", joined)
        self.assertIn("`created` must be an ISO date", joined)
        self.assertIn("`updated` must be an ISO date", joined)

    def test_date_placeholder_is_allowed_only_in_system_templates(self) -> None:
        manifest = json.loads(json.dumps(self.manifest))
        manifest["note_types"]["common"]["date_fields"] = ["created", "updated"]
        rule = {"note_type": "common"}
        fm = {
            "type": "session",
            "status": "active",
            "created": "YYYY-MM-DD",
            "updated": "YYYY-MM-DD",
            "tags": "[]",
            "summary": "Template",
        }

        template = self.root / "50_System/Templates/Session.md"
        template.parent.mkdir(parents=True)
        template.write_text(
            "---\n"
            "type: session\nstatus: active\ncreated: YYYY-MM-DD\n"
            "updated: YYYY-MM-DD\ntags: []\nsummary: Template\n"
            "---\n# Session\n",
            encoding="utf-8",
        )
        self.assertEqual(MODULE.validate_path(template, self.root, manifest), [])

        template.write_text(
            "---\n"
            "type: session\nstatus: active\ncreated: YYYY-MM-DD\n"
            "updated: YYYY-MM-DD\ntags: []\n"
            "---\n# Session\n",
            encoding="utf-8",
        )
        self.assertTrue(
            any(
                "missing frontmatter keys: summary" in item
                for item in MODULE.validate_path(template, self.root, manifest)
            )
        )
        template.write_text("# No frontmatter\n", encoding="utf-8")
        self.assertTrue(
            any(
                "missing YAML frontmatter" in item
                for item in MODULE.validate_path(template, self.root, manifest)
            )
        )

        self.assertEqual(
            MODULE.frontmatter_constraint_warnings(
                "50_System/Templates/Session.md", fm, rule, manifest
            ),
            [],
        )
        outside = MODULE.frontmatter_constraint_warnings(
            "60_Sessions/demo/2026-07-23.md", fm, rule, manifest
        )
        self.assertEqual(len(outside), 2)
        self.assertTrue(all("must be an ISO date" in item for item in outside))

        malformed = dict(fm, created="DATE", updated="2026-99-99")
        template_warnings = MODULE.frontmatter_constraint_warnings(
            "50_System/Templates/Session.md", malformed, rule, manifest
        )
        self.assertEqual(len(template_warnings), 2)

    def test_exact_type_true_uses_selected_path_rule_note_type(self) -> None:
        manifest = json.loads(json.dumps(self.manifest))
        manifest["note_types"]["manifest"]["exact_type"] = True
        target = self.root / "10_Projects/demo/Memory_Manifest.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "---\n"
            "type: project\nstatus: active\ncreated: 2026-07-23\n"
            "updated: 2026-07-23\nproject: demo\ntags: []\nsummary: Demo\n"
            "---\n",
            encoding="utf-8",
        )
        warnings = MODULE.validate_path(target, self.root, manifest)
        self.assertTrue(any("type must be `manifest`" in item for item in warnings))

    def test_accepted_legacy_type_and_status_alias_are_silent(self) -> None:
        manifest = json.loads(json.dumps(self.manifest))
        manifest["note_types"]["manifest"].update(
            {
                "exact_type": "manifest",
                "accepted_types": ["project", "memory-manifest"],
                "status_values": ["active", "paused"],
                "status_aliases": {"accepted": "active"},
            }
        )
        target = self.root / "10_Projects/demo/Memory_Manifest.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "---\n"
            "type: project\nstatus: accepted\ncreated: 2026-07-23\n"
            "updated: 2026-07-23\nproject: demo\ntags: []\nsummary: Demo\n"
            "---\n",
            encoding="utf-8",
        )
        self.assertEqual(MODULE.validate_path(target, self.root, manifest), [])

    def test_discovers_delete_and_move_actions(self) -> None:
        source = self.root / "10_Projects/demo/old.md"
        destination = self.root / "10_Projects/demo/new.md"
        delete_payload = {
            "tool_input": f"*** Begin Patch\n*** Delete File: {source}\n*** End Patch\n"
        }
        self.assertEqual(
            MODULE.discover_touches(delete_payload, self.root),
            [MODULE.Touch(source.resolve(), "delete", None)],
        )

        move_payload = {
            "tool_input": (
                "*** Begin Patch\n"
                f"*** Update File: {source}\n"
                f"*** Move to: {destination}\n"
                "*** End Patch\n"
            )
        }
        touches = MODULE.discover_touches(move_payload, self.root)
        self.assertIn(
            MODULE.Touch(source.resolve(), "move_from", destination.resolve()), touches
        )
        self.assertIn(
            MODULE.Touch(destination.resolve(), "move_to", source.resolve()), touches
        )

    def test_deleted_canonical_manifest_is_reported_after_it_is_gone(self) -> None:
        target = self.root / "10_Projects/demo/Memory_Manifest.md"
        target.parent.mkdir(parents=True)
        warnings = MODULE.validate_touch(
            MODULE.Touch(target.resolve(), "delete"), self.root, self.manifest
        )
        joined = "\n".join(warnings)
        self.assertIn("vault path was deleted", joined)
        self.assertIn("canonical manifest was deleted", joined)

    def test_invalid_control_plane_json_is_fail_loud(self) -> None:
        target = self.root / "50_System/project-routing.json"
        target.write_text("{", encoding="utf-8")
        warnings = MODULE.validate_touch(
            MODULE.Touch(target.resolve(), "write"), self.root, self.manifest
        )
        joined = "\n".join(warnings)
        self.assertIn("control-plane path changed", joined)
        self.assertIn("cannot read valid JSON", joined)

    def test_invalid_hook_input_warns_but_never_blocks(self) -> None:
        code, output = self.run_main(raw="{")
        self.assertEqual(code, 0)
        message = json.loads(output)["systemMessage"]
        self.assertIn("input is not valid JSON", message)

        code, output = self.run_main(payload=[])
        self.assertEqual(code, 0)
        self.assertIn("must be a JSON object", json.loads(output)["systemMessage"])

    def test_unknown_write_payload_shape_is_fail_loud(self) -> None:
        code, output = self.run_main(payload={"tool_input": {"unknown": "value"}})
        self.assertEqual(code, 0)
        self.assertIn("no discoverable touched path", json.loads(output)["systemMessage"])

    def test_outside_vault_write_remains_silent(self) -> None:
        outside = self.root.parent / "outside.md"
        code, output = self.run_main(
            payload={"tool_input": {"file_path": str(outside)}}
        )
        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_missing_vault_and_missing_system_manifest_are_reported(self) -> None:
        missing_vault = self.root / "missing-vault"
        target = missing_vault / "10_Projects/demo/Memory_Manifest.md"
        code, output = self.run_main(
            payload={"tool_input": {"file_path": str(target)}}, vault=missing_vault
        )
        self.assertEqual(code, 0)
        self.assertIn("configured Context Vault is missing", json.loads(output)["systemMessage"])

        (self.root / "50_System/system-manifest.json").unlink()
        existing = self.root / "10_Projects/demo/Memory_Manifest.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("---\nproject: demo\n---\n", encoding="utf-8")
        code, output = self.run_main(
            payload={"tool_input": {"file_path": str(existing)}}
        )
        self.assertEqual(code, 0)
        self.assertIn(
            "50_System/system-manifest.json: file is missing",
            json.loads(output)["systemMessage"],
        )

    def test_clean_supported_write_produces_no_hook_noise(self) -> None:
        target = self.root / "10_Projects/demo/Memory_Manifest.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "---\n"
            "type: manifest\nstatus: active\ncreated: 2026-07-23\n"
            "updated: 2026-07-23\nproject: demo\ntags: []\nsummary: Demo\n"
            "---\n# Demo\n",
            encoding="utf-8",
        )
        code, output = self.run_main(
            payload={"tool_input": {"file_path": str(target)}}
        )
        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_main_reports_delete_and_move_without_blocking(self) -> None:
        target = self.root / "10_Projects/demo/Memory_Manifest.md"
        target.parent.mkdir(parents=True)
        delete_payload = {
            "tool_input": f"*** Begin Patch\n*** Delete File: {target}\n*** End Patch\n"
        }
        code, output = self.run_main(payload=delete_payload)
        self.assertEqual(code, 0)
        self.assertIn("canonical manifest was deleted", json.loads(output)["systemMessage"])

        source = self.root / "10_Projects/demo/old.md"
        destination = self.root.parent / "moved-out.md"
        move_payload = {
            "tool_input": (
                "*** Begin Patch\n"
                f"*** Update File: {source}\n"
                f"*** Move to: {destination}\n"
                "*** End Patch\n"
            )
        }
        code, output = self.run_main(payload=move_payload)
        self.assertEqual(code, 0)
        self.assertIn("moved out of vault", json.loads(output)["systemMessage"])

    def test_control_plane_write_is_always_visible(self) -> None:
        target = self.root / "50_System/project-routing.json"
        code, output = self.run_main(
            payload={"tool_input": {"file_path": str(target)}}
        )
        self.assertEqual(code, 0)
        self.assertIn("control-plane path changed", json.loads(output)["systemMessage"])

    def test_schema_interface_shape_rejects_invalid_optional_fields(self) -> None:
        manifest = json.loads(json.dumps(self.manifest))
        manifest["note_types"]["manifest"].update(
            {
                "exact_type": 7,
                "status_values": "active",
                "nonempty": ["project", 7],
                "date_fields": None,
                "accepted_types": "project",
                "status_aliases": ["accepted", "active"],
            }
        )
        manifest["validation"]["allowed_legacy_session_roots"] = "legacy"
        manifest["validation"]["session_project_sentinels"] = ["_general", 7]
        joined = "\n".join(MODULE.validate_system_manifest_shape(manifest))
        self.assertIn("exact_type", joined)
        self.assertIn("status_values", joined)
        self.assertIn("nonempty", joined)
        self.assertIn("date_fields", joined)
        self.assertIn("accepted_types", joined)
        self.assertIn("status_aliases", joined)
        self.assertIn("allowed_legacy_session_roots", joined)
        self.assertIn("session_project_sentinels", joined)

    def test_registry_normalization_collapses_edge_and_repeated_underscores(self) -> None:
        self.assertEqual(MODULE.normalize_slug("__My__Project__"), "my-project")
        self.assertEqual(MODULE.normalize_slug("___"), "")

    def test_control_plane_schema_versions_and_workspace_roots_fail_loud(self) -> None:
        manifest = json.loads(json.dumps(self.manifest))
        manifest["schema_version"] = 3
        self.assertTrue(
            any(
                "schema_version must be 1 or 2" in item
                for item in MODULE.validate_system_manifest_shape(manifest)
            )
        )

        registry = {
            "schema_version": 2,
            "workspace_roots": ["relative/Projects"],
            "workspace_aliases": {},
        }
        self.assertTrue(
            any(
                "workspace roots must be absolute or use ~/" in item
                for item in MODULE.validate_registry_shape(registry, self.root)
            )
        )


if __name__ == "__main__":
    unittest.main()
