"""Lossless cost compaction regressions; no real view or cache files are written."""
import contextlib
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import server
from _cost_test_fixture import patch_cost_state
from agy_cost_events import AgyCostDedup
from cost_archive import CompactedRows, NormalizedCostRows, load_compacted_rows, persisted_compaction
from cost_compaction import plan_compaction
from cost_schema import COMPACTED_UID
from cost_runtime import CostRows, CostRuntime
from droid_ledger import DroidLedger

NOW = 1_800_000_000.0
DAY = 86400
OLD = NOW - 12 * DAY
DIGEST = "a" * 64


def event(ts, uid="", family="gemini", cost=.25, model=""):
    return (ts, cost, family, uid, 12, 3, 2, 1, 4, model)


def entry(ts, *rows):
    return (ts, NormalizedCostRows(rows))


def folded(files, quota=None, pending=(), exists=lambda _: True, now=NOW):
    out = dict(files)
    for key, replacement in plan_compaction(files, set(pending), quota or {}, now,
                                            server._normalize_cost_event, exists).items():
        if replacement is None:
            out.pop(key, None)
        else:
            out[key] = replacement
    return out


def aggregate(files, quota=None, now=NOW):
    daily, totals, estimates = CostRows().aggregate(
        [(k, rows) for k, (_, rows) in files.items()], quota or {}, now, server._normalize_cost_event)
    return daily, totals, estimates, server._build_calendar(daily, now)


def uid(ts, clock="", digest=DIGEST):
    return f"4:12:3:2:{int(ts)}#{digest}" + ("@" + clock if clock else "")


