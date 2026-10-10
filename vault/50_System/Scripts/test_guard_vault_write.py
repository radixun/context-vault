from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("guard_vault_write.py")
SPEC = importlib.util.spec_from_file_location("guard_vault_write", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(guard)


class VaultWriteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.note = self.vault / "10_Projects/demo/Memory_Manifest.md"
        self.note.parent.mkdir(parents=True)
        (self.vault / "70_Decisions").mkdir()
        self.note.write_bytes(b"---\nproject: demo\n---\n# Demo\n")
        self.receipt = self.base / "receipt.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_main(self, *arguments: str) -> tuple[int, dict, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = guard.main(list(arguments))
        raw = output.getvalue()
        return exit_code, json.loads(raw), raw

    def snapshot(self) -> tuple[int, dict, str]:
        return self.run_main(
            "snapshot",
            "--vault",
            str(self.vault),
            "--path",
            "10_Projects/demo/Memory_Manifest.md",
            "--receipt",
            str(self.receipt),
        )

    def verify(self, *, strict: bool = False) -> tuple[int, dict, str]:
        arguments = [
            "verify",
            "--vault",
            str(self.vault),
            "--receipt",
            str(self.receipt),
        ]
        if strict:
            arguments.append("--strict-identity")
        return self.run_main(*arguments)

    def test_snapshot_receipt_and_unchanged_verify(self) -> None:
        original = self.note.read_bytes()
        original_stat = self.note.stat()
        code, result, _raw = self.snapshot()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ok")
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(
            set(receipt),
            {
                "schema_version",
                "path",
                "sha256",
                "size",
                "mtime_ns",
                "device",
                "inode",
                "ctime_ns",
                "timestamp",
            },
        )
        self.assertEqual(receipt["path"], "10_Projects/demo/Memory_Manifest.md")
        self.assertEqual(len(receipt["sha256"]), 64)
        self.assertEqual(self.note.read_bytes(), original)
        self.assertEqual(self.note.stat().st_mtime_ns, original_stat.st_mtime_ns)

        code, report, _raw = self.verify()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["content_match"])
        self.assertTrue(report["identity_match"])
        self.assertEqual(report["warnings"], [])

    def test_same_size_content_drift_is_stale(self) -> None:
        self.assertEqual(self.snapshot()[0], 0)
        before = self.note.read_bytes()
        self.note.write_bytes(before.replace(b"Demo", b"Dome"))
        code, report, _raw = self.verify()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "drift")
        self.assertFalse(report["content_match"])
        self.assertIn("sha256_drift", {item["code"] for item in report["failures"]})

    def test_missing_note_is_stale(self) -> None:
        self.assertEqual(self.snapshot()[0], 0)
        self.note.unlink()
        code, report, _raw = self.verify()
        self.assertEqual(code, 1)
        self.assertEqual(report["failures"][0]["code"], "missing_note")

    def test_syncthing_style_identity_replacement_is_advisory_by_default(self) -> None:
        self.assertEqual(self.snapshot()[0], 0)
        replacement = self.note.with_name("replacement.tmp")
        replacement.write_bytes(self.note.read_bytes())
        os.replace(replacement, self.note)

        code, report, _raw = self.verify()
        self.assertEqual(code, 0)
        self.assertTrue(report["content_match"])
        self.assertFalse(report["identity_match"])
        self.assertEqual(report["warnings"][0]["code"], "identity_drift")

        code, report, _raw = self.verify(strict=True)
        self.assertEqual(code, 1)
        self.assertTrue(report["content_match"])
        self.assertIn("identity_drift", {item["code"] for item in report["failures"]})

    def test_traversal_and_outside_paths_are_rejected(self) -> None:
        code, result, _raw = self.run_main(
            "hash",
            "--vault",
            str(self.vault),
            "--path",
            "10_Projects/demo/../demo/Memory_Manifest.md",
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "unsafe_path")

        outside = self.base / "10_Projects/demo/Memory_Manifest.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("outside\n", encoding="utf-8")
        code, result, _raw = self.run_main(
            "hash", "--vault", str(self.vault), "--path", str(outside)
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "outside_vault")

    def test_note_symlinks_are_rejected(self) -> None:
        external = self.base / "external.md"
        external.write_bytes(self.note.read_bytes())
        self.note.unlink()
        self.note.symlink_to(external)
        code, result, _raw = self.run_main(
            "hash",
            "--vault",
            str(self.vault),
            "--path",
            "10_Projects/demo/Memory_Manifest.md",
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "unsafe_symlink")

    def test_noncanonical_and_conflict_artifact_paths_are_rejected(self) -> None:
        resource = self.vault / "30_Resources/example.md"
        resource.parent.mkdir()
        resource.write_text("# Resource\n", encoding="utf-8")
        code, result, _raw = self.run_main(
            "hash", "--vault", str(self.vault), "--path", "30_Resources/example.md"
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "unsupported_path")

        conflict = self.vault / "70_Decisions/change.sync-conflict-host.md"
        conflict.write_text("# Conflict\n", encoding="utf-8")
        code, result, _raw = self.run_main(
            "hash",
            "--vault",
            str(self.vault),
            "--path",
            "70_Decisions/change.sync-conflict-host.md",
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "sync_conflict")

    def test_project_home_and_decision_paths_are_supported(self) -> None:
        home = self.note.with_name("demo.md")
        home.write_text("# Demo home\n", encoding="utf-8")
        decision = self.vault / "70_Decisions/2026-08-12-demo.md"
        decision.write_text("# Decision\n", encoding="utf-8")
        for relative in (
            "10_Projects/demo/demo.md",
            "70_Decisions/2026-08-12-demo.md",
        ):
            code, result, _raw = self.run_main(
                "hash", "--vault", str(self.vault), "--path", relative
            )
            self.assertEqual(code, 0)
            self.assertEqual(result["path"], relative)

    def test_receipt_must_be_outside_vault(self) -> None:
        receipt = self.vault / "receipt.json"
        code, result, _raw = self.run_main(
            "snapshot",
            "--vault",
            str(self.vault),
            "--path",
            "10_Projects/demo/Memory_Manifest.md",
            "--receipt",
            str(receipt),
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "receipt_inside_vault")
        self.assertFalse(receipt.exists())

    def test_receipt_symlinks_are_rejected(self) -> None:
        self.receipt.symlink_to(self.base / "missing-target.json")
        code, result, _raw = self.snapshot()
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "unsafe_receipt")

    def test_malformed_and_unsafe_receipts_fail_closed(self) -> None:
        self.receipt.write_text("{", encoding="utf-8")
        code, result, _raw = self.verify()
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "malformed_receipt")

        self.assertEqual(self.snapshot()[0], 0)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["path"] = "../70_Decisions/escape.md"
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        code, result, _raw = self.verify()
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "unsafe_path")

    def test_snapshot_replaces_receipt_atomically(self) -> None:
        self.receipt.write_text("old incomplete receipt", encoding="utf-8")
        original_replace = os.replace
        vault = guard.resolve_vault(self.vault)
        with patch.object(guard.os, "replace", wraps=original_replace) as replace:
            receipt, receipt_path = guard.snapshot_note(
                vault,
                "10_Projects/demo/Memory_Manifest.md",
                self.receipt,
            )
        replace.assert_called_once()
        self.assertEqual(receipt_path, self.receipt.resolve())
        self.assertEqual(json.loads(self.receipt.read_text(encoding="utf-8")), receipt)
        self.assertEqual(list(self.base.glob(f".{self.receipt.name}.*.tmp")), [])

    def test_hash_is_stable_and_writes_nothing(self) -> None:
        arguments = (
            "hash",
            "--vault",
            str(self.vault),
            "--path",
            "10_Projects/demo/Memory_Manifest.md",
        )
        first_code, first, first_raw = self.run_main(*arguments)
        second_code, second, second_raw = self.run_main(*arguments)
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first, second)
        self.assertEqual(first_raw, second_raw)
        self.assertFalse(self.receipt.exists())


if __name__ == "__main__":
    unittest.main()
