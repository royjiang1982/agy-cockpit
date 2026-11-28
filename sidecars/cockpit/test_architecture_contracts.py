"""Keep the Sidecar's published files, cache keys and shared policy aligned."""
from fnmatch import fnmatchcase
from html.parser import HTMLParser
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import dispatch_policy
import server
from agent_runtime import dispatch_policy_reader, policy_contract


COCKPIT = Path(__file__).resolve().parent
REPO = COCKPIT.parents[1]
SHARED_WATCHED = {
    REPO / "skills/cockpit-dispatch/scripts/resolve.py",
    REPO / "runtime/agent_runtime/policy_contract.py",
}


def _test_python(path):
    return fnmatchcase(path.name, "test_*.py") or fnmatchcase(path.name, "*_test_fixture.py")


class _VersionedAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = set()

    def handle_starttag(self, tag, attrs):
        for attr, value in attrs:
            if attr not in ("src", "href") or not value or not value.startswith("/static/"):
                continue
            url = urlsplit(value)
            if "__UI_VERSION__" in parse_qs(url.query).get("v", ()):
                self.names.add(url.path.removeprefix("/static/"))


class ArchitectureContracts(unittest.TestCase):
    def test_hot_reload_watches_only_existing_production_files(self):
        watched = set(server.WATCHED_PY_FILES)
        production = {
            path for path in COCKPIT.rglob("*.py")
            if path.is_file() and not _test_python(path)
            and not {"tests", "__pycache__"}.intersection(path.relative_to(COCKPIT).parts)
        }
        self.assertEqual(sorted((production | SHARED_WATCHED) - watched), [], "Missing hot-reload files")
        self.assertEqual(sorted(path for path in watched if not path.is_file()), [], "Invalid hot-reload paths")
        self.assertEqual(sorted(path for path in watched if _test_python(path)), [], "Tests in hot reload")

    def test_ui_version_changes_when_any_referenced_static_asset_changes(self):
        index = COCKPIT / "index.html"
        assets = _VersionedAssets()
        assets.feed(index.read_text(encoding="utf-8"))
        assets.close()
        self.assertTrue(assets.names, "No versioned static assets found in index.html")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(index, root / index.name)
            for name in sorted(assets.names):
                relative = Path(name)
                self.assertTrue(relative.parts and not relative.is_absolute() and ".." not in relative.parts, name)
                dest = root / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(COCKPIT / name, dest)
            with (
                patch.object(server, "SIDECAR_DIR", root),
                patch.object(server, "INDEX_HTML_FILE", root / index.name),
                patch.object(server, "_HTML_CACHE", None),
            ):
                baseline, _ = server._ui_asset()
                for name in sorted(assets.names):
                    with self.subTest(asset=name):
                        path = root / name
                        original = path.read_bytes()
                        # Change the size as well as the content; no filesystem clock assumptions.
                        path.write_bytes(original + b"\n/* architecture contract */\n")
                        changed, _ = server._ui_asset()
                        path.write_bytes(original)
                        self.assertNotEqual(changed, baseline, "Asset did not invalidate the UI version")
                        self.assertEqual(server._ui_asset()[0], baseline)

    def test_fresh_install_includes_all_watched_files_but_no_tests(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "plugin"
            result = subprocess.run(
                ["bash", str(REPO / "install.sh"), "--allow-custom-target", str(target)],
                env={**os.environ, "HOME": str(root / "home")},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(sorted(path.relative_to(target) for path in target.rglob("*") if _test_python(path)), [])
            self.assertFalse((target / "runtime/tests").exists())
            for path in server.WATCHED_PY_FILES:
                if REPO in path.parents:
                    relative = path.relative_to(REPO)
                    self.assertTrue((target / relative).is_file(), f"Not installed: {relative}")

    def test_policy_reader_and_sidecar_share_effort_contract(self):
        for consumer in (dispatch_policy, dispatch_policy_reader):
            with self.subTest(consumer=consumer.__name__):
                self.assertIs(consumer.EFFORTS, policy_contract.EFFORTS)
                self.assertIs(consumer.resolve_effective_effort, policy_contract.resolve_effective_effort)
                # Both resolve inherited effort from the shared defaults, not a copied table.
                with patch.dict(policy_contract.AGY_DEFAULT_EFFORT, {"neon": "xhigh"}):
                    self.assertEqual(consumer.resolve_effective_effort("neon", "same"), "xhigh")


if __name__ == "__main__":
    unittest.main()
