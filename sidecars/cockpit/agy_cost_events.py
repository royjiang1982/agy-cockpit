"""Join AGY transcript fallback with its own SQLite session, never another session."""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from cockpit_io import parse_iso_ts
from cost_archive import exact_rows
from cost_schema import COMPACTED_UID, clock_groups, decode_event_clock, valid_event_time


def _uid_time(uid, fallback):
    parts = uid.split(":", 4)
    if len(parts) == 5:
        try:
            ts = float(parts[4])
            if math.isfinite(ts):
                return ":".join(parts[:4]), ts
        except ValueError:
            pass
    return "", fallback


def _fork_skips(copies):
    """Choose representatives before the calendar visits either source."""
    skips = Counter()
    for payload, records in copies.items():
        if len(records) == 1 or all(r[0] == records[0][0] for r in records):
            continue
        raws = {r[2] for r in records}
        groups = clock_groups({payload: raws})
        ranks = {}
        for raw in raws:
            start, first, end = decode_event_clock(raw)
            # SQLite dates the billed event from the measured start/end pair,
            # or from start alone; first-token alone cannot anchor that date.
            ranks[raw] = (not bool(start), not bool(end), not bool(first))
        counts, maxima = Counter(), Counter()
        # Visit the strongest evidence first. Keep occurrence n only if no
        # better source supplied an nth occurrence in the same clock group.
        for key, uid, raw, ts in sorted(records, key=lambda r: (ranks[r[2]], r[3], r[0], r[1])):
            group = groups[payload, raw]
            counts[group, key] += 1
            if counts[group, key] <= maxima[group]:
                skips[key, uid, ts] += 1
            else:
                maxima[group] = counts[group, key]
    return skips


class AgyCostDedup:
    def __init__(self, files, now, cutoffs=None, zone=None):
        self.files = exact_rows(list(files), cutoffs, zone)
        self._uids = set()
        self._bounds = {}
        self._intervals = defaultdict(set)
        copies = defaultdict(list)
        for key, events in self.files:
            if not key.startswith("db:"):
                continue
            session_id = key[3:]
            starts = []
            ends = []
            for item in events:
                ts = float(item[0])
                if not valid_event_time(ts, now):
                    continue
                uid = str(item[3]) if len(item) >= 4 else ""
                if uid == COMPACTED_UID:
                    continue
                full_uid = uid
                uid, _, evidence = uid.partition("#")
                if evidence:
                    digest, _, raw = evidence.partition("@")
                    copies[digest].append((key, full_uid, raw, round(ts, 3)))
                signature, start = _uid_time(uid, ts)
                end = max(start, ts)
                starts.append(start)
                ends.append(end)
                if uid:
                    self._uids.add((session_id, uid))
                if signature:
                    self._intervals[session_id, signature].add((start, end))
            if starts:
                self._bounds[session_id] = (min(starts), max(ends))

        self._skips = _fork_skips(copies)

    def skip(self, key, uid, ts):
        if uid == COMPACTED_UID:
            return False
        if key.startswith("db:"):
            token = (key, uid, round(ts, 3))
            if self._skips[token]:
                self._skips[token] -= 1
                return True
            return False
        if not key.startswith("agy_tr:"):
            return False
        session_id = key[7:]
        if (session_id, uid) in self._uids:
            return True
        bounds = self._bounds.get(session_id)
        if not bounds:
            return False
        signature, start = _uid_time(uid, ts)
        if signature:
            # DB UIDs describe call start, but transcript updates can describe
            # completion. Match the same call before accepting a newer tail.
            return any(a - 1 <= start <= b + 1 for a, b in self._intervals.get((session_id, signature), ()))
        return not uid and bounds[0] - 1 <= start <= bounds[1]


def step_update_time(record, update):
    """Prefer the step's own timestamp, then the enclosing event's timestamp."""
    for obj in (update, record):
        for key in ("timestamp", "created_at", "createdAt", "timestamp_ms", "timestampMs"):
            raw = obj.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                value = parse_iso_ts(raw) if isinstance(raw, str) else None
            else:
                if key in ("timestamp_ms", "timestampMs") or value >= 1e11:
                    value /= 1000
            if value and math.isfinite(value) and value > 0:
                return value
    return None


def transcript_model(record):
    """Read structured model metadata, never names mentioned in transcript text."""
    for fields in (record.get("step_update"), record):
        if not isinstance(fields, dict):
            continue
        for key in ("model", "model_id", "modelId", "model_name", "modelName"):
            value = fields.get(key)
            if isinstance(value, str):
                name = value.strip()
                if name and name.lower() not in ("auto", "inherit", "unknown"):
                    return name
    return ""
