#!/usr/bin/env python3
"""Node/JS embedded script static inspection for sa-guard."""
from __future__ import annotations

import posixpath
import re
import shlex
from typing import Callable, List, Optional, Tuple

_JS_CHILD_PROC_RE = re.compile(
    r"""require\s*\(\s*['"](?:node:)?child_process['"]\s*\)|\bfrom\s*['"](?:node:)?child_process['"]|\b(?:execSync|execFileSync|spawnSync|execFile)\s*\("""
)
_JS_EXEC_CALL_RE = re.compile(r"""\b(?P<fn>exec|execSync|execFile|execFileSync|spawn|spawnSync|fork)\s*\(""")
_JS_FS_CALL_RE = re.compile(
    r"""\b(?P<fn>readFile|readFileSync|writeFile|writeFileSync|appendFile|appendFileSync|unlink|unlinkSync|rm|rmSync|rmdir|rmdirSync|rename|renameSync|open|openSync|createReadStream|createWriteStream)\s*\("""
)
_JS_FS_MOD_RE = re.compile(
    r"""require\s*\(\s*['"](?:node:)?fs(?:/promises)?['"]\s*\)|\bfrom\s*['"](?:node:)?fs(?:/promises)?['"]"""
)
_JS_LIT_BODY = r"""(?:'(?P<sq>(?:\\.|[^'\\])*)'|"(?P<dq>(?:\\.|[^"\\])*)"|`(?P<bq>(?:\\.|[^`\\])*)`)"""
_JS_STR_RE = re.compile(rf"""^\s*{_JS_LIT_BODY}\s*(?=[,)\]])""")
_JS_OPT_STR_VAL_RE = re.compile(rf"""^{_JS_LIT_BODY}$""")
_JS_SAFE_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}


_JS_SIMPLE_ESC = {
    "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v",
    "\\": "\\", "'": "'", '"': '"', "`": "`", "/": "/",
}
_HEX_CHARS = set("0123456789abcdefABCDEF")


def _decode_js_str(raw: str) -> Optional[str]:
    """Decode standard JS string escapes, returning None on ambiguous or malformed escapes."""
    if "\\" not in raw:
        return raw
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            return None
        esc = raw[i + 1]
        if esc in _JS_SIMPLE_ESC:
            out.append(_JS_SIMPLE_ESC[esc])
            i += 2
        elif esc == "\n":
            i += 2
        elif esc == "\r":
            i += 3 if i + 2 < n and raw[i + 2] == "\n" else 2
        elif esc == "x" and i + 3 < n and all(c in _HEX_CHARS for c in raw[i + 2 : i + 4]):
            out.append(chr(int(raw[i + 2 : i + 4], 16)))
            i += 4
        elif esc == "u":
            if i + 2 < n and raw[i + 2] == "{":
                end = raw.find("}", i + 3)
                hx = raw[i + 3 : end] if end > i + 3 else ""
                if not hx or not all(c in _HEX_CHARS for c in hx) or int(hx, 16) > 0x10FFFF:
                    return None
                out.append(chr(int(hx, 16)))
                i = end + 1
            elif i + 5 < n and all(c in _HEX_CHARS for c in raw[i + 2 : i + 6]):
                out.append(chr(int(raw[i + 2 : i + 6], 16)))
                i += 6
            else:
                return None
        elif esc == "0" and (i + 2 >= n or raw[i + 2] not in "01234567"):
            out.append("\0")
            i += 2
        elif esc not in ("x", "u") and not ("0" <= esc <= "7"):
            out.append(esc)
            i += 2
        else:
            return None
    return "".join(out)


def _js_lit_val(m: re.Match) -> Optional[str]:
    """Return decoded string value from a `_JS_LIT_BODY` match, or None if template/invalid."""
    if m.group("sq") is not None:
        return _decode_js_str(m.group("sq"))
    if m.group("dq") is not None:
        return _decode_js_str(m.group("dq"))
    bq = m.group("bq")
    return None if "${" in bq else _decode_js_str(bq)


def _extract_js_braced(text: str, pos: int) -> Optional[Tuple[str, int]]:
    """Extract a balanced `{...}` JS options object literal starting at `pos`."""
    depth, quote, i, n = 0, "", pos, len(text)
    while i < n:
        ch = text[i]
        if quote:
            i += 2 if ch == "\\" else 1
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[pos : i + 1], i + 1
        i += 1
    return None


_TEMPLATE_TOKEN_RE = re.compile(r"\\(?:\r\n|[^\r]|\r)|\$\{", re.DOTALL)
_GUARD_COMPONENT_RE = re.compile(r"hooks\.json|sa-guard(?:-[A-Za-z0-9_.-]+|\.sh)?")


