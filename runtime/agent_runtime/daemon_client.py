"""Unix-socket client, daemon startup and CLI management commands."""
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .cli_events import CliOutput
from .daemon_config import build_clean_subagent_env, get_socket_path


def _verify_socket_security(sock_path: Path, *, require_socket: bool = False) -> None:
    uid = os.getuid()
    parent = sock_path.parent
    if parent.exists() or parent.is_symlink():
        pst = parent.lstat()
        if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode) or pst.st_uid != uid:
            raise RuntimeError(f"不安全的守护进程目录：{parent}")
    if sock_path.exists() or sock_path.is_symlink():
        sst = sock_path.lstat()
        if stat.S_ISLNK(sst.st_mode) or not stat.S_ISSOCK(sst.st_mode) or sst.st_uid != uid:
            raise RuntimeError(f"不安全的守护进程 Socket：{sock_path}")
    elif require_socket:
        raise RuntimeError(f"守护进程 Socket 不存在：{sock_path}")


def ensure_daemon_running(timeout: float = 5.0) -> Path:
    sock_path = get_socket_path()
    _verify_socket_security(sock_path)
    if _ping_daemon(sock_path):
        return sock_path

    daemon_dir = sock_path.parent
    daemon_dir.mkdir(parents=True, exist_ok=True)
    _verify_socket_security(sock_path)
    os.chmod(daemon_dir, 0o700)
    lock_file = daemon_dir / "spawn.lock"

    # The Unix daemon uses flock; stream-only callers can still import on Windows.
    import fcntl

    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        if _ping_daemon(sock_path):
            return sock_path
        log_path = daemon_dir / "daemon.log"
        pkg_root = str(Path(__file__).resolve().parents[1])
        env = build_clean_subagent_env()
        env["PYTHONPATH"] = pkg_root + (
            ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        with open(log_path, "ab") as log_f:
            subprocess.Popen(
                [sys.executable, "-m", "agent_runtime.daemon", "serve"],
                cwd=str(Path.home()),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _ping_daemon(sock_path):
                return sock_path
            time.sleep(0.05)
    raise RuntimeError("无法启动常驻子代理守护进程 sa-subagentd")


def _ping_daemon(sock_path: Path) -> bool:
    if not sock_path.exists():
        return False
    try:
        _verify_socket_security(sock_path, require_socket=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(str(sock_path))
            s.sendall(b'{"action":"status"}\n')
            data = s.recv(65536)
            return b'"ok": true' in data or b'"ok":true' in data
    except Exception:
        return False


def query_daemon(action: str) -> Dict[str, Any]:
    sock_path = get_socket_path()
    if not sock_path.exists():
        return {"ok": False, "status": "stopped"}
    try:
        _verify_socket_security(sock_path, require_socket=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(sock_path))
            s.sendall((json.dumps({"action": action}) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            data = json.loads(buf.decode("utf-8", errors="replace").strip())
            if not isinstance(data, dict) or data.get("ok") is not True or data.get("status") == "stopped":
                err = (data.get("error") or (data.get("result") or {}).get("error")) if isinstance(data, dict) else None
                return {"ok": False, "status": "error", "error": str(err or "unexpected daemon response")}
            return data
    except ConnectionRefusedError as exc:
        return {"ok": False, "status": "error", "error": str(exc), "stale_socket": True}
    except Exception as exc:
        return {"ok": False, "status": "error", "error": str(exc)}


def run_subagent_client(
    *,
    agent: str,
    model: str,
    effort: str = "",
    service_tier: str = "",
    mode: str = "",
    session_id: str = "",
    cwd: Optional[str] = None,
    prompt: str,
    config: Optional[List[str]] = None,
    image_paths: Optional[List[str]] = None,
    board: Optional[str] = None,
    name: Optional[str] = None,
    log_file: Optional[str] = None,
) -> int:
    """Connect to standalone SubagentDaemon, stream JSONL events to stdout, and return exit code."""
    sock_path = ensure_daemon_running()
    _verify_socket_security(sock_path, require_socket=True)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(str(sock_path))

    def handle_stop(signum, frame):
        try:
            conn.close()
        except Exception:
            pass
        raise SystemExit(128 + signum)

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, handle_stop)
        signal.signal(signal.SIGINT, handle_stop)

    req = {
        "action": "run",  # Legacy daemons dispatch on action; newer peers also accept op.
        "op": "run",
        "emit_deltas": os.environ.get("SA_CODEX_EMIT_DELTAS") == "1",
        "agent": agent,
        "model": model,
        "effort": effort,
        "service_tier": service_tier,
        "mode": mode,
        "session_id": session_id,
        "cwd": cwd or os.getcwd(),
        "prompt": prompt,
        "config": config or [],
        "image_paths": image_paths or [],
    }
    conn.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))

    with conn, CliOutput(log_file or os.environ.get("SA_RAW_LOG")) as output:
        final_sid, effective_agent, switched_engine = session_id, agent, False
        final_result = {"status": "ERROR", "error": "常驻守护进程连接意外断开"}
        try:
            with conn.makefile("r", encoding="utf-8", errors="replace") as rf:
                for line in rf:
                    line = line.strip()
                    if not line:
                        continue
                    frame = json.loads(line)
                    ftype = frame.get("type")
                    if ftype == "event":
                        evt = frame.get("event")
                        evt = evt if isinstance(evt, dict) else frame
                        output.emit(evt)
                        if evt.get("event") == "quota_fallback" or evt.get("type") == "quota_fallback":
                            effective_agent, final_sid, switched_engine = evt.get("to_agent") or effective_agent, "", True
                    elif ftype == "error":
                        final_result = {"status": "ERROR", "error": frame.get("error", "预检失败")}
                        output.emit(frame)
                    elif ftype == "result":
                        effective_agent = frame.get("agent") or effective_agent
                        final_sid = (frame.get("conversation_id") or "") if switched_engine or effective_agent != agent else (frame.get("conversation_id") or final_sid)
                        final_result = frame.get("result") or final_result
                        break
        finally:
            try:
                conn.close()
            except Exception:
                pass

        out_record: Dict[str, Any] = {
            "event": "result",
            "agent": effective_agent,
            "conversation_id": final_sid,
            "result": final_result,
        }
        if effective_agent == "codex":
            out_record["thread_id"] = final_sid
        output.emit(out_record)
        return 0 if final_result.get("status") == "SUCCESS" else 1


def main(argv: Optional[List[str]] = None, *, serve: Callable[[], None]) -> int:
    """Run a management command with the server entry point supplied by the daemon."""
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "serve":
        serve()
        return 0
    if cmd == "start":
        sock = ensure_daemon_running()
        snap = query_daemon("status")
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0 if sock.exists() else 1
    if cmd == "stop":
        res = query_daemon("shutdown")
        print(json.dumps(res, ensure_ascii=False))
        return 1 if res.get("status") == "error" else 0
    if cmd == "restart":
        def valid_pid(pid):
            return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
        sock_path = get_socket_path()
        before = query_daemon("status")
        if ((before.get("status") == "error" and not before.get("stale_socket"))
                or (before.get("ok") is True and not valid_pid(before.get("daemon_pid")))):
            err = {"ok": False, "status": "error", "error": before.get("error") or "invalid daemon_pid before restart"}
            print(json.dumps(err, ensure_ascii=False, indent=2))
            return 1
        old_pid = before.get("daemon_pid") if before.get("ok") else None
        res = query_daemon("shutdown") if old_pid is not None else {}
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _ping_daemon(sock_path) and (not res.get("ok") or not sock_path.exists()):
                break
            time.sleep(0.05)
        ensure_daemon_running()
        snap = query_daemon("status")
        if (snap.get("ok") is not True or not valid_pid(snap.get("daemon_pid"))
                or snap.get("daemon_pid") == old_pid):
            snap.update(ok=False, error=snap.get("error") or "daemon status or PID check failed after restart")
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0 if snap.get("ok") else 1
    if cmd == "status":
        res = query_daemon("status")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 1 if res.get("status") == "error" else 0
    print(f"Usage: sa-subagentd [start|stop|restart|status|serve]", file=sys.stderr)
    return 2
