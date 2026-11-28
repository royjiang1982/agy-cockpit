"""Schedule cost snapshots and persist them without holding the reader lock."""
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from datetime import date, datetime

from agy_cost_events import AgyCostDedup
from calendar_models import add_event_usage
from cockpit_io import atomic_text_writer
from cost_archive import (ARCHIVE_MAPS, NormalizedCostRows, cost_ticks, load_cached_files,
                          persisted_compaction, timezone_key as _timezone_key)
from cost_compaction import plan_compaction, safe_cutoffs, source_exists
from cost_migration import legacy_droid_keys, pending_keys
from cost_schema import valid_event_time
from cost_windows import EVENT_TIMING_REV, WindowCosts, cached_event_mtime, merge_quota_cache


class CostRows:
    """Reuse normalized rows and their exact local dates until a file changes."""

    def __init__(self):
        self._files = {}
        self._zone = None

    def aggregate(self, files, quota, now, normalize):
        zone = _timezone_key()
        same_zone = self._zone == zone
        prepared = {}
        daily = {}
        windows = WindowCosts(quota, now)
        # The existing deduplicator sees the original rows (including legacy UIDs).
        dedup = AgyCostDedup(files, now, safe_cutoffs(quota, now), zone)
        for key, raw in dedup.files:
            source = key.partition(':')[0]
            cached = self._files.get(key)
            if cached is not None and cached[0] is raw:
                _, events, days = cached
                if not same_zone:
                    days = [None] * len(events)
            else:
                events = raw if isinstance(raw, NormalizedCostRows) else [normalize(item) for item in raw]
                days = [None] * len(events)
            prepared[key] = raw, events, days
            fixed_days = getattr(raw, "days", None)
            for i, (ts, cost, fam, uid, inp, cache, out, turns, cw, model) in enumerate(events):
                if not valid_event_time(ts, now) or dedup.skip(key, uid, ts):
                    continue
                day = days[i]
                if day is None:
                    # Cache the exact datetime result, not a bucket that could
                    # straddle local midnight or an offset transition.
                    day = days[i] = (date.fromordinal(fixed_days[i]) if fixed_days is not None
                                     else datetime.fromtimestamp(ts).date())
                add_event_usage(daily, day, fam, model, turns, inp - cw, cw, cache, out, cost_ticks(cost), source=source)
                windows.add(source, fam, ts, cost, uid)
        self._files, self._zone = prepared, zone
        return daily, windows.totals, windows.eligible_estimates()


def _write_snapshot(stream, header, files, normalize):
    # The C JSON encoder holds the GIL for a whole dumps call. Bound the cost
    # of each call even if one conversation contains a very large event list.
    maps = ARCHIVE_MAPS
    stream.write(json.dumps({k: v for k, v in header.items() if k not in maps})[:-1])
    for field, values in header.items():
        if field not in maps:
            continue
        stream.write(f', {json.dumps(field)}: {{')
        for i, (key, value) in enumerate(values.items()):
            if i:
                stream.write(', ')
            stream.write(f'{json.dumps(key)}: {json.dumps(value)}')
        stream.write('}')
    stream.write(', "files": {')
    for i, (key, (mtime, rows)) in enumerate(files.items()):
        if i:
            stream.write(', ')
        stream.write(f'{json.dumps(key)}: [{json.dumps(mtime)}, [')
        already_normalized = isinstance(rows, NormalizedCostRows)
        for start in range(0, len(rows), 256):
            if start:
                stream.write(', ')
            batch = rows[start:start + 256]
            if not already_normalized:
                batch = [normalize(row) for row in batch]
            stream.write(json.dumps(batch)[1:-1])
        stream.write(']]')
    stream.write('}}')


