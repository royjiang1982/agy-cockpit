#!/usr/bin/env python3
"""Shell cwd tracking, redirection, lookup, tmp-delete, and CLI helpers for sa-guard."""
from __future__ import annotations

from bisect import bisect_right
from functools import lru_cache
import importlib.util
from pathlib import Path
import re
import shlex
from typing import Callable, List, Optional, Sequence, Tuple

_REDIR_TARGET = r"""(?:[^\s;&|()<>"']+|"[^"\r\n]*"|'[^'\r\n]*')+"""
_REDIR_RE = re.compile(rf"""(?:[0-9]+)?(?P<op>>\||>>?|<(?!<))(?![<&>])[ \t]*(?P<target>{_REDIR_TARGET})""")
_TMP_DELETE_RE = re.compile(r"^(?:/tmp/[^/]+|(?:tmp|\./tmp|(?:[^\s;&|]+/)?tmp)(?:/|$))")
_CD_HEAD_RE = re.compile(r"^[ \t]*(?:\{[ \t]+)?(?:builtin[ \t]+)?cd(?=[ \t]|$)")
_EXIT_RE = re.compile(r"^[ \t]*(?:exit|return)(?:[ \t;)]|$)")


@lru_cache(maxsize=4)
def _load_sibling(filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_").removesuffix(".py"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def safe_tmp_target(path: Optional[str]) -> bool:
    """Return True only when `path` is a safe explicit temporary subpath target."""
    if not path or "zomi-subagent-sessions" in path or ".." in path.split("/"):
        return False
    return bool(_TMP_DELETE_RE.match(path)) and not any(c in path for c in "$`\\")


def is_command_lookup_prefix(prefix: str) -> bool:
    """Return True when the matched wrapper prefix ends in non-executing `command -v/-V`."""
    cleaned = re.sub(r"^[\s;&|(`$]+", "", prefix).strip()
    if not cleaned:
        return False
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        return False
    cmd_idx = next((idx for idx in range(len(tokens) - 1, -1, -1) if tokens[idx] == "command"), None)
    if cmd_idx is None:
        return False
    lookup = False
    for tok in tokens[cmd_idx + 1 :]:
        if tok == "--":
            break
        if tok.startswith("-") and len(tok) > 1 and any(ch in tok[1:] for ch in ("v", "V")):
            lookup = True
        elif not tok.startswith("-"):
            return False
    return lookup


def _unquote_word(raw_target: str) -> str:
    try:
        parts = shlex.split(raw_target)
        if len(parts) == 1:
            return parts[0]
    except ValueError:
        pass
    return raw_target.replace("'", "").replace('"', "")


def _parse_cd_target(raw_seg: str) -> Optional[str]:
    m = _CD_HEAD_RE.match(raw_seg)
    if not m:
        return None
    lexer = shlex.shlex(raw_seg[m.end() :], posix=True, punctuation_chars="><")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        toks = list(lexer)
    except ValueError:
        return None
    clean: List[str] = []
    idx, n = 0, len(toks)
    while idx < n:
        tok = toks[idx]
        if tok.startswith((">", "<")):
            idx += 1
            if tok in (">", "<") and idx < n and toks[idx] in ("|", "&"):
                tok_op = toks[idx]
                idx += 1
                if idx < n:
                    idx += 1 if tok_op == "|" or not toks[idx].startswith("&") or len(toks[idx]) > 1 else 2
            elif not tok.endswith(("&", "|")) and idx < n:
                idx += 1 if not toks[idx].startswith(("&", "|")) or len(toks[idx]) > 1 else 2
            continue
        if idx + 1 < n and (tok.isdigit() or tok == "&") and toks[idx + 1].startswith((">", "<")):
            idx += 2
            if idx < n and toks[idx] == "|":
                idx += 1
            if idx < n:
                idx += 1 if not toks[idx].startswith("&") or len(toks[idx]) > 1 else 2
            continue
        clean.append(tok)
        idx += 1
    opts = True
    for tok in clean:
        if opts and tok == "--":
            opts = False
            continue
        if opts and tok != "-" and tok.startswith("-") and all(c in "LPe@" for c in tok[1:]):
            continue
        return tok
    return "~"


def _dedupe_dirs(items: Sequence[Optional[str]]) -> Tuple[Optional[str], ...]:
    seen: List[Optional[str]] = []
    for item in items:
        if item not in seen:
            seen.append(item)
            if len(seen) >= 16:
                break
    return tuple(seen)


def build_dir_resolver(
    scan_cmd: str,
    raw_cmd: str,
    unmasked_spans: Sequence[Tuple[int, int]],
    normalize_fn: Callable[[str, Optional[str]], str],
    initial_dir: Optional[str],
) -> Callable[[int], Tuple[Optional[str], ...]]:
    """Track unquoted top-level/subshell `cd` changes (including `||` branches) across `scan_cmd`."""
    cur_dirs: Tuple[Optional[str], ...] = (initial_dir,)
    prev_dirs: Tuple[Optional[str], ...] = (initial_dir,)
    or_saved: Optional[Tuple[Optional[str], ...]] = None
    breaks: List[Tuple[int, Tuple[Optional[str], ...]]] = [(0, cur_dirs)]
    stack: List[Tuple[Tuple[Optional[str], ...], Tuple[Optional[str], ...], Optional[Tuple[Optional[str], ...]]]] = []
    seg_start, span_idx, i, n = 0, 0, 0, len(scan_cmd)

    def _step(raw_seg: str, bases: Tuple[Optional[str], ...], prevs: Tuple[Optional[str], ...]) -> Optional[Tuple[Tuple[Optional[str], ...], Tuple[Optional[str], ...]]]:
        target = _parse_cd_target(raw_seg)
        if target is None:
            return None
        if target == "-":
            return prevs, bases
        out: List[Optional[str]] = []
        for bd in bases:
            norm = normalize_fn(target, bd)
            out.append(bd if any(c in norm for c in "$`") else norm)
        return _dedupe_dirs(out), bases

    def _finalize(end_pos: int, record_pos: int, reset_or: bool = True) -> None:
        nonlocal cur_dirs, prev_dirs, or_saved
        raw_seg = raw_cmd[seg_start:end_pos]
        stepped = _step(raw_seg, cur_dirs, prev_dirs)
        if stepped is not None:
            cur_dirs, prev_dirs = stepped
        if reset_or and or_saved is not None:
            cur_dirs = or_saved if _EXIT_RE.match(raw_seg) else _dedupe_dirs(or_saved + cur_dirs)
            or_saved = None
        if cur_dirs != breaks[-1][1]:
            breaks.append((record_pos, cur_dirs))

    while i < n:
        while span_idx < len(unmasked_spans) and i >= unmasked_spans[span_idx][1]:
            span_idx += 1
        if span_idx < len(unmasked_spans) and unmasked_spans[span_idx][0] <= i < unmasked_spans[span_idx][1]:
            i = seg_start = unmasked_spans[span_idx][1]
            span_idx += 1
            continue
        ch = scan_cmd[i]
        if ch == "(":
            stack.append((cur_dirs, prev_dirs, or_saved))
            or_saved = None
            seg_start = i + 1
            i += 1
        elif ch == ")":
            _finalize(i, i)
            if stack:
                cur_dirs, prev_dirs, or_saved = stack.pop()
                breaks.append((i + 1, cur_dirs))
            seg_start = i + 1
            i += 1
        elif ch in (";", "\n"):
            _finalize(i, i + 1)
            seg_start = i + 1
            i += 1
        elif ch == "&" and (i == 0 or scan_cmd[i - 1] not in "<>|") and scan_cmd[i : i + 2] != "&>":
            op_len = 2 if scan_cmd[i : i + 2] == "&&" else 1
            if op_len == 2:
                _finalize(i, i + 2)
            else:
                or_saved = None
            seg_start = i + op_len
            i += op_len
        elif ch == "|" and (i == 0 or scan_cmd[i - 1] != ">"):
            op_len = 2 if scan_cmd[i : i + 2] in ("||", "|&") else 1
            if scan_cmd[i : i + 2] == "||":
                stepped = _step(raw_cmd[seg_start:i], cur_dirs, prev_dirs)
                succ_dirs = stepped[0] if stepped is not None else cur_dirs
                or_saved = _dedupe_dirs((or_saved or ()) + succ_dirs)
            seg_start = i + op_len
            i += op_len
        else:
            i += 1

    positions = [pos for pos, _ in breaks]
    dirs = [d for _, d in breaks]
    return lambda pos: dirs[bisect_right(positions, pos) - 1]


def redirection_restricted(
    scan_cmd: str,
    raw_cmd: str,
    is_sensitive_fn: Callable[[str], bool],
    is_guard_fn: Callable[[str], bool],
    base_dir_for_pos: Optional[Callable[[int], object]] = None,
) -> bool:
    """Inspect unquoted shell redirection targets (> / >> / >| / <)."""
    for m in _REDIR_RE.finditer(scan_cmd):
        op = m.group("op")
        raw_target = raw_cmd[m.start("target") : m.end("target")].strip()
        if not raw_target or raw_target == "/dev/null" or raw_target.startswith("&"):
            continue
        target = _unquote_word(raw_target)
        raw_dirs = base_dir_for_pos(m.start()) if base_dir_for_pos else (None,)
        dirs = raw_dirs if isinstance(raw_dirs, tuple) else (raw_dirs,)
        for cur_dir in dirs:
            kw = {"base_dir": cur_dir} if cur_dir is not None else {}
            if is_sensitive_fn(target, **kw) or (op.startswith(">") and is_guard_fn(target, **kw)):  # type: ignore[call-arg]
                return True
    return False


def script_heredoc_restricted(
    kind: str,
    body: str,
    shell_restricted_fn: Callable[[str], bool],
    is_sensitive_fn: Callable[[str], bool],
    is_guard_fn: Callable[[str], bool],
) -> bool:
    """Inspect extracted quoted heredoc script content according to interpreter kind."""
    if kind == "data":
        return False
    if kind == "python":
        return _load_sibling("sa-guard-ast.py").python_ast_restricted(
            body, shell_restricted_fn, is_sensitive_fn, is_guard_fn, safe_tmp_target
        )
    if kind == "js":
        return _load_sibling("sa-guard-js.py").js_script_restricted(
            body, shell_restricted_fn, is_sensitive_fn, is_guard_fn, safe_tmp_target
        )
    return True


def read_cli_restricted(
    name: str,
    args: Sequence[str],
    shell_restricted_fn: Callable[[str], bool],
    is_sensitive_fn: Callable[[str], bool],
    is_guard_fn: Callable[[str], bool],
) -> bool:
    """Validate `_READ_CLIS` arguments including inline `python -c` and `sed -i` targets."""
    if name in ("python", "python3"):
        for idx, arg in enumerate(args[:-1]):
            if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
                if _load_sibling("sa-guard-ast.py").python_ast_restricted(
                    args[idx + 1], shell_restricted_fn, is_sensitive_fn, is_guard_fn, safe_tmp_target
                ):
                    return True
                non_code = [a for j, a in enumerate(args) if j != idx + 1]
                return any(is_sensitive_fn(a) for a in non_code)
    if name == "sed" and any(re.match(r"^-[Eernzsu]*i", a) or a.startswith("--in-place") for a in args):
        if any(is_sensitive_fn(arg) or is_guard_fn(arg) for arg in args):
            return True
    return any(is_sensitive_fn(arg) for arg in args)
