"""Annotations reader and SQLite conversation trajectory analyzer."""

from __future__ import annotations

import contextlib
import logging
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from annotation_index import AnnotationIndex
from cockpit_io import APP_DIRS, BoundedCache, cached_paths, connect_readonly, get_db_mtime, serialized
from cost_schema import ACTIVE_STEP_STATUSES, ASK_QUESTION_STEP_STATUSES, CostEvent, ask_question_step, call_uid, payload_id
from metric_items import add_model_usage
from models_board import build_models_board
from proto_wire import decode_proto, decode_varint, parse_annotation_text, parse_proto_duration
from metric_rates import db_perf, proto_split, sum_known
from pricing import (
    estimate_source,
    CTX_WINDOW_MAX,
    MODEL_MAP,
    calc_call_cost,
    calc_call_cost_breakdown,
    canonical_model_label,
    get_model_map_epoch,
    get_observed_model_ctx_limit,
    model_quota_family,
    record_model_ctx_limit,
    register_dynamic_models,
)

_LOG = logging.getLogger(__name__)


def last_successful_analysis(conv_id: str, error='') -> dict | None:
    cached = _ANALYSIS_CACHE.get(conv_id)
    if not cached or cached[1].get('empty'):
        return None
    out = dict(cached[1])
    out.update(stale=True, live_status={}, error=str(error))
    return out


UUID_PAT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
RE_INVOKE_ID = re.compile(rf'"conversationId"\s*:\s*"({UUID_PAT})"')
RE_PROTO_SUB_UUID = re.compile(
    rb"actions.{1,16}?\n\$([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\x12",
    re.DOTALL,
)

_ANNOTATIONS_INDEX = AnnotationIndex()
_ANALYSIS_CACHE = BoundedCache(128)
_SCANNED_BUILTIN_STEPS = BoundedCache(4096)
_BUILTIN_SUBAGENT_MODEL_ENUM = BoundedCache(2048)
_BUILTIN_ENUM_LOCK = threading.Lock()


def snapshot_builtin_model_enum() -> dict[str, int]:
    """Stable copy so callers can iterate or serialize while other threads update the map."""
    with _BUILTIN_ENUM_LOCK:
        return dict(_BUILTIN_SUBAGENT_MODEL_ENUM)


class AnnotationTitles(dict):
    """Legacy title mapping plus source-specific flags, without breaking unpacking."""

    def __init__(self):
        super().__init__()
        self.by_source: dict[tuple[str, str], dict] = {}


@serialized(lambda: "annotations")
def read_annotations() -> tuple[dict[str, str], dict[str, float], dict[str, int]]:
    """Read conversation titles, exact `last_user_view_time`, and `.pbtxt` nanosecond mtimes."""
    return _ANNOTATIONS_INDEX.read(APP_DIRS, cached_paths, parse_annotation_text, AnnotationTitles, get_db_mtime, connect_readonly)


RE_SCHEDULE_DURATION = re.compile(rb'"DurationSeconds"\s*:\s*(\d+)')


def _step_start_ts(meta_or_payload: bytes) -> float:
    """Extract step start timestamp (field 1) from step metadata or step_payload."""
    if not meta_or_payload:
        return 0.0
    try:
        top = {k: v for k, t, v in decode_proto(meta_or_payload) if t == "bytes" and isinstance(v, bytes)}
        for buf in (meta_or_payload, top.get(5, b"")):
            if not buf:
                continue
            m_map = {k: v for k, t, v in decode_proto(buf) if t == "bytes" and isinstance(v, bytes)}
            ts = parse_proto_duration(m_map.get(1, b""))
            if ts > 0:
                return ts
    except Exception:
        pass
    return 0.0


def _schedule_step_expired_or_ask(
    meta: bytes,
    payload: bytes,
    last_step_is_ask_question: bool,
    now_ts: float | None = None,
) -> bool:
    """Return True if a `schedule` tool step is superseded by `ask_question` or its one-shot timer expired."""
    ap = payload or b""
    if b"\x12\x08schedule" not in ap:
        return False
    if last_step_is_ask_question:
        return True
    if b'"CronExpression"' in ap:
        return False
    m = RE_SCHEDULE_DURATION.search(ap)
    if not m:
        return False
    try:
        dur_sec = int(m.group(1))
    except ValueError:
        return False
    start_ts = _step_start_ts(meta) or _step_start_ts(ap)
    cur_now = time.time() if now_ts is None else now_ts
    return bool(start_ts > 0 and (cur_now - start_ts) > (dur_sec + 60.0))


def _task_cleared_by_later_steps(task_idx: int, tool_rows: list[tuple]) -> bool:
    """Return True if a later tool step in `tool_rows` (`(idx, ..., step_payload)`) cleared `task_idx`."""
    task_tag = f"task-{task_idx}".encode("ascii")
    for r in tool_rows:
        t_idx = int(r[0])
        if t_idx <= task_idx:
            continue
        tp = r[-1] or b""
        if b"No background tasks are currently running." in tp:
            return True
        if task_tag in tp and (
            b"Killed" in tp
            or b"terminated" in tp
            or b'"Action":"kill"' in tp
            or b'"Action": "kill"' in tp
        ):
            return True
    return False


