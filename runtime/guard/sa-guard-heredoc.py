#!/usr/bin/env python3
"""Shell boundary masking and heredoc inspection/extraction for sa-guard."""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

_PATH_ATOM = r"""(?:[A-Za-z0-9_./:~-]+|'[^'\r\n$`\\]*'|"[^"\r\n$`\\]*")+"""
_HD_OP = r"""<<(?P<strip>-?)[ \t]*(?P<delim>(?:[^\s;&|()<>"'$`\\]+|'[^'\r\n]*'|"[^"\r\n$`\\]*"|\\[^\r\n])+)"""
_CHAIN_TAIL = r"""(?:[ \t]*(?:[);&|#]|&&|\|\|).*)?"""

_CAT_HDR_1 = re.compile(
    rf"""^[ \t]*cat[ \t]+(?:[0-9]*>>?)[ \t]*(?P<target>{_PATH_ATOM})[ \t]+{_HD_OP}{_CHAIN_TAIL}$"""
)
_CAT_HDR_2 = re.compile(
    rf"""^[ \t]*cat[ \t]+{_HD_OP}[ \t]+(?:[0-9]*>>?)[ \t]*(?P<target>{_PATH_ATOM}){_CHAIN_TAIL}$"""
)
_CAT_HDR_3 = re.compile(rf"""^[ \t]*cat[ \t]+{_HD_OP}(?:[ \t]*(?:[;&|#]|&&|\|\|).*)?$""")
_ENV_WRAP = (
    r"""(?:[A-Za-z_][A-Za-z0-9_]*=(?:[^\s;&|"']+|"[^"\r\n]*"|'[^'\r\n]*')[ \t]+"""
    r"""|env[ \t]+(?:(?:-[iuCS]|--unset|--chdir)[ \t]+[^\s;&|]+[ \t]+|--?[^\s;&|]+[ \t]+)*)*"""
)
_SCRIPT_HDR = re.compile(
    rf"""^[ \t]*{_ENV_WRAP}(?:[^\s;&|"'()]*/)?(?P<interp>python3|python|node)"""
    rf"""(?:[ \t]+(?:--?[A-Za-z0-9_.-]+(?:=[^\s;&|]+)?|[0-9]*>>?[ \t]*{_PATH_ATOM}|[0-9]*>&\d|-))*"""
    rf"""[ \t]*{_HD_OP}(?:[ \t]+(?:[0-9]*>>?[ \t]*{_PATH_ATOM}|[0-9]*>&\d))*{_CHAIN_TAIL}$"""
)
_ANY_HD_OP = re.compile(r"""(?<!<)<<(?P<strip>-?)(?![<&>])[ \t]*""")


