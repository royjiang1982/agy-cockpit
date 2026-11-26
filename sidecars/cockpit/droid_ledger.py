"""Droid cumulative-usage ledger and conservative, persisted quota-cycle anchors.

Ledger mutations run under the server's cost lock; no file I/O or probes live here.
Factory has no per-turn usage: only newly observed deltas may be distributed.
A single day-only countdown can undercount the first day. Use the conservative
upper bound until successive probes narrow the reset enough to recover the start.
"""
from __future__ import annotations

import calendar
import math
import re
from datetime import datetime, timezone

from pricing import calc_call_cost, canonical_model_label, model_quota_family

_TOKENS = ("raw_inp", "cw", "cache", "out")
_WINDOWS = {"droid-5h": 18000, "droid-weekly": 604800, "droid-monthly": 2592000}
_TTL = 1800
_DURATION = re.compile(
    r"(\d+)\s*(days?|d|天|hours?|h|小时|minutes?|mins?|m|分钟|seconds?|secs?|s|秒)", re.I)


def _refresh_pending(mark, anchors, rejected):
    """Update only unbilled turns; never amend an already priced delta."""
    visible = {key: (stamp, model) for key, stamp, model in anchors}
    pending = []
    entries = mark.get("pending_cost", ())
    for i, entry in enumerate(entries):
        if len(entry) == 3:
            key, stamp, model = entry
        else:
            # Older caches stored only (time, model). Prefer an exact anchor;
            # otherwise use its position in the previously seen unbilled tail.
            stamp, model = entry
            pos = max(0, mark["seen_turns"] - len(entries)) + i
            key = next((k for k, t, m in anchors if (t, m) == (stamp, model)),
                       f"slot:{pos}")
        if key.startswith("slot:"):
            pos = int(key[5:])
            if 0 <= pos < len(anchors):
                key = anchors[pos][0]
        if key not in rejected:
            pending.append((key, *visible.get(key, (stamp, model))))
    return pending


