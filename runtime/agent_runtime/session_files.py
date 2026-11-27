"""Session-file location and unchanged-content cache; no disk IO under map locks."""
import copy
import os
import threading
from pathlib import Path

_LOCK = threading.RLock()
_CACHE = {}
_LEDGER_LOCKS = {}


def ledger_lock(sid, daemon_dir):
    # Independent ledgers must never serialize each other's disk writes.
    key = (str(daemon_dir), os.environ.get('SA_SESSIONS_LEDGER_DIR'),
           os.environ.get('SA_SUBAGENTD_DIR'), sid)
    with _LOCK:
        return _LEDGER_LOCKS.setdefault(key, threading.RLock())


def resolve(agent, session_id):
    home, jsonl_path, settings_path, direct = Path.home(), None, None, Path(os.path.expanduser(session_id))
    if direct.is_file():
        settings_path = direct if direct.name.endswith(".settings.json") else None
        cand = direct.with_name(direct.name.replace(".settings.json", ".jsonl")) if settings_path else direct
        jsonl_path = cand if cand.is_file() else None
    elif agent in ("codex", "pi"):
        root = home / (".codex/sessions" if agent == "codex" else ".pi/agent/sessions")
        matches = sorted(root.rglob(f"*{session_id}*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []
        jsonl_path = matches[0] if matches else None
    elif agent == "droid" and (home / ".factory/sessions").is_dir():
        root = home / ".factory/sessions"
        s_matches = sorted(root.rglob(f"*{session_id}*.settings.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if s_matches:
            settings_path, cand = s_matches[0], s_matches[0].with_name(s_matches[0].name.replace(".settings.json", ".jsonl"))
            jsonl_path = cand if cand.is_file() else None
        if jsonl_path is None:
            j_matches = sorted(root.rglob(f"*{session_id}*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            jsonl_path = j_matches[0] if j_matches else None
    return jsonl_path, settings_path


def signature(paths):
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p else None for p in paths)


def safe_signature(paths):
    try:
        return signature(paths) if paths else None
    except OSError:
        return None


def cached_jsonl_inspection(parse):
    def inspect(agent, session_id):
        key = (os.getpid(), str(Path.home()), agent, session_id)
        with _LOCK:
            cached = _CACHE.get(key)
        paths = cached[0] if cached else None
        before = safe_signature(paths)
        if before is None:
            paths = resolve(agent, session_id)
            before = safe_signature(paths)
        if cached and agent == 'droid' and paths:
            jsonl, settings = paths
            companion = (jsonl.with_name(jsonl.stem + '.settings.json') if jsonl and not settings
                         else settings.with_name(settings.name.replace('.settings.json', '.jsonl')) if settings and not jsonl else None)
            if companion and companion.is_file():
                paths = resolve(agent, session_id)
                before = safe_signature(paths)
        if cached and paths == cached[0] and before is not None and before == cached[1]:
            return copy.deepcopy(cached[2])
        result = parse(agent, session_id, paths)
        # Never cache a snapshot taken while the producer was writing or deleting it.
        after = safe_signature(paths)
        if result is not None and before is not None and after == before:
            with _LOCK:
                _CACHE[key] = (paths, before, result)
        return copy.deepcopy(result)
    return inspect
