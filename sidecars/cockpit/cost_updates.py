"""Reconcile source scans off-lock and commit only against their input version."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from cost_archive import CompactedRows, NormalizedCostRows, cost_ticks
from cost_compaction import is_droid_probe, restore_entry
from cost_context import CostContext
from cost_migration import merge_history, scan_update
from droid_ledger import DroidLedger


def update_db(state: CostContext, cid, mtime, events):
    key = f"db:{cid}"
    with state.cost_lock, state.builtin_enum_lock:
        previous = state.files.get(key)
        pending = key in state.pending_rebuild
        damaged = key in state.runtime.damaged
        enum = int(state.builtin_models.get(cid) or 0)
        applied = state.db_applied_builtin.get(cid, 0)
    if previous and previous[0] > mtime:
        return
    original = None if damaged else restore_entry(previous)
    if original and abs(original[0] - mtime) < .001 and original[1] == events and applied == enum:
        return
    if pending and not damaged:
        events, _ = merge_history(original, events)
    with state.cost_lock, state.builtin_enum_lock:
        if (state.files.get(key) is not previous or pending != (key in state.pending_rebuild)
                or damaged != (key in state.runtime.damaged)
                or enum != int(state.builtin_models.get(cid) or 0)):
            return
        # Let the canonical scan replace a damaged artifact and release the save guard.
        if damaged:
            return
        state.files[key] = (mtime, NormalizedCostRows(events))
        state.generation += 1
        state.db_applied_builtin[cid] = enum


@dataclass
class ScanChange:
    key: str
    previous: tuple | None
    entry: tuple
    pending: bool
    pending_next: bool
    damaged: bool
    complete: bool
    replace: bool
    dirty: bool
    droid: bool
    watermark: dict | None
    next_mark: dict | None


def _covers_visible_history(previous, replacement, normalize):
    """A readable but truncated source must not unlock a damaged snapshot."""
    if not previous or not previous[1]:
        return True

    def buckets(rows):
        totals = defaultdict(lambda: [0] * 6)
        for item in rows:
            ts, cost, family, _, inp, cache, out, turns, cw, _ = normalize(item)
            if ts > 0:
                cell = totals[datetime.fromtimestamp(ts).date(), family]
                for i, value in enumerate((cost_ticks(cost), inp, cache, out, turns, cw)):
                    cell[i] += value
        return totals

    try:
        observed = buckets(replacement)
        return all(all(actual >= known for actual, known in zip(observed[key], amounts))
                   for key, amounts in buckets(previous[1]).items())
    except (ValueError, OverflowError, OSError):
        return False


def prepare_scan_updates(state: CostContext, updates, scan_start):
    with state.cost_lock:
        inputs = [(key, mt, rows, state.files.get(key), key in state.pending_rebuild,
                   key in state.runtime.damaged, state.droid_ledger.watermarks.get(key))
                  for key, (mt, rows) in updates.items()]
    results = []
    for key, mtime, rows, previous, pending, damaged, mark in inputs:
        if previous is not None and previous is not scan_start.get(key) and mtime < previous[0] + .001:
            # A callback won the race, potentially with more facts at the same mtime.
            continue
        droid = key.startswith("droid:")
        preserve = key.startswith("agy_tr:") and previous and isinstance(previous[1], CompactedRows)
        rebuilding = pending or bool(preserve)
        original = (None if damaged else restore_entry(previous)
                    if droid or rebuilding else previous)
        ledger = DroidLedger()
        if droid and mark is not None and not damaged:
            ledger.watermarks[key] = mark
        result, complete, _ = scan_update(key, original, rows, rebuilding, ledger)
        if damaged and (not complete or not _covers_visible_history(previous, result, state.normalize)):
            continue
        mark_after = ledger.watermarks.get(key)
        entry = mtime, result
        replace = damaged or previous != entry or (previous and isinstance(previous[1], CompactedRows)
                                                    and original is not previous)
        dirty = (mark != mark_after or damaged or
                 bool(replace and (not droid or previous is None or previous[1] != result)))
        results.append(ScanChange(key, previous, entry, pending, rebuilding and not complete, damaged,
                                  complete, bool(replace), dirty, droid, mark, mark_after))
    return results


def commit_scan_updates(state: CostContext, prepared):
    """Called with the reader lock; no archive or JSON work occurs here."""
    dirty, committed = False, set()
    for change in prepared:
        key = change.key
        marks = state.droid_ledger.watermarks
        if (state.files.get(key) is not change.previous
                or (key in state.pending_rebuild) != change.pending
                or (key in state.runtime.damaged) != change.damaged
                or (change.droid and marks.get(key) is not change.watermark)):
            continue
        if change.replace:
            state.files[key] = change.entry
        if change.droid:
            if change.next_mark is None:
                marks.pop(key, None)
            else:
                marks[key] = change.next_mark
        if change.pending_next:
            state.pending_rebuild.add(key)
        else:
            state.pending_rebuild.discard(key)
        if change.damaged and change.complete:
            state.runtime.damaged.discard(key)
        dirty |= change.dirty or change.pending != change.pending_next
        committed.add(key)
    return dirty, committed


def scan_droid_file(state: CostContext, path, reusable, previous, calendar_cutoff):
    runtime, ledger = state.runtime, state.droid_ledger
    jl = path.with_name(path.name[:-len(".settings.json")] + ".jsonl")
    stamp = (path.stat().st_mtime, jl.stat().st_mtime if jl.exists() else 0)
    mtime = max(stamp)
    if mtime < calendar_cutoff:
        return None
    key = f"droid:{path}"
    probe = is_droid_probe(path)
    cached = reusable.get(key)
    if probe and runtime.known_empty_probe(key, stamp):
        return None
    if cached and abs(cached[0] - mtime) < .001 and (
            ledger.has_usage(key) or (cached[1] and ledger.has(key)) or (not probe and not cached[1])):
        return None
    metrics = state.analyze_droid(path)
    if metrics is None:
        return None
    if (probe and key not in runtime.damaged and not ledger.has_usage(key)
            and not (previous.get(key) or (0, ()))[1] and runtime.remember_empty_probe(key, stamp, metrics)):
        return None
    return key, (mtime, metrics)
