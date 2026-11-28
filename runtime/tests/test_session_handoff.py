"""Session handoff, local history recovery, and ledger/cache invariants."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, daemon_request, running_daemon, write_rpc_server
from agent_runtime import SubagentDaemon, daemon as daemon_mod
from agent_runtime.session_handoff import inspect_local_session, load_session_ledger, prepare_session_handoff, save_session_ledger


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val >= 0x80:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _encode_proto_bytes(fnum: int, data: bytes) -> bytes:
    return _encode_varint((fnum << 3) | 2) + _encode_varint(len(data)) + data


def _encode_proto_varint(fnum: int, val: int) -> bytes:
    return _encode_varint((fnum << 3) | 0) + _encode_varint(val)


def test_session_auto_handoff():
    """Verify auto-handoff on expired cache (>600s) + context > 200k across multi-turn ledger & SQLite/JSONL fallbacks."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ledger_dir = tmp / "ledger"
        fake_home = tmp / "home"
        fake_home.mkdir(parents=True, exist_ok=True)
        sock_path = tmp / "handoff.sock"

        captured_calls = []
        next_tokens = [80_000]
        next_reply = ["R1"]
        sid_seq = [0]
        entered, release = threading.Event(), threading.Event()
        block_stream = [False]
        fail_in_init = [False]
        fail_mid_stream = [False]

        class FakeHandoffStream:
            def __init__(self, cmd, *, cwd, env, bot, prompt, **kwargs):
                if fail_in_init[0]:
                    raise RuntimeError("simulated init failure before session assigned")
                req_sid = str(bot.get("session_id") or "")
                if req_sid:
                    self.session_id = req_sid
                else:
                    sid_seq[0] += 1
                    self.session_id = f"sid-{sid_seq[0]}"
                captured_calls.append({"session_id": req_sid, "assigned_sid": self.session_id, "prompt": prompt})
                self.is_resident_turn = True
                self.resident = kwargs.get("resident")
                self._inbox = kwargs.get("inbox")
                self._tok = next_tokens[0]
                self._reply = next_reply[0]
                self._block = block_stream[0]
                self._fail_mid = fail_mid_stream[0]

            def __iter__(self):
                yield json.dumps({"event": "init", "agent": "agy", "conversation_id": self.session_id}) + "\n"
                if self._block:
                    entered.set()
                    if not release.wait(TIMEOUT):
                        raise RuntimeError("test stream was not released")
                if self._fail_mid:
                    yield json.dumps({"type": "native_question", "questions": []}) + "\n"
                    return
                yield json.dumps({"type": "text_delta", "delta": self._reply}) + "\n"
                yield json.dumps({"type": "turn.completed", "usage": {"total_tokens": self._tok}}) + "\n"

            def poll(self):
                return 0

            def close(self):
                if self.resident and self._inbox:
                    self.resident.release(self._inbox)

        def run_req(sid, prompt_text, tok, reply_text):
            next_tokens[0], next_reply[0] = tok, reply_text
            with daemon_request(sock_path, action="run", agent="agy", model="gemini-4-carbon-medium",
                                session_id=sid, cwd=td, prompt=prompt_text) as frames:
                return list(frames)

        cmd = [sys.executable, str(write_rpc_server(tmp, "fake_host.py"))]
        env = {k: v for k, v in os.environ.items()
               if k not in ("SA_HANDOFF_TTL_SEC", "SA_HANDOFF_TOKEN_THRESHOLD", "SA_DISABLE_AUTO_HANDOFF")}
        env.update(HOME=str(fake_home), SA_SESSIONS_LEDGER_DIR=str(ledger_dir),
                   ZOMI_SUBAGENT_SESSIONS_DIR=str(tmp / "markers"))
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "build_server_cmd_and_stream_cls", return_value=(cmd, FakeHandoffStream)),
            running_daemon(SubagentDaemon(sock_path)),
        ):
            # Turn 1: initial new session -> sid-1 (80k tokens)
            f1 = run_req("", "Prompt Turn 1", 80_000, "Response Turn 1")
            assert f1[-1]["conversation_id"] == "sid-1"
            assert captured_calls[-1]["session_id"] == ""

            # Turn 2: resume sid-1 while cache warm & tokens < 200k -> no handoff, updates tokens to 210k
            f2 = run_req("sid-1", "Prompt Turn 2", 210_000, "Response Turn 2")
            assert f2[-1]["conversation_id"] == "sid-1"
            assert captured_calls[-1]["session_id"] == "sid-1"
            assert captured_calls[-1]["prompt"] == "Prompt Turn 2"
            assert not any(f.get("type") == "event" and f["event"].get("event") == "session_handoff" for f in f2)

            # Turn 3: resume sid-1 while tokens > 200k (210k) but cache still warm (idle < 600s) -> no handoff
            long_r3 = "Response Turn 3 " + ("LONG_PAYLOAD_" * 1500)
            f3 = run_req("sid-1", "Prompt Turn 3", 215_000, long_r3)
            assert f3[-1]["conversation_id"] == "sid-1"
            assert captured_calls[-1]["session_id"] == "sid-1"
            assert captured_calls[-1]["prompt"] == "Prompt Turn 3"

            # Verify warm/not-yet-10m (<=600s) with tokens > 200k does NOT handoff,
            # and expired cache (>600s) at exact boundary tokens == 200k does NOT handoff
            rec1 = load_session_ledger("sid-1")
            assert rec1 is not None and len(rec1["turns"]) == 3
            rec1["updated_at"] = time.time() - 500.0
            rec1["used_tokens"] = 215_000
            save_session_ledger(rec1)
            assert prepare_session_handoff("agy", "sid-1", "Check warm 500s") is None

            rec1["updated_at"] = time.time() - 700.0
            rec1["used_tokens"] = 200_000
            save_session_ledger(rec1)
            assert prepare_session_handoff("agy", "sid-1", "Check exact 200k boundary") is None

            # Age sid-1 (>600s) with used_tokens=215,000 (>200k) -> Turn 4 MUST trigger auto-handoff to sid-2
            rec1["updated_at"] = time.time() - 700.0
            rec1["used_tokens"] = 215_000
            save_session_ledger(rec1)

            f4 = run_req("sid-1", "Prompt Turn 4", 55_000, "Response Turn 4")
            assert f4[-1]["conversation_id"] == "sid-2", f4
            handoff_evts = [f["event"] for f in f4 if f.get("type") == "event" and f["event"].get("event") == "session_handoff"]
            assert len(handoff_evts) == 1, f4
            assert handoff_evts[0]["from_session_id"] == "sid-1"
            assert handoff_evts[0]["turns_carried"] == 3
            assert handoff_evts[0]["used_tokens"] == 215_000

            call4 = captured_calls[-1]
            assert call4["session_id"] == "", "Handoff must clear session_id passed to underlying stream!"
            p4 = call4["prompt"]
            assert '<previous_session_history from_session="sid-1" turns="3">' in p4
            assert "Prompt Turn 1" in p4 and "Response Turn 1" in p4
            assert "Prompt Turn 2" in p4 and "Response Turn 2" in p4
            assert "Prompt Turn 3" in p4 and long_r3 in p4, "Long response must not be truncated!"
            assert p4.endswith("【本轮新指令】：\nPrompt Turn 4")

            # Verify sid-2 ledger inherited all 3 prior turns + raw Turn 4 (no nested tags in ledger)
            rec2 = load_session_ledger("sid-2")
            assert rec2 is not None
            assert rec2["handoff_from"] == "sid-1"
            assert len(rec2["turns"]) == 4
            assert rec2["turns"][3]["prompt"] == "Prompt Turn 4"
            assert "<previous_session_history" not in rec2["turns"][3]["prompt"]

            # Age sid-2 and set tokens=220,000 -> Turn 5 triggers 2nd handoff (sid-2 -> sid-3) without nesting tags
            rec2["updated_at"] = time.time() - 750.0
            rec2["used_tokens"] = 220_000
            save_session_ledger(rec2)

            f5 = run_req("sid-2", "Prompt Turn 5", 60_000, "Response Turn 5")
            assert f5[-1]["conversation_id"] == "sid-3"
            call5 = captured_calls[-1]
            assert call5["session_id"] == ""
            p5 = call5["prompt"]
            assert '<previous_session_history from_session="sid-2" turns="4">' in p5
            assert p5.count("<previous_session_history") == 1, "Must never nest previous_session_history blocks!"
            for idx in range(1, 5):
                assert f"=== 第 {idx} 轮 ===" in p5
                assert f"Prompt Turn {idx}" in p5
            assert long_r3 in p5

            # Part 1b: Verify all 7 adversarial review fixes on daemon + ledger lifecycle
            # Fix 4 (Medium 5): Token compaction / lower token update must NOT be pinned to old max high-water mark
            rec3 = load_session_ledger("sid-3")
            assert rec3 is not None and rec3["used_tokens"] == 60_000
            rec3["used_tokens"] = 220_000
            save_session_ledger(rec3)
            # Subsequent warm turn reports compacted 28,000 tokens -> ledger must drop to 28,000 (not stay at 220,000)
            f5_compact = run_req("sid-3", "Prompt Turn 5b after compact", 28_000, "Response Turn 5b")
            assert f5_compact[-1]["result"]["status"] == "SUCCESS"
            rec3_after = load_session_ledger("sid-3")
            assert rec3_after is not None and rec3_after["used_tokens"] == 28_000, rec3_after
            rec3_after["updated_at"] = time.time() - 700.0
            save_session_ledger(rec3_after)
            assert prepare_session_handoff("agy", "sid-3", "Should not handoff after compact") is None

            # Fix 6 (Medium 7): Handoff triggered, but stream initialization fails BEFORE new sid is assigned -> returns requested_sid ("sid-3")
            rec3_after["updated_at"] = time.time() - 700.0
            rec3_after["used_tokens"] = 230_000
            save_session_ledger(rec3_after)
            fail_in_init[0] = True
            try:
                f_init_err = run_req("sid-3", "Prompt Init Fail", 50_000, "Never")
            finally:
                fail_in_init[0] = False
            assert f_init_err[-1]["result"]["status"] == "ERROR"
            assert f_init_err[-1]["conversation_id"] == "sid-3", f"Expected fallback to requested_sid 'sid-3', got {f_init_err[-1]}"

            # Fix 1 (High 1 + Medium 3): Concurrent requests with the same expired requested_sid ("sid-3") must be mutually exclusive
            calls_before_conc = len(captured_calls)
            block_stream[0] = True
            conc_results = [None, None]

            def first_turn():
                conc_results[0] = run_req("sid-3", "Concurrent Prompt 0", 50_000, "Concurrent Reply 0")

            first = threading.Thread(target=first_turn, daemon=True)
            first.start()
            try:
                assert entered.wait(TIMEOUT), "First handoff never entered its active turn"
                conc_results[1] = run_req("sid-3", "Concurrent Prompt 1", 50_000, "Concurrent Reply 1")
            finally:
                release.set()
                first.join(TIMEOUT)
            assert not first.is_alive()
            block_stream[0] = False
            assert len(captured_calls) == calls_before_conc + 1, "Only ONE new session may be spawned for concurrent same-sid requests!"
            statuses = sorted(r[-1]["result"]["status"] for r in conc_results if r)
            assert statuses == ["ERROR", "SUCCESS"], conc_results
            err_frame = next(r[-1] for r in conc_results if r[-1]["result"]["status"] == "ERROR")
            assert "正在运行中，拒绝并发重入" in err_frame["result"]["error"]
            assert err_frame["conversation_id"] == "sid-3"
            ok_frame = next(r[-1] for r in conc_results if r[-1]["result"]["status"] == "SUCCESS")
            assert ok_frame["conversation_id"] == "sid-4"

            # Fix 2a (High 2): Mid-turn failure during handoff still seeds carried_turns to new session ledger (sid-5)
            rec4 = load_session_ledger("sid-4")
            assert rec4 is not None
            rec4["updated_at"] = time.time() - 700.0
            rec4["used_tokens"] = 240_000
            save_session_ledger(rec4)
            fail_mid_stream[0] = True
            try:
                f_mid_err = run_req("sid-4", "Prompt Turn Aborted Mid-Handoff", 50_000, "Partial")
            finally:
                fail_mid_stream[0] = False
            assert f_mid_err[-1]["result"]["status"] == "ERROR"
            assert f_mid_err[-1]["conversation_id"] == "sid-5"
            seeded_rec5 = load_session_ledger("sid-5")
            assert seeded_rec5 is not None, "Seeded ledger for sid-5 must exist even when turn aborted mid-stream!"
            assert seeded_rec5["handoff_from"] == "sid-4"
            assert len(seeded_rec5["turns"]) == len(rec4["turns"])
            # Resuming sid-5 on next turn must preserve all inherited turns + append the new turn
            f_recover5 = run_req("sid-5", "Prompt Turn Recovered on sid-5", 62_000, "Response Recovered")
            assert f_recover5[-1]["result"]["status"] == "SUCCESS"
            rec5_final = load_session_ledger("sid-5")
            assert rec5_final is not None and len(rec5_final["turns"]) == len(rec4["turns"]) + 1
            assert rec5_final["turns"][-1]["prompt"] == "Prompt Turn Recovered on sid-5"

    print("PASS: transparent cross-session handoff, ledger repair and same-session exclusion")


