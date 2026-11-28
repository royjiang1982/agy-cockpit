"""Resolve and cache CLI session metrics; delegate log interpretation to each engine."""
from __future__ import annotations

from pathlib import Path

from cockpit_io import (CODEX_SESSIONS_DIR, FACTORY_SESSIONS_DIR, GROK_SESSIONS_DIR, PI_SESSIONS_DIR,
                        DROID_SETTINGS_PATHS, GROK_SESSION_DIRS, PI_SESSION_PATHS,
                        BoundedCache, cached_paths, dependency_signature, serialized)
from metric_cache import _live_metrics, _publish_metrics
from metric_codex import read_codex
from metric_droid import read_droid
from metric_grok import read_grok
from metric_pi import read_pi


class _SessionMetrics:
    def __init__(self, paths, pattern, suffix="", *, latest=False, directory=False):
        self.pattern = pattern
        self.suffix = suffix
        self.latest = latest
        self.directory = directory
        self.paths = paths
        self.metrics = BoundedCache(128)

    def _valid(self, path):
        return path.is_dir() if self.directory else path.exists()

    def _resolve(self, value, root):
        if isinstance(value, Path) or (isinstance(value, str) and (
            "/" in value or (self.suffix and value.endswith(self.suffix))
        )):
            path = Path(value)
            if self.directory and path.is_file():
                path = path.parent
            return path if self._valid(path) else None

        session_id = str(value)
        # Codex and Pi historically require their root even on a path-cache hit.
        if self.latest and not root.exists():
            return None
        path = self.paths.get(session_id)
        if path is not None and self._valid(path):
            return path
        if not self.latest and (not root.exists() or session_id.startswith("pending-")):
            return None
        matches = cached_paths(root, self.pattern.format(session_id), recursive=not self.directory)
        if not matches:
            return None
        path = max(matches) if self.latest else matches[0]
        if self.directory and not path.is_dir():
            return None
        self.paths[session_id] = path
        return path

    def analyze(self, value, root, measure, read, raw_file_str=""):
        source = measure(self._resolve(value, root), raw_file_str)
        if source is None:
            return None
        cache_key, mtime, args = source
        cached = self.metrics.get(cache_key)
        if cached and cached[0] == mtime:
            return _live_metrics(cached[1])
        result = read(*args)
        return _publish_metrics(self.metrics, cache_key, mtime, result) if result is not None else None


_CODEX_SESSIONS = _SessionMetrics(BoundedCache(512), "*{}.jsonl", ".jsonl", latest=True)
_PI_SESSIONS = _SessionMetrics(PI_SESSION_PATHS, "*_{}.jsonl", ".jsonl", latest=True)
_DROID_SESSIONS = _SessionMetrics(DROID_SETTINGS_PATHS, "{}.settings.json", ".json")
_GROK_SESSIONS = _SessionMetrics(GROK_SESSION_DIRS, "*/{}", directory=True)


def _measure_jsonl(path, _raw_file_str):
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except Exception:
        return None
    return str(path), mtime, (path, mtime)


def _measure_droid(settings_path, raw_file_str):
    raw_path = Path(raw_file_str) if raw_file_str else None
    src = (settings_path if settings_path and settings_path.exists()
           else raw_path if raw_path and raw_path.exists() else None)
    if src is None:
        return None

    jsonl_path = None
    if settings_path and settings_path.name.endswith(".settings.json"):
        sid = settings_path.name[:-len(".settings.json")]
        candidate = settings_path.with_name(f"{sid}.jsonl")
        if candidate.exists():
            jsonl_path = candidate
    try:
        mtime = src.stat().st_mtime
        if jsonl_path is not None:
            mtime = max(mtime, jsonl_path.stat().st_mtime)
    except Exception:
        return None

    cache_key = (str(src), dependency_signature(p for p in (raw_path, jsonl_path) if p))
    return cache_key, mtime, (src, raw_path, jsonl_path, mtime)


def _measure_grok(sess_dir, raw_file_str):
    raw_path = Path(raw_file_str) if raw_file_str else None
    if sess_dir is None and (raw_path is None or not raw_path.exists()):
        return None
    summary_path = sess_dir / "summary.json" if sess_dir else None
    updates_path = sess_dir / "updates.jsonl" if sess_dir else None
    events_path = sess_dir / "events.jsonl" if sess_dir else None
    usage_path = sess_dir / "usage.json" if sess_dir else None
    mtime = usage_mtime = 0.0
    for path in (summary_path, updates_path, events_path, usage_path, raw_path):
        if path is not None and path.exists():
            try:
                stamp = path.stat().st_mtime
                mtime = max(mtime, stamp)
                if path == usage_path:
                    usage_mtime = stamp
            except Exception:
                pass
    if mtime <= 0:
        return None
    cache_key = (str(sess_dir or raw_path), usage_mtime)
    return cache_key, mtime, (summary_path, updates_path, events_path, usage_path, raw_path, mtime, usage_mtime)


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_codex_thread(thread_or_path: str | Path) -> dict | None:
    """Analyze a Codex CLI thread ID or direct JSONL path."""
    return _CODEX_SESSIONS.analyze(thread_or_path, CODEX_SESSIONS_DIR, _measure_jsonl, read_codex)


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_pi_session(session_or_path: str | Path) -> dict | None:
    """Analyze a Pi CLI session ID or direct JSONL path."""
    return _PI_SESSIONS.analyze(session_or_path, PI_SESSIONS_DIR, _measure_jsonl, read_pi)


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_droid_session(session_or_path: str | Path, raw_file_str: str = "") -> dict | None:
    """Analyze a Droid CLI session or its raw JSON fallback."""
    return _DROID_SESSIONS.analyze(session_or_path, FACTORY_SESSIONS_DIR, _measure_droid, read_droid, raw_file_str)


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_grok_session(session_or_path: str | Path, raw_file_str: str = "") -> dict | None:
    """Analyze a Grok CLI session or its streaming JSON fallback."""
    return _GROK_SESSIONS.analyze(session_or_path, GROK_SESSIONS_DIR, _measure_grok, read_grok, raw_file_str)
