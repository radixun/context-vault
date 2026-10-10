import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "skill" / "scripts"
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

resolver = load("resolve_project_manifest")
validator = load("validate_vault_write")

class MetadataContractTests(unittest.TestCase):
    def test_invalid_manifest_never_activates_and_hook_rejects_it(self):
        path = Path("/synthetic/10_Projects/demo/Memory_Manifest.md")
        for text in ["---\nproject: demo\nstatus: paused\nstatus: active\n---\n", "---\nproject: demo\nstatus: active\n---bad\n"]:
            with patch.object(Path, "read_text", return_value=text):
                record = resolver.manifest_record(path)
            result, code = resolver.routed_manifest_result(Path("/synthetic/demo"), record, source="test")
            self.assertEqual(result["status"], "inactive")
            self.assertIsNone(validator.frontmatter(text))