class DroidLedger:
    def __init__(self):
        self.watermarks = {}

    def has(self, key):
        return key in self.watermarks

    def load(self, raw, legacy_turns=()):
        self.watermarks = {}
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            try:
                mark = {k: max(0, int(value[k])) for k in (*_TOKENS, "seen_turns")}
                mark["last_ts"] = float(value["last_ts"])
                mark["synthetic_pending"] = max(0, int(value.get("synthetic_pending", 0)))
                if key in legacy_turns or value.get("legacy_turns") is True:
                    mark["legacy_turns"] = True
                mark["pending_cost"] = [(*map(str, entry[:-2]), float(entry[-2]), str(entry[-1]))
                                        for entry in value.get("pending_cost", []) if len(entry) in (2, 3)]
                if str(key).startswith("droid:") and math.isfinite(mark["last_ts"]):
                    self.watermarks[str(key)] = mark
            except (KeyError, TypeError, ValueError, OverflowError):
                continue

    def has_usage(self, key):
        mark = self.watermarks.get(key, {})
        return bool(any(mark.get(token) for token in _TOKENS) or mark.get("seen_turns")
                    or mark.get("pending_cost") or mark.get("synthetic_pending"))

    def dump(self, files):
        return {k: dict(v) for k, v in self.watermarks.items() if k in files and self.has_usage(k)}

    def prune(self, files):
        saved = self.dump(files)
        changed = len(saved) != len(self.watermarks)
        if changed:
            self.watermarks = saved
        return changed

    def align_legacy_turns(self, key, usage):
        """Translate the v15 assistant-call cursor, not the immutable billed rows."""
        mark = self.watermarks[key]
        turns = usage["turns"]
        if not turns:
            if mark["seen_turns"]:
                return False  # Retry when the transcript becomes readable.
            self.watermarks[key] = {k: v for k, v in mark.items() if k != "legacy_turns"}
            return True
        calls = usage.get("first_assistant_calls", ())
        seen = 0
        for i, (ts, _) in enumerate(turns):
            first = calls[i] if i < len(calls) else None
            # A turn may finish after the v15 last-call timestamp. If its first
            # assistant call was counted, it is historical nonetheless.
            if first is not None:
                ordinal, stamp = first
                if ordinal > mark["seen_turns"] or (stamp and stamp > mark["last_ts"] + .001):
                    break
            elif ts > mark["last_ts"] + .001:
                break
            seen += 1
        self.watermarks[key] = dict(mark, seen_turns=seen)
        self.watermarks[key].pop("legacy_turns", None)
        return True

    def apply(self, key, metrics, previous):
        """Report actual watermark changes, not mere rescans/mtime changes."""
        before = self.watermarks.get(key)
        events = self.record(key, metrics, previous)
        return events, before != self.watermarks.get(key)

    def record(self, key, metrics, previous):
        """Return complete immutable-history events and commit the matching watermark."""
        old_events = list(previous[1]) if previous else []
        usage = metrics.get("_droid_usage")
        if not usage:
            return old_events if previous else list(metrics.get("cost_events") or [])
        totals = {k: max(0, int(usage[k])) for k in _TOKENS}
        turns = usage["turns"]
        ids = usage.get("turn_ids", ())
        anchors = [(ids[i] if i < len(ids) else f"slot:{i}", t, m) for i, (t, m) in enumerate(turns)]
        rejected = {anchors[i][0] for i in usage.get("rejected_indices", ()) if 0 <= i < len(anchors)}
        ts = max(float(usage["event_ts"]), float(metrics["mtime"]))
        mark = self.watermarks.get(key) if previous else None
        if mark is None and previous and old_events:
            # Schema-15 has combined input only. Preserve its historical costs;
            # attribute existing cache writes to history before pricing new input.
            inp, cache, out, seen = (sum(int(e[i]) for e in old_events) for i in range(4, 8))
            cw = min(totals["cw"], inp)
            mark = dict(raw_inp=inp - cw, cw=cw, cache=cache, out=out,
                        seen_turns=min(seen, len(turns)), last_ts=max(e[0] for e in old_events))
            unknown_usage = not (inp or cache or out) and any(e[1] > 0 for e in old_events)
            if abs(previous[0] - metrics["mtime"]) < .001 or unknown_usage:
                mark.update(totals)
                if unknown_usage:
                    mark["seen_turns"] = len(turns)
            self.watermarks[key] = mark
        reset = mark and any(totals[k] < mark[k] for k in _TOKENS)
        if reset:
            # Only a disjoint, dated transcript proves a new segment. A short or
            # unchanged transcript can accompany a transient settings flush.
            first_ts = usage.get("first_turn_ts", turns[0][0] if turns else 0)
            if not (mark["seen_turns"] and turns and first_ts and first_ts > mark["last_ts"]):
                return old_events
            self.watermarks[key] = dict(totals, seen_turns=len(turns),
                                        last_ts=max(t for t, _ in turns), synthetic_pending=0)
            return old_events
        if mark is None:
            # First observation: retain the analyzer's full-session contract.
            # A zero watermark is valid, but must not consume still-unbilled turns.
            measured = any(totals.values())
            last = max((t for t, _ in turns), default=0)
            self.watermarks[key] = dict(totals, seen_turns=len(turns),
                                        last_ts=last, synthetic_pending=0, pending_cost=[] if measured else
                                        [r for r in anchors if r[0] not in rejected])
            return list(metrics.get("cost_events") or [])
        mark = dict(mark, synthetic_pending=mark.get("synthetic_pending", 0))
        delta = {k: totals[k] - mark[k] for k in _TOKENS}
        # A missing/short transcript is not a token reset. Retain its old index.
        start = mark["seen_turns"]
        fresh = turns[start:]
        added = [(round(t, 3), 0., model_quota_family(m), "", 0, 0, 0, 1, 0,
                  canonical_model_label(m)) for t, m in fresh]
        # Counting a turn doesn't consume its billing anchor. Keep the dated
        # model until a settings delta arrives, including across cache reloads.
        pending = _refresh_pending(mark, anchors, rejected)
        mark["seen_turns"] += len(fresh)
        billable = [r for r in anchors[start:] if r[0] not in rejected]
        claimed = min(mark["synthetic_pending"], len(billable))
        mark["synthetic_pending"] -= claimed
        pending.extend(billable[claimed:])
        if any(delta.values()):
            if not pending:
                mark["synthetic_pending"] += 1
                pending = [("", max(ts, mark["last_ts"]), usage["model"])]
            parts = {k: divmod(v, len(pending)) for k, v in delta.items()}
            for i, (_, stamp, model) in enumerate(pending):
                raw, cw, cache, out = (q + int(i < r) for q, r in (parts[k] for k in _TOKENS))
                cost = calc_call_cost(model, raw, cache, out, cw, event_ts=stamp)
                added.append((round(stamp, 3), cost, model_quota_family(model), "",
                              raw + cw, cache, out, 0, cw, canonical_model_label(model)))
            pending = []
        mark.update(totals, last_ts=max([mark["last_ts"]] + [t for t, _ in turns]), pending_cost=pending)
        self.watermarks[key] = mark
        return old_events + added


