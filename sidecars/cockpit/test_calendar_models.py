"""Model attribution, exact calendar accounting, and legacy rebuild coverage."""
import base64
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

import analyzer
from calendar_models import USAGE_FIELDS
from cost_archive import COMPACTED_UID, CompactedRows, cost_ticks, pack_rows, timezone_key, unpack_rows
from cost_compaction import exact_rows
from cost_migration import merge_history
from cost_windows import EVENT_TIMING_REV
from grok_costs import BILLED_UID, UNPLACED_UID
from metric_codex import read_codex
from metric_droid import read_droid
from metric_grok import read_grok
from metric_pi import PI_MTIME_UID, read_pi
from pricing import build_models_board
import server
import subagents
from test_cost_compaction import DAY, NOW, OLD, ScanFixture, aggregate, entry, event, folded, uid
from test_orchestrator_recognition import _build_db, _encode_gen_metadata, _encode_user_step


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def write_jsonl(path, *rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def legacy_pack(rows):
    """Encode the unprefixed v16/v17 wire layout, including its binary digest table."""
    hashes, records = bytearray(), []
    for row in rows:
        prefix, sep, tail = row[3].partition("#")
        if sep:
            digest, at, clock = tail.partition("@")
            hashes.extend(bytes.fromhex(digest))
            row = (*row[:3], [prefix, at + clock], *row[4:])
        records.append(row)
    payload = zlib.compress(json.dumps(records).encode())
    return base64.b85encode(zlib.compress(len(hashes).to_bytes(4, "big") + hashes + payload)).decode()


class CalendarModelTests(unittest.TestCase):
    def test_normalizes_4_8_9_and_10_fields(self):
        row = (NOW, .125, "gemini", uid(NOW).split("#")[0], 12, 3, 2, 1, 4, "Neon")
        self.assertEqual(server._normalize_cost_event(row[:4]), (*row[:7], 0, 0, ""))
        self.assertEqual(server._normalize_cost_event(row[:8]), (*row[:8], 0, ""))
        self.assertEqual(server._normalize_cost_event(row[:9]), (*row[:9], ""))
        self.assertEqual(server._normalize_cost_event((*row[:9], "  Neon \n")), row)
        self.assertEqual(server._normalize_cost_event((*row[:9], "   ")), (*row[:9], ""))

    def test_day_week_month_models_totals_sort_and_historical_fallback(self):
        today = datetime(2026, 11, 18, 12)
        now = today.timestamp()
        prior = (today - timedelta(days=1)).timestamp()
        main = (now, .251, "gemini", "", 600, 32000, 2400, 8, 0, "Neon")
        rows = [main, (prior, .1, "gemini", "", 10, 0, 50, 1, 4, "Neon")]
        rows += [(now, .25, "other", "", 0, 0, out, turns, 0, name)
                 for name, out, turns in (("B", 12, 2), ("D", 11, 99), ("C", 12, 1), ("A", 12, 2))]
        for family in ("gemini", "claude", "grok", "other"):
            rows.append((now, 0.0, family, "", 0, 0, 1, 0, 0, ""))
        rows.append(((today - timedelta(days=2)).timestamp(), 0.0, "other", "", 0, 0, 0, 0, 0, "Idle"))
        daily, _, _, calendar = aggregate(
            {"pi:test": entry(now, *rows), "codex:deleted": entry(now, (now, 0.0, "other", "", 0, 0, 1, 0, 0, ""))},
            now=now,
        )
        expected_names = ["Neon", "A", "B", "C", "D", "Claude（历史）", "Codex（历史）", "Gemini（历史）", "Grok（历史）", "其他模型（历史）"]
        for period, view in calendar.items():
            first = view["items"][0]
            self.assertEqual([model["model"] for model in first["models"]], expected_names)
            for item in view["items"]:
                for field in (*USAGE_FIELDS, "prompt", "total"):
                    self.assertEqual(sum(model[field] for model in item["models"]), item[field], (period, field))
                self.assertEqual(sum(cost_ticks(model["cost"]) for model in item["models"]), cost_ticks(item["cost"]))
            self.assertNotIn("models", view["summary"])
            self.assertEqual(view["items"][-1]["models"], [])
        self.assertEqual(calendar["day"]["items"][2]["models"], [])
        self.assertEqual(calendar["week"]["period_label"], "近7周")
        self.assertEqual(len(calendar["week"]["items"]), 7)
        today_neon = calendar["day"]["items"][0]["models"][0]
        self.assertEqual(today_neon, dict(model="Neon", turns=8, **{"in": 600}, cache_write=0, cache=32000,
                                         out=2400, prompt=32600, total=35000, hit_pct=98.2, cost=.251))
        for period in ("week", "month"):
            neon = calendar[period]["items"][0]["models"][0]
            self.assertEqual((neon["in"], neon["cache_write"], neon["prompt"], neon["total"], neon["cost"]),
                             (606, 4, 32610, 35060, .351))
            self.assertEqual(neon["hit_pct"], round(32000 * 100.0 / 32610, 1))
        self.assertIsNone(calendar["day"]["items"][0]["models"][1]["hit_pct"])
        for row in daily.values():
            self.assertEqual(sum(model["_cost_ticks"] for model in row["model_usage"].values()), row["_cost_ticks"])

    def test_each_model_rounds_once_per_item_with_no_tail_redistribution(self):
        now = datetime(2026, 11, 18, 12).timestamp()
        prior = datetime(2026, 11, 17, 12).timestamp()
        rows = (event(now, model="A", cost=.00000030), event(now, model="B", cost=.00000030),
                event(prior, model="A", cost=.00000030))
        daily, _, _, calendar = aggregate({"pi:test": entry(now, *rows)}, now=now)
        item = calendar["day"]["items"][0]
        self.assertEqual(item["cost"], .000001)
        self.assertEqual([model["cost"] for model in item["models"]], [0, 0])
        self.assertEqual(sum(cell["_cost_ticks"] for cell in daily[datetime.fromtimestamp(now).date()]["model_usage"].values()), 60)
        for period in ("week", "month"):
            item = calendar[period]["items"][0]
            self.assertEqual(item["cost"], .000001)
            self.assertEqual([(m["model"], m["cost"]) for m in item["models"]], [("A", .000001), ("B", 0)])

    def test_versioned_archive_and_legacy_payloads(self):
        row = event(OLD, uid(OLD, "0101::"), model="Neon")
        rows = [row, event(OLD + 5, family="claude", model="Opus 5.5"), event(OLD + 7)]
        self.assertEqual(unpack_rows(pack_rows(rows)), rows)
        old = [r[:9] for r in rows]
        self.assertEqual(unpack_rows(legacy_pack(old)), [(*r, "") for r in old])
        with_dates = [(*r, "2027-01-01") for r in old]
        self.assertEqual(unpack_rows(legacy_pack(with_dates)), [(*r, "") for r in old])

    def test_compaction_keeps_model_family_and_local_day_separate(self):
        noon = datetime(2027, 1, 1, 12)
        first, second = noon.timestamp(), (noon + timedelta(days=1)).timestamp()
        rows = [event(first, model="Neon", cost=.00100002), event(first + 1, model="Neon", cost=.00200048),
                event(first, model="Argon"), event(first, model="Neon", family="claude"), event(second, model="Neon")]
        original = {"pi:test": entry(first, *rows)}
        compacted = folded(original)
        summaries = compacted["pi:test"][1]
        self.assertEqual([(r[2], r[9], r[3]) for r in summaries], [
            ("gemini", "Neon", COMPACTED_UID), ("gemini", "Argon", COMPACTED_UID),
            ("claude", "Neon", COMPACTED_UID), ("gemini", "Neon", COMPACTED_UID),
        ])
        self.assertEqual(summaries.days, (noon.toordinal(),) * 3 + (noon.toordinal() + 1,))
        self.assertEqual(summaries[0][1], .00300050)
        self.assertEqual(aggregate(original), aggregate(compacted))
        self.assertEqual(exact_rows([("pi:test", summaries)], zone="changed"), [("pi:test", rows)])


class CalendarModelSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def assert_models(self, events, labels):
        self.assertTrue(events)
        self.assertTrue(all(len(row) == 10 and row[9] and row[9] == row[9].strip() for row in events))
        self.assertEqual({row[9] for row in events}, set(labels))

    def test_agy_database_scan_and_analysis_callback_match_model_board(self):
        path = self.root / "calendar-db.db"
        _build_db(path, [_encode_gen_metadata(1, 1532, 1000, 2000, 300),
                         _encode_gen_metadata(2, 326, 1000, 2000, 300)],
                  [(0, 14, 3, b"", _encode_user_step(1532)), (1, 15, 3, b"", b""), (2, 15, 3, b"", b"")])
        calls = []
        analysis = analyzer._analyze_conversation_base(
            str(path), path.stem, "test", "test", NOW, include_subagents=False,
            on_cost_events=lambda cid, mtime, rows: calls.extend(rows))
        labels = {item["model"] for item in analysis["main_models"]}
        self.assertEqual(labels, {"Neon", "Argon"})
        self.assert_models(calls, labels)
        self.assert_models(analyzer.extract_db_cost_events(path, NOW), labels)

    def test_agy_transcript_and_pi_use_canonical_board_names(self):
        tr = write_jsonl(self.root / "transcript.jsonl", dict(
            type="PLANNER_RESPONSE", model=" gemini-pro-orchestrator-neon ", step_index=1,
            input_tokens=100, cache_read_tokens=20, output_tokens=10, created_at=iso(NOW - 10)))
        metrics = subagents.analyze_agy_transcript_fallback(tr)
        self.assert_models(metrics["cost_events"], {"Neon"})
        pi = write_jsonl(self.root / "pi.jsonl", dict(
            type="message", timestamp=iso(NOW - 10), message={
                "role": "assistant", "model": " claude-opus-5-5 ", "usage": {"input": 100, "cacheWrite": 50}}))
        metrics = read_pi(pi, NOW)
        self.assert_models(metrics["cost_events"], {item["model"] for item in build_models_board([], [metrics])["items"]})

    def test_codex_modern_and_legacy_records_keep_models_and_turns(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 50, "cache_write_input_tokens": 20, "output_tokens": 5}
        records = (("token_usage_record", {"usage": usage}),
                   ("event_msg", {"type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage}}))
        for kind, payload in records:
            with self.subTest(kind=kind):
                path = write_jsonl(self.root / f"{kind}.jsonl",
                                   dict(type="turn_context", payload={"model": "gpt-6.2-sol", "effort": "high"}),
                                   dict(type=kind, payload=payload, timestamp=iso(NOW - 10)))
                metrics = read_codex(path, NOW)
                self.assert_models(metrics["cost_events"], {"gpt-6.2-sol"})
                self.assertEqual(metrics["cost_events"][0][7:9], (1, 20))

    def test_droid_full_session_turn_and_cost_events_use_model_board_names(self):
        path = self.root / "droid.settings.json"
        path.write_text(json.dumps({"model": "claude-opus-5-5", "tokenUsage": {"inputTokens": 100}}))
        tr = write_jsonl(self.root / "droid.jsonl",
                         dict(turnId="1", timestamp=iso(NOW - 10), message={"role": "assistant", "model": "claude-sonnet-5-5"}),
                         dict(type="agent_turn_outcome", turnId="1", timestamp=iso(NOW - 9), model="claude-sonnet-5-5"))
        metrics = read_droid(path, None, tr, NOW)
        self.assert_models(metrics["cost_events"], {"Sonnet 5.5"})
        self.assertEqual([e[7] for e in metrics["cost_events"]], [0, 1])

    def test_grok_billed_and_unplaced_paths_carry_attributed_models(self):
        model_usage = {" grok-4.7-fast ": {"inputTokens": 80, "outputTokens": 10, "costUsdTicks": 1_000_000}}
        usage = {"inputTokens": 80, "outputTokens": 10, "costUsdTicks": 2_000_000, "modelUsage": model_usage}
        upd = write_jsonl(self.root / "updates.jsonl", dict(
            params={"update": {"sessionUpdate": "turn_completed", "usage": usage},
                    "_meta": {"agentTimestampMs": (NOW - 10) * 1000}}))
        summary = self.root / "usage.json"
        summary.write_text(json.dumps({"inputTokens": 180, "outputTokens": 20, "costUsdTicks": 3_000_000}))
        rows = read_grok(None, upd, None, summary, None, NOW, NOW)["cost_events"]
        self.assertEqual([(e[3], e[9]) for e in rows],
                         [(BILLED_UID, "grok-4.7-fast"), (BILLED_UID, ""), (UNPLACED_UID, "")])
        self.assertEqual(sum(cost_ticks(e[1]) for e in rows), 30000)
        summary.write_text(json.dumps(usage))
        rows = read_grok(None, None, None, summary, None, NOW, NOW)["cost_events"]
        self.assertEqual([(e[3], e[9], cost_ticks(e[1])) for e in rows],
                         [(UNPLACED_UID, "grok-4.7-fast", 10000), (UNPLACED_UID, "", 10000)])

    def test_grok_places_disclosed_model_remainder_after_same_model_subtraction(self):
        regular, fast = "grok-4.7", "grok-4.7-fast"
        placed = dict(inputTokens=60, cachedReadTokens=8, cacheCreationTokens=2, outputTokens=10,
                      costUsdTicks=6_000_000_000)
        extra = dict(inputTokens=40, cachedReadTokens=2, cacheCreationTokens=3, outputTokens=5,
                     costUsdTicks=4_000_000_000)
        upd = write_jsonl(self.root / "updates.jsonl", dict(
            params={"update": {"sessionUpdate": "turn_completed", "usage": placed},
                    "_meta": {"agentTimestampMs": (NOW - 10) * 1000}}))
        summary = self.root / "usage.json"
        total = {k: placed[k] + extra[k] for k in placed}
        summary.write_text(json.dumps(dict(total, modelUsage={regular: placed, fast: extra})))
        rows = read_grok(None, upd, None, summary, None, NOW, NOW)["cost_events"]
        self.assertEqual([(e[0], e[9], e[4:7], e[8], cost_ticks(e[1])) for e in rows], [
            (NOW - 10, regular, (52, 8, 10), 2, 60_000_000),
            (NOW, fast, (38, 2, 5), 3, 40_000_000),
        ])

    def test_grok_partial_model_detail_preserves_unassigned_tokens_and_cost(self):
        partial = dict(inputTokens=60, cacheCreationTokens=14, cachedReadTokens=2, outputTokens=7,
                       costUsdTicks=6_000_000_000)
        total = dict(inputTokens=100, cacheCreationTokens=20, cachedReadTokens=4, outputTokens=12,
                     costUsdTicks=10_000_000_000, modelUsage={"grok-4.7-fast": partial})
        summary = self.root / "usage.json"
        summary.write_text(json.dumps(total))
        upd = write_jsonl(self.root / "updates.jsonl", {
            "params": {"update": {"sessionUpdate": "turn_completed", "usage": total}}})
        complete = self.root / "complete.json"
        complete.write_text(json.dumps(dict(total, modelUsage={**total["modelUsage"], "grok-4.7": {
            "inputTokens": 40, "cacheCreationTokens": 6, "cachedReadTokens": 2, "outputTokens": 5,
            "costUsd": .4}})))
        for result, expected_uid in ((read_grok(None, None, None, summary, None, NOW, NOW), UNPLACED_UID),
                                     (read_grok(None, upd, None, None, None, NOW, 0), BILLED_UID),
                                     (read_grok(None, upd, None, complete, None, NOW, NOW), BILLED_UID)):
            with self.subTest(uid=expected_uid):
                rows = result["cost_events"]
                self.assertEqual([(e[3], e[9], e[4:7], e[8], cost_ticks(e[1])) for e in rows], [
                    (expected_uid, "grok-4.7-fast", (58, 2, 7), 14, 60_000_000),
                    (expected_uid, "", (38, 2, 5), 6, 40_000_000),
                ])
                calendar = aggregate({"grok:s": entry(NOW, *rows)})[3]["day"]["items"][0]
                self.assertEqual({m["model"]: (m["prompt"], m["cost"]) for m in calendar["models"]},
                                 {"grok-4.7-fast": (60, .6), "Grok（历史）": (40, .4)})


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
                   analyzer.cached_paths(base, pattern, recursive, ttl=0))
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
