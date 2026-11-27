"""Subagent discovery (step_type=132) and metrics analyzers for AGY, Codex, Pi, and Droid."""

from __future__ import annotations

import contextlib
import json
import logging
import zlib
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Callable

from analyzer import (
    _ANALYSIS_CACHE,
    BoundedCache, CostEvent, cached_paths, connect_readonly, log_lines, serialized,
    resolve_builtin_enum_from_parent,
    _BUILTIN_ENUM_LOCK,
    _BUILTIN_SUBAGENT_MODEL_ENUM,
    APP_DIRS,
    HOME,
    RE_INVOKE_ID,
    RE_PROTO_SUB_UUID,
    UUID_PAT,
    analyze_conversation,
    decode_proto,
    get_db_mtime,
    parse_proto_duration,
)
from droid_acp_fallback import discovery_hints
from metric_logs import parse_iso_ts
from swarm_subagents import discover_swarm_stubs, finish_swarm_item, is_swarm_worker, merge_swarm_stub, native_stub_status, native_worker_status, swarm_dir_signature
from metric_rates import speed
from metric_codex import read_codex
from metric_pi import read_pi
from metric_droid import read_droid
from metric_grok import read_grok
from agy_cost_events import step_update_time, transcript_model
from metric_items import make_sub_item as _make_sub_item, summarize
from pricing import MODEL_MAP, canonical_model_label, estimate_source, calc_call_cost, model_quota_family, short_model_hint
from subagent_labels import dispatch_paths, dispatch_patterns, extract_cli_task_label, format_native_label

CODEX_SESSIONS_DIR = HOME / ".codex" / "sessions"
PI_SESSIONS_DIR = HOME / ".pi" / "agent" / "sessions"
FACTORY_SESSIONS_DIR = HOME / ".factory" / "sessions"
GROK_SESSIONS_DIR = HOME / ".grok" / "sessions"
SUBAGENT_APP_DIRS = [*APP_DIRS, ("CLI", HOME / ".gemini" / "antigravity-subagent")]

RE_SA_VIEW = re.compile(rf"▶\s*(agy|codex|claude|pi|droid|grok)\s+会话\s+({UUID_PAT})(?:（([^）]+)）)?")
RE_CONV_ID = re.compile(rf'"conversation_id"\s*:\s*"({UUID_PAT})"')
RE_SESSION_ID = re.compile(rf'"session_id"\s*:\s*"({UUID_PAT})"')
RE_GROK_SESSION_ID = re.compile(rf'"sessionId"\s*:\s*"({UUID_PAT})"')
RE_SA_RUN, RE_AGY_RESUME, RE_CODEX_RESUME, RE_DROID_RESUME, RE_GROK_RESUME, RE_PI_RESUME = dispatch_patterns(UUID_PAT)
RE_DIRECT_AGY = re.compile(r"(?:^|[\s/&;|])(?:agy\s+-p\b|sa-agy\b)")
RE_DIRECT_CODEX = re.compile(r"(?:^|[\s/&;|])(?:codex\s+exec\b|sa-codex\b)")
RE_DIRECT_PI = re.compile(r"(?:^|[\s/&;|])(?:pi\s+-p\b|sa-pi\b)")
RE_DIRECT_DROID = re.compile(r"(?:^|[\s/&;|])(?:droid\s+exec\b|sa-droid\b)")
RE_DIRECT_GROK = re.compile(r"(?:^|[\s/&;|])(?:grok\s+-p\b|sa-grok\b)")
RE_CD_DIR = re.compile(r"""(?:^|&&|;)\s*cd\s+((?:"[^"]*"|'[^']*'|[^\s;&|"']+))""")
RE_MODEL_LONG = re.compile(r"--model\s+([^\s\"']+)")
RE_MODEL_SHORT = re.compile(r"(?:^|\s)-m\s+([^\s\"']+)")
RE_EFFORT_ARG = re.compile(
    r"""(?:model_reasoning_effort=['"]?([a-zA-Z0-9_-]+)['"]?|--effort\s+([a-zA-Z0-9_-]+)|(?:^|\s)-r\s+(low|medium|high|xhigh)\b)"""
)
RE_CAT_SPEC = re.compile(r"\bcat\s+([^\s\"')]+)")
RE_DROID_OUT = re.compile(r">\s*([^\s;&|\"']+\.json)\b")
RE_PI_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2})-(\d{2})-(\d{2})-(\d{3})Z_([0-9a-f-]{36})\.jsonl$")
_DISPATCH_BYTES = (
    b"sa-run.sh",
    b"sa-codex",
    b"sa-agy",
    b"sa-droid",
    b"sa-grok",
    b"sa-pi",
    b"codex exec",
    b"agy -p",
    b"agy --conversation",
    b"pi -p",
    b"droid exec",
    b"grok -p",
    b"grok --resume",
    b"grok -r",
    b"grok -s",
    b"grok --session-id",
    b"invoke_subagent",
    b"send_message",
)

_SUBAGENT_STUBS_CACHE = BoundedCache(128)
_COMPLETED_STEP_STUBS = BoundedCache(8000)
_STUBS_PARSE_LOCK = threading.Lock()
_CODEX_ROLLOUT_PATHS = BoundedCache(512)
_CODEX_METRICS_CACHE = BoundedCache(128)
_PI_SESSION_PATHS = BoundedCache(512)
_PI_METRICS_CACHE = BoundedCache(128)
_DROID_SETTINGS_PATHS = BoundedCache(512)
_DROID_METRICS_CACHE = BoundedCache(128)
_GROK_SESSION_DIRS = BoundedCache(512)
_GROK_METRICS_CACHE = BoundedCache(128)
_AGY_TRANSCRIPT_CACHE = BoundedCache(128)


_LOG = logging.getLogger(__name__)
_STEP_DEPENDENCIES = BoundedCache(8000)


def _dependency_signature(paths):
    result = []
    for path in sorted(set(map(str, paths))):
        try:
            st = Path(path).stat()
            result.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            result.append((path, 0, 0))
    return tuple(result)


def _stub_dependencies(conv_id, bdir, stubs):
    paths = [st['raw_file'] for st in stubs if st.get('raw_file')]
    for st in stubs:
        if st.get('step_idx'):
            paths.append(bdir / 'steps' / f"{st['step_idx']}" / 'output.txt')
    for key in _STEP_DEPENDENCIES:
        if key[0] == conv_id:
            paths.extend(_STEP_DEPENDENCIES.get(key, ()))
    return _dependency_signature(paths)


def _live_metrics(value):
    out = dict(value)
    if '_saw_terminal' in out:
        out['is_done'] = bool(out.get('_terminal_done', True)) if out['_saw_terminal'] else time.time() - out['mtime'] >= 120.0
    return out


