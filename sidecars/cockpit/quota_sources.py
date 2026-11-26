"""Normalize Codex limits without inferring a duration or mixing limit IDs."""
from __future__ import annotations

import math
from urllib.parse import quote

DEFAULT_CODEX_LIMIT = "codex"
_SOURCE_PRIORITY = {"rollout": 1, "rpc": 2}


def _number(value, default=0.0):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _label(minutes):
    if minutes == 10080:
        return "周"
    if not minutes:
        return "未知周期"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    return (f"{days}d" if days else "") + (f"{hours}h" if hours else "") + (f"{mins}m" if mins else "")


def _limit_id(entry, fallback=""):
    return str(entry.get("limitId") or entry.get("limit_id") or fallback)


def _add_windows(out, entry, limit_id, observed, *, camel, anonymous_label=False):
    fields = (("usedPercent", "windowDurationMins", "resetsAt") if camel
              else ("used_percent", "window_minutes", "resets_at"))
    cost_scope = limit_id == DEFAULT_CODEX_LIMIT
    for slot in ("primary", "secondary"):
        win = entry.get(slot)
        if not isinstance(win, dict):
            continue
        used = _number(win.get(fields[0]), None)
        if used is None:
            continue
        duration = _number(win.get(fields[1]))
        minutes = int(duration) if 0 < duration <= 2**31 - 1 and duration.is_integer() else None
        standard = {300: "codex-5h", 10080: "codex-weekly"}.get(minutes) if cost_scope or anonymous_label else None
        bid = standard if standard and standard not in out else f"codex-window-{quote(limit_id, safe='')}-{slot}"
        out.setdefault(bid, {
            "rem_frac": max(0.0, min(1.0, (100.0 - used) / 100.0)),
            "reset_ts": _number(win.get(fields[2])), "observed_at": observed,
            "window_minutes": minutes, "window_label": _label(minutes),
            "limit_id": limit_id or None, "cost_scope": cost_scope,
            "quota_source": "rpc" if camel else "rollout", "quota_slot": slot,
        })


def codex_rpc_windows(payload, observed_at):
    """Keep each visible limit; the default ledger cannot price other limit IDs."""
    out = {}
    top = payload.get("rateLimits")
    raw_by_id = payload.get("rateLimitsByLimitId")
    by_id = {k: v for k, v in raw_by_id.items() if isinstance(v, dict)} if isinstance(raw_by_id, dict) else {}
    top_id = _limit_id(top) if isinstance(top, dict) else None
    if top_id == "" and DEFAULT_CODEX_LIMIT in by_id:
        default = by_id[DEFAULT_CODEX_LIMIT]
        reported = {slot: top[slot] for slot in ("primary", "secondary") if isinstance(top.get(slot), dict)}
        if (_limit_id(default, DEFAULT_CODEX_LIMIT) == DEFAULT_CODEX_LIMIT and reported
                and all(default.get(slot) == window for slot, window in reported.items())):
            top_id = DEFAULT_CODEX_LIMIT
    if top_id is not None:
        # The by-ID representation can supply a window omitted from the alias.
        matching = by_id.get(top_id, {})
        if _limit_id(matching, top_id) != top_id:
            matching = {}
        entry = {**matching, **{k: v for k, v in top.items() if v is not None}}
        _add_windows(out, entry, top_id, observed_at, camel=True, anonymous_label=not top_id and not by_id)
    for key, entry in by_id.items():
        limit_id = _limit_id(entry, key)
        if limit_id == top_id and key == top_id:
            continue
        _add_windows(out, entry, limit_id, observed_at, camel=True)
    return out


def codex_rollout_windows(limits, observed_at):
    out = {}
    _add_windows(out, limits, _limit_id(limits), observed_at, camel=False)
    return out


def _codex_rows(data):
    return {key: row for key, row in data.items() if key.startswith("codex-")
            and isinstance(row, dict) and row.get("rem_frac") is not None}


def _observed(row):
    return _number(row.get("observed_at"))


def _known_window(row):
    return _number(row.get("window_minutes")) > 0 and _number(row.get("reset_ts")) > 0


def full_unstarted(row):
    rem, reset = row.get("rem_frac"), row.get("reset_ts")
    no_reset = reset is None or (type(reset) in (int, float) and -math.inf < reset <= 0)
    return no_reset and type(rem) in (int, float) and 1.0 <= rem < math.inf


def _replaces_expired_window(previous, row):
    limit_id, slot = row.get("limit_id"), row.get("quota_slot")
    return (row.get("quota_source") == "rpc" and bool(limit_id) and previous.get("limit_id") == limit_id
            and previous.get("quota_slot") in (None, slot) and full_unstarted(row)
            and 0 < _number(previous.get("reset_ts")) <= _observed(row))


def merge_codex_quota(out, parsed):
    """A stale or incomplete fallback cannot replace a fresher account snapshot."""
    old = _codex_rows(out)
    incoming = _codex_rows(parsed)
    out.update({key: row for key, row in parsed.items() if key not in incoming})
    newest = max((_observed(row) for row in old.values()), default=0)
    if not old:
        out.update(incoming)
        return
    fresh = {key: row for key, row in incoming.items() if _observed(row) > newest}
    source_rank = {_SOURCE_PRIORITY.get(row.get("quota_source"), 0) for row in fresh.values()}
    default_slots = {row.get("quota_slot") for row in fresh.values()
                     if row.get("limit_id") == DEFAULT_CODEX_LIMIT and _known_window(row)}
    if (len(fresh) == len(incoming) and default_slots == {"primary", "secondary"}
            and len(source_rank) == 1 and min(source_rank) >= max(
                _SOURCE_PRIORITY.get(row.get("quota_source"), 0) for row in old.values())
            and all(_known_window(row) for row in fresh.values())):
        for key in old:
            out.pop(key)
        out.update(fresh)
        return
    for key, row in fresh.items():
        previous = old.get(key)
        if previous and (previous.get("limit_id") not in (None, row.get("limit_id"))
                         or (_known_window(previous) and not _known_window(row)
                             and not _replaces_expired_window(previous, row))):
            continue
        limit_id, slot = row.get("limit_id"), row.get("quota_slot")
        if limit_id and slot and _known_window(row):
            for prior, cell in old.items():
                if prior != key and (cell.get("limit_id"), cell.get("quota_slot")) == (limit_id, slot):
                    out.pop(prior, None)
        out[key] = row
