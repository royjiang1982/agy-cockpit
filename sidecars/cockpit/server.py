#!/usr/bin/env python3
"""AGY Cockpit UI Extension (Sidecar Web Server) — Entry Point & State Orchestrator."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import argparse
import logging
import signal
import zlib
import re
import json
import os
import socket
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SIDECAR_DIR = Path(__file__).resolve().parent
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))

from analyzer import (  # noqa: E402
    _BUILTIN_ENUM_LOCK,
    last_successful_analysis,
    _BUILTIN_SUBAGENT_MODEL_ENUM,
    analyze_conversation,
    extract_db_cost_events,
    get_cached_analysis_events,
    read_annotations,
    snapshot_builtin_model_enum,
)
from pricing import (  # noqa: E402
    PRICING_EPOCH,
    export_dynamic_models,
    export_model_ctx_limits,
    get_model_map_epoch,
    import_dynamic_models,
    import_model_ctx_limits,
    needs_reprice,
)
from rpc_quota import (  # noqa: E402
    _find_cli_bin,
    fetch_codex_quota,
    fetch_quota_buckets_with_meta,
    active_probe_count, pause_probes, resume_probes, terminate_active_probes,
)
from subagents import (  # noqa: E402
    analyze_agy_transcript_fallback,
    analyze_codex_thread,
    analyze_droid_session,
    analyze_grok_session,
    analyze_pi_session,
    collect_subagents,
)
import dispatch_api
from cockpit_io import (APP_DIRS, HOME, CODEX_SESSIONS_DIR, FACTORY_SESSIONS_DIR, GROK_SESSIONS_DIR, PI_SESSIONS_DIR,
                        SUBAGENT_APP_DIRS, atomic_write_text, cached_paths, connect_readonly, get_db_mtime)
from cost_schema import CostEvent
from theme_config import get_live_theme
from swarm_subagents import list_swarm_worker_ids
from calendar_models import calendar_item, calendar_summary as _calendar_summary
from cost_archive import CompactedRows, NormalizedCostRows
from cost_compaction import should_rescan_transcript
from cost_updates import commit_scan_updates, prepare_scan_updates, scan_droid_file, update_db
from droid_ledger import DroidLedger
from cost_runtime import CostRuntime
from cost_windows import attach_quota_costs, quota_status_fields

BASE_UI_VERSION = 29
INDEX_HTML_FILE = SIDECAR_DIR / "index.html"
WATCHED_PY_FILES = [
    SIDECAR_DIR / "server.py",
    *(SIDECAR_DIR / f"{name}.py" for name in ("cockpit_io", "proto_wire", "cost_schema", "models_board", "theme_config")),
    SIDECAR_DIR / "pricing.py",
    SIDECAR_DIR / "analyzer.py",
    SIDECAR_DIR / "subagents.py",
    SIDECAR_DIR / "subagent_labels.py",
    SIDECAR_DIR / "rpc_quota.py",
    SIDECAR_DIR / "agy_quota.py",
    *(SIDECAR_DIR / f"{name}.py" for name in ("dispatch_models", "dispatch_policy", "dispatch_api", "swarm_subagents")),
    SIDECAR_DIR.parents[1] / "skills/cockpit-dispatch/scripts/resolve.py",
    SIDECAR_DIR.parents[1] / "runtime/agent_runtime/policy_contract.py",
    SIDECAR_DIR / "droid_ledger.py",
    *(SIDECAR_DIR / f"{name}.py" for name in ("agy_cost_events", "cost_windows", "quota_sources", "grok_costs", "grok_timing", "cost_runtime", "annotation_index",
        "cost_migration", "cost_compaction", "cost_archive", "cost_updates", "calendar_models", "metric_rates", "metric_items", "metric_logs", "metric_pi", "metric_droid", "metric_codex", "metric_grok", "droid_acp_fallback")),
]

STATE_FILE = HOME / ".gemini" / "antigravity" / "sidecar_data" / "statusline" / "view_state.json"
_COST_CACHE_FILE = STATE_FILE.parent / "cost_events_cache.json"
_COST_CACHE_SCHEMA = 18
_PENDING_REBUILD: set[str] = set()

_STATE_LOCK = threading.Lock()
_COST_LOCK = threading.Lock()
_PREV_VTIMES: dict[str, float] = {}
_PREV_PBTXT_MTIMES_NS: dict[str, int] = {}
_CURRENT_ACTIVE_ID: str = ""
_PREV_MAX_VTIME: float = 0.0
_QUOTA_CACHE: dict[str, dict] = {}
_LAST_QUOTA_FETCH_TS: float = 0.0
_QUOTA_REFRESH_LOCK = threading.Lock()
_QUOTA_CACHE_TTL_SEC = 300.0
_LAST_UI_ACTIVITY_TS: float = 0.0

_COST_FILE_EVENTS: dict[str, tuple[float, list[CostEvent]]] = {}
_DB_APPLIED_BUILTIN_ENUM: dict[str, int] = {}
_DROID_LEDGER = DroidLedger()
_APPLIED_MODEL_MAP_EPOCH: int = 0
_APPLIED_PRICING_EPOCH: int = PRICING_EPOCH
_COST_CACHE_LOADED: bool = False
_CALENDAR_DAYS: dict[date, dict] = {}
_CALENDAR_CACHE: dict = {}
# Published with _QUOTA_CACHE under _STATE_LOCK.
_WINDOW_ESTIMATES = {}
_WINDOW_COSTS: dict[str, float] = {
    "gemini-5h": 0.0,
    "gemini-weekly": 0.0,
    "3p-5h": 0.0,
    "3p-weekly": 0.0,
    "codex-5h": 0.0,
    "codex-weekly": 0.0,
    "droid-5h": 0.0,
    "droid-weekly": 0.0,
    "droid-monthly": 0.0,
    "grok-weekly": 0.0,
    "5h": 0.0,
    "weekly": 0.0,
}
_LAST_COST_SCAN_TS: float = 0.0
_HTML_CACHE: tuple[tuple[int, int, bytes], int, bytes] | None = None
_HTML_LOCK = threading.RLock()
_RELOADING = False
_ACTIVE_BACKGROUND_JOBS = 0
_STOP_EVENT = threading.Event()
_LOG = logging.getLogger(__name__)
_COST_GENERATION = 0
_COST_RUNTIME = CostRuntime()
_CONVERSATION_INDEX_CACHE: tuple[float, list[dict]] | None = None
_INDEX_LOCK = threading.RLock()
_SERVER_INSTANCE: ThreadingHTTPServer | None = None
_HTTP_REQ_LOCK = threading.Lock()
_ACTIVE_HTTP_REQUESTS: int = 0


def _snapshot_py_mtimes() -> dict[str, float]:
    out: dict[str, float] = {}
    for p in WATCHED_PY_FILES:
        try:
            out[str(p)] = p.stat().st_mtime
        except Exception:
            out[str(p)] = 0.0
    return out


START_PY_MTIMES = _snapshot_py_mtimes()


@contextlib.contextmanager
def _background_job():
    global _ACTIVE_BACKGROUND_JOBS
    with _HTTP_REQ_LOCK:
        admitted = not _RELOADING and not _STOP_EVENT.is_set()
        if admitted:
            _ACTIVE_BACKGROUND_JOBS += 1
    try:
        yield admitted
    finally:
        if admitted:
            with _HTTP_REQ_LOCK:
                _ACTIVE_BACKGROUND_JOBS -= 1


def _hot_reload_if_changed() -> bool:
    """Run from the accept loop, so no new connections are accepted while draining."""
    global _RELOADING
    cur = _snapshot_py_mtimes()
    if cur == START_PY_MTIMES or _SERVER_INSTANCE is None:
        return False
    if any(time.time() - mt < 0.15 for mt in cur.values() if mt > 0):
        return False
    with _HTTP_REQ_LOCK:
        if _ACTIVE_BACKGROUND_JOBS:
            return False
    try:
        for path in WATCHED_PY_FILES:
            compile(path.read_bytes(), str(path), 'exec')
    except (OSError, SyntaxError):
        _LOG.exception('hot reload preflight failed')
        return False
    with _HTTP_REQ_LOCK:
        if _ACTIVE_BACKGROUND_JOBS:
            return False
        _RELOADING = True
    pause_probes()
    listen_fd = -1
    try:
        deadline = time.monotonic() + 0.8
        while True:
            with _HTTP_REQ_LOCK:
                busy = _ACTIVE_HTTP_REQUESTS + _ACTIVE_BACKGROUND_JOBS
            if not busy and not active_probe_count():
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        terminate_active_probes()
        listen_fd = _SERVER_INSTANCE.fileno()
        if listen_fd < 3:
            return False
        # All other descriptors opened here use Python's close-on-exec defaults.
        os.set_inheritable(listen_fd, True)
        os.environ['STATUSLINE_LISTEN_FD'] = str(listen_fd)
        os.execv(sys.executable, [sys.executable, *sys.argv])
        return True
    except Exception:
        _LOG.exception('hot reload failed; keeping current server')
        return False
    finally:
        if listen_fd >= 3:
            with contextlib.suppress(OSError):
                os.set_inheritable(listen_fd, False)
        os.environ.pop('STATUSLINE_LISTEN_FD', None)
        resume_probes()
        with _HTTP_REQ_LOCK:
            _RELOADING = False


def _ui_asset() -> tuple[int, bytes]:
    global _HTML_CACHE
    with _HTML_LOCK:
        stat = INDEX_HTML_FILE.stat()
        extra_paths = tuple(SIDECAR_DIR / name for name in (
            'dispatch_drawer.css', 'dispatch_drawer.catalog.js', 'dispatch_drawer.js',
        ))
        extra_sig = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (0, 0) for p in extra_paths)
        csrf = dispatch_api.get_csrf_token().encode('ascii')
        signature = (stat.st_mtime_ns, stat.st_size, extra_sig, csrf)
        if _HTML_CACHE is not None and _HTML_CACHE[0] == signature:
            return _HTML_CACHE[1:]
        raw = INDEX_HTML_FILE.read_bytes()
        crc = zlib.crc32(csrf, zlib.crc32(raw))
        for p in extra_paths:
            if p.exists():
                crc = zlib.crc32(p.read_bytes(), crc)
        version = BASE_UI_VERSION * (1 << 32) + crc
        body = raw.replace(b'__UI_VERSION__', str(version).encode('ascii'))
        body = body.replace(b'__CSRF_TOKEN__', csrf)
        _HTML_CACHE = (signature, version, body)
        return version, body


def _current_ui_version() -> int:
    return _ui_asset()[0]


def _render_html_page() -> bytes:
    return _ui_asset()[1]


def _load_persisted_state() -> None:
    global _PREV_VTIMES, _PREV_PBTXT_MTIMES_NS, _CURRENT_ACTIVE_ID, _PREV_MAX_VTIME, _QUOTA_CACHE, _LAST_QUOTA_FETCH_TS
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _PREV_VTIMES = {str(k): float(v) for k, v in (data.get("vtimes") or {}).items()}
            _PREV_PBTXT_MTIMES_NS = {
                str(k): int(v) for k, v in (data.get("pbtxt_mtimes_ns") or {}).items()
            }
            _CURRENT_ACTIVE_ID = str(data.get("active_id") or "")
            _PREV_MAX_VTIME = float(data.get("max_vtime") or 0.0)
            if "quota_cache" in data and isinstance(data["quota_cache"], dict):
                _QUOTA_CACHE = data["quota_cache"]
            raw_fetched = data.get("quota_fetched_at")
            if raw_fetched is not None:
                _LAST_QUOTA_FETCH_TS = float(raw_fetched or 0.0)
            elif _QUOTA_CACHE:
                _LAST_QUOTA_FETCH_TS = max(
                    (float(v.get("observed_at") or 0.0) for v in _QUOTA_CACHE.values() if isinstance(v, dict)),
                    default=0.0,
                )
            if "dynamic_models" in data:
                import_dynamic_models(data["dynamic_models"])
            if "model_ctx_limits" in data:
                import_model_ctx_limits(data["model_ctx_limits"])
    except Exception:
        pass


def _save_persisted_state() -> bool:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "active_id": _CURRENT_ACTIVE_ID,
                "max_vtime": _PREV_MAX_VTIME,
                "vtimes": _PREV_VTIMES,
                "pbtxt_mtimes_ns": _PREV_PBTXT_MTIMES_NS,
                "quota_cache": _QUOTA_CACHE,
                "quota_fetched_at": _LAST_QUOTA_FETCH_TS,
                "dynamic_models": export_dynamic_models(),
                "model_ctx_limits": export_model_ctx_limits(),
            }
        )
        atomic_write_text(STATE_FILE, payload)
        return True
    except Exception:
        _LOG.exception("cannot persist view state")
        return False




def resolve_active_and_list(limit: int = 30, preferred_id: str = "") -> tuple[str, list[dict]]:
    """Detect the focused App conversation and return it ahead of the truncated list."""
    global _PREV_VTIMES, _PREV_PBTXT_MTIMES_NS, _CURRENT_ACTIVE_ID, _PREV_MAX_VTIME
    titles, view_times, pbtxt_mtimes_ns = read_annotations()
    global _CONVERSATION_INDEX_CACHE
    with _INDEX_LOCK:
        now = time.monotonic()
        cached_index = _CONVERSATION_INDEX_CACHE
        if cached_index and now - cached_index[0] < 0.2:
            items = [dict(item) for item in cached_index[1]]
        else:
            items: list[dict] = []
            for source_label, base in APP_DIRS:
                conv_dir = base / "conversations"
                if not conv_dir.exists():
                    continue
                for db_path in cached_paths(conv_dir, "*.db", ttl=0.2):
                    cid = db_path.stem
                    try:
                        mtime = get_db_mtime(db_path)
                    except Exception:
                        continue
                    vtime = view_times.get(cid, 0.0)
                    items.append({
                        "id": cid,
                        "source": source_label,
                        "title": titles.get(cid) or cid[:8],
                        "view_time": vtime,
                        "mtime": mtime,
                        "db_path": str(db_path),
                    })
            _CONVERSATION_INDEX_CACHE = (now, [dict(item) for item in items])
    for item in items:
        base = str(Path(item['db_path']).parent.parent)
        annotation = getattr(titles, 'by_source', {}).get((base, item['id']), {})
        item['title'] = annotation.get('title') or titles.get(item['id']) or item['id'][:8]
        item['archived'] = bool(annotation.get('archived', False))
        item['view_time'] = view_times.get(item['id'], 0.0)
    # Retain explicitly requested archived sessions for pinned iframe analysis;
    # they never participate in normal listing or focus arbitration.
    preferred = next((x for x in items if x['id'] == preferred_id), None) if preferred_id else None
    swarm_workers = list_swarm_worker_ids(APP_DIRS)
    items = [x for x in items if not x['archived'] and x['id'] not in swarm_workers]

    with _STATE_LOCK:
        prev_active = _CURRENT_ACTIVE_ID
        app_items = [x for x in items if x["source"] == "App"]
        valid_ids = {x['id'] for x in app_items}
        if _CURRENT_ACTIVE_ID not in valid_ids:
            latest = max(app_items, key=lambda x: (x['view_time'], x['mtime']), default=None)
            _CURRENT_ACTIVE_ID = latest['id'] if latest else ''
        app_vtimes = {x["id"]: x["view_time"] for x in app_items if x["view_time"] > 0}
        app_pbtxt_ns = {x["id"]: int(pbtxt_mtimes_ns.get(x["id"], 0)) for x in app_items}
        snap_changed = False
        if app_vtimes:
            max_vt = max(app_vtimes.values())
            tied = [cid for cid, vt in app_vtimes.items() if (max_vt - vt) <= 0.05]
            vtimes_changed = app_vtimes != _PREV_VTIMES
            mtimes_changed = app_pbtxt_ns != _PREV_PBTXT_MTIMES_NS
            should_redecide = (
                (max_vt > _PREV_MAX_VTIME + 1e-6) or vtimes_changed or mtimes_changed
            )
            if len(tied) == 1:
                _CURRENT_ACTIVE_ID = tied[0]
            elif should_redecide and len(tied) > 1:
                # Same last_user_view_time written to A then B across polls: follow the conv whose
                # view time or .pbtxt mtime actually advanced, and leave the previously latched id.
                grown = [
                    c
                    for c in tied
                    if app_vtimes[c] > _PREV_VTIMES.get(c, 0.0) + 1e-6
                    or app_pbtxt_ns.get(c, 0) > _PREV_PBTXT_MTIMES_NS.get(c, 0)
                ]
                non_current = [c for c in grown if c != _CURRENT_ACTIVE_ID]
                if non_current:
                    _CURRENT_ACTIVE_ID = max(
                        non_current,
                        key=lambda c: (app_vtimes.get(c, 0.0), app_pbtxt_ns.get(c, 0)),
                    )
                elif _CURRENT_ACTIVE_ID not in tied:
                    _CURRENT_ACTIVE_ID = max(
                        tied,
                        key=lambda c: (app_vtimes.get(c, 0.0), app_pbtxt_ns.get(c, 0)),
                    )
            elif _CURRENT_ACTIVE_ID not in tied and tied:
                _CURRENT_ACTIVE_ID = max(
                    tied,
                    key=lambda c: (app_vtimes.get(c, 0.0), app_pbtxt_ns.get(c, 0)),
                )
            snap_changed = (max_vt != _PREV_MAX_VTIME) or vtimes_changed or mtimes_changed
            _PREV_MAX_VTIME = max_vt
            _PREV_VTIMES = dict(app_vtimes)
            _PREV_PBTXT_MTIMES_NS = dict(app_pbtxt_ns)
        else:
            snap_changed = bool(_PREV_VTIMES or _PREV_PBTXT_MTIMES_NS)
            _PREV_VTIMES, _PREV_PBTXT_MTIMES_NS, _PREV_MAX_VTIME = {}, {}, 0.0

        if snap_changed or _CURRENT_ACTIVE_ID != prev_active:
            _save_persisted_state()
        active_id = _CURRENT_ACTIVE_ID

    for x in items:
        is_active = 1 if x["id"] == active_id else 0
        is_app = 1 if (x["source"] == "App" and x["view_time"] > 0) else 0
        ts = x["view_time"] if x["view_time"] > 0 else x["mtime"]
        x["sort_key"] = (is_active, is_app, ts)

    items.sort(key=lambda x: x["sort_key"], reverse=True)
    if preferred is not None and (preferred['archived'] or preferred_id in swarm_workers):
        items.insert(0, preferred)
    elif preferred_id:
        pref_idx = next((i for i, x in enumerate(items) if x["id"] == preferred_id), -1)
        if pref_idx > 0:
            items.insert(0, items.pop(pref_idx))
    return active_id, items[:limit]


def _normalize_cost_event(item) -> CostEvent:
    """Preserve usage tails and recover token counts from legacy AGY UIDs."""
    uid = str(item[3]) if len(item) >= 4 else ""
    usage = (0, 0, 0, 0)
    if len(item) >= 8:
        usage = tuple(max(0, int(value)) for value in item[4:8])
    elif uid:
        parts = uid.split(":")
        if len(parts) == 5:
            with contextlib.suppress(ValueError, TypeError):
                int(parts[0])
                float(parts[4])
                usage = (*[max(0, int(value)) for value in parts[1:4]], 0)
    return CostEvent(round(float(item[0]), 3), round(float(item[1]), 8), str(item[2]), uid, *usage,
            int(item[8]) if len(item) > 8 else 0, str(item[9] or "").strip() if len(item) > 9 else "")


def _build_calendar(daily: dict[date, dict], now: float) -> dict:
    today = datetime.fromtimestamp(now).date()
    weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    days = []
    for offset in range(15):
        day = today - timedelta(days=offset)
        days.append({"key": day.isoformat(), "label": day.strftime("%m-%d"),
                     "sub_label": "今日" if offset == 0 else "昨日" if offset == 1 else weekdays[day.weekday()],
                     **calendar_item([daily.get(day, {})])})
    this_monday = today - timedelta(days=today.weekday())
    weeks = []
    for offset in range(15):
        monday = this_monday - timedelta(days=7 * offset)
        sunday = monday + timedelta(days=6)
        b_end = min(sunday, today)
        weeks.append({"key": f"{monday.isoformat()}~{sunday.isoformat()}",
                      "label": f"{monday.strftime('%m/%d')}–{b_end.strftime('%m/%d')}",
                      "sub_label": "本周" if offset == 0 else "上周" if offset == 1 else "",
                      **calendar_item(row for day, row in daily.items() if monday <= day <= b_end)})
    months = []
    for offset in range(12):
        year, zero_month = divmod(today.year * 12 + today.month - 1 - offset, 12)
        month = zero_month + 1
        key = f"{year:04d}-{month:02d}"
        months.append({"key": key, "label": key, "sub_label": "本月" if offset == 0 else "",
                       **calendar_item(row for day, row in daily.items()
                                       if day.year == year and day.month == month and day <= today)})
    return {name: {"period_label": label, "summary": _calendar_summary(items), "items": items}
            for name, label, items in (("day", "近15天", days), ("week", "近15周", weeks), ("month", "近1年", months))}


def _get_live_calendar(now: float | None = None) -> dict:
    global _CALENDAR_CACHE
    now = time.time() if now is None else now
    with _COST_LOCK:
        _CALENDAR_CACHE = _build_calendar(_CALENDAR_DAYS, now)
        return _CALENDAR_CACHE


def _load_cost_events_cache() -> None:
    _COST_RUNTIME.load(sys.modules[__name__])


def _save_cost_events_cache() -> None:
    _COST_RUNTIME.save(sys.modules[__name__])


def _recompute_window_costs_from_memory() -> None:
    _COST_RUNTIME.recompute(sys.modules[__name__])


def _update_conv_cost_events(
    conv_id: str,
    mtime: float,
    cost_events: list[tuple],
) -> None:
    events = NormalizedCostRows(_normalize_cost_event(item) for item in cost_events)
    update_db(sys.modules[__name__], conv_id, mtime, events)


def _refresh_window_costs(force: bool = False) -> None:
    global _LAST_COST_SCAN_TS, _COST_GENERATION, _APPLIED_MODEL_MAP_EPOCH, _APPLIED_PRICING_EPOCH
    now = time.time()
    cur_epoch = get_model_map_epoch()
    epoch_changed = cur_epoch != _APPLIED_MODEL_MAP_EPOCH
    if (
        not force
        and not epoch_changed
        and _COST_CACHE_LOADED
        and _APPLIED_PRICING_EPOCH == PRICING_EPOCH
        and (now - _LAST_COST_SCAN_TS) < 15.0
    ):
        _COST_RUNTIME.tick(sys.modules[__name__])
        return
    _LAST_COST_SCAN_TS = now

    _load_cost_events_cache()
    with _COST_LOCK:
        pricing_changed = _APPLIED_PRICING_EPOCH != PRICING_EPOCH
        if epoch_changed:
            stale_keys = [
                k for k, (_, ev) in _COST_FILE_EVENTS.items()
                if k.startswith("db:") and any(len(it) >= 3 and it[2] == "other" for it in ev)
            ]
            _COST_GENERATION += bool(set(stale_keys) - _PENDING_REBUILD)
            _PENDING_REBUILD.update(stale_keys)
            _APPLIED_MODEL_MAP_EPOCH = cur_epoch
        events_snap = dict(_COST_FILE_EVENTS)
        reusable = {k: v for k, v in events_snap.items() if k not in _PENDING_REBUILD}
        applied_snap = dict(_DB_APPLIED_BUILTIN_ENUM)
    enum_snap = snapshot_builtin_model_enum()
    calendar_cutoff = now - 370 * 86400.0
    reprice_since = calendar_cutoff if pricing_changed else None
    updates: dict[str, tuple[float, list | dict]] = {}
    applied_updates: dict[str, int] = {}

    db_by_cid: dict[str, tuple[float, Path]] = {}
    for _, base in SUBAGENT_APP_DIRS:
        cdir = base / "conversations"
        if not cdir.exists():
            continue
        try:
            for p in cached_paths(cdir, "*.db"):
                try:
                    mt = get_db_mtime(p)
                except Exception:
                    continue
                if mt < calendar_cutoff:
                    continue
                cid = p.stem
                prev_db = db_by_cid.get(cid)
                if prev_db is None or mt > prev_db[0]:
                    db_by_cid[cid] = (mt, p)
        except Exception:
            pass

    for cid, (mt, p) in db_by_cid.items():
        try:
            key = f"db:{cid}"
            cur_b_enum = int(enum_snap.get(cid) or 0)
            cached = reusable.get(key)
            if (
                cached
                and abs(cached[0] - mt) < 0.001
                and applied_snap.get(cid, 0) == cur_b_enum
                and not needs_reprice(cached, mt, reprice_since)
            ):
                continue
            ana_ev = None if needs_reprice(cached, mt, reprice_since) else get_cached_analysis_events(cid, mt, cur_b_enum)
            ev = ana_ev if ana_ev is not None else extract_db_cost_events(p, mt)
            updates[key] = (mt, ev)
            with _BUILTIN_ENUM_LOCK:
                applied_updates[cid] = int(_BUILTIN_SUBAGENT_MODEL_ENUM.get(cid) or 0)
        except Exception:
            continue

    enum_after = snapshot_builtin_model_enum()
    for child_cid, new_enum in enum_after.items():
        applied_now = applied_updates.get(child_cid, applied_snap.get(child_cid, 0))
        if child_cid in db_by_cid and applied_now != int(new_enum):
            try:
                c_mt, c_p = db_by_cid[child_cid]
                ev = extract_db_cost_events(c_p, c_mt)
                updates[f"db:{child_cid}"] = (c_mt, ev)
                applied_updates[child_cid] = int(new_enum)
            except Exception:
                pass

    for _, base in SUBAGENT_APP_DIRS:
        bdir = base / "brain"
        if not bdir.exists():
            continue
        try:
            for sub_dir in bdir.iterdir():
                if not sub_dir.is_dir():
                    continue
                cid = sub_dir.name
                tf = sub_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
                t = sub_dir / ".system_generated" / "logs" / "transcript.jsonl"
                src = tf if tf.exists() else (t if t.exists() else None)
                if src is None:
                    continue
                try:
                    mt = src.stat().st_mtime
                except Exception:
                    continue
                if mt < calendar_cutoff:
                    continue
                key = f"agy_tr:{cid}"
                cached = reusable.get(key)
                pair_reopened = should_rescan_transcript(key, cached, reusable, updates)
                if cached and abs(cached[0] - mt) < 0.001 and not needs_reprice(cached, mt, reprice_since) and not pair_reopened:
                    continue
                fb = analyze_agy_transcript_fallback(src)
                if fb is None and (key in _COST_RUNTIME.damaged or (cached and isinstance(cached[1], CompactedRows))):
                    continue  # Retain the exact archived fallback if the source cannot be read.
                updates[key] = (mt, list((fb or {}).get("cost_events") or []))
        except Exception:
            pass

    if CODEX_SESSIONS_DIR.exists():
        try:
            for p in cached_paths(CODEX_SESSIONS_DIR, "*.jsonl", recursive=True):
                try:
                    mt = p.stat().st_mtime
                    if mt < calendar_cutoff:
                        continue
                    key = f"codex:{p}"
                    cached = reusable.get(key)
                    if cached and abs(cached[0] - mt) < 0.001 and not needs_reprice(cached, mt, reprice_since):
                        continue
                    m_res = analyze_codex_thread(p)
                    if m_res is None:
                        continue
                    updates[key] = (mt, list(m_res.get("cost_events") or []))
                except Exception:
                    continue
        except Exception:
            pass

    if PI_SESSIONS_DIR.exists():
        try:
            for p in cached_paths(PI_SESSIONS_DIR, "*.jsonl", recursive=True):
                try:
                    mt = p.stat().st_mtime
                    if mt < calendar_cutoff:
                        continue
                    key = f"pi:{p}"
                    cached = reusable.get(key)
                    if cached and abs(cached[0] - mt) < 0.001 and not needs_reprice(cached, mt, reprice_since):
                        continue
                    m_res = analyze_pi_session(p)
                    if m_res is None:
                        continue
                    updates[key] = (mt, list(m_res.get("cost_events") or []))
                except Exception:
                    continue
        except Exception:
            pass

    if FACTORY_SESSIONS_DIR.exists():
        try:
            for p in cached_paths(FACTORY_SESSIONS_DIR, "*.settings.json", recursive=True):
                try:
                    if scanned := scan_droid_file(sys.modules[__name__], p, reusable, events_snap, calendar_cutoff):
                        updates[scanned[0]] = scanned[1]
                except Exception:
                    continue
        except Exception:
            pass

    if GROK_SESSIONS_DIR.exists():
        try:
            for p in cached_paths(GROK_SESSIONS_DIR, "*/*/updates.jsonl"):
                try:
                    sdir = p.parent
                    mt = p.stat().st_mtime
                    sum_p = sdir / "summary.json"
                    if sum_p.exists():
                        mt = max(mt, sum_p.stat().st_mtime)
                    usage_p = sdir / "usage.json"
                    if usage_p.exists():
                        mt = max(mt, usage_p.stat().st_mtime)
                    if mt < calendar_cutoff:
                        continue
                    key = f"grok:{sdir}"
                    cached = reusable.get(key)
                    if cached and abs(cached[0] - mt) < 0.001 and not needs_reprice(cached, mt, reprice_since):
                        continue
                    m_res = analyze_grok_session(sdir)
                    if m_res is None:
                        continue
                    updates[key] = (mt, list(m_res.get("cost_events") or []))
                except Exception:
                    continue
        except Exception:
            pass

    prepared = prepare_scan_updates(sys.modules[__name__], updates, events_snap)
    with _COST_LOCK:
        dirty, committed = commit_scan_updates(sys.modules[__name__], prepared)
        for cid, new_enum in applied_updates.items():
            if f"db:{cid}" not in committed:
                continue
            if _DB_APPLIED_BUILTIN_ENUM.get(cid) != int(new_enum):
                _DB_APPLIED_BUILTIN_ENUM[cid] = int(new_enum)
                dirty = True
        for k in list(_COST_FILE_EVENTS.keys()):
            if _COST_FILE_EVENTS[k][0] < calendar_cutoff and k not in _COST_RUNTIME.damaged:
                _COST_FILE_EVENTS.pop(k, None)
                _PENDING_REBUILD.discard(k)
                dirty = True
        if dirty or pricing_changed:
            _COST_GENERATION += 1
        if pricing_changed:
            _APPLIED_PRICING_EPOCH = PRICING_EPOCH
    _COST_RUNTIME.compact(sys.modules[__name__], now)
    _COST_RUNTIME.tick(sys.modules[__name__], force=force)


def _touch_ui_activity() -> None:
    global _LAST_UI_ACTIVITY_TS
    with _STATE_LOCK:
        _LAST_UI_ACTIVITY_TS = time.time()


def _quota_cache_fresh(now: float | None = None) -> bool:
    eff_now = time.time() if now is None else float(now)
    with _STATE_LOCK:
        has_data = any(
            isinstance(v, dict) and v.get("rem_frac") is not None
            for v in _QUOTA_CACHE.values()
        )
        fetched_at = _LAST_QUOTA_FETCH_TS
    return has_data and fetched_at > 0.0 and (eff_now - fetched_at) < _QUOTA_CACHE_TTL_SEC


def _refresh_quota(
    force_refresh: bool = False,
    agy_force_refresh: bool | None = False,
    *,
    only_if_stale: bool = False,
) -> dict:
    with _QUOTA_REFRESH_LOCK:
        now = time.time()
        if only_if_stale and _quota_cache_fresh(now):
            return {"reason": "cached", "probed": False, "ok": True, "error": "", "refreshed": False}
        with _STATE_LOCK:
            seed_snap = dict(_QUOTA_CACHE)
            ui_active = (now - _LAST_UI_ACTIVITY_TS) <= 30.0
        parsed, agy_meta = fetch_quota_buckets_with_meta(
            force_refresh=force_refresh,
            seed_cache=seed_snap,
            agy_force_refresh=agy_force_refresh,
            ui_active=ui_active,
        )
        has_bucket_data = any(
            isinstance(v, dict) and v.get("rem_frac") is not None
            for v in parsed.values()
        )
        agy_ok = bool(agy_meta.get("ok", True)) or agy_meta.get("reason") == "not_installed"
        if parsed:
            _COST_RUNTIME.refresh_quota(sys.modules[__name__], parsed, mark_fresh=agy_ok and has_bucket_data)
            with _STATE_LOCK:
                _save_persisted_state()
        return {**agy_meta, "refreshed": True}


def _get_live_quota_snapshot() -> tuple[dict[str, dict], float]:
    out: dict[str, dict] = {}
    with _STATE_LOCK:
        snap = dict(_QUOTA_CACHE)
        window_costs, estimates = dict(_WINDOW_COSTS), dict(_WINDOW_ESTIMATES)
        fetched_at = _LAST_QUOTA_FETCH_TS
    now = time.time()
    has_codex = _find_cli_bin("codex") is not None
    has_droid = _find_cli_bin("droid") is not None
    has_grok = _find_cli_bin("grok") is not None
    for bid, info in snap.items():
        if not isinstance(info, dict):
            continue
        if bid.startswith("codex-") and not has_codex:
            continue
        if bid.startswith("droid-") and not has_droid:
            continue
        if bid.startswith("grok-") and not has_grok:
            continue
        if info.get("rem_frac") is None:
            continue
        if bid == "grok-5h":
            continue
        out[bid] = quota_status_fields(bid, info, now, estimates.get(bid))
    active_bids = ["gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly"]
    if has_codex:
        out["codex-installed"] = {"installed": True}
        active_bids.extend(["codex-5h", "codex-weekly"])
    if has_droid:
        out["droid-installed"] = {"installed": True}
        active_bids.extend(["droid-5h", "droid-weekly", "droid-monthly"])
    if has_grok:
        out["grok-installed"] = {"installed": True}
        active_bids.append("grok-weekly")
    return attach_quota_costs(out, active_bids, window_costs), fetched_at


def _analyze_with_hooks(
    db_path_str: str,
    conv_id: str,
    title: str,
    source: str,
    mtime: float,
    include_subagents: bool = True,
) -> dict:
    try:
        return analyze_conversation(
            db_path_str,
            conv_id,
            title,
            source,
            mtime,
            include_subagents=include_subagents,
            subagents_collector=(
                lambda cid: collect_subagents(cid, on_cost_events=_update_conv_cost_events)
            )
            if include_subagents
            else None,
            on_cost_events=_update_conv_cost_events,
        )
    except Exception as exc:
        cached = last_successful_analysis(conv_id, str(exc))
        if cached is not None:
            return cached
        raise


def _preheat_recent_app_conversations() -> None:
    """Warm the in-memory analysis cache for the conversations most likely to be switched to."""
    try:
        _, convs = resolve_active_and_list(limit=30)
    except Exception:
        return
    recent = [c for c in convs if c.get("source") == "App"]
    recent.sort(key=lambda c: float(c.get("view_time") or c.get("mtime") or 0.0), reverse=True)
    for target in recent[:8]:
        try:
            _analyze_with_hooks(
                target["db_path"],
                target["id"],
                target["title"],
                target["source"],
                target["mtime"],
                include_subagents=True,
            )
        except Exception:
            continue


def _quota_and_cost_worker() -> None:
    """Refresh window costs and recently viewed session metrics (quota is refreshed on drawer open/click only)."""
    tick = 0
    while not _STOP_EVENT.is_set():
        _STOP_EVENT.wait(0.15)
        tick += 1
        with _background_job() as admitted:
            if not admitted:
                continue
            try:
                _refresh_window_costs()
                if tick == 1 or tick % 67 == 0:
                    _preheat_recent_app_conversations()
            except Exception:
                _LOG.exception("quota/cost worker failed")


class StatuslineHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def _respond(self, status, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_HEAD(self):
        self._dispatch()

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        global _ACTIVE_HTTP_REQUESTS
        with _HTTP_REQ_LOCK:
            _ACTIVE_HTTP_REQUESTS += 1
            reloading = _RELOADING
        try:
            if reloading:
                self._respond(503, {'ok': False, 'state': 'reloading', 'msg': '服务重载中'})
                return
            host = None
            with contextlib.suppress(ValueError):
                host = urlparse('http://' + self.headers.get('Host', '')).hostname
            if host not in ('localhost', '127.0.0.1', '::1'):
                self._respond(403, {'ok': False, 'state': 'invalid_host'})
                return
            origin = self.headers.get('Origin')
            if origin and origin != 'null' and not (self.command == 'POST' and self.path.startswith('/api/dispatch-')):
                parsed_origin = urlparse(origin)
                local = parsed_origin.scheme in ('http', 'https') and parsed_origin.hostname in ('localhost', '127.0.0.1', '::1')
                plugin = parsed_origin.scheme == 'plugin' and bool(re.fullmatch(r'[a-zA-Z0-9_.-]+', parsed_origin.netloc or ''))
                if not (local or plugin):
                    self._respond(403, {'ok': False, 'state': 'invalid_origin'})
                    return
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as exc:
            _LOG.exception('HTTP request failed')
            self._respond(500, {'ok': False, 'state': 'error', 'error': str(exc)})
        finally:
            with _HTTP_REQ_LOCK:
                _ACTIVE_HTTP_REQUESTS -= 1

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        try:
            qs = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
            if len(qs.get('id', [])) > 1 or len((qs.get('id') or [''])[0]) > 128:
                raise ValueError('invalid conversation id')
        except ValueError:
            self._respond(400, {'ok': False, 'state': 'invalid_parameters'})
            return
        if parsed.path.startswith(('/api/dispatch-', '/static/dispatch_drawer.')):
            dispatch_api.handle_dispatch_request(self, self.path.split('?', 1)[0], qs, sys.modules[__name__])
            return
        if parsed.path == '/api/config':
            if self.command not in ('GET', 'HEAD'):
                self._respond(405, {'ok': False, 'state': 'method_not_allowed'})
                return
            self._respond(200, {'ok': True, 'service': 'agy-cockpit'})
            return

        if parsed.path == '/api/quota/refresh':
            if self.command != 'POST':
                self._respond(405, {'ok': False, 'state': 'method_not_allowed'})
                return
            _touch_ui_activity()
            mode = (qs.get('mode') or ['manual'])[0].strip().lower()
            only_if_stale = mode in ('open', 'auto')
            with _background_job() as admitted:
                if not admitted:
                    self._respond(503, {'ok': False, 'state': 'reloading', 'msg': '服务重载中'})
                    return
                agy_meta = _refresh_quota(
                    force_refresh=True,
                    agy_force_refresh=True,
                    only_if_stale=only_if_stale,
                )
            quota, fetched_at = _get_live_quota_snapshot()
            self._respond(200, {
                'ok': bool(agy_meta.get('ok', True)),
                'refreshed': bool(agy_meta.get('refreshed', False)),
                'error': str(agy_meta.get('error') or ''),
                'reason': str(agy_meta.get('reason') or ''),
                'quota_fetched_at': fetched_at,
                'quota_cache_ttl_sec': _QUOTA_CACHE_TTL_SEC,
                'quota': quota,
            })
            return

        if parsed.path == "/api/status":
            if self.command not in ("GET", "HEAD"):
                self._respond(405, {"ok": False, "state": "method_not_allowed"})
                return
            _touch_ui_activity()
            req_id = (qs.get("id") or [""])[0].strip()
            active_id, convs = resolve_active_and_list(limit=30, preferred_id=req_id)
            target = None
            if req_id:
                for c in convs:
                    if c["id"] == req_id:
                        target = c
                        break
            if not req_id and target is None and active_id:
                for c in convs:
                    if c["id"] == active_id:
                        target = c
                        break
            if not req_id and target is None and convs:
                target = convs[0]

            active = None
            if req_id and target is None:
                titles, _, _ = read_annotations()
                active = {"id": req_id, "title": titles.get(req_id) or "新会话", "source": "App",
                          "empty": True, "live_status": {}}
            if target:
                try:
                    active = _analyze_with_hooks(
                        target["db_path"],
                        target["id"],
                        target["title"],
                        target["source"],
                        target["mtime"],
                        include_subagents=True,
                    )
                except Exception as exc:
                    active = last_successful_analysis(target["id"], str(exc)) or {"id": target["id"], "title": target["title"], "source": target["source"], "empty": True, "live_status": {}, "stale": True, "error": str(exc)}
            if active is not None:
                if active.get("empty"):
                    active = {**active, "live_status": {}}
                active = {key: value for key, value in active.items() if key != "cost_events"}
                active['archived'] = bool(target and target.get('archived'))

            quota, fetched_at = _get_live_quota_snapshot()
            self._respond(200, {
                "ui_version": _current_ui_version(),
                "theme": get_live_theme(),
                "quota": quota,
                "quota_fetched_at": fetched_at,
                "quota_cache_ttl_sec": _QUOTA_CACHE_TTL_SEC,
                "calendar": _get_live_calendar(),
                "conversations": [
                    {"id": c["id"], "title": c["title"], "source": c["source"], "mtime": c["mtime"],
                     "archived": bool(c.get('archived'))}
                    for c in convs
                ],
                "active": active,
            })
            return

        if parsed.path.startswith('/api/'):
            self._respond(404, {'ok': False, 'state': 'not_found'})
            return
        if parsed.path != '/':
            self._respond(404, {'ok': False, 'state': 'not_found'})
            return
        if self.command not in ('GET', 'HEAD'):
            self._respond(405, {'ok': False, 'state': 'method_not_allowed'})
            return
        _touch_ui_activity()
        self._respond(200, _render_html_page(), 'text/html; charset=utf-8')


class CockpitHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(64)
        super().__init__(*args, **kwargs)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10.0)
        return sock, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def service_actions(self):
        if not _STOP_EVENT.is_set():
            _hot_reload_if_changed()


def _parse_port(argv=None):
    parser = argparse.ArgumentParser(description='AGY Cockpit sidecar')
    parser.add_argument('--port', type=int)
    args = parser.parse_args(argv)
    value = args.port
    if value is None:
        try:
            value = int(os.environ.get('ANTIGRAVITY_SIDECAR_WEB_PORT') or os.environ.get('PORT') or 18765)
        except ValueError:
            parser.error('port must be an integer')
    if not 1 <= value <= 65535:
        parser.error('port must be between 1 and 65535')
    return value


def _create_server(port):
    inherited = os.environ.pop('STATUSLINE_LISTEN_FD', '')
    fd = int(inherited) if inherited.isdigit() else -1
    if fd >= 0:
        srv = None
        inherited_socket = None
        try:
            inherited_socket = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
            if inherited_socket.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
                raise ValueError('inherited FD is not a stream socket')
            try:
                if not inherited_socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                    raise ValueError('inherited FD is not listening')
            except OSError as exc:
                if exc.errno not in (errno.ENOPROTOOPT, errno.EOPNOTSUPP):
                    raise
                inherited_socket.listen()
            address = inherited_socket.getsockname()
            if address[0] not in ('127.0.0.1', '::1') or address[1] != port:
                raise ValueError('inherited FD address mismatch')
            srv = CockpitHTTPServer(address, StatuslineHandler, bind_and_activate=False)
            srv.socket.close()
            srv.socket = inherited_socket
            srv.server_address = address
            os.set_inheritable(srv.fileno(), False)
            return srv
        except Exception:
            if inherited_socket is not None:
                inherited_socket.close()
            if srv is not None:
                srv.server_close()
            _LOG.warning('invalid inherited listen FD; binding again', exc_info=True)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
    return CockpitHTTPServer(('127.0.0.1', port), StatuslineHandler)


def main() -> None:
    global _SERVER_INSTANCE
    logging.basicConfig(level=logging.INFO)
    port = _parse_port()
    _load_persisted_state()
    _load_cost_events_cache()
    _recompute_window_costs_from_memory()
    _STOP_EVENT.clear()
    resume_probes()
    server = _create_server(port)
    _SERVER_INSTANCE = server
    workers = [threading.Thread(target=target, daemon=True) for target in (_quota_and_cost_worker,)]

    def stop(signum, frame):
        _STOP_EVENT.set()
        pause_probes()
        terminate_active_probes()
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for worker in workers:
            worker.start()
        print(f'AGY Cockpit UI Extension listening on http://127.0.0.1:{port}', flush=True)
        server.serve_forever(poll_interval=0.15)
    finally:
        _STOP_EVENT.set()
        pause_probes()
        terminate_active_probes()
        server.server_close()
        for worker in workers:
            worker.join(timeout=1.0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        _SERVER_INSTANCE = None


if __name__ == '__main__':
    main()