def test_legacy_session_handoff():
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, HOME=td, SA_SESSIONS_LEDGER_DIR=td):
        fake_home = Path(td)
        # Verify fallback parsing of pre-existing local session files (SQLite & JSONL) without ledger
        old_ts = time.time() - 8000.0

        # 2a. agy SQLite DB fallback (WAL mode without -shm/-wal, multi-step assistant reply concatenation, and step_type=23 compaction)
        agy_db_dir = fake_home / ".gemini" / "antigravity-cli" / "conversations"
        agy_db_dir.mkdir(parents=True, exist_ok=True)
        agy_db = agy_db_dir / "legacy-agy-sid.db"
        con = sqlite3.connect(str(agy_db))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB)")
        con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        ts_blob = _encode_proto_bytes(8, _encode_proto_varint(1, int(old_ts)))
        u1_blob = _encode_proto_bytes(
            19,
            _encode_proto_bytes(
                2,
                "你是被派出的子代理（SA_SUBAGENT=1）。只遵守任务书，不派生子代理、不提交代码。\n\nLegacy Agy Q1".encode("utf-8"),
            ),
        )
        a1_part1_blob = _encode_proto_bytes(20, _encode_proto_bytes(1, b"Legacy Agy Finding A"))
        a1_tool_blob = _encode_proto_bytes(20, _encode_proto_bytes(7, b"tool_call_intermediate"))
        a1_final_blob = _encode_proto_bytes(20, _encode_proto_bytes(1, b"Legacy Agy A1"))
        u2_blob = _encode_proto_bytes(19, _encode_proto_bytes(3, _encode_proto_bytes(1, b"Legacy Agy Q2")))
        a2_final_blob = _encode_proto_bytes(20, _encode_proto_bytes(8, b"Legacy Agy A2"))
        con.executemany(
            "INSERT INTO steps VALUES (?, ?, 3, ?, ?)",
            [
                (0, 14, ts_blob, u1_blob),
                (1, 15, ts_blob, a1_part1_blob),
                (2, 15, ts_blob, a1_tool_blob),
                (3, 15, ts_blob, a1_final_blob),
                (4, 14, ts_blob, u2_blob),
                (5, 15, ts_blob, a2_final_blob),
            ],
        )
        usage_blob = _encode_proto_bytes(
            1,
            _encode_proto_bytes(4, _encode_proto_varint(2, 45_000) + _encode_proto_varint(5, 165_000)),
        )
        con.execute("INSERT INTO gen_metadata VALUES (1, ?)", (usage_blob,))
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        for suffix in ("-shm", "-wal"):
            p_side = Path(f"{agy_db}{suffix}")
            if p_side.exists():
                p_side.unlink()
        assert not Path(f"{agy_db}-shm").exists() and not Path(f"{agy_db}-wal").exists()

        inspected_agy = inspect_local_session("agy", "legacy-agy-sid")
        assert inspected_agy is not None, "inspect_local_session failed on WAL-mode DB without -shm/-wal"
        assert inspected_agy["used_tokens"] == 210_000
        assert len(inspected_agy["turns"]) == 2

        ho_agy = prepare_session_handoff("agy", "legacy-agy-sid", "Agy Q3")
        assert ho_agy is not None
        assert ho_agy["used_tokens"] == 210_000
        assert ho_agy["event"]["turns_carried"] == 2
        assert ho_agy["carried_turns"][0]["prompt"] == "Legacy Agy Q1"
        assert ho_agy["carried_turns"][0]["response"] == "Legacy Agy Finding A\n\nLegacy Agy A1"
        assert ho_agy["carried_turns"][1]["prompt"] == "Legacy Agy Q2"
        assert ho_agy["carried_turns"][1]["response"] == "Legacy Agy A2"

        # Verify agy step_type=23 (compaction/checkpoint) ignores pre-compaction gen_metadata rows
        con = sqlite3.connect(str(agy_db))
        con.execute("INSERT INTO steps VALUES (6, 23, 3, ?, ?)", (ts_blob, b""))
        con.commit()
        con.close()
        inspected_agy_compact = inspect_local_session("agy", "legacy-agy-sid")
        assert inspected_agy_compact is not None and inspected_agy_compact["used_tokens"] <= 200_000
        assert prepare_session_handoff("agy", "legacy-agy-sid", "Agy Q3 after compact") is None

        # 2b. codex JSONL fallback (2-hour TTL = 7200s) + unwrapping handoff-wrapped prompt from JSONL without ledger
        codex_dir = fake_home / ".codex" / "sessions" / "2026" / "11" / "10"
        codex_dir.mkdir(parents=True, exist_ok=True)
        codex_jl = codex_dir / "rollout-legacy-codex-sid.jsonl"
        wrapped_codex_u2 = ho_agy["effective_prompt"].replace("Agy Q3", "Codex Unwrapped Q3")
        codex_jl.write_text(
            "\n".join(
                [
                    json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for repo"}]}}),
                    json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": wrapped_codex_u2}]}}),
                    json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Codex Unwrapped A3"}]}}),
                    json.dumps({"type": "token_usage_record", "payload": {"usage": {"input_tokens": 205_000, "output_tokens": 2_000}}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(codex_jl, (old_ts, old_ts))
        ho_codex = prepare_session_handoff("codex", "legacy-codex-sid", "Codex Q4")
        assert ho_codex is not None and ho_codex["used_tokens"] == 207_000
        assert len(ho_codex["carried_turns"]) == 3, ho_codex["carried_turns"]
        assert ho_codex["carried_turns"][0]["prompt"] == "Legacy Agy Q1"
        assert ho_codex["carried_turns"][1]["prompt"] == "Legacy Agy Q2"
        assert ho_codex["carried_turns"][2] == {"prompt": "Codex Unwrapped Q3", "response": "Codex Unwrapped A3", "ts": old_ts}
        assert ho_codex["effective_prompt"].count("<previous_session_history") == 1

        # Verify JSONL compaction event resets used_tokens below threshold
        with open(codex_jl, "a", encoding="utf-8") as wf:
            wf.write(json.dumps({"type": "compaction", "usage": {"input_tokens": 165_000, "output_tokens": 1_200}}) + "\n")
        os.utime(codex_jl, (old_ts, old_ts))
        inspected_codex_compact = inspect_local_session("codex", "legacy-codex-sid")
        assert inspected_codex_compact is not None and inspected_codex_compact["used_tokens"] == 15_200
        assert prepare_session_handoff("codex", "legacy-codex-sid", "Codex Q4 after compact") is None

        # 2c. droid settings + JSONL fallback (lastCallTokenUsage priority & multi-part assistant reply)
        droid_dir = fake_home / ".factory" / "sessions" / "ws"
        droid_dir.mkdir(parents=True, exist_ok=True)
        droid_s = droid_dir / "legacy-droid-sid.settings.json"
        droid_j = droid_dir / "legacy-droid-sid.jsonl"
        droid_s.write_text(
            json.dumps(
                {
                    "tokenUsage": {"inputTokens": 300_000, "cacheReadTokens": 100_000, "outputTokens": 10_000},
                    "lastCallTokenUsage": {"inputTokens": 105_000, "cacheReadTokens": 100_000, "outputTokens": 1_000},
                }
            ),
            encoding="utf-8",
        )
        droid_j.write_text(
            "\n".join(
                [
                    json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "<system-reminder>sys</system-reminder>"}, {"type": "text", "text": "Droid Q1"}]}}),
                    json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Droid Finding 1"}]}}),
                    json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool out"}]}}),
                    json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "done"}, {"type": "text", "text": "Droid A1"}]}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(droid_s, (old_ts, old_ts))
        os.utime(droid_j, (old_ts, old_ts))
        ho_droid = prepare_session_handoff("droid", "legacy-droid-sid", "Droid Q2")
        assert ho_droid is not None and ho_droid["used_tokens"] == 206_000
        assert ho_droid["carried_turns"][0]["prompt"] == "Droid Q1"
        assert ho_droid["carried_turns"][0]["response"] == "Droid Finding 1\n\nDroid A1"

        # Verify Droid lastCallTokenUsage <= 200k prevents false handoff even when cumulative tokenUsage is 410k
        droid_s.write_text(
            json.dumps(
                {
                    "tokenUsage": {"inputTokens": 300_000, "cacheReadTokens": 100_000, "outputTokens": 10_000},
                    "lastCallTokenUsage": {"inputTokens": 20_000, "cacheReadTokens": 25_000, "outputTokens": 500},
                }
            ),
            encoding="utf-8",
        )
        os.utime(droid_s, (old_ts, old_ts))
        assert inspect_local_session("droid", "legacy-droid-sid")["used_tokens"] == 45_500
        assert prepare_session_handoff("droid", "legacy-droid-sid", "Droid Q2 low lastCall") is None

    print("PASS: local history recovery (SQLite and JSONL), compaction and ordered handoff")


