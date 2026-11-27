"""Daemon socket lifecycle, pool isolation, and process cleanup."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, daemon_request, running_daemon, signal_socket, write_rpc_server
from agent_runtime import AgyAcpStream, SubagentDaemon, daemon as daemon_mod, terminate_proc_group
from agent_runtime.host_reaper import HOST_IDLE_TIMEOUT_SECONDS, evict_idle_hosts, resolve_host_idle_timeout


def test_daemon_eof_cancels_acp():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sock = tmp / "daemon.sock"
        cancel_signal = tmp / "cancel.sock"
        script = write_rpc_server(tmp, "fake_acp.py", setup="startup = True")
        with (
            patch.dict(os.environ, SA_AGY_QUOTA_PREFLIGHT="0", ZOMI_SUBAGENT_SESSIONS_DIR=str(tmp / "markers")),
            patch.object(daemon_mod, "build_server_cmd_and_stream_cls",
                         return_value=([sys.executable, str(script), str(cancel_signal)], AgyAcpStream)),
            signal_socket(cancel_signal) as listener,
            running_daemon(SubagentDaemon(sock)),
        ):
            with daemon_request(sock, action="run", agent="agy", model="gemini-4-carbon-medium",
                                cwd=td, prompt="WAIT_FOR_CANCEL") as frames:
                assert next(frames)["event"]["event"] == "init"
                assert next(frames)["event"]["type"] == "thinking"
            # Closing the client must put session/cancel on the actual ACP wire.
            with listener.accept()[0] as notice, notice.makefile("rb") as wire:
                notice.settimeout(TIMEOUT)
                assert wire.read() == b"sid-1"

    print("PASS: SubagentDaemon EOF cancellation")


def test_active_session_collision():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
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
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sock = tmp / "daemon.sock"
        cmd = [sys.executable, str(write_rpc_server(tmp, "fake_host.py"))]
        with (
            patch.dict(os.environ, ZOMI_SUBAGENT_SESSIONS_DIR=str(tmp / "markers")),
            patch.object(daemon_mod, "build_server_cmd_and_stream_cls", return_value=(cmd, FakeMultiMsgCodexStream)),
            running_daemon(SubagentDaemon(sock)),
            daemon_request(sock, action="run", agent="codex", cwd=td, prompt="hi") as frames,
        ):
            result = [f for f in frames if f.get("type") == "result"][-1]["result"]
            assert result["response"] == "First segment. Second segment.", result

    print("PASS: active session collisions and complete Codex result text")


def host_reaper_cases():
    prev_env = os.environ.get("SA_HOST_IDLE_TIMEOUT_SEC")
    try:
        # 1. Timeout resolution (explicit, env, disable=0, invalid fallbacks)
        os.environ.pop("SA_HOST_IDLE_TIMEOUT_SEC", None)
        assert resolve_host_idle_timeout() == HOST_IDLE_TIMEOUT_SECONDS
        assert resolve_host_idle_timeout(120.0) == 120.0
        assert resolve_host_idle_timeout(0.0) == 0.0
        assert resolve_host_idle_timeout(-5.0) == HOST_IDLE_TIMEOUT_SECONDS
        os.environ["SA_HOST_IDLE_TIMEOUT_SEC"] = "45"
        assert resolve_host_idle_timeout() == 45.0
        os.environ["SA_HOST_IDLE_TIMEOUT_SEC"] = "0"
        assert resolve_host_idle_timeout() == 0.0
        os.environ["SA_HOST_IDLE_TIMEOUT_SEC"] = "not-a-number"
        assert resolve_host_idle_timeout() == HOST_IDLE_TIMEOUT_SECONDS
        os.environ["SA_HOST_IDLE_TIMEOUT_SEC"] = "nan"
        assert resolve_host_idle_timeout() == HOST_IDLE_TIMEOUT_SECONDS
        os.environ.pop("SA_HOST_IDLE_TIMEOUT_SEC", None)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            dir_a = tmp / "repo_a"
            dir_b = tmp / "repo_b"
            dir_a.mkdir()
            dir_b.mkdir()
            fake_acp = write_rpc_server(tmp, "fake_acp.py", setup="capabilities = {}")
            cmd = [sys.executable, str(fake_acp)]
            env = os.environ.copy()

            # 2. Idle host in repo_a evicted while busy host in repo_b stays alive
            daemon = SubagentDaemon(socket_path=tmp / "d1.sock", idle_timeout=1800.0, host_idle_timeout=10.0)
            try:
                host_a, inbox_a = daemon._acquire_resident("agy", str(dir_a), cmd, "", env)
                host_b, inbox_b = daemon._acquire_resident("agy", str(dir_b), cmd, "", env)
                host_a.release(inbox_a, healthy=True)
                # Keep host_b busy (inbox_b still active), age both past timeout
                past = time.monotonic() - 25.0
                host_a.last_used_monotonic = past
                host_b.last_used_monotonic = past

                evicted = evict_idle_hosts(daemon)
                assert evicted == 1, evicted
                assert host_a.closed and host_a.process.poll() is not None
                assert not host_b.closed and host_b.process.poll() is None
                assert host_a not in daemon.all_hosts and host_b in daemon.all_hosts
                assert len(daemon.pools) == 1

                # Re-acquiring repo_a spawns a clean fresh host
                host_a2, inbox_a2 = daemon._acquire_resident("agy", str(dir_a), cmd, "", env)
                assert host_a2 is not host_a and not host_a2.closed and host_a2.process.poll() is None
                host_a2.release(inbox_a2, healthy=True)
                host_b.release(inbox_b, healthy=True)
            finally:
                daemon.shutdown_all()

            # 3. host_idle_timeout=0 disables per-host eviction
            daemon_off = SubagentDaemon(socket_path=tmp / "d2.sock", idle_timeout=1800.0, host_idle_timeout=0.0)
            try:
                host_c, inbox_c = daemon_off._acquire_resident("agy", str(dir_a), cmd, "", env)
                host_c.release(inbox_c, healthy=True)
                host_c.last_used_monotonic = time.monotonic() - 9999.0
                assert evict_idle_hosts(daemon_off) == 0
                assert not host_c.closed and host_c.process.poll() is None
            finally:
                daemon_off.shutdown_all()
    finally:
        if prev_env is None:
            os.environ.pop("SA_HOST_IDLE_TIMEOUT_SEC", None)
        else:
            os.environ["SA_HOST_IDLE_TIMEOUT_SEC"] = prev_env
    print("PASS: host_reaper_cases")


def _assert_pid_reaped(pid):
    # EOF means the process exited; the OS may not have reaped the orphan yet.
    deadline = time.monotonic() + TIMEOUT
    pause = threading.Event()
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"Grandchild PID {pid} still exists after socket EOF"
        pause.wait(min(0.01, remaining))


def test_terminate_process_group():
    child = r"""
