"""Decode the AGY tool-step protobuf payloads used for discovery."""
from __future__ import annotations

import json
import urllib.parse

from proto_wire import decode_proto
from subagent_labels import format_native_label


def _extract_compacted_run_command(payload: bytes) -> dict:
    """Recover `CommandLine` (and optional `Cwd`) from a compacted `step_type = 132` (`status = 5`) terminal payload."""
    def _child_bytes(buf: bytes, field_num: int):
        try:
            for k, t, v in decode_proto(buf):
                if k == field_num and t == "bytes" and isinstance(v, bytes):
                    yield v
        except ValueError:
            return

    for f140 in _child_bytes(payload, 140):
        for f2 in _child_bytes(f140, 2):
            for f3 in _child_bytes(f2, 3):
                for f10 in _child_bytes(f3, 10):
                    for inner in _child_bytes(f10, 3):
                        cmd = ""
                        cwd = ""
                        try:
                            for k, t, v in decode_proto(inner):
                                if t == "bytes" and isinstance(v, bytes):
                                    if k == 1 and not cmd:
                                        cmd = v.decode("utf-8", errors="ignore")
                                    elif k == 2 and not cwd:
                                        cwd = v.decode("utf-8", errors="ignore")
                        except ValueError:
                            continue
                        if cmd:
                            out = {"CommandLine": cmd}
                            if cwd:
                                out["Cwd"] = cwd
                            return out
    return {}


def _parse_step132_payload(payload: bytes) -> tuple[str, dict, str]:
    """Extract (tool_name, args_dict, inline_output_text) from a `step_type = 132` or `127` protobuf payload."""
    tool_name = ""
    args_dict: dict = {}
    texts: list[str] = []

    def walk(buf: bytes, depth=0) -> None:
        nonlocal tool_name, args_dict
        if depth >= 8:
            return
        try:
            fields = decode_proto(buf)
        except ValueError:
            return
        for _, t, v in fields:
            if t == "bytes" and isinstance(v, bytes):
                is_leaf_text = False
                try:
                    s = v.decode("utf-8")
                    if s in ("run_command", "invoke_subagent", "send_message") and not tool_name:
                        tool_name = s
                        is_leaf_text = True
                    elif (
                        s.startswith("{")
                        and ("CommandLine" in s or "TypeName" in s or "Role" in s or "Subagents" in s or "Recipient" in s)
                        and not args_dict
                    ):
                        try:
                            args_dict = json.loads(s)
                        except Exception:
                            pass
                        is_leaf_text = True
                    elif len(s) > 10:
                        texts.append(s)
                        if all(ord(ch) >= 32 or ch in "\t\n\r" for ch in s):
                            is_leaf_text = True
                except Exception:
                    pass
                if not is_leaf_text:
                    walk(v, depth + 1)

    if payload:
        walk(payload)
        if tool_name == "run_command" and "CommandLine" not in args_dict:
            recovered = _extract_compacted_run_command(payload)
            if recovered:
                args_dict = {**recovered, **args_dict}
    return tool_name, args_dict, "\n".join(texts)


def _parse_step127_subagents(payload: bytes, args: dict) -> list[tuple[str, str, str, str]]:
    """Extract `(sub_id, role, model_hint, transcript_path)` items from a `step_type = 127` batch subagent step."""
    specs = args.get("Subagents") if isinstance(args.get("Subagents"), list) else []
    spawned: list[tuple[str, str]] = []
    for k, t, v in decode_proto(payload):
        if k == 143 and t == "bytes" and isinstance(v, bytes):
            for sk, st, sv in decode_proto(v):
                if sk == 10 and st == "bytes" and isinstance(sv, bytes):
                    sid = ""
                    tpath = ""
                    for ssk, sst, ssv in decode_proto(sv):
                        if sst == "bytes" and isinstance(ssv, bytes):
                            txt = ssv.decode("utf-8", errors="ignore").strip()
                            if ssk == 1 and txt:
                                sid = txt
                            elif ssk == 2 and "transcript" in txt:
                                raw_uri = urllib.parse.unquote(txt)
                                if raw_uri.startswith("file://"):
                                    raw_uri = raw_uri[len("file://") :]
                                tpath = raw_uri
                    if sid:
                        spawned.append((sid, tpath))
    out: list[tuple[str, str, str, str]] = []
    for i, (sid, tpath) in enumerate(spawned):
        sp = specs[i] if i < len(specs) and isinstance(specs[i], dict) else {}
        role = format_native_label(str(sp.get("Role") or sp.get("TypeName") or "subagent"), str(sp.get("Prompt") or sp.get("Task") or ""))
        m_hint = str(sp.get("Model") or "")
        out.append((sid, role, m_hint, tpath))
    return out
