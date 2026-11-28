"""Daemon socket lifecycle, pool isolation, and process cleanup."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from unittest.mock import Mock, patch

from _runtime_fixture import (
    TIMEOUT, daemon_request, isolated_env, isolated_runtime, running_daemon, signal_socket, write_rpc_server,
)
from agent_runtime import AgyAcpStream, SubagentDaemon, daemon as daemon_mod, terminate_proc_group
from runtime_process_cases import host_reaper_cases, test_terminate_process_group


def test_daemon_eof_cancels_acp():
    with isolated_runtime() as tmp:
        sock = tmp / "daemon.sock"
        cancel_signal = tmp / "cancel.sock"
        script = write_rpc_server(tmp, "fake_acp.py", setup="startup = True")
        with (
            patch.dict(os.environ, SA_AGY_QUOTA_PREFLIGHT="0"),
            patch.object(daemon_mod, "build_server_cmd_and_stream_cls",
                         return_value=([sys.executable, str(script), str(cancel_signal)], AgyAcpStream)),
            signal_socket(cancel_signal) as listener,
            running_daemon(SubagentDaemon(sock)),
        ):
            with daemon_request(sock, action="run", agent="agy", model="gemini-4-carbon-medium",
                                cwd=str(tmp), prompt="WAIT_FOR_CANCEL") as frames:
                assert next(frames)["event"]["event"] == "init"
                assert next(frames)["event"]["type"] == "thinking"
            # Closing the client must put session/cancel on the actual ACP wire.
            with listener.accept()[0] as notice, notice.makefile("rb") as wire:
                notice.settimeout(TIMEOUT)
                assert wire.read() == b"sid-1"

    print("PASS: SubagentDaemon EOF cancellation")


def test_active_session_collision():
    with isolated_runtime() as tmp:
        # 1. Cross-pool & same-pool active session_id collision rejection in SubagentDaemon
        fake_acp = write_rpc_server(tmp, "fake_acp.py")
        daemon = SubagentDaemon(socket_path=tmp / "col.sock", idle_timeout=60)
        env = os.environ.copy()
        cmd = [sys.executable, str(fake_acp)]
        dir_a = tmp / "dir_a"
        dir_b = tmp / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        try:
            h1, inbox1 = daemon._acquire_resident("agy", str(dir_a), cmd, "shared-sid-1", env)
            # Same session_id in same cwd or different cwd must be rejected instead of spawning 2nd server
            for target_dir in (dir_a, dir_b):
                try:
                    daemon._acquire_resident("agy", str(target_dir), cmd, "shared-sid-1", env)
                    raise AssertionError("Expected duplicate active session_id to be rejected!")
                except RuntimeError as exc:
                    assert "占用" in str(exc), exc
            h1.release(inbox1)
        finally:
            daemon.shutdown_all()


class FakeMultiMsgCodexStream:
    def __init__(self, *args, **kwargs):
        self.thread_id = "th-multi"
        self.is_resident_turn = True
        self._inbox = kwargs.get("inbox")
        self.resident = kwargs.get("resident")

    def __iter__(self):
        yield json.dumps({"type": "thread.started", "thread_id": "th-multi"}) + "\n"
        yield json.dumps({
            "type": "item.completed",
            "delta": True,
            "item": {"id": "msg-1", "type": "agent_message", "text": "First segment. "},
        }) + "\n"
        yield json.dumps({
            "type": "item.completed",
            "item": {"id": "msg-1", "type": "agent_message", "text": "First segment. "},
        }) + "\n"
        yield json.dumps({
            "type": "item.completed",
            "delta": True,
            "item": {"id": "msg-2", "type": "agent_message", "text": "Second segment."},
        }) + "\n"
        yield json.dumps({
            "type": "item.completed",
            "item": {"id": "msg-2", "type": "agent_message", "text": "Second segment."},
        }) + "\n"
        yield json.dumps({"type": "turn.completed"}) + "\n"

    def poll(self):
        return 0

    def close(self):
        if self.resident and self._inbox:
            self.resident.release(self._inbox)


def test_codex_message_segments():
    with isolated_runtime() as tmp:
        sock = tmp / "daemon.sock"
        cmd = [sys.executable, str(write_rpc_server(tmp, "fake_host.py"))]
        with (
            patch.object(daemon_mod, "build_server_cmd_and_stream_cls", return_value=(cmd, FakeMultiMsgCodexStream)),
            running_daemon(SubagentDaemon(sock)),
            daemon_request(sock, action="run", agent="codex", cwd=str(tmp), prompt="hi") as frames,
        ):
            result = [f for f in frames if f.get("type") == "result"][-1]["result"]
            assert result["response"] == "First segment. Second segment.", result

    print("PASS: active session collisions and complete Codex result text")


@contextmanager
def _track_replacement_daemon():
    spawned = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        proc = popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    try:
        with patch.object(daemon_mod.subprocess, "Popen", side_effect=launch):
            yield spawned
    finally:
        if spawned:
            daemon_mod.query_daemon("shutdown")
        for proc in spawned:
            try:
                proc.wait(timeout=TIMEOUT)
            finally:
                if proc.poll() is None:
                    terminate_proc_group(proc)


def test_restart_recovers_stale_socket():
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, isolated_env(td, {"SA_SUBAGENTD_DIR": td}), clear=True):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale, _track_replacement_daemon() as spawned:
            assert daemon_mod.query_daemon("status") == {"ok": False, "status": "stopped"}
            stale.bind(str(daemon_mod.get_socket_path()))  # Deliberately never listen.
            status = daemon_mod.query_daemon("status")
            assert (status["ok"], status["status"], status["stale_socket"]) == (False, "error", True)
            assert status["error"]
            assert daemon_mod.main(["restart"]) == 0
            replacement = daemon_mod.query_daemon("status")
            pid = replacement.get("daemon_pid")
            assert replacement["ok"] is True and type(pid) is int and pid > 0, replacement
            assert len(spawned) == 1 and spawned[0].pid == pid
    print("PASS: restart recovers a real stale Unix socket and returns a live daemon PID")


def test_restart_during_old_host_shutdown():
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class SlowHost:
        agent, busy, closed = "agy", False, False
        active_turns, session_inboxes = (), {}

        class process:
            pid = 99999

            @staticmethod
            def poll():
                return None

        def shutdown(self):
            entered.set()
            if release.wait(TIMEOUT):
                self.closed = True
            closed.set()

    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, isolated_env(td, {"SA_SUBAGENTD_DIR": td}), clear=True):
        old = SubagentDaemon(daemon_mod.get_socket_path())
        slow_host = SlowHost()
        old.all_hosts.add(slow_host)
        with _track_replacement_daemon() as spawned, running_daemon(old):
            try:
                assert daemon_mod.main(["restart"]) == 0
                assert len(spawned) == 1
                assert entered.wait(TIMEOUT) and not closed.is_set(), "Restart waited for old host teardown"
                replacement = daemon_mod.query_daemon("status")
                assert replacement["ok"] is True and replacement["daemon_pid"] != os.getpid(), replacement
                release.set()
                assert closed.wait(TIMEOUT) and slow_host.closed
                old.shutdown_all()  # Old process finalizers must not unlink the replacement socket.
                assert daemon_mod.query_daemon("status").get("daemon_pid") == replacement["daemon_pid"]
            finally:
                release.set()
                if entered.is_set():
                    assert closed.wait(TIMEOUT)
    print("PASS: restart unlinks before slow shutdown and preserves the replacement socket")


def test_query_rejects_malformed_frames():
    with isolated_runtime():
        path = daemon_mod.get_socket_path()
        path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            with patch.object(daemon_mod.socket, "socket") as client:
                conn = client.return_value.__enter__.return_value
                for response in ([], {"ok": 1}, {"ok": "true"},
                                 {"ok": False, "status": "stopped", "stale_socket": True},
                                 {"ok": True, "status": "stopped"}):
                    conn.recv.return_value = (json.dumps(response) + "\n").encode()
                    assert daemon_mod.query_daemon("status") == {
                        "ok": False, "status": "error", "error": "unexpected daemon response",
                    }, response


def test_status_and_stop_exit_codes():
    for command, action in (("status", "status"), ("stop", "shutdown")):
        for status, code in (("error", 1), ("stopped", 0)):
            response = {"ok": False, "status": status}
            stdout = io.StringIO()
            with patch.object(daemon_mod, "query_daemon", return_value=response) as query, redirect_stdout(stdout):
                assert daemon_mod.main([command]) == code
                query.assert_called_once_with(action)
            assert json.loads(stdout.getvalue()) == response


def test_restart_checks_replacement_daemon():
    running = {"ok": True, "daemon_pid": 101}
    stopped = {"ok": False, "status": "stopped"}
    replacement = {"ok": True, "daemon_pid": 102}
    bad = tuple({"ok": True, "daemon_pid": pid} for pid in (None, True, 0, -1, "123", 1.5))
    cases = (
        (running, running, 1), (running, {"ok": 1, "daemon_pid": 102}, 1),
        *((running, invalid, 1) for invalid in bad),
        (running, replacement, 0), (running, {"ok": False, "status": "error"}, 1),
        (stopped, replacement, 0), *((invalid, None, 1) for invalid in bad),
        ({"ok": False, "status": "error", "error": "boom"}, None, 1),
    )
    for before, after, code in cases:
        current, shutdowns, launches = before, 0, 0

        def query(action):
            nonlocal current, shutdowns
            if action == "status":
                return current.copy()
            assert action == "shutdown" and current == running
            shutdowns += 1
            if after != running:
                current = stopped
            return {"ok": True}

        def launch():
            nonlocal current, launches
            assert after is not None and shutdowns == (before == running)
            launches += 1
            current = after

        stdout = io.StringIO()
        with (
            isolated_runtime(), patch.object(daemon_mod, "query_daemon", side_effect=query),
            patch.object(daemon_mod, "ensure_daemon_running", side_effect=launch),
            patch.object(daemon_mod, "_ping_daemon", side_effect=lambda _: current == running),
            patch.object(daemon_mod, "time", Mock(monotonic=Mock(side_effect=(0.0, 0.1, 3.0)))),
            redirect_stdout(stdout),
        ):
            assert daemon_mod.main(["restart"]) == code, (before, after)
        assert (launches, shutdowns) == (after is not None, before == running)
        got = json.loads(stdout.getvalue())
        if code == 0:
            assert got == after, got
        else:
            assert got["ok"] is False and got["error"], got
            if before.get("error"):
                assert got["error"] == before["error"]


def test_failure_to_write_ledger_keeps_successful_result():
    class LedgerStream(FakeMultiMsgCodexStream):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.thread_id = "fake-sid"

        def __iter__(self):
            yield {"event": "init", "conversation_id": "fake-sid"}
            yield {"type": "text_delta", "delta": "第一行\n"}
            yield {"type": "text_delta", "delta": "second line", "used_tokens": 12}

    with isolated_runtime() as root:
        sock, stderr = root / "daemon.sock", io.StringIO()
        cmd = [sys.executable, str(write_rpc_server(root, "fake_host.py"))]
        with (
            patch.object(daemon_mod, "build_server_cmd_and_stream_cls", return_value=(cmd, LedgerStream)),
            patch.object(daemon_mod, "record_turn_completion", side_effect=OSError("ledger disk full")) as record,
            redirect_stderr(stderr), running_daemon(SubagentDaemon(sock)),
            daemon_request(sock, action="run", agent="codex", cwd=str(root), prompt="test") as frames,
        ):
            result = [frame for frame in frames if frame["type"] == "result"]
        response = "第一行\nsecond line"
        record.assert_called_once_with("codex", "fake-sid", "test", response, 12, None, root)
        assert result == [{
            "type": "result", "agent": "codex", "conversation_id": "fake-sid",
            "result": {"status": "SUCCESS", "response": response, "ledger_warning": "ledger disk full"},
        }], result
        assert "ledger disk full" in stderr.getvalue()


CASES = (
    host_reaper_cases, test_daemon_eof_cancels_acp, test_active_session_collision,
    test_codex_message_segments, test_terminate_process_group, test_restart_recovers_stale_socket,
    test_restart_during_old_host_shutdown, test_query_rejects_malformed_frames,
    test_status_and_stop_exit_codes, test_restart_checks_replacement_daemon,
    test_failure_to_write_ledger_keeps_successful_result,
)


if __name__ == "__main__":
    for case in CASES:
        case()