def _durations(tail):
    for match in _DURATION.finditer(tail):
        unit = match[2].lower()
        scale = (86400 if unit.startswith("d") or unit == "天" else
                 3600 if unit.startswith("h") or unit == "小时" else
                 60 if unit.startswith("m") or unit == "分钟" else 1)
        yield int(match[1]), scale


def parse_droid_reset_sec(tail):
    return sum(n * scale for n, scale in _durations(tail))


def droid_reset_precision(tail):
    return min((scale for _, scale in _durations(tail)), default=0)


def quota_stale(info, now, bid=""):
    observed = float(info.get("observed_at") or 0)
    reset = droid_reset_bounds(info, bid)[1] if bid.startswith("droid-") else float(info.get("reset_ts") or 0)
    rem_frac = info.get("rem_frac")
    full_unstarted = (
        bid == "droid-5h"
        and reset <= 0
        and rem_frac is not None
        and float(rem_frac) >= 0.9995
    )
    return (bool(info.get("stale")) or not observed or now - observed > _TTL
            or (0 < reset <= now)
            or (bid.startswith("droid-") and reset <= 0 and not full_unstarted))


def droid_probe_due(cache, now, force=False):
    return force or any(bid not in cache or quota_stale(cache[bid], now, bid) for bid in _WINDOWS)


def _precision(info, bid):
    if "reset_precision_sec" in info:
        return float(info["reset_precision_sec"] or 0)
    # Old view_state entries lack precision. Week/month were day-truncated.
    return 86400.0 if bid != "droid-5h" else 60.0


def droid_reset_bounds(info, bid):
    reset = float(info.get("reset_ts") or 0)
    if not math.isfinite(reset) or reset <= 0:
        return 0.0, 0.0
    if "reset_floor_ts" in info:
        return min(reset, float(info["reset_floor_ts"] or reset)), reset
    precision = _precision(info, bid)
    if "win_start_ts" in info:
        return max(0.0, reset - precision), reset
    return reset, reset + precision


def _reset_tolerance(bid):
    return 60 if bid == "droid-5h" else 1800


def _reset_width(floor, upper):
    return max(0.0, upper - floor)


def _previous_month(ts):
    dt = datetime.fromtimestamp(ts, timezone.utc)
    year, month0 = divmod(dt.year * 12 + dt.month - 2, 12)
    prev_last = calendar.monthrange(year, month0 + 1)[1]
    day = prev_last if dt.day == calendar.monthrange(dt.year, dt.month)[1] else min(dt.day, prev_last)
    return dt.replace(year=year, month=month0 + 1, day=day).timestamp()


def _start_from_upper(upper, bid):
    return _previous_month(upper) if bid == "droid-monthly" else upper - _WINDOWS[bid]


def _initial_start(info, bid, observed):
    _, upper = droid_reset_bounds(info, bid)
    return min(observed, _start_from_upper(upper, bid)) if upper > 0 else observed


def droid_window_start(info, bid, now):
    """Never fall back to a rolling window, including expired or 31-day months."""
    if not isinstance(info, dict):
        return now  # Unknown cycle: no justified current-cycle spend yet.
    observed = float(info.get("observed_at") or now)
    _, upper = droid_reset_bounds(info, bid)
    if upper <= 0:
        return observed
    start = float(info.get("win_start_ts") or _initial_start(info, bid, observed))
    return max(start, upper) if upper <= now else start


