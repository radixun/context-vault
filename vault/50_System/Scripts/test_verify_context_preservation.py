import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


SCRIPT = Path(__file__).with_name("verify_context_preservation.py")
SPEC = importlib.util.spec_from_file_location("verify_context_preservation", SCRIPT)
preservation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = preservation
SPEC.loader.exec_module(preservation)


class ContextPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        for root in preservation.DEFAULT_ROOTS:
            (self.vault / root).mkdir(parents=True)
        self.write_note(
            "10_Projects/example/Memory_Manifest.md",
            "---\nproject: example\nstatus: active\n---\n"
            "# Example\n\nKeep [[Reference]] and [source](https://example.test).\n",
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_note(self, relative, content):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def snapshot(self):
        inventory, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertEqual(issues, [])
        return preservation.snapshot_document(inventory)

    def empty_allowlist(self):
        return preservation.empty_allowlist()

    def verify(
        self,
        baseline,
        allowlist=None,
        expected_count=None,
        expected_allowlist_sha256=None,
    ):
        return preservation.verify_preservation(
            baseline,
            self.vault,
            allowlist or self.empty_allowlist(),
            baseline["inventory_sha256"],
            expected_baseline_count=expected_count,
            expected_allowlist_sha256=expected_allowlist_sha256,
        )

    def test_snapshot_separates_exact_body_from_frontmatter(self):
        snapshot = self.snapshot()
        record = snapshot["files"]["10_Projects/example/Memory_Manifest.md"]
        self.assertEqual(snapshot["note_count"], 1)
        self.assertTrue(record["has_frontmatter"])
        self.assertEqual(record["structure"]["heading_count"], 1)
        self.assertEqual(record["structure"]["wikilink_count"], 1)
        self.assertEqual(record["structure"]["markdown_link_count"], 1)
        self.assertEqual(
            record["body_sha256"],
            preservation.sha256_bytes(
                b"# Example\n\nKeep [[Reference]] and [source](https://example.test).\n"
            ),
        )

    def test_unapproved_body_modification_fails(self):
        baseline = self.snapshot()
        self.write_note(
            "10_Projects/example/Memory_Manifest.md",
            "---\nproject: example\nstatus: active\n---\n# Replaced\n",
        )
        report = self.verify(baseline, expected_count=1)
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            "unapproved_modification", {item["code"] for item in report["failures"]}
        )

    def test_frontmatter_only_change_requires_exact_pins_and_preserves_body(self):
        baseline = self.snapshot()
        relative = "10_Projects/example/Memory_Manifest.md"
        self.write_note(
            relative,
            "---\nproject: example\nstatus: paused\n---\n"
            "# Example\n\nKeep [[Reference]] and [source](https://example.test).\n",
        )
        after, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertEqual(issues, [])
        allowlist = {
            "schema_version": 1,
            "changes": [
                {
                    "path": relative,
                    "mode": "frontmatter_only",
                    "before_sha256": baseline["files"][relative]["sha256"],
                    "after_sha256": after["files"][relative]["sha256"],
                    "reason": "approved status transition",
                }
            ],
            "additions": [],
            "renames": [],
        }
        report = self.verify(baseline, allowlist, expected_count=1)
        self.assertEqual(report["status"], "pass", report["failures"])
        self.assertEqual(report["summary"]["approved_changes"], 1)

        pinned_after = allowlist["changes"][0]["after_sha256"]
        allowlist["changes"][0]["after_sha256"] = "0" * 64
        report = self.verify(baseline, allowlist)
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            "after_hash_mismatch", {item["code"] for item in report["failures"]}
        )
        allowlist["changes"][0]["after_sha256"] = pinned_after

        self.write_note(
            relative,
            "---\nproject: example\nstatus: paused\n---\n"
            "# Example\n\nLost the original body context.\n",
        )
        tampered, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertEqual(issues, [])
        allowlist["changes"][0]["after_sha256"] = tampered["files"][relative][
            "sha256"
        ]
        report = self.verify(baseline, allowlist)
        self.assertEqual(report["status"], "fail")
        self.assertIn("body_changed", {item["code"] for item in report["failures"]})

    def test_append_only_change_preserves_complete_old_prefix(self):
        relative = "60_Sessions/example/2026-08-12.md"
        self.write_note(relative, "---\nproject: example\n---\n# Log\n\nFirst.\n")
        baseline = self.snapshot()
        with (self.vault / relative).open("a", encoding="utf-8", newline="") as handle:
            handle.write("\nSecond.\n")
        after, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertEqual(issues, [])
        allowlist = {
            "schema_version": 1,
            "changes": [
                {
                    "path": relative,
                    "mode": "append_only",
                    "before_sha256": baseline["files"][relative]["sha256"],
                    "after_sha256": after["files"][relative]["sha256"],
                    "reason": "rollout log append",
                }
            ],
            "additions": [],
            "renames": [],
        }
        report = self.verify(baseline, allowlist)
        self.assertEqual(report["status"], "pass", report["failures"])

        self.write_note(relative, "---\nproject: example\n---\n# Log\n\nChanged.\nSecond.\n")
        tampered, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertEqual(issues, [])
        allowlist["changes"][0]["after_sha256"] = tampered["files"][relative][
            "sha256"
        ]
        report = self.verify(baseline, allowlist)
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            "not_append_only", {item["code"] for item in report["failures"]}
        )

    def test_allowlisted_addition_does_not_hide_loss(self):
        baseline = self.snapshot()
        relative = "70_Decisions/2026-08-12-rollout.md"
        self.write_note(relative, "---\ndate: 2026-08-12\n---\n# Rollout\n")
        after, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertEqual(issues, [])
        allowlist = {
            "schema_version": 1,
            "changes": [],
            "additions": [
                {
                    "path": relative,
                    "after_sha256": after["files"][relative]["sha256"],
                    "reason": "new rollout decision",
                }
            ],
            "renames": [],
        }
        report = self.verify(baseline, allowlist)
        self.assertEqual(report["status"], "pass", report["failures"])
        (self.vault / "10_Projects/example/Memory_Manifest.md").unlink()
        report = self.verify(baseline, allowlist)
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            "unapproved_deletion", {item["code"] for item in report["failures"]}
        )

    def test_exact_file_rename_is_one_to_one(self):
        baseline = self.snapshot()
        source = "10_Projects/example/Memory_Manifest.md"
        target = "10_Projects/example/Manifest_Renamed.md"
        (self.vault / source).rename(self.vault / target)
        after, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertEqual(issues, [])
        allowlist = {
            "schema_version": 1,
            "changes": [],
            "additions": [],
            "renames": [
                {
                    "from": source,
                    "to": target,
                    "mode": "exact_file",
                    "before_sha256": baseline["files"][source]["sha256"],
                    "after_sha256": after["files"][target]["sha256"],
                    "reason": "approved path correction",
                }
            ],
        }
        report = self.verify(baseline, allowlist)
        self.assertEqual(report["status"], "pass", report["failures"])
        self.assertEqual(report["after_note_count"], report["baseline_note_count"])

        duplicate = json.loads(json.dumps(allowlist["renames"][0]))
        duplicate["from"] = "20_Areas/another.md"
        allowlist["renames"].append(duplicate)
        with self.assertRaises(preservation.PreservationError):
            preservation.validate_allowlist(allowlist, baseline["roots"])

    def test_unlisted_addition_and_sync_conflict_fail(self):
        baseline = self.snapshot()
        self.write_note("20_Areas/unlisted.md", "# Unlisted\n")
        self.write_note(
            "20_Areas/unlisted.sync-conflict-20260812-195400.md", "# Conflict\n"
        )
        report = self.verify(baseline)
        codes = {item["code"] for item in report["failures"]}
        self.assertIn("unapproved_addition", codes)
        self.assertIn("sync_conflict", codes)

    def test_tampered_snapshot_digest_is_rejected(self):
        baseline = self.snapshot()
        relative = "10_Projects/example/Memory_Manifest.md"
        baseline["files"][relative]["body_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            preservation.PreservationError, "inventory_sha256"
        ):
            preservation.validate_snapshot(baseline)

    def test_self_consistent_baseline_forgery_fails_external_digest_pin(self):
        baseline = self.snapshot()
        frozen_digest = baseline["inventory_sha256"]
        relative = "10_Projects/example/Memory_Manifest.md"
        baseline["files"][relative]["body_sha256"] = "0" * 64
        payload = {
            "schema_version": baseline["schema_version"],
            "roots": baseline["roots"],
            "note_count": baseline["note_count"],
            "files": baseline["files"],
        }
        baseline["inventory_sha256"] = preservation.digest_json(payload)
        with self.assertRaisesRegex(preservation.PreservationError, "externally pinned"):
            preservation.verify_preservation(
                baseline,
                self.vault,
                self.empty_allowlist(),
                frozen_digest,
            )

    def test_allowlist_is_externally_pinned(self):
        baseline = self.snapshot()
        relative = "10_Projects/example/Memory_Manifest.md"
        self.write_note(
            relative,
            "---\nproject: example\nstatus: paused\n---\n"
            "# Example\n\nKeep [[Reference]] and [source](https://example.test).\n",
        )
        after, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertEqual(issues, [])
        allowlist = {
            "schema_version": 1,
            "changes": [
                {
                    "path": relative,
                    "mode": "frontmatter_only",
                    "before_sha256": baseline["files"][relative]["sha256"],
                    "after_sha256": after["files"][relative]["sha256"],
                    "reason": "approved status transition",
                }
            ],
            "additions": [],
            "renames": [],
        }
        pinned_digest = preservation.digest_json(
            preservation.validate_allowlist(allowlist, baseline["roots"])
        )
        allowlist["changes"][0]["reason"] = "changed after approval"
        with self.assertRaisesRegex(preservation.PreservationError, "externally pinned"):
            self.verify(
                baseline,
                allowlist,
                expected_allowlist_sha256=pinned_digest,
            )

    def test_unsafe_and_overlapping_roots_are_rejected(self):
        for roots in (
            ("/tmp/outside",),
            ("../outside",),
            ("10_Projects/example",),
            ("10_Projects", "10_Projects/example"),
        ):
            with self.subTest(roots=roots):
                with self.assertRaises(preservation.PreservationError):
                    preservation.collect_inventory(self.vault, roots)

    def test_non_markdown_sync_conflict_blocks_proof(self):
        conflict = self.vault / "20_Areas" / "asset.sync-conflict-20260812.bin"
        conflict.write_bytes(b"conflict")
        _inventory, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertIn("sync_conflict", {item["code"] for item in issues})

    def test_malformed_frontmatter_blocks_snapshot(self):
        self.write_note("20_Areas/broken.md", "---\nstatus: active\n# No closer\n")
        _inventory, issues = preservation.collect_inventory(
            self.vault, preservation.DEFAULT_ROOTS
        )
        self.assertIn("unreadable_note", {item["code"] for item in issues})

    def test_evidence_output_inside_measured_vault_is_refused(self):
        baseline = self.snapshot()
        with self.assertRaisesRegex(preservation.PreservationError, "outside"):
            preservation.write_json_outside_vault(
                self.vault / "baseline.json", baseline, self.vault
            )

    def test_cli_freezes_dynamic_count_and_requires_external_digest(self):
        self.write_note("20_Areas/second.md", "# Second\n\nContext.\n")
        baseline_path = self.base / "evidence" / "baseline.json"
        report_path = self.base / "evidence" / "report.json"
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            snapshot_status = preservation.main(
                [
                    "snapshot",
                    "--vault",
                    str(self.vault),
                    "--output",
                    str(baseline_path),
                ]
            )
        self.assertEqual(snapshot_status, 0, output.getvalue())
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(baseline["note_count"], 2)

        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            verify_status = preservation.main(
                [
                    "verify",
                    "--baseline",
                    str(baseline_path),
                    "--after-vault",
                    str(self.vault),
                    "--expected-baseline-sha256",
                    baseline["inventory_sha256"],
                    "--report",
                    str(report_path),
                ]
            )
        self.assertEqual(verify_status, 0, output.getvalue())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["baseline_note_count"], 2)
        self.assertEqual(report["after_note_count"], 2)


if __name__ == "__main__":
    unittest.main()
