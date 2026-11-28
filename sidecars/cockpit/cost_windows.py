"""Assign local cost events to provider windows and to an aligned quota snapshot."""
from __future__ import annotations

import math
from dataclasses import dataclass

from cockpit_io import finite_number
from cost_schema import valid_event_time
from droid_ledger import (
    droid_baseline, droid_remaining_frac, droid_reset_bounds, droid_reset_is_coarse,
    droid_start_uncertain, droid_window_start, quota_stale,
)
from grok_costs import UNPLACED_UID
from quota_sources import DEFAULT_CODEX_LIMIT, full_unstarted, merge_codex_quota

HOURS_5 = 18000
WEEK = 604800
_MIN_SAMPLE_AGE = 300
_MIN_DELTA_USED = 0.05
EVENT_TIMING_REV = 2
_DEFAULT_WINDOWS = {
    "gemini-5h": HOURS_5, "gemini-weekly": WEEK, "3p-5h": HOURS_5, "3p-weekly": WEEK,
    "codex-5h": HOURS_5, "codex-weekly": WEEK, "grok-weekly": WEEK,
    "droid-5h": HOURS_5, "droid-weekly": WEEK, "droid-monthly": 30 * 86400,
}


def _quota_number(value):
    # Legacy quota snapshots coerce booleans and use zero for missing/invalid fields.
    number = finite_number(int(value) if isinstance(value, bool) else value)
    return number if number is not None else 0.0


def _legacy_codex(bid, info):
    return (bid in ("codex-5h", "codex-weekly") and "window_minutes" not in info
            and info.get("limit_id") in (None, DEFAULT_CODEX_LIMIT))


def cached_event_mtime(key, mtime, timing_rev):
    # Re-read extant logs after the timestamp fix; retain history whose logs are gone.
    changed = timing_rev != EVENT_TIMING_REV and key.startswith(("agy_tr:", "grok:"))
    return float(mtime) - int(changed)


@dataclass(frozen=True)
class DeltaSample:
    base_ts: float
    used_frac: float


def _droid_delta(info, bid, observed, now):
    baseline = droid_baseline(info, observed)
    rem = droid_remaining_frac(info.get("rem_frac"))
    if baseline is None or rem is None:
        return None
    base_ts, base_rem = baseline
    # Preserve an exact 5-point threshold despite binary subtraction noise.
    return DeltaSample(base_ts, round(base_rem - rem, 15))


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    observed: float
    reset: float
    scope: bool
    boundary_known: bool
    trusted: bool
    limit_id: str | None
    delta: DeltaSample | None


def quota_window(bid, info, now):
    info = info if isinstance(info, dict) else {}
    reset = _quota_number(info.get("reset_ts"))
    raw_observed = info.get("observed_at")
    observed = _quota_number(raw_observed)
    if not bid.startswith("droid-") and (type(raw_observed) not in (int, float) or not valid_event_time(observed, now)):
        observed = 0.0
    length = _DEFAULT_WINDOWS.get(bid, 0)
    end = math.inf
    scoped = True
    known_start = False
    delta = None
    if bid.startswith("droid-"):
        floor, reset = droid_reset_bounds(info, bid)
        start = droid_window_start(info or None, bid, now)
        known_start = floor > now and math.isfinite(start) and not droid_start_uncertain(info, bid)
        if reset > now:
            end = reset
        if not known_start and 0 < observed <= now < floor and observed < reset:
            delta = _droid_delta(info, bid, observed, now)
    elif bid == "grok-weekly":
        official_start = _quota_number(info.get("period_start_ts"))
        known_start = 0 < official_start < reset and official_start <= now
        start = official_start if known_start else (reset - WEEK if reset > 0 else now - WEEK)
        if reset > 0:
            end = reset
        elif full_unstarted(info):
            start = max(start, observed, official_start if 0 < official_start <= now else 0.0)
    else:
        legacy = _legacy_codex(bid, info)
        if bid.startswith("codex-"):
            minutes = _quota_number(info.get("window_minutes"))
            scoped = legacy or (minutes > 0 and info.get("limit_id") == DEFAULT_CODEX_LIMIT
                                and info.get("cost_scope", False))
            if minutes > 0:
                length = minutes * 60
        known_start = not legacy and 0 < length and now < reset <= now + length
        start = reset - length if known_start else now - length
        if known_start:
            end = reset
        elif length > 0 and not legacy and observed and full_unstarted(info):
            start = max(observed, start)
    trusted = (scoped and known_start and reset > now and valid_event_time(observed, now)
               and observed - start >= _MIN_SAMPLE_AGE and observed < reset)
    return Window(start, end, observed, reset, scoped, known_start, trusted, info.get("limit_id"), delta)