def session_cases():
    from agent_runtime import session_handoff as sh, session_files as sf
    with tempfile.TemporaryDirectory() as td, patch.object(Path, 'home', return_value=Path(td)):
        path = Path(td) / '.codex/sessions/2026/rollout-cache-sid.jsonl'
        path.parent.mkdir(parents=True)
        def turn(prompt, reply):
            return ''.join(json.dumps(dict(type='message', message=dict(role=role, content=text))) + '\n'
                           for role, text in [('user', prompt), ('assistant', reply)])
        path.write_text(turn('first', 'answer'))
        original_read = Path.read_text
        def read(p, *args, **kwargs):
            if p == path:
                assert not sf.ledger_lock("cache-sid", Path(td))._is_owned()
            return original_read(p, *args, **kwargs)
        with patch.object(Path, 'rglob', autospec=True, wraps=Path.rglob) as scan:
            # autospec+wraps is not a real generator on every Python version.
            scan.side_effect = lambda root, pattern: original_glob(root, pattern)
            with patch.object(Path, 'read_text', autospec=True, side_effect=read) as reads:
                first = sh.inspect_local_session('codex', 'cache-sid')
                assert first['turns'][0]['prompt'] == 'first'
                assert sh.inspect_local_session('codex', 'cache-sid') == first
                assert scan.call_count == reads.call_count == 1
                first['turns'].clear()
                assert len(sh.inspect_local_session('codex', 'cache-sid')['turns']) == 1
                path.write_text(turn('first', 'answer') + turn('second', 'complete'))
                updated = sh.inspect_local_session('codex', 'cache-sid')
                assert len(updated['turns']) == 2 and scan.call_count == 1 and reads.call_count == 2
                sh.record_turn_completion('codex', 'cache-sid', 'second', 'complete', daemon_dir=Path(td))
                ledger = sh.load_session_ledger('cache-sid', Path(td))
                assert [(t['prompt'], t['response']) for t in ledger['turns']] == [('first', 'answer'), ('second', 'complete')]
        assert sf.ledger_lock('a', td) is not sf.ledger_lock('b', td)
        ephemeral = Path(td) / '.codex/sessions/2026/rollout-ephemeral.jsonl'
        ephemeral.write_text(turn('vanish', 'ok'))
        wrapped = sf.cached_jsonl_inspection(lambda a, s, ps: (ephemeral.unlink(), {'ok': True})[1])
        assert wrapped('codex', 'ephemeral') == {'ok': True}
    print('PASS: cached session path/content, changed-file refresh, isolated ledger locks and complete history')


original_glob = Path.rglob
