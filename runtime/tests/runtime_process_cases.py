"""Host expiry policy and process group shutdown contracts."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

from _runtime_fixture import TIMEOUT, isolated_env, isolated_runtime, signal_socket, write_rpc_server
from agent_runtime import SubagentDaemon, terminate_proc_group
from agent_runtime.host_reaper import HOST_IDLE_TIMEOUT_SECONDS, evict_idle_hosts, resolve_host_idle_timeout


def host_reaper_cases():
    with isolated_runtime():
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
                                start_new_session=True, env=isolated_env(td))
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


