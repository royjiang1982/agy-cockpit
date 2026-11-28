"""Legacy calendar model backfill, reconciliation, and preserved archive history."""
from datetime import datetime
import json
import os

from cockpit_io import cached_paths
from cost_archive import CompactedRows, cost_ticks, timezone_key
from cost_schema import COMPACTED_UID
from cost_migration import merge_history
from cost_windows import EVENT_TIMING_REV
from grok_costs import BILLED_UID, UNPLACED_UID
from metric_droid import read_droid
from metric_pi import PI_MTIME_UID, read_pi
import server
from test_calendar_models import iso, legacy_pack, write_jsonl
from test_cost_compaction import DAY, NOW, OLD, ScanFixture, aggregate, entry, event, uid


class CalendarModelMigrationTests(ScanFixture):
    def load_17(self, files, **additional):
        snapshot = {"schema_version": 17, "event_timing_rev": EVENT_TIMING_REV,
                    "pricing_epoch": server.PRICING_EPOCH, "files": files, **additional}
        self.cache.write_text(json.dumps(snapshot))
        server._COST_CACHE_LOADED = False
        server._load_cost_events_cache()
        self.assertEqual(server._PENDING_REBUILD, set(files))

    def test_17_backfills_live_file_preserves_deleted_archive_and_truncated_tail(self):
        pi_dir = self.root / "pi"
        pi_dir.mkdir()
        self.patch("PI_SESSIONS_DIR", pi_dir)
        stamp = NOW - 60
        path = write_jsonl(pi_dir / "live.jsonl", dict(
            type="message", timestamp=iso(stamp), message={"role": "assistant", "model": "claude-opus-5-5",
                                                         "usage": {"input": 100, "cacheWrite": 20, "output": 10}}))
        os.utime(path, (stamp, stamp))
        live = read_pi(path, stamp)["cost_events"][0]
        key, deleted_key = f"pi:{path}", f"pi:{pi_dir / 'deleted.jsonl'}"
        deleted = event(OLD, family="grok", cost=.01234567)
        tail = event(stamp - 10, family="claude", cost=.01)
        self.load_17({key: [stamp, [live[:9], tail[:9]]],
                      deleted_key: [OLD, [[*deleted[:3], COMPACTED_UID, *deleted[4:9]]]]},
                     compacted_rows={deleted_key: legacy_pack([deleted[:9]])},
                     compacted_dates={deleted_key: [datetime.fromtimestamp(OLD).toordinal()]},
                     compacted_links={}, compacted_db_digests={},
                     compacted_zone={deleted_key: timezone_key()})
        baseline = sum(row["_cost_ticks"] for row in aggregate(self.files)[0].values())
        for _ in range(2):
            server._refresh_window_costs(force=True)
            self.assertEqual(self.files[key][1], [tail, live])
            self.assertEqual(self.files[deleted_key][1].original(), [deleted])
            self.assertEqual(sum(row["_cost_ticks"] for row in aggregate(self.files)[0].values()), baseline)
        self.assertEqual(json.loads(self.cache.read_text())["schema_version"], 18)
        month_names = {m["model"] for row in server._CALENDAR_CACHE["month"]["items"] for m in row["models"]}
        self.assertEqual(month_names, {"Opus 5.5", "Claude（历史）", "Grok（历史）"})

    def test_17_droid_backfill_does_not_realign_current_watermark_or_duplicate_deltas(self):
        settings = self.factory / "session.settings.json"
        transcript = self.factory / "session.jsonl"
        initial = NOW - 60
        models = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5-5")

        def records(index):
            stamp = initial + index * 10
            fields = {"turnId": str(index), "model": models[index]}
            return [dict(fields, timestamp=iso(stamp + i), message={"role": "assistant"}) for i in (0, 1)] + [
                dict(fields, timestamp=iso(stamp + 2), type="agent_turn_outcome", reason="completed")]

        def write_session(turns, tokens, stamp):
            settings.write_text(json.dumps({"model": models[0], "tokenUsage": {"inputTokens": tokens}}))
            write_jsonl(transcript, *(row for idx in range(turns) for row in records(idx)))
            for path in (settings, transcript):
                os.utime(path, (stamp, stamp))

        write_session(2, 100, initial + 12)
        old = read_droid(settings, None, transcript, initial + 12)
        key = f"droid:{settings}"
        original = old["cost_events"]
        mark = {"raw_inp": 100, "cw": 0, "cache": 0, "out": 0, "seen_turns": 2, "last_ts": initial + 12}
        self.load_17({key: [initial + 12, [row[:9] for row in original]]}, droid_watermarks={key: mark})
        server._refresh_window_costs(force=True)
        self.assertEqual(self.files[key][1], [server._normalize_cost_event(row) for row in original])
        self.assertEqual(self.ledger.watermarks[key]["seen_turns"], 2)
        self.assertNotIn(key, server._PENDING_REBUILD)
        write_session(3, 160, initial + 22)
        for _ in range(2):
            server._refresh_window_costs(force=True)
        added = self.files[key][1][len(original):]
        self.assertEqual([(e[4], e[7], e[9]) for e in added], [(0, 1, "Opus 5.5"), (60, 0, "Opus 5.5")])

    def test_grok_relocated_cumulative_remainder_is_replaced_not_added(self):
        old = (NOW, 1., "grok", UNPLACED_UID, 100, 0, 0, 1, 0, "")
        located = (NOW - 20, .6, "grok", BILLED_UID, 60, 0, 0, 1, 0, "grok-4.7")
        remainder = (NOW, .4, "grok", UNPLACED_UID, 40, 0, 0, 0, 0, "grok-4.7-fast")
        rows, complete = merge_history(entry(NOW, old), [located, remainder], grok=True)
        self.assertTrue(complete)
        self.assertEqual(rows, [located, remainder])
        self.assertEqual(sum(r[4] for r in rows), 100)
        self.assertEqual(sum(cost_ticks(r[1]) for r in rows), 100_000_000)
        # A matched, already-placed event cannot also cover a missing remainder.
        rows, complete = merge_history(entry(NOW, located, remainder), [located], grok=True)
        self.assertFalse(complete)
        self.assertCountEqual(rows, [located, remainder])
        later = (NOW + 10, 1., "grok", BILLED_UID, 100, 0, 0, 1, 0, "grok-4.7")
        self.assertEqual(merge_history(entry(NOW, old), [later], grok=True), ([old, later], False))

    def test_db_clock_enrichment_replaces_same_step_and_digest_by_occurrence(self):
        before = (NOW - 90, 1., "gemini", uid(NOW - 90, "080a::"), 100, 0, 0, 1, 0, "")
        after = (NOW - 60, 1., "gemini", uid(NOW - 90, "080a::0814"), 100, 0, 0, 1, 0, "Neon")
        self.assertEqual(merge_history(entry(NOW - 30, before), [after]), ([after], True))
        # A second occurrence of that payload has no replacement; it survives.
        self.assertEqual(merge_history(entry(NOW - 30, before, before), [after]), ([before, after], False))

    def test_pi_fallback_mtime_drift_matches_only_undated_rows(self):
        pi_dir = self.root / "pi"
        pi_dir.mkdir()
        self.patch("PI_SESSIONS_DIR", pi_dir)
        path = write_jsonl(pi_dir / "drift.jsonl", dict(
            type="message", message={"role": "assistant", "model": "claude-opus-5-5", "usage": {"input": 100}}))
        old_mtime, new_mtime = NOW - 120, NOW - 60
        new = read_pi(path, new_mtime)["cost_events"][0]
        self.assertEqual(new[3], PI_MTIME_UID)
        old = (old_mtime, new[1], new[2], "", *new[4:9])
        dated = (old_mtime - 30, *old[1:])
        os.utime(path, (new_mtime, new_mtime))
        key = f"pi:{path}"
        self.load_17({key: [old_mtime, [old, dated]]})
        for _ in range(2):
            server._refresh_window_costs(force=True)
            self.assertEqual(self.files[key][1], [(*dated, ""), new])
            self.assertEqual(sum(e[4] for e in self.files[key][1]), 200)

    def test_droid_without_watermark_keeps_disjoint_new_segment_and_old_usage(self):
        settings = self.factory / "new-segment.settings.json"
        transcript = self.factory / "new-segment.jsonl"
        new_stamp = NOW - 30
        settings.write_text(json.dumps({"model": "claude-opus-5-5", "tokenUsage": {"inputTokens": 50}}))
        write_jsonl(transcript, dict(turnId="new", timestamp=iso(new_stamp), message={"role": "assistant"}))
        for path in (settings, transcript):
            os.utime(path, (new_stamp, new_stamp))
        key = f"droid:{settings}"
        historic = (NOW - 120, .5, "claude", "", 100, 0, 0, 1, 0, "")
        self.load_17({key: [historic[0], [historic[:9]]]})
        for _ in range(2):
            server._refresh_window_costs(force=True)
            rows = self.files[key][1]
            self.assertEqual(rows[0], historic)
            self.assertEqual([(r[4], r[7], r[9]) for r in rows[1:]], [(50, 0, "Opus 5.5"), (0, 1, "Opus 5.5")])
            self.assertEqual(sum(r[4] for r in rows), 150)
            self.assertEqual(self.ledger.watermarks[key]["raw_inp"], 50)

    def test_droid_late_transcript_retries_model_only_even_at_same_settings_mtime(self):
        settings = self.factory / "late.settings.json"
        transcript = self.factory / "late.jsonl"
        settings.write_text(json.dumps({"model": "claude-opus-5-5", "tokenUsage": {"inputTokens": 100}}))
        stamp, mtime = NOW - 60, NOW - 30
        os.utime(settings, (mtime, mtime))
        key = f"droid:{settings}"
        old = [(stamp, .123, "claude", "", 100, 0, 0, 0, 0),
               (stamp + 1, 0., "claude", "", 0, 0, 0, 1, 0)]
        mark = {"raw_inp": 100, "cw": 0, "cache": 0, "out": 0, "seen_turns": 1, "last_ts": stamp + 1}
        self.load_17({key: [mtime, old]}, droid_watermarks={key: mark})
        server._refresh_window_costs(force=True)
        self.assertIn(key, server._PENDING_REBUILD)
        self.assertEqual(self.files[key][1], [(*r, "") for r in old])
        write_jsonl(transcript,
                    dict(turnId="late", timestamp=iso(stamp), model="claude-sonnet-5-5", message={"role": "assistant"}),
                    dict(turnId="late", timestamp=iso(stamp + 1), type="agent_turn_outcome", model="claude-sonnet-5-5"))
        os.utime(transcript, (stamp + 1, stamp + 1))
        server._refresh_window_costs(force=True)
        self.assertEqual(self.files[key][1], [(*r, "Sonnet 5.5") for r in old])
        self.assertEqual({k: self.ledger.watermarks[key][k] for k in mark}, mark)
        self.assertNotIn(key, server._PENDING_REBUILD)

    def test_deleted_pending_source_compacts_then_reopens_and_expires_normally(self):
        pi_dir = self.root / "pi"
        pi_dir.mkdir()
        self.patch("PI_SESSIONS_DIR", pi_dir)
        # Consecutive forced scans in this test stand in for separated worker ticks.
        self.patch("cached_paths", lambda base, pattern, recursive=False:
                   cached_paths(base, pattern, recursive, ttl=0))
        path = pi_dir / "later.jsonl"
        key, expired = f"pi:{path}", f"codex:{self.root / 'expired.jsonl'}"
        row = (OLD, .1, "claude", "", 100, 0, 0, 1, 0, "")
        expiry = NOW - 371 * DAY
        self.load_17({key: [OLD, [row[:9]]], expired: [expiry, [event(expiry)[:9]]]})
        server._refresh_window_costs(force=True)
        self.assertNotIn(expired, self.files)
        self.assertNotIn(expired, server._PENDING_REBUILD)
        self.assertIn(key, server._PENDING_REBUILD)
        archived = self.files[key][1]
        self.assertIsInstance(archived, CompactedRows)
        self.assertEqual(archived.original(), [row])
        self.assertEqual(sum(m["_cost_ticks"] for m in aggregate(self.files)[0].values()), 10_000_000)
        write_jsonl(path, dict(type="message", timestamp=iso(OLD), message={
            "role": "assistant", "model": "claude-opus-5-5", "usage": {"input": 100, "cost": .1}}))
        os.utime(path, (OLD, OLD))
        server._refresh_window_costs(force=True)
        self.assertEqual(self.files[key][1], [(*row[:9], "Opus 5.5")])
        self.assertNotIn(key, server._PENDING_REBUILD)