def _path_fragment_restricted(fragment, is_sensitive_fn, is_guard_fn):
    if fragment is None:
        return True  # An undecodable literal is not a known-safe path.
    path = fragment.replace("\\", "/").strip("/")
    if is_sensitive_fn(fragment) or is_guard_fn(fragment) or is_guard_fn("~/" + path):
        return True
    parts = path.split("/")
    return any(_GUARD_COMPONENT_RE.fullmatch(part) or is_sensitive_fn(part)
               or is_sensitive_fn("~/" + "/".join(parts[i:i + 2])) for i, part in enumerate(parts))


def _template_path_restricted(raw, is_sensitive_fn, is_guard_fn):
    # Scan individual literals and join adjacent constant pieces, without evaluating JS.
    runs = [[]]
    start = 0
    while True:
        token = _TEMPLATE_TOKEN_RE.search(raw, start)
        while token and token[0] != "${":
            token = _TEMPLATE_TOKEN_RE.search(raw, token.end())
        fragment = _decode_js_str(raw[start:token.start() if token else len(raw)])
        if _path_fragment_restricted(fragment, is_sensitive_fn, is_guard_fn):
            return True
        runs[-1].append(fragment)
        if not token:
            break
        braced = _extract_js_braced(raw, token.start() + 1)
        if not braced:
            return True
        expression, start = braced
        expression = expression[1:-1].strip()
        constant = _JS_OPT_STR_VAL_RE.fullmatch(expression)
        if constant:
            value = _js_lit_val(constant)
            if _path_fragment_restricted(value, is_sensitive_fn, is_guard_fn):
                return True
            runs[-1].append(value)
            continue
        runs.append([])
        for literal in re.finditer(_JS_LIT_BODY, expression):
            if _path_fragment_restricted(_js_lit_val(literal), is_sensitive_fn, is_guard_fn):
                return True
    paths = ["".join(run) for run in runs]
    if len(paths) > 1:
        paths.append("<dynamic>".join(paths))
    return any(_path_fragment_restricted(path, is_sensitive_fn, is_guard_fn) for path in paths)


def _parse_js_top_level_props(opts_src: str) -> Optional[List[Tuple[str, Optional[str]]]]:
    """Parse top-level keys and raw values from a balanced `{...}` JS options object."""
    props: List[Tuple[str, Optional[str]]] = []
    i, n = 1, len(opts_src) - 1
    while i < n:
        while i < n and opts_src[i].isspace():
            i += 1
        if i >= n:
            break
        if opts_src[i] in ("'", '"'):
            q = opts_src[i]
            i += 1
            buf: List[str] = []
            while i < n and opts_src[i] != q:
                if opts_src[i] == "\\":
                    if i + 1 >= n:
                        return None
                    buf.append(opts_src[i + 1])
                    i += 2
                else:
                    buf.append(opts_src[i])
                    i += 1
            if i >= n:
                return None
            key = "".join(buf)
            i += 1
        elif opts_src[i].isalpha() or opts_src[i] in "_$":
            k0 = i
            while i < n and (opts_src[i].isalnum() or opts_src[i] in "_$"):
                i += 1
            key = opts_src[k0:i]
        else:
            return None
        while i < n and opts_src[i].isspace():
            i += 1
        if i >= n:
            props.append((key, None))
            break
        if opts_src[i] == ",":
            props.append((key, None))
            i += 1
            continue
        if opts_src[i] != ":":
            return None
        i += 1
        while i < n and opts_src[i].isspace():
            i += 1
        v0, depth, quote = i, 0, ""
        while i < n:
            ch = opts_src[i]
            if quote:
                i += 2 if ch == "\\" else 1
                if ch == quote:
                    quote = ""
                continue
            if ch in ("'", '"', "`"):
                quote = ch
            elif ch in "({[":
                depth += 1
            elif ch in ")}]":
                if depth == 0:
                    return None
                depth -= 1
            elif ch == "," and depth == 0:
                break
            i += 1
        if quote or depth != 0:
            return None
        val = opts_src[v0:i].strip()
        if not val:
            return None
        props.append((key, val))
        if i < n and opts_src[i] == ",":
            i += 1
    return props


