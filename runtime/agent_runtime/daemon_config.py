"""Paths and isolated environments shared by the daemon and its clients."""
import os
from pathlib import Path
from typing import Dict

from .proc_utils import EXTRA_BIN_DIRS


def get_daemon_dir() -> Path:
    custom = os.environ.get("SA_SUBAGENTD_DIR")
    if custom:
        return Path(custom)
    return Path(f"/tmp/dev-subagentd-{os.getuid()}")


def get_socket_path() -> Path:
    return get_daemon_dir() / "daemon.sock"


def get_pid_path() -> Path:
    return get_daemon_dir() / "daemon.pid"


def build_clean_subagent_env() -> Dict[str, str]:
    env = os.environ.copy()
    for k in list(env.keys()):
        if k.startswith("ZOMI_"):
            env.pop(k, None)
    cur_path = env.get("PATH", "")
    parts = [p for p in EXTRA_BIN_DIRS if p not in cur_path.split(":")]
    env["PATH"] = ":".join([p for p in parts + cur_path.split(":") if p])
    env["SA_SUBAGENT"] = "1"
    env["GROK_SUBAGENTS"] = "0"
    return env
