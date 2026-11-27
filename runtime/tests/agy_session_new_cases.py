"""Isolated unit tests for AgyAcpStream session/new serialization and transient retry."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.acp_stream import AgyAcpStream, agy_server_command
from agent_runtime.proc_utils import DEFAULT_AGY_APP_DATA_DIR, agy_app_data_dir
from agent_runtime.resident import ResidentServer
from agent_runtime.session_handoff import _inspect_agy_db


def _write_fake_agy_server(tmp: Path) -> Path:
    script = tmp / "fake_agy_session_new.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, sys, threading, time

state_file = sys.argv[1]
fail_count = int(sys.argv[2])
fail_msg = sys.argv[3]
delay_ms = int(sys.argv[4])

lock = threading.Lock()
in_flight = 0
max_in_flight = 0
calls = 0
write_lock = threading.Lock()


def send(obj):
    with write_lock:
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()


def dump_state():
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"calls": calls, "max_in_flight": max_in_flight}, f)


def handle_open(rid, method, params):
    global in_flight, max_in_flight, calls
    with lock:
        calls += 1
        call_no = calls
        in_flight += 1
        if in_flight > max_in_flight:
            max_in_flight = in_flight
        dump_state()
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    with lock:
        in_flight -= 1
    if call_no <= fail_count:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": fail_msg}})
        return
    sid = params.get("sessionId") or f"sid-{call_no}"
    send({
        "jsonrpc": "2.0",
        "id": rid,
        "result": {
            "sessionId": sid,
            "models": {"availableModels": [{"modelId": "gemini-4-hydrogen-high"}]},
        },
    })


for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"resume": {}}},
            },
        })
    elif method in ("session/new", "session/resume", "session/load"):
        threading.Thread(target=handle_open, args=(rid, method, params), daemon=True).start()
    elif method in ("session/set_model", "session/set_mode"):
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        sid = params.get("sessionId", "")
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": f"ok:{sid}"},
                },
            },
        })
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def agy_session_new_cases():
    orig_delays = AgyAcpStream.SESSION_NEW_RETRY_DELAYS
    AgyAcpStream.SESSION_NEW_RETRY_DELAYS = (0.01, 0.01)
    prev_markers = os.environ.get("ZOMI_SUBAGENT_SESSIONS_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = str(tmp / "markers")
            server = _write_fake_agy_server(tmp)
            env = os.environ.copy()

            # 1. Transient session/new failure retries up to 2 times and keeps ResidentServer healthy
            st1 = tmp / "st1.json"
            cmd1 = [sys.executable, str(server), str(st1), "2", "Failed to start Antigravity conversation", "0"]
            host1 = ResidentServer(cmd1, cwd=str(tmp), env=env, agent="agy")
            try:
                t1 = AgyAcpStream(
                    cmd1, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high"}, prompt="hi",
                    resident=host1, is_subagent=True,
                )
                evs1 = [json.loads(line) for line in t1]
                t1.close()
                assert any(e.get("delta") == "ok:sid-3" for e in evs1), evs1
                assert json.loads(st1.read_text())["calls"] == 3
                assert not host1.closed and not host1.retire_after_turn and host1.process.poll() is None

                # Next turn reuses the same healthy host cleanly
                t2 = AgyAcpStream(
                    cmd1, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high"}, prompt="hi again",
                    resident=host1, is_subagent=True,
                )
                evs2 = [json.loads(line) for line in t2]
                t2.close()
                assert any(e.get("delta") == "ok:sid-4" for e in evs2), evs2
            finally:
                host1.shutdown()

            # 2. Exhausted retries (3 failures) raises the original transient error
            st2 = tmp / "st2.json"
            cmd2 = [sys.executable, str(server), str(st2), "5", "Failed to start Antigravity conversation", "0"]
            host2 = ResidentServer(cmd2, cwd=str(tmp), env=env, agent="agy")
            try:
                t_fail = AgyAcpStream(
                    cmd2, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high"}, prompt="hi",
                    resident=host2, is_subagent=True,
                )
                raised = None
                try:
                    list(t_fail)
                except RuntimeError as exc:
                    raised = str(exc)
                finally:
                    t_fail.close()
                assert raised and "Failed to start Antigravity conversation" in raised, raised
                assert json.loads(st2.read_text())["calls"] == 3
            finally:
                host2.shutdown()

            # 3. Non-transient error (429) and session/resume MUST NOT retry
            st3 = tmp / "st3.json"
            cmd3 = [sys.executable, str(server), str(st3), "5", "429 rate limit exceeded", "0"]
            host3 = ResidentServer(cmd3, cwd=str(tmp), env=env, agent="agy")
            try:
                t_rate = AgyAcpStream(
                    cmd3, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high"}, prompt="hi",
                    resident=host3, is_subagent=True,
                )
                raised = None
                try:
                    list(t_rate)
                except RuntimeError as exc:
                    raised = str(exc)
                finally:
                    t_rate.close()
                assert raised and "429" in raised, raised
                assert json.loads(st3.read_text())["calls"] == 1
            finally:
                host3.shutdown()

            st4 = tmp / "st4.json"
            cmd4 = [sys.executable, str(server), str(st4), "5", "Failed to start Antigravity conversation", "0"]
            host4 = ResidentServer(cmd4, cwd=str(tmp), env=env, agent="agy")
            try:
                t_resume = AgyAcpStream(
                    cmd4, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high", "session_id": "existing-sid"}, prompt="hi",
                    resident=host4, is_subagent=True,
                )
                raised = None
                try:
                    list(t_resume)
                except RuntimeError as exc:
                    raised = str(exc)
                finally:
                    t_resume.close()
                assert raised and "session/resume" in raised, raised
                assert json.loads(st4.read_text())["calls"] == 1
            finally:
                host4.shutdown()

            # 4. Concurrent session/new on the same ResidentServer is strictly serialized
            st5 = tmp / "st5.json"
            cmd5 = [sys.executable, str(server), str(st5), "0", "unused", "80"]
            host5 = ResidentServer(cmd5, cwd=str(tmp), env=env, agent="agy")
            try:
                results = []
                errors = []

                def run_one(idx):
                    try:
                        turn = AgyAcpStream(
                            cmd5, cwd=str(tmp), env=env,
                            bot={"model": "gemini-4-hydrogen-high"}, prompt=f"p{idx}",
                            resident=host5, is_subagent=True,
                        )
                        evs = [json.loads(line) for line in turn]
                        turn.close()
                        results.append(evs)
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=run_one, args=(i,)) for i in range(3)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=15)
                assert not errors, errors
                assert len(results) == 3, len(results)
                state = json.loads(st5.read_text())
                assert state["calls"] == 3 and state["max_in_flight"] == 1, state
            finally:
                host5.shutdown()
    finally:
        AgyAcpStream.SESSION_NEW_RETRY_DELAYS = orig_delays
        if prev_markers is None:
            os.environ.pop("ZOMI_SUBAGENT_SESSIONS_DIR", None)
        else:
            os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = prev_markers

    # 5. Isolated --app-data-dir command builder and SQLite handoff lookup priority
    prev_app_dir = os.environ.get("SA_AGY_APP_DATA_DIR")
    prev_home = os.environ.get("HOME")
    try:
        os.environ.pop("SA_AGY_APP_DATA_DIR", None)
        assert agy_app_data_dir() == DEFAULT_AGY_APP_DATA_DIR
        assert agy_server_command(["agy"]) == [
            "agy", "acp", "--app-data-dir", DEFAULT_AGY_APP_DATA_DIR,
        ]
        os.environ["SA_AGY_APP_DATA_DIR"] = "custom-subagent"
        assert agy_server_command(["agy"]) == [
            "agy", "acp", "--app-data-dir", "custom-subagent",
        ]
        os.environ["SA_AGY_APP_DATA_DIR"] = ""
        assert agy_server_command(["agy"]) == ["agy", "acp"]
        os.environ["SA_AGY_APP_DATA_DIR"] = "../bad/path"
        assert agy_app_data_dir() == DEFAULT_AGY_APP_DATA_DIR
        os.environ["SA_AGY_APP_DATA_DIR"] = "--injected"
        assert agy_app_data_dir() == DEFAULT_AGY_APP_DATA_DIR

        os.environ.pop("SA_AGY_APP_DATA_DIR", None)
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            os.environ["HOME"] = str(fake_home)
            iso_dir = fake_home / ".gemini" / DEFAULT_AGY_APP_DATA_DIR / "conversations"
            cli_dir = fake_home / ".gemini" / "antigravity-cli" / "conversations"
            iso_dir.mkdir(parents=True)
            cli_dir.mkdir(parents=True)

            def _make_min_db(db_path: Path, user_text: str):
                con = sqlite3.connect(str(db_path))
                con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, metadata BLOB, step_payload TEXT)")
                con.executemany(
                    "INSERT INTO steps VALUES (?, ?, NULL, ?)",
                    [(1, 14, user_text), (2, 15, f"reply:{user_text}")],
                )
                con.commit()
                con.close()

            _make_min_db(iso_dir / "sid-iso.db", "hello-iso")
            _make_min_db(cli_dir / "sid-iso.db", "hello-cli-shadowed")
            _make_min_db(cli_dir / "sid-legacy.db", "hello-legacy")

            iso_info = _inspect_agy_db("sid-iso")
            assert iso_info and iso_info["turns"] and iso_info["turns"][0]["prompt"] == "hello-iso", iso_info
            legacy_info = _inspect_agy_db("sid-legacy")
            assert legacy_info and legacy_info["turns"] and legacy_info["turns"][0]["prompt"] == "hello-legacy", legacy_info
    finally:
        if prev_app_dir is None:
            os.environ.pop("SA_AGY_APP_DATA_DIR", None)
        else:
            os.environ["SA_AGY_APP_DATA_DIR"] = prev_app_dir
        if prev_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prev_home
    print("PASS: agy_session_new_cases")


if __name__ == "__main__":
    agy_session_new_cases()