class WindowCosts:
    def __init__(self, quota, now):
        self.now = now
        bids = dict.fromkeys((*_DEFAULT_WINDOWS, *(key for key in quota if key.startswith("codex-window-"))))
        self.windows = {bid: quota_window(bid, quota.get(bid), now) for bid in bids}
        self.totals = {bid: 0.0 for bid, window in self.windows.items() if window.scope}
        self.totals.update({"5h": 0.0, "weekly": 0.0})
        self.estimates = {bid: (window, 0.0) for bid, window in self.windows.items()
                          if window.trusted or window.delta is not None}
        self._unplaced = set()
        self._providers = {provider: tuple(bid for bid in self.totals if bid.startswith(provider + "-"))
                           for provider in ("gemini", "3p", "codex", "droid", "grok")}

    def add(self, source, family, ts, cost, uid):
        if ts >= self.now - WEEK:
            self.totals["weekly"] += cost
        if ts >= self.now - HOURS_5:
            self.totals["5h"] += cost

        if source in ("db", "agy_tr"):
            source = {"gemini": "gemini", "claude": "3p"}.get(family)
        for bid in self._providers.get(source, ()):
            window = self.windows[bid]
            in_window = window.start <= ts < window.end
            if in_window:
                self.totals[bid] += cost
                if uid == UNPLACED_UID:
                    self._unplaced.add(bid)
            if ts <= window.observed and ((window.trusted and in_window) or
                                          (window.delta is not None and window.delta.base_ts < ts)):
                _, amount = self.estimates[bid]
                self.estimates[bid] = (window, amount + cost)

    def eligible_estimates(self):
        return {bid: value for bid, value in self.estimates.items() if bid not in self._unplaced}


def _delta_status_fields(window, delta_usd):
    delta = window.delta
    fields = {"estimate_mode": "delta", "estimate_delta_pct": max(0.0, delta.used_frac * 100),
              "estimate_total_usd": None}
    if (window.observed - delta.base_ts >= _MIN_SAMPLE_AGE
            and delta.used_frac >= _MIN_DELTA_USED and delta_usd > 0):
        total = delta_usd / delta.used_frac
        if math.isfinite(total):
            fields.update(estimate_ok=True, estimate_usd=delta_usd,
                          estimate_used_frac=delta.used_frac, estimate_total_usd=total)
    return fields


def quota_status_fields(bid, info, now, estimate):
    window = quota_window(bid, info, now)
    stale = quota_stale({**info, "observed_at": window.observed,
                         "reset_ts": info.get("reset_ts") if bid.startswith("droid-") else window.reset}, now, bid)
    aligned = estimate is not None and window == estimate[0]
    estimate_ok = aligned and window.trusted
    return {
        "pct": max(0.0, min(1.0, _quota_number(info.get("rem_frac")))) * 100,
        "observed_at": window.observed, "stale": stale,
        "reset_sec": max(0, math.ceil(window.reset - now)),
        "estimate_ok": estimate_ok, "estimate_usd": estimate[1] if estimate_ok else None,
        "reset_is_estimate": bid.startswith("droid-") and droid_reset_is_coarse(info, bid),
        "window_boundary_uncertain": not window.boundary_known,
        **(_delta_status_fields(window, estimate[1] if aligned else 0.0) if window.delta is not None else {}),
        **({"usd_scope": "legacy_rolling"} if _legacy_codex(bid, info) else {}),
        **{key: info[key] for key in ("window_label", "window_minutes", "limit_id", "period_start_ts", "period_type") if key in info},
    }


def attach_quota_costs(out, active_bids, costs):
    for bid in dict.fromkeys((*active_bids, *out)):
        cell = out.setdefault(bid, {})
        if bid in costs:
            cell["usd"] = float(costs[bid])
    out["cost-5h"] = {"usd": float(costs.get("5h", 0.0))}
    out["cost-weekly"] = {"usd": float(costs.get("weekly", 0.0))}
    return out


def merge_quota_cache(previous, parsed):
    out = dict(previous)
    merge_codex_quota(out, parsed)
    return out
