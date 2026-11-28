"""Collect subagent metrics, with compatibility exports for discovery and readers."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from analyzer import (
    _ANALYSIS_CACHE,
    _BUILTIN_ENUM_LOCK,
    _BUILTIN_SUBAGENT_MODEL_ENUM,
    analyze_conversation,
    resolve_builtin_enum_from_parent,
)
from cockpit_io import SUBAGENT_APP_DIRS, get_db_mtime
from cost_schema import CostEvent
from metric_agy import analyze_agy_transcript_fallback
from metric_items import make_sub_item as _make_sub_item, summarize
from metric_session import analyze_codex_thread, analyze_droid_session, analyze_grok_session, analyze_pi_session
from pricing import MODEL_MAP, canonical_model_label, short_model_hint
from subagent_commands import (RE_SA_RUN, RE_AGY_RESUME, RE_CODEX_RESUME, RE_DROID_RESUME,
                               RE_GROK_RESUME, RE_PI_RESUME)
from subagent_discovery import discover_subagent_stubs
from subagent_payload import _parse_step132_payload
from swarm_subagents import finish_swarm_item, is_swarm_worker, native_stub_status, native_worker_status


def _resolve_builtin_model_enum(parent_conv_id: str, model_hint: str) -> int:
    """Resolve the true `MODEL_MAP` enum for a built-in subagent (`inherit`, `pro`, `flash`, `flash_lite`)."""
    parent_cached = _ANALYSIS_CACHE.get(parent_conv_id)
    parent_enum = int((parent_cached[1].get('model') or {}).get('enum') or 0) if parent_cached else 0
    return resolve_builtin_enum_from_parent(parent_enum, model_hint)


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