class CompactionTests(unittest.TestCase):
    def test_age_cutoffs_calendar_windows_and_estimates(self):
        today = datetime.fromtimestamp(NOW).replace(hour=12, minute=0, second=0, microsecond=0)
        old = (today - timedelta(days=12)).timestamp()
        older = (today - timedelta(days=36)).timestamp()
        near = NOW - 120
        quota = {
            "gemini-5h": {"reset_ts": NOW + 3600, "observed_at": NOW - 30, "rem_frac": .5},
            "droid-monthly": {"reset_ts": NOW + 2000, "win_start_ts": NOW - 25 * DAY,
                              "observed_at": NOW - 30, "reset_precision_sec": 60, "rem_frac": .4},
        }
        originals = {
            "db:a": entry(old, event(old - 400), event(old), event(old, family="claude"),
                          event(old - DAY)),
            "codex:a": entry(old, event(old)),
            "pi:old": entry(old, event(old)),
            "grok:old": entry(old, event(old)),
            "droid:old": entry(older, event(older - 30), event(older)),
            "droid:within-month": entry(NOW - 25 * DAY, event(NOW - 25 * DAY)),
            "grok:recent-event": entry(old, event(NOW - DAY)),
            "pi:cutoff": entry(NOW - 10 * DAY, event(old)),
            "pi:pending": entry(old, event(old)),
            "db:recent": entry(near, event(near)),
        }
        out = folded(originals, quota, ("pi:pending",))
        for key in ("db:a", "codex:a", "pi:old", "grok:old", "droid:old"):
            self.assertIsInstance(out[key][1], CompactedRows)
            self.assertTrue(all(row[3] == COMPACTED_UID and len(row) == 10 for row in out[key][1]))
        self.assertEqual(len(out["db:a"][1]), 3)
        self.assertEqual(len(out["droid:old"][1]), 1)
        for key in ("droid:within-month", "grok:recent-event", "pi:cutoff", "pi:pending", "db:recent"):
            self.assertIs(out[key], originals[key])
        self.assertEqual(aggregate(originals, quota), aggregate(out, quota))
        self.assertEqual(out, folded(out, quota, ("pi:pending",)))

    def test_old_parent_waits_when_a_fork_is_still_active(self):
        near = NOW - 90
        originals = {"db:parent": entry(OLD, event(OLD, uid(OLD))),
                     "db:fork": entry(near, event(near, uid(near)))}
        out = folded(originals)
        self.assertIs(out["db:parent"], originals["db:parent"])
        self.assertEqual(aggregate(out)[1]["5h"], 0)
        self.assertEqual(aggregate(out), aggregate(originals))

    def test_future_undated_fork_after_parent_fold_and_reload(self):
        parent = entry(OLD, event(OLD, uid(OLD, "080a::")), event(OLD, uid(OLD, "080a::")),
                       event(OLD, "8:12:3:2:123"))
        orig = {"db:parent": parent}
        first = folded(orig)
        snapshot = json.loads(json.dumps(persisted_compaction(first)))
        self.assertEqual(snapshot["compacted_db_digests"]["db:parent"], DIGEST[:16] * 2)
        mtime, rows = first["db:parent"]
        data = json.loads(json.dumps(rows))
        first["db:parent"] = (mtime, load_compacted_rows(
            "db:parent", NormalizedCostRows(map(tuple, data)), snapshot))
        near = NOW - 60
        new = entry(near, event(near, uid(near)), event(near, uid(near)))
        first["db:fork"] = orig["db:fork"] = new
        self.assertEqual(aggregate(first), aggregate(orig))
        self.assertEqual(aggregate(first)[1]["5h"], 0)
        self.assertEqual(aggregate(first)[1]["weekly"], 0)

    def test_multiclock_and_prefix_collision_do_not_overdedup(self):
        near = NOW - 60
        raw = {"db:parent": entry(OLD, event(OLD, uid(OLD, "080a::")))}
        compact = folded(raw)
        fork = entry(near, event(near, uid(near, "0814::")),
                     event(near, uid(near, "080a::", DIGEST[:16] + "b"*48)))
        compact["db:fork"] = raw["db:fork"] = fork
        self.assertEqual(aggregate(compact), aggregate(raw))
        self.assertEqual(aggregate(compact)[1]["5h"], .5)

    def test_reopened_winner_needs_all_previously_folded_fork_sources(self):
        parent = event(OLD, uid(OLD))
        raw = {"db:a": entry(OLD, parent), "db:b": entry(OLD, parent)}
        compact = folded(raw)
        self.assertEqual(compact["db:a"][1][0][3], COMPACTED_UID)
        self.assertIsInstance(compact["db:b"][1], CompactedRows)
        self.assertEqual(compact["db:b"][1], [])
        # A different, stronger clock now has priority over the historical row.
        near = NOW - 60
        compact["db:b"] = raw["db:b"] = entry(near, event(near, uid(near, "080a::")))
        self.assertEqual(aggregate(compact), aggregate(raw))

    def test_aged_peer_removal_or_complete_replacement_restores_skipped_history(self):
        parent = event(OLD, uid(OLD))
        raw = {"db:a": entry(OLD, parent), "db:b": entry(OLD, parent)}
        compact = folded(raw)
        self.assertEqual(compact["db:b"][1], [])
        for winner in (None, entry(NOW, event(NOW - 60, uid(NOW - 60, digest="b"*64)))):
            expected, actual = dict(raw), dict(compact)
            if winner is None:
                del expected["db:a"], actual["db:a"]
            else:
                expected["db:a"] = actual["db:a"] = winner
            self.assertEqual(aggregate(expected), aggregate(actual))
            self.assertEqual(aggregate(expected), aggregate(folded(actual)))

    def test_pairs_wait_for_both_sides_and_preserve_transcript_tail(self):
        match = f"4:12:3:2:{int(OLD)}"
        db = ("db:cid", entry(OLD, event(OLD, uid(OLD))))
        fallback = (event(OLD, match), event(OLD + 200, ""))
        old = dict([db, ("agy_tr:cid", entry(OLD, *fallback))])
        live = dict(old, **{"agy_tr:cid": entry(NOW, *fallback)})
        self.assertIs(folded(live)["db:cid"], live["db:cid"])
        self.assertIs(folded(old, pending={"agy_tr:cid"})["db:cid"], old["db:cid"])
        both = folded(old)
        self.assertEqual(len(both["agy_tr:cid"][1]), 1)
        self.assertEqual(both["agy_tr:cid"][1][0][1], .25)
        self.assertEqual(aggregate(both), aggregate(old))
        dedup = AgyCostDedup([(k, r) for k, (_, r) in both.items()], NOW)
        self.assertFalse(dedup.skip("agy_tr:cid", COMPACTED_UID, OLD + 200))

    def test_stale_grok_window_and_subsequent_snapshot(self):
        midnight = (datetime.fromtimestamp(NOW) - timedelta(days=20)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        morning, evening = midnight + 8 * 3600, midnight + 18 * 3600
        raw = {"grok:sample": entry(evening, event(morning, cost=1), event(evening, cost=1))}
        start = midnight + 17 * 3600
        quota = {"grok-weekly": {"reset_ts": start + 7 * DAY, "period_start_ts": start}}
        checked = folded(raw, quota)
        self.assertIs(checked["grok:sample"], raw["grok:sample"])
        self.assertEqual(aggregate(checked, quota)[1]["grok-weekly"], 1)
        already_folded = folded(raw)
        self.assertIsInstance(already_folded["grok:sample"][1], CompactedRows)
        self.assertEqual(aggregate(already_folded, quota), aggregate(raw, quota))

    @unittest.skipUnless(hasattr(time, "tzset"), "host does not support TZ changes")
    def test_folded_day_rebuilds_exactly_after_timezone_change(self):
        old = datetime(2026, 1, 2, 1).replace(tzinfo=timezone.utc).timestamp()
        raw = {"pi:tz": entry(old, event(old))}
        try:
            with patch.dict(os.environ, {"TZ": "UTC"}):
                time.tzset()
                original = aggregate(raw)[0]
                compact = folded(raw)
            with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                time.tzset()
                self.assertNotEqual(aggregate(raw)[0], original)
                self.assertEqual(aggregate(compact), aggregate(raw))
                self.assertFalse(hasattr(compact["pi:tz"][1].original(), "days"))
        finally:
            time.tzset()


class ScanFixture(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(TemporaryDirectory()))
        self.files = {}
        self.runtime = CostRuntime()
        self.ledger = DroidLedger()
        self.cache = self.root / "cache.json"
        self.factory = self.root / "droid"
        self.factory.mkdir()
        self.patch = lambda name, value: stack.enter_context(patch.object(server, name, value))
        patch_cost_state(stack, path=self.cache, runtime=self.runtime, events=self.files, ledger=self.ledger,
                         SUBAGENT_APP_DIRS=[("test", self.root)], FACTORY_SESSIONS_DIR=self.factory,
                         PI_SESSIONS_DIR=self.root / "missing", CODEX_SESSIONS_DIR=self.root / "missing",
                         GROK_SESSIONS_DIR=self.root / "missing")
        stack.enter_context(patch.object(server.time, "time", return_value=NOW))

    def touch(self, path, epoch):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (epoch, epoch))
        return path


