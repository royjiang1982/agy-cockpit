"""Standalone Unix-socket daemon and isolated ResidentServer pools."""
import atexit
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# Keep the original module entry points as re-exports for callers.
from .daemon_client import (
    _ping_daemon, _verify_socket_security, ensure_daemon_running,
    main as client_main, query_daemon, run_subagent_client,
)
from .daemon_config import build_clean_subagent_env, get_daemon_dir, get_pid_path, get_socket_path
from .host_reaper import evict_idle_hosts, resolve_host_idle_timeout
from .resident import ResidentServer
from .turn_runner import (
    AgyAcpStream, CodexStream, DroidAcpStream, GrokAcpStream, PiRpcStream,
    build_server_cmd_and_stream_cls, preflight_agy_request, record_turn_completion, run_turn,
)

IDLE_TIMEOUT_SECONDS = 1800.0


class SubagentDaemon:
    """Multiplexed Unix-socket daemon managing standalone ResidentServer pools."""

    def __init__(
        self,
        socket_path: Optional[Path] = None,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        host_idle_timeout: Optional[float] = None,
    ):
        self.socket_path = socket_path or get_socket_path()
        self.pid_path = self.socket_path.parent / "daemon.pid"
        self.idle_timeout = idle_timeout
        self.host_idle_timeout = resolve_host_idle_timeout(host_idle_timeout)
        self.started_ts = time.time()
        self.last_activity_mono = time.monotonic()
        self.lock = threading.RLock()
        self.pools: Dict[Tuple[str, str, Tuple[str, ...]], List[ResidentServer]] = {}
        self.all_hosts: Set[ResidentServer] = set()
        self.active_sessions: Set[str] = set()
        self.active_runs = 0
        self.stopping = threading.Event()
        self.server_sock: Optional[socket.socket] = None
        self._bound_socket_inode: Optional[int] = None

    def _acquire_resident(
        self, agent: str, cwd: str, server_cmd: List[str], session_id: str, env: Dict[str, str]
    ) -> Tuple[ResidentServer, Any]:
        real_cwd = str(Path(cwd).resolve())
        key = (agent, real_cwd, tuple(server_cmd))
        with self.lock:
            if self.stopping.is_set():
                raise RuntimeError("守护进程正在关闭，无法分配会话槽位")
            self.last_activity_mono = time.monotonic()
            self.all_hosts = {
                h for h in self.all_hosts if not h.closed and h.process.poll() is None
            }
            if session_id and any(session_id in h.session_inboxes for h in self.all_hosts):
                raise RuntimeError("会话已被另一个活动回合占用")
            hosts = self.pools.get(key, [])
            alive_hosts: List[ResidentServer] = []
            chosen_host: Optional[ResidentServer] = None
            chosen_inbox = None
            for h in hosts:
                if h.closed or h.retire_after_turn or h.process.poll() is not None:
                    continue
                alive_hosts.append(h)
                self.all_hosts.add(h)
                if chosen_inbox is None:
                    inbox = h.acquire(session_id)
                    if inbox is not None:
                        chosen_host = h
                        chosen_inbox = inbox
            self.pools[key] = alive_hosts

        if chosen_host is not None and chosen_inbox is not None:
            return chosen_host, chosen_inbox

        new_host = ResidentServer(
            server_cmd,
            cwd=real_cwd,
            env=env,
            agent=agent,
            inject_document_token=False,
            client_name="coding-agent-subagentd",
        )
        with self.lock:
            if self.stopping.is_set():
                new_host.shutdown()
                raise RuntimeError("守护进程正在关闭，无法分配会话槽位")
            self.all_hosts = {
                h for h in self.all_hosts if not h.closed and h.process.poll() is None
            }
            if session_id and any(session_id in h.session_inboxes for h in self.all_hosts):
                new_host.shutdown()
                raise RuntimeError("会话已被另一个活动回合占用")
            inbox = new_host.acquire(session_id)
            if inbox is None:
                new_host.shutdown()
                raise RuntimeError(f"无法在新启动的 {agent} 常驻进程上分配会话槽位")
            self.all_hosts.add(new_host)
            self.pools.setdefault(key, []).append(new_host)
        return new_host, inbox

    def status_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            self.all_hosts = {
                h for h in self.all_hosts if not h.closed and h.process.poll() is None
            }
            servers_info = []
            seen = set()
            for (agent, cwd, _), hosts in self.pools.items():
                for h in hosts:
                    if not h.closed and h.process.poll() is None and h not in seen:
                        seen.add(h)
                        servers_info.append(
                            {
                                "agent": agent,
                                "cwd": cwd,
                                "pid": h.process.pid,
                                "busy": h.busy,
                                "active_turns": len(h.active_turns),
                                "sessions": list(h.session_inboxes.keys()),
                            }
                        )
            for h in self.all_hosts:
                if h not in seen and not h.closed and h.process.poll() is None:
                    seen.add(h)
                    servers_info.append(
                        {
                            "agent": h.agent,
                            "cwd": "",
                            "pid": h.process.pid,
                            "busy": h.busy,
                            "active_turns": len(h.active_turns),
                            "sessions": list(h.session_inboxes.keys()),
                        }
                    )
            return {
                "ok": not self.stopping.is_set(),
                "daemon_pid": os.getpid(),
                "socket": str(self.socket_path),
                "uptime_sec": round(time.time() - self.started_ts, 1),
                "active_runs": self.active_runs,
                "active_chains": 0,
                "resident_servers": servers_info,
            }

    def shutdown_all(self) -> None:
        self.stopping.set()
        if self.server_sock is not None:
            try:
                self.server_sock.close()
            except Exception:
                pass
        try:
            if self.socket_path.exists():
                if (
                    self._bound_socket_inode is None
                    or self.socket_path.lstat().st_ino == self._bound_socket_inode
                ):
                    self.socket_path.unlink()
        except OSError:
            pass
        try:
            if self.pid_path.exists() and self.pid_path.read_text().strip() == str(os.getpid()):
                self.pid_path.unlink()
        except OSError:
            pass
        with self.lock:
            all_hosts = set(self.all_hosts)
            for hosts in self.pools.values():
                all_hosts.update(hosts)
            self.all_hosts.clear()
            self.pools.clear()
        for h in all_hosts:
            try:
                h.shutdown()
            except Exception:
                pass

    def is_session_busy(self, session_id: str) -> bool:
        with self.lock:
            return bool(session_id and (session_id in self.active_sessions or any(session_id in h.session_inboxes for h in self.all_hosts)))

    def claim_session_id(self, session_id: str) -> bool:
        with self.lock:
            if not session_id:
                return False
            if self.is_session_busy(session_id):
                raise RuntimeError(f"会话 {session_id} 正在运行中，拒绝并发重入")
            self.active_sessions.add(session_id)
            return True

    def release_session_id(self, session_id: str) -> None:
        if session_id:
            with self.lock:
                self.active_sessions.discard(session_id)

    def _handle_run(self, conn: socket.socket, req: Dict[str, Any]) -> None:
        run_turn(self, conn, req)

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            line, _ = buf.split(b"\n", 1)
            req = json.loads(line.decode("utf-8", errors="replace"))
            action = req.get("action") or req.get("op")
            if action == "status":
                conn.sendall((json.dumps(self.status_snapshot(), ensure_ascii=False) + "\n").encode("utf-8"))
            elif action == "shutdown":
                self.stopping.set()
                conn.sendall((json.dumps({"ok": True, "stopping": True}) + "\n").encode("utf-8"))
                threading.Thread(target=self.shutdown_all, daemon=True).start()
            elif action == "run":
                self._handle_run(conn, req)
            else:
                conn.sendall(
                    (json.dumps({"ok": False, "error": f"Unknown action: {action}"}) + "\n").encode("utf-8")
                )
        except Exception as exc:
            try:
                conn.sendall(
                    (
                        json.dumps(
                            {"type": "result", "result": {"status": "ERROR", "error": str(exc)}},
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def serve_forever(self) -> None:
        daemon_dir = self.socket_path.parent
        daemon_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(daemon_dir, 0o700)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self._bound_socket_inode = self.socket_path.lstat().st_ino
        srv.listen(64)
        srv.settimeout(1.0)
        self.server_sock = srv
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(self.shutdown_all)

        def handle_sig(signum, frame):
            self.shutdown_all()
            raise SystemExit(0)

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, handle_sig)
            signal.signal(signal.SIGINT, handle_sig)

        try:
            while not self.stopping.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    evict_idle_hosts(self)
                    with self.lock:
                        idle = time.monotonic() - self.last_activity_mono
                        if self.active_runs == 0 and idle >= self.idle_timeout:
                            break
                    continue
                except OSError:
                    if self.stopping.is_set():
                        break
                    raise
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
        finally:
            self.shutdown_all()


def main(argv: Optional[List[str]] = None) -> int:
    return client_main(argv, serve=lambda: SubagentDaemon().serve_forever())


if __name__ == "__main__":
    sys.exit(main())
