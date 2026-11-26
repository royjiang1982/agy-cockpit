"""Acceptance regressions for exact history, cache integrity, and scan locking."""
import copy
import json
import os
import unittest
from unittest.mock import patch

import cost_archive
import server
from cost_archive import CompactedRows, persisted_compaction
from test_cost_compaction import DAY, NOW, OLD, ScanFixture, aggregate, entry, event, folded, uid


class ArchiveMathTests(unittest.TestCase):
    def test_exact_half_microdollar_across_families_files_months_and_snapshot(self):
        files = {
            "pi:a": entry(OLD, event(OLD, cost=.00100002), event(OLD, cost=.00200048),
                          event(OLD, family="claude", cost=.00000301)),
            "pi:b": entry(OLD, event(OLD, cost=.00000999), event(OLD - DAY, cost=.2100035)),
        }
        compressed = folded(files)
        self.assertEqual(compressed["pi:a"][1][0][1], .00300050)
        self.assertEqual(aggregate(files), aggregate(compressed))
        self.assertEqual(server._calendar_summary([{"cost": .00300050}])["cost"], .003000)
        snapshot = json.loads(json.dumps(persisted_compaction(compressed)))
        for key, (mtime, rows) in compressed.items():
            compressed[key] = mtime, cost_archive.load_compacted_rows(key, list(rows), snapshot)
        self.assertEqual(aggregate(files), aggregate(compressed))