import os, signal, socket, sys, threading
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with socket.socket(socket.AF_UNIX) as peer:
    peer.connect(sys.argv[1])
    peer.sendall(f"{os.getpid()}\n".encode())
    threading.Event().wait()
"""
    leader = """
import subprocess, sys, threading
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
threading.Event().wait()
"""
    with tempfile.TemporaryDirectory() as td, signal_socket(Path(td) / "child.sock") as listener:
        proc = subprocess.Popen([sys.executable, "-c", leader, child, listener.getsockname()],
                                start_new_session=True)
        try:
            with listener.accept()[0] as peer, peer.makefile("rb") as reader:
                peer.settimeout(TIMEOUT)
                child_pid = int(reader.readline())  # The grandchild already ignores SIGTERM.
                assert child_pid > 0
                terminate_proc_group(proc)
                assert proc.poll() == -signal.SIGTERM
                assert reader.read() == b"", "Grandchild survived process group termination"
                _assert_pid_reaped(child_pid)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=TIMEOUT)
    print("PASS: process group reaps SIGTERM-ignoring grandchild after leader exits")


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
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, SA_SUBAGENTD_DIR=td, HOME=td):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale, _track_replacement_daemon() as spawned:
            stale.bind(str(daemon_mod.get_socket_path()))  # Deliberately never listen.
            assert daemon_mod.query_daemon("status").get("stale_socket") is True
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

    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, SA_SUBAGENTD_DIR=td, HOME=td):
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


CASES = (
    host_reaper_cases, test_daemon_eof_cancels_acp, test_active_session_collision,
    test_codex_message_segments, test_terminate_process_group, test_restart_recovers_stale_socket,
    test_restart_during_old_host_shutdown,
)


if __name__ == "__main__":
    with patch.dict(os.environ, SA_AGY_QUOTA_PREFLIGHT="0"):
        for case in CASES:
            case()
