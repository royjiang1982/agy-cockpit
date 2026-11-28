#!/usr/bin/env python3
"""Installer preflight, target isolation and release contents."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from _runtime_fixture import RUNTIME, isolated_env

REPO = RUNTIME.parent


class InstallTests(unittest.TestCase):
    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def make_source(self, source):
        source.mkdir(parents=True)
        shutil.copy2(REPO / "install.sh", source)
        self.write(source / "plugin.json", '{"name": "agy-cockpit"}')
        self.write(source / "hooks.json", "{}")
        self.write(source / "rules/AGENTS.md", "rules")
        self.write(source / "sidecars/cockpit/server.py", "pass\n")
        self.write(source / "runtime/bin/sa-agy", "#!/usr/bin/env python3\npass\n")
        self.write(source / "runtime/guard/sa-guard.sh", "#!/bin/sh\n:\n")
        self.write(source / "runtime/tests/sentinel.py", "# keep source tests\n")
        return source

    @staticmethod
    def install(source, target, home):
        return subprocess.run(
            ["bash", str(source / "install.sh"), "--allow-custom-target", str(target)],
            env=isolated_env(home),
            capture_output=True, text=True, timeout=30,
        )

    def test_rejects_both_directions_of_source_target_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root / "source")
            child, home = source / "child", root / "home"
            for target in (root, child):
                self.write(target / "plugin.json", '{"name": "agy-cockpit"}')
            sentinel = source / "runtime/tests/sentinel.py"
            for target in (source, child, root):
                with self.subTest(target=target):
                    result = self.install(source, target, home)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("目标路径与源码仓重叠", result.stderr)
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "# keep source tests\n")
                    self.assertEqual(sorted(p.name for p in child.iterdir()), ["plugin.json"])

    def test_rejects_symlinks_in_managed_paths_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            source = self.make_source(root / "source")
            outside = self.write(root / "outside/keep", "untouched")
            data = self.write(home / ".gemini/antigravity/sidecar_data/keep", "untouched")
            for idx, (rel, destination) in enumerate((
                ("rules", outside.parent),
                ("runtime/bin", data.parent),
                ("skills/cockpit-dispatch/scripts/dangling", root / "absent"),
                ("sidecars/cockpit/nested/in-target", None),
            )):
                with self.subTest(rel=rel):
                    target = root / f"target-{idx}"
                    manifest = self.write(target / "plugin.json", '{"name": "agy-cockpit", "old": true}')
                    link = target / rel
                    link.parent.mkdir(parents=True, exist_ok=True)
                    destination = destination if destination is not None else target
                    link.symlink_to(destination, target_is_directory=destination.is_dir())
                    result = self.install(source, target, home)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("受管路径不能是符号链接", result.stderr)
                    self.assertTrue(link.is_symlink())
                    self.assertTrue(json.loads(manifest.read_text(encoding="utf-8"))["old"])
                    for sentinel in (outside, data):
                        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")

    def test_rejects_new_target_whose_managed_tree_resolves_to_sidecar_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, target = root / "home", root / "target"
            source = self.make_source(root / "source")
            data = home / ".gemini/antigravity/sidecar_data"
            data.parent.mkdir(parents=True)
            data.symlink_to(target / "sidecars/cockpit", target_is_directory=True)
            result = self.install(source, target, home)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("命中 sidecar_data", result.stderr)
            self.assertFalse(target.exists())

    def test_rejects_case_alias_of_absent_sidecar_data_on_first_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = self.write(root / "CaseProbe", "")
            alias = root / "caseprobe"
            if not alias.exists() or not alias.samefile(probe):
                self.skipTest("requires a case-insensitive filesystem")

            home = root / "home"
            source = self.make_source(root / "source")
            data = home / ".gemini/antigravity/sidecar_data"
            for parent_exists in (False, True):
                if parent_exists:
                    data.parent.mkdir(parents=True)
                alias = home / ".gemini/ANTIGRAVITY/SIDECAR_DATA"
                for target in (alias, alias / "statusline"):
                    with self.subTest(target=target, parent_exists=parent_exists):
                        self.assertFalse(data.exists())
                        self.assertFalse(target.exists())
                        result = self.install(source, target, home)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn("命中 sidecar_data", result.stderr)
                        self.assertFalse(data.exists())
                        self.assertFalse(target.exists())

    def test_rejects_symlinked_source_parents_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("skills", "sidecars"):
                with self.subTest(rel=rel):
                    source = self.make_source(root / f"source-{rel}")
                    parent, outside = source / rel, root / f"outside-{rel}"
                    if parent.exists():
                        parent.rename(outside)
                    else:
                        self.write(outside / "cockpit-dispatch/SKILL.md", "outside")
                    parent.symlink_to(outside, target_is_directory=True)
                    target = root / f"target-{rel}"
                    result = self.install(source, target, root / "home")
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("源树受管父目录不能是符号链接", result.stderr)
                    self.assertFalse(target.exists())

    def test_rejects_invalid_python_entrypoints_json_and_shell_before_writes(self):
        invalid = (
            ("runtime/bin/sa-agy", "#!/usr/bin/env python3\ndef broken(\n"),
            ("plugin.json", "{"),
            ("hooks.json", "{"),
            ("runtime/guard/sa-guard.sh", "#!/bin/sh\nif true; then\n"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            for idx, (rel, contents) in enumerate(invalid):
                with self.subTest(rel=rel):
                    source = self.make_source(root / f"source-{idx}")
                    self.write(source / rel, contents)
                    result = self.install(source, target, root / "home")
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("[2/4]", result.stdout)
                    self.assertIn(Path(rel).name, result.stderr)
                    self.assertFalse(target.exists())

    def test_upgrades_real_source_without_leaving_legacy_tests_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            self.write(target / "plugin.json", '{"name": "agy-cockpit"}')
            cockpit = target / "sidecars/cockpit"
            self.write(cockpit / "test_dispatch_api.py", "# stale installed test\n")
            self.write(cockpit / "_cost_test_fixture.py", "# stale installed fixture\n")
            self.write(target / "runtime/tests/sentinel.py", "# stale installed test\n")
            result = self.install(REPO, target, root / "home")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("manifest verified", result.stdout)
            self.assertEqual((target / "plugin.json").read_bytes(), (REPO / "plugin.json").read_bytes())
            self.assertTrue(os.access(target / "runtime/bin/sa-agy", os.X_OK))
            self.assertEqual(list(cockpit.glob("test_*.py")), [])
            self.assertFalse((cockpit / "_cost_test_fixture.py").exists())
            self.assertFalse((target / "runtime/tests").exists())

    def test_ignores_excluded_source_files_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root / "source")
            cockpit = source / "sidecars/cockpit"
            self.write(cockpit / "test_never_compile.py", "def broken(\n")
            (cockpit / "_cost_test_fixture.py").symlink_to(root / "missing")
            result = self.install(source, root / "target", root / "home")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("manifest verified", result.stdout)

    def test_rejects_case_alias_overlap_and_allows_unrelated_sibling_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root / "source")
            alias = source.with_name("SOURCE")
            if alias.exists() and alias.samefile(source):
                res = self.install(source, alias, root / "home")
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn("与源码仓重叠", res.stderr)
            target = root / "target"
            outside = self.write(root / "outside/note.txt", "ok")
            sibling = target / "skills/other-skill"
            sibling.parent.mkdir(parents=True, exist_ok=True)
            sibling.symlink_to(outside.parent, target_is_directory=True)
            self.write(target / "plugin.json", '{"name": "agy-cockpit"}')
            ok = self.install(source, target, root / "home")
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)


if __name__ == "__main__":
    unittest.main()