def attach_live_status(res: dict) -> dict:
    out = dict(res)
    if out.get("empty"):
        out["live_status"] = {}
        return out
    last_end_ts = float(out.get("last_end_ts") or 0.0)
    last_start_ts = float(out.get("last_start_ts") or last_end_ts)
    last_step_type = int(out.get("last_step_type") or 15)
    last_step_status = int(out.get("last_step_status") or 3)
    last_step_ts = float(out.get("last_step_ts") or last_end_ts)
    is_idle = bool(out.get("is_idle", True))
    has_pending_bg = bool(out.get("has_pending_bg", False))
    waiting_ask_question = bool(out.get("waiting_ask_question", False))
    now = time.time()
    step_stalled = False
    if last_step_status == 3 and last_step_type not in (15, 17, 23):
        if (now - last_step_ts) < 600.0:
            is_idle = False
            out["is_idle"] = False
        else:
            step_stalled = True
    idle_sec = max(0.0, now - last_end_ts) if last_end_ts > 0 else 0.0
    call_dur = max(0.0, last_end_ts - last_start_ts) if (last_end_ts > 0 and last_start_ts > 0) else 0.0
    since_start_sec = max(idle_sec, now - last_start_ts) if last_start_ts > 0 else idle_sec
    cache_age_sec = idle_sec
    rem_sec = 0.0 if step_stalled else (max(0.0, 300.0 - idle_sec) if last_end_ts > 0 else 0.0)
    out["live_status"] = {
        "is_idle": is_idle,
        "has_pending_bg": has_pending_bg,
        "waiting_ask_question": waiting_ask_question,
        "idle_sec": idle_sec,
        "call_dur": call_dur,
        "since_start_sec": since_start_sec,
        "cache_age_sec": cache_age_sec,
        "cache_rem_sec": rem_sec,
    }
    if not is_idle and not step_stalled and last_end_ts > 0:
        extra_live = max(0.0, now - last_end_ts)
        if extra_live < 1800.0:
            if "cumulative" in out and isinstance(out["cumulative"], dict):
                out["cumulative"] = dict(out["cumulative"])
                out["cumulative"]["dur"] = round(float(out["cumulative"].get("dur") or 0.0) + extra_live, 1)
            if out.get("turns"):
                new_turns = [dict(t) for t in out["turns"]]
                new_turns[0]["dur"] = round(float(new_turns[0].get("dur") or 0.0) + extra_live, 1)
                out["turns"] = new_turns
    return out