class CompactionScanTests(ScanFixture):
    def test_reopen_db_forces_paired_transcript_rescan(self):
        db = self.touch(self.root / "conversations" / "cid.db", NOW - 1)
        tr = self.touch(self.root / "brain" / "cid" / ".system_generated" / "logs" / "transcript.jsonl", OLD)
        match, tail = event(OLD, f"4:12:3:2:{int(OLD)}"), event(OLD + 200)
        original_db = event(OLD, uid(OLD))
        raw = {"db:cid": entry(OLD, original_db), "agy_tr:cid": entry(OLD, match, tail)}
        self.files.update(folded(raw))
        addition = event(NOW - 60, "3:12:3:2:1799999940")
        self.patch("get_db_mtime", lambda p: p.stat().st_mtime)
        with patch.object(server, "extract_db_cost_events", return_value=[original_db, addition]) as scan_db, \
             patch.object(server, "get_cached_analysis_events", return_value=None), \
             patch.object(server, "analyze_agy_transcript_fallback", return_value={"cost_events": [match, tail]}) as scan_tr:
            server._refresh_window_costs(force=True)
        scan_db.assert_called_once_with(db, NOW - 1)
        scan_tr.assert_called_once_with(tr)
        self.assertEqual(self.files["db:cid"][1], [original_db, addition])
        self.assertEqual(self.files["agy_tr:cid"][1], [match, tail])
        raw["db:cid"] = entry(NOW - 1, original_db, addition)
        self.assertEqual(aggregate(self.files), aggregate(raw))
        self.assertNotIn("db:cid", json.loads(self.cache.read_text()).get("compacted_db_digests", {}))

    def test_droid_probes_zero_marks_empty_cache_hits_and_deleted_history(self):
        normal = self.touch(self.factory / "normal" / "x.settings.json", NOW - 1)
        probes = [self.touch(self.factory / slug / "x.settings.json", NOW - 1)
                  for slug in ("-private-tmp-agy-cockpit-droid-probe", "-tmp-agy-cockpit-droid-probe")]
        keep = "droid:" + str(normal)
        drop = ["droid:" + str(p) for p in probes] + ["droid:" + str(self.factory / "missing.settings.json")]
        history = "droid:" + str(self.factory / "deleted-billed.settings.json")
        historical = NOW - 40 * DAY
        zero = dict(raw_inp=0, cw=0, cache=0, out=0, seen_turns=0, last_ts=0, pending_cost=[])
        self.ledger.watermarks.update({k: dict(zero) for k in [keep, *drop]})
        self.ledger.watermarks[history] = dict(zero, raw_inp=8, seen_turns=1, last_ts=historical)
        self.files.update({k: entry(NOW - 1) for k in [keep, *drop]})
        self.files[history] = entry(historical, event(historical))
        expired = "droid:" + str(self.factory / "expired.settings.json")
        self.files[expired] = entry(NOW - 371 * DAY, event(NOW - 371 * DAY))
        before = aggregate({history: self.files[history]})
        empty = {"cost_events": [], "_droid_usage": {"raw_inp": 0, "cw": 0, "cache": 0, "out": 0, "turns": []}}
        with patch.object(server, "analyze_droid_session", return_value=empty) as scan:
            server._refresh_window_costs(force=True)
            server._refresh_window_costs(force=True)
        self.assertCountEqual([args[0] for args, _ in scan.call_args_list], probes)
        self.assertEqual(set(self.files), {history, keep})
        self.assertEqual(aggregate(self.files), before)
        persisted = json.loads(self.cache.read_text())
        self.assertEqual(set(persisted["droid_watermarks"]), {history})
        self.assertFalse(self.ledger.has(keep))

    def test_empty_droid_cache_hit_after_reloading_no_watermark(self):
        p = self.touch(self.factory / "normal" / "x.settings.json", NOW - 1)
        key = "droid:" + str(p)
        empty = {"mtime": NOW - 1, "cost_events": [],
                 "_droid_usage": {"turns": [], "event_ts": NOW - 1, "model": "",
                                  "raw_inp": 0, "cw": 0, "cache": 0, "out": 0}}
        with patch.object(server, "analyze_droid_session", return_value=empty) as scan:
            server._refresh_window_costs(force=True)
            self.assertEqual(self.files[key][1], [])
            self.assertEqual(json.loads(self.cache.read_text())["droid_watermarks"], {})
            self.files.clear()
            server._COST_CACHE_LOADED = False
            server._refresh_window_costs(force=True)
        scan.assert_called_once_with(p)


if __name__ == "__main__":
    unittest.main()
