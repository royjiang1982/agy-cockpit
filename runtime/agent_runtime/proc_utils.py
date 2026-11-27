"""Process group management and CLI binary discovery for agent-runtime."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
from typing import Any, Optional, Tuple

EXTRA_BIN_DIRS = [
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / ".grok" / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
]

_PG_TERM_LOCK = threading.Lock()
DEFAULT_AGY_APP_DATA_DIR = "antigravity-subagent"


def agy_app_data_dir() -> str:
    """Resolve the isolated --app-data-dir name under ~/.gemini for sa-agy."""
    raw = os.environ.get("SA_AGY_APP_DATA_DIR")
    if raw is None:
        return DEFAULT_AGY_APP_DATA_DIR
    val = raw.strip()
    if not val:
        return ""
    if "/" in val or "\\" in val or ".." in val or val.startswith("-"):
        return DEFAULT_AGY_APP_DATA_DIR
    return val


def split_model_effort(
    model: Optional[str],
    effort: Optional[str] = None,
    *,
    default_model: str = "",
    default_effort: str = "",
    valid_efforts: Tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
) -> Tuple[str, str]:
    """Split a recognized model:effort suffix, giving explicit effort precedence."""
    model = str(model or "").strip()
    effort = str(effort or "").strip().lower()
    if ":" in model:
        base, suffix = model.rsplit(":", 1)
        suffix = suffix.lower()
        if suffix in valid_efforts:
            model = base.strip()
            effort = effort or suffix
    return model or default_model.strip(), effort or default_effort.strip().lower()


def find_cli_binary(name: str) -> Optional[str]:
    """Locate a CLI binary across standard user/homebrew paths and PATH."""
    for d in EXTRA_BIN_DIRS:
        cand = Path(d) / name
        if cand.is_file() and os.access(str(cand), os.X_OK):
            return str(cand)
    return shutil.which(name)


def terminate_proc_group(proc: Optional[Any], wait_timeout: float = 0.8) -> None:
    """Safely cancel a ResidentTurn or terminate/reap an entire CLI process group."""
    if proc is None:
        return
    if getattr(proc, "is_resident_turn", False):
        if proc.poll() is None:
            def cancel_turn() -> None:
                try:
                    proc.cancel()
                except Exception:
                    pass

            threading.Thread(target=cancel_turn, daemon=True).start()
            finished = getattr(proc, "_finished", None)
            if finished is not None:
                finished.wait(max(0.0, min(wait_timeout, 0.4)))
        proc.close()
        return

    with _PG_TERM_LOCK:
        if getattr(proc, "_pg_terminated", False):
            return
        try:
            setattr(proc, "_pg_terminated", True)
        except Exception:
            pass

    finished = getattr(proc, "_finished", None)
    if callable(getattr(proc, "cancel", None)) and finished is not None and not finished.is_set():
        def cancel_single() -> None:
            try:
                proc.cancel()
            except Exception:
                pass

        threading.Thread(target=cancel_single, daemon=True).start()
        finished.wait(min(wait_timeout, 0.3))

    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 0 and proc.poll() is None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    if wait_timeout > 0 and proc.poll() is None:
        try:
            proc.wait(timeout=wait_timeout)
        except Exception:
            pass
    if isinstance(pid, int) and pid > 0:
        pg_alive = proc.poll() is None
        if not pg_alive:
            try:
                os.killpg(pid, 0)
                pg_alive = True
            except ProcessLookupError:
                pg_alive = False
            except PermissionError:
                pg_alive = True
            except Exception:
                pass
        if pg_alive:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                pass
    try:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=0.3)
    except Exception:
        pass


_terminate_proc_group = terminate_proc_group