def _extract_sqlite_calls(
    cur: sqlite3.Cursor,
) -> tuple[list[int], dict[int, int], int | None, list[dict]]:
    """Shared extraction of user_steps, user_models, latest_user_model, and sorted calls (including step_type=23)."""
    cur.execute("SELECT data FROM gen_metadata ORDER BY idx")
    gen_rows = cur.fetchall()
    cur.execute("SELECT idx, metadata FROM steps WHERE step_type IN (15, 17) ORDER BY idx")
    step15_rows = cur.fetchall()
    cur.execute("SELECT idx, status, metadata, step_payload FROM steps WHERE step_type = 23 ORDER BY idx")
    step23_rows = cur.fetchall()
    cur.execute("SELECT idx, step_payload FROM steps WHERE step_type = 14 ORDER BY idx")
    step14_rows = cur.fetchall()

    user_steps: list[int] = []
    user_models: dict[int, int] = {}
    latest_user_model: int | None = None
    for u_idx, u_payload in step14_rows:
        user_steps.append(u_idx)
        if u_payload:
            p_top = {k: v for k, t, v in decode_proto(u_payload) if t == "bytes" and isinstance(v, bytes)}
            f19 = {k: v for k, t, v in decode_proto(p_top.get(19, b"")) if t == "bytes" and isinstance(v, bytes)}
            f12 = {k: v for k, t, v in decode_proto(f19.get(12, b"")) if t == "bytes" and isinstance(v, bytes)}
            f1 = {k: v for k, t, v in decode_proto(f12.get(1, b"")) if t == "varint" and isinstance(v, int)}
            m_val = f1.get(1)
            if isinstance(m_val, int) and m_val > 0:
                user_models[u_idx] = m_val
                latest_user_model = m_val

    measured_clocks = {}
    step_timings: dict[int, tuple[float, float, float, float]] = {}
    for idx, meta in step15_rows:
        if not meta:
            continue
        f_map = {k: v for k, t, v in decode_proto(meta) if t == "bytes" and isinstance(v, bytes)}
        t1 = parse_proto_duration(f_map.get(1, b""))
        t6 = parse_proto_duration(f_map.get(6, b""))
        t7 = parse_proto_duration(f_map.get(7, b""))
        measured_clocks[idx] = ":".join(f_map.get(k, b"").hex() for k in (1, 6, 7)) if t1 or t6 or t7 else ""
        if t1 > 0 and t7 >= t1:
            step_timings[idx] = (max(0.0, t6 - t1) if t6 >= t1 and t6 <= t7 else None, t7 - t1, t1, t7)
        elif t1 > 0:
            step_timings[idx] = (None, 0.0, t1, t1)

    raw_calls: list[dict] = []
    for (data,) in gen_rows:
        if not data:
            continue
        top_fields = decode_proto(data)
        step_idx: int | None = None
        for k, t, v in top_fields:
            if k == 2:
                if t == "bytes" and isinstance(v, bytes) and v:
                    step_idx, _ = decode_varint(v, 0)
                elif t == "varint" and isinstance(v, int):
                    step_idx = v
        for k, t, v in top_fields:
            if k == 1 and t == "bytes" and isinstance(v, bytes):
                f1_bytes: dict[int, bytes] = {}
                model_enum: int | None = None
                for k2, t2, v2 in decode_proto(v):
                    if t2 == "bytes" and isinstance(v2, bytes):
                        f1_bytes.setdefault(k2, v2)
                    elif k2 == 3 and t2 == "varint" and isinstance(v2, int):
                        model_enum = v2
                if model_enum and model_enum not in MODEL_MAP:
                    model_id = f1_bytes.get(19, b"").decode("utf-8", errors="ignore").strip()
                    label = f1_bytes.get(21, b"").decode("utf-8", errors="ignore").strip() or model_id
                    if label:
                        register_dynamic_models([(model_enum, label, model_id)])
                usage = {
                    k3: v3
                    for k3, t3, v3 in decode_proto(f1_bytes.get(4, b""))
                    if t3 == "varint" and isinstance(v3, int)
                }
                inp = usage.get(2, 0)
                out = usage.get(3, 0)
                cache = usage.get(5, 0)
                think, resp = proto_split(usage, out)
                ttft = parse_proto_duration(f1_bytes.get(11, b""))
                stream = parse_proto_duration(f1_bytes.get(12, b""))
                max_ctx = 0
                if f1_bytes.get(9):
                    with contextlib.suppress(ValueError):
                        for sk, st, sv in decode_proto(f1_bytes[9]):
                            if sk == 10 and st == "bytes" and isinstance(sv, bytes):
                                for ck, ct, cv in decode_proto(sv):
                                    if ck == 4 and ct == "varint" and isinstance(cv, int) and cv > 0:
                                        max_ctx = cv
                e2e_ttft, e2e_dur, start_ts, end_ts = step_timings.get(
                    step_idx if step_idx is not None else -1, (None, ttft + stream, 0.0, 0.0)
                )
                if model_enum and max_ctx > 0:
                    record_model_ctx_limit(model_enum, max_ctx, end_ts or start_ts)
                raw_calls.append({
                    "step_idx": step_idx or 0,
                    "model_enum": model_enum,
                    "in": inp,
                    "cache": cache,
                    "prompt": inp + cache,
                    "out": out,
                    "think": think,
                    "resp": resp,
                    "measured_clock": measured_clocks.get(step_idx, ""),
                    "payload_id": payload_id(data), "ttft": ttft if 11 in f1_bytes else e2e_ttft,
                    "stream": stream,
                    "is_streamed": stream > 0,
                    "e2e_ttft": e2e_ttft,
                    "e2e_dur": e2e_dur,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "is_checkpoint": False,
                    "post_ctx": inp + cache,
                    "max_ctx": max_ctx,
                })

    base_overhead = 14_000
    valid_ck_indices = [r[0] for r in step23_rows if r[1] in (3, 5)]
    for ck_idx, ck_status, _, ck_payload in step23_rows:
        if ck_status not in (3, 5):
            continue
        next_ck_idx = (
            valid_ck_indices[valid_ck_indices.index(ck_idx) + 1]
            if (ck_idx in valid_ck_indices and valid_ck_indices.index(ck_idx) + 1 < len(valid_ck_indices))
            else float("inf")
        )
        p_map = {k: v for k, t, v in decode_proto(ck_payload or b"") if t == "bytes" and isinstance(v, bytes)}
        f30 = {k: v for k, t, v in decode_proto(p_map.get(30, b"")) if t == "bytes" and isinstance(v, bytes)}
        sb = len(f30.get(5, b""))
        next_metered = [c for c in raw_calls if ck_idx < c["step_idx"] < next_ck_idx and c["prompt"] > 0]
        first_after = next_metered[0] if next_metered else None
        if first_after and first_after["prompt"] < 60_000 and sb > 0:
            est_base = first_after["prompt"] - max(1_500, sb // 4)
            if 10_000 <= est_base <= 25_000:
                base_overhead = est_base

    for ck_idx, ck_status, ck_meta, ck_payload in step23_rows:
        if ck_status in (1, 2, 8):
            continue
        m_map = {k: v for k, t, v in decode_proto(ck_meta or b"") if t == "bytes" and isinstance(v, bytes)}
        t1 = parse_proto_duration(m_map.get(1, b""))
        t8 = parse_proto_duration(m_map.get(8, b""))
        dur = t8 - t1 if 0 < t1 <= t8 else 0.0
        end_ts = t8 if t8 > 0 else t1
        f28 = {k: v for k, t, v in decode_proto(m_map.get(28, b"")) if t == "bytes" and isinstance(v, bytes)}
        usage = {
            k3: v3
            for k3, t3, v3 in decode_proto(f28.get(2, b""))
            if t3 == "varint" and isinstance(v3, int)
        }
        inp = usage.get(2, 0)
        out = usage.get(3, 0)
        cache = usage.get(5, 0)
        think, resp = proto_split(usage, out)
        p_map = {k: v for k, t, v in decode_proto(ck_payload or b"") if t == "bytes" and isinstance(v, bytes)}
        f30 = {k: v for k, t, v in decode_proto(p_map.get(30, b"")) if t == "bytes" and isinstance(v, bytes)}
        summary_bytes = len(f30.get(5, b""))
        next_ck_idx = (
            valid_ck_indices[valid_ck_indices.index(ck_idx) + 1]
            if (ck_idx in valid_ck_indices and valid_ck_indices.index(ck_idx) + 1 < len(valid_ck_indices))
            else float("inf")
        )
        next_metered = [c for c in raw_calls if ck_idx < c["step_idx"] < next_ck_idx and c["prompt"] > 0]
        first_after = next_metered[0] if next_metered else None
        is_valid_ckpt = bool(
            (ck_status == 3 and summary_bytes > 0)
            or (ck_status in (3, 5) and (inp + cache > 0 or out > 0 or summary_bytes > 0 or first_after is not None))
        )
        if not is_valid_ckpt and (inp + cache) == 0:
            continue
        if is_valid_ckpt:
            if summary_bytes > 0:
                post_ctx = base_overhead + max(1_500, summary_bytes // 4)
            elif first_after is not None and first_after["prompt"] < 60_000:
                post_ctx = first_after["prompt"]
            else:
                post_ctx = base_overhead + max(1_500, out if out > 0 else 4_000)
        else:
            post_ctx = inp + cache

        preceding_u = [u for u in user_steps if u < ck_idx]
        ck_model = user_models.get(preceding_u[-1]) if preceding_u else None
        if not ck_model:
            prec_model_candidates = [c for c in raw_calls if c["step_idx"] < ck_idx and c["model_enum"]]
            ck_model = (
                max(prec_model_candidates, key=lambda c: c["step_idx"])["model_enum"]
                if prec_model_candidates
                else latest_user_model
            )

        is_estimated_usage = False
        if inp + cache == 0:
            is_estimated_usage = True
            prec_prompt_candidates = [
                c for c in raw_calls if c["step_idx"] < ck_idx and (c.get("post_ctx") or c["prompt"]) > 0
            ]
            if prec_prompt_candidates:
                latest_prec = max(prec_prompt_candidates, key=lambda c: c["step_idx"])
                inp = latest_prec["post_ctx"] if latest_prec.get("is_checkpoint") else latest_prec["prompt"]
            else:
                inp = post_ctx

        raw_calls.append({
            "step_idx": ck_idx,
            "model_enum": ck_model,
            "in": inp,
            "cache": cache,
            "prompt": inp + cache,
            "out": out,
            "think": think,
            "resp": resp,
            "payload_id": payload_id(ck_meta) if ck_meta and t1 > 0 else "", "ttft": None,
            "stream": 0.0,
            "is_streamed": False,
            "e2e_ttft": None,
            "e2e_dur": dur,
            "start_ts": t1 if t1 > 0 else end_ts,
            "end_ts": end_ts,
            "is_checkpoint": is_valid_ckpt,
            "is_estimated": is_estimated_usage,
            "post_ctx": post_ctx,
            "max_ctx": 0,
        })

    raw_calls.sort(key=lambda c: c["step_idx"])
    for call_num, c in enumerate(raw_calls, 1):
        c["call_num"] = call_num
    return user_steps, user_models, latest_user_model, raw_calls


def _is_pro_capable_enum(enum_id: int) -> bool:
    entry = MODEL_MAP.get(enum_id)
    if not entry:
        return False
    low = f"{entry[0]} {entry[1]}".lower()
    if any(k in low for k in ("flash", "claude", "opus", "sonnet", "gpt", "glm")):
        return False
    return any(k in low for k in ("pro", "carbon", "argon", "hydrogen", "neon", "orchestrator"))


def resolve_builtin_enum_from_parent(parent_enum: int, model_hint: str) -> int:
    """Resolve `MODEL_MAP` enum for a built-in subagent (`inherit`, `pro`, `flash`, `flash_lite`)."""
    hint = (model_hint or "").strip().lower()
    if hint == "flash_lite":
        return 1050
    if hint == "flash":
        return 1322
    if hint in ("", "inherit"):
        return parent_enum if parent_enum in MODEL_MAP else 1437
    if hint == "pro":
        if _is_pro_capable_enum(parent_enum):
            return parent_enum
        return 1437
    return parent_enum if parent_enum in MODEL_MAP else 1437


def _parent_enum_at_step(
    step_idx: int,
    user_models: dict[int, int],
    calls: list[dict],
    fallback_enum: int,
) -> int:
    """Resolve the active parent model enum at or immediately before `step_idx`."""
    prec_u = [u for u in user_models if u < step_idx]
    if prec_u:
        u_enum = user_models.get(max(prec_u))
        if u_enum and u_enum in MODEL_MAP:
            return u_enum
    prec_c = [
        c["model_enum"]
        for c in calls
        if c["step_idx"] < step_idx and c.get("model_enum") in MODEL_MAP and c.get("model_enum") != 1322
    ]
    if prec_c:
        return int(prec_c[-1])
    return fallback_enum if fallback_enum in MODEL_MAP else 1437


def _scan_db_builtin_subagent_models(
    cur: sqlite3.Cursor,
    parent_cid: str,
    parent_enum: int,
    user_models: dict[int, int] | None = None,
    calls: list[dict] | None = None,
) -> None:
    """Populate `_BUILTIN_SUBAGENT_MODEL_ENUM` from any true `invoke_subagent` steps inside `cur`."""
    try:
        rows = cur.execute(
            "SELECT idx, step_type, step_payload FROM steps WHERE step_type = 127 OR (step_type = 132 AND instr(step_payload, ?) > 0) ORDER BY idx",
            (b"invoke_subagent",),
        ).fetchall()
    except Exception:
        return
    if not rows:
        return

    u_models = user_models or {}
    c_list = calls or []

    for step_idx, st_type, payload in rows:
        if not payload:
            continue
        step_cache_key = (parent_cid, int(step_idx or 0), int(st_type or 0), len(payload))
        cached_step = _SCANNED_BUILTIN_STEPS.get(step_cache_key)
        if cached_step is not None:
            if not cached_step:
                continue
            step_parent_enum = _parent_enum_at_step(int(step_idx or 0), u_models, c_list, parent_enum)
            with _BUILTIN_ENUM_LOCK:
                for sid, m_hint in cached_step:
                    _BUILTIN_SUBAGENT_MODEL_ENUM[sid] = resolve_builtin_enum_from_parent(step_parent_enum, m_hint)
            continue

        tool_name = "invoke_subagent" if st_type == 127 else ""
        args_dict: dict = {}
        texts: list[str] = []

        def _walk(buf: bytes, depth=0) -> None:
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
                        if s in ("run_command", "invoke_subagent") and not tool_name:
                            tool_name = s
                            is_leaf_text = True
                        elif (
                            s.startswith("{")
                            and ("CommandLine" in s or "TypeName" in s or "Role" in s or "Subagents" in s)
                            and not args_dict
                        ):
                            with contextlib.suppress(Exception):
                                args_dict = json.loads(s)
                            is_leaf_text = True
                        elif len(s) > 10:
                            texts.append(s)
                            if all(ord(ch) >= 32 or ch in "\t\n\r" for ch in s):
                                is_leaf_text = True
                    except Exception:
                        pass
                    if not is_leaf_text:
                        _walk(v, depth + 1)

        _walk(payload)
        if tool_name != "invoke_subagent" or "CommandLine" in args_dict:
            _SCANNED_BUILTIN_STEPS[step_cache_key] = ()
            continue
        step_parent_enum = _parent_enum_at_step(int(step_idx or 0), u_models, c_list, parent_enum)
        resolved_pairs: list[tuple[str, str]] = []
        if st_type == 127:
            specs = args_dict.get("Subagents") if isinstance(args_dict.get("Subagents"), list) else []
            sids: list[str] = []
            for k, t, v in decode_proto(payload):
                if k == 143 and t == "bytes" and isinstance(v, bytes):
                    for sk, st, sv in decode_proto(v):
                        if sk == 10 and st == "bytes" and isinstance(sv, bytes):
                            for ssk, sst, ssv in decode_proto(sv):
                                if ssk == 1 and sst == "bytes" and isinstance(ssv, bytes):
                                    sid = ssv.decode("utf-8", errors="ignore").strip()
                                    if sid:
                                        sids.append(sid)
            for i, sid in enumerate(sids):
                if sid and sid != parent_cid:
                    sp = specs[i] if i < len(specs) and isinstance(specs[i], dict) else {}
                    m_hint = str(sp.get("Model") or "")
                    resolved_pairs.append((sid, m_hint))
                    with _BUILTIN_ENUM_LOCK:
                        _BUILTIN_SUBAGENT_MODEL_ENUM[sid] = resolve_builtin_enum_from_parent(step_parent_enum, m_hint)
        else:
            combined = "\n".join(texts)
            m_id = RE_INVOKE_ID.search(combined)
            sid = m_id.group(1) if m_id else ""
            if not sid:
                m_p = RE_PROTO_SUB_UUID.search(payload)
                if m_p:
                    sid = m_p.group(1).decode("ascii")
            if sid and sid != parent_cid:
                m_hint = str(args_dict.get("Model") or "")
                resolved_pairs.append((sid, m_hint))
                with _BUILTIN_ENUM_LOCK:
                    _BUILTIN_SUBAGENT_MODEL_ENUM[sid] = resolve_builtin_enum_from_parent(step_parent_enum, m_hint)
        if resolved_pairs:
            _SCANNED_BUILTIN_STEPS[step_cache_key] = tuple(resolved_pairs)


def _iter_turn_calls(user_steps: list[int], calls: list[dict]):
    """Share the user-turn boundaries between analysis and cost extraction."""
    starts = list(user_steps)
    if calls and (not starts or calls[0]["step_idx"] < starts[0]):
        starts.insert(0, -1)
    cursor = 0
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else float("inf")
        turn_calls = []
        while cursor < len(calls) and calls[cursor]["step_idx"] < end:
            call = calls[cursor]
            if call["step_idx"] > start:
                turn_calls.append(call)
            cursor += 1
        if turn_calls:
            yield start, turn_calls


def _first_metered_turn_steps(user_steps: list[int], calls: list[dict]) -> set[int]:
    first_steps = set()
    for _, turn_calls in _iter_turn_calls(user_steps, calls):
        metered = [
            c
            for c in turn_calls
            if not c.get("is_estimated")
            and (
                ("in" not in c and "cache" not in c and "out" not in c)
                or c.get("in", 0) > 0
                or c.get("cache", 0) > 0
                or c.get("out", 0) > 0
            )
        ]
        if metered:
            first_steps.add(metered[0]["step_idx"])
    return first_steps


def extract_db_cost_events(db_path: Path, mtime: float) -> list[CostEvent]:
    """Extract metered cost/token/turn events using the same calls as analysis."""
    events: list[CostEvent] = []
    try:
        with contextlib.closing(connect_readonly(db_path, timeout=0.4)) as conn:
            cur = conn.cursor()
            cur.execute("BEGIN")
            user_steps, user_models, latest_user_model, calls = _extract_sqlite_calls(cur)
            if not calls:
                return events
            override_enum = _BUILTIN_SUBAGENT_MODEL_ENUM.get(db_path.stem)
            cur_model_enum = latest_user_model or calls[-1]["model_enum"]
            if not cur_model_enum:
                for c in reversed(calls):
                    if c["model_enum"]:
                        cur_model_enum = c["model_enum"]
                        break
            if not cur_model_enum:
                cur_model_enum = override_enum
            _scan_db_builtin_subagent_models(cur, db_path.stem, cur_model_enum or 0, user_models, calls)
        default_mname, default_short, _ = MODEL_MAP.get(
            cur_model_enum or 0, (f"Model #{cur_model_enum}", f"#{cur_model_enum}", CTX_WINDOW_MAX)
        )
        first_steps = _first_metered_turn_steps(user_steps, calls)
        for c in calls:
            if c.get("is_estimated"):
                continue
            c_enum = c["model_enum"] or override_enum or cur_model_enum
            c_mname, c_mshort, _ = MODEL_MAP.get(c_enum or 0, (default_mname, default_short, CTX_WINDOW_MAX))
            c_ts = float(c.get("end_ts") or c.get("start_ts") or mtime)
            c_cost = calc_call_cost(c_mname, c["in"], c["cache"], c["out"], event_ts=c_ts)
            if c_cost > 0:
                s_ts = float(c.get("start_ts") or c_ts)
                uid = call_uid(c, s_ts)
                events.append(CostEvent(round(c_ts, 3), round(c_cost, 8), model_quota_family(c_mname), uid,
                               int(c["in"]), int(c["cache"]), int(c["out"]), int(c["step_idx"] in first_steps),
                               0, canonical_model_label(c_mshort or c_mname)))
    except Exception:
        _LOG.exception("cost extraction failed: %s", db_path)
        raise
    return events


def get_cached_analysis_events(
    conv_id: str,
    mtime: float,
    model_enum_override: int | None = None,
) -> list[tuple] | None:
    eff_override = int(model_enum_override or _BUILTIN_SUBAGENT_MODEL_ENUM.get(conv_id) or 0)
    cur_epoch = get_model_map_epoch()
    ana = _ANALYSIS_CACHE.get(conv_id)
    if (
        ana
        and abs(ana[0][0] - mtime) < 0.001
        and len(ana[0]) >= 6
        and int(ana[0][4]) == eff_override
        and int(ana[0][5]) == cur_epoch
        and "cost_events" in ana[1]
    ):
        return ana[1]["cost_events"]
    return None


@serialized(lambda db_path_str, conv_id, *args, **kwargs: ("analysis", conv_id))
def _analyze_conversation_base(
    db_path_str: str,
    conv_id: str,
    title: str,
    source: str,
    mtime: float,
    include_subagents: bool = True,
    subagents_collector: Callable[[str], tuple[dict, list[dict]]] | None = None,
    on_cost_events: Callable[[str, float, list[CostEvent]], None] | None = None,
    model_enum_override: int | None = None,
) -> dict:
    eff_override = model_enum_override or _BUILTIN_SUBAGENT_MODEL_ENUM.get(conv_id) or 0
    cache_key = (mtime, title, db_path_str, source, eff_override, get_model_map_epoch())
    cached = _ANALYSIS_CACHE.get(conv_id)
    if cached and cached[0] == cache_key:
        res = attach_live_status(cached[1])
        if include_subagents and not res.get("empty") and subagents_collector is not None:
            sub_sum, sub_list = subagents_collector(conv_id)
            res["subagents_summary"] = sub_sum
            res["subagents"] = sub_list
            res["models_board"] = build_models_board(res.get("main_models") or [], sub_list)
        return res

    with contextlib.closing(connect_readonly(db_path_str, timeout=0.5)) as conn:
        cur = conn.cursor()
        cur.execute("BEGIN")
        user_steps, user_models, latest_user_model, calls = _extract_sqlite_calls(cur)
        _scan_db_builtin_subagent_models(cur, conv_id, eff_override or latest_user_model or 0, user_models, calls)
        cur.execute("SELECT idx, step_type, status, metadata, step_payload FROM steps WHERE status IN (1, 2, 8, 9) ORDER BY idx")
        active_step_rows = cur.fetchall()
        cur.execute("SELECT idx, step_type, status, metadata, step_payload FROM steps ORDER BY idx DESC LIMIT 1")
        last_step_row = cur.fetchone()
        last_u_idx = user_steps[-1] if user_steps else 0
        cur.execute(
            "SELECT idx, step_type, status, step_payload FROM steps WHERE idx > ? AND step_type IN (101, 127, 132) ORDER BY idx",
            (last_u_idx,),
        )
        cur_turn_tool_rows = cur.fetchall()

    if not calls:
        res = {
            "id": conv_id,
            "title": title,
            "source": source,
            "empty": True,
        }
        _ANALYSIS_CACHE.publish(conv_id, (cache_key, res), lambda entry: entry[0][0])
        return attach_live_status(res)

    turns: list[dict] = []
    turn_metered_refs: list[list[dict]] = []
    for u_idx, t_calls in _iter_turn_calls(user_steps, calls):
        t_metered = [c for c in t_calls if not c.get("is_estimated")] or t_calls
        turn_metered_refs.append(t_metered)
        t_in = sum(c["in"] for c in t_metered)
        t_cache = sum(c["cache"] for c in t_metered)
        t_prompt = t_in + t_cache
        t_out = sum(c["out"] for c in t_metered)
        t_think = sum_known(t_metered, "think")
        t_resp = sum_known(t_metered, "resp")
        perf = db_perf(t_calls)
        last_tc = t_calls[-1]
        has_ckpt = any(c["is_checkpoint"] for c in t_calls)
        t_ctx = last_tc["post_ctx"] if last_tc["is_checkpoint"] else last_tc["prompt"]
        s_vals = [c["start_ts"] for c in t_calls if c["start_ts"] > 0]
        e_vals = [c["end_ts"] for c in t_calls if c["end_ts"] > 0]
        span = (max(e_vals) - min(s_vals)) if (s_vals and e_vals and max(e_vals) >= min(s_vals)) else 0.0
        untimed_dur = sum(
            c["e2e_dur"] for c in t_calls if c["start_ts"] <= 0 or c["end_ts"] <= 0
        )
        t_dur = max(span + untimed_dur, sum(c["e2e_dur"] for c in t_calls))
        turns.append({
            "turn": len(turns) + 1,
            "user_step": u_idx,
            "calls_count": len(t_calls),
            "ctx": t_ctx,
            "compacted": has_ckpt,
            "pre_ctx": last_tc["prompt"] if last_tc["is_checkpoint"] else 0,
            "in": t_in, "cache_write": 0,
            "cache": t_cache,
            "prompt": t_prompt,
            "hit_pct": (t_cache * 100.0 / t_prompt) if t_prompt > 0 else None,
            "out": t_out,
            "think": t_think,
            "resp": t_resp,
            **perf,
            "dur": round(t_dur, 1),
            "perf_mode": "stream" if perf["tps_dur"] else "unavailable",
        })

    cur_model_enum = latest_user_model or calls[-1]["model_enum"]
    if not cur_model_enum:
        for c in reversed(calls):
            if c["model_enum"]:
                cur_model_enum = c["model_enum"]
                break
    if not cur_model_enum:
        cur_model_enum = eff_override
    model_name, model_short, default_ctx_limit = MODEL_MAP.get(
        cur_model_enum or 0, (f"Model #{cur_model_enum}", f"#{cur_model_enum}", CTX_WINDOW_MAX)
    )
    observed_model_ctx = get_observed_model_ctx_limit(cur_model_enum) if cur_model_enum else None
    fallback_ctx_limit = observed_model_ctx or default_ctx_limit
    matched_ctx = [
        int(c.get("max_ctx") or 0) for c in calls
        if int(c.get("max_ctx") or 0) > 0 and (not cur_model_enum or c.get("model_enum") == cur_model_enum)
    ]
    pending_user_model = user_models.get(user_steps[-1]) if user_steps else None
    prev_user_models = [
        user_models.get(u_idx)
        for u_idx in user_steps[:-1]
        if u_idx <= calls[-1]["step_idx"] and user_models.get(u_idx) is not None
    ]
    prev_user_model = prev_user_models[-1] if prev_user_models else None
    same_user_selection = prev_user_model is not None and pending_user_model == prev_user_model
    has_pending_switch = bool(
        user_steps
        and user_steps[-1] > calls[-1]["step_idx"]
        and pending_user_model is not None
        and not same_user_selection
        and pending_user_model != calls[-1].get("model_enum")
    )
    any_ctx = (
        [int(c.get("max_ctx") or 0) for c in calls if int(c.get("max_ctx") or 0) > 0]
        if not has_pending_switch
        else []
    )
    ctx_limit = (matched_ctx or any_ctx or [fallback_ctx_limit])[-1]

    first_steps = _first_metered_turn_steps(user_steps, calls)
    metered_calls = [c for c in calls if not c.get("is_estimated")]
    sess_in = sum(c["in"] for c in metered_calls)
    sess_cache = sum(c["cache"] for c in metered_calls)
    sess_prompt = sess_in + sess_cache
    sess_hit_pct = (sess_cache * 100.0 / sess_prompt) if sess_prompt > 0 else None
    sess_out = sum(c["out"] for c in metered_calls)
    sess_think = sum_known(metered_calls, "think")
    sess_resp = sum_known(metered_calls, "resp")

    main_model_map: dict[str, dict] = {}
    cost_events: list[CostEvent] = []
    sess_cost = 0.0
    sess_cost_prompt = 0.0
    sess_cost_cache = 0.0
    sess_cost_in = 0.0
    sess_cost_out = 0.0
    sess_cost_think = 0.0
    sess_cost_resp = 0.0
    call_costs: dict[int, float] = {}
    for c in metered_calls:
        c_enum = c["model_enum"] or eff_override or cur_model_enum
        c_mname, c_mshort, _ = MODEL_MAP.get(c_enum or 0, (model_name, model_short, CTX_WINDOW_MAX))
        c_ts = float(c.get("end_ts") or c.get("start_ts") or mtime)
        bd = calc_call_cost_breakdown(c_mname, c["in"], c["cache"], c["out"], c["think"] or 0, c["resp"] or 0, event_ts=c_ts)
        c_cost = bd["total"]
        call_costs[id(c)] = c_cost
        sess_cost += c_cost
        sess_cost_prompt += bd["prompt"]
        sess_cost_cache += bd["cache"]
        sess_cost_in += bd["in"]
        sess_cost_out += bd["out"]
        sess_cost_think += bd["think"]
        sess_cost_resp += bd["resp"]
        m_key = canonical_model_label(c_mshort or c_mname)
        if c_cost > 0:
            s_ts = float(c.get("start_ts") or c_ts)
            uid = call_uid(c, s_ts)
            cost_events.append(CostEvent(round(c_ts, 3), round(c_cost, 8), model_quota_family(c_mname), uid,
                                int(c["in"]), int(c["cache"]), int(c["out"]), int(c["step_idx"] in first_steps), 0, m_key))
        add_model_usage(main_model_map, m_key, calls=1, prompt=c["prompt"], input_tokens=c["in"],
                        cache=c["cache"], out=c["out"], cost=c_cost)
    for turn_obj, t_metered in zip(turns, turn_metered_refs):
        turn_obj["cost"] = round(sum(call_costs.get(id(c), 0.0) for c in t_metered), 6)
    main_models = list(main_model_map.values())

    perf = db_perf(calls)
    last_perf = db_perf(calls[-1:])
    last_stream_ttft, last_stream_tps = last_perf["ttft"], last_perf["tps"]

    latest_call = calls[-1]
    is_compacted_now = bool(latest_call["is_checkpoint"])
    ctx_used = latest_call["post_ctx"] if is_compacted_now else latest_call["prompt"]
    ctx_pre_compact = latest_call["prompt"] if is_compacted_now else 0
    ctx_pct = (ctx_used * 100.0 / ctx_limit) if ctx_limit > 0 else 0.0
    latest_call_hit = (latest_call["cache"] * 100.0 / latest_call["prompt"]) if latest_call["prompt"] > 0 else None

    last_end_ts = max((c["end_ts"] for c in calls if c["end_ts"] > 0), default=mtime)
    last_start_ts = float(latest_call.get("start_ts") or last_end_ts)
    last_cache_ts = max(
        (c.get("start_ts") or c["end_ts"] for c in calls if (c.get("start_ts") or c["end_ts"]) > 0),
        default=last_end_ts,
    )
    last_step_idx = last_step_row[0] if last_step_row else latest_call["step_idx"]
    last_step_type = last_step_row[1] if last_step_row else 15
    last_step_status = last_step_row[2] if last_step_row else 3
    last_step_ts = mtime
    if last_step_row and last_step_row[3]:
        lm = {k: v for k, t, v in decode_proto(last_step_row[3]) if t == "bytes" and isinstance(v, bytes)}
        lt_ts = (
            parse_proto_duration(lm.get(8, b""))
            or parse_proto_duration(lm.get(7, b""))
            or parse_proto_duration(lm.get(1, b""))
        )
        if lt_ts > 0:
            last_step_ts = lt_ts

    if (
        last_step_type == 17
        and last_step_status == 3
        and last_step_idx > latest_call["step_idx"]
        and last_step_ts > last_end_ts
    ):
        last_end_ts = last_step_ts

    last_step_is_ask_question = bool(
        last_step_row
        and last_step_type == 132
        and last_step_status in ASK_QUESTION_STEP_STATUSES
        and last_step_idx > latest_call["step_idx"]
        and (ask_question_step(last_step_row[3] or b"") or ask_question_step(last_step_row[4] or b""))
    )
    last_ckpt_idx = max((c["step_idx"] for c in calls if c.get("is_checkpoint")), default=0)
    min_valid_bg_idx = max(last_u_idx, last_ckpt_idx)
    now_ts = time.time()
    is_generating = bool(last_step_status in ACTIVE_STEP_STATUSES and not last_step_is_ask_question)
    has_pending_bg = False
    for a_idx, a_type, a_status, a_meta, a_payload in active_step_rows:
        ap = a_payload or b""
        am = a_meta or b""
        if b'"IsDaemon":true' in ap or b'"IsDaemon": true' in ap:
            continue
        if a_idx == last_step_idx and last_step_is_ask_question:
            continue
        if a_idx < last_step_idx:
            if a_type != 132 or a_status != 2 or a_idx < min_valid_bg_idx:
                continue
            if _schedule_step_expired_or_ask(am, ap, last_step_is_ask_question, now_ts):
                continue
            if _task_cleared_by_later_steps(a_idx, cur_turn_tool_rows):
                continue
            has_pending_bg = True
        elif a_type == 132 and b"\x12\x08schedule" in ap:
            if _schedule_step_expired_or_ask(am, ap, last_step_is_ask_question, now_ts):
                continue
            has_pending_bg = True
        else:
            is_generating = True

    pending_subagents: set[str] = set()
    for _, s_type, s_status, s_payload in cur_turn_tool_rows:
        sp = s_payload or b""
        if s_type in (127, 132) and s_status in (3, 5) and (s_type == 127 or b"\x12\x0finvoke_subagent" in sp):
            for m in RE_INVOKE_ID.finditer(sp.decode("utf-8", errors="ignore")):
                pending_subagents.add(m.group(1))
            for m_b in RE_PROTO_SUB_UUID.finditer(sp):
                pending_subagents.add(m_b.group(1).decode("ascii"))
        elif s_type == 101 and pending_subagents:
            txt = sp.decode("utf-8", errors="ignore")
            for sub_id in list(pending_subagents):
                if sub_id in txt:
                    pending_subagents.discard(sub_id)
    if pending_subagents:
        has_pending_bg = True

    is_idle = (not is_generating) and (not has_pending_bg)
    waiting_ask_question = bool(last_step_is_ask_question and is_idle)

    sess_dur = round(sum(t["dur"] for t in turns), 1) if turns else round(sum(c["e2e_dur"] for c in calls), 1)

    latest_turn = turns[-1] if turns else {
        "turn": 1,
        "calls_count": 1,
        "ctx": ctx_used,
        "compacted": is_compacted_now,
        "pre_ctx": ctx_pre_compact,
        "in": latest_call["in"],
        "cache": latest_call["cache"],
        "prompt": latest_call["prompt"],
        "hit_pct": latest_call_hit,
        "out": latest_call["out"],
        "think": latest_call["think"],
        "resp": latest_call["resp"],
        "ttft": last_stream_ttft,
        "tps": last_stream_tps,
        "dur": round(latest_call["e2e_dur"], 1),
        "perf_mode": "stream",
    }

    res = {
        "id": conv_id,
        "title": title,
        "source": source,
        "empty": False,
        "has_user_steps": bool(user_steps),
        "updated_at": time.strftime("%H:%M:%S"),
        "last_end_ts": last_end_ts,
        "last_start_ts": last_start_ts,
        "last_cache_ts": last_cache_ts,
        "last_step_idx": last_step_idx,
        "last_step_type": last_step_type,
        "last_step_status": last_step_status,
        "last_step_ts": last_step_ts,
        "is_idle": is_idle,
        "has_pending_bg": has_pending_bg,
        "waiting_ask_question": waiting_ask_question,
        "model": {
            "enum": cur_model_enum,
            "name": model_name,
            "short": model_short,
            "ctx_limit": ctx_limit,
        },
        "context": {
            "used": ctx_used,
            "limit": ctx_limit, "limit_default": not (matched_ctx or any_ctx or observed_model_ctx),
            "pct": ctx_pct,
            "compacted": is_compacted_now,
            "pre_compact": ctx_pre_compact,
            "is_estimated": is_compacted_now,
        },
        "cumulative": {
            "calls": len(calls),
            "streamed_calls": perf["tps_calls"], **perf,
            "turns": len(turns),
            "in": sess_in, "cache_write": 0,
            "cache": sess_cache,
            "prompt": sess_prompt,
            "hit_pct": sess_hit_pct,
            "out": sess_out,
            "think": sess_think,
            "resp": sess_resp,
            "avg_ttft": perf["ttft"],
            "dec_tps": perf["tps"],
            "dur": sess_dur,
            "cost": sess_cost, "cost_source": "unmatched_estimate" if any(estimate_source(m["model"]) == "unmatched_estimate" for m in main_models) else "estimated",
            "cost_prompt": sess_cost_prompt,
            "cost_cache": sess_cost_cache,
            "cost_in": sess_cost_in,
            "cost_out": sess_cost_out,
            "cost_think": sess_cost_think if sess_think is not None else None,
            "cost_resp": sess_cost_resp if sess_resp is not None else None,
        },
        "main_models": main_models,
        "cost_events": cost_events,
        "latest_turn": latest_turn,
        "latest_call": {
            "step_idx": latest_call["step_idx"],
            "is_checkpoint": is_compacted_now,
            "is_estimated": bool(latest_call.get("is_estimated") or is_compacted_now),
            "in": latest_call["in"],
            "cache": latest_call["cache"],
            "prompt": latest_call["prompt"],
            "hit_pct": latest_call_hit,
            "pre_compact_hit_pct": latest_call_hit,
            "out": latest_call["out"],
            "think": latest_call["think"],
            "resp": latest_call["resp"],
            "last_stream_ttft": last_stream_ttft,
            "last_stream_tps": last_stream_tps,
        },
        "turns": list(reversed(turns)),
    }
    prev_cached = _ANALYSIS_CACHE.get(conv_id)
    if prev_cached and isinstance(prev_cached[1], dict):
        for k in ("subagents_summary", "subagents", "models_board"):
            if k in prev_cached[1]:
                res[k] = prev_cached[1][k]
    if "models_board" not in res:
        res["models_board"] = build_models_board(main_models, res.get("subagents") or [])
    _ANALYSIS_CACHE.publish(conv_id, (cache_key, res), lambda entry: entry[0][0])
    if on_cost_events is not None:
        on_cost_events(conv_id, mtime, cost_events)
    out_res = attach_live_status(res)
    if include_subagents and subagents_collector is not None:
        sub_sum, sub_list = subagents_collector(conv_id)
        out_res["subagents_summary"] = sub_sum
        out_res["subagents"] = sub_list
        out_res["models_board"] = build_models_board(main_models, sub_list)
    return out_res


def analyze_conversation(db_path_str, conv_id, title, source, mtime,
                         include_subagents=True,
                         subagents_collector=None, on_cost_events=None,
                         model_enum_override=None):
    # Never hold a parent analysis lock while acquiring another conversation lock.
    result = _analyze_conversation_base(
        db_path_str, conv_id, title, source, mtime, include_subagents=False,
        on_cost_events=on_cost_events,
        model_enum_override=model_enum_override)
    if include_subagents and not result.get('empty') and subagents_collector:
        summary, items = subagents_collector(conv_id)
        result = dict(result)
        result['subagents_summary'], result['subagents'] = summary, items
        result['models_board'] = build_models_board(result.get('main_models') or [], items)
        cached = _ANALYSIS_CACHE.get(conv_id)
        if cached and isinstance(cached[1], dict):
            updated = dict(cached[1])
            updated['subagents_summary'] = summary
            updated['subagents'] = items
            updated['models_board'] = result['models_board']
            _ANALYSIS_CACHE.publish(conv_id, (cached[0], updated), lambda entry: entry[0][0])
    return result
