"""Isolated unit tests for per-host idle eviction in SubagentDaemon."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.daemon import SubagentDaemon
from agent_runtime.host_reaper import (
    HOST_IDLE_TIMEOUT_SECONDS,
    evict_idle_hosts,
    resolve_host_idle_timeout,
)


def _write_fake_acp(tmp: Path) -> Path:
    script = tmp / "fake_reaper_acp.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"protocolVersion": 1, "agentCapabilities": {}},
        }) + "\\n")
        sys.stdout.flush()
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


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
            fake_acp = _write_fake_acp(tmp)
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


if __name__ == "__main__":
    host_reaper_cases()
