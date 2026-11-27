#!/usr/bin/env python3
"""Python AST embedded-script static guards for sa-guard."""
from __future__ import annotations

import ast
import posixpath
import shlex
from typing import Callable, Dict, List, Optional, Set, Tuple

_SHELL_EXEC_METHODS = {
    "os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput",
    "asyncio.create_subprocess_shell", "system", "popen", "getoutput", "getstatusoutput",
}
_PROC_EXEC_METHODS = {
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "asyncio.create_subprocess_exec", "pty.spawn", "Popen",
}
_OS_SPAWN_PREFIXES = ("os.spawn", "os.exec")
_WRITE_PATH_METHODS = {"write_text", "write_bytes", "unlink", "rmdir", "rename", "replace", "touch", "mkdir"}
_DELETE_PATH_METHODS = {"unlink", "rmdir"}
_DELETE_PATH_FUNCS = {"os.remove", "os.unlink", "os.rmdir", "shutil.rmtree"}
_READ_PATH_METHODS = {"read_text", "read_bytes", "exists", "is_file", "is_dir", "stat", "lstat", "resolve", "expanduser"}
_SAFE_GIT_SUBCMDS = {
    "status", "diff", "log", "show", "rev-parse", "ls-files", "grep",
    "describe", "symbolic-ref", "cat-file", "for-each-ref", "check-ignore",
}
_WRITE_PATH_FUNCS = {
    "os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace", "os.chmod", "os.chown",
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.move", "shutil.rmtree",
}


def _dotted_name(node: ast.AST, aliases: Dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _has_starred(node: ast.Call) -> bool:
    return any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)


def _resolve_expr(node: Optional[ast.AST], var_env: Dict[str, Optional[str]], aliases: Dict[str, str]) -> Optional[str]:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return var_env.get(node.id)
    if isinstance(node, ast.Subscript):
        if _dotted_name(node.value, aliases) == "os.environ" and _resolve_expr(node.slice, var_env, aliases) == "HOME":
            return "~"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _resolve_expr(node.left, var_env, aliases), _resolve_expr(node.right, var_env, aliases)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _resolve_expr(node.left, var_env, aliases), _resolve_expr(node.right, var_env, aliases)
        return posixpath.join(left, right) if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                res = _resolve_expr(v.value, var_env, aliases)
                parts.append(res if res is not None else "__VAR__")
            else:
                parts.append("__VAR__")
        return "".join(parts)
    if isinstance(node, ast.Call):
        cname = _dotted_name(node.func, aliases)
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("strip", "lstrip", "rstrip") and not node.args:
            inner = _resolve_expr(node.func.value, var_env, aliases)
            return getattr(inner, node.func.attr)() if inner is not None else None
        if cname in ("Path.home", "pathlib.Path.home") and not node.args:
            return "~"
        if cname == "os.environ.get" and node.args and _resolve_expr(node.args[0], var_env, aliases) == "HOME":
            return "~"
        if cname in ("tempfile.mkdtemp", "tempfile.TemporaryDirectory"):
            if _has_starred(node):
                return None
            dir_node = node.args[2] if len(node.args) >= 3 else next((k.value for k in node.keywords if k.arg == "dir"), None)
            if dir_node is None or (isinstance(dir_node, ast.Constant) and dir_node.value is None):
                return "/tmp/__sa_guard_tempdir__"
            dir_val = _resolve_expr(dir_node, var_env, aliases)
            return posixpath.join(dir_val, "__sa_guard_tempdir__") if dir_val is not None else None
        if cname in ("pathlib.Path", "Path", "str", "os.path.expanduser", "expanduser") and len(node.args) == 1:
            return _resolve_expr(node.args[0], var_env, aliases)
        if cname in ("os.path.join", "posixpath.join") and node.args:
            bits = [_resolve_expr(a, var_env, aliases) for a in node.args]
            return posixpath.join(*bits) if all(b is not None for b in bits) else None  # type: ignore[arg-type]
        if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath" and node.args:
            base_p = _resolve_expr(node.func.value, var_env, aliases)
            bits = [_resolve_expr(a, var_env, aliases) for a in node.args]
            return posixpath.join(base_p, *bits) if base_p is not None and all(b is not None for b in bits) else None  # type: ignore[arg-type]
    return None