class CostRuntime:
    """Coordinate off-request cost work; ``state`` is the sidecar server module."""

    def __init__(self):
        self._rows = CostRows()
        self._recompute_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self.damaged = set()
        self._empty_probes = OrderedDict()
        self._display_generation = -1
        self._display_quota = None
        self._last_recompute = float('-inf')
        self._saved_generation = 0
        self._last_save_attempt = float('-inf')
        self._last_compaction = float('-inf')

    def load(self, state):
        with self._load_lock:
            if state._COST_CACHE_LOADED:
                return
            raw, files, damage, epoch, repairs = {}, {}, set(), state.PRICING_EPOCH, False
            try:
                raw = json.loads(state._COST_CACHE_FILE.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("schema_version") not in (15, 16, 17, state._COST_CACHE_SCHEMA):
                    raise ValueError("unsupported cost cache schema")
                files, damage, repairs = load_cached_files(raw, state._normalize_cost_event, cached_event_mtime, state._LOG)
                epoch = raw["pricing_epoch"] if type(raw.get("pricing_epoch")) is int else 0
            except FileNotFoundError:
                pass
            except (ValueError, OSError, RecursionError) as exc:
                state._LOG.error("cost cache: %s; original snapshot will not be overwritten", exc)
                raw = {}
                damage.add(None)
            raw = raw if isinstance(raw, dict) else {}
            with state._COST_LOCK:
                state._COST_FILE_EVENTS.update(files)
                state._PENDING_REBUILD.update(pending_keys(raw, state._COST_CACHE_SCHEMA))
                state._PENDING_REBUILD.update(key for key in damage if key is not None)
                state._APPLIED_PRICING_EPOCH = epoch
                state._DROID_LEDGER.load(raw.get("droid_watermarks"), legacy_droid_keys(raw))
                models = raw.get("builtin_models")
                if isinstance(models, dict):
                    with state._BUILTIN_ENUM_LOCK:
                        state._BUILTIN_SUBAGENT_MODEL_ENUM.update((k, v) for k, v in models.items() if type(v) is int)
                applied = raw.get("db_applied_builtin")
                if isinstance(applied, dict):
                    state._DB_APPLIED_BUILTIN_ENUM.update((k, v) for k, v in applied.items() if type(v) is int)
                self.damaged = damage
                state._COST_GENERATION += bool(repairs or raw.get("schema_version", state._COST_CACHE_SCHEMA)
                                               < state._COST_CACHE_SCHEMA)
                state._COST_CACHE_LOADED = True

    def known_empty_probe(self, key, stamp):
        return self._empty_probes.get(key) == stamp

    def remember_empty_probe(self, key, stamp, metrics):
        usage = metrics.get("_droid_usage")
        tokens = ("raw_inp", "cw", "cache", "out")
        if (not isinstance(usage, dict) or not all(k in usage and usage[k] == 0 for k in tokens)
                or usage.get("turns") or metrics.get("cost_events") or metrics.get("cost")):
            self._empty_probes.pop(key, None)
            return False
        self._empty_probes[key] = stamp
        self._empty_probes.move_to_end(key)
        if len(self._empty_probes) > 8192:
            self._empty_probes.popitem(last=False)
        return True

    def recompute(self, state, *, force=True):
        # Only cost producers (worker, startup, manual quota refresh) take this
        # lock. State and cost locks are never held together.
        with self._recompute_lock:
            with state._STATE_LOCK:
                quota = state._QUOTA_CACHE  # The server replaces this dict on refresh.
            costs = self._recompute_costs(state, quota, force=force)
            if costs is not None:
                with state._STATE_LOCK:
                    state._WINDOW_COSTS, state._WINDOW_ESTIMATES = costs
                self._last_recompute = time.monotonic()

    def refresh_quota(self, state, parsed: dict[str, dict], *, mark_fresh: bool):
        with self._recompute_lock:
            with state._STATE_LOCK:
                quota = merge_quota_cache(state._QUOTA_CACHE, parsed)
            costs = self._recompute_costs(state, quota, force=True) if state._COST_CACHE_LOADED else None
            # Keep serving the previous pair until the new one is ready.
            with state._STATE_LOCK:
                state._QUOTA_CACHE = quota
                if costs is not None:
                    state._WINDOW_COSTS, state._WINDOW_ESTIMATES = costs
                if mark_fresh:
                    state._LAST_QUOTA_FETCH_TS = time.time()
            if costs is not None:
                self._last_recompute = time.monotonic()

    def _recompute_costs(self, state, quota, *, force):
        # Called by the serialized publishers; the source rows can still change.
        with state._COST_LOCK:
            generation = state._COST_GENERATION
            age = time.monotonic() - self._last_recompute
            dirty = self._display_generation != generation or self._display_quota is not quota
            if not force and age < 15.0 and (not dirty or age < 0.75):
                return None
            # Lists are replaced, not mutated, by the ingestion paths.
            files = [(key, rows) for key, (_, rows) in state._COST_FILE_EVENTS.items()]
        now = time.time()
        daily, totals, estimates = self._rows.aggregate(files, quota, now, state._normalize_cost_event)
        calendar = state._build_calendar(daily, now)
        with state._COST_LOCK:
            state._CALENDAR_DAYS, state._CALENDAR_CACHE = daily, calendar
            # Record the snapshot's generation, not the source's current one, so
            # the next eligible tick picks up rows written during aggregation.
            self._display_generation, self._display_quota = generation, quota
        return totals, estimates

    def compact(self, state, now):
        # The scan runs on the worker, not the status path. Compute outside the
        # locks and discard the plan if analysis publishes newer source rows.
        clock = time.monotonic()
        if clock - self._last_compaction < 300:
            return
        self._last_compaction = clock
        with state._STATE_LOCK:
            quota = state._QUOTA_CACHE
        with state._COST_LOCK:
            generation = state._COST_GENERATION
            files = dict(state._COST_FILE_EVENTS)
            pending = set(state._PENDING_REBUILD)
            damage = set(self.damaged)
            protected_empty = {k for k, (_, rows) in files.items() if not rows and state._DROID_LEDGER.has_usage(k)}
        changes = plan_compaction(files, pending, quota, now, state._normalize_cost_event,
                                  lambda key: source_exists(key, state.SUBAGENT_APP_DIRS), protected_empty, damage)
        with state._COST_LOCK:
            if generation != state._COST_GENERATION:
                self._last_compaction = float('-inf')
                return
            for key, entry in changes.items():
                if entry is None:
                    state._COST_FILE_EVENTS.pop(key, None)
                    state._PENDING_REBUILD.discard(key)
                else:
                    state._COST_FILE_EVENTS[key] = entry
            dirty = state._DROID_LEDGER.prune(state._COST_FILE_EVENTS)
            if dirty or changes:
                state._COST_GENERATION += 1

    def tick(self, state, *, force=False):
        self.recompute(state, force=force)
        with state._COST_LOCK:
            save_due = (self._saved_generation != state._COST_GENERATION
                        and time.monotonic() - self._last_save_attempt >= 15.0)
        if save_due:
            state._save_cost_events_cache()

    def save(self, state):
        # Serializing saves prevents an older snapshot from replacing a newer
        # one on disk. Record only the generation actually written.
        with self._save_lock:
            with state._COST_LOCK:
                generation = state._COST_GENERATION
                self._last_save_attempt = time.monotonic()
                if self.damaged:
                    return
                files = dict(state._COST_FILE_EVENTS)
                pending = tuple(state._PENDING_REBUILD)
                header = {
                    'schema_version': state._COST_CACHE_SCHEMA,
                    'event_timing_rev': EVENT_TIMING_REV,
                    'pricing_epoch': state.PRICING_EPOCH,
                    'droid_watermarks': state._DROID_LEDGER.dump(files),
                    'builtin_models': state.snapshot_builtin_model_enum(),
                    'db_applied_builtin': dict(state._DB_APPLIED_BUILTIN_ENUM),
                }
            header['pending_rebuild'] = sorted(pending)
            header.update(persisted_compaction(files))
            path = state._COST_CACHE_FILE
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with atomic_text_writer(path) as stream:
                    _write_snapshot(stream, header, files, state._normalize_cost_event)
            except Exception:
                state._LOG.exception('cannot persist cost event cache')
                return
            with state._COST_LOCK:
                self._saved_generation = generation