def _scan_shell(
    command: str,
    stack: Optional[List[object]] = None,
    skip_spans: Sequence[Tuple[int, int]] = (),
) -> Tuple[str, List[str], List[object]]:
    """Scan POSIX shell text, masking separators/redirects inside quotes and extracting backticks."""
    st: List[object] = list(stack) if stack is not None else []
    out, n, i, word_start, backticks = list(command), len(command), 0, not st, []
    sp_idx, n_spans = 0, len(skip_spans)
    while i < n:
        ch = command[i]
        if not st and sp_idx < n_spans:
            while sp_idx < n_spans and i >= skip_spans[sp_idx][1]:
                sp_idx += 1
            if sp_idx < n_spans and skip_spans[sp_idx][0] <= i < skip_spans[sp_idx][1]:
                if ch == "\\":
                    if i + 1 >= n:
                        return command, [], st
                    if command[i + 1] in ";&|()`'\"$<>":
                        out[i + 1] = "_"
                    i += 2
                    continue
                if ch == "$" and i + 1 < n and command[i + 1] == "(":
                    st.append(["CS", 1])
                    word_start = True
                    i += 2
                    continue
                if ch == "`":
                    out[i] = "_"
                    st.append(["BT", [], False])
                    word_start = False
                    i += 1
                    continue
                if ch in ";&|()`'\"$<>":
                    out[i] = "_"
                word_start = ch in " \t\r\n"
                i += 1
                continue
        top = st[-1] if st else None
        kind = top[0] if isinstance(top, list) else top
        if kind == "SQ":
            if ch == "'":
                st.pop()
            elif ch in ";&|()`\n<>{}":
                out[i] = "_"
            i += 1
        elif kind == "ANSISQ":
            if ch == "\\":
                if i + 1 >= n:
                    return command, [], st
                if command[i + 1] in ";&|()`\n\"'$<>{}":
                    out[i + 1] = "_"
                i += 2
            elif ch == "'":
                st.pop()
                i += 1
            else:
                if ch in ";&|()`\n<>{}":
                    out[i] = "_"
                i += 1
        elif kind == "BT":
            _, buf, in_dq = top  # type: ignore[misc]
            out[i] = "_"
            if ch == "\\":
                if i + 1 >= n:
                    return command, [], st
                out[i + 1] = "_"
                nxt = command[i + 1]
                if nxt in ("$", "`", "\\") or (in_dq and nxt == '"'):
                    buf.append(nxt)
                elif nxt != "\n":
                    buf.extend(("\\", nxt))
                i += 2
            elif ch == "`":
                st.pop()
                backticks.append("".join(buf))
                i += 1
            else:
                buf.append(ch)
                i += 1
        elif kind == "DQ":
            if ch == "\\":
                if i + 1 >= n:
                    return command, [], st
                if command[i + 1] in ";&|()`\n\"$<>{}":
                    out[i + 1] = "_"
                i += 2
            elif ch == '"':
                st.pop()
                i += 1
            elif ch == "$" and i + 1 < n and command[i + 1] == "(":
                st.append(["CS", 1])
                word_start = True
                i += 2
            elif ch == "`":
                out[i] = "_"
                st.append(["BT", [], True])
                i += 1
            else:
                if ch in ";&|()\n<>{}":
                    out[i] = "_"
                i += 1
        elif ch == "\\":
            if i + 1 >= n:
                return command, [], st
            if command[i + 1] == "\n":
                out[i] = out[i + 1] = " "
            else:
                if command[i + 1] in ";&|()`'\"$<>{}":
                    out[i + 1] = "_"
                word_start = False
            i += 2
        elif ch == "#" and word_start:
            while i < n and command[i] != "\n":
                out[i] = " "
                i += 1
        elif ch == "$" and i + 1 < n and command[i + 1] == "'":
            st.append("ANSISQ")
            word_start = False
            i += 2
        elif ch == "'":
            st.append("SQ")
            word_start = False
            i += 1
        elif ch == '"':
            st.append("DQ")
            word_start = False
            i += 1
        elif ch == "`":
            out[i] = "_"
            st.append(["BT", [], False])
            word_start = False
            i += 1
        elif ch == "$" and i + 1 < n and command[i + 1] == "(":
            st.append(["CS", 1])
            word_start = True
            i += 2
        elif kind == "CS" and ch in "()":
            top[1] += 1 if ch == "(" else -1  # type: ignore[index]
            if top[1] == 0:
                st.pop()
            word_start = ch == "("
            i += 1
        else:
            word_start = ch in " \t\r\n;&|("
            i += 1
    return (command, [], st) if st else ("".join(out), backticks, st)


def command_boundary_view(
    command: str, skip_spans: Sequence[Tuple[int, int]] = ()
) -> Tuple[str, List[str]]:
    """Public shared shell-boundary masker returning (masked_cmd, backtick_bodies)."""
    masked, backticks, st = _scan_shell(command, skip_spans=skip_spans)
    return (command, []) if st else (masked, backticks)


def _match_recognized_header(
    line_no_nl: str, masked_line: str, *, allow_cat_stdout: bool = False
) -> Optional[Tuple[str, str, bool, int]]:
    """Match a cat or python/node quoted heredoc header (standalone or tail-chained)."""
    if "<<<" in masked_line:
        return None
    hd_ops = list(_ANY_HD_OP.finditer(masked_line))
    if len(hd_ops) != 1:
        return None
    before_hd = masked_line[: hd_ops[0].start()]
    m_sep = re.search(r"^.*(?:&&|\|\||[;&|(])[ \t]*", before_hd)
    starts = (0, m_sep.end()) if m_sep and m_sep.end() > 0 else (0,)
    for start in starts:
        if re.search(r"(?<!\|)\|(?!\|)", masked_line[start:]):
            continue
        seg = line_no_nl[start:]
        for rx in (_CAT_HDR_1, _CAT_HDR_2, _CAT_HDR_3):
            if rx is _CAT_HDR_3 and (
                not allow_cat_stdout
                or any(c in masked_line[:start] for c in "({")
                or any(c in masked_line[start:] for c in ")}")
            ):
                continue
            m = rx.fullmatch(seg)
            if m and any(c in m.group("delim") for c in ("'", '"', "\\")):
                delim = _parse_heredoc_delim(m.group("delim"), 0)
                if delim:
                    return "data", delim, m.group("strip") == "-", start
        m = _SCRIPT_HDR.fullmatch(seg)
        if m and any(c in m.group("delim") for c in ("'", '"', "\\")):
            delim = _parse_heredoc_delim(m.group("delim"), 0)
            if delim:
                kind = "js" if m.group("interp") == "node" else "python"
                return kind, delim, m.group("strip") == "-", start
    return None


