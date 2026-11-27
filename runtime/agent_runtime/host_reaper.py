"""Per-host idle eviction for SubagentDaemon resident server pools."""
import math
import os
import time
from typing import Any, List, Optional

HOST_IDLE_TIMEOUT_SECONDS = 600.0


def resolve_host_idle_timeout(explicit: Optional[float] = None) -> float:
    """Resolve per-host idle timeout in seconds (0 disables per-host eviction)."""
    if explicit is not None:
        val = float(explicit)
        return val if math.isfinite(val) and val >= 0.0 else HOST_IDLE_TIMEOUT_SECONDS
    raw = os.environ.get("SA_HOST_IDLE_TIMEOUT_SEC")
    if raw is None or not raw.strip():
        return HOST_IDLE_TIMEOUT_SECONDS
    try:
        val = float(raw.strip())
    except ValueError:
        return HOST_IDLE_TIMEOUT_SECONDS
    return val if math.isfinite(val) and val >= 0.0 else HOST_IDLE_TIMEOUT_SECONDS


def evict_idle_hosts(daemon: Any, *, now: Optional[float] = None) -> int:
    """Atomically retire and shut down idle resident hosts past host_idle_timeout."""
    timeout = getattr(daemon, "host_idle_timeout", HOST_IDLE_TIMEOUT_SECONDS)
    if timeout <= 0.0:
        return 0
    ts = time.monotonic() if now is None else now
    to_shutdown: List[Any] = []
    with daemon.lock:
        if daemon.stopping.is_set():
            return 0
        for key in list(daemon.pools.keys()):
            kept = []
            for host in daemon.pools.get(key, []):
                with host.lock:
                    dead = host.closed or host.process.poll() is not None
                    if dead:
                        daemon.all_hosts.discard(host)
                        continue
                    idle_for = ts - getattr(host, "last_used_monotonic", ts)
                    if not host.active_turns and not host.retire_after_turn and idle_for >= timeout:
                        host.retire_after_turn = True
                        daemon.all_hosts.discard(host)
                        to_shutdown.append(host)
                        continue
                kept.append(host)
            if kept:
                daemon.pools[key] = kept
            else:
                daemon.pools.pop(key, None)
    for host in to_shutdown:
        try:
            host.shutdown()
        except Exception:
            pass
    return len(to_shutdown)
