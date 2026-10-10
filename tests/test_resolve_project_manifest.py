import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill" / "scripts" / "resolve_project_manifest.py"
SPEC = importlib.util.spec_from_file_location("resolver", SCRIPT)
resolver = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(resolver)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.projects = self.vault / "10_Projects"
        self.system = self.vault / "50_System"
        self.workspaces = self.root / "workspaces"
        self.projects.mkdir(parents=True)
        self.system.mkdir()
        self.workspaces.mkdir()
        self.write_manifest("sample-app")
        self.write_manifest("context-vault")
        self.write_registry()

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(
        self,
        slug: str,
        *,
        status: str = "active",
        project: str | None = None,
        include_status: bool = True,
    ) -> Path:
        path = self.projects / slug / "Memory_Manifest.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        status_line = f"status: {status}\n" if include_status else ""
        path.write_text(
            "---\n"
            "type: manifest\n"
            f"{status_line}"
            f"project: {project if project is not None else slug}\n"
            "---\n# Manifest\n",
            encoding="utf-8",
        )
        return path

    def write_registry(self, value: dict | None = None) -> None:
        if value is None:
            value = {
                "schema_version": 1,
                "workspace_roots": [str(self.workspaces)],
                "workspace_aliases": {},
            }
        (self.system / "project-routing.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def workspace(self, name: str, root: Path | None = None) -> Path:
        path = (root or self.workspaces) / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_separator_normalization_resolves_existing_manifest(self):
        payload, code = resolver.resolve(
            self.vault, self.workspace("sample_app"), home=self.root / "home"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["slug"], "sample-app")
        self.assertEqual(payload["source"], "normalized")

    def test_nested_workspace_uses_project_component_below_trusted_root(self):
        payload, code = resolver.resolve(
            self.vault,
            self.workspace("sample_app/docs/reference"),
            home=self.root / "home",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["slug"], "sample-app")
        self.assertEqual(payload["source"], "normalized")

    def test_nfkc_case_and_separator_normalization_is_complete(self):
        self.write_manifest("my-codex-bar")
        payload, code = resolver.resolve(
            self.vault,
            self.workspace("Ｍｙ＿Ｃｏｄｅｘ－Ｂａｒ"),
            home=self.root / "home",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["slug"], "my-codex-bar")
        self.assertEqual(payload["source"], "normalized")

    def test_repeated_and_edge_underscores_normalize_to_kebab_case(self):
        self.write_manifest("my-codex-bar")
        payload, code = resolver.resolve(
            self.vault,
            self.workspace("__My__Codex___Bar__"),
            home=self.root / "home",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["slug"], "my-codex-bar")
        self.assertEqual(resolver.normalize("___"), "")

    def test_registry_alias_resolves_semantic_workspace_name(self):
        self.write_registry(
            {
                "workspace_roots": [str(self.workspaces)],
                "workspace_aliases": {"Primary Infrastructure": "sample-app"},
            }
        )
        payload, code = resolver.resolve(
            self.vault,
            self.workspace("Primary Infrastructure"),
            home=self.root / "home",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["slug"], "sample-app")
        self.assertEqual(payload["source"], "registry")

    def test_nested_workspace_routes_by_top_level_alias(self):
        self.write_registry(
            {
                "workspace_roots": [str(self.workspaces)],
                "workspace_aliases": {"Primary Infrastructure": "sample-app"},
            }
        )
        payload, code = resolver.resolve(
            self.vault,
            self.workspace("Primary Infrastructure/docs"),
            home=self.root / "home",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["slug"], "sample-app")
        self.assertEqual(payload["source"], "registry")

    def test_unknown_workspace_is_inactive_and_creates_nothing(self):
        unknown = self.workspace("unknown")
        payload, code = resolver.resolve(self.vault, unknown, home=self.root / "home")
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "inactive")
        self.assertFalse((self.projects / "unknown").exists())

    def test_unrelated_nested_workspace_never_routes_by_leaf_basename(self):
        self.write_manifest("docs")
        payload, code = resolver.resolve(
            self.vault,
            self.workspace("unrelated/docs"),
            home=self.root / "home",
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "inactive")
        self.assertEqual(payload["normalized"], "unrelated")

    def test_deepest_workspace_root_wins_and_root_itself_is_inactive(self):
        nested_root = self.workspace("container")
        nested_workspace = self.workspace("sample_app/docs", nested_root)
        self.write_registry(
            {
                "schema_version": 2,
                "workspace_roots": [str(self.workspaces), str(nested_root)],
                "workspace_aliases": {},
            }
        )

        payload, code = resolver.resolve(
            self.vault, nested_workspace, home=self.root / "home"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["slug"], "sample-app")

        root_payload, root_code = resolver.resolve(
            self.vault, nested_root, home=self.root / "home"
        )
        self.assertEqual(root_code, 1)
        self.assertEqual(root_payload["reason"], "workspace-root")

    def test_private_tmp_devspace_is_never_activated_by_basename_alone(self):
        self.write_manifest("devspace")
        payload, code = resolver.resolve(
            self.vault, Path("/private/tmp/devspace"), home=self.root / "home"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "inactive")
        self.assertIn(
            payload["reason"], {"workspace-missing", "untrusted-workspace-root"}
        )

    def test_existing_same_named_workspace_outside_trusted_roots_is_inactive(self):
        self.write_manifest("devspace")
        outside = self.workspace("devspace", self.root / "outside")
        payload, code = resolver.resolve(self.vault, outside, home=self.root / "home")
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "untrusted-workspace-root")

    def test_symlink_escape_is_checked_by_canonical_workspace_identity(self):
        outside = self.workspace("sample_app", self.root / "outside")
        self.workspace("docs", outside)
        link = self.workspaces / "sample_app_link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        payload, code = resolver.resolve(
            self.vault, link / "docs", home=self.root / "home"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "untrusted-workspace-root")

    def test_legacy_registry_defaults_to_home_projects_on_every_host(self):
        self.write_registry({"schema_version": 1, "workspace_aliases": {}})
        for host in ("mac", "dev", "linux"):
            with self.subTest(host=host):
                home = self.root / f"{host}-home"
                workspace = self.workspace("sample_app", home / "Projects")
                payload, code = resolver.resolve(self.vault, workspace, home=home)
                self.assertEqual(code, 0)
                self.assertEqual(payload["slug"], "sample-app")

    def test_context_vault_kit_alias_remains_compatible(self):
        self.write_registry(
            {
                "schema_version": 1,
                "workspace_aliases": {"context-vault-kit": "context-vault"},
            }
        )
        home = self.root / "mac-home"
        workspace = self.workspace("context-vault-kit", home / "Projects")
        payload, code = resolver.resolve(self.vault, workspace, home=home)
        self.assertEqual(code, 0)
        self.assertEqual(payload["slug"], "context-vault")
        self.assertEqual(payload["source"], "registry")

    def test_v2_exact_binding_authorizes_workspace_outside_roots(self):
        outside = self.workspace("custom-name", self.root / "outside")
        self.write_registry(
            {
                "schema_version": 2,
                "workspace_roots": [],
                "workspace_bindings": {str(outside): "sample-app"},
                "workspace_aliases": {},
            }
        )
        payload, code = resolver.resolve(self.vault, outside, home=self.root / "home")
        self.assertEqual(code, 0)
        self.assertEqual(payload["source"], "binding")
        self.assertEqual(payload["slug"], "sample-app")

    def test_v2_longest_binding_prefix_routes_bound_root_and_descendants(self):
        parent = self.workspace("bound-parent", self.root / "outside")
        nested = self.workspace("nested", parent)
        parent_child = self.workspace("docs", parent)
        nested_child = self.workspace("src/deep", nested)
        self.write_registry(
            {
                "schema_version": 2,
                "workspace_roots": [],
                "workspace_bindings": {
                    str(parent): "context-vault",
                    str(nested): "sample-app",
                },
                "workspace_aliases": {},
            }
        )

        for workspace, slug in (
            (parent, "context-vault"),
            (parent_child, "context-vault"),
            (nested, "sample-app"),
            (nested_child, "sample-app"),
        ):
            with self.subTest(workspace=workspace):
                payload, code = resolver.resolve(
                    self.vault, workspace, home=self.root / "home"
                )
                self.assertEqual(code, 0)
                self.assertEqual(payload["slug"], slug)
                self.assertEqual(payload["source"], "binding")

    def test_registry_target_must_exist(self):
        self.write_registry(
            {
                "workspace_roots": [str(self.workspaces)],
                "workspace_aliases": {"missing": "does-not-exist"},
            }
        )
        payload, code = resolver.resolve(
            self.vault, self.workspace("missing"), home=self.root / "home"
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "invalid-registry")

    def test_registry_target_cannot_escape_project_root(self):
        self.write_registry(
            {
                "workspace_roots": [str(self.workspaces)],
                "workspace_aliases": {"escape": "../outside"},
            }
        )
        payload, code = resolver.resolve(
            self.vault, self.workspace("escape"), home=self.root / "home"
        )
        self.assertEqual(code, 3)
        self.assertIn("non-canonical", payload["error"])

    def test_malformed_registry_fails_closed(self):
        (self.system / "project-routing.json").write_text("{", encoding="utf-8")
        payload, code = resolver.resolve(
            self.vault, self.workspace("unknown"), home=self.root / "home"
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "invalid-registry")
        self.assertIn("valid JSON", payload["error"])

    def test_normalized_alias_collision_fails_closed(self):
        self.write_registry(
            {
                "workspace_roots": [str(self.workspaces)],
                "workspace_aliases": {
                    "Primary Infrastructure": "sample-app",
                    "primary_infrastructure": "sample-app",
                },
            }
        )
        payload, code = resolver.resolve(
            self.vault, self.workspace("unknown"), home=self.root / "home"
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "invalid-registry")
        self.assertIn("same value", payload["error"])

    def test_alias_cannot_override_normalized_canonical_project(self):
        self.write_manifest("foo-bar")
        self.write_manifest("other")
        self.write_registry(
            {
                "workspace_roots": [str(self.workspaces)],
                "workspace_aliases": {"foo_bar": "other"},
            }
        )
        payload, code = resolver.resolve(
            self.vault, self.workspace("unknown"), home=self.root / "home"
        )
        self.assertEqual(code, 3)
        self.assertIn("conflicts with canonical", payload["error"])

    def test_missing_vault_and_missing_registry_have_distinct_statuses(self):
        missing_payload, missing_code = resolver.resolve(
            self.root / "missing-vault",
            self.workspace("sample-app"),
            home=self.root / "home",
        )
        self.assertEqual(missing_code, 4)
        self.assertEqual(missing_payload["status"], "missing-vault")

        (self.system / "project-routing.json").unlink()
        registry_payload, registry_code = resolver.resolve(
            self.vault, self.workspace("sample-app"), home=self.root / "home"
        )
        self.assertEqual(registry_code, 5)
        self.assertEqual(registry_payload["status"], "missing-registry")

    def test_paused_manifest_is_inactive_without_alias_fallthrough(self):
        self.write_manifest("paused-project", status="paused")
        self.write_registry(
            {
                "workspace_roots": [str(self.workspaces)],
                "workspace_aliases": {"paused_alias": "paused-project"},
            }
        )
        for name in ("paused-project", "paused_alias"):
            with self.subTest(name=name):
                payload, code = resolver.resolve(
                    self.vault, self.workspace(name), home=self.root / "home"
                )
                self.assertEqual(code, 1)
                self.assertEqual(payload["status"], "inactive")
                self.assertEqual(payload["reason"], "manifest-not-active")
                self.assertEqual(payload["manifest_status"], "paused")

    def test_manifest_project_must_be_nonempty_and_match_folder(self):
        self.write_manifest("mismatch", project="other")
        payload, code = resolver.resolve(
            self.vault, self.workspace("mismatch"), home=self.root / "home"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "invalid-manifest")
        self.assertIn("does not match", payload["error"])

        self.write_manifest("empty-project", project="")
        payload, code = resolver.resolve(
            self.vault, self.workspace("empty-project"), home=self.root / "home"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "invalid-manifest")
        self.assertIn("empty", payload["error"])

    def test_manifest_status_must_be_nonempty(self):
        self.write_manifest("missing-status", include_status=False)
        payload, code = resolver.resolve(
            self.vault, self.workspace("missing-status"), home=self.root / "home"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "invalid-manifest")
        self.assertIn("status is empty", payload["error"])


if __name__ == "__main__":
    unittest.main()
