"""Keep the Sidecar's published files, cache keys and shared policy aligned."""
import ast
from fnmatch import fnmatchcase
from html.parser import HTMLParser
import os
from pathlib import Path
import shutil
import subprocess
import sys
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
RUNTIME_TESTS = REPO / "runtime" / "tests"
if str(RUNTIME_TESTS) not in sys.path:
    sys.path.insert(0, str(RUNTIME_TESTS))

from _production_symbols import undefined_globals


def _test_python(path):
    return fnmatchcase(path.name, "test_*.py") or fnmatchcase(path.name, "*_test_fixture.py")


def _production_python():
    return {
        path for path in COCKPIT.rglob("*.py")
        if path.is_file() and not _test_python(path)
        and not {"tests", "__pycache__"}.intersection(path.relative_to(COCKPIT).parts)
    }


def _local_targets(node, package, modules):
    def local(name):
        for prefix in ("sidecars.cockpit", "cockpit"):
            if name == prefix:
                return ""
            if name.startswith(prefix + "."):
                return name[len(prefix) + 1:]
        return name

    if isinstance(node, ast.Import):
        candidates = [local(alias.name) for alias in node.names]
    else:
        if node.level:
            parents = package.split(".") if package else []
            if node.level > len(parents) + 1:
                return set()
            base = ".".join(filter(None, [*parents[:len(parents) + 1 - node.level], node.module]))
        else:
            base = local(node.module or "")
        candidates = [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
        if node.module:
            candidates.append(base)
    return {name for name in candidates if name in modules}


def _sidecar_imports(sources):
    modules = {}
    for path, source in sources.items():
        parts = path.relative_to(COCKPIT).with_suffix("").parts
        package = ".".join(parts[:-1])
        module = package if parts[-1] == "__init__" else ".".join(parts)
        modules[module] = package, ast.parse(source, filename=str(path))
    graph = {name: set() for name in modules}
    delayed = []
    for name, (package, tree) in modules.items():
        top_level = set(tree.body)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            targets = _local_targets(node, package, modules)
            graph[name].update(targets)
            if targets and node not in top_level:
                delayed.append((name, node.lineno, sorted(targets)))
    return graph, sorted(delayed)


def _cyclic_components(graph):
    """Tarjan's SCCs, including a module that imports itself."""
    index, low = {}, {}
    stack, on_stack = [], set()
    cycles = []

    def visit(name):
        index[name] = low[name] = len(index)
        stack.append(name)
        on_stack.add(name)
        for target in sorted(graph[name]):
            if target not in index:
                visit(target)
                low[name] = min(low[name], low[target])
            elif target in on_stack:
                low[name] = min(low[name], index[target])
        if low[name] == index[name]:
            component = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == name:
                    break
            if len(component) > 1 or name in graph[name]:
                cycles.append(sorted(component))

    for name in sorted(graph):
        if name not in index:
            visit(name)
    return sorted(cycles)


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
    @classmethod
    def setUpClass(cls):
        sources = {path: path.read_text(encoding="utf-8") for path in _production_python()}
        cls.import_graph, cls.delayed_imports = _sidecar_imports(sources)

    def test_production_import_graph_is_a_dag(self):
        cycles = _cyclic_components(self.import_graph)
        self.assertEqual(len(cycles), 0, f"Cyclic Sidecar modules: {cycles}")

    def test_local_production_imports_are_top_level(self):
        self.assertEqual(self.delayed_imports, [], "Local imports must be at module top level")

    def test_production_has_no_undefined_globals(self):
        shared = {path for path in SHARED_WATCHED if not path.is_relative_to(REPO / "runtime")}
        issues = [issue for path in sorted(_production_python() | shared)
                  for issue in undefined_globals(path.read_text(encoding="utf-8"), path.relative_to(REPO))]
        self.assertEqual(issues, [], "\n".join(issues))

    def test_undefined_global_gate_distinguishes_local_and_module_bindings(self):
        source = (
            "from os import path\n"
            "def read():\n"
            "    from json import loads\n"
            "    def inner(): return loads(path.basename(__file__))\n"
            "    return inner\n"
            "def missing(): return loads('null')\n"
        )
        issues = undefined_globals(source, "fixture.py")
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("missing references undefined global 'loads'", issues[0])

    def test_hot_reload_watches_only_existing_production_files(self):
        watched = set(server.WATCHED_PY_FILES)
        production = _production_python()
        expected = production | SHARED_WATCHED
        self.assertEqual(sorted(expected - watched), [], "Missing hot-reload files")
        self.assertEqual(sorted(watched - expected), [], "Unexpected hot-reload files")
        self.assertEqual(len(server.WATCHED_PY_FILES), len(watched), "Duplicate hot-reload paths")
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