_OS_READ_FLAGS = {"os.O_RDONLY", "O_RDONLY", "os.O_CLOEXEC", "O_CLOEXEC", "os.O_NONBLOCK", "O_NONBLOCK", "os.O_NOFOLLOW", "O_NOFOLLOW", "os.O_DIRECTORY", "O_DIRECTORY"}


def _os_flags_write_state(node: Optional[ast.AST], aliases: Dict[str, str]) -> Optional[bool]:
    """Return False iff os.open flags are statically read-only, True/None otherwise."""
    if node is None:
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _os_flags_write_state(node.left, aliases)
        right = _os_flags_write_state(node.right, aliases)
        return False if left is False and right is False else True
    name = _dotted_name(node, aliases)
    return False if name in _OS_READ_FLAGS else True


def _open_write_state(cname: str, node: ast.Call, aliases: Dict[str, str]) -> Optional[bool]:
    """Return True if open/os.open uses write semantics, False if read-only, None if dynamic."""
    if _has_starred(node):
        return None
    if cname == "os.open":
        flags_node = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "flags"), None)
        return _os_flags_write_state(flags_node, aliases)
    mode_node = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
    if mode_node is None:
        return False
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return any(ch in mode_node.value for ch in ("w", "a", "x", "+"))
    return None


def _check_python_exec_call(
    cname: str,
    node: ast.Call,
    var_env: Dict[str, Optional[str]],
    aliases: Dict[str, str],
    shell_restricted_fn: Callable[[str], bool],
    is_sensitive_fn: Callable[[str], bool],
    is_guard_fn: Callable[[str], bool],
    safe_tmp_fn: Callable[[Optional[str]], bool],
    depth: int,
) -> bool:
    shell_kw = any(kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords)
    is_shell = cname in _SHELL_EXEC_METHODS or shell_kw
    arg0 = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg in ("args", "cmd")), None)
    if arg0 is None:
        return True
    if is_shell:
        cmd_str = _resolve_expr(arg0, var_env, aliases)
        return True if cmd_str is None else bool(shell_restricted_fn(cmd_str))
    if isinstance(arg0, (ast.List, ast.Tuple)) and arg0.elts:
        resolved = [_resolve_expr(e, var_env, aliases) for e in arg0.elts]
        if all(r is not None for r in resolved):
            return bool(shell_restricted_fn(" ".join(shlex.quote(r) for r in resolved)))  # type: ignore[arg-type]
        head_name = resolved[0]
        is_py = _dotted_name(arg0.elts[0], aliases) in ("sys.executable", "executable") or (
            head_name is not None and posixpath.basename(head_name) in ("python", "python3")
        )
        if is_py:
            for idx, item in enumerate(resolved[1:], 1):
                if item == "-c" or (isinstance(item, str) and item.startswith("-") and not item.startswith("--") and "c" in item[1:]):
                    if idx + 1 >= len(resolved) or resolved[idx + 1] is None:
                        return True
                    if python_ast_restricted(resolved[idx + 1], shell_restricted_fn, is_sensitive_fn, is_guard_fn, safe_tmp_fn, depth=depth + 1):  # type: ignore[arg-type]
                        return True
                elif item is not None and is_sensitive_fn(item):
                    return True
            return False
        if head_name is not None and posixpath.basename(head_name) == "git":
            subcmd, idx = None, 1
            while idx < len(resolved):
                tok = resolved[idx]
                if tok is None:
                    return True
                if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                subcmd = tok
                break
            if subcmd not in _SAFE_GIT_SUBCMDS:
                return True
            return any(tok is not None and is_sensitive_fn(tok) for tok in resolved[idx + 1 :])
        return True
    cmd_str = _resolve_expr(arg0, var_env, aliases)
    return True if cmd_str is None else bool(shell_restricted_fn(cmd_str))