def _inspect_js_exec_opts(
    opts_src: Optional[str],
    is_sensitive_fn: Callable[[str], bool],
    is_guard_fn: Callable[[str], bool],
) -> Tuple[bool, bool, Optional[str]]:
    """Return `(restricted, use_shell, cwd)` from parsed top-level JS child_process options."""
    if not opts_src:
        return False, False, None
    props = _parse_js_top_level_props(opts_src)
    if props is None:
        return True, True, None
    use_shell = False
    cwd_val: Optional[str] = None
    for key, val in props:
        if key == "shell":
            if val is None:
                return True, True, None
            if val in ("false", "undefined", "null"):
                use_shell = False
            elif val == "true":
                use_shell = True
            else:
                ms = _JS_OPT_STR_VAL_RE.match(val)
                sval = _js_lit_val(ms) if ms else None
                if sval is None or posixpath.basename(sval) not in _JS_SAFE_SHELLS:
                    return True, True, None
                use_shell = True
        elif key == "cwd":
            if val is None:
                return True, use_shell, None
            if val in ("undefined", "null"):
                cwd_val = None
                continue
            ms = _JS_OPT_STR_VAL_RE.match(val)
            cwd_val = _js_lit_val(ms) if ms else None
            if cwd_val is None or is_sensitive_fn(cwd_val) or is_guard_fn(cwd_val):
                return True, use_shell, None
    return False, use_shell, cwd_val


def _parse_js_call(body: str, pos: int) -> Optional[Tuple[List[str], Optional[str]]]:
    """Parse static command string, optional string array, and optional `{...}` options."""
    rest = body[pos:]
    m0 = _JS_STR_RE.match(rest)
    v0 = _js_lit_val(m0) if m0 else None
    if v0 is None:
        return None
    parts, idx = [v0], m0.end()
    tail = rest[idx:].lstrip()
    if tail.startswith(")"):
        return parts, None
    if not tail.startswith(","):
        return None
    idx = len(rest) - len(tail) + 1
    after = rest[idx:].lstrip()
    idx = len(rest) - len(after)
    if after.startswith("["):
        idx += 1
        while idx < len(rest):
            sub = rest[idx:].lstrip()
            idx = len(rest) - len(sub)
            if sub.startswith("]"):
                idx += 1
                break
            ms = _JS_STR_RE.match(sub)
            vi = _js_lit_val(ms) if ms else None
            if vi is None:
                return None
            parts.append(vi)
            idx += ms.end()
            sep = rest[idx:].lstrip()
            idx = len(rest) - len(sep)
            if sep.startswith(","):
                idx += 1
            elif not sep.startswith("]"):
                return None
        else:
            return None
        tail = rest[idx:].lstrip()
        if tail.startswith(")"):
            return parts, None
        if not tail.startswith(","):
            return None
        idx = len(rest) - len(tail) + 1
        after = rest[idx:].lstrip()
        idx = len(rest) - len(after)
    if after.startswith("{"):
        braced = _extract_js_braced(rest, idx)
        if not braced:
            return None
        opts_src, next_idx = braced
        return (parts, opts_src) if rest[next_idx:].lstrip().startswith(")") else None
    return None


def js_script_restricted(
    body: str,
    shell_restricted_fn: Callable[[str], bool],
    is_sensitive_fn: Callable[[str], bool],
    is_guard_fn: Callable[[str], bool],
    safe_tmp_fn: Callable[[Optional[str]], bool],
) -> bool:
    """Conservatively inspect Node/JS snippets."""
    if _JS_CHILD_PROC_RE.search(body):
        matched_any = False
        for call_m in _JS_EXEC_CALL_RE.finditer(body):
            matched_any = True
            fn = call_m.group("fn")
            parsed = _parse_js_call(body, call_m.end())
            if not parsed:
                return True
            args, opts_src = parsed
            opts_bad, use_shell, cwd_val = _inspect_js_exec_opts(opts_src, is_sensitive_fn, is_guard_fn)
            if opts_bad:
                return True
            raw_shell = (fn in ("exec", "execSync") and len(args) == 1) or use_shell
            base_cmd = " ".join(args) if raw_shell else " ".join(shlex.quote(a) for a in args)
            cmd_str = f"cd {shlex.quote(cwd_val)} && {base_cmd}" if cwd_val else base_cmd
            if shell_restricted_fn(cmd_str):
                return True
        if not matched_any:
            return True
    for fs_m in _JS_FS_CALL_RE.finditer(body):
        if fs_m.group("fn") in ("unlink", "unlinkSync", "rm", "rmSync", "rmdir", "rmdirSync"):
            parsed = _parse_js_call(body, fs_m.end())
            if not parsed or not safe_tmp_fn(parsed[0][0]):
                return True
    has_fs = bool(_JS_FS_MOD_RE.search(body) or _JS_FS_CALL_RE.search(body))
    for m in re.finditer(_JS_LIT_BODY, body):
        lit = _js_lit_val(m)
        if lit is None:
            template = m.group("bq")
            if has_fs and (template is None or "${" not in template or
                           _template_path_restricted(template, is_sensitive_fn, is_guard_fn)):
                return True
            continue
        if len(lit) <= 260 and (is_sensitive_fn(lit) or (has_fs and is_guard_fn(lit))):
            return True
    return False
