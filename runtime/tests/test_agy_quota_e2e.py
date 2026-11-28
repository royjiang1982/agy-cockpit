#!/usr/bin/env python3
"""Real daemon and client routing around a fake quota source and fake ACP peers."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, daemon_request, isolated_env, running_daemon, signal_socket, write_rpc_server
from agent_runtime import AgyAcpStream, DroidAcpStream, agy_quota as q, daemon as d
from agent_runtime import daemon_client, session_handoff as sh, turn_runner
from test_agy_quota import OPUS, UsageFixture, reset


QUOTA_ACP_SCRIPT = r'''
engine, log, gate = sys.argv[1:]
sid_prefix = f"{engine}-sid"
session_info = {"configOptions": [{"id": "reasoning_effort"}]}

def record(value):
    with open(log, "a", encoding="utf-8") as out:
        out.write(json.dumps(value) + "\n")

def boot():
    record(dict(kind="spawn", engine=engine))

def handle_prompt(rid, sid, text):
    record(dict(kind="prompt", engine=engine, text=text, sid=sid))
    if "HOLD_FOR_RELEASE" in text:
        child_signal(gate, hold=True)
    if "RUNTIME_LIMIT" in text:
        error(rid, 429, "RESOURCE_EXHAUSTED quota")
    else:
        update(sid, "reply:" + engine)
        reply(rid, {"stopReason": "end_turn"})
'''


def e2e(good, root):
    sock, home, log = root / "daemon" / "daemon.sock", root / "home", root / "mock.jsonl"
    gate = root / "prompt.sock"
    script = write_rpc_server(root, "mock.py", setup=QUOTA_ACP_SCRIPT)
    env = isolated_env(home, {"SA_SUBAGENTD_DIR": str(sock.parent), "SA_SUBAGENT": "1"})

    def build(ag, cfg=None):
        return [sys.executable, str(script), ag, str(log), str(gate)], DroidAcpStream if ag == "droid" else AgyAcpStream

    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(q, "_fetch_snapshot", side_effect=good.fetch),
        patch.object(turn_runner, "build_server_cmd_and_stream_cls", side_effect=build),
        signal_socket(gate) as listener, running_daemon(d.SubagentDaemon(socket_path=sock)) as daemon,
    ):
        def client(sid="", prompt="task"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.object(daemon_client, "ensure_daemon_running", return_value=sock), patch.object(daemon_client.signal, "signal"):
                rc = d.run_subagent_client(agent="agy", model=OPUS, mode="plan", session_id=sid, cwd=str(root), prompt=prompt)
            return rc, [json.loads(line) for line in out.getvalue().splitlines()]
        def records(): return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        def direct(prompt="task", sid=""):
            with daemon_request(sock, action="run", agent="agy", model=OPUS, session_id=sid,
                                cwd=str(root), prompt=prompt) as frames:
                return list(frames)
        good.set(0)
        start = len(good.calls)
        rc, frames = client()
        assert rc == 0 and frames[-1]["agent"] == "droid" and "thread_id" not in frames[-1]
        assert any(f.get("event") == "quota_fallback" for f in frames)
        assert len(good.calls) - start == 1
        assert all(r["engine"] == "droid" for r in records())
        assert sh._strip_subagent_prefix(records()[-1]["text"]) == "task"
        db = home / ".gemini/antigravity-cli/conversations/legacy.db"
        db.parent.mkdir(parents=True)
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, metadata BLOB, step_payload TEXT)")
            con.executemany("INSERT INTO steps VALUES (?, ?, NULL, ?)", [(1, 14, "old-A"), (2, 15, "answer-A"), (3, 14, "old-B"), (4, 15, "answer-B")])
        wrapped = sh.format_handoff_prompt("older", [dict(prompt="old-A", response="answer-A")], "old-B")
        sh.save_session_ledger(dict(agent="agy", session_id="legacy", turns=[dict(prompt=wrapped, response="answer-B"), dict(prompt="old-C", response="answer-C")]))
        good.set(.03)
        rc, frames = client("legacy", "continue-task")
        assert rc == 0 and frames[-1]["agent"] == "droid"
        sid = frames[-1]["conversation_id"]
        text = records()[-1]["text"]
        assert text.count("<previous_session_history") == 1 and all(t in text for t in ("old-A", "old-B", "old-C", "continue-task"))
        assert text.index("old-A") < text.index("old-B") < text.index("old-C")
        ledger = sh.load_session_ledger(sid)
        assert ledger["agent"] == "droid" and ledger["handoff_from"] == "legacy" and len(ledger["turns"]) == 4
        assert ledger["turns"][-1]["prompt"] == "continue-task"
        good.set(0); before = len(records())
        rc, frames = client("missing-sid")
        assert rc == 1 and "context could not be loaded" in frames[-1]["result"]["error"]
        assert len(records()) == before
        frames = direct(sid="legacy")
        assert frames[-1]["agent"] == "droid" and not daemon.active_sessions
        before = len(records())
        assert direct(sid="missing-direct")[-1]["result"]["status"] == "ERROR" and len(records()) == before
        daemon.active_sessions.add("busy")
        with patch.object(turn_runner, "preflight_agy_request", side_effect=AssertionError("must not inspect an active session")):
            assert direct(sid="busy")[-1]["result"]["status"] == "ERROR"
        daemon.active_sessions.discard("busy")
        good.set(0); good.gate, good.entered = threading.Event(), threading.Event()
        raced, before = [], len(records())
        first = threading.Thread(target=lambda: raced.append(client("legacy", "HOLD_FOR_RELEASE")), daemon=True)
        first.start()
        try:
            assert good.entered.wait(TIMEOUT) and daemon.is_session_busy("legacy")
            assert "拒绝并发重入" in direct(sid="legacy")[-1]["result"]["error"] and len(records()) == before
            good.gate.set()
            with listener.accept()[0]:
                assert any("HOLD_FOR_RELEASE" in r.get("text", "") for r in records()[before:])
                assert daemon.is_session_busy("legacy") and "拒绝并发重入" in direct(sid="legacy")[-1]["result"]["error"]
        finally:
            good.gate.set()
            first.join(TIMEOUT)
            good.gate = None
        assert not first.is_alive() and raced[0][0] == 0 and not daemon.active_sessions
        failure = root / "failed.py"
        failure.write_text("raise SystemExit(7)" + chr(10))
        good.set(0)
        with patch.object(turn_runner, "build_server_cmd_and_stream_cls", return_value=([sys.executable, str(failure)], DroidAcpStream)):
            rc, frames = client("legacy")
        assert rc == 1 and frames[-1]["agent"] == "droid" and frames[-1]["conversation_id"] == ""
        before = len(records())
        with patch.object(daemon, "_acquire_resident", side_effect=RuntimeError("mock acquire failure")):
            rc, frames = client("legacy")
        assert rc == 1 and frames[-1]["agent"] == "droid" and frames[-1]["conversation_id"] == ""
        assert frames[-1]["result"]["error"] == "mock acquire failure" and not daemon.is_session_busy("legacy") and len(records()) == before
        good.set(.5)
        assert direct("RUNTIME_LIMIT")[-1]["result"]["status"] == "ERROR" and q._CACHE is None
        print("PASS: daemon + client fallback, merged SQLite/ledger migration, missing-context fast fail and runtime invalidation")


def main():
    with tempfile.TemporaryDirectory() as td, patch.object(q, "find_cli_binary", return_value="/mock/droid"):
        try:
            e2e(UsageFixture(), Path(td))
        finally:
            reset()
    print("ALL AGY QUOTA DAEMON VERIFICATIONS PASSED")


if __name__ == "__main__":
    main()
