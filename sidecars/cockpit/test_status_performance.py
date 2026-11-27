"""Regression tests for the cost worker and the live conversation index."""
import calendar
import contextlib
import json
import os
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import analyzer
import cost_runtime
import server
from agy_cost_events import AgyCostDedup
from annotation_index import AnnotationIndex
from cost_windows import WindowCosts, valid_event_time
from droid_ledger import DroidLedger


NOW = 1_800_000_000.0
BID = '3p-5h'
QUOTA = {BID: {'reset_ts': NOW + 3600, 'observed_at': NOW - 60, 'rem_frac': 0.4}}
REFRESHED_QUOTA = {BID: dict(QUOTA[BID], observed_at=NOW - 10, rem_frac=0.3)}


def legacy_aggregate(files, quota, now):
    daily = {}
    windows = WindowCosts(quota, now)
    dedup = AgyCostDedup(files, now)
    for key, events in files:
        for item in events:
            ts, cost, fam, uid, inp, cache, out, turns, cw, _ = server._normalize_cost_event(item)
            if not valid_event_time(ts, now) or dedup.skip(key, uid, ts):
                continue
            day = datetime.fromtimestamp(ts).date()
            row = daily.setdefault(day, {'turns': 0, 'in': 0, 'cache_write': 0, 'cache': 0, 'out': 0, 'cost': Decimal(0)})
            for field, value in (('turns', turns), ('in', inp - cw), ('cache_write', cw),
                                 ('cache', cache), ('out', out), ('cost', Decimal(str(cost)))):
                row[field] += value
            windows.add(key.partition(':')[0], fam, ts, cost, uid)
    for row in daily.values():
        row["_cost_ticks"] = int(row.pop("cost") * 100_000_000)
    return daily, windows.totals, windows.eligible_estimates()


def without_models(daily):
    return {day: {k: v for k, v in row.items() if k != "model_usage"} for day, row in daily.items()}


class CostRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tick = 100.0
        self.runtime = cost_runtime.CostRuntime()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = Path(self.stack.enter_context(TemporaryDirectory()))
        self.path = tmp / 'cost_events_cache.json'
        self.events = {
            'agy_tr:main': (NOW, [(NOW - 600, 0.83, 'claude', '1:25:4:2:1799999400')]),
            'grok:thread': (NOW, [(NOW - 0.0006, 0.123456789, 'other', '', 30, 4, 3, 2, 10)]),
            'codex:rollout': (NOW, [(NOW - 86400, 1.23, 'other', '', 0, 4, 0, 0)]),
        }
        for name, value in {
            '_COST_RUNTIME': self.runtime, '_COST_CACHE_FILE': self.path, '_COST_CACHE_LOADED': True,
            'STATE_FILE': tmp / 'view_state.json', '_LAST_QUOTA_FETCH_TS': NOW - 60,
            '_COST_GENERATION': 0, '_COST_FILE_EVENTS': self.events, '_PENDING_REBUILD': set(),
            '_DB_APPLIED_BUILTIN_ENUM': {}, '_DROID_LEDGER': DroidLedger(),
            '_CALENDAR_DAYS': {}, '_CALENDAR_CACHE': {}, '_WINDOW_COSTS': {}, '_WINDOW_ESTIMATES': {},
            '_QUOTA_CACHE': QUOTA, '_LAST_COST_SCAN_TS': NOW, '_APPLIED_PRICING_EPOCH': server.PRICING_EPOCH,
            '_APPLIED_MODEL_MAP_EPOCH': server.get_model_map_epoch(),
        }.items():
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(cost_runtime.time, 'time', return_value=NOW))
        self.stack.enter_context(patch.object(cost_runtime.time, 'monotonic', side_effect=lambda: self.tick))

    def update(self, name, cost=0.25):
        server._update_conv_cost_events(name, NOW, [(NOW - 600, cost, 'claude', '1:25:4:2:1799999400', 25, 4, 2, 1)])

    def assert_aggregate_matches_legacy(self, quota=QUOTA):
        rows = [(key, events) for key, (_, events) in self.events.items()]
        daily, totals, estimates = legacy_aggregate(rows, quota, NOW)
        self.assertEqual(without_models(server._CALENDAR_DAYS), daily)
        self.assertEqual(server._WINDOW_COSTS, totals)
        self.assertEqual(server._WINDOW_ESTIMATES, estimates)
        for period, expected in server._build_calendar(daily, NOW).items():
            actual = server._CALENDAR_CACHE[period]
            self.assertEqual(actual["summary"], expected["summary"])
            self.assertEqual([{**item, "models": []} for item in actual["items"]], expected["items"])

    def test_callback_marks_dirty_and_worker_coalesces_and_refreshes_periodically(self):
        with patch.object(self.runtime._rows, 'aggregate', wraps=self.runtime._rows.aggregate) as aggregate:
            server._recompute_window_costs_from_memory()
            with patch.object(server, '_recompute_window_costs_from_memory') as recompute:
                self.update('main')
                self.update('child')
                self.update('child')  # Identical analysis must not dirty another generation.
                recompute.assert_not_called()
            self.assertEqual(server._COST_GENERATION, 2)
            self.assertEqual(aggregate.call_count, 1)
            self.tick += 0.3
            server._refresh_window_costs()
            self.assertEqual(aggregate.call_count, 1)
            self.tick += 0.6
            server._refresh_window_costs()
            self.assertEqual(aggregate.call_count, 2)
            self.assert_aggregate_matches_legacy()
            self.tick += 14.9
            server._refresh_window_costs()
            self.assertEqual(aggregate.call_count, 2)
            self.tick += 0.2
            server._refresh_window_costs()
            self.assertEqual(aggregate.call_count, 3)

    def prepare_quota_refresh(self):
        # Already in the cost snapshot but newer than the old quota observation.
        server._update_conv_cost_events('between', NOW, [(NOW - 30, 0.5, 'claude')])
        server._recompute_window_costs_from_memory()
        self.stack.enter_context(patch.object(server, 'fetch_quota_buckets_with_meta',
                                              return_value=(REFRESHED_QUOTA, {'ok': True})))
        self.stack.enter_context(patch.object(server, '_find_cli_bin', return_value=None))

    def test_generation_changed_during_quota_recompute_keeps_estimate_and_stays_dirty(self):
        self.prepare_quota_refresh()
        generation = server._COST_GENERATION
        original = self.runtime._rows.aggregate

        def update_after_snapshot(*args):
            self.assertFalse(server._COST_LOCK.locked())
            self.update('child')
            return original(*args)

        with patch.object(self.runtime._rows, 'aggregate', side_effect=update_after_snapshot):
            server._refresh_quota()
        self.assertEqual(server._COST_GENERATION, generation + 1)
        self.assertEqual(self.runtime._display_generation, generation)
        current = server._get_live_quota()[BID]
        self.assertEqual(current['observed_at'], REFRESHED_QUOTA[BID]['observed_at'])
        self.assertIs(current['estimate_ok'], True)
        self.assertAlmostEqual(current['estimate_usd'], 1.33)

        self.tick += 1.0
        server._refresh_window_costs()
        self.assertEqual(self.runtime._display_generation, server._COST_GENERATION)
        self.assert_aggregate_matches_legacy(REFRESHED_QUOTA)
        current = server._get_live_quota()[BID]
        self.assertIs(current['estimate_ok'], True)
        self.assertAlmostEqual(current['estimate_usd'], 1.58)

    def test_quota_read_during_refresh_keeps_previous_cost_snapshot(self):
        self.prepare_quota_refresh()
        before, old_fetched_at = server._get_live_quota_snapshot()
        self.assertIs(before[BID]['estimate_ok'], True)
        self.assertAlmostEqual(before[BID]['estimate_usd'], 0.83)
        self.assertEqual(old_fetched_at, NOW - 60)
        entered, may_continue = threading.Event(), threading.Event()
        original = self.runtime._rows.aggregate
        reader, state_lock = threading.current_thread(), server._STATE_LOCK

        def pause_after_snapshot(*args):
            entered.set()
            if not may_continue.wait(4):
                raise TimeoutError('test quota refresh stalled')
            return original(*args)

        def publish_on_reader_unlock(*_):
            state_lock.release()
            if threading.current_thread() is reader:
                may_continue.set()
                refresh.result(timeout=4)

        with patch.object(self.runtime._rows, 'aggregate', side_effect=pause_after_snapshot), \
                ThreadPoolExecutor(max_workers=1) as worker:
            refresh = worker.submit(server._refresh_quota)
            try:
                self.assertTrue(entered.wait(4))
                self.assertEqual(server._get_live_quota_snapshot(), (before, old_fetched_at))
                # Publish between the reader's first unlock and any later cache read.
                with patch.object(server, '_STATE_LOCK') as reader_lock:
                    reader_lock.__enter__.side_effect = state_lock.acquire
                    reader_lock.__exit__.side_effect = publish_on_reader_unlock
                    self.assertEqual(server._get_live_quota_snapshot(), (before, old_fetched_at))
            finally:
                may_continue.set()
            self.assertTrue(refresh.result(timeout=4)['refreshed'])

        after, fetched_at = server._get_live_quota_snapshot()
        self.assertEqual(after[BID]['observed_at'], REFRESHED_QUOTA[BID]['observed_at'])
        self.assertIs(after[BID]['estimate_ok'], True)
        self.assertAlmostEqual(after[BID]['estimate_usd'], 1.33)
        self.assertEqual(fetched_at, NOW)

    def test_failed_quota_aggregation_does_not_publish_or_save(self):
        self.prepare_quota_refresh()
        before = server._get_live_quota_snapshot()
        quota, estimates = server._QUOTA_CACHE, server._WINDOW_ESTIMATES
        with patch.object(self.runtime._rows, 'aggregate', side_effect=RuntimeError('aggregation failed')), \
                patch.object(server, '_save_persisted_state') as save:
            with self.assertRaisesRegex(RuntimeError, 'aggregation failed'):
                server._refresh_quota()
        self.assertIs(server._QUOTA_CACHE, quota)
        self.assertIs(server._WINDOW_ESTIMATES, estimates)
        self.assertEqual(server._get_live_quota_snapshot(), before)
        save.assert_not_called()

    def test_serialization_and_replace_release_reader_lock_and_keep_newer_generation_dirty(self):
        self.events['pi:long'] = (NOW, [(NOW - i, 0.0123456789, 'other') for i in range(257)])
        self.update('main')
        entered, may_continue = threading.Event(), threading.Event()
        dumps, replace = json.dumps, os.replace
        unlocked = []

        def note_lock():
            acquired = server._COST_LOCK.acquire(blocking=False)
            unlocked.append(acquired)
            if acquired:
                server._COST_LOCK.release()

        def encode(*args, **kwargs):
            note_lock()
            entered.set()
            if not may_continue.wait(4):
                raise TimeoutError('test writer stalled')
            return dumps(*args, **kwargs)

        def rename(*args):
            note_lock()
            return replace(*args)

        with patch.object(cost_runtime.json, 'dumps', side_effect=encode), patch.object(cost_runtime.os, 'replace', side_effect=rename):
            worker = threading.Thread(target=server._save_cost_events_cache, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(4))
                self.assertTrue(server._COST_LOCK.acquire(blocking=False))
                server._COST_LOCK.release()
                self.update('child')
            finally:
                may_continue.set()
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertTrue(len(unlocked) > 1 and all(unlocked))
        first = json.loads(self.path.read_text())
        self.assertEqual(set(first['files']), set(self.events) - {'db:child'})
        self.tick += 16
        server._refresh_window_costs()
        second = json.loads(self.path.read_text())
        self.assertEqual(second['files'], {
            key: [mtime, [list(server._normalize_cost_event(item)) for item in rows]]
            for key, (mtime, rows) in self.events.items()
        })

    @unittest.skipUnless(hasattr(time, 'tzset'), 'platform has no tzset')
    def test_cached_dates_match_legacy_across_midnight_and_zone_changes(self):
        files = [('db:boundary', [])]
        for day in (datetime(1987, 10, 25), datetime(2025, 3, 9), datetime(2025, 4, 6), datetime(2025, 11, 2)):
            start = calendar.timegm(day.timetuple())
            for offset in range(-86400, 86400, 15 * 60):
                ts = start + offset - 0.0006
                files[0][1].append((ts, 0.123456789, 'claude', '', 20, 2, 1, 1, 4))
        for zone in ('UTC0', 'Asia/Kathmandu', 'America/St_Johns', 'Australia/Lord_Howe'):
            with self.subTest(zone=zone):
                with patch.dict(os.environ, {'TZ': zone}):
                    time.tzset()
                    expected = legacy_aggregate(files, QUOTA, NOW)
                    with patch.object(cost_runtime, 'datetime', wraps=datetime) as clock:
                        daily, *windows = self.runtime._rows.aggregate(files, QUOTA, NOW, server._normalize_cost_event)
                        self.assertEqual((without_models(daily), *windows), expected)
                        self.assertGreater(clock.fromtimestamp.call_count, 0)
                        clock.fromtimestamp.reset_mock()
                        daily, *windows = self.runtime._rows.aggregate(files, QUOTA, NOW, server._normalize_cost_event)
                        self.assertEqual((without_models(daily), *windows), expected)
                        clock.fromtimestamp.assert_not_called()
                time.tzset()