def _publish_metrics(cache, key, mtime, value):
    cache.publish(key, (mtime, value), lambda entry: entry[0])
    return _live_metrics(value)


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_codex_thread(thread_or_path: str | Path) -> dict | None:
    """Analyze a Codex CLI subagent session by `thread_id` or direct `.jsonl` file `Path`."""
    rollout: Path | None = None
    if isinstance(thread_or_path, Path) or (
        isinstance(thread_or_path, str) and (thread_or_path.endswith(".jsonl") or "/" in thread_or_path)
    ):
        cand = Path(thread_or_path)
        if cand.exists():
            rollout = cand
    else:
        thread_id = str(thread_or_path)
        if not CODEX_SESSIONS_DIR.exists():
            return None
        rollout = _CODEX_ROLLOUT_PATHS.get(thread_id)
        if rollout is None or not rollout.exists():
            matches = sorted(cached_paths(CODEX_SESSIONS_DIR, f"*{thread_id}.jsonl", recursive=True))
            if not matches:
                return None
            rollout = matches[-1]
            _CODEX_ROLLOUT_PATHS[thread_id] = rollout

    if rollout is None:
        return None

    try:
        mtime = rollout.stat().st_mtime
    except Exception:
        return None

    cache_key = str(rollout)
    cached = _CODEX_METRICS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return _live_metrics(cached[1])

    res = read_codex(rollout, mtime)
    return _publish_metrics(_CODEX_METRICS_CACHE, cache_key, mtime, res) if res is not None else None


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_pi_session(session_or_path: str | Path) -> dict | None:
    """Analyze a `pi` CLI subagent session by `session_id` or direct `.jsonl` file `Path`."""
    sess_path: Path | None = None
    if isinstance(session_or_path, Path) or (
        isinstance(session_or_path, str) and (session_or_path.endswith(".jsonl") or "/" in session_or_path)
    ):
        cand = Path(session_or_path)
        if cand.exists():
            sess_path = cand
    else:
        session_id = str(session_or_path)
        if not PI_SESSIONS_DIR.exists():
            return None
        sess_path = _PI_SESSION_PATHS.get(session_id)
        if sess_path is None or not sess_path.exists():
            matches = sorted(cached_paths(PI_SESSIONS_DIR, f"*_{session_id}.jsonl", recursive=True))
            if not matches:
                return None
            sess_path = matches[-1]
            _PI_SESSION_PATHS[session_id] = sess_path

    if sess_path is None:
        return None

    try:
        mtime = sess_path.stat().st_mtime
    except Exception:
        return None

    cache_key = str(sess_path)
    cached = _PI_METRICS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return _live_metrics(cached[1])

    res = read_pi(sess_path, mtime)
    return _publish_metrics(_PI_METRICS_CACHE, cache_key, mtime, res) if res is not None else None


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_droid_session(session_or_path: str | Path, raw_file_str: str = "") -> dict | None:
    """Analyze a `droid exec` fallback subagent session from `~/.factory/sessions` or `-o json`."""
    settings_path: Path | None = None
    if isinstance(session_or_path, Path) or (
        isinstance(session_or_path, str) and (session_or_path.endswith(".json") or "/" in session_or_path)
    ):
        cand = Path(session_or_path)
        if cand.exists():
            settings_path = cand
    else:
        session_id = str(session_or_path)
        cached_sp = _DROID_SETTINGS_PATHS.get(session_id)
        if cached_sp and cached_sp.exists():
            settings_path = cached_sp
        elif FACTORY_SESSIONS_DIR.exists() and not session_id.startswith("pending-"):
            matches = list(cached_paths(FACTORY_SESSIONS_DIR, f"{session_id}.settings.json", recursive=True))
            if matches:
                settings_path = matches[0]
                _DROID_SETTINGS_PATHS[session_id] = settings_path

    raw_path = Path(raw_file_str) if raw_file_str else None
    src = (
        settings_path
        if (settings_path and settings_path.exists())
        else (raw_path if (raw_path and raw_path.exists()) else None)
    )
    if src is None:
        return None

    jsonl_path: Path | None = None
    if settings_path and settings_path.name.endswith(".settings.json"):
        sid = settings_path.name[: -len(".settings.json")]
        cand_jl = settings_path.with_name(f"{sid}.jsonl")
        if cand_jl.exists():
            jsonl_path = cand_jl

    try:
        mtime = src.stat().st_mtime
        if jsonl_path is not None:
            mtime = max(mtime, jsonl_path.stat().st_mtime)
    except Exception:
        return None

    cache_key = (str(src), _dependency_signature(p for p in (raw_path, jsonl_path) if p))
    cached = _DROID_METRICS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return _live_metrics(cached[1])

    res = read_droid(src, raw_path, jsonl_path, mtime)
    return _publish_metrics(_DROID_METRICS_CACHE, cache_key, mtime, res) if res is not None else None


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_grok_session(session_or_path: str | Path, raw_file_str: str = "") -> dict | None:
    """Analyze a Grok CLI session from `~/.grok/sessions/<cwd>/<session_id>/` or streaming-json `raw_file`."""
    sess_dir: Path | None = None
    if isinstance(session_or_path, Path) or (isinstance(session_or_path, str) and "/" in session_or_path):
        cand = Path(session_or_path)
        if cand.is_file():
            cand = cand.parent
        if cand.is_dir():
            sess_dir = cand
    else:
        session_id = str(session_or_path)
        cached_sd = _GROK_SESSION_DIRS.get(session_id)
        if cached_sd and cached_sd.is_dir():
            sess_dir = cached_sd
        elif GROK_SESSIONS_DIR.exists() and not session_id.startswith("pending-"):
            matches = list(cached_paths(GROK_SESSIONS_DIR, f"*/{session_id}"))
            if matches and matches[0].is_dir():
                sess_dir = matches[0]
                _GROK_SESSION_DIRS[session_id] = sess_dir

    raw_path = Path(raw_file_str) if raw_file_str else None
    if sess_dir is None and (raw_path is None or not raw_path.exists()):
        return None

    summary_path = (sess_dir / "summary.json") if sess_dir else None
    updates_path = (sess_dir / "updates.jsonl") if sess_dir else None
    events_path = (sess_dir / "events.jsonl") if sess_dir else None
    usage_path = (sess_dir / "usage.json") if sess_dir else None

    mtime = usage_mtime = 0.0
    for p in (summary_path, updates_path, events_path, usage_path, raw_path):
        if p is not None and p.exists():
            try:
                stamp = p.stat().st_mtime
                mtime = max(mtime, stamp)
                if p == usage_path:
                    usage_mtime = stamp
            except Exception:
                pass
    if mtime <= 0:
        return None

    cache_key = (str(sess_dir or raw_path), usage_mtime)
    cached = _GROK_METRICS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return _live_metrics(cached[1])

    res = read_grok(summary_path, updates_path, events_path, usage_path, raw_path, mtime, usage_mtime)
    return _publish_metrics(_GROK_METRICS_CACHE, cache_key, mtime, res) if res is not None else None


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_agy_transcript_fallback(sub_or_path: str | Path, model_hint: str = "") -> dict | None:
    """Fallback analyzer when an `agy` subagent saved `transcript.jsonl` without `.db`."""
    candidates: list[Path] = []
    if isinstance(sub_or_path, Path) or (
        isinstance(sub_or_path, str) and (sub_or_path.endswith(".jsonl") or "/" in sub_or_path)
    ):
        cand = Path(sub_or_path)
        if cand.exists():
            candidates.append(cand)
    else:
        sub_id = str(sub_or_path)
        for _, base in SUBAGENT_APP_DIRS:
            tf = base / "brain" / sub_id / ".system_generated" / "logs" / "transcript_full.jsonl"
            t = base / "brain" / sub_id / ".system_generated" / "logs" / "transcript.jsonl"
            src = tf if tf.exists() else (t if t.exists() else None)
            if src is not None:
                candidates.append(src)

    for src in candidates:
        try:
            mtime = src.stat().st_mtime
        except Exception:
            continue
        eff_hint = model_hint
        if not eff_hint:
            cand_cid = str(sub_or_path) if not ("/" in str(sub_or_path)) else ""
            if not cand_cid and len(src.parents) >= 3 and src.parents[1].name == ".system_generated":
                cand_cid = src.parents[2].name
            b_enum = _BUILTIN_SUBAGENT_MODEL_ENUM.get(cand_cid)
            if b_enum and b_enum in MODEL_MAP:
                eff_hint = MODEL_MAP[b_enum][0]
        cache_key = f"{src}:{eff_hint}"
        cached = _AGY_TRANSCRIPT_CACHE.get(cache_key)
        if cached and cached[0] == mtime:
            return _live_metrics(cached[1])
        tot_in = 0
        tot_cache = 0
        tot_out = 0
        calls = 0
        gen_out = 0
        gen_dur = 0.0
        timed_steps = 0
        objs: list[dict] = []
        try:
            for line in log_lines(src):
                try:
                    objs.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue
        if not objs:
            continue

        model = eff_hint
        tot_cost = 0.0
        cost_events: list[CostEvent] = []
        per_model_map: dict[str, dict] = {}

        new_turn = True
        for i, obj in enumerate(objs):
            model = transcript_model(obj) or model
            if obj.get("type") == "USER_INPUT":
                new_turn = True
            inp = 0
            cache = 0
            out = 0
            t_start = 0.0
            s_idx = 0
            dur_sec = 0.0

            if obj.get("type") == "PLANNER_RESPONSE":
                inp = int(obj.get("input_tokens") or 0)
                cache = int(obj.get("cache_read_tokens") or 0)
                out = int(obj.get("output_tokens") or 0)
                t_start = parse_iso_ts(obj.get("created_at"))
                s_idx = int(obj.get("step_index") or 0)
            elif obj.get("event") == "step_update":
                su = obj.get("step_update") or {}
                if su.get("step_type") == "user_input":
                    new_turn = True
                usage = su.get("usage") or {}
                inp = int(usage.get("input_tokens") or 0)
                cache = int(usage.get("cache_read_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                s_idx = int(su.get("step_index") or 0)
                dur_sec = float(su.get("duration_seconds") or 0.0)
                t_start = step_update_time(obj, su, parse_iso_ts) or mtime

            if inp > 0 or cache > 0 or out > 0:
                calls += 1
                tot_in += inp
                tot_cache += cache
                tot_out += out
                s_ts = float(t_start or mtime)
                c_cost = calc_call_cost(model, inp, cache, out, event_ts=s_ts)
                tot_cost += c_cost
                m_label = short_model_hint(model) or "Unknown"
                pm = per_model_map.setdefault(
                    m_label,
                    {"model": m_label, "calls": 0, "prompt": 0, "in": 0, "cache": 0, "cache_write": 0, "out": 0, "cost": 0.0},
                )
                pm["calls"] += 1
                pm["prompt"] += inp + cache
                pm["in"] += inp
                pm["cache"] += cache
                pm["out"] += out
                pm["cost"] += c_cost
                if c_cost > 0:
                    uid = f"{s_idx}:{inp}:{cache}:{out}:{int(s_ts)}"
                    fam = model_quota_family(model)
                    cost_events.append((round(s_ts, 3), round(c_cost, 8), fam, uid, inp, cache, out, int(new_turn),
                                        0, canonical_model_label(m_label)))
                    new_turn = False
                if dur_sec > 0 and out > 0:
                    gen_out += out
                    timed_steps += 1
                    gen_dur += dur_sec
                elif obj.get("type") == "PLANNER_RESPONSE":
                    t_end: float | None = None
                    if t_start:
                        for j in range(i + 1, len(objs)):
                            t_next = parse_iso_ts(objs[j].get("created_at"))
                            if t_next and t_next >= t_start:
                                t_end = t_next
                                break
                        if out > 0 and t_end and t_end > t_start:
                            gen_out += out
                            timed_steps += 1
                            gen_dur += t_end - t_start

        last_obj = objs[-1]
        last_obj_ts = parse_iso_ts(last_obj.get("created_at")) or (mtime if last_obj.get("event") else 0.0)
        is_idle = bool(
            (last_obj.get("type") == "PLANNER_RESPONSE" and last_obj.get("status") == "DONE")
            or (last_obj.get("event") == "result")
        )
        prompt = tot_in + tot_cache
        hit_pct = (tot_cache * 100.0 / prompt) if prompt > 0 else None
        res = {
            "model": short_model_hint(model) or "Unknown",
            "calls": calls,
            "prompt": prompt,
            "in": tot_in,
            "cache": tot_cache,
            "hit_pct": hit_pct,
            "out": tot_out,
            "think": None, "resp": None, "cache_write": 0,
            **speed("step_est", gen_out, gen_dur, timed_steps), "cost_source": estimate_source(model),
            "cost": tot_cost,
            "cost_events": cost_events,
            "per_model": list(per_model_map.values()),
            "is_idle": is_idle,
            "last_ts": last_obj_ts,
        }
        return _publish_metrics(_AGY_TRANSCRIPT_CACHE, cache_key, mtime, res)
    return None


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


def _resolve_builtin_model_enum(parent_conv_id: str, model_hint: str) -> int:
    """Resolve the true `MODEL_MAP` enum for a built-in subagent (`inherit`, `pro`, `flash`, `flash_lite`)."""
    parent_cached = _ANALYSIS_CACHE.get(parent_conv_id)
    parent_enum = int((parent_cached[1].get('model') or {}).get('enum') or 0) if parent_cached else 0
    return resolve_builtin_enum_from_parent(parent_enum, model_hint)


def _list_pi_session_meta() -> list[dict]:
    """Fast filename-based index of `~/.pi/agent/sessions/**/*.jsonl` without reading file contents."""
    out: list[dict] = []
    if not PI_SESSIONS_DIR.exists():
        return out
    try:
        for p in cached_paths(PI_SESSIONS_DIR, "*.jsonl", recursive=True):
            m = RE_PI_FILENAME.match(p.name)
            if m:
                iso = f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{m.group(4)}Z"
                ts = parse_iso_ts(iso) or 0.0
                sid = m.group(5)
                _PI_SESSION_PATHS.setdefault(sid, p)
                out.append({
                    "id": sid,
                    "ts": ts,
                    "dir_name": p.parent.name,
                    "path": p,
                })
    except Exception:
        pass
    return out


def _list_droid_session_meta() -> list[dict]:
    """Fast stat-based index of `~/.factory/sessions/**/*.settings.json` for running/cleaned-up droid matching."""
    out: list[dict] = []
    if not FACTORY_SESSIONS_DIR.exists():
        return out
    try:
        for p in cached_paths(FACTORY_SESSIONS_DIR, "*.settings.json", recursive=True):
            if not p.name.endswith(".settings.json"):
                continue
            sid = p.name[: -len(".settings.json")]
            _DROID_SETTINGS_PATHS.setdefault(sid, p)
            jl = p.with_name(f"{sid}.jsonl")
            st = jl.stat() if jl.exists() else p.stat()
            ts = float(getattr(st, "st_birthtime", 0.0) or st.st_mtime)
            out.append({
                "id": sid,
                "ts": ts,
                "dir_name": p.parent.name,
                "path": p,
            })
    except Exception:
        pass
    return out


def _list_grok_session_meta() -> list[dict]:
    """Fast stat-based index of `~/.grok/sessions/*/*/summary.json` for running/completed grok subagent matching."""
    out: list[dict] = []
    if not GROK_SESSIONS_DIR.exists():
        return out
    try:
        for p in cached_paths(GROK_SESSIONS_DIR, "*/*/summary.json"):
            sdir = p.parent
            sid = sdir.name
            _GROK_SESSION_DIRS.setdefault(sid, sdir)
            st = p.stat()
            ts = float(getattr(st, "st_birthtime", 0.0) or st.st_mtime)
            dir_dec = urllib.parse.unquote(sdir.parent.name)
            out.append({
                "id": sid,
                "ts": ts,
                "dir_name": dir_dec,
                "path": sdir,
            })
    except Exception:
        pass
    return out


@serialized(lambda first, *args, **kwargs: str(first))
def discover_subagent_stubs(conv_id: str) -> list[dict]:
    """Deterministically bind each parent tool step (`step_type IN (127, 132)`) to its own subagent & live status."""
    db_path: Path | None = None
    matched_base: Path = APP_DIRS[0][1]
    for _, base in APP_DIRS:
        cand = base / "conversations" / f"{conv_id}.db"
        if cand.exists():
            db_path = cand
            matched_base = base
            break
    if db_path is None:
        return discover_swarm_stubs(conv_id, APP_DIRS)

    bdir = matched_base / "brain" / conv_id / ".system_generated"
    tasks_dir = bdir / "tasks"
    db_mtime = 0.0
    tasks_mtime = 0.0
    try:
        db_mtime = get_db_mtime(db_path)
        if tasks_dir.exists():
            for lf in tasks_dir.glob("*.log"):
                tasks_mtime = max(tasks_mtime, lf.stat().st_mtime)
    except Exception:
        pass

    cache_key = (db_mtime, tasks_mtime, swarm_dir_signature(conv_id, APP_DIRS))
    cached = _SUBAGENT_STUBS_CACHE.get(conv_id)
    dependencies = _stub_dependencies(conv_id, bdir, cached[1] if cached else [])
    if cached and cached[0] == cache_key and cached[3] == dependencies:
        if not cached[4] or time.monotonic() - cached[2] < 0.5:
            return [dict(stub) for stub in cached[1]]
    incomplete = False

    try:
        with contextlib.closing(connect_readonly(db_path, timeout=0.5)) as conn:
            _dispatch_clauses = " OR ".join(["instr(step_payload, ?) > 0"] * len(_DISPATCH_BYTES))
            rows = conn.execute(
                f"""
                SELECT idx, step_type, status, metadata, step_payload
                FROM steps
                WHERE step_type = 127
                   OR (
                        step_type = 132
                        AND (
                            {_dispatch_clauses}
                        )
                   )
                ORDER BY idx
                """,
                _DISPATCH_BYTES,
            ).fetchall()
    except Exception:
        if cached:
            return [dict(stub) for stub in cached[1]]
        return discover_swarm_stubs(conv_id, APP_DIRS)

    found: dict[str, dict] = {}
    pi_meta_list: list[dict] | None = None
    used_pi_ids: set[str] = set()
    droid_meta_list: list[dict] | None = None
    used_droid_ids: set[str] = set()
    grok_meta_list: list[dict] | None = None
    used_grok_ids: set[str] = set()

    for idx, st_type, st_status, meta, payload in rows:
        log_ns = 0
        try:
            tlog_p = tasks_dir / f"task-{idx}.log"
            sout_p = bdir / "steps" / f"{idx}" / "output.txt"
            if tlog_p.exists():
                log_ns = tlog_p.stat().st_mtime_ns
            if sout_p.exists():
                log_ns = max(log_ns, sout_p.stat().st_mtime_ns)
        except Exception:
            pass
        payload_crc = zlib.crc32(payload or b"")
        step_key = (conv_id, int(idx or 0), int(st_status or 0), int(log_ns),
                    payload_crc, _dependency_signature(_STEP_DEPENDENCIES.get((conv_id, idx), ())))
        terminal = int(st_status or 0) not in (1, 2, 8)
        if terminal:
            with _STUBS_PARSE_LOCK:
                cache_hit = step_key in _COMPLETED_STEP_STUBS
                cached_step = list(_COMPLETED_STEP_STUBS[step_key]) if cache_hit else None
            if cache_hit:
                for sid, stub in cached_step:
                    found[sid] = dict(stub)
                continue
        before_found = {k: dict(v) for k, v in found.items()} if terminal else {}
        step_incomplete = False
        try:
            if not payload or (st_type == 132 and not any(b in payload for b in _DISPATCH_BYTES)):
                continue
            tname, args, inline_txt = _parse_step132_payload(payload)
            if st_type == 127:
                tname = "invoke_subagent"
            if tname not in ("run_command", "invoke_subagent", "send_message"):
                continue

            f_map = {k: v for k, t, v in decode_proto(meta) if t == "bytes" and isinstance(v, bytes)} if meta else {}
            t_start = parse_proto_duration(f_map.get(1, b""))

            if tname == "send_message":
                recipient = str(args.get("Recipient") or "").strip()
                if recipient and recipient in found:
                    if t_start > 0:
                        found[recipient]["prompt_ts"] = max(
                            float(found[recipient].get("prompt_ts") or found[recipient].get("step_ts") or 0.0),
                            t_start,
                        )
                    found[recipient]["prompt_idx"] = max(
                        int(found[recipient].get("prompt_idx") or found[recipient].get("step_idx") or 0),
                        int(idx or 0),
                    )
                else:
                    step_incomplete = True
                continue

            step_texts = [inline_txt]
            tlog = tasks_dir / f"task-{idx}.log"
            if tlog.exists():
                try:
                    step_texts.append(tlog.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    pass
            sout = bdir / "steps" / f"{idx}" / "output.txt"
            if sout.exists():
                try:
                    step_texts.append(sout.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    pass
            combined_out = "\n".join(step_texts)

            if tname == "invoke_subagent":
                if st_type == 127:
                    spawned_127 = _parse_step127_subagents(payload, args)
                    if not spawned_127:
                        incomplete = True
                        step_incomplete = True
                    for sid, role, m_hint, tpath in spawned_127:
                        if sid and sid != conv_id:
                            found[sid] = {
                                "id": sid,
                                "agent": "builtin",
                                "label": role,
                                "model_hint": m_hint,
                                "effort_hint": "",
                                "step_status": st_status,
                                "step_idx": idx,
                                "step_ts": t_start,
                                "prompt_idx": idx,
                                "prompt_ts": t_start,
                                "has_done_marker": False,
                                "has_err_marker": True if st_status in (4, 6, 7) else False,
                                "raw_file": tpath,
                            }
                    continue

                _STEP_DEPENDENCIES[(conv_id, idx)] = (str(sout),)
                role = str(args.get("Role") or args.get("TypeName") or "subagent")
                m_id = RE_INVOKE_ID.search(combined_out)
                sub_id: str | None = m_id.group(1) if m_id else None
                if not sub_id:
                    m_proto = RE_PROTO_SUB_UUID.search(payload)
                    if m_proto:
                        sub_id = m_proto.group(1).decode("ascii")
                if not sub_id and st_status in (1, 2, 8):
                    sub_id = f"pending-{idx}"
                if not sub_id or sub_id.startswith("pending-"):
                    incomplete = True
                    step_incomplete = True
                if sub_id and sub_id != conv_id:
                    found[sub_id] = {
                        "id": sub_id,
                        "agent": "builtin",
                        "label": format_native_label(role, str(args.get("Prompt") or args.get("Task") or "")),
                        "model_hint": str(args.get("Model") or ""),
                        "effort_hint": "",
                        "step_status": st_status,
                        "step_idx": idx,
                        "step_ts": t_start,
                        "prompt_idx": idx,
                        "prompt_ts": t_start,
                        "has_done_marker": False,
                        "has_err_marker": True if st_status in (4, 6, 7) else False,
                        "raw_file": "",
                    }
                continue

            cmd = str(args.get("CommandLine") or "")
            stripped = cmd.lstrip()
            if (
                any(
                    stripped.startswith(p)
                    for p in ("python3", "grep", "rg", "cat", "ls", "jq", "ps ", "echo ", "PYTHONPATH=")
                )
                or "--help" in cmd
            ):
                continue

            m_cd = RE_CD_DIR.search(cmd)
            cwd = str(Path(args.get("Cwd") or HOME / "Dev") / (m_cd[1].strip("\"'").replace("~", str(HOME)) if m_cd else ""))
            m_long = list(RE_MODEL_LONG.finditer(cmd))
            m_short = list(RE_MODEL_SHORT.finditer(cmd))
            model_hint = m_long[-1].group(1) if m_long else (m_short[-1].group(1) if m_short else "")
            m_eff = list(RE_EFFORT_ARG.finditer(cmd))
            effort_hint = (m_eff[-1].group(1) or m_eff[-1].group(2) or m_eff[-1].group(3)) if m_eff else ""

            sa_matches = list(RE_SA_RUN.finditer(cmd.split("--prompt", 1)[0]))
            prog = ""
            label = ""
            raw_file: Path | None = None
            if sa_matches:
                m = sa_matches[-1]
                raw_path_str, prog = dispatch_paths(m, HOME)
                if prog.startswith("sa-"):
                    prog = prog[3:]
                stem = Path(raw_path_str).stem
                for pfx in ("sa-", "subagent-", "tmp-"):
                    if stem.startswith(pfx) and len(stem) > len(pfx):
                        stem = stem[len(pfx) :]
                label = stem
                raw_file = Path(raw_path_str) if Path(raw_path_str).is_absolute() else (Path(cwd) / raw_path_str)
            elif RE_DIRECT_AGY.search(cmd) or RE_AGY_RESUME.search(cmd):
                prog = "agy"
            elif RE_DIRECT_CODEX.search(cmd) or RE_CODEX_RESUME.search(cmd):
                prog = "codex"
            elif RE_DIRECT_PI.search(cmd) or RE_PI_RESUME.search(cmd):
                prog = "pi"
            elif RE_DIRECT_DROID.search(cmd) or RE_DROID_RESUME.search(cmd):
                prog = "droid"
            elif RE_DIRECT_GROK.search(cmd) or RE_GROK_RESUME.search(cmd):
                prog = "grok"
            else:
                continue

            if not label:
                m_out = RE_DROID_OUT.search(cmd)
                m_cat = RE_CAT_SPEC.search(cmd)
                if m_out:
                    out_rel = m_out.group(1).replace("~", str(HOME))
                    label = Path(out_rel).stem
                    raw_file = Path(out_rel) if Path(out_rel).is_absolute() else (Path(cwd) / out_rel)
                elif m_cat:
                    cat_stem = Path(m_cat.group(1)).stem
                    for pfx in ("spec_", "spec-", "task_", "task-", "brief_", "brief-"):
                        if cat_stem.startswith(pfx) and len(cat_stem) > len(pfx):
                            cat_stem = cat_stem[len(pfx) :]
                    label = f"{prog}_{cat_stem}"
                else:
                    label = f"{prog}-cli"
            label = extract_cli_task_label(cmd, args, prog, label if raw_file else "", label.removeprefix(f"{prog}_") if not raw_file and label.startswith(f"{prog}_") else "")

            sub_id: str | None = None
            resume_id: str | None = None
            m_res_agy = RE_AGY_RESUME.search(cmd)
            m_res_codex = RE_CODEX_RESUME.search(cmd)
            m_res_droid = RE_DROID_RESUME.search(cmd)
            m_res_grok = RE_GROK_RESUME.search(cmd) if prog == "grok" else None
            m_res_pi = RE_PI_RESUME.search(cmd) if prog == "pi" else None
            if m_res_agy:
                resume_id = m_res_agy.group(1)
            elif m_res_codex:
                resume_id = m_res_codex.group(1)
            elif m_res_droid:
                resume_id = m_res_droid.group(1)
            elif m_res_grok:
                resume_id = m_res_grok.group(1)
            elif m_res_pi:
                resume_id = m_res_pi.group(1)

            if not sub_id:
                for m_v in RE_SA_VIEW.finditer(combined_out):
                    if m_v.group(2) != conv_id:
                        sub_id = m_v.group(2)
                        prog = m_v.group(1)
                        if m_v.group(3) and m_v.group(3) != "acp":
                            model_hint = m_v.group(3)
                if sub_id:
                    if prog == "droid":
                        used_droid_ids.add(sub_id)
                    elif prog == "grok":
                        used_grok_ids.add(sub_id)
            if not sub_id and prog == "agy":
                for m_c in RE_CONV_ID.finditer(combined_out):
                    if m_c.group(1) != conv_id:
                        sub_id = m_c.group(1)
                        break
            rf_probe = ""
            if raw_file and raw_file.exists():
                try:
                    st_rf = raw_file.stat()
                    if t_start <= 0 or st_rf.st_mtime >= t_start - 5.0:
                        if st_rf.st_size < 2_000_000:
                            rf_txt = raw_file.read_text(encoding="utf-8", errors="replace")
                            combined_out += "\n" + rf_txt
                            rf_probe = rf_txt
                        else:
                            with raw_file.open("r", encoding="utf-8", errors="replace") as rf_fh:
                                rf_probe = rf_fh.read(16_384)
                        prog, sub_id, model_hint, effort_hint = discovery_hints(rf_probe, prog, sub_id, model_hint, effort_hint, conv_id)
                        if sub_id:
                            if prog == "droid":
                                used_droid_ids.add(sub_id)
                            elif prog == "grok":
                                used_grok_ids.add(sub_id)
                        if not sub_id and prog == "agy":
                            m_c = RE_CONV_ID.search(rf_probe)
                            if m_c and m_c.group(1) != conv_id:
                                sub_id = m_c.group(1)
                        elif not sub_id and prog == "codex":
                            match = re.search(r'"thread_id"\s*:\s*"(' + UUID_PAT + r')"', rf_probe)
                            if match:
                                sub_id = match.group(1)
                        elif not sub_id and prog == "grok":
                            m_g = RE_GROK_SESSION_ID.search(rf_probe)
                            if m_g and m_g.group(1) != conv_id:
                                sub_id = m_g.group(1)
                                used_grok_ids.add(sub_id)
                except Exception:
                    pass
            if not sub_id and prog in ("droid", "claude"):
                m_sid = RE_SESSION_ID.search(combined_out)
                if m_sid and m_sid.group(1) != conv_id:
                    sub_id = m_sid.group(1)
                    if prog == "droid":
                        used_droid_ids.add(sub_id)
            if not sub_id and prog == "grok":
                m_gsid = RE_GROK_SESSION_ID.search(combined_out)
                if m_gsid and m_gsid.group(1) != conv_id:
                    sub_id = m_gsid.group(1)
                    used_grok_ids.add(sub_id)
            saw_handoff = ('"session_handoff"' in combined_out) or ('"session_handoff"' in rf_probe)
            if not sub_id and resume_id and not saw_handoff:
                sub_id = resume_id
                if prog == "droid":
                    used_droid_ids.add(sub_id)
                elif prog == "grok":
                    used_grok_ids.add(sub_id)
            if not sub_id and prog == "grok":
                if grok_meta_list is None:
                    grok_meta_list = _list_grok_session_meta()
                cands = [
                    gs
                    for gs in grok_meta_list
                    if gs["id"] not in used_grok_ids
                    and (gs["dir_name"] == cwd or cwd in gs["dir_name"] or gs["dir_name"] in cwd)
                    and t_start > 0
                    and -5.0 <= (gs["ts"] - t_start) <= 240.0
                ]
                if cands:
                    cands.sort(key=lambda gs: abs(gs["ts"] - t_start))
                    sub_id = cands[0]["id"]
                    used_grok_ids.add(sub_id)
            if not sub_id and prog == "droid":
                if droid_meta_list is None:
                    droid_meta_list = _list_droid_session_meta()
                cwd_slug = "-" + cwd.strip("/").replace("/", "-")
                cands = [
                    ds
                    for ds in droid_meta_list
                    if ds["id"] not in used_droid_ids
                    and (ds["dir_name"] == cwd_slug or cwd_slug in ds["dir_name"])
                    and t_start > 0
                    and -5.0 <= (ds["ts"] - t_start) <= 240.0
                ]
                if cands:
                    cands.sort(key=lambda ds: abs(ds["ts"] - t_start))
                    sub_id = cands[0]["id"]
                    used_droid_ids.add(sub_id)
            if not sub_id and prog == "pi":
                if pi_meta_list is None:
                    pi_meta_list = _list_pi_session_meta()
                cwd_slug = "--" + cwd.strip("/").replace("/", "-") + "--"
                cands = [
                    ps
                    for ps in pi_meta_list
                    if ps["id"] not in used_pi_ids
                    and (ps["dir_name"] == cwd_slug or cwd_slug in ps["dir_name"])
                    and t_start > 0
                    and -5.0 <= (ps["ts"] - t_start) <= 240.0
                ]
                if cands:
                    cands.sort(key=lambda ps: abs(ps["ts"] - t_start))
                    sub_id = cands[0]["id"]
                    used_pi_ids.add(sub_id)

            if not sub_id and st_status in (1, 2, 8):
                sub_id = f"pending-{idx}"

            if not sub_id or sub_id.startswith("pending-"):
                incomplete = True
                step_incomplete = True
            step_deps = [str(sout)]
            if raw_file:
                step_deps.append(str(raw_file))
            _STEP_DEPENDENCIES[(conv_id, idx)] = tuple(step_deps)
            if not sub_id or sub_id == conv_id:
                continue

            has_done = any(
                line.startswith("■ ") and not line.startswith("■ ERROR")
                for line in combined_out.splitlines()
            )
            has_err = any(
                line.startswith(("■ ERROR", "✗ "))
                for line in combined_out.splitlines()
            )

            if sub_id not in found:
                found[sub_id] = {
                    "id": sub_id,
                    "agent": prog,
                    "label": label,
                    "model_hint": model_hint,
                    "effort_hint": effort_hint,
                    "step_status": st_status,
                    "step_idx": idx,
                    "step_ts": t_start,
                    "prompt_idx": idx,
                    "prompt_ts": t_start,
                    "has_done_marker": has_done,
                    "has_err_marker": has_err,
                    "raw_file": str(raw_file) if raw_file else "",
                }
            else:
                if found[sub_id]["label"].endswith("-cli") and not label.endswith("-cli"):
                    found[sub_id]["label"] = label
                if model_hint and not found[sub_id]["model_hint"]:
                    found[sub_id]["model_hint"] = model_hint
                if effort_hint and not found[sub_id]["effort_hint"]:
                    found[sub_id]["effort_hint"] = effort_hint
                found[sub_id]["step_status"] = st_status
                found[sub_id]["step_idx"] = idx
                found[sub_id]["prompt_idx"] = max(int(found[sub_id].get("prompt_idx") or 0), int(idx or 0))
                if t_start > 0:
                    found[sub_id]["step_ts"] = t_start
                    found[sub_id]["prompt_ts"] = max(float(found[sub_id].get("prompt_ts") or 0.0), t_start)
                found[sub_id]["has_done_marker"] = has_done
                found[sub_id]["has_err_marker"] = has_err
                if raw_file:
                    found[sub_id]["raw_file"] = str(raw_file)

        finally:
            if terminal and not step_incomplete:
                changed = [
                    (sid, dict(stub))
                    for sid, stub in found.items()
                    if before_found.get(sid) != stub
                ]
                final_step_key = (
                    conv_id,
                    int(idx or 0),
                    int(st_status or 0),
                    int(log_ns),
                    payload_crc,
                    _dependency_signature(_STEP_DEPENDENCIES.get((conv_id, idx), ())),
                )
                with _STUBS_PARSE_LOCK:
                    if len(_COMPLETED_STEP_STUBS) > 8000:
                        _COMPLETED_STEP_STUBS.clear()
                    if not any(sid.startswith("pending-") for sid, _ in changed):
                        _COMPLETED_STEP_STUBS[final_step_key] = changed
    if found:
        try:
            with contextlib.closing(connect_readonly(db_path, timeout=0.5)) as conn:
                kill_rows = conn.execute(
                    "SELECT idx, step_payload FROM steps WHERE step_type = 132 AND instr(step_payload, ?) > 0 ORDER BY idx",
                    (b"manage_task",),
                ).fetchall()
            killed_steps: set[int] = set()
            for k_idx, k_payload in kill_rows:
                tname, k_args, _ = _parse_step132_payload(k_payload or b"")
                if tname == "manage_task" and str(k_args.get("Action") or "") == "kill":
                    tid = str(k_args.get("TaskId") or "")
                    m_tk = re.search(r"/task-(\d+)\b", tid)
                    if m_tk and int(m_tk.group(1)) < int(k_idx or 0):
                        killed_steps.add(int(m_tk.group(1)))
            for stub in found.values():
                if int(stub.get("step_idx") or 0) in killed_steps:
                    stub["has_done_marker"] = True
        except Exception:
            pass
    for stub in discover_swarm_stubs(conv_id, APP_DIRS):
        merge_swarm_stub(found, stub)
    stubs = list(found.values())
    dependencies = _stub_dependencies(conv_id, bdir, stubs)
    incomplete = incomplete or any(st["id"].startswith("pending-") for st in stubs)
    _SUBAGENT_STUBS_CACHE[conv_id] = (cache_key, stubs, time.monotonic(), dependencies, incomplete)
    return [dict(stub) for stub in stubs]


discover_subagents = discover_subagent_stubs

def _resolve_subagent_status(st: dict, sub_running: bool | None = None, sub_err: bool = False) -> str:
    if st.get("killed"):
        return "killed"
    step_st = int(st.get("step_status") or 3)
    has_done = bool(st.get("has_done_marker"))
    has_err = bool(st.get("has_err_marker")) or sub_err
    if has_err or step_st in (4, 6, 7):
        return "error"
    if step_st == 5:
        return "done"
    if sub_running is True and not has_done:
        return "running"
    if step_st in (1, 2, 8) and not has_done:
        return "running"
    return "done"


def collect_subagents(
    conv_id: str,
    on_cost_events: Callable[[str, float, list[CostEvent]], None] | None = None,
) -> tuple[dict, list[dict]]:
    """Collect token, TPS, cost & live status metrics for all subagents dispatched by `conv_id`."""
    stubs = discover_subagent_stubs(conv_id)
    items: list[dict] = []

    for source_stub in stubs:
        before_len = len(items)
        st = dict(source_stub)
        sub_id = st["id"]
        agent = st["agent"]
        label = st["label"]
        model_hint = st.get("model_hint") or ""
        effort_hint = st.get("effort_hint") or ""
        step_st = int(st.get("step_status") or 3)
        step_idx = int(st.get("step_idx") or 0)
        step_ts = float(st.get("step_ts") or 0.0)
        prompt_idx = int(st.get("prompt_idx") or step_idx)
        prompt_ts = float(st.get("prompt_ts") or step_ts)
        is_parent_active = step_st in (1, 2, 8)
        builtin_enum_override: int | None = None
        if agent == "builtin":
            with _BUILTIN_ENUM_LOCK:
                builtin_enum_override = _BUILTIN_SUBAGENT_MODEL_ENUM.get(sub_id)
            if not builtin_enum_override:
                builtin_enum_override = _resolve_builtin_model_enum(conv_id, model_hint)
            if not sub_id.startswith("pending-"):
                with _BUILTIN_ENUM_LOCK:
                    _BUILTIN_SUBAGENT_MODEL_ENUM.setdefault(sub_id, builtin_enum_override)
            _, b_short, _ = MODEL_MAP.get(builtin_enum_override, ("Gemini 4 Carbon (Medium)", "Carbon Med", 300_000))
            default_model = b_short
        else:
            default_model = short_model_hint(model_hint) or agent
            if effort_hint and f"({effort_hint})" not in default_model:
                default_model = f"{default_model} ({effort_hint})"

        if agent in ("agy", "builtin", "swarm"):
            db_path: Path | None = None
            if not sub_id.startswith("pending-"):
                for _, base in SUBAGENT_APP_DIRS:
                    cand = base / "conversations" / f"{sub_id}.db"
                    if cand.exists():
                        db_path = cand
                        break
            added = False
            if db_path is not None:
                try:
                    sub_mtime = get_db_mtime(db_path)
                    sub_data = analyze_conversation(
                        str(db_path),
                        sub_id,
                        label,
                        "CLI" if agent == "agy" else "App",
                        sub_mtime,
                        include_subagents=False,
                        on_cost_events=on_cost_events,
                        model_enum_override=builtin_enum_override,
                    )
                    if not sub_data.get("empty"):
                        cum = sub_data["cumulative"]
                        m_info = sub_data["model"]
                        last_ts = max(step_ts, float(sub_data.get("last_end_ts") or 0.0))
                        items.append(
                            _make_sub_item(
                                sub_id,
                                agent,
                                label,
                                native_worker_status(st, sub_data, _resolve_subagent_status),
                                m_info.get("short") or m_info.get("name") or default_model,
                                step_idx,
                                last_ts,
                                cum,
                                tps_key="dec_tps",
                                per_model=list(sub_data.get("main_models") or []),
                            )
                        )
                        added = True
                        finish_swarm_item(items[-1], st, _ANALYSIS_CACHE, conv_id, sub_data, canonical_model_label)
                except Exception:
                    pass
            if not added and not sub_id.startswith("pending-"):
                fb_src = st.get("raw_file") or sub_id
                fb_hint = default_model if agent == "builtin" else model_hint
                fb = analyze_agy_transcript_fallback(fb_src, fb_hint)
                if fb is not None and (fb["calls"] > 0 or is_parent_active):
                    if fb.get("is_idle", True):
                        st["has_done_marker"] = True
                    items.append(
                        _make_sub_item(
                            sub_id,
                            agent,
                            label,
                            _resolve_subagent_status(st, sub_running=not fb.get("is_idle", True)),
                            fb["model"],
                            step_idx,
                            max(step_ts, float(fb.get("last_ts") or 0.0)),
                            fb,
                            per_model=list(fb.get("per_model") or []),
                        )
                    )
                    added = True
                    finish_swarm_item(items[-1], st, _ANALYSIS_CACHE, conv_id, fb, canonical_model_label)
            if not added and (is_parent_active or is_swarm_worker(st) or (agent == "builtin" and db_path)):
                items.append(_make_sub_item(sub_id, agent, label, native_stub_status(st, db_path, _resolve_subagent_status), default_model, step_idx, step_ts))
        elif agent == "codex":
            c_data = analyze_codex_thread(sub_id) if not sub_id.startswith("pending-") else None
            if c_data is not None and (c_data["calls"] > 0 or is_parent_active):
                ev = c_data.get("last_task_event") or ""
                c_mtime = float(c_data.get("mtime") or 0.0)
                c_running = (
                    (ev == "task_started")
                    and not st.get("has_done_marker")
                    and (is_parent_active or (c_mtime > 0 and (time.time() - c_mtime) < 300.0))
                )
                if ev == "task_complete" or (ev == "task_started" and not c_running):
                    st["has_done_marker"] = True
                items.append(
                    _make_sub_item(
                        sub_id,
                        "codex",
                        label,
                        _resolve_subagent_status(
                            st,
                            sub_running=c_running,
                            sub_err=(ev in ("turn_aborted", "error")),
                        ),
                        c_data["model"],
                        step_idx,
                        max(step_ts, c_mtime),
                        c_data,
                    )
                )
            elif is_parent_active:
                items.append(_make_sub_item(sub_id, "codex", label, "running", default_model, step_idx, step_ts))
        elif agent == "pi":
            p_data = analyze_pi_session(sub_id) if not sub_id.startswith("pending-") else None
            if p_data is not None and (p_data["calls"] > 0 or is_parent_active):
                l_stop = p_data.get("last_stop") or ""
                p_mtime = float(p_data.get("mtime") or 0.0)
                p_running = (
                    (l_stop not in ("stop", "endTurn", "error", "aborted"))
                    and not st.get("has_done_marker")
                    and (is_parent_active or (p_mtime > 0 and (time.time() - p_mtime) < 300.0))
                )
                if l_stop in ("stop", "endTurn") or not p_running:
                    st["has_done_marker"] = True
                items.append(
                    _make_sub_item(
                        sub_id,
                        "pi",
                        label,
                        _resolve_subagent_status(
                            st,
                            sub_running=p_running,
                            sub_err=(l_stop in ("error", "aborted")),
                        ),
                        p_data["model"],
                        step_idx,
                        max(step_ts, p_mtime),
                        p_data,
                    )
                )
            elif is_parent_active:
                items.append(_make_sub_item(sub_id, "pi", label, "running", default_model, step_idx, step_ts))
        elif agent == "droid":
            d_data = analyze_droid_session(sub_id, st.get("raw_file") or "") if not sub_id.startswith("pending-") else None
            if d_data is not None and (d_data["calls"] > 0 or d_data.get("is_err") or is_parent_active):
                if not d_data.get("is_done"):
                    st["has_done_marker"] = False
                if d_data.get("is_done"):
                    st["has_done_marker"] = True
                items.append(
                    _make_sub_item(
                        sub_id,
                        "droid",
                        label,
                        _resolve_subagent_status(
                            st,
                            sub_running=not bool(d_data.get("is_done", True)),
                            sub_err=bool(d_data.get("is_err")),
                        ),
                        d_data["model"],
                        step_idx,
                        max(step_ts, float(d_data.get("mtime") or 0.0)),
                        d_data,
                    )
                )
            elif is_parent_active:
                items.append(_make_sub_item(sub_id, "droid", label, "running", default_model, step_idx, step_ts))
        elif agent == "grok":
            g_data = analyze_grok_session(sub_id, st.get("raw_file") or "") if not sub_id.startswith("pending-") else None
            if g_data is not None and (g_data["calls"] > 0 or is_parent_active):
                if not g_data.get("is_done"):
                    st["has_done_marker"] = False
                if g_data.get("is_done"):
                    st["has_done_marker"] = True
                items.append(
                    _make_sub_item(
                        sub_id,
                        "grok",
                        label,
                        _resolve_subagent_status(
                            st,
                            sub_running=not bool(g_data.get("is_done", True)),
                            sub_err=bool(g_data.get("is_err")),
                        ),
                        g_data["model"],
                        step_idx,
                        max(step_ts, float(g_data.get("mtime") or 0.0)),
                        g_data,
                    )
                )
            elif is_parent_active:
                items.append(_make_sub_item(sub_id, "grok", label, "running", default_model, step_idx, step_ts))
        elif agent == "claude":
            items.append(
                _make_sub_item(
                    sub_id,
                    "claude",
                    label,
                    _resolve_subagent_status(st),
                    default_model,
                    step_idx,
                    step_ts,
                )
            )
            items[-1]["metrics_unavailable"] = True

        for it in items[before_len:]:
            it["last_prompt_ts"] = prompt_ts
            it["last_prompt_idx"] = prompt_idx

    items.sort(
        key=lambda x: (
            x.get("last_prompt_ts", 0.0),
            x.get("last_prompt_idx", x.get("step_idx", 0)),
        ),
        reverse=True,
    )

    return summarize(items), items
