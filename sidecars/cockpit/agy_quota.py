"""Official Antigravity CLI (`/usage --output-format json`) on-demand quota monitor."""

from __future__ import annotations

import json
import math
import time
from typing import Callable

from cockpit_io import APP_DIRS, cached_paths, get_db_mtime, parse_iso_ts, serialized

AGY_BUCKET_IDS: tuple[str, ...] = ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly")
AGY_CLI_TIMEOUT_SEC = 15.0

_AGY_QUOTA_CACHE: dict[str, dict] = {}
_LAST_AGY_FETCH_TS: float = 0.0
_LAST_AGY_FETCH_OK: bool = True
_LAST_AGY_SUCCESS_MTIME: float = 0.0
_LAST_AGY_META: dict = {"reason": "init", "probed": False, "ok": True, "error": ""}


def parse_agy_usage_json(raw_text: str, now: float | None = None) -> tuple[dict[str, dict], str]:
    """Parse `agy -p /usage --output-format json` payload into Cockpit bucket entries."""
    eff_now = time.time() if now is None else float(now)
    text = (raw_text or "").strip()
    if not text:
        return {}, "空输出"
    try:
        obj = json.loads(text)
    except Exception as exc:
        return {}, f"JSON 解析失败: {exc}"
    if not isinstance(obj, dict):
        return {}, "输出非 JSON 对象"
    status = obj.get("status")
    if status is not None and str(status).upper() != "SUCCESS":
        return {}, f"CLI 状态异常: {status}"
    groups = ((obj.get("command") or {}).get("data") or {}).get("groups") or []
    if not isinstance(groups, list):
        return {}, "缺少 groups 字段"
    parsed: dict[str, dict] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            bid = str(bucket.get("id") or "").strip()
            raw_frac = bucket.get("remaining_fraction")
            if not bid or raw_frac is None or isinstance(raw_frac, bool):
                continue
            try:
                rem_frac = float(raw_frac)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(rem_frac):
                continue
            rtime = str(bucket.get("reset_time") or "")
            rts = float(parse_iso_ts(rtime) or 0.0)
            parsed[bid] = {
                "rem_frac": max(0.0, min(1.0, rem_frac)),
                "reset_ts": rts,
                "observed_at": eff_now,
            }
    if not parsed:
        return {}, "未返回任何配额桶"
    return parsed, ""


def max_agy_db_mtime(app_dirs=APP_DIRS) -> float:
    """Return latest App/CLI conversation DB (or WAL) mtime for activity-driven refresh."""
    max_mt = 0.0
    for _, base in app_dirs:
        cdir = base / "conversations"
        if not cdir.exists():
            continue
        try:
            for path in cached_paths(cdir, "*.db", ttl=2.0):
                try:
                    max_mt = max(max_mt, get_db_mtime(path))
                except Exception:
                    continue
        except Exception:
            continue
    return max_mt


def should_probe_agy_quota(
    cache: dict[str, dict],
    now: float,
    *,
    max_db_mt: float,
    last_fetch_ts: float,
    last_success_mt: float,
    ui_active: bool,
    force_refresh: bool,
    last_ok: bool = True,
) -> tuple[bool, str]:
    """Only probe `agy -p /usage` on explicit refresh; never auto-poll in background."""
    if force_refresh:
        return True, "manual"
    return False, "cached"


@serialized(lambda *args, **kwargs: "agy-quota")
def fetch_agy_quota(
    *,
    cli_bin: str | None,
    run_cmd: Callable[[list[str], float], str],
    seed_cache: dict[str, dict] | None = None,
    force_refresh: bool = False,
    ui_active: bool = False,
    app_dirs=APP_DIRS,
) -> tuple[dict[str, dict], dict]:
    """Fetch AGY quota buckets via official `agy -p /usage --output-format json` on demand."""
    global _AGY_QUOTA_CACHE, _LAST_AGY_FETCH_TS, _LAST_AGY_FETCH_OK, _LAST_AGY_SUCCESS_MTIME, _LAST_AGY_META

    if not _AGY_QUOTA_CACHE and seed_cache:
        for bid in AGY_BUCKET_IDS:
            entry = seed_cache.get(bid)
            if isinstance(entry, dict) and entry.get("rem_frac") is not None:
                _AGY_QUOTA_CACHE[bid] = dict(entry)

    if cli_bin is None:
        _LAST_AGY_META = {
            "reason": "not_installed",
            "probed": False,
            "ok": False,
            "error": "未找到 agy CLI",
        }
        return {k: dict(v) for k, v in _AGY_QUOTA_CACHE.items()}, dict(_LAST_AGY_META)

    now = time.time()
    scan_mt = max_agy_db_mtime(app_dirs)
    need_probe, reason = should_probe_agy_quota(
        _AGY_QUOTA_CACHE,
        now,
        max_db_mt=scan_mt,
        last_fetch_ts=_LAST_AGY_FETCH_TS,
        last_success_mt=_LAST_AGY_SUCCESS_MTIME,
        ui_active=ui_active,
        force_refresh=force_refresh,
        last_ok=_LAST_AGY_FETCH_OK,
    )
    if not need_probe:
        _LAST_AGY_META = {"reason": reason, "probed": False, "ok": True, "error": ""}
        return {k: dict(v) for k, v in _AGY_QUOTA_CACHE.items()}, dict(_LAST_AGY_META)

    _LAST_AGY_FETCH_TS = now
    parsed: dict[str, dict] = {}
    err_msg = ""
    try:
        out = run_cmd([cli_bin, "-p", "/usage", "--output-format", "json"], AGY_CLI_TIMEOUT_SEC)
        parsed, err_msg = parse_agy_usage_json(out, now=time.time())
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__

    _LAST_AGY_FETCH_OK = bool(parsed)
    if parsed:
        _AGY_QUOTA_CACHE.update(parsed)
        if scan_mt > 0.0:
            _LAST_AGY_SUCCESS_MTIME = max(_LAST_AGY_SUCCESS_MTIME, scan_mt)
        else:
            _LAST_AGY_SUCCESS_MTIME = max(_LAST_AGY_SUCCESS_MTIME, time.time())
        _LAST_AGY_META = {"reason": reason, "probed": True, "ok": True, "error": ""}
    else:
        _LAST_AGY_META = {
            "reason": reason,
            "probed": True,
            "ok": False,
            "error": err_msg or "配额解析失败",
        }

    return {k: dict(v) for k, v in _AGY_QUOTA_CACHE.items()}, dict(_LAST_AGY_META)
