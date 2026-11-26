"""LanguageServer ConnectRPC client (handoff/model sync), official CLI quota monitors, and live theme reader."""

from __future__ import annotations

from metric_rates import grok_remaining

import concurrent.futures
import contextlib
import fcntl
import http.client
import json
import os
import pty
import re
import select
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import termios
import threading
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from analyzer import (HOME, _schedule_step_expired_or_ask, _task_cleared_by_later_steps,
                      cached_paths, connect_readonly, decode_proto, decode_varint,
                      encode_proto_bytes, encode_varint, log_lines, serialized)
from agy_quota import fetch_agy_quota
from pricing import MODEL_MAP, register_dynamic_models
from quota_sources import codex_rpc_windows as _extract_codex_rpc_windows, codex_rollout_windows
from droid_ledger import parse_droid_reset_sec, droid_reset_precision, droid_probe_due, anchor_droid_quota
from subagents import CODEX_SESSIONS_DIR, FACTORY_SESSIONS_DIR, GROK_SESSIONS_DIR, parse_iso_ts

_PROBE_LOCK = threading.RLock()
_ACTIVE_PROBES: dict[int, tuple[subprocess.Popen, tuple[int, ...]]] = {}
_PROBES_PAUSED = False
_MAX_PROBE_OUTPUT = 1024 * 1024
_LAST_DROID_SUCCESS_MTIME = 0.0
_LAST_GROK_SUCCESS_MTIME = 0.0


def pause_probes():
    global _PROBES_PAUSED
    with _PROBE_LOCK:
        _PROBES_PAUSED = True


def resume_probes():
    global _PROBES_PAUSED
    with _PROBE_LOCK:
        _PROBES_PAUSED = False


def active_probe_count():
    with _PROBE_LOCK:
        return len(_ACTIVE_PROBES)


def _start_probe(*args, probe_fds=(), **kwargs):
    kwargs.update(start_new_session=True, close_fds=True)
    with _PROBE_LOCK:
        if _PROBES_PAUSED:
            raise RuntimeError('quota probes paused')
        proc = subprocess.Popen(*args, **kwargs)
        _ACTIVE_PROBES[proc.pid] = (proc, tuple(probe_fds))
        return proc


def _finish_probe(proc):
    with _PROBE_LOCK:
        entry = _ACTIVE_PROBES.pop(proc.pid, None)
    if entry is None:
        return
    # PGID is the PID captured at spawn, even if the group leader already exited.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=1.0)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
    for fd in entry[1]:
        with contextlib.suppress(OSError):
            os.close(fd)


def terminate_active_probes():
    with _PROBE_LOCK:
        active = [entry[0] for entry in _ACTIVE_PROBES.values()]
    for proc in active:
        _finish_probe(proc)


def _probe_output(command, *, timeout, text=True, stderr=subprocess.DEVNULL):
    proc = _start_probe(command, stdout=subprocess.PIPE, stderr=stderr, text=text)
    try:
        output, _ = proc.communicate(timeout=timeout)
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, command)
        return output
    finally:
        _finish_probe(proc)


_CACHED_LS_ENDPOINT: tuple[int, str] | None = None
_LAST_LS_MODEL_SYNC: tuple[tuple[int, str], float] | None = None
_CODEX_QUOTA_CACHE: tuple[tuple[str, float], dict[str, dict]] | None = None
_LAST_CODEX_SCAN_TS: float = 0.0
_DROID_QUOTA_CACHE: dict[str, dict] = {}
_LAST_DROID_FETCH_TS: float = 0.0
_LAST_DROID_SESSIONS_MTIME: float = 0.0
_GROK_QUOTA_CACHE: dict[str, dict] = {}
_LAST_GROK_FETCH_TS: float = 0.0
_LAST_GROK_SESSIONS_MTIME: float = 0.0
_AGY_CONFIG_FILE = HOME / ".gemini" / "config" / "config.json"
_THEME_CFG_MTIME_NS: int = -1
_THEME_CFG_CACHE: dict = {
    "mode": "dark",
    "dark": {"background": "#101010", "foreground": "#cccccc", "primary": "#007acc"},
    "light": {"background": "#F9F9F9", "foreground": "#101010", "primary": "#007acc"},
}


