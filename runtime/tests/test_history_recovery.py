"""Local SQLite/JSONL history recovery, cache invalidation and merge bounds."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, isolated_runtime
from agent_runtime import session_files as sf, session_handoff as sh
from agent_runtime import build_forced_handoff_prompt
from agent_runtime.session_handoff import inspect_local_session, prepare_session_handoff


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


def test_legacy_session_handoff():
    with isolated_runtime() as fake_home:
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


def test_cached_history():
    with isolated_runtime() as root:
        path = root / '.codex/sessions/2026/rollout-cache-sid.jsonl'
        path.parent.mkdir(parents=True)
        def turn(prompt, reply):
            return ''.join(json.dumps(dict(type='message', message=dict(role=role, content=text))) + '\n'
                           for role, text in [('user', prompt), ('assistant', reply)])
        path.write_text(turn('first', 'answer'))
        original_glob, original_read = Path.rglob, Path.read_text
        def read(p, *args, **kwargs):
            if p == path:
                assert not sf.ledger_lock("cache-sid", root)._is_owned()
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
                sh.record_turn_completion('codex', 'cache-sid', 'second', 'complete', daemon_dir=root)
                ledger = sh.load_session_ledger('cache-sid', root)
                assert [(t['prompt'], t['response']) for t in ledger['turns']] == [('first', 'answer'), ('second', 'complete')]
        assert sf.ledger_lock('a', root) is not sf.ledger_lock('b', root)
        ephemeral = root / '.codex/sessions/2026/rollout-ephemeral.jsonl'
        ephemeral.write_text(turn('vanish', 'ok'))
        wrapped = sf.cached_jsonl_inspection(lambda a, s, ps: (ephemeral.unlink(), {'ok': True})[1])
        assert wrapped('codex', 'ephemeral') == {'ok': True}
    print('PASS: cached session path/content, changed-file refresh, isolated ledger locks and complete history')


def test_linear_history_merge():
    def measure(size):
        work = 0

        class CountedText(str):
            def __str__(self):
                return self

            def strip(self):
                nonlocal work
                work += 1
                return self  # Test strings have no outer whitespace.

            def __hash__(self):
                nonlocal work
                work += 1
                return super().__hash__()

            def __eq__(self, other):
                nonlocal work
                work += 1
                return super().__eq__(other)

        def rows(start, end):
            return [dict(prompt=CountedText(f"question-{i}"), response=CountedText(f"answer-{i}"), ts=i)
                    for i in range(start, end)]

        def can_enter():
            if lock.acquire(blocking=False):
                lock.release()
                return True
            return False

        def load(*_):
            observations.append(("load", peer.submit(can_enter).result(timeout=TIMEOUT)))
            return dict(turns=rows(size // 3, size))

        def inspect(*_):
            observations.append(("inspect", peer.submit(can_enter).result(timeout=TIMEOUT)))
            return dict(turns=rows(0, 2 * size // 3))

        def format_prompt(*args):
            observations.append(("format", peer.submit(can_enter).result(timeout=TIMEOUT)))
            return original_format(*args)

        observations = []
        with (
            isolated_runtime() as state_dir,
            ThreadPoolExecutor(max_workers=1) as peer,
            patch.object(sh, "load_session_ledger", side_effect=load),
            patch.object(sh, "inspect_local_session", side_effect=inspect),
            patch.object(sh, "format_handoff_prompt", side_effect=format_prompt),
        ):
            lock = sf.ledger_lock("mock", state_dir)
            prompt = build_forced_handoff_prompt("agy", "mock", "", "next", state_dir)
        assert observations == [("load", False), ("inspect", True), ("format", True)], observations
        carried, clean = sh._unwrap_handoff_prompt(prompt)
        assert [(t["prompt"], t["response"]) for t in carried] == [
            (f"question-{i}", f"answer-{i}") for i in range(size)
        ] and clean == "next"
        assert prompt.count("<previous_session_history") == 1
        return work

    original_format = sh.format_handoff_prompt

    small, large = measure(150), measure(300)
    assert 0 < small and large <= 2.2 * small and large <= 24 * 300, (small, large)
    print("PASS: linear 300-turn merge, complete ordered history, unlocked SQLite/formatting")


CASES = (test_cached_history, test_legacy_session_handoff, test_linear_history_merge)


if __name__ == "__main__":
    for case in CASES:
        case()
