"""Cheap, lazy metadata matching for CLI dispatches without a session ID."""
from __future__ import annotations

import re
import urllib.parse

from cockpit_io import (DROID_SETTINGS_PATHS, FACTORY_SESSIONS_DIR, GROK_SESSIONS_DIR, GROK_SESSION_DIRS,
                        PI_SESSIONS_DIR, PI_SESSION_PATHS, cached_paths, parse_iso_ts)

RE_PI_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2})-(\d{2})-(\d{2})-(\d{3})Z_([0-9a-f-]{36})\.jsonl$")


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
                PI_SESSION_PATHS.setdefault(sid, p)
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
            DROID_SETTINGS_PATHS.setdefault(sid, p)
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
            GROK_SESSION_DIRS.setdefault(sid, sdir)
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


class SessionMatcher:
    """Per-parent lazy indexes; do not reuse a heuristically matched session."""

    def __init__(self):
        self._metadata = {}
        self._used = {"pi": set(), "droid": set(), "grok": set()}

    def remember_explicit(self, agent, session_id):
        # Pi has historically reserved only timestamp matches.
        if agent in ("droid", "grok"):
            self._used[agent].add(session_id)

    def match(self, agent, cwd, started_at):
        if agent not in self._metadata:
            list_meta = {"pi": _list_pi_session_meta, "droid": _list_droid_session_meta, "grok": _list_grok_session_meta}
            if agent not in list_meta:
                return None
            self._metadata[agent] = list_meta[agent]()

        slug = cwd.strip("/").replace("/", "-")
        if agent == "grok":
            matches_dir = lambda name: cwd in name or name in cwd
        else:
            prefix = "--" + slug + "--" if agent == "pi" else "-" + slug
            matches_dir = lambda name: prefix in name
        used = self._used[agent]
        candidates = (row for row in self._metadata[agent]
                      if row["id"] not in used and matches_dir(row["dir_name"])
                      and started_at > 0 and -5.0 <= row["ts"] - started_at <= 240.0)
        candidate = min(candidates, key=lambda row: abs(row["ts"] - started_at), default=None)
        if candidate is None:
            return None
        used.add(candidate["id"])
        return candidate["id"]