def discover_ls_endpoints(*, timeout: float = 6.0) -> list[tuple[int, str]]:
    """Find running Antigravity App language_server (port, csrf) endpoints."""
    endpoints: list[tuple[int, str]] = []
    deadline = time.monotonic() + timeout
    try:
        out = _probe_output(["ps", "-axww", "-o", "pid,command"], text=True, timeout=min(2.0, timeout))
        for line in reversed(out.splitlines()):
            if "language_server" in line and "--csrf_token" in line and "--subclient_type hub" in line:
                pid = line.strip().split()[0]
                m = re.search(r"--csrf_token\s+([^\s]+)", line)
                csrf = m.group(1) if m else ""
                if not csrf:
                    continue
                if time.monotonic() >= deadline:
                    break
                try:
                    lsof = _probe_output(["lsof", "-nP", "-p", pid], text=True, timeout=min(2.0, max(0.1, deadline - time.monotonic())))
                    ports = [int(p) for p in re.findall(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", lsof)]
                    for port in ports:
                        if (port, csrf) not in endpoints:
                            endpoints.append((port, csrf))
                except Exception:
                    continue
    except Exception:
        pass
    return endpoints


def _build_rich_session_summary_prompt(max_out_tokens: int = 16384) -> str:
    if max_out_tokens <= 8192:
        total_budget_str = "5,000–6,500 tokens (hard ceiling: 8,192 output tokens)"
        sec2_budget_str = "at most 1,800–2,000 tokens (no more than 1/3 of the summary)"
    else:
        total_budget_str = "10,000–14,000 tokens (hard ceiling: 16,384 output tokens)"
        sec2_budget_str = "at most 3,500–4,000 tokens (no more than 1/3 of the summary)"

    return (
        "You have been working on the task described above but have not yet completed\n"
        "it. Write a comprehensive, high-density continuation summary that will allow\n"
        "you (or another instance of yourself) to resume work efficiently in a future\n"
        "context window where the full conversation history will NOT be available—"
        "only this summary.\n\n"
        "This summary is all that will be available to you going forward in the future\n"
        "context window. Do not call any tools, simply provide the summary based on\n"
        "the information available in the current context window.\n\n"
        f"Target a thorough, detail-rich technical summary ({total_budget_str}\n"
        "when the session history is extensive) using a budget-disciplined two-tier\n"
        "retention strategy: preserve the most recent 3 substantive conversational\n"
        "turns and their active tool context right after the Task Overview (Section 2,\n"
        f"strictly capped at {sec2_budget_str}) while condensing older\n"
        "history and marathon-turn intermediate steps into deduplicated, high-signal\n"
        "technical facts (Sections 3–8). Always reserve sufficient output budget so\n"
        "Sections 7 (`Next Steps`), 8 (`Commitments & Constraints`), and the closing\n"
        "`</summary>` tag are complete and never truncated.\n\n"
        "Include the following sections:\n\n"
        "1. **Task Overview**\n"
        "   - Chronological progression of the user's core requests, current active\n"
        "     request, success criteria, and requested deliverables. Record any target\n"
        "     IDs, URLs, ports, or reporting recipients.\n"
        "   - Scope boundaries, task-specific constraints, and resolved ambiguities.\n\n"
        "2. **Recent 3 Substantive Turns & Active Tool Context (High-Fidelity Retention — Exclude `/compact` Itself)**\n"
        f"   Strictly cap this section at {sec2_budget_str}. Do NOT count\n"
        "   the `/compact` trigger command itself as one of the 3 turns. Preserve the\n"
        "   most recent 3 substantive working turns prior to `/compact` in\n"
        "   chronological order:\n"
        "   - **Turn-by-Turn Dialogue**: The user's exact prompts/selections and the\n"
        "     assistant's final proposals, decisions, or pending question options from\n"
        "     the last 3 substantive turns. If the latest turn ended with an\n"
        "     unanswered or compaction-canceled `ask_question` call, preserve the\n"
        "     exact question text and all options (including `(Recommended)`) verbatim\n"
        "     here and in Section 7 (`Next Steps`) so the assistant can immediately act\n"
        "     on the user's reply.\n"
        "   - **Long / Marathon Turn Folding Rule (>15 steps or 1–2 hour multi-step turns)**:\n"
        "     If any of the last 3 turns is a long multi-step execution turn, do NOT\n"
        "     recount step-by-step logs in Section 2. Anchor only on the turn's\n"
        "     initial user request, intermediate user decision points (`ask_question`),\n"
        "     and final outcome; drop `schedule` timer wakeups, repetitive\n"
        "     `manage_task` status polls, and superseded intermediate status messages.\n"
        "     Move all intermediate code changes, errors/fixes, task/subagent IDs, and\n"
        "     irreversible state transitions from that marathon turn into Sections 3–6\n"
        "     so process folding never loses state.\n"
        "   - **Recent Tool Calls & Outputs (Globally Last 5–8 Active Invocations)**:\n"
        "     Across the 3 turns, include only the last 5–8 still-relevant tool calls\n"
        "     (`read_file`, `edit_file`, `run_command`, `ask_question`, etc.) with\n"
        "     exact file paths, line ranges, key diffs/snippets, and latest test/\n"
        "     command outputs needed for immediate continuation without re-reading.\n\n"
        "3. **Progress & Code Changes**\n"
        "   - Deduplicated, high-signal inventory of completed work with exact\n"
        "     absolute file paths, function/class/symbol names, line ranges, key\n"
        "     logic changes, schema/version numbers, and verified test outcomes.\n"
        "   - Key plans, design docs, or artifacts produced and their current state.\n"
        "   - Skills, governance rules, or reference files read or inspected.\n"
        "   - What is currently in progress or only inspected/planned. Distinguish\n"
        "     completed edits from files that were only read or partially edited, and\n"
        "     never mark unverified test commands as passed.\n\n"
        "4. **Key Findings & Technical Decisions**\n"
        "   - Root causes, reverse-engineered internals, protocol/protobuf field\n"
        "     mappings, memory offsets, configuration keys, and domain constraints.\n"
        "   - Architectural and implementation decisions made and their rationale.\n\n"
        "5. **Errors & Fixes**\n"
        "    Deduplicated record of significant failures and lessons learned:\n"
        "   - Key errors, exceptions, failed commands, or test/build failures\n"
        "     encountered, with exact error messages and root causes.\n"
        "   - How each error was fixed or worked around, and any user feedback.\n"
        "   - Approaches tried and abandoned (and why, to prevent repeating them).\n\n"
        "6. **Active Context**\n"
        "   - State of git repositories (branch, uncommitted changes), running\n"
        "     daemons/servers/ports, background tasks, and subagent conversations.\n"
        "   - Relevant intermediate results, metrics, or working assumptions.\n\n"
        "7. **Next Steps**\n"
        "   Prioritized, actionable checklist of remaining steps, files to create or\n"
        "   modify, verification commands to run, cleanup items, and open questions.\n\n"
        "8. **Commitments & Constraints**\n"
        "   - Standing rules, personas, workflow requirements (e.g., Grill-Me\n"
        "     interactive question rules, authorization gates), and user preferences.\n"
        "   - Explicit promises made to the user and scope boundaries.\n\n"
        "Be thorough and concrete—always include exact file paths, symbol names, line\n"
        "numbers, parameter values, and command strings needed for immediate\n"
        "resumption without duplicate work.\n\n"
        "Wrap your response in <summary></summary> tags.\n"
    )


_RICH_SESSION_SUMMARY_PROMPT = _build_rich_session_summary_prompt(16384)


def _iter_proto_raw_fields(buf: bytes):
    """Yield (number, wire type, original bytes, length-delimited value)."""
    pos = 0
    while pos < len(buf):
        start = pos
        tag, pos = decode_varint(buf, pos)
        number, wire = tag >> 3, tag & 7
        if not 0 < number <= 0x1FFFFFFF:
            raise ValueError("invalid protobuf field")
        value = None
        if wire == 0:
            _, pos = decode_varint(buf, pos)
        elif wire == 2:
            length, pos = decode_varint(buf, pos)
            end = pos + length
            if end > len(buf):
                raise ValueError("truncated protobuf bytes")
            value, pos = buf[pos:end], end
        elif wire in (1, 5):
            pos += 8 if wire == 1 else 4
            if pos > len(buf):
                raise ValueError("truncated protobuf fixed field")
        else:
            raise ValueError("unsupported protobuf wire type")
        yield number, wire, buf[start:pos], value


def _byte_fields(payload: bytes) -> dict[int, bytes]:
    return {k: v for k, t, v in decode_proto(payload) if t == "bytes"}


def _is_compact_control_step(step_payload: bytes) -> bool:
    """Recognize bare /compact control rows so handoff skips them when recovering config."""
    if step_payload.strip() == b"/compact":
        return True
    try:
        compact = False
        for number, kind, value in decode_proto(_byte_fields(step_payload).get(19, b"")):
            if kind != "bytes":
                continue
            if number == 2:
                if value.strip() not in (b"", b"/compact"):
                    return False
                compact |= value.strip() == b"/compact"
            elif number == 3:
                item = _byte_fields(value)
                if item.get(1, b"").strip() not in (b"", b"/compact"):
                    return False
                slash = _byte_fields(item.get(2, b"")).get(27, b"")
                name = _byte_fields(_byte_fields(slash).get(1, b"")).get(1)
                if name is not None and name != b"compact":
                    return False
                compact |= name == b"compact"
        return compact
    except (ValueError, TypeError):
        return False


def select_latest_user_cascade_cfg(cur) -> bytes:
    """Find the newest valid user config, falling back only to a valid control row."""
    fallback = b""
    for (payload,) in cur.execute(
            "SELECT step_payload FROM steps WHERE step_type = 14 ORDER BY idx DESC"):
        payload = payload or b""
        try:
            user = _byte_fields(_byte_fields(payload).get(19, b""))
            cfg = user.get(12) or user.get(13) or b""
            if not cfg or not list(_iter_proto_raw_fields(cfg)):
                continue
        except (ValueError, TypeError):
            continue
        if not _is_compact_control_step(payload):
            return cfg
        if not fallback:
            fallback = cfg
    return fallback


def _extract_planner_model_enum(cascade_cfg: bytes) -> int | None:
    """Extract the requested planner model enum from `CascadeConfig.planner_config` (field 1).

    Honors Protobuf merge semantics across repeated `planner_config` (field 1)
    and repeated nested `requested_model` (fields 15 / 35) messages.
    """
    try:
        direct_f1: int | None = None
        nested_model: int | None = None
        for fnum, wtype, _, val_bytes in _iter_proto_raw_fields(cascade_cfg):
            if fnum == 1 and wtype == 2 and val_bytes:
                for pf_num, pf_wtype, pf_raw, pf_val in _iter_proto_raw_fields(val_bytes):
                    if pf_num == 1 and pf_wtype == 0:
                        _, tag_end = decode_varint(pf_raw, 0)
                        v, _ = decode_varint(pf_raw, tag_end)
                        if v is not None:
                            direct_f1 = int(v)
                    elif pf_num in (15, 35) and pf_wtype == 2 and pf_val:
                        for nf_num, nf_wtype, nf_raw, _ in _iter_proto_raw_fields(pf_val):
                            if nf_num == 1 and nf_wtype == 0:
                                _, tag_end = decode_varint(nf_raw, 0)
                                v, _ = decode_varint(nf_raw, tag_end)
                                if v is not None:
                                    nested_model = int(v)
        return nested_model if nested_model is not None else direct_f1
    except Exception:
        return None


def _inject_rich_checkpoint_config(
    cascade_cfg: bytes, token_threshold: int = 200_000,
    checkpoint_model_enum: int | None = None,
) -> bytes:
    """Enrich `CascadeConfig.checkpoint_config` (field 2) for high-retention `/compact`.

    Targets ~28k–39k total post-compaction context (~16k system base + 10k–16k
    summary + <=7k auxiliary user requests/artifacts/subagents), preserves the
    last 3 turns at high fidelity with marathon-turn folding, and raises
    `CheckpointConfig.token_threshold` (field 1) above `language_server`'s
    built-in 50k default to prevent premature native auto-compaction.
    """
    if not cascade_cfg:
        return cascade_cfg
    raw_th = int(token_threshold or 0)
    eff_threshold = min(max(raw_th, 50_000), 0xFFFFFFFF) if raw_th > 0 else 200_000
    planner_enum = checkpoint_model_enum if checkpoint_model_enum is not None else _extract_planner_model_enum(cascade_cfg)
    max_out_tokens = (
        8192
        if (planner_enum == 342 or (planner_enum is not None and planner_enum >= 100 and planner_enum not in MODEL_MAP))
        else 16384
    )
    prompt_bytes = _build_rich_session_summary_prompt(max_out_tokens).encode("utf-8")
    override_fields: dict[int, bytes] = {
        1: encode_varint((1 << 3) | 0) + encode_varint(eff_threshold),   # token_threshold
        7: (encode_varint(7 << 3) + encode_varint(checkpoint_model_enum)
            if checkpoint_model_enum is not None else b""),           # strip old model in original mode
        11: encode_varint((11 << 3) | 0) + encode_varint(max_out_tokens),  # max_output_tokens
        16: encode_varint((16 << 3) | 0) + encode_varint(20),            # max_user_requests
        17: encode_varint((17 << 3) | 0) + encode_varint(12000),         # max_user_request_bytes (~3k tokens cap)
        18: encode_varint((18 << 3) | 0) + encode_varint(12),            # max_subagent_snapshots
        19: encode_varint((19 << 3) | 0) + encode_varint(1),             # strategy = SAME_MODEL
        20: encode_proto_bytes(20, prompt_bytes),
        23: encode_varint((23 << 3) | 0) + encode_varint(int(checkpoint_model_enum is None)),
        25: encode_varint((25 << 3) | 0) + encode_varint(1),             # include_artifact_snapshots
        26: encode_varint((26 << 3) | 0) + encode_varint(1),             # include_subagent_snapshots
        27: encode_varint((27 << 3) | 0) + encode_varint(1),             # include_running_task_snapshots
        29: encode_varint((29 << 3) | 0) + encode_varint(1),             # include_last_user_message
        32: encode_varint((32 << 3) | 0) + encode_varint(12288),         # max_artifact_section_bytes (~3k tokens cap)
    }
    try:
        kept_top: list[bytes] = []
        existing_ckpt_raw: list[bytes] = []
        for fnum, wtype, raw_bytes, val_bytes in _iter_proto_raw_fields(cascade_cfg):
            if not (fnum == 2 and wtype == 2):
                kept_top.append(raw_bytes)
            elif val_bytes:
                for ck_num, _, ck_raw, _ in _iter_proto_raw_fields(val_bytes):
                    if ck_num not in override_fields:
                        existing_ckpt_raw.append(ck_raw)
        ckpt_payload = b"".join(existing_ckpt_raw) + b"".join(override_fields[k] for k in sorted(override_fields))
        return b"".join(kept_top) + encode_proto_bytes(2, ckpt_payload)
    except Exception:
        return cascade_cfg


class NativeRPCError(RuntimeError):
    """An unknown outcome must be reconciled, never retried as a new mutation."""

    def __init__(self, message, state='rejected'):
        super().__init__(message)
        self.state = state


def _proto_fields(data):
    return {k: v for k, _, v in decode_proto(data)}


def _proto_uint(number, value):
    return encode_varint(number << 3) + encode_varint(value)


def _is_manual_handoff_user_step(step_payload: bytes | None) -> bool:
    """Return True only when the latest USER_INPUT step explicitly invoked /handoff."""
    if not step_payload:
        return False
    try:
        u_blob = _proto_fields(step_payload).get(19, b'')
        if not u_blob:
            return False
        raw_text = ''
        for key, wire_type, val in decode_proto(u_blob):
            if key == 2 and wire_type == 'bytes':
                raw_text = val.decode('utf-8', 'replace')
            elif key == 3 and wire_type == 'bytes':
                scope = _proto_fields(val).get(2, b'')
                slash = _proto_fields(scope).get(27, b'') if scope else b''
                info = _proto_fields(slash).get(1, b'') if slash else b''
                if info and _proto_fields(info).get(1, b'') == b'handoff':
                    return True
        return bool(re.match(r'^\s*/handoff(?:\s|$)', raw_text))
    except (ValueError, TypeError):
        return False


def extract_handoff_metadata(db_path: str | Path, *, require_manual_slash: bool = True) -> dict:
    """Recover creation context from a single readonly SQLite snapshot.

    trajectory_metadata_blob.data is CortexTrajectoryMetadata, not a Step.
    StartCascade.project_env_config is constructed separately from fields 18/19;
    copying the metadata bytes into field 17 would be an incompatible message.
    """
    with contextlib.closing(connect_readonly(db_path)) as con:
        con.execute('BEGIN')
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        metadata = {}
        metadata_blob = b''
        for table in ('trajectory_metadata_blob', 'trajectory_metadata'):
            if table not in tables:
                continue
            columns = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
            if 'data' in columns:
                row = con.execute(f'SELECT data FROM {table} LIMIT 1').fetchone()
                if row and row[0]:
                    metadata_blob = row[0]
                    metadata = _proto_fields(metadata_blob)
                    break
        last = con.execute('SELECT idx FROM steps ORDER BY idx DESC LIMIT 1').fetchone()
        last_user_row = con.execute(
            'SELECT idx, step_payload FROM steps WHERE step_type=14 ORDER BY idx DESC LIMIT 1'
        ).fetchone()
        last_user = last_user_row[0] if last_user_row else None
        if require_manual_slash and (not last_user_row or not _is_manual_handoff_user_step(last_user_row[1])):
            raise ValueError('仅允许用户在本轮手动输入 /handoff 触发会话交接，禁止 Agent 自行调用')
        cfg = select_latest_user_cascade_cfg(con.cursor())
        if not cfg:
            raise ValueError('会话缺少可恢复的 cascade_config')
        requested = _extract_planner_model_enum(cfg) or 0
        if not isinstance(requested, int):
            requested = 0
        # Decode the original message to preserve repeated workspace fields.
        uris = [v.decode('utf-8') for k, t, v in decode_proto(metadata_blob)
                if k == 7 and t == 'bytes']
        if not uris:
            uris = [_proto_fields(v).get(1, b'').decode('utf-8')
                    for k, t, v in decode_proto(metadata_blob)
                    if k == 1 and t == 'bytes']
        if not uris:
            uris = [v.decode('utf-8') for k, t, v in decode_proto(cfg) if k == 12 and t == 'bytes']
        uris = list(dict.fromkeys(u for u in uris if u))
        if any('\x00' in u or len(u) > 8192 or not urllib.parse.urlparse(u).scheme for u in uris):
            raise ValueError('会话工作区 URI 无效')
        project = metadata.get(18, b'').decode('utf-8')
        environment = metadata.get(19, b'').decode('utf-8')
        if not uris and not project:
            raise ValueError('无法恢复会话工作区或项目，已停止创建')
        if environment and not project:
            raise ValueError('会话环境缺少项目归属')
        project_cfg = b''
        if project:
            project_cfg = encode_proto_bytes(1, project.encode('utf-8'))
            project_cfg += (encode_proto_bytes(2, environment.encode('utf-8')) if environment
                            else encode_proto_bytes(4, b''))
        return {'workspace_uris': uris, 'project_env_config': project_cfg,
                'requested_model': requested, 'cascade_cfg': cfg, 'backend': 1,
                'source': 1, 'trajectory_type': 4,
                'snapshot_step_idx': last[0] if last else -1,
                'last_user_step_idx': last_user if last_user is not None else -1}


def _native_rpc(method: str, payload: bytes, endpoint: tuple[int, str], timeout=8.0) -> bytes:
    """One Connect unary attempt on the endpoint selected by a readonly query."""
    port, csrf = endpoint
    request = urllib.request.Request(
        f'http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}',
        data=payload, method='POST',
        headers={'Content-Type': 'application/proto', 'Connect-Protocol-Version': '1',
                 'x-codeium-csrf-token': csrf})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise NativeRPCError(f'{method}: HTTP {response.status}', 'unknown')
            body = response.read(32 * 1024 * 1024 + 1)
            if len(body) > 32 * 1024 * 1024:
                raise NativeRPCError(f'{method}: 响应过大', 'unknown')
            return body
    except urllib.error.HTTPError as exc:
        state = 'rejected' if exc.code in (400, 401, 403, 404, 405) else 'unknown'
        detail = ''
        with contextlib.suppress(Exception):
            raw = exc.read(512).decode('utf-8', errors='replace').strip()
            if raw:
                detail = f' ({raw[:240]})'
        raise NativeRPCError(f'{method}: HTTP {exc.code}{detail}', state) from None
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise NativeRPCError(f'{method}: 连接失败或响应不确定 ({type(exc).__name__})', 'unknown') from None


def get_native_cascade_summaries(endpoint: tuple[int, str], timeout: float = 8.0) -> dict[str, dict]:
    data = _native_rpc('GetAllCascadeTrajectories', b'', endpoint, timeout=timeout)
    summaries = {}
    for number, kind, entry in decode_proto(data):
        if number != 1 or kind != 'bytes':
            continue
        pair = _proto_fields(entry)
        cid = pair.get(1, b'').decode('utf-8')
        summary = _proto_fields(pair.get(2, b''))
        summaries[cid] = {
            'status': summary.get(5, 0), 'has_active_children': bool(summary.get(18, 0)),
            'not_fully_idle': bool(summary.get(21, 0)),
            'background_task_count': summary.get(29, 0),
            'has_running_subagents': bool(summary.get(30, 0)),
        }
    return summaries


def is_runtime_settled(summary: dict) -> bool:
    """Shared, readonly definition of a fully idle native cascade."""
    return summary.get('status') == 1 and not any(
        summary.get(k) for k in ('has_active_children', 'not_fully_idle',
                                 'background_task_count', 'has_running_subagents'))


def find_native_cascade_endpoint(conv_id: str, *, timeout: float | None = None) -> tuple[int, str]:
    """Do not broadcast writes across unrelated LS instances or auxiliary ports."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    candidates = ([_CACHED_LS_ENDPOINT] if _CACHED_LS_ENDPOINT else []) + (
        discover_ls_endpoints(timeout=min(6.0, timeout)) if timeout is not None else discover_ls_endpoints())
    for endpoint in dict.fromkeys(candidates):
        remaining = deadline - time.monotonic() if deadline is not None else 8.0
        if remaining <= 0:
            break
        try:
            if conv_id in get_native_cascade_summaries(endpoint, timeout=min(8.0, remaining)):
                return endpoint
        except (NativeRPCError, ValueError):
            continue
    raise NativeRPCError('未找到拥有此 App 会话的 LanguageServer')


def _build_start_cascade_payload(new_id: str, context: dict, *, use_project_env: bool) -> bytes:
    payload = (encode_proto_bytes(7, new_id.encode('utf-8'))
               + _proto_uint(4, context.get('source', 1))
               + _proto_uint(5, context.get('trajectory_type', 4))
               + _proto_uint(24, context.get('backend', 1)))
    if use_project_env and context.get('project_env_config'):
        payload += encode_proto_bytes(17, context['project_env_config'])
    else:
        for uri in context.get('workspace_uris', []):
            payload += encode_proto_bytes(8, uri.encode('utf-8'))
    if context.get('requested_model'):
        payload += _proto_uint(14, context['requested_model'])
    return payload


def start_native_cascade(new_id: str, context: dict, *, endpoint: tuple[int, str]) -> str:
    # No parent_conversation_id/base_trajectory_identifier: this is a fresh mainline.
    has_proj = bool(context.get('project_env_config'))
    payload = _build_start_cascade_payload(new_id, context, use_project_env=has_proj)
    try:
        response = _native_rpc('StartCascade', payload, endpoint, timeout=15.0)
    except NativeRPCError as exc:
        if has_proj and exc.state == 'rejected' and context.get('workspace_uris'):
            payload = _build_start_cascade_payload(new_id, context, use_project_env=False)
            response = _native_rpc('StartCascade', payload, endpoint, timeout=15.0)
        else:
            raise
    try:
        returned = _proto_fields(response).get(1, b'').decode('utf-8')
    except (ValueError, AttributeError):
        raise NativeRPCError('StartCascade 响应无法解析，创建结果不确定', 'unknown') from None
    if returned != new_id:
        raise NativeRPCError('StartCascade 返回的会话 ID 不匹配', 'unknown')
    return returned


def send_native_cascade_message(
    conv_id: str,
    text: str,
    cascade_cfg: bytes,
    *,
    endpoint: tuple[int, str],
    token_threshold: int = 200_000,
):
    enriched_cfg = _inject_rich_checkpoint_config(cascade_cfg, token_threshold=token_threshold) if cascade_cfg else cascade_cfg
    payload = (encode_proto_bytes(1, conv_id.encode('utf-8'))
               + encode_proto_bytes(2, encode_proto_bytes(1, text.encode('utf-8')))
               + encode_proto_bytes(5, enriched_cfg))
    _native_rpc('SendUserCascadeMessage', payload, endpoint)


def update_conversation_annotations(conv_id: str, *, title=None, archived=None,
                                    endpoint: tuple[int, str]):
    annotation = b''
    if title is not None:
        annotation += encode_proto_bytes(1, title.encode('utf-8'))
    if archived is not None:
        annotation += _proto_uint(4, int(archived))
    if not annotation:
        raise ValueError('缺少 annotation 更新字段')
    payload = (encode_proto_bytes(2, annotation) + _proto_uint(3, 1)
               + encode_proto_bytes(4, conv_id.encode('utf-8')))
    _native_rpc('UpdateConversationAnnotations', payload, endpoint)


def _daemon_step(payload: bytes) -> bool:
    """Only a structured run_command argument can exempt a running step."""
    try:
        # Step.metadata(5).tool_call(4): function name(2), JSON arguments(3).
        # Never inspect output/result text for a daemon exemption.
        metadata = _proto_fields(_proto_fields(payload).get(5, b''))
        call = _proto_fields(metadata.get(4, b''))
        if call.get(2) != b'run_command':
            return False
        args = json.loads(call.get(3, b'{}'))
        return isinstance(args, dict) and args.get('IsDaemon') is True and 'CommandLine' in args
    except (ValueError, TypeError, UnicodeError):
        return False


def inspect_cascade_idle(db_path: str | Path, *, snapshot_step_idx=None,
                         max_step_idx=None, runtime_summary=None, subagents=()) -> dict:
    """Fail closed on unknown runtime state, new user input or active children.

    snapshot_step_idx allows this handoff turn's final reply to finish. A hard
    max_step_idx is available for callers that require an exact upper bound.
    """
    with contextlib.closing(connect_readonly(db_path)) as con:
        con.execute('BEGIN')
        last = con.execute('SELECT idx,step_type,status FROM steps ORDER BY idx DESC LIMIT 1').fetchone()
        user = con.execute('SELECT max(idx) FROM steps WHERE step_type=14').fetchone()[0]
        ckpt = con.execute('SELECT max(idx) FROM steps WHERE step_type=23 AND status IN (3,5)').fetchone()[0]
        active = con.execute('SELECT idx,step_type,status,metadata,step_payload FROM steps WHERE status IN (1,2,8,9)').fetchall()
        min_bg_idx = max(user or 0, ckpt or 0)
        turn_tools = con.execute(
            'SELECT idx,step_type,status,step_payload FROM steps WHERE idx > ? AND step_type=132 ORDER BY idx',
            (min_bg_idx,),
        ).fetchall()
    last_idx = int(last[0]) if last else -1
    now_ts = time.time()
    has_blocking_active = False
    for a_idx, kind, a_status, meta, payload in active:
        ap = payload or b''
        am = meta or b''
        if kind == 132 and _daemon_step(ap):
            continue
        if last_idx >= 0 and int(a_idx) < last_idx:
            if int(kind) != 132 or int(a_status) != 2 or int(a_idx) < min_bg_idx:
                continue
            if _schedule_step_expired_or_ask(am, ap, False, now_ts):
                continue
            if _task_cleared_by_later_steps(int(a_idx), turn_tools):
                continue
        has_blocking_active = True
    result = {'safe': False, 'last_step_idx': last[0] if last else -1,
              'last_user_step_idx': user if user is not None else -1}
    reason = ''
    if snapshot_step_idx is not None and user is not None and user > snapshot_step_idx:
        reason = 'new_user_input'
    elif max_step_idx is not None and last and last[0] > max_step_idx:
        reason = 'step_limit_exceeded'
    elif not last or last[1] not in (15, 17) or last[2] != 3:
        reason = 'busy'
    elif has_blocking_active:
        reason = 'active_steps'
    elif runtime_summary is None or runtime_summary.get('status') != 1:
        reason = 'runtime_unverified'
    elif any(runtime_summary.get(k) for k in (
            'has_active_children', 'not_fully_idle', 'background_task_count', 'has_running_subagents')):
        reason = 'active_children'
    elif any(item.get('status') == 'running' for item in subagents):
        reason = 'active_subagents'
    result.update(safe=not reason, reason=reason)
    return result


def _find_cli_bin(name: str) -> str | None:
    """Return executable path if CLI `name` is installed on the host, else None."""
    found = shutil.which(name)
    if found:
        return found
    for d in (
        HOME / ".local" / "bin",
        HOME / ".grok" / "bin",
        HOME / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ):
        cand = d / name
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except Exception:
            continue
    return None


def _store_codex_quota(sig: tuple[str, float], payload: dict[str, dict]) -> dict[str, dict]:
    global _CODEX_QUOTA_CACHE, _LAST_CODEX_SCAN_TS
    copied = dict(payload)
    _CODEX_QUOTA_CACHE = (sig, dict(copied))
    _LAST_CODEX_SCAN_TS = time.time()
    return copied


def _fetch_codex_quota_rpc(codex_bin: str, timeout: float = 8.0) -> dict[str, dict]:
    """Query live Codex rate limits via official `codex app-server --stdio` JSON-RPC."""
    proc: subprocess.Popen | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="agy-cockpit-codex-quota-") as td:
            proc = _start_probe(
                [codex_bin, "app-server", "--stdio"],
                cwd=td,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            assert proc.stdin is not None and proc.stdout is not None
            fd = proc.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            buf = b""
            deadline = time.monotonic() + max(2.0, float(timeout))

            def _send(obj: dict) -> None:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()

            def _wait_id(target_id: int) -> dict | None:
                nonlocal buf
                while time.monotonic() < deadline and len(buf) < _MAX_PROBE_OUTPUT:
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(msg, dict) and msg.get("id") == target_id:
                            return msg
                    rem = max(0.05, min(0.25, deadline - time.monotonic()))
                    r, _, _ = select.select([fd], [], [], rem)
                    if not r:
                        if proc.poll() is not None:
                            break
                        continue
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        break
                    buf = (buf + chunk)[:_MAX_PROBE_OUTPUT]
                return None

            _send({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "agy-cockpit", "version": "1.0"}, "capabilities": {"experimentalApi": True}},
            })
            if not _wait_id(1):
                return {}
            _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            _send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
            rep = _wait_id(2)
            res = rep.get("result") if isinstance(rep, dict) and isinstance(rep.get("result"), dict) else None
            if not res:
                return {}
            return _extract_codex_rpc_windows(res, time.time())
    except Exception:
        return {}
    finally:
        if proc is not None:
            _finish_probe(proc)


@serialized(lambda *args, **kwargs: "codex-quota")
def fetch_codex_quota(force_refresh: bool = False) -> dict[str, dict]:
    """Query live Codex rate limits via `codex app-server` RPC on refresh, falling back to local sessions."""
    global _CODEX_QUOTA_CACHE, _LAST_CODEX_SCAN_TS
    now = time.time()
    if not force_refresh and _CODEX_QUOTA_CACHE is not None and (now - _LAST_CODEX_SCAN_TS) < 3.0:
        return dict(_CODEX_QUOTA_CACHE[1])
    codex_bin = _find_cli_bin("codex")
    if codex_bin is None:
        return _store_codex_quota(("", 0.0), {})
    out: dict[str, dict] = {"codex-installed": {"installed": True}}
    if force_refresh:
        live = _fetch_codex_quota_rpc(codex_bin, timeout=8.0)
        if live:
            out.update(live)
            return _store_codex_quota(("rpc", time.time()), out)
    if not CODEX_SESSIONS_DIR.exists():
        return _store_codex_quota(("", 0.0), out)
    try:
        rollouts = sorted(cached_paths(CODEX_SESSIONS_DIR, "*.jsonl", recursive=True), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return _store_codex_quota(("", 0.0), out)
    if not rollouts:
        return _store_codex_quota(("", 0.0), out)
    newest = rollouts[0]
    try:
        newest_mt = newest.stat().st_mtime
    except Exception:
        return _store_codex_quota(("", 0.0), out)
    sig = tuple((str(path), path.stat().st_mtime_ns) for path in rollouts[:8])
    if not force_refresh and _CODEX_QUOTA_CACHE is not None and _CODEX_QUOTA_CACHE[0] == sig:
        _LAST_CODEX_SCAN_TS = time.time()
        return dict(_CODEX_QUOTA_CACHE[1])

    candidates = []
    for rollout in rollouts[:8]:
        try:
            for line in reversed(list(log_lines(rollout))):
                if 'rate_limits' not in line:
                    continue
                try:
                    obj = json.loads(line)
                    limits = (obj.get('payload') or {}).get('rate_limits')
                    if isinstance(limits, dict):
                        candidates.append((parse_iso_ts(obj.get('timestamp')) or 0.0, limits))
                        break
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError:
            continue
    if candidates:
        observed, limits = max(candidates, key=lambda item: item[0])
        out.update(codex_rollout_windows(limits, observed))

    return _store_codex_quota(sig, out)


def _parse_droid_reset_sec(tail: str) -> int:
    """Parse `↻ 1 day`, `↻ 3h 20min`, or `↻ 45min` from Droid `/limits` output into seconds."""
    return parse_droid_reset_sec(tail)



@serialized(lambda *args, **kwargs: "droid-quota")
def fetch_droid_quota(seed_cache: dict[str, dict] | None = None, force_refresh: bool = False) -> dict[str, dict]:
    """Fetch Droid (Factory) Standard Usage quota via `/limits` PTY probe if `droid` is installed."""
    global _DROID_QUOTA_CACHE, _LAST_DROID_FETCH_TS, _LAST_DROID_SESSIONS_MTIME, _LAST_DROID_SUCCESS_MTIME
    droid_bin = _find_cli_bin("droid")
    if droid_bin is None:
        return {}

    if not _DROID_QUOTA_CACHE and seed_cache:
        for k in ("droid-5h", "droid-weekly", "droid-monthly"):
            if isinstance(seed_cache.get(k), dict) and seed_cache[k].get("rem_frac") is not None:
                _DROID_QUOTA_CACHE[k] = dict(seed_cache[k])

    probe_cwd_path = Path("/tmp/agy-cockpit-droid-probe")
    probe_cwd_path.mkdir(parents=True, exist_ok=True)
    probe_cwd = str(probe_cwd_path)
    probe_slug_dir = FACTORY_SESSIONS_DIR / "-tmp-agy-cockpit-droid-probe"

    max_s_mt = 0.0
    if FACTORY_SESSIONS_DIR.exists():
        try:
            for p in cached_paths(FACTORY_SESSIONS_DIR, "*.settings.json", recursive=True):
                try:
                    jl = p.with_name(p.name[: -len(".settings.json")] + ".jsonl")
                    if jl.exists() and jl.stat().st_size < 450:
                        continue
                    max_s_mt = max(max_s_mt, p.stat().st_mtime)
                except Exception:
                    continue
        except Exception:
            pass

    now = time.time()
    _LAST_DROID_SESSIONS_MTIME = max(_LAST_DROID_SESSIONS_MTIME, max_s_mt)
    if not force_refresh:
        return {"droid-installed": {"installed": True}, **_DROID_QUOTA_CACHE}

    _LAST_DROID_FETCH_TS = now
    _LAST_DROID_SESSIONS_MTIME = max_s_mt
    pre_probe_files = set(probe_slug_dir.iterdir()) if probe_slug_dir.exists() else set()
    master_fd = -1
    slave_fd = -1
    proc = None
    buf = b""
    try:
        master_fd, slave_fd = pty.openpty()
        with contextlib.suppress(Exception):
            os.set_inheritable(master_fd, False)
            os.set_inheritable(slave_fd, False)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 120, 0, 0))
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        proc = _start_probe(
            [droid_bin], probe_fds=(master_fd,),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=probe_cwd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        sent = 0
        trusted = False
        enter_offset = 0
        deadline = time.monotonic() + 8.0
        t_sent = time.monotonic()
        enter_offset = 0
        while time.monotonic() < deadline and len(buf) < _MAX_PROBE_OUTPUT:
            r, _, _ = select.select([master_fd], [], [], 0.15)
            if r:
                try:
                    chunk = os.read(master_fd, 16384)
                    if not chunk:
                        break
                    buf = (buf + chunk)[:_MAX_PROBE_OUTPUT]
                except OSError:
                    break
            if not trusted and b"agy-cockpit-droid-probe" in buf and (b"\xe4\xbf\xa1\xe4\xbb\xbb\xe6\xad\xa4\xe6\x96\x87\xe4\xbb\xb6\xe5\xa4\xb9" in buf or b"Trust this" in buf):
                time.sleep(0.2)
                os.write(master_fd, b"\r")
                trusted = True
                buf = b""
                continue
            if sent == 0 and (b"Model" in buf or b"\xe8\xaf\x95\xe9\x97\xae" in buf):
                time.sleep(0.35)
                os.write(master_fd, b"/limits")
                sent = 1
                t_sent = time.monotonic()
            elif sent == 1 and time.monotonic() - t_sent > 0.25:
                enter_offset = len(buf)
                os.write(master_fd, b"\r")
                sent = 2
                t_sent = time.monotonic()
            elif sent == 2:
                post_buf = buf[enter_offset:]
                if (b"\xe6\xaf\x8f\xe6\x9c\x88" in post_buf or b"Monthly" in post_buf) and time.monotonic() - t_sent > 0.35:
                    time.sleep(0.15)
                    while time.monotonic() < deadline and len(buf) < _MAX_PROBE_OUTPUT:
                        r2, _, _ = select.select([master_fd], [], [], 0.05)
                        if not r2:
                            break
                        try:
                            c2 = os.read(master_fd, 16384)
                            if not c2:
                                break
                            buf = (buf + c2)[:_MAX_PROBE_OUTPUT]
                        except OSError:
                            break
                    break
                if time.monotonic() - t_sent > 3.5:
                    break
    except Exception:
        pass
    finally:
        if slave_fd >= 0:
            with contextlib.suppress(Exception):
                os.close(slave_fd)
        if proc is not None:
            _finish_probe(proc)
            master_fd = -1
        if master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        if probe_slug_dir.exists():
            with contextlib.suppress(Exception):
                for f in list(probe_slug_dir.iterdir()):
                    if f not in pre_probe_files:
                        with contextlib.suppress(Exception):
                            f.unlink()
                for p in list(probe_slug_dir.glob("*.settings.json")):
                    jl = p.with_name(p.name[: -len(".settings.json")] + ".jsonl")
                    if jl.exists() and jl.stat().st_size < 450:
                        with contextlib.suppress(Exception):
                            p.unlink()
                        with contextlib.suppress(Exception):
                            jl.unlink()

    previous = dict(_DROID_QUOTA_CACHE)
    parsed_any = False
    if buf:
        after = buf[enter_offset:] if enter_offset > 0 else buf
        clean = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", b" ", after)
        txt = clean.decode("utf-8", errors="ignore")
        now_done = time.time()
        m_5h = re.search(r"(?:5\s*小时|5-hour|Five[\s-]*hour)\s+(\d+(?:\.\d+)?)%([^\r\n]*)", txt, re.I)
        if m_5h:
            parsed_any = True
            u5 = float(m_5h.group(1))
            rsec5 = _parse_droid_reset_sec(m_5h.group(2))
            _DROID_QUOTA_CACHE["droid-5h"] = {
                "reset_precision_sec": droid_reset_precision(m_5h.group(2)),
                "rem_frac": max(0.0, min(1.0, (100.0 - u5) / 100.0)),
                "reset_ts": (now_done + rsec5) if rsec5 > 0 else 0.0,
            }
        m_wk = re.search(r"(?:每周|Weekly)\s+(\d+(?:\.\d+)?)%([^\r\n]*)", txt, re.I)
        if m_wk:
            parsed_any = True
            uwk = float(m_wk.group(1))
            rsec_wk = _parse_droid_reset_sec(m_wk.group(2))
            _DROID_QUOTA_CACHE["droid-weekly"] = {
                "reset_precision_sec": droid_reset_precision(m_wk.group(2)),
                "rem_frac": max(0.0, min(1.0, (100.0 - uwk) / 100.0)),
                "reset_ts": (now_done + rsec_wk) if rsec_wk > 0 else 0.0,
            }
        m_mo = re.search(r"(?:每月|Monthly)\s+(\d+(?:\.\d+)?)%([^\r\n]*)", txt, re.I)
        if m_mo:
            parsed_any = True
            umo = float(m_mo.group(1))
            rsec_mo = _parse_droid_reset_sec(m_mo.group(2))
            _DROID_QUOTA_CACHE["droid-monthly"] = {
                "reset_precision_sec": droid_reset_precision(m_mo.group(2)),
                "rem_frac": max(0.0, min(1.0, (100.0 - umo) / 100.0)),
                "reset_ts": (now_done + rsec_mo) if rsec_mo > 0 else 0.0,
            }

    if parsed_any:
        _LAST_DROID_SUCCESS_MTIME = max_s_mt
        _DROID_QUOTA_CACHE = anchor_droid_quota(_DROID_QUOTA_CACHE, previous, now_done)
    return {"droid-installed": {"installed": True}, **_DROID_QUOTA_CACHE}


def _sync_ls_model_configs(port: int, csrf: str, force_refresh: bool = False) -> bool:
    """Sync live `ClientModelConfig` enum mappings from LanguageServer so newly launched models never render as `#14xx`."""
    global _LAST_LS_MODEL_SYNC
    now = time.monotonic()
    if (
        not force_refresh
        and _LAST_LS_MODEL_SYNC is not None
        and _LAST_LS_MODEL_SYNC[0] == (port, csrf)
        and (now - _LAST_LS_MODEL_SYNC[1]) < 300.0
    ):
        return False
    try:
        raw = _native_rpc("GetCascadeModelConfigData", b"", (port, csrf), timeout=2.0)
        entries: list[tuple[int, str, str]] = []
        for fnum, wtype, val in decode_proto(raw):
            if fnum == 1 and wtype == "bytes" and isinstance(val, bytes):
                label = ""
                enum_id = 0
                model_id = ""
                for cf_num, cf_t, cf_v in decode_proto(val):
                    if cf_num == 1 and cf_t == "bytes" and isinstance(cf_v, bytes):
                        label = cf_v.decode("utf-8", errors="ignore").strip()
                    elif cf_num == 2 and cf_t == "bytes" and isinstance(cf_v, bytes):
                        for mf_num, mf_t, mf_v in decode_proto(cf_v):
                            if mf_num == 1 and mf_t == "varint" and isinstance(mf_v, int):
                                enum_id = mf_v
                    elif cf_num == 21 and cf_t == "bytes" and isinstance(cf_v, bytes):
                        model_id = cf_v.decode("utf-8", errors="ignore").strip()
                if enum_id > 0 and label:
                    entries.append((enum_id, label, model_id))
        if entries:
            _LAST_LS_MODEL_SYNC = ((port, csrf), now)
            return register_dynamic_models(entries)
    except Exception:
        pass
    return False


def sync_ls_model_configs(force_refresh: bool = False) -> bool:
    """Discover local LanguageServer and sync dynamic `ClientModelConfig` labels (no upstream network calls)."""
    global _CACHED_LS_ENDPOINT
    if not force_refresh and _LAST_LS_MODEL_SYNC is not None and (time.monotonic() - _LAST_LS_MODEL_SYNC[1]) < 300.0:
        return False
    candidates: list[tuple[int, str]] = []
    if _CACHED_LS_ENDPOINT is not None:
        candidates.append(_CACHED_LS_ENDPOINT)
    for ep in discover_ls_endpoints(timeout=2.5):
        if ep not in candidates:
            candidates.append(ep)
    for port, csrf in candidates:
        before = _LAST_LS_MODEL_SYNC
        changed = _sync_ls_model_configs(port, csrf, force_refresh=force_refresh)
        if _LAST_LS_MODEL_SYNC != before or (_LAST_LS_MODEL_SYNC is not None and _LAST_LS_MODEL_SYNC[0] == (port, csrf)):
            _CACHED_LS_ENDPOINT = (port, csrf)
            return changed
    return False


def fetch_quota_buckets_with_meta(
    force_refresh: bool = False,
    seed_cache: dict[str, dict] | None = None,
    *,
    agy_force_refresh: bool | None = None,
    ui_active: bool = False,
) -> tuple[dict[str, dict], dict]:
    """Fetch user quota summary via official CLIs in parallel on demand."""
    eff_agy_force = force_refresh if agy_force_refresh is None else agy_force_refresh
    parsed_buckets: dict[str, dict] = {}
    agy_meta: dict = {"reason": "cached", "probed": False, "ok": True, "error": ""}

    def _run_agy() -> tuple[dict[str, dict], dict]:
        return fetch_agy_quota(
            cli_bin=_find_cli_bin("agy"),
            run_cmd=lambda cmd, timeout: _probe_output(cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL),
            seed_cache=seed_cache,
            force_refresh=eff_agy_force,
            ui_active=ui_active,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="quota-probe") as pool:
        f_agy = pool.submit(_run_agy)
        f_codex = pool.submit(fetch_codex_quota, force_refresh)
        f_droid = pool.submit(fetch_droid_quota, seed_cache, force_refresh)
        f_grok = pool.submit(fetch_grok_quota, seed_cache, force_refresh)

        try:
            agy_buckets, agy_meta = f_agy.result()
            parsed_buckets.update(agy_buckets)
        except Exception as exc:
            agy_meta = {"reason": "error", "probed": bool(eff_agy_force), "ok": False, "error": str(exc) or type(exc).__name__}

        for fut in (f_codex, f_droid, f_grok):
            try:
                res = fut.result()
                if isinstance(res, dict):
                    parsed_buckets.update(res)
            except Exception:
                continue

    return parsed_buckets, agy_meta


def fetch_quota_buckets(
    force_refresh: bool = False,
    seed_cache: dict[str, dict] | None = None,
    *,
    agy_force_refresh: bool | None = None,
    ui_active: bool = False,
) -> dict[str, dict]:
    """Fetch user quota summary via official CLIs (AGY `/usage`, Codex sessions, Droid `/limits`, Grok stdio)."""
    parsed_buckets, _ = fetch_quota_buckets_with_meta(
        force_refresh=force_refresh,
        seed_cache=seed_cache,
        agy_force_refresh=agy_force_refresh,
        ui_active=ui_active,
    )
    return parsed_buckets


_GROK_IDLE_FORCE_PROBE_SEC = 900.0  # 15m idle sync cadence (within server 1800s stale window)


@serialized(lambda *args, **kwargs: "grok-quota")
def fetch_grok_quota(seed_cache: dict[str, dict] | None = None, force_refresh: bool = False) -> dict[str, dict]:
    """Fetch Grok (SuperGrok) weekly quota via `grok agent stdio` (`_x.ai/billing`) if `grok` is installed."""
    global _GROK_QUOTA_CACHE, _LAST_GROK_FETCH_TS, _LAST_GROK_SESSIONS_MTIME, _LAST_GROK_SUCCESS_MTIME
    grok_bin = _find_cli_bin("grok")
    if grok_bin is None:
        return {}

    if not _GROK_QUOTA_CACHE and seed_cache:
        for k in ("grok-weekly",):
            if isinstance(seed_cache.get(k), dict) and seed_cache[k].get("rem_frac") is not None:
                _GROK_QUOTA_CACHE[k] = dict(seed_cache[k])

    max_s_mt = 0.0
    if GROK_SESSIONS_DIR.exists():
        try:
            for p in cached_paths(GROK_SESSIONS_DIR, "*/*/updates.jsonl"):
                try:
                    max_s_mt = max(max_s_mt, p.stat().st_mtime)
                except Exception:
                    continue
        except Exception:
            pass

    now = time.time()
    _LAST_GROK_SESSIONS_MTIME = max(_LAST_GROK_SESSIONS_MTIME, max_s_mt)
    if not force_refresh:
        return {"grok-installed": {"installed": True}, **_GROK_QUOTA_CACHE}

    _LAST_GROK_FETCH_TS = now
    _LAST_GROK_SESSIONS_MTIME = max(_LAST_GROK_SESSIONS_MTIME, max_s_mt)

    proc: subprocess.Popen | None = None
    parsed_ok = False
    try:
        proc = _start_probe(
            [grok_bin, "agent", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "agy-cockpit", "version": "1.0"},
            },
        }
        bill_req = {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}}
        proc.stdin.write(json.dumps(init_req) + "\n" + json.dumps(bill_req) + "\n")
        proc.stdin.flush()

        fd = proc.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        buf = b""
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and len(buf) < _MAX_PROBE_OUTPUT:
            r, _, _ = select.select([fd], [], [], 0.25)
            if not r:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                break
            buf = (buf + chunk)[:_MAX_PROBE_OUTPUT]
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("id") == 2 and isinstance(obj.get("result"), dict):
                    cfg = obj["result"].get("config") or {}
                    period = cfg.get("currentPeriod") or {}
                    end_str = str(period.get("end") or cfg.get("billingPeriodEnd") or "")
                    reset_ts = parse_iso_ts(end_str) or 0.0
                    raw_used = cfg.get("creditUsagePercent")
                    rem_frac = grok_remaining(raw_used)
                    if rem_frac is not None:
                        parsed_ok = True
                        _GROK_QUOTA_CACHE["grok-weekly"] = {
                            "rem_frac": rem_frac,
                            "reset_ts": reset_ts,
                            "observed_at": time.time(),
                            "period_start_ts": parse_iso_ts(period.get("start")) or 0.0,
                            "period_type": period.get("type"),
                        }
                    deadline = 0.0
                    break
    except Exception:
        pass
    finally:
        if proc is not None:
            _finish_probe(proc)

    if parsed_ok:
        _LAST_GROK_SUCCESS_MTIME = max_s_mt
    return {"grok-installed": {"installed": True}, **_GROK_QUOTA_CACHE}


def _norm_hex_color(val: object, default: str) -> str:
    if not isinstance(val, str):
        return default
    s = val.strip()
    if not s:
        return default
    if not s.startswith("#"):
        s = "#" + s
    if len(s) == 4:
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return s if re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", s) else default


@serialized(lambda: "live-theme")
def get_live_theme() -> dict:
    """Read Antigravity userSettings theme seeds from ~/.gemini/config/config.json with mtime_ns caching."""
    global _THEME_CFG_MTIME_NS, _THEME_CFG_CACHE
    try:
        st = _AGY_CONFIG_FILE.stat()
        if st.st_mtime_ns == _THEME_CFG_MTIME_NS:
            return _THEME_CFG_CACHE
        raw = json.loads(_AGY_CONFIG_FILE.read_text(encoding="utf-8"))
        us = (raw.get("userSettings") if isinstance(raw, dict) else None) or {}
        raw_mode = us.get("themeMode")
        mode = "dark"
        if raw_mode == 2 or (isinstance(raw_mode, str) and "LIGHT" in raw_mode.upper()):
            mode = "light"
        elif raw_mode == 3 or (
            isinstance(raw_mode, str) and any(k in raw_mode.upper() for k in ("INHERIT", "SYSTEM", "AUTO"))
        ):
            mode = "system"

        d_seeds = us.get("customThemeSeedsDark") if isinstance(us.get("customThemeSeedsDark"), dict) else {}
        l_seeds = us.get("customThemeSeedsLight") if isinstance(us.get("customThemeSeedsLight"), dict) else {}

        _THEME_CFG_CACHE = {
            "mode": mode,
            "dark": {
                "background": _norm_hex_color(d_seeds.get("background"), "#101010"),
                "foreground": _norm_hex_color(d_seeds.get("foregroundOverride"), "#cccccc"),
                "primary": _norm_hex_color(d_seeds.get("primary"), "#007acc"),
            },
            "light": {
                "background": _norm_hex_color(l_seeds.get("background"), "#F9F9F9"),
                "foreground": _norm_hex_color(l_seeds.get("foregroundOverride"), "#101010"),
                "primary": _norm_hex_color(l_seeds.get("primary"), "#007acc"),
            },
        }
        _THEME_CFG_MTIME_NS = st.st_mtime_ns
    except Exception:
        pass
    return _THEME_CFG_CACHE