def _parse_heredoc_delim(text: str, pos: int) -> str:
    """Parse one POSIX heredoc delimiter word (quoted, escaped, or bare) starting at pos."""
    out: List[str] = []
    i, n = pos, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n;&|()<>":
            break
        if ch == "'":
            end = text.find("'", i + 1)
            if end < 0:
                return ""
            out.append(text[i + 1 : end])
            i = end + 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n and text[j + 1] in ('"', "$", "`", "\\"):
                    out.append(text[j + 1])
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            if j >= n:
                return ""
            i = j + 1
        elif ch == "\\" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _find_unmasked_heredocs(line_no_nl: str, masked_line: str) -> List[Tuple[str, bool]]:
    """Collect any unquoted POSIX heredoc openers on a line in order."""
    openers: List[Tuple[str, bool]] = []
    for m in _ANY_HD_OP.finditer(masked_line):
        delim = _parse_heredoc_delim(line_no_nl, m.end())
        if delim:
            openers.append((delim, m.group("strip") == "-"))
    return openers


def _find_heredoc_end(lines: List[str], start: int, delim: str, strip_tabs: bool) -> Optional[int]:
    for j in range(start, len(lines)):
        cand = lines[j].rstrip("\r\n")
        if (cand.lstrip("\t") if strip_tabs else cand) == delim:
            return j
    return None


def _skip_unmasked_heredocs(lines: List[str], start: int, openers: List[Tuple[str, bool]]) -> Optional[int]:
    cursor = start
    for delim, strip_tabs in openers:
        end = _find_heredoc_end(lines, cursor, delim, strip_tabs)
        if end is None:
            return None
        cursor = end + 1
    return cursor


def mask_literal_heredocs(
    command: str,
    kinds: Tuple[str, ...] = ("data", "python", "js"),
    positions: Optional[List[int]] = None,
    unmasked_spans: Optional[List[Tuple[int, int]]] = None,
) -> Tuple[str, List[Tuple[str, str]]]:
    """Mask top-level recognized heredocs and record/preserve unrecognized heredoc bodies."""
    lines = command.splitlines(keepends=True)
    result: List[str] = []
    heredocs: List[Tuple[str, str]] = []
    stack: List[object] = []
    i, n, out_len, paren_depth, brace_depth = 0, len(lines), 0, 0, 0
    while i < n:
        line = lines[i]
        line_no_nl = line.rstrip("\r\n")
        was_empty = not stack
        masked_line, _, next_stack = _scan_shell(line, stack)
        ml_no_nl = masked_line.rstrip("\r\n")
        next_paren = max(0, paren_depth + ml_no_nl.count("(") - ml_no_nl.count(")"))
        next_brace = max(0, brace_depth + ml_no_nl.count("{") - ml_no_nl.count("}"))
        if was_empty:
            matched = _match_recognized_header(
                line_no_nl, ml_no_nl, allow_cat_stdout=(paren_depth == 0 and brace_depth == 0)
            )
            if matched and matched[0] in kinds and not next_stack:
                kind, delim, strip_tabs, seg_start = matched
                end = _find_heredoc_end(lines, i + 1, delim, strip_tabs)
                if end is not None:
                    if positions is not None:
                        positions.append(out_len + seg_start)
                    body = "".join(lines[k].lstrip("\t") if strip_tabs else lines[k] for k in range(i + 1, end))
                    heredocs.append((kind, body))
                    result.append(line)
                    out_len += len(line)
                    pad = ["\n" for _ in range(i + 1, end + 1)]
                    result.extend(pad)
                    out_len += len(pad)
                    stack, paren_depth, brace_depth = [], next_paren, next_brace
                    i = end + 1
                    continue
            unmasked = _find_unmasked_heredocs(line_no_nl, ml_no_nl)
            if unmasked and not next_stack:
                cursor = _skip_unmasked_heredocs(lines, i + 1, unmasked)
                if cursor is not None:
                    chunk = lines[i:cursor]
                    chunk_len = sum(len(s) for s in chunk)
                    if unmasked_spans is not None and cursor > i + 1:
                        body_start = out_len + len(line)
                        body_end = out_len + chunk_len
                        if body_end > body_start:
                            unmasked_spans.append((body_start, body_end))
                    result.extend(chunk)
                    out_len += chunk_len
                    stack, paren_depth, brace_depth = [], next_paren, next_brace
                    i = cursor
                    continue
        result.append(line)
        out_len += len(line)
        stack, paren_depth, brace_depth = next_stack, next_paren, next_brace
        i += 1
    return "".join(result), heredocs
