"""Fold quota-inactive rows while preserving the inputs to exact reconciliation."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from cost_archive import (
    COMPACTED_UID, CompactedRows, NormalizedCostRows, cost_ticks, cost_usd,
    load_compacted_rows, pack_rows, persisted_compaction, timezone_key,
)
from cost_windows import WindowCosts, valid_event_time

_DAY = 86400
_PROBE_DIRS = {"-tmp-agy-cockpit-droid-probe", "-private-tmp-agy-cockpit-droid-probe"}
_AGY = ("db:", "agy_tr:")
_PROVIDERS = {"db": ("gemini-", "3p-"), "agy_tr": ("gemini-", "3p-"),
              "codex": ("codex-",), "grok": ("grok-",), "droid": ("droid-",), "pi": ()}


def _prefixes(rows):
    if isinstance(rows, CompactedRows):
        return (rows.digests[i:i+16] for i in range(0, len(rows.digests), 16))
    return (_digest(row) for row in rows)

def _digest(row):
    if len(row) >= 4:
        digest = str(row[3]).partition("#")[2].partition("@")[0]
        return digest[:16].ljust(16, "!") if digest else ""
    return ""

def _index(rows):
    return "".join(d for d in _prefixes(rows) if len(d) == 16)



def safe_cutoffs(quota, now):
    windows = WindowCosts(quota, now).windows
    cutoffs = {}
    for source, providers in _PROVIDERS.items():
        times = [now - (35 if source == "droid" else 10) * _DAY]
        for bid, window in windows.items():
            if providers and bid.startswith(providers):
                times.extend(ts - _DAY for ts in
                             (window.start, window.delta.base_ts if window.delta else math.inf)
                             if math.isfinite(ts))
        cutoffs[source] = min(times)
    return cutoffs


def _agy_groups(files):
    """Form connected components so no digest or DB/transcript edge is cut."""
    parents = {key: key for key in files if key.startswith(_AGY)}
    owners = {}

    def root(key):
        while parents[key] != key:
            parents[key] = key = parents[parents[key]]
        return key

    for key in parents:
        if isinstance(files[key], CompactedRows):
            for peer in files[key].links:
                if peer in parents:
                    parents[root(peer)] = root(key)
        if not key.startswith("db:"):
            continue
        peer = "agy_tr:" + key[3:]
        if peer in parents:
            parents[root(peer)] = root(key)
        for digest in _prefixes(files[key]):
            if digest:
                parents[root(owners.setdefault(digest, key))] = root(key)
    groups = defaultdict(set)
    for key in parents:
        groups[root(key)].add(key)
    return groups.values()


def exact_rows(files, cutoffs=None, zone=None):
    """Use short digests only as an index. On a match, use original identities.

    Blindly skipping on hash/count would lose separate measured clock groups,
    or keep the wrong billed representative if a new source has a better clock.
    Restore the entire linked component and let the original deduplicator decide.
    """
    if not any(isinstance(rows, CompactedRows) for _, rows in files):
        return files
    mapped = dict(files)
    zone = timezone_key() if zone is None else zone
    unfold = {key for key, rows in files if isinstance(rows, CompactedRows)
              and (rows.zone != zone or (cutoffs and any(
                  row[0] >= cutoffs.get(key.partition(":")[0], math.inf) for row in rows)))}
    if any(isinstance(rows, CompactedRows) and key.startswith(_AGY) for key, rows in files):
        for group in _agy_groups(mapped):
            folded = {key for key in group if isinstance(mapped[key], CompactedRows)}
            if folded and (folded != group or unfold & group or _lost_peer(folded, mapped)):
                unfold.update(folded)
    return [(key, rows.original() if key in unfold else rows) for key, rows in files]

def _lost_peer(folded, files):
    return any(peer not in files for key in folded for peer in files[key].links)


def restore_entry(entry):
    if entry and isinstance(entry[1], CompactedRows):
        return entry[0], entry[1].original()
    return entry


def should_rescan_transcript(key, cached, reusable, updates):
    if not cached or not isinstance(cached[1], CompactedRows):
        return False
    db = "db:" + key[7:]
    parent = reusable.get(db)
    return db in updates or (parent is not None and not isinstance(parent[1], CompactedRows))


def is_droid_probe(path):
    return not _PROBE_DIRS.isdisjoint(path.parts[:-1])


def source_exists(key, bases=()):
    source, _, name = key.partition(":")
    if source == "db":
        paths = (base / "conversations" / (name + ".db") for _, base in bases)
    elif source == "agy_tr":
        paths = (base / "brain" / name / ".system_generated" / "logs" / filename
                 for _, base in bases for filename in ("transcript_full.jsonl", "transcript.jsonl"))
    elif source in _PROVIDERS:
        paths = (Path(name),)
    else:
        return True
    try:
        return any(path.exists() for path in paths)
    except OSError:
        return True  # An unreadable path is not proof that it was deleted.

def _daily_fold(events, key, dedup, now):
    buckets = {}
    for ts, cost, family, uid, inp, cache, out, turns, cw, model in events:
        if not valid_event_time(ts, now) or dedup.skip(key, uid, ts):
            continue
        day = datetime.fromtimestamp(ts).date().toordinal()
        cell = buckets.setdefault((day, family, model), [ts, 0, 0, 0, 0, 0, 0])
        cell[0] = max(cell[0], ts)
        cell[1] += cost_ticks(cost)
        for i, value in enumerate((inp, cache, out, turns, cw), 2):
            cell[i] += value
    return ([(ts, cost_usd(cost), family, COMPACTED_UID, *usage, model)
             for (_, family, model), (ts, cost, *usage) in buckets.items()],
            [day for day, _, _ in buckets])


def plan_compaction(files, pending, quota, now, normalize, exists=source_exists, protected_empty=(), damaged=()):
    """Return changed entries (None means delete); never mutate the snapshot."""
    from agy_cost_events import AgyCostDedup

    cutoffs = safe_cutoffs(quota, now)
    zone = timezone_key()
    changes, eligible = {}, set()
    rows_by_key = {key: rows for key, (_, rows) in files.items()}
    missing_pending = {key for key in pending if key in files and not exists(key)}

    def old(key, rows):
        cutoff = cutoffs.get(key.partition(":")[0])
        return (cutoff is not None and key not in damaged and files[key][0] < cutoff
                and (key not in pending or key in missing_pending)
                and all(float(row[0]) < cutoff and (len(row) < 4 or row[3] != COMPACTED_UID) for row in rows))

    for key, (_, rows) in files.items():
        if isinstance(rows, CompactedRows):
            continue  # An empty folded AGY record can still carry live dedup evidence.
        if not rows:
            if key not in damaged and (not exists(key) or
                    (key not in protected_empty and key.startswith("droid:") and is_droid_probe(Path(key[6:])))):
                changes[key] = None
        elif old(key, rows):
            # Future-skewed events, even if currently invalid, may become valid later.
            eligible.add(key)

    groups = _agy_groups(rows_by_key)
    for group in groups:
        wrapped = {key for key in group if isinstance(rows_by_key[key], CompactedRows)}
        if not (group & eligible or _lost_peer(wrapped, rows_by_key)):
            continue
        if any(key not in eligible and (rows_by_key[key] or not old(key, ())) for key in group - wrapped):
            eligible.difference_update(group)
            continue
        expanded = {key: rows_by_key[key].original() for key in wrapped}
        if not all(old(key, rows) for key, rows in expanded.items()):
            eligible.difference_update(group)
            continue
        rows_by_key.update(expanded)
        eligible.update(expanded)
    if not eligible:
        return changes

    dedup = AgyCostDedup(list(rows_by_key.items()), now)
    folded = {}
    missing_evidence = set()
    for key in eligible:
        raw = rows_by_key[key]
        events = raw if isinstance(raw, NormalizedCostRows) else NormalizedCostRows(map(normalize, raw))
        rows, days = _daily_fold(events, key, dedup, now)
        if not rows and not exists(key) and key not in missing_pending:
            if key.startswith(_AGY) and any(valid_event_time(event[0], now) for event in events):
                # Future reopening can change which source contributes. If
                # its peer can no longer be read, retain the evidence in place.
                missing_evidence.add(key)
                continue
            if not events:
                folded[key] = None
                continue
        folded[key] = files[key][0], events, rows, days
    links = {}
    for group in groups:
        if group & missing_evidence:
            for key in group:
                folded.pop(key, None)
        retained = sorted(k for k in group if folded.get(k) is not None)
        if len(retained) > 1:
            links[retained[0]] = tuple(retained[1:])
            links.update((k, (retained[0],)) for k in retained[1:])
    for key, entry in folded.items():
        if entry is None:
            changes[key] = None
        else:
            mtime, events, rows, days = entry
            changes[key] = (mtime, CompactedRows(rows, days, pack_rows(events),
                                                _index(events) if key.startswith("db:") else "", links.get(key, ()), zone))
    return changes
