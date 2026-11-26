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
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SIDECAR_DIR = Path(__file__).resolve().parent
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))

from analyzer import (  # noqa: E402
    _BUILTIN_ENUM_LOCK,
    CostEvent, cached_paths, last_successful_analysis,
    _BUILTIN_SUBAGENT_MODEL_ENUM,
    APP_DIRS,
    HOME,
    analyze_conversation,
    extract_db_cost_events,
    get_cached_analysis_events,
    get_db_mtime,
    connect_readonly,
    read_annotations,
    read_conversation_annotation,
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
    get_live_theme,
    sync_ls_model_configs,
    extract_handoff_metadata, find_native_cascade_endpoint, get_native_cascade_summaries,
    start_native_cascade, send_native_cascade_message, update_conversation_annotations,
    inspect_cascade_idle, NativeRPCError,
    active_probe_count, pause_probes, resume_probes, terminate_active_probes,
)
from subagents import (  # noqa: E402
    SUBAGENT_APP_DIRS,
    CODEX_SESSIONS_DIR,
    FACTORY_SESSIONS_DIR,
    GROK_SESSIONS_DIR,
    PI_SESSIONS_DIR,
    analyze_agy_transcript_fallback,
    analyze_codex_thread,
    analyze_droid_session,
    analyze_grok_session,
    analyze_pi_session,
    collect_subagents,
)
import dispatch_api
from swarm_subagents import list_swarm_worker_ids
from calendar_models import calendar_item, calendar_summary as _calendar_summary
from cost_archive import CompactedRows
from cost_compaction import should_rescan_transcript
from cost_updates import commit_scan_updates, prepare_scan_updates, scan_droid_file, update_db
from droid_ledger import DroidLedger
from cost_runtime import CostRuntime, NormalizedCostRows
from cost_windows import attach_quota_costs, merge_quota_cache, quota_status_fields

