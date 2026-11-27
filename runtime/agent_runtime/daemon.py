"""Standalone Resident Subagent Daemon for Coding Agents (isolated from zomi-bot-agy)."""
import atexit
import json
import os
import re
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .acp_stream import (
    AgyAcpStream,
    DroidAcpStream,
    GrokAcpStream,
    agy_server_command,
    droid_server_command,
    grok_server_command,
)
from .codex_stream import CodexStream, codex_server_command
from .pi_stream import PiRpcStream, pi_server_command
from .proc_utils import EXTRA_BIN_DIRS, find_cli_binary, terminate_proc_group
from .resident import ResidentServer
from .agy_quota import invalidate_agy_quota_cache, preflight_agy_request
from .session_handoff import (
    extract_event_tokens,
    _unwrap_handoff_prompt,
    prepare_session_handoff,
    record_turn_completion,
    seed_handoff_ledger,
)

from .cli_events import CliOutput, should_emit
from .host_reaper import evict_idle_hosts, resolve_host_idle_timeout

IDLE_TIMEOUT_SECONDS = 1800.0


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


def build_server_cmd_and_stream_cls(
    agent: str, config_list: Optional[List[str]] = None
) -> Tuple[List[str], Any]:
    agent = agent.strip().lower()
    binary = find_cli_binary(agent)
    if not binary:
        raise RuntimeError(f"找不到官方 {agent} CLI 可执行文件")
    if agent == "agy":
        return agy_server_command([binary]), AgyAcpStream
    if agent == "codex":
        raw_cmd = [binary, "exec", "--dangerously-bypass-hook-trust"]
        for c in config_list or []:
            raw_cmd.extend(["-c", c])
        raw_cmd.append("prompt")
        return codex_server_command(raw_cmd), CodexStream
    if agent == "droid":
        return droid_server_command([binary]), DroidAcpStream
    if agent == "grok":
        return grok_server_command([binary]), GrokAcpStream
    if agent == "pi":
        return pi_server_command([binary]), PiRpcStream
    raise ValueError(f"不支持的子代理引擎：{agent}")


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
        agent = str(req.get("agent") or "agy").strip().lower()
        model = str(req.get("model") or "").strip()
        effort = str(req.get("effort") or "").strip()
        service_tier = str(req.get("service_tier") or "").strip().lower()
        mode = str(req.get("mode") or "").strip()
        session_id = str(req.get("session_id") or "").strip()
        cwd = str(req.get("cwd") or os.getcwd()).strip()
        prompt = str(req.get("prompt") or "")
        config_list = req.get("config") or []
        image_paths = req.get("image_paths") or []
        preflight_event, preflight_handoff = None, None

        requested_sid = session_id
        discovered_sid = session_id
        claimed_sid = False

        with self.lock:
            self.active_runs += 1
            self.last_activity_mono = time.monotonic()

        proc = None
        host = None
        inbox = None
        client_eof = threading.Event()
        turn_done = threading.Event()

        def watch_client_disconnect() -> None:
            while not turn_done.is_set() and not client_eof.is_set():
                try:
                    rlist, _, _ = select.select([conn], [], [], 0.2)
                    if rlist:
                        data = conn.recv(1, socket.MSG_PEEK)
                        if not data:
                            client_eof.set()
                            if proc is not None:
                                terminate_proc_group(proc, wait_timeout=0.5)
                            break
                except Exception:
                    client_eof.set()
                    if proc is not None:
                        terminate_proc_group(proc, wait_timeout=0.5)
                    break

        watcher = threading.Thread(target=watch_client_disconnect, daemon=True)
        watcher.start()

        def send_frame(obj: Dict[str, Any]) -> bool:
            if client_eof.is_set():
                return False
            if obj.get("type") == "event" and not should_emit(obj.get("event"), req.get("emit_deltas")):
                return True
            try:
                conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                return True
            except Exception:
                client_eof.set()
                return False

        pending_result: Optional[Dict[str, Any]] = None
        try:
            if requested_sid:
                claimed_sid = self.claim_session_id(requested_sid)
            if agent == "agy":
                pf = preflight_agy_request(agent=agent, model=model, effort=effort, mode=mode, requested_sid=requested_sid,
                                           cwd=cwd, prompt=prompt, state_dir=self.socket_path.parent)
                if pf["should_fallback"]:
                    agent, model, effort, mode, prompt = (pf[k] for k in ("agent", "model", "effort", "mode", "prompt"))
                    session_id, discovered_sid, preflight_event, preflight_handoff = "", "", pf["event"], pf["handoff"]
            if preflight_event:
                send_frame({"type": "event", "event": preflight_event})
            os.makedirs(cwd, exist_ok=True)
            env = build_clean_subagent_env()
            env["PATH"] = str(Path(__file__).resolve().parents[1] / "bin") + os.pathsep + env.get("PATH", "")
            server_cmd, stream_cls = build_server_cmd_and_stream_cls(agent, config_list)
            raw_prompt, d_dir = prompt, self.socket_path.parent
            if preflight_handoff:
                _, raw_prompt = _unwrap_handoff_prompt(prompt)
            handoff = preflight_handoff or prepare_session_handoff(agent, session_id, raw_prompt, d_dir)
            if handoff and not preflight_handoff:
                session_id, prompt = "", handoff["effective_prompt"]
                send_frame({"type": "event", "event": handoff["event"]})
            host, inbox = self._acquire_resident(agent, cwd, server_cmd, session_id, env)
            bot_cfg: Dict[str, Any] = {
                "model": model,
                "session_id": session_id,
                "reasoning_effort": effort,
            }
            if service_tier:
                bot_cfg["service_tier"] = service_tier
            if mode:
                bot_cfg["mode"] = mode

            proc = stream_cls(
                server_cmd,
                cwd=cwd,
                env=env,
                bot=bot_cfg,
                prompt=prompt,
                image_paths=image_paths,
                resident=host,
                inbox=inbox,
                is_subagent=True,
                **({"emit_cumulative": bool(req.get("emit_deltas", os.environ.get("SA_CODEX_EMIT_DELTAS") == "1"))}
                   if stream_cls is CodexStream else {}),
            )
            init_sid = (
                getattr(proc, "session_file", "")
                or getattr(proc, "session_id", "")
                or getattr(proc, "thread_id", "")
            )
            handoff_seeded = False
            if handoff and init_sid:
                seed_handoff_ledger(agent, init_sid, handoff, d_dir)
                handoff_seeded = True
            if client_eof.is_set():
                terminate_proc_group(proc, wait_timeout=0.5)
                return

            chunks: List[str] = []
            codex_messages: Dict[str, str] = {}
            discovered_sid = init_sid or discovered_sid
            stream_tokens = 0

            events = proc.iter_events() if isinstance(proc, CodexStream) else proc
            for line in events:
                if client_eof.is_set():
                    break
                event = json.loads(line) if isinstance(line, str) else line
                stream_tokens = extract_event_tokens(event) or stream_tokens
                if event.get("event") == "init" and event.get("conversation_id"):
                    discovered_sid = str(event["conversation_id"])
                    if handoff and discovered_sid and not handoff_seeded:
                        seed_handoff_ledger(agent, discovered_sid, handoff, d_dir)
                        handoff_seeded = True
                elif event.get("type") == "thread.started" and event.get("thread_id"):
                    discovered_sid = str(event["thread_id"])
                    if handoff and discovered_sid and not handoff_seeded:
                        seed_handoff_ledger(agent, discovered_sid, handoff, d_dir)
                        handoff_seeded = True
                elif event.get("type") == "text_delta":
                    chunks.append(event.get("delta") or "")
                elif (
                    event.get("type") == "item.completed"
                    and isinstance(event.get("item"), dict)
                    and event["item"].get("type") == "agent_message"
                ):
                    text_val = event["item"].get("text") or ""
                    item_id = str(event["item"].get("id") or f"msg_{len(codex_messages)}")
                    if event.get("delta") or text_val:
                        codex_messages[item_id] = text_val
                if not send_frame({"type": "event", "event": event}):
                    break
                if event.get("type") == "native_question":
                    raise RuntimeError("子代理触发了交互式提问（禁止子代理向用户提问），已中止该回合")

            final_sid = (
                getattr(proc, "session_file", "")
                or getattr(proc, "session_id", "")
                or getattr(proc, "thread_id", "")
                or discovered_sid
            )
            response_text = (
                "".join(codex_messages.values()) if codex_messages else "".join(chunks)
            )
            pending_result = {
                "type": "result", "agent": agent,
                "conversation_id": final_sid,
                "result": {"status": "SUCCESS", "response": response_text},
            }
            if not client_eof.is_set():
                try:
                    record_turn_completion(agent, final_sid, raw_prompt, response_text, stream_tokens, handoff, d_dir)
                except Exception as ledger_exc:
                    warning = str(ledger_exc)
                    pending_result["result"]["ledger_warning"] = warning
                    try:
                        print(f"session ledger write failed: {warning}", file=sys.stderr)
                    except (OSError, ValueError):
                        pass  # Logging must not override the completed turn.
        except Exception as exc:
            if agent == "agy" and re.search(r"quota|exhausted|rate[ _-]?limit|\b429\b|配额|额度", str(exc), re.I):
                invalidate_agy_quota_cache()
            if isinstance(exc, RuntimeError) and "cross-engine handoff" in str(exc):
                send_frame({"type": "error", "error": str(exc)})
            final_sid = discovered_sid or ""
            pending_result = {
                "type": "result", "agent": agent,
                "conversation_id": final_sid,
                "result": {"status": "ERROR", "error": str(exc)},
            }
        finally:
            turn_done.set()
            if proc is not None:
                terminate_proc_group(proc, wait_timeout=0.5)
            elif host is not None and inbox is not None:
                try:
                    host.release(inbox, healthy=False)
                except Exception:
                    pass
            with self.lock:
                if claimed_sid:
                    self.release_session_id(requested_sid)
                self.active_runs = max(0, self.active_runs - 1)
                self.last_activity_mono = time.monotonic()
            if pending_result is not None:
                send_frame(pending_result)

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
            return json.loads(buf.decode("utf-8", errors="replace").strip())
    except Exception as exc:
        return {"ok": False, "status": "stopped", "error": str(exc)}


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


def main(argv: Optional[List[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "serve":
        daemon = SubagentDaemon()
        daemon.serve_forever()
        return 0
    if cmd == "start":
        sock = ensure_daemon_running()
        snap = query_daemon("status")
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0 if sock.exists() else 1
    if cmd == "stop":
        res = query_daemon("shutdown")
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if cmd == "restart":
        sock_path = get_socket_path()
        res = query_daemon("shutdown")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _ping_daemon(sock_path) and (not res.get("ok") or not sock_path.exists()):
                break
            time.sleep(0.05)
        ensure_daemon_running()
        print(json.dumps(query_daemon("status"), ensure_ascii=False, indent=2))
        return 0
    if cmd == "status":
        print(json.dumps(query_daemon("status"), ensure_ascii=False, indent=2))
        return 0
    print(f"Usage: sa-subagentd [start|stop|restart|status|serve]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
