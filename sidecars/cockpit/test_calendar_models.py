"""Model attribution, exact calendar accounting, and legacy rebuild coverage."""
import base64
from datetime import datetime, timedelta, timezone
import json
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

import analyzer
from calendar_models import USAGE_FIELDS
from cost_archive import COMPACTED_UID, cost_ticks, pack_rows, unpack_rows
from cost_compaction import exact_rows
from grok_costs import BILLED_UID, UNPLACED_UID
from metric_codex import read_codex
from metric_droid import read_droid
from metric_grok import read_grok
from metric_pi import read_pi
from pricing import build_models_board
import server
import subagents
from test_cost_compaction import NOW, OLD, aggregate, entry, event, folded, uid
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
        self.assertEqual(calendar["day"]["period_label"], "近15天")
        self.assertEqual(len(calendar["day"]["items"]), 15)
        self.assertEqual(calendar["week"]["period_label"], "近15周")
        self.assertEqual(len(calendar["week"]["items"]), 15)
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