BASE_UI_VERSION = 29
INDEX_HTML_FILE = SIDECAR_DIR / "index.html"
WATCHED_PY_FILES = [
    SIDECAR_DIR / "server.py",
    SIDECAR_DIR / "pricing.py",
    SIDECAR_DIR / "analyzer.py",
    SIDECAR_DIR / "subagents.py",
    SIDECAR_DIR / "rpc_quota.py",
    SIDECAR_DIR / "agy_quota.py",
    *(SIDECAR_DIR / f"{name}.py" for name in ("dispatch_policy", "dispatch_api", "swarm_subagents", "test_dispatch_policy", "test_dispatch_api", "test_swarm_subagents")),
    SIDECAR_DIR.parents[1] / "skills/cockpit-dispatch/scripts/resolve.py",
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
_HANDOFF_FOCUS: tuple[str, float, frozenset[str]] | None = None


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
    # Use the same lock order at both checks. Submissions register background
    # activity before endpoint discovery; registered-but-not-started workers also
    # count as active. A long handoff must not repeatedly close the HTTP gate.
    with _HANDOFF_CONTROLLER.lock:
        with _HTTP_REQ_LOCK:
            if _ACTIVE_BACKGROUND_JOBS or _HANDOFF_CONTROLLER.has_active_threads():
                return False
    try:
        for path in WATCHED_PY_FILES:
            compile(path.read_bytes(), str(path), 'exec')
    except (OSError, SyntaxError):
        _LOG.exception('hot reload preflight failed')
        return False
    with _HANDOFF_CONTROLLER.lock:
        with _HTTP_REQ_LOCK:
            if _ACTIVE_BACKGROUND_JOBS or _HANDOFF_CONTROLLER.has_active_threads():
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
        extra_paths = (SIDECAR_DIR / 'dispatch_drawer.css', SIDECAR_DIR / 'dispatch_drawer.js')
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
        tmp_file = STATE_FILE.with_name(f".{STATE_FILE.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        tmp_file.write_text(payload, encoding="utf-8")
        os.replace(tmp_file, STATE_FILE)
        return True
    except Exception:
        _LOG.exception("cannot persist view state")
        return False




def resolve_active_and_list(limit: int = 30, preferred_id: str = "") -> tuple[str, list[dict]]:
    """Detect the focused App conversation and return it ahead of the truncated list."""
    global _PREV_VTIMES, _PREV_PBTXT_MTIMES_NS, _CURRENT_ACTIVE_ID, _PREV_MAX_VTIME, _HANDOFF_FOCUS
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

        if _HANDOFF_FOCUS:
            focus_id, baseline, excluded = _HANDOFF_FOCUS
            other_views = (stamp for cid, stamp in app_vtimes.items() if cid not in excluded)
            if focus_id in valid_ids and max(other_views, default=0.0) <= baseline:
                _CURRENT_ACTIVE_ID = focus_id
            else:
                _HANDOFF_FOCUS = None

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


class HandoffController:
    """Durable, per-old-id jobs. Only this resident process may finalize archives."""

    TERMINAL = {'done', 'failed', 'unknown', 'needs_manual_archive'}
    INITIALIZING = {'creating', 'created', 'sending', 'waiting_new_reply'}

    def __init__(self, poll_interval=1.0, timeout=600.0):
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.preparing: set[str] = set()
        self.loaded = False
        self.poll_interval, self.timeout = poll_interval, timeout

    def _load(self):
        if self.loaded:
            return
        path = STATE_FILE.parent / 'handoff_jobs.json'
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf-8'))
            if raw.get('schema_version') != 1 or not isinstance(raw.get('jobs'), dict):
                raise ValueError('无法识别 handoff 作业文件')
            self.jobs = raw['jobs']
            for job in self.jobs.values():
                if job['state'] not in self.TERMINAL:
                    # A lost Send/Start response has no server idempotency key.
                    # Restart never resends or archives based on stale evidence.
                    if job.get('seed_verified'):
                        job.update(state='needs_manual_archive', error='Sidecar 已重启，请核对新会话；未重发或自动归档')
                    else:
                        job.update(state='unknown', error='Sidecar 已重启，交接正文送达未确认；请核对新会话')
        self.loaded = True

    def _save(self):
        directory = STATE_FILE.parent
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'handoff_jobs.json'
        temporary = directory / 'handoff_jobs.json.tmp'
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump({'schema_version': 1, 'jobs': self.jobs}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _transition(self, job_id, state, **fields):
        with self.lock:
            self.jobs[job_id].update(state=state, updated_at=time.time(), **fields)
            self._save()
            with (STATE_FILE.parent / 'handoff.log').open('a', encoding='utf-8') as stream:
                # No summary, CSRF credential or raw configuration is logged.
                stream.write(json.dumps({'job_id': job_id, 'state': state, **fields},
                                        ensure_ascii=False) + '\n')

    def _latest(self, field, identifier):
        matches = [job for job in reversed(self.jobs.values()) if job[field] == identifier]
        return max(matches, key=lambda j: (j.get('created_at', 0), j.get('updated_at', 0)),
                   default=None)

    def get(self, identifier, *, for_status=False):
        with self.lock:
            self._load()
            job = self.jobs.get(identifier) or self._latest('old_id', identifier)
            if job is None:
                job = self._latest('new_id', identifier)
                # Once injection is complete, this job's archive state belongs
                # to its old_id. Do not attach that warning to the new session.
                if for_status and job and job['state'] not in self.INITIALIZING:
                    return None
            if job is None:
                return None
            return {**job, 'ok': job['state'] not in ('failed', 'unknown'),
                    'completed': job['state'] in self.TERMINAL,
                    'conversation_url': f"conversation://{job['new_id']}"}

    def busy(self, conv_id):
        with self.lock:
            self._load()
            return conv_id in self.preparing or any(
                (j['old_id'] == conv_id and j['state'] not in self.TERMINAL) or
                (j['new_id'] == conv_id and j['state'] in self.INITIALIZING)
                for j in self.jobs.values())

    def has_active_threads(self):
        with self.lock:
            # A worker registered before Thread.start() must protect that gap.
            return bool(self.preparing or self.threads)

    def _make_room(self):
        while len(self.jobs) >= 256:
            finished = [job for job in self.jobs.values()
                        if job['state'] in ('done', 'failed') and job['job_id'] not in self.threads]
            if not finished:
                raise ValueError('交接作业记录已满，请先核对未完成或结果不确定的作业')
            oldest = min(finished, key=lambda j: (j.get('created_at', 0), j.get('updated_at', 0)))
            del self.jobs[oldest['job_id']]

    def _focus_new(self, job_id):
        global _CURRENT_ACTIVE_ID, _HANDOFF_FOCUS
        with self.lock:
            job = dict(self.jobs[job_id])
            if self._latest('old_id', job['new_id']) is not None:
                # A -> B finishing late must not pull focus back from B -> C.
                return
            excluded = frozenset({job['old_id']} | {
                j['old_id'] for j in self.jobs.values()
                if j['state'] in ('waiting_old_idle', 'archiving')})
        _, view_times, _ = read_annotations()
        baseline = max((stamp for cid, stamp in view_times.items() if cid not in excluded), default=0.0)
        with self.lock:
            if self._latest('old_id', job['new_id']) is not None:
                return
            with _STATE_LOCK:
                _CURRENT_ACTIVE_ID = job['new_id']
                _HANDOFF_FOCUS = (job['new_id'], baseline, excluded)
                if not _save_persisted_state():
                    raise OSError('无法保存新会话焦点')

    def submit(self, old_id, markdown, title=None, expected_step_idx=None):
        # Register before discovery/StartCascade, and keep the lease until the
        # worker is registered and started. Reload cannot interrupt the handoff
        # between successful creation and _run entering its background context.
        with _background_job() as admitted:
            if not admitted:
                raise ValueError('服务正在重载或停止，请稍后提交交接')
            return self._submit(old_id, markdown, title, expected_step_idx)

    def _submit(self, old_id, markdown, title=None, expected_step_idx=None):
        app_base = next((base for source, base in APP_DIRS if source == 'App'), None)
        db = app_base / 'conversations' / f'{old_id}.db' if app_base else None
        if db is None or not db.is_file():
            raise FileNotFoundError('未找到 App 会话')
        digest = hashlib.sha256(markdown.encode('utf-8')).hexdigest()
        with self.lock:
            self._load()
            previous = self._latest('old_id', old_id)
            if (previous and previous['state'] in ('failed', 'unknown')
                    and not (app_base / 'conversations' / f"{previous['new_id']}.db").exists()):
                del self.jobs[previous['job_id']]
                self._save()
                previous = None
            if previous:
                if previous['summary_sha256'] == digest and (title is None or previous['title'] == title):
                    return self.get(previous['job_id'])
                raise ValueError('此旧会话已有交接作业，请查询原 job_id')
            incoming = self._latest('new_id', old_id)
            if old_id in self.preparing or (incoming and incoming['state'] in self.INITIALIZING):
                raise ValueError('会话正在初始化或准备交接，请稍后重试')
            self.preparing.add(old_id)
        job_id = None
        try:
            annotation = read_conversation_annotation(db)
            if annotation.get('archived'):
                raise ValueError('旧会话已归档')
            context = extract_handoff_metadata(db)
            snapshot = context['snapshot_step_idx']
            if expected_step_idx is not None:
                if expected_step_idx > snapshot or context['last_user_step_idx'] > expected_step_idx:
                    raise ValueError('交接摘要已过期：会话新增用户输入或 step 范围不匹配')
            titles, _, _ = read_annotations()
            resolved_title = title or annotation.get('title') or titles.get(old_id) or old_id[:8]
            # Discovery is readonly. Its failure leaves no immutable job, so a
            # later submit may safely retry without inventing a second cascade.
            endpoint = find_native_cascade_endpoint(old_id)
            with self.lock:
                self._make_room()
                job_id, new_id = str(uuid.uuid4()), str(uuid.uuid4())
                self.jobs[job_id] = {
                    'job_id': job_id, 'old_id': old_id, 'new_id': new_id, 'title': resolved_title,
                    'summary_sha256': digest, 'snapshot_step_idx': snapshot,
                    'seed_sent': False, 'seed_verified': False,
                    'state': 'creating', 'created_at': time.time(), 'updated_at': time.time(),
                }
                self._save()  # Reserve IDs before the first possible side effect.
            start_native_cascade(new_id, context, endpoint=endpoint)
            warning = {}
            try:
                update_conversation_annotations(new_id, title=resolved_title, endpoint=endpoint)
            except Exception as exc:
                # Creation is confirmed; an initial rename error must not leave
                # the new conversation unseeded. _run sets the title again.
                warning['warning'] = f'首次命名未确认，将在交接回复后重设：{exc}'
            self._transition(job_id, 'created', **warning)
            response = self.get(job_id)
            thread = threading.Thread(
                target=self._run, args=(job_id, db, context, endpoint, markdown),
                name=f'handoff-{job_id[:8]}', daemon=True)
            with self.lock:
                self.threads[job_id] = thread
            thread.start()
            return response
        except Exception as exc:
            if job_id is not None:
                with self.lock:
                    self.threads.pop(job_id, None)
                extra = {'rejected': True} if getattr(exc, 'state', '') == 'rejected' else {}
                self._transition(job_id, 'unknown' if getattr(exc, 'state', '') == 'unknown' else 'failed',
                                 error=str(exc), **extra)
            raise
        finally:
            with self.lock:
                self.preparing.discard(old_id)

    def _wait(self, predicate):
        deadline = time.monotonic() + self.timeout
        while not _STOP_EVENT.is_set():
            result = predicate()
            if result:
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError('等待会话确认或完全空闲超时')
            if _STOP_EVENT.wait(self.poll_interval):
                break
        raise InterruptedError('Sidecar 正在停止，未继续归档')

    def _inspect(self, db, endpoint, snapshot=None):
        summary = get_native_cascade_summaries(endpoint).get(db.stem)
        _, children = collect_subagents(db.stem)
        return inspect_cascade_idle(db, snapshot_step_idx=snapshot,
                                    runtime_summary=summary, subagents=children)

    def _run(self, job_id, old_db, context, endpoint, markdown):
        with _background_job() as admitted:
            if not admitted:
                self._transition(job_id, 'failed', error='服务正在重载或停止，未发送交接正文')
                with self.lock:
                    self.threads.pop(job_id, None)
                return
            seed_sent = False
            seed_verified = False
            try:
                job = self.get(job_id)
                new_db = old_db.with_name(f"{job['new_id']}.db")
                marker = f"[handoff:{job_id}]"
                text = (f"{marker}\n来源会话：{job['old_id']}\n"
                        f"快照 step：{job['snapshot_step_idx']}\n\n{markdown}\n\n"
                        "你是主代理（当前为会话交接首轮注入）。本轮仅简短回复确认已接收上下文并原地待命，"
                        "不要在本轮调用任何工具、修改文件或派发子代理；待用户发出下一条消息后，按主代理规则正常执行与调度子代理。")
                self._transition(job_id, 'sending')
                send_native_cascade_message(
                    job['new_id'],
                    text,
                    context['cascade_cfg'],
                    endpoint=endpoint,
                    token_threshold=200_000,
                )
                seed_sent = True
                self._transition(job_id, 'waiting_new_reply', seed_sent=True)

                def received():
                    if not new_db.is_file():
                        return False
                    state = self._inspect(new_db, endpoint)
                    if state['last_user_step_idx'] < 0 or not state['safe']:
                        return False
                    # Require the submitted message, not merely an unrelated idle DB.
                    with contextlib.closing(connect_readonly(new_db)) as con:
                        return bool(con.execute(
                            'SELECT 1 FROM steps WHERE step_type=14 AND instr(step_payload,?)>0 LIMIT 1',
                            (marker.encode('utf-8'),)).fetchone())

                self._wait(received)
                seed_verified = True
                update_conversation_annotations(job['new_id'], title=job['title'], endpoint=endpoint)
                self._wait(lambda: read_conversation_annotation(new_db).get('title') == job['title'])
                self._transition(job_id, 'waiting_old_idle', seed_verified=True)
                self._focus_new(job_id)
                stable = [None]

                def old_ready():
                    state = self._inspect(old_db, endpoint, job['snapshot_step_idx'])
                    if state['reason'] == 'new_user_input':
                        raise ValueError('旧会话出现新的 user_input，已放弃自动归档')
                    # Two stable observations, plus a final check immediately
                    # before mutation. The LS offers no atomic if_idle condition.
                    previous = stable[0]
                    stable[0] = state['last_step_idx'] if state['safe'] else None
                    return state if state['safe'] and previous == state['last_step_idx'] else False

                confirmed = self._wait(old_ready)
                self._transition(job_id, 'archiving')
                latest = self._inspect(old_db, endpoint, job['snapshot_step_idx'])
                if not latest['safe'] or latest['last_step_idx'] != confirmed['last_step_idx']:
                    raise ValueError('归档前旧会话发生变化，已放弃自动归档')
                if _STOP_EVENT.is_set():
                    raise InterruptedError('Sidecar 正在停止')
                update_conversation_annotations(job['old_id'], archived=True, endpoint=endpoint)
                self._wait(lambda: read_conversation_annotation(old_db).get('archived') is True)
                self._focus_new(job_id)
                self._transition(job_id, 'done')
            except NativeRPCError as exc:
                if exc.state == 'unknown':
                    target_state = 'unknown'
                elif not seed_verified:
                    target_state = 'failed' if not seed_sent else 'unknown'
                else:
                    target_state = 'needs_manual_archive'
                self._transition(job_id, target_state, error=str(exc))
            except Exception as exc:
                target_state = 'needs_manual_archive' if seed_verified else ('unknown' if seed_sent else 'failed')
                self._transition(job_id, target_state, error=str(exc))
            finally:
                with self.lock:
                    self.threads.pop(job_id, None)


_HANDOFF_CONTROLLER = HandoffController()


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
    return (round(float(item[0]), 3), round(float(item[1]), 8), str(item[2]), uid, *usage,
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
    global _QUOTA_CACHE, _LAST_QUOTA_FETCH_TS
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
            with _STATE_LOCK:
                _QUOTA_CACHE = merge_quota_cache(_QUOTA_CACHE, parsed)
                if agy_ok and has_bucket_data:
                    _LAST_QUOTA_FETCH_TS = time.time()
                _save_persisted_state()
            if _COST_CACHE_LOADED:
                _recompute_window_costs_from_memory()
        return {**agy_meta, "refreshed": True}


def _get_live_quota() -> dict[str, dict]:
    now = time.time()
    out: dict[str, dict] = {}
    with _STATE_LOCK:
        snap = dict(_QUOTA_CACHE)
    with _COST_LOCK:
        window_costs, estimates = dict(_WINDOW_COSTS), dict(_WINDOW_ESTIMATES)
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
    return attach_quota_costs(out, active_bids, window_costs)


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
                if tick == 1 or tick % 2000 == 0:
                    if sync_ls_model_configs():
                        with _STATE_LOCK:
                            _save_persisted_state()
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

    def _handle_handoff(self, qs):
        if self.command == 'GET':
            identifier = (qs.get('id') or [''])[0]
            if not identifier:
                self._respond(400, {'ok': False, 'state': 'invalid_parameters', 'error': '缺少 id'})
                return
            job = _HANDOFF_CONTROLLER.get(identifier)
            self._respond(200 if job else 404, job or {'ok': False, 'state': 'not_found'})
            return
        if self.command != 'POST':
            self._respond(405, {'ok': False, 'state': 'method_not_allowed'})
            return
        # CLI loopback requests have no Origin. A sandboxed web document's
        # opaque Origin is not equivalent to a trusted local CLI.
        if self.headers.get('Origin') == 'null':
            self._respond(403, {'ok': False, 'state': 'invalid_origin'})
            return
        if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
            self._respond(400, {'ok': False, 'state': 'invalid_parameters', 'error': '需要 JSON 请求体'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= 768 * 1024:
                raise ValueError('请求体长度无效')
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('JSON 字段重复')
                    result[key] = value
                return result
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('请求体不完整')
            data = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(data, dict) or set(data) - {'old_id', 'handoff_markdown', 'title', 'expected_step_idx'}:
                raise ValueError('JSON 字段无效')
            old_id, markdown, title = data.get('old_id'), data.get('handoff_markdown'), data.get('title')
            expected = data.get('expected_step_idx')
            if not isinstance(old_id, str) or str(uuid.UUID(old_id)) != old_id:
                raise ValueError('old_id 必须是规范 UUID')
            if not isinstance(markdown, str) or not markdown.strip() or '\x00' in markdown:
                raise ValueError('交接单必须是非空文本且不含 NUL')
            md_bytes = len(markdown.encode('utf-8'))
            non_ascii = sum(1 for ch in markdown if ord(ch) >= 128)
            ascii_chars = len(markdown) - non_ascii
            est_tokens = (ascii_chars + 3) // 4 + (non_ascii * 2 + 2) // 3
            if md_bytes > 96 * 1024 or est_tokens > 24_000:
                raise ValueError(
                    f'交接单超出上下文预算（当前约 {est_tokens} tokens / {md_bytes} bytes；'
                    '上限为 24,000 tokens 且不超过 96 KiB，以确保新会话总上下文 ≤50k tokens）'
                )
            if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 512 or any(c in title for c in '\x00\r\n')):
                raise ValueError('title 无效')
            if expected is not None and (type(expected) is not int or not 0 <= expected <= 2147483647):
                raise ValueError('expected_step_idx 无效')
        except (ValueError, TypeError, UnicodeError) as exc:
            self._respond(400, {'ok': False, 'state': 'invalid_parameters', 'error': str(exc)})
            return
        try:
            result = _HANDOFF_CONTROLLER.submit(old_id, markdown, title, expected)
            self._respond(200, result)
        except FileNotFoundError as exc:
            self._respond(404, {'ok': False, 'state': 'not_found', 'error': str(exc)})
        except ValueError as exc:
            self._respond(409, {'ok': False, 'state': 'conflict', 'error': str(exc)})
        except NativeRPCError as exc:
            job = _HANDOFF_CONTROLLER.get(old_id)
            self._respond(502, {**(job or {}), 'ok': False, 'state': exc.state, 'error': str(exc)})

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
        if parsed.path == '/api/handoff':
            self._handle_handoff(qs)
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
            with _STATE_LOCK:
                fetched_at = _LAST_QUOTA_FETCH_TS
            self._respond(200, {
                'ok': bool(agy_meta.get('ok', True)),
                'refreshed': bool(agy_meta.get('refreshed', False)),
                'error': str(agy_meta.get('error') or ''),
                'reason': str(agy_meta.get('reason') or ''),
                'quota_fetched_at': fetched_at,
                'quota_cache_ttl_sec': _QUOTA_CACHE_TTL_SEC,
                'quota': _get_live_quota(),
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
                active['handoff'] = _HANDOFF_CONTROLLER.get(active['id'], for_status=True)

            with _STATE_LOCK:
                fetched_at = _LAST_QUOTA_FETCH_TS
            self._respond(200, {
                "ui_version": _current_ui_version(),
                "theme": get_live_theme(),
                "quota": _get_live_quota(),
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
        with _HANDOFF_CONTROLLER.lock:
            handoffs = list(_HANDOFF_CONTROLLER.threads.values())
        for worker in handoffs:
            worker.join(timeout=1.0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        _SERVER_INSTANCE = None


if __name__ == '__main__':
    main()
