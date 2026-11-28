"""Isolated unit tests for AgyAcpStream session/new serialization and transient retry."""
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
import threading

from _runtime_fixture import TIMEOUT, isolated_runtime, signal_socket, write_rpc_server
from agent_runtime.acp_stream import AgyAcpStream, agy_server_command
from agent_runtime.proc_utils import DEFAULT_AGY_APP_DATA_DIR, agy_app_data_dir
from agent_runtime.resident import ResidentServer
from agent_runtime.session_handoff import _inspect_agy_db


SESSION_OPEN_SCRIPT = '''
state_file, fail_count, fail_message = sys.argv[1:4]
open_gate = sys.argv[4] if len(sys.argv) > 4 else None
capabilities["sessionCapabilities"] = {"resume": {}}
session_info = {"models": {"availableModels": [{"modelId": "gemini-4-hydrogen-high"}]}}
reply_prefix = "ok:"
counts = {"calls": 0, "max_in_flight": 0}
in_flight = 0
lock = threading.Lock()

def open_session(rid, params):
    global in_flight
    with lock:
        counts["calls"] += 1
        call_no = counts["calls"]
        in_flight += 1
        counts["max_in_flight"] = max(in_flight, counts["max_in_flight"])
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(counts, f)
    if call_no == 1 and open_gate:
        child_signal(open_gate, hold=True)
    with lock:
        in_flight -= 1
    if call_no <= int(fail_count):
        error(rid, -32000, fail_message)
    else:
        reply(rid, {"sessionId": params.get("sessionId") or f"sid-{call_no}", **session_info})

def handle_open(rid, params):
    threading.Thread(target=open_session, args=(rid, params), daemon=True).start()
'''


def agy_session_new_cases():
    with isolated_runtime() as tmp, patch.object(AgyAcpStream, "SESSION_NEW_RETRY_DELAYS", (0, 0)):
        server = write_rpc_server(tmp, "fake_agy_session.py", setup=SESSION_OPEN_SCRIPT)
        env = os.environ.copy()

        # 1. Transient session/new failure retries up to 2 times and keeps ResidentServer healthy
        st1 = tmp / "st1.json"
        cmd1 = [sys.executable, str(server), str(st1), "2", "Failed to start Antigravity conversation"]
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
        cmd2 = [sys.executable, str(server), str(st2), "5", "Failed to start Antigravity conversation"]
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
        cmd3 = [sys.executable, str(server), str(st3), "5", "429 rate limit exceeded"]
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
        cmd4 = [sys.executable, str(server), str(st4), "5", "Failed to start Antigravity conversation"]
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
        st5, gate = tmp / "st5.json", tmp / "open.sock"
        cmd5 = [sys.executable, str(server), str(st5), "0", "unused", str(gate)]
        with signal_socket(gate) as listener:
            host5 = ResidentServer(cmd5, cwd=str(tmp), env=env, agent="agy")
            try:
                results, errors = [], []
                attempted = threading.Barrier(4)
                lock = host5.session_create_lock

                class ObservedLock:
                    def acquire(self, **kwargs):
                        attempted.wait(TIMEOUT)
                        return lock.acquire(**kwargs)

                    def release(self):
                        lock.release()

                host5.session_create_lock = ObservedLock()

                def run_one(idx):
                    try:
                        turn = AgyAcpStream(
                            cmd5, cwd=str(tmp), env=env,
                            bot={"model": "gemini-4-hydrogen-high"}, prompt=f"p{idx}",
                            resident=host5, is_subagent=True, rpc_timeout=TIMEOUT,
                        )
                        try:
                            results.append([json.loads(line) for line in turn])
                        finally:
                            turn.close()
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=run_one, args=(i,), daemon=True) for i in range(3)]
                for th in threads:
                    th.start()
                try:
                    # All three callers reach session/new before the first wire response can finish.
                    attempted.wait(TIMEOUT)
                    with listener.accept()[0]:
                        assert lock.locked() and json.loads(st5.read_text())["calls"] == 1
                finally:
                    for th in threads:
                        th.join(TIMEOUT)
                assert not any(th.is_alive() for th in threads) and not errors, errors
                assert len(results) == 3, len(results)
                state = json.loads(st5.read_text())
                assert state["calls"] == 3 and state["max_in_flight"] == 1, state
            finally:
                host5.shutdown()

    # 5. Isolated --app-data-dir command builder and SQLite handoff lookup priority
    with isolated_runtime() as fake_home:
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
    print("PASS: agy_session_new_cases")


if __name__ == "__main__":
    agy_session_new_cases()
