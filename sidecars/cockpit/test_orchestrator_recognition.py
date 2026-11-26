"""Regression tests for Orchestrator model recognition and Models Board UI grid."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analyzer
import droid_acp_fallback
import pricing
import subagents


def _varint(val: int) -> bytes:
    out = bytearray()
    v = int(val)
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_varint(fnum: int, val: int) -> bytes:
    return _varint((fnum << 3) | 0) + _varint(val)


def _field_bytes(fnum: int, payload: bytes) -> bytes:
    return _varint((fnum << 3) | 2) + _varint(len(payload)) + payload


def _encode_gen_metadata(
    step_idx: int,
    model_enum: int,
    inp: int,
    cache: int,
    out: int,
    max_ctx: int | None = None,
) -> bytes:
    usage = _field_varint(2, inp) + _field_varint(3, out) + _field_varint(5, cache)
    f1 = _field_bytes(4, usage) + _field_varint(3, model_enum)
    if max_ctx is not None:
        cwm = _field_varint(1, inp + cache) + _field_varint(4, max_ctx)
        f1 += _field_bytes(9, _field_bytes(10, cwm))
    return _field_bytes(1, f1) + _field_varint(2, step_idx)


def _encode_user_step(model_enum: int) -> bytes:
    f1 = _field_varint(1, model_enum)
    return _field_bytes(19, _field_bytes(12, _field_bytes(1, f1)))


def _build_db(path: Path, gen_rows: list[bytes], step_rows: list[tuple[int, int, int, bytes, bytes]]) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    con.execute(
        "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB)"
    )
    for i, blob in enumerate(gen_rows):
        con.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (i, blob))
    for idx, stype, status, meta, payload in step_rows:
        con.execute(
            "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
            (idx, stype, status, meta, payload),
        )
    con.commit()
    con.close()


class OrchestratorRecognitionTests(unittest.TestCase):
    def setUp(self):
        with pricing._DYNAMIC_MODEL_LOCK:
            self._saved_obs = dict(pricing._OBSERVED_MODEL_CTX_LIMITS)
            pricing._OBSERVED_MODEL_CTX_LIMITS.clear()

    def tearDown(self):
        with pricing._DYNAMIC_MODEL_LOCK:
            pricing._OBSERVED_MODEL_CTX_LIMITS.clear()
            pricing._OBSERVED_MODEL_CTX_LIMITS.update(self._saved_obs)

    def test_canonical_label_and_quota_family_for_all_orchestrators(self):
        expected_labels = {
            "gemini-pro-orchestrator": "Carbon",
            "Orchestrator (Carbon)": "Carbon",
            "gemini-pro-orchestrator-argon": "Argon",
            "Orchestrator (Argon)": "Argon",
            "gemini-pro-orchestrator-neon": "Neon",
            "Orchestrator (Neon)": "Neon",
        }
        for raw, label in expected_labels.items():
            self.assertEqual(pricing.canonical_model_label(raw), label, raw)
            self.assertEqual(pricing.model_quota_family(raw), "gemini", raw)

        self.assertEqual(pricing.model_quota_family("Custom Orchestrator"), "gemini")
        self.assertEqual(pricing.model_quota_family("Claude Orchestrator"), "claude")

    def test_resolve_builtin_enum_from_parent_pro_capabilities(self):
        for enum_id in (1667, 1681, 1537, 1532, 1525, 1427, 1417):
            self.assertEqual(analyzer.resolve_builtin_enum_from_parent(enum_id, "pro"), enum_id)
        for enum_id in (1322, 1402, 342):
            self.assertEqual(analyzer.resolve_builtin_enum_from_parent(enum_id, "pro"), 1437)

    def test_continuous_orchestrator_turns_keep_context_limit_and_real_switch_resets(self):
        with tempfile.TemporaryDirectory(prefix="cockpit-orch-ctx-") as tmp:
            db_same = Path(tmp) / "orch-same.db"
            _build_db(
                db_same,
                [_encode_gen_metadata(1, 1532, 20_000, 180_000, 600, max_ctx=1_000_000)],
                [
                    (0, 14, 3, b"", _encode_user_step(1537)),
                    (1, 15, 3, b"", b""),
                    (2, 14, 1, b"", _encode_user_step(1537)),
                ],
            )
            res_same = analyzer._analyze_conversation_base(
                str(db_same), "orch-same", "t", "app", db_same.stat().st_mtime, include_subagents=False
            )
            self.assertEqual(res_same["context"]["limit"], 1_000_000)

            db_switch = Path(tmp) / "orch-switch.db"
            _build_db(
                db_switch,
                [_encode_gen_metadata(1, 342, 10_000, 50_000, 300, max_ctx=131_072)],
                [
                    (0, 14, 3, b"", _encode_user_step(342)),
                    (1, 15, 3, b"", b""),
                    (2, 14, 1, b"", _encode_user_step(1437)),
                ],
            )
            res_switch = analyzer._analyze_conversation_base(
                str(db_switch), "orch-switch", "t", "app", db_switch.stat().st_mtime, include_subagents=False
            )
            self.assertEqual(res_switch["context"]["limit"], pricing.CTX_WINDOW_MAX)

            db_orch_switch = Path(tmp) / "orch-switch-tier.db"
            _build_db(
                db_orch_switch,
                [_encode_gen_metadata(1, 1532, 20_000, 180_000, 600, max_ctx=1_000_000)],
                [
                    (0, 14, 3, b"", _encode_user_step(1537)),
                    (1, 15, 3, b"", b""),
                    (2, 14, 1, b"", _encode_user_step(1667)),
                ],
            )
            res_orch_switch = analyzer._analyze_conversation_base(
                str(db_orch_switch),
                "orch-switch-tier",
                "t",
                "app",
                db_orch_switch.stat().st_mtime,
                include_subagents=False,
            )
            self.assertEqual(res_orch_switch["context"]["limit"], pricing.CTX_WINDOW_MAX)

    def test_builtin_override_is_fallback_only_when_db_call_lacks_model_enum(self):
        with tempfile.TemporaryDirectory(prefix="cockpit-sub-ovr-") as tmp:
            db_real = Path(tmp) / "sub-real.db"
            _build_db(
                db_real,
                [_encode_gen_metadata(1, 1532, 10_000, 20_000, 500, max_ctx=512_000)],
                [(0, 14, 3, b"", b""), (1, 15, 3, b"", b"")],
            )
            res_real = analyzer._analyze_conversation_base(
                str(db_real),
                "sub-real",
                "t",
                "app",
                db_real.stat().st_mtime,
                include_subagents=False,
                model_enum_override=1437,
            )
            self.assertEqual(res_real["model"]["short"], "Neon High")
            self.assertEqual([m["model"] for m in res_real["main_models"]], ["Neon"])

            with analyzer._BUILTIN_ENUM_LOCK:
                prev_sub = analyzer._BUILTIN_SUBAGENT_MODEL_ENUM.get("sub-real")
                analyzer._BUILTIN_SUBAGENT_MODEL_ENUM["sub-real"] = 1437
            try:
                ev_real = analyzer.extract_db_cost_events(db_real, db_real.stat().st_mtime)
            finally:
                with analyzer._BUILTIN_ENUM_LOCK:
                    if prev_sub is None:
                        analyzer._BUILTIN_SUBAGENT_MODEL_ENUM.pop("sub-real", None)
                    else:
                        analyzer._BUILTIN_SUBAGENT_MODEL_ENUM["sub-real"] = prev_sub

            expected_neon_cost = pricing.calc_call_cost("Gemini 4 Neon (High)", 10_000, 20_000, 500)
            self.assertAlmostEqual(ev_real[0][1], expected_neon_cost, places=6)

            db_zero = Path(tmp) / "sub-zero.db"
            _build_db(
                db_zero,
                [_encode_gen_metadata(1, 0, 10_000, 20_000, 500)],
                [(0, 14, 3, b"", b""), (1, 15, 3, b"", b"")],
            )
            res_zero = analyzer._analyze_conversation_base(
                str(db_zero),
                "sub-zero",
                "t",
                "app",
                db_zero.stat().st_mtime,
                include_subagents=False,
                model_enum_override=1532,
            )
            self.assertEqual(res_zero["model"]["short"], "Neon High")
            self.assertEqual([m["model"] for m in res_zero["main_models"]], ["Neon"])

    def test_transcript_fallback_emits_per_model_for_models_board(self):
        with tempfile.TemporaryDirectory(prefix="cockpit-tr-pm-") as tmp:
            tr = Path(tmp) / "transcript.jsonl"
            lines = [
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-pro-orchestrator-neon",
                    "step_index": 1,
                    "input_tokens": 2000,
                    "cache_read_tokens": 8000,
                    "output_tokens": 400,
                    "created_at": "2026-11-25T10:00:00Z",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-3.8-flash-high",
                    "step_index": 2,
                    "input_tokens": 1000,
                    "cache_read_tokens": 4000,
                    "output_tokens": 200,
                    "created_at": "2026-11-25T10:00:05Z",
                    "status": "DONE",
                },
            ]
            tr.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
            fb = subagents.analyze_agy_transcript_fallback(tr)
            self.assertIsNotNone(fb)
            self.assertEqual([m["model"] for m in fb["per_model"]], ["Orchestrator (Neon)", "Flash High"])

            board = pricing.build_models_board([], [fb])
            self.assertEqual([it["model"] for it in board["items"]], ["Neon", "Flash"])

    def test_index_html_models_board_grid_and_js_syntax(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="modelPanel"', html)
        self.assertIn(".mb-chart {", html)
        self.assertIn(".mb-bar-col:not(.is-zero) .cal-bar-fill {", html)
        self.assertNotIn("' · 旧'", html)
        self.assertNotIn("'待刷新'", html)
        self.assertNotIn("b.stale", html)
        self.assertIn("if (!b || b.estimate_ok !== true) return null;", html)
        self.assertIn("if (b && Number.isFinite(b.pct)) pcts.push(b.pct);", html)
        self.assertIn("triggerQuotaRefresh({ manual: false });", html)

        scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
        self.assertTrue(scripts)
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=True) as tf:
            tf.write("\n".join(scripts).replace("__UI_VERSION__", "1"))
            tf.flush()
            proc = subprocess.run(["node", "--check", tf.name], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_discovery_hints_resolves_new_handoff_session_ids_for_all_engines(self):
        old_id = "11111111-1111-4111-8111-111111111111"
        new_id = "22222222-2222-4222-8222-222222222222"
        parent_id = "99999999-9999-4999-8999-999999999999"

        for agent, init_line in (
            ("agy", json.dumps({"event": "init", "agent": "agy", "conversation_id": new_id})),
            ("grok", json.dumps({"event": "init", "agent": "grok", "session_id": new_id})),
            ("droid", json.dumps({"event": "init", "agent": "droid", "session_id": new_id, "model": "claude-opus-5-5"})),
            ("codex", json.dumps({"type": "thread.started", "thread_id": new_id})),
        ):
            handoff_txt = json.dumps({"event": "session_handoff", "from_session_id": old_id}) + "\n" + init_line + "\n"
            prog, sid, _, _ = droid_acp_fallback.discovery_hints(handoff_txt, agent, None, "", "", parent_id)
            self.assertEqual(prog, agent)
            self.assertEqual(sid, new_id)

        self.assertIsNone(droid_acp_fallback.acp_metrics(None))

    def test_discover_subagent_stubs_tracks_handoff_new_id_and_pending_without_clobbering_old(self):
        parent_cid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        old_sid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        new_sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

        with tempfile.TemporaryDirectory(prefix="cockpit-handoff-stubs-") as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            proj_dir = root / "proj"
            (app_dir / "conversations").mkdir(parents=True, exist_ok=True)
            bdir = app_dir / "brain" / parent_cid / ".system_generated"
            (bdir / "steps" / "1").mkdir(parents=True, exist_ok=True)
            (bdir / "steps" / "2").mkdir(parents=True, exist_ok=True)
            (proj_dir / "tmp").mkdir(parents=True, exist_ok=True)

            rf1 = proj_dir / "tmp" / "opus.jsonl"
            rf1.write_text(
                json.dumps({"event": "init", "agent": "agy", "conversation_id": old_sid}) + "\n",
                encoding="utf-8",
            )
            rf2 = proj_dir / "tmp" / "opus-r2.jsonl"
            rf2.write_text(
                json.dumps({"event": "session_handoff", "from_session_id": old_sid})
                + "\n"
                + json.dumps({"event": "init", "agent": "agy", "conversation_id": new_sid})
                + "\n",
                encoding="utf-8",
            )
            (bdir / "steps" / "1" / "output.txt").write_text("■ 完成\n", encoding="utf-8")

            cmd1 = f"cd {proj_dir} && sa-run.sh tmp/opus.jsonl sa-agy --model claude-opus-5-5-high --prompt hi"
            cmd2 = f"cd {proj_dir} && sa-run.sh tmp/opus-r2.jsonl sa-agy --conversation {old_sid} --model claude-opus-5-5-high --prompt more"
            p1 = _field_bytes(1, b"run_command") + _field_bytes(2, json.dumps({"CommandLine": cmd1}).encode("utf-8"))
            p2 = _field_bytes(1, b"run_command") + _field_bytes(2, json.dumps({"CommandLine": cmd2}).encode("utf-8"))

            db_path = app_dir / "conversations" / f"{parent_cid}.db"
            _build_db(db_path, [], [(1, 132, 3, b"", p1), (2, 132, 2, b"", p2)])

            orig_app_dirs = subagents.APP_DIRS
            orig_sub_dirs = subagents.SUBAGENT_APP_DIRS
            try:
                subagents.APP_DIRS = [("App", app_dir)]
                subagents.SUBAGENT_APP_DIRS = [("App", app_dir)]
                subagents._SUBAGENT_STUBS_CACHE.clear()
                subagents._STEP_DEPENDENCIES.clear()
                with subagents._STUBS_PARSE_LOCK:
                    subagents._COMPLETED_STEP_STUBS.clear()

                # Case 1: Handoff with init written -> keeps old_sid (done) AND adds new_sid (running)
                stubs = {s["id"]: s for s in subagents.discover_subagent_stubs(parent_cid)}
                self.assertIn(old_sid, stubs)
                self.assertIn(new_sid, stubs)
                self.assertTrue(stubs[old_sid]["has_done_marker"])
                self.assertEqual(stubs[new_sid]["step_status"], 2)
                self.assertFalse(stubs[new_sid]["has_done_marker"])

                # Case 2: Handoff announced before init arrives -> pending-2, does NOT clobber old_sid
                rf2.write_text(
                    json.dumps({"event": "session_handoff", "from_session_id": old_sid}) + "\n",
                    encoding="utf-8",
                )
                subagents._SUBAGENT_STUBS_CACHE.clear()
                stubs_pending = {s["id"]: s for s in subagents.discover_subagent_stubs(parent_cid)}
                self.assertIn(old_sid, stubs_pending)
                self.assertIn("pending-2", stubs_pending)
                self.assertTrue(stubs_pending[old_sid]["has_done_marker"])

                # Case 3: Normal in-place resume (no session_handoff, no init yet) -> binds to old_sid
                rf2.write_text("", encoding="utf-8")
                subagents._SUBAGENT_STUBS_CACHE.clear()
                stubs_resume = {s["id"]: s for s in subagents.discover_subagent_stubs(parent_cid)}
                self.assertEqual(list(stubs_resume.keys()), [old_sid])
                self.assertEqual(stubs_resume[old_sid]["step_status"], 2)
            finally:
                subagents.APP_DIRS = orig_app_dirs
                subagents.SUBAGENT_APP_DIRS = orig_sub_dirs
                subagents._SUBAGENT_STUBS_CACHE.clear()
                subagents._STEP_DEPENDENCIES.clear()
                with subagents._STUBS_PARSE_LOCK:
                    subagents._COMPLETED_STEP_STUBS.clear()

    def test_subagents_sorted_by_last_prompt_time_and_send_message_bumps_order(self):
        parent_cid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        sub_a = "11111111-aaaa-4aaa-8aaa-111111111111"
        sub_b = "22222222-bbbb-4bbb-8bbb-222222222222"

        def _meta_ts(sec: int) -> bytes:
            return _field_bytes(1, _field_varint(1, sec))

        with tempfile.TemporaryDirectory(prefix="cockpit-sub-sort-") as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            (app_dir / "conversations").mkdir(parents=True, exist_ok=True)
            bdir = app_dir / "brain" / parent_cid / ".system_generated"
            (bdir / "steps" / "1").mkdir(parents=True, exist_ok=True)
            (bdir / "steps" / "2").mkdir(parents=True, exist_ok=True)
            (bdir / "steps" / "1" / "output.txt").write_text(
                json.dumps({"conversationId": sub_a}) + "\n", encoding="utf-8"
            )
            (bdir / "steps" / "2" / "output.txt").write_text(
                json.dumps({"conversationId": sub_b}) + "\n", encoding="utf-8"
            )

            # sub_a launched earlier (t=100) but finished much later (t=500)
            _build_db(
                app_dir / "conversations" / f"{sub_a}.db",
                [_encode_gen_metadata(1, 1532, 1000, 2000, 300)],
                [(0, 14, 3, _meta_ts(100), b""), (1, 15, 3, _meta_ts(500), b"")],
            )
            # sub_b launched later (t=200) and finished earlier (t=250)
            _build_db(
                app_dir / "conversations" / f"{sub_b}.db",
                [_encode_gen_metadata(1, 1532, 1000, 2000, 300)],
                [(0, 14, 3, _meta_ts(200), b""), (1, 15, 3, _meta_ts(250), b"")],
            )

            p_a = _field_bytes(1, b"invoke_subagent") + _field_bytes(2, json.dumps({"Role": "SlowA"}).encode("utf-8"))
            p_b = _field_bytes(1, b"invoke_subagent") + _field_bytes(2, json.dumps({"Role": "FastB"}).encode("utf-8"))
            p_msg_a = _field_bytes(1, b"send_message") + _field_bytes(
                2, json.dumps({"Recipient": sub_a, "Message": "follow up"}).encode("utf-8")
            )

            db_path = app_dir / "conversations" / f"{parent_cid}.db"
            _build_db(db_path, [], [(1, 132, 3, _meta_ts(100), p_a), (2, 132, 3, _meta_ts(200), p_b)])

            orig_app_dirs = subagents.APP_DIRS
            orig_sub_dirs = subagents.SUBAGENT_APP_DIRS
            try:
                subagents.APP_DIRS = [("App", app_dir)]
                subagents.SUBAGENT_APP_DIRS = [("App", app_dir)]
                subagents._SUBAGENT_STUBS_CACHE.clear()
                subagents._STEP_DEPENDENCIES.clear()
                with subagents._STUBS_PARSE_LOCK:
                    subagents._COMPLETED_STEP_STUBS.clear()

                _, items1 = subagents.collect_subagents(parent_cid)
                self.assertEqual([x["id"] for x in items1], [sub_b, sub_a])

                # Sending a follow-up prompt via send_message at t=300 bumps sub_a above sub_b
                db_path.unlink()
                _build_db(
                    db_path,
                    [],
                    [
                        (1, 132, 3, _meta_ts(100), p_a),
                        (2, 132, 3, _meta_ts(200), p_b),
                        (3, 132, 3, _meta_ts(300), p_msg_a),
                    ],
                )
                subagents._SUBAGENT_STUBS_CACHE.clear()
                _, items2 = subagents.collect_subagents(parent_cid)
                self.assertEqual([x["id"] for x in items2], [sub_a, sub_b])
                self.assertEqual(items2[0]["last_prompt_ts"], 300.0)
            finally:
                subagents.APP_DIRS = orig_app_dirs
                subagents.SUBAGENT_APP_DIRS = orig_sub_dirs
                subagents._SUBAGENT_STUBS_CACHE.clear()
                subagents._STEP_DEPENDENCIES.clear()
                with subagents._STUBS_PARSE_LOCK:
                    subagents._COMPLETED_STEP_STUBS.clear()

    def test_compacted_status5_run_command_recovers_command_line_from_terminal_payload(self):
        cmd = "cd /tmp/proj && sa-run.sh tmp/backend.jsonl sa-codex --model gpt-6.2-sol --prompt hi"
        out = "▶ codex 会话 33333333-cccc-4ccc-8ccc-333333333333（gpt-6.2-sol）\n■ 完成\n"
        inner = _field_bytes(1, cmd.encode("utf-8")) + _field_bytes(2, b"/tmp/proj") + _field_bytes(4, out.encode("utf-8"))
        term = _field_bytes(10, _field_bytes(3, inner))
        f140 = _field_bytes(2, _field_bytes(3, term))
        f5 = _field_bytes(4, _field_bytes(1, b"call_1") + _field_bytes(2, b"run_command"))
        payload = _field_varint(1, 132) + _field_varint(4, 5) + _field_bytes(5, f5) + _field_bytes(140, f140)

        tname, args, inline_txt = subagents._parse_step132_payload(payload)
        self.assertEqual(tname, "run_command")
        self.assertEqual(args.get("CommandLine"), cmd)
        self.assertEqual(args.get("Cwd"), "/tmp/proj")
        self.assertIn("33333333-cccc-4ccc-8ccc-333333333333", inline_txt)


if __name__ == "__main__":
    unittest.main()
