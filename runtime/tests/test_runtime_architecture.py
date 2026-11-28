#!/usr/bin/env python3
"""Guard the production runtime dependency direction and leaf sizes via AST."""
import ast
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
import unittest


RUNTIME = Path(__file__).resolve().parents[1]
PACKAGE = "agent_runtime"
CAPS = {
    "agy_session_db": 220,
    "cli_wrappers": 200,
    "daemon": 380,
    "daemon_client": 350,
    "stream_base": 350,
    "turn_runner": 350,
}


def module_name(path):
    parts = path.relative_to(RUNTIME).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def production_sources():
    for path in sorted(RUNTIME.rglob("*")):
        rel = path.relative_to(RUNTIME)
        if rel.parts[0] == "tests" or "__pycache__" in rel.parts or not path.is_file():
            continue
        if path.suffix == ".py" or (rel.parts[0] == "bin" and not path.suffix):
            yield path, path.read_text(encoding="utf-8")


def import_targets(node, package, modules):
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        base = package.split(".")[:len(package.split(".")) - node.level + 1] if node.level else []
        root = ".".join(part for part in [*base, node.module] if part)
        for alias in node.names:
            candidate = f"{root}.{alias.name}" if root else alias.name
            # from pkg import submodule names the submodule, not the package facade.
            yield candidate if candidate in modules or alias.name == "sidecars" or not root else root
    elif isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in ("__import__", "import_module") and node.args:
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                level = len(target.value) - len(target.value.lstrip("."))
                if level:
                    base = package.split(".")[:len(package.split(".")) - level + 1]
                    yield ".".join([*base, target.value.lstrip(".")])
                else:
                    yield target.value


class RuntimeArchitectureTests(unittest.TestCase):
    def test_production_imports_are_a_dag_without_reverse_or_delayed_local_imports(self):
        sources = {module_name(path): (path, source) for path, source in production_sources()}
        modules = {name for name in sources if name.split(".")[0] == PACKAGE}
        self.assertTrue(modules, "No agent_runtime production modules found")
        graph = {name: set() for name in modules}
        import_names = modules | {f"runtime.{name}" for name in modules}
        violations = []
        for name, (path, source) in sources.items():
            tree = ast.parse(source, filename=str(path))
            nodes = list(ast.walk(tree))
            local = {
                id(child) for scope in nodes if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                for stmt in (scope.body if isinstance(scope.body, list) else [scope.body])
                for child in ast.walk(stmt)
            }
            package = name if path.name == "__init__.py" else name.rpartition(".")[0]
            for node in nodes:
                for target in import_targets(node, package, import_names):
                    target = target.removeprefix("runtime.")
                    location = f"{path.relative_to(RUNTIME)}:{node.lineno}"
                    if "sidecars" in target.split("."):
                        violations.append(f"{location}: runtime imports {target}")
                    if name not in modules:
                        continue
                    if target in modules:
                        graph[name].add(target)
                    if target.split(".")[0] == PACKAGE and id(node) in local:
                        violations.append(f"{location}: delayed local import of {target}")
        try:
            tuple(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            violations.append("Import cycle: " + " -> ".join(exc.args[1]))
        self.assertFalse(violations, "\n".join(violations))

    def test_runtime_leaf_size_budgets(self):
        files = {path: source for path, source in production_sources() if path.relative_to(RUNTIME).parts[0] in (PACKAGE, "bin")}
        files[Path(__file__)] = Path(__file__).read_text(encoding="utf-8")
        runner = Path(__file__).with_name("run_all.py")
        files[runner] = runner.read_text(encoding="utf-8")
        for path, source in sorted(files.items()):
            with self.subTest(path=path.relative_to(RUNTIME)):
                limit = 120 if path == Path(__file__) else CAPS.get(path.stem, 400)
                self.assertLessEqual(len(source.splitlines()), limit)


if __name__ == "__main__":
    unittest.main()