def python_ast_restricted(
    code: str,
    shell_restricted_fn: Callable[[str], bool],
    is_sensitive_fn: Callable[[str], bool],
    is_guard_fn: Callable[[str], bool],
    safe_tmp_fn: Callable[[Optional[str]], bool],
    *,
    depth: int = 0,
) -> bool:
    """Inspect Python source via AST for subprocesses, sensitive/guard files, or non-tmp deletes."""
    if depth > 4:
        return True
    try:
        tree = ast.parse(code)
    except Exception:
        return True

    aliases: Dict[str, str] = {}
    var_env: Dict[str, Optional[str]] = {}
    has_write = False
    assigns: List[Tuple[str, ast.AST]] = []
    ambiguous: Set[str] = set()

    def _record_var(tid: str, val_node: ast.AST) -> None:
        if any(k == tid for k, _ in assigns):
            ambiguous.add(tid)
        assigns.append((tid, val_node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            _record_var(node.targets[0].id, node.value)
        elif isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    _record_var(item.optional_vars.id, item.context_expr)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in _WRITE_PATH_METHODS:
                has_write = True
            cname = _dotted_name(node.func, aliases)
            if cname in _WRITE_PATH_FUNCS or (cname in ("open", "io.open", "os.open") and _open_write_state(cname, node, aliases) is not False):
                has_write = True

    for _ in range(3):
        for tid, val_node in assigns:
            var_env[tid] = None if tid in ambiguous else _resolve_expr(val_node, var_env, aliases)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        cname = _dotted_name(node.func, aliases)
        if cname == "getattr" and len(node.args) >= 2:
            if _resolve_expr(node.args[1], var_env, aliases) in ("system", "popen", "Popen", "check_output", "check_call"):
                return True
        if cname == "__import__" and node.args and _resolve_expr(node.args[0], var_env, aliases) in ("subprocess", "pty"):
            return True
        if cname in _SHELL_EXEC_METHODS or cname in _PROC_EXEC_METHODS or cname.startswith(_OS_SPAWN_PREFIXES):
            if _check_python_exec_call(cname, node, var_env, aliases, shell_restricted_fn, is_sensitive_fn, is_guard_fn, safe_tmp_fn, depth):
                return True
        if cname in ("exec", "eval") and node.args:
            inner = node.args[0]
            resolved_exec = _resolve_expr(inner, var_env, aliases)
            if resolved_exec is None and isinstance(inner, ast.Call) and _dotted_name(inner.func, aliases) == "compile" and inner.args:
                resolved_exec = _resolve_expr(inner.args[0], var_env, aliases)
            if resolved_exec is None or python_ast_restricted(resolved_exec, shell_restricted_fn, is_sensitive_fn, is_guard_fn, safe_tmp_fn, depth=depth + 1):
                return True

        attr_name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if cname in ("open", "io.open", "os.open") and _has_starred(node):
            return True
        is_write_call = cname in _WRITE_PATH_FUNCS or attr_name in _WRITE_PATH_METHODS
        if cname in ("open", "io.open", "os.open"):
            is_write_call = _open_write_state(cname, node, aliases) is not False

        if cname in _DELETE_PATH_FUNCS:
            del_node = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "path"), None)
            if not safe_tmp_fn(_resolve_expr(del_node, var_env, aliases)):
                return True
        elif attr_name in _DELETE_PATH_METHODS and isinstance(node.func, ast.Attribute):
            if not safe_tmp_fn(_resolve_expr(node.func.value, var_env, aliases)):
                return True

        candidates: List[ast.AST] = list(node.args) + [kw.value for kw in node.keywords]
        if isinstance(node.func, ast.Attribute):
            candidates.append(node.func.value)
        for arg_node in candidates:
            p = _resolve_expr(arg_node, var_env, aliases)
            if not p or "\n" in p or len(p) > 300:
                continue
            if is_sensitive_fn(p) or ("zomi-subagent-sessions" in p and is_write_call):
                return True
            if is_guard_fn(p):
                is_safe_read = (
                    attr_name in _READ_PATH_METHODS
                    or (cname in ("open", "io.open", "os.open") and not is_write_call)
                    or cname in ("pathlib.Path", "Path", "ast.parse", "print", "str")
                )
                if is_write_call or (has_write and not is_safe_read):
                    return True
    return False
