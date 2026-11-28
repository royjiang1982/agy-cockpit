"""Reconcile each source with its legacy ledger, without estimating missing history."""
from __future__ import annotations

from collections import defaultdict

from cost_archive import cost_ticks
from cost_schema import CostEvent
from grok_costs import BILLED_UID, UNPLACED_UID
from metric_pi import PI_MTIME_UID


def pending_keys(cache, current_schema=18):
    files = cache.get("files") or {}
    keys = files if cache.get("schema_version", current_schema) < current_schema else cache.get("pending_rebuild", ())
    return ({key for key in keys if isinstance(key, str) and key in files}
            if isinstance(files, dict) and isinstance(keys, (dict, list, tuple)) else set())


def legacy_droid_keys(cache):
    # Only v15-era rebuilds translate an assistant-call cursor into a turn cursor.
    # The wider model backfill must not change the current, already aligned cursor.
    return pending_keys(cache, 16) if cache.get("schema_version") in (15, 16, 17) else ()


def _stamped_usage(row):
    # Price, turn flags and cache-write breakdown may change on re-analysis.
    return (round(row[0], 3), row[2], *row[4:7])


def _step_digest(row):
    legacy, sep, evidence = row[3].partition("#")
    return (legacy.partition(":")[0], evidence.partition("@")[0]) if sep and evidence else None


def _grok_amounts(rows):
    amounts = [0] * 6
    for row in rows:
        for i, value in enumerate((cost_ticks(row[1]), *row[4:8], row[8] if len(row) > 8 else 0)):
            amounts[i] += value
    return amounts


def merge_history(previous, replacement, *, grok=False, pi=False):
    """Match occurrences strongest evidence first, retaining unavailable history."""
    if not previous or not previous[1]:
        return replacement, True
    old = previous[1]
    used = set()
    matched = set()

    def reconcile(old_key, new_key):
        candidates = defaultdict(list)
        for i, row in enumerate(replacement):
            if i not in used and (key := new_key(row)) is not None:
                candidates[key].append(i)
        for i, row in enumerate(old):
            if i not in matched and (slots := candidates.get(old_key(row))):
                used.add(slots.pop())
                matched.add(i)

    # Ignore fallback file mtimes for fingerprinted DB calls. Pre-fingerprint
    # DB/transcript UIDs contain the step, counters and start timestamp.
    reconcile(lambda r: r[3].partition("#")[2] or None,
              lambda r: r[3].partition("#")[2] or None)
    # A more complete measured clock in the same DB does not create a new call.
    reconcile(_step_digest, _step_digest)
    reconcile(lambda r: r[3] if r[3].count(":") == 4 and "#" not in r[3] else None,
              lambda r: r[3].partition("#")[0])
    reconcile(lambda r: (r[3], _stamped_usage(r)) if "#" not in r[3] and r[3].count(":") != 4 else None,
              lambda r: ("" if "#" in r[3] else r[3], _stamped_usage(r)))
    if grok:
        # Only billed turn updates are evidence of the old seconds-only clock.
        # Reserve exact matches first; summary mtimes and untagged rows are not evidence.
        def billed_second(row):
            if row[3] == BILLED_UID:
                return int(row[0]), round(row[1], 8), row[2], *row[4:7]
        reconcile(lambda r: billed_second(r) if float(r[0]).is_integer() else None,
                  lambda r: billed_second(r) if not float(r[0]).is_integer() else None)
        # Dated rows can replace an earlier cumulative, unplaced remainder.
        # Already matched occurrences cannot also cover that remainder.
        unplaced = [i for i, row in enumerate(old) if i not in matched and row[3] == UNPLACED_UID]
        if unplaced:
            available = [row for i, row in enumerate(replacement) if i not in used]
            if not any(row[3] == UNPLACED_UID for row in available):
                end = max(old[i][0] for i in unplaced)
                available = [row for row in available if row[0] <= end + .001]
            before = _grok_amounts(old[i] for i in unplaced)
            observed = _grok_amounts(available)
            if all(new >= known for known, new in zip(before, observed)):
                matched.update(unplaced)
    if pi:
        def usage(row):
            return row[2], *row[4:7], row[8] if len(row) > 8 else 0

        reconcile(lambda r: usage(r) if r[3] == PI_MTIME_UID or
                  (not r[3] and round(r[0], 3) == round(previous[0], 3)) else None,
                  lambda r: usage(r) if r[3] == PI_MTIME_UID else None)
    missing = [row for i, row in enumerate(old) if i not in matched]
    return missing + replacement, not missing


def scan_update(key, previous, replacement, rebuilding, ledger):
    """Return rows, source-reconciled flag, and whether its watermark changed."""
    if not key.startswith("droid:"):
        rows, complete = (merge_history(previous, replacement, grok=key.startswith("grok:"), pi=key.startswith("pi:"))
                          if rebuilding else (replacement, True))
        return rows, complete, False
    if rebuilding and not replacement.get("_droid_usage"):
        return (previous[1] if previous else []), False, False
    mark = ledger.watermarks.get(key, {})
    if rebuilding and mark.get("legacy_turns") and not ledger.align_legacy_turns(key, replacement["_droid_usage"]):
        return (previous[1] if previous else []), False, False
    if rebuilding and previous and not ledger.has(key):
        # A cumulative ledger without a cursor may describe an entirely new
        # segment. First record that segment, then reconcile visible overlap.
        visible, dirty = ledger.apply(key, replacement, None)
        rows, _ = merge_history(previous, visible)
    else:
        rows, dirty = ledger.apply(key, replacement, previous)
    if rebuilding:
        rows = _enrich_droid_models(rows, replacement.get("cost_events", ()))
    complete = not rebuilding or not any(
        (len(row) < 10 or not row[9]) and (row[1] or any(row[4:9])) for row in rows)
    return rows, complete, dirty or (rebuilding and bool(mark.get("legacy_turns")))


def _enrich_droid_models(previous, visible):
    """Overlay names only: a snapshot's cumulative allocations cannot reprice a delta."""
    exact, at_time = defaultdict(set), defaultdict(set)
    for row in visible:
        if len(row) > 9 and row[9]:
            at_time[row[0], row[2]].add(row[9])
            exact[_stamped_usage(row), row[7:9]].add(row[9])
    result = []
    for row in previous:
        if len(row) > 9 and not row[9]:
            names = exact.get((_stamped_usage(row), row[7:9])) or at_time.get((row[0], row[2]), ())
            if len(names) == 1:
                row = CostEvent(*row[:9], next(iter(names)))
        result.append(row)
    return result
