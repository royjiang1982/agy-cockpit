"""Session handoff envelopes and live daemon ledger migration."""
import json
import sys
import threading
import time
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, daemon_request, isolated_runtime, running_daemon, write_rpc_server
from agent_runtime import SubagentDaemon, daemon as daemon_mod
from agent_runtime.session_handoff import (
    _unwrap_handoff_prompt, format_handoff_prompt, load_session_ledger, prepare_session_handoff, save_session_ledger,
)


def test_session_auto_handoff():
    """Verify auto-handoff on expired cache (>600s) + context > 200k across multi-turn ledger & SQLite/JSONL fallbacks."""
    with isolated_runtime({"SA_AGY_QUOTA_PREFLIGHT": "0"}) as tmp:
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
                                session_id=sid, cwd=str(tmp), prompt=prompt_text) as frames:
                return list(frames)

        cmd = [sys.executable, str(write_rpc_server(tmp, "fake_host.py"))]
        with (
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


def test_handoff_envelope():
    opening = '<previous_session_history from_session="old">'
    closing = '</previous_session_history>'
    instruction = '【本轮新指令】：'
    for markers in (
        (opening,), (closing,), (instruction,), (opening, closing),
        (closing, opening, instruction), (instruction, opening, closing),
        (opening, instruction, closing),
    ):
        prompt = '\n'.join(('任意正文', *markers, '继续保留'))
        assert _unwrap_handoff_prompt(prompt) == ([], prompt), markers

    turn = {"prompt": "历史问题", "response": "两行\n历史回复", "ts": 12.5}
    prompt = f"请解释 {closing} 这个标签"
    wrapped = format_handoff_prompt("old-sid", [turn], prompt)
    assert _unwrap_handoff_prompt(wrapped, 12.5) == ([turn], prompt)
    reversed_round = wrapped.replace(
        "[主代理输入]:\n历史问题\n[子代理回复]:\n两行\n历史回复",
        "[子代理回复]:\n两行\n历史回复\n[主代理输入]:\n历史问题",
    )
    assert reversed_round != wrapped
    assert _unwrap_handoff_prompt(reversed_round) == ([], prompt)


CASES = (test_handoff_envelope, test_session_auto_handoff)


if __name__ == "__main__":
    for case in CASES:
        case()