class AnnotationIndexTests(unittest.TestCase):
    def setUp(self):
        self.tick = 100.0
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(TemporaryDirectory()))
        self.annotations = self.base / 'annotations'
        self.annotations.mkdir()
        self.stack.enter_context(patch.object(analyzer, 'APP_DIRS', [('App', self.base)]))
        self.stack.enter_context(patch.object(analyzer, '_ANNOTATIONS_INDEX', AnnotationIndex()))
        self.stack.enter_context(patch.object(analyzer.time, 'monotonic', side_effect=lambda: self.tick))

    def rewrite(self, cid, text, ns):
        path = self.annotations / f'{cid}.pbtxt'
        path.write_text(text, encoding='utf-8')
        os.utime(path, ns=(ns, ns))
        return path.stat().st_mtime_ns

    def test_only_changed_file_is_reparsed_and_bad_rewrite_keeps_archived_flag(self):
        reference = 1_750_000_000_000_000_000
        self.rewrite('a', 'title: "Old" last_user_view_time: { seconds: 40 nanos: 125000000 }', reference)
        self.rewrite('b', 'last_user_view_time: { seconds: 10 }', reference)
        with contextlib.closing(sqlite3.connect(self.base / 'conversation_summaries.db')) as con, con:
            con.execute('CREATE TABLE conversation_summaries (conversation_id, title)')
            con.executemany('INSERT INTO conversation_summaries VALUES (?, ?)', [('a', 'Fallback A'), ('b', 'Fallback B')])
        with patch.object(analyzer, 'parse_annotation_text', wraps=analyzer.parse_annotation_text) as parse:
            titles, views, _ = analyzer.read_annotations()
            self.assertEqual(dict(titles), {'a': 'Old', 'b': 'Fallback B'})
            self.assertEqual(views['a'], 40.125)
            self.assertEqual(parse.call_count, 2)
            mtime = self.rewrite('a', 'title: "New" archived: true last_user_view_time: { seconds: 41 }', reference + 1000)
            self.tick += 0.1
            self.assertEqual(analyzer.read_annotations()[0]['a'], 'Old')
            self.tick += 0.11
            titles, views, mtimes = analyzer.read_annotations()
            self.assertEqual((titles['a'], titles['b'], views['a'], mtimes['a']), ('New', 'Fallback B', 41, mtime))
            self.assertTrue(titles.by_source[str(self.base), 'a']['archived'])
            self.assertEqual(parse.call_count, 3)
            self.rewrite('a', 'title: "unterminated', reference + 2000)
            self.tick += 0.21
            titles, views, _ = analyzer.read_annotations()
            self.assertEqual(titles['a'], 'Fallback A')
            self.assertNotIn('a', views)
            stale = titles.by_source[str(self.base), 'a']
            self.assertTrue(stale['archived'] and stale['annotation_error'])
            self.assertEqual(stale['title'], 'New')
            self.assertEqual(parse.call_count, 4)

    def test_same_view_timestamp_switches_by_annotation_mtime(self):
        reference = 1_750_000_000_000_000_000
        conversations = self.base / 'conversations'
        conversations.mkdir()
        for cid in 'ab':
            (conversations / f'{cid}.db').touch()
        text = 'last_user_view_time: { seconds: 40 }'
        self.rewrite('a', text, reference)
        self.rewrite('b', 'last_user_view_time: { seconds: 39 }', reference)
        for name, value in {
            'APP_DIRS': analyzer.APP_DIRS, '_CONVERSATION_INDEX_CACHE': None,
            '_CURRENT_ACTIVE_ID': '', '_PREV_VTIMES': {}, '_PREV_PBTXT_MTIMES_NS': {},
            '_PREV_MAX_VTIME': 0.0,
        }.items():
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, '_save_persisted_state'))
        self.assertEqual(server.resolve_active_and_list()[0], 'a')
        self.rewrite('b', text, reference + 1000)
        self.tick += 0.21
        self.assertEqual(server.resolve_active_and_list()[0], 'b')
        self.rewrite('a', text, reference + 2000)
        self.tick += 0.21
        self.assertEqual(server.resolve_active_and_list()[0], 'a')


if __name__ == '__main__':
    unittest.main()