def droid_start_uncertain(info, bid):
    floor, upper = droid_reset_bounds(info, bid)
    tolerance = _reset_tolerance(bid)
    if not upper or _reset_width(floor, upper) > tolerance:
        return True
    observed = float(info.get("observed_at") or 0)
    expected = _start_from_upper(upper, bid)
    start = float(info.get("win_start_ts") or expected)
    # A stale uncertainty flag is not evidence against an aligned, narrow interval.
    return not (0 < start < observed and abs(start - expected) <= tolerance)


def droid_reset_is_coarse(info, bid):
    floor, upper = droid_reset_bounds(info, bid)
    return upper > 0 and _reset_width(floor, upper) > _reset_tolerance(bid)


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def droid_remaining_frac(value):
    rem = _finite_number(value)
    return rem if rem is not None and 0 <= rem <= 1 else None


def droid_baseline(info, observed):
    ts = _finite_number(info.get("base_ts"))
    rem = droid_remaining_frac(info.get("base_rem_frac"))
    if ts is not None and 0 < ts <= observed and rem is not None:
        return ts, rem
    return None


def _previous_baseline(old, bid, now, old_end, old_rem):
    old_obs = old["observed_at"]
    if not 0 < old_obs <= now or old_obs >= old_end:
        return None
    baseline = droid_baseline(old, old_obs)
    if baseline is None and "base_ts" not in old and old_rem is not None and not quota_stale(old, now, bid):
        return old_obs, old_rem
    return baseline


def anchor_droid_quota(current, previous, now):
    """Anchor newly parsed dictionaries only; a partial probe must not freshen others."""
    result = dict(current)
    for bid in _WINDOWS:
        item = current.get(bid)
        old = previous.get(bid)
        if item is None or item is old:
            continue
        rem = droid_remaining_frac(item.get("rem_frac"))
        if rem is None:
            if old is None:
                result.pop(bid, None)
            else:
                result[bid] = old
            continue
        item = dict(item, observed_at=now, base_ts=now, base_rem_frac=rem)
        precision = _precision(item, bid)
        floor = float(item.get("reset_ts") or (now if precision else 0))
        if not floor:
            result[bid] = item
            continue
        upper = floor + precision
        start = min(now, _start_from_upper(upper, bid))
        tolerance = _reset_tolerance(bid)
        uncertain = _reset_width(floor, upper) > tolerance or start >= now
        if old:
            old = dict(old, observed_at=_finite_number(old.get("observed_at")) or 0.0)
            old_floor, old_end = droid_reset_bounds(old, bid)
            old_precision = _precision(old, bid)
            old_rem = droid_remaining_frac(old.get("rem_frac"))
            rem_jump = rem > (old_rem or 0) + .05
            rollover_tolerance = max(precision, old_precision, 600 if bid == "droid-5h" else 3600)
            # Consumption contradicts a weak countdown-only rollover signal.
            advanced = floor > old_end + rollover_tolerance and rem >= (old_rem or 0) - .005
            rollover = old_end <= now or rem_jump or advanced
            if not rollover:
                baseline = _previous_baseline(old, bid, now, old_end, old_rem)
                if baseline is not None:
                    item["base_ts"], item["base_rem_frac"] = baseline
                start = float(old.get("win_start_ts") or
                              _initial_start(old, bid, old["observed_at"] or now))
                # Backfill the first segment only when the intersection is narrow enough.
                lo, hi = max(old_floor, floor), min(old_end, upper)
                if lo <= hi:
                    floor, upper = lo, hi
                else:
                    floor, upper = old_floor, old_end
                uncertain = _reset_width(floor, upper) > tolerance
                if not uncertain:
                    expected = _start_from_upper(upper, bid)
                    if droid_start_uncertain(old, bid) or abs(start - expected) > tolerance:
                        start = min(now, expected)
                    uncertain = not 0 < start < now
            else:
                bound = min(now, old_end)
                uncertain = uncertain or bound - start > tolerance or bound >= now or (
                    bound > start and _reset_width(old_floor, old_end) > tolerance)
                start = max(start, bound)
        item.update(reset_ts=upper, reset_floor_ts=floor, win_start_ts=start,
                    win_start_uncertain=uncertain, win_start_checked=True)
        result[bid] = item
    return result