class ArchiveScanTests(ScanFixture):
    def snapshot(self, version=18):
        originals = {"db:bad": entry(OLD, event(OLD, uid(OLD))),
                     "db:good": entry(OLD, event(OLD, uid(OLD, digest="b"*64))),
                     "pi:live": entry(NOW, event(NOW))}
        files = folded(originals) if version >= 17 else originals
        return originals, {"schema_version": version, "pricing_epoch": server.PRICING_EPOCH,
                           "event_timing_rev": 2, "files": files, **persisted_compaction(files)}

    def write_and_load(self, payload):
        self.cache.write_text(json.dumps(payload), encoding="utf-8")
        self.files.clear()
        server._PENDING_REBUILD.clear()
        server._COST_CACHE_LOADED = False
        server._load_cost_events_cache()

    def test_schema_18_writes_and_15_16_17_18_read(self):
        for version in (15, 16, 17, 18):
            with self.subTest(version=version):
                originals, payload = self.snapshot(version)
                self.write_and_load(payload)
                self.assertTrue(server._COST_CACHE_LOADED)
                self.assertEqual(self.runtime.damaged, set())
                self.assertEqual(aggregate(self.files), aggregate(originals))
                server._save_cost_events_cache()
                self.assertEqual(json.loads(self.cache.read_text())["schema_version"], 18)
        legacy, v16 = self.snapshot()
        v16["schema_version"] = 16
        del v16["compacted_zone"]
        with self.assertLogs(server._LOG, level="WARNING"):
            self.write_and_load(v16)
        self.assertIn("#", self.files["db:bad"][1][0][3])
        self.assertEqual(aggregate(self.files), aggregate(legacy))

    def test_recover_each_bad_metadata_without_losing_another_file(self):
        originals, payload = self.snapshot()
        cases = [
            ("compacted_dates", None, False), ("compacted_db_digests", [], False),
            ("compacted_links", None, False), ("compacted_zone", 123, False),
            ("compacted_dates", [0], True), ("compacted_dates", ["123"], True),
            ("compacted_dates", [True], True), ("compacted_dates", [99999999], True),
            ("compacted_db_digests", "f"*16, True), ("compacted_links", [123], True),
            ("compacted_zone", "invalid timezone", True),
        ]
        for field, value, individual in cases:
            with self.subTest(field=field, value=value):
                broken = copy.deepcopy(payload)
                if individual:
                    broken[field]["db:bad"] = value
                else:
                    broken[field] = value
                with self.assertLogs(server._LOG, level="WARNING"):
                    self.write_and_load(broken)
                self.assertEqual(aggregate(self.files), aggregate(originals))
                self.assertNotIsInstance(self.files["db:bad"][1], CompactedRows)
                self.assertFalse(self.runtime.damaged)
        broken = copy.deepcopy(payload)
        del broken["compacted_db_digests"]["db:bad"]
        with self.assertLogs(server._LOG, level="WARNING"):
            self.write_and_load(broken)
        self.assertEqual(aggregate(self.files), aggregate(originals))

    def test_bad_archive_does_not_block_startup_or_save_partial_cache(self):
        original, payload = self.snapshot()
        payload["compacted_rows"]["db:bad"] = "not-a-packed-archive"
        payload["compacted_dates"]["db:bad"] = [0]
        with self.assertLogs(server._LOG, level="ERROR") as logs:
            self.write_and_load(payload)
        self.assertIn("db:bad", "\n".join(logs.output))
        self.assertIn("queued rebuild", "\n".join(logs.output))
        self.assertEqual(self.runtime.damaged, {"db:bad"})
        self.assertIn("db:bad", server._PENDING_REBUILD)
        self.assertIn("db:good", self.files)
        self.assertIn("pi:live", self.files)
        server._recompute_window_costs_from_memory()
        remaining = self.cache.read_bytes()
        server._save_cost_events_cache()
        self.assertEqual(self.cache.read_bytes(), remaining)
        db = self.touch(self.root / "conversations" / "bad.db", NOW - 1)
        self.patch("get_db_mtime", lambda p: p.stat().st_mtime)
        self.runtime._last_save_attempt = float("-inf")
        with patch.object(server, "get_cached_analysis_events", return_value=None), \
             patch.object(server, "extract_db_cost_events", return_value=original["db:bad"][1]) as scan:
            server._refresh_window_costs(force=True)
        scan.assert_called_once_with(db, NOW - 1)
        self.assertNotIn("db:bad", server._PENDING_REBUILD)
        self.assertFalse(self.runtime.damaged)
        self.assertEqual(aggregate(self.files), aggregate(original))
        self.assertNotEqual(self.cache.read_bytes(), remaining)
        self.assertEqual(json.loads(self.cache.read_text())["schema_version"], 18)

    def test_truncated_rescan_does_not_release_damaged_snapshot_guard(self):
        _, payload = self.snapshot()
        payload["compacted_rows"]["db:bad"] = "invalid"
        with self.assertLogs(server._LOG, level="ERROR"):
            self.write_and_load(payload)
        disk = self.cache.read_bytes()
        self.touch(self.root / "conversations" / "bad.db", NOW - 1)
        self.patch("get_db_mtime", lambda p: p.stat().st_mtime)
        with patch.object(server, "get_cached_analysis_events", return_value=None),              patch.object(server, "extract_db_cost_events", return_value=[]):
            server._refresh_window_costs(force=True)
        self.assertIn("db:bad", self.runtime.damaged)
        self.assertIn("db:bad", server._PENDING_REBUILD)
        self.assertEqual(self.cache.read_bytes(), disk)

    def test_bad_archive_map_preserves_other_files_and_protects_disk(self):
        for value in (None, [], "bad"):
            with self.subTest(value=value):
                _, payload = self.snapshot()
                payload["compacted_rows"] = value
                with self.assertLogs(server._LOG, level="ERROR"):
                    self.write_and_load(payload)
                self.assertEqual(self.runtime.damaged, {"db:bad", "db:good"})
                self.assertEqual(set(self.files), {"db:bad", "db:good", "pi:live"})
                old = self.cache.read_bytes()
                server._save_cost_events_cache()
                self.assertEqual(self.cache.read_bytes(), old)

    def test_bad_json_is_not_overwritten(self):
        self.cache.write_text('{"files":', encoding="utf-8")
        server._COST_CACHE_LOADED = False
        with self.assertLogs(server._LOG, level="ERROR"):
            server._load_cost_events_cache()
        server._save_cost_events_cache()
        self.assertEqual(self.cache.read_text(), '{"files":')

    def assert_archive_io_unlocked(self, packed):
        self.assertTrue(server._COST_LOCK.acquire(blocking=False), "archive I/O held _COST_LOCK")
        server._COST_LOCK.release()
        return self._real_unpack(packed)

    def test_same_mtime_db_callback_keeps_new_analysis_and_never_unpacks_under_lock(self):
        inp = event(OLD, uid(OLD))
        self.files.update(folded({"db:cid": entry(OLD, inp)}))
        self._real_unpack = cost_archive.unpack_rows
        with patch.object(cost_archive, "unpack_rows", side_effect=self.assert_archive_io_unlocked) as decode:
            server._update_conv_cost_events("cid", OLD, [inp])
            self.assertIsInstance(self.files["db:cid"][1], CompactedRows)
            extra = event(OLD + 1, "7:12:3:2:1")
            server._update_conv_cost_events("cid", OLD, [inp, extra])
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(self.files["db:cid"], entry(OLD, inp, extra))

    def test_truncated_transcript_retains_tail_and_never_unpacks_under_lock(self):
        db = self.touch(self.root / "conversations" / "cid.db", NOW - 1)
        tr = self.touch(self.root / "brain" / "cid" / ".system_generated" / "logs" / "transcript.jsonl", OLD)
        parent = event(OLD, uid(OLD))
        known, tail = event(OLD, uid(OLD).split("#")[0]), event(OLD + 200)
        raw = {"db:cid": entry(OLD, parent), "agy_tr:cid": entry(OLD, known, tail)}
        self.files.update(folded(raw))
        self.patch("get_db_mtime", lambda p: p.stat().st_mtime)
        self._real_unpack = cost_archive.unpack_rows
        with patch.object(cost_archive, "unpack_rows", side_effect=self.assert_archive_io_unlocked) as decode, \
             patch.object(server, "extract_db_cost_events", return_value=[parent]) as scan_db, \
             patch.object(server, "get_cached_analysis_events", return_value=None), \
             patch.object(server, "analyze_agy_transcript_fallback", return_value={"cost_events": [known]}) as scan_tr:
            server._refresh_window_costs(force=True)
            server._refresh_window_costs(force=True)
        scan_db.assert_called_once_with(db, NOW - 1)
        self.assertEqual(scan_tr.call_count, 2)
        self.assertTrue(decode.called)
        self.assertEqual(self.files["agy_tr:cid"][1], [tail, known])
        self.assertEqual(aggregate(self.files), aggregate(raw))

    def test_same_mtime_scan_does_not_clobber_newer_callback(self):
        db = self.touch(self.root / "conversations" / "cid.db", NOW - 1)
        parent, extra = event(OLD, uid(OLD)), event(NOW - 60, "7:12:3:2:1")
        self.files.update(folded({"db:cid": entry(OLD, parent)}))
        self.patch("get_db_mtime", lambda p: p.stat().st_mtime)
        def stale_scan(p, mtime):
            self.assertEqual(p, db)
            server._update_conv_cost_events("cid", mtime, [parent, extra])
            return [parent]
        with patch.object(server, "get_cached_analysis_events", return_value=None), \
             patch.object(server, "extract_db_cost_events", side_effect=stale_scan):
            server._refresh_window_costs(force=True)
        self.assertEqual(self.files["db:cid"][1], [parent, extra])

    def test_probe_is_parsed_once_when_empty_and_metered_after_mtime_change(self):
        probe = self.touch(self.factory / "-private-tmp-agy-cockpit-droid-probe" / "probe.settings.json", NOW - 120)
        key = f"droid:{probe}"
        def settings(tokens, mtime):
            probe.write_text(json.dumps({"model": "claude-sonnet-4-6", "tokenUsage": {"inputTokens": tokens}}))
            os.utime(probe, (mtime, mtime))
        self.runtime._last_save_attempt = float("-inf")
        settings(0, NOW - 120)
        with patch.object(server, "analyze_droid_session", wraps=server.analyze_droid_session) as scan:
            server._refresh_window_costs(force=True)
            server._refresh_window_costs(force=True)
            scan.assert_called_once_with(probe)
            self.assertNotIn(key, self.files)
            self.assertFalse(self.ledger.has(key))
            settings(1000, NOW - 90)
            server._refresh_window_costs(force=True)
            self.assertGreater(sum(row[1] for row in self.files[key][1]), 0)
            initial = sum(row[1] for row in self.files[key][1])
            settings(2000, NOW - 60)
            self.runtime._last_save_attempt = float("-inf")
            server._refresh_window_costs(force=True)
            server._refresh_window_costs(force=True)
        self.assertEqual(scan.call_count, 3)
        self.assertGreater(sum(row[1] for row in self.files[key][1]), initial)
        self.assertEqual(self.ledger.watermarks[key]["raw_inp"], 2000)
        self.assertIn(key, json.loads(self.cache.read_text())["files"])
