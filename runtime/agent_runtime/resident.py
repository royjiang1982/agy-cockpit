"""Multiplexed stdio resident server and shared turn lifecycle across CLI engines."""
import json
import queue
import secrets
import subprocess
import threading
import time

from .proc_utils import terminate_proc_group
from .stream_base import ResidentTurn, SteeringRejected, SteeringUnknown

ACP_AGENTS = ("agy", "droid", "grok")


class ResidentServer:
    """Runtime-owned multiplexed stdio server with one reader and per-session mailboxes."""

    def __init__(
        self,
        cmd,
        *,
        cwd,
        env,
        agent,
        popen=subprocess.Popen,
        rpc_timeout=30,
        inject_document_token=False,
        client_name="agent-runtime",
    ):
        self.agent = str(agent or "agy").strip().lower()
        self.client_name = str(client_name or "agent-runtime")
        self.lease_token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.session_create_lock = threading.Lock()
        self.active_turns = []
        self.request_inboxes = {}
        self.session_inboxes = {}
        self.release_callbacks = {}
        self.retire_callbacks = []
        self.server_request_inboxes = {}
        self.bot_id = env.get("ZOMI_BOT_ID", "")
        self.parent_turns = 0
        self.retire_after_turn = False
        self.closed = False
        self.next_id = 0
        self.after_retire = None
        self.initialized = None
        self.last_used_monotonic = time.monotonic()

        spawn_env = dict(env)
        if inject_document_token:
            spawn_env["ZOMI_DOCUMENT_TOKEN"] = self.lease_token
            self.subagent_dispatch_token = spawn_env["ZOMI_DOCUMENT_TOKEN"]
        else:
            spawn_env.pop("ZOMI_DOCUMENT_TOKEN", None)
            self.subagent_dispatch_token = self.lease_token

        self.process = popen(
            cmd,
            cwd=cwd,
            env=spawn_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        inbox = self.acquire()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            rid = self.allocate_id(inbox)
            if self.agent == "pi":
                init_msg = {"id": str(rid), "type": "get_state"}
            elif self.agent in ACP_AGENTS:
                init_msg = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": client_name, "version": "1.0.0"},
                    },
                }
            elif self.agent == "codex":
                init_msg = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": client_name, "version": "1.0.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                }
            else:
                raise ValueError(f"Unsupported resident agent type: {self.agent}")

            self.process.stdin.write(json.dumps(init_msg) + "\n")
            self.process.stdin.flush()

            deadline = time.monotonic() + rpc_timeout
            event = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"{self.agent} 常驻连接初始化超时")
                try:
                    raw = inbox.get(timeout=remaining)
                except queue.Empty:
                    raise RuntimeError(f"{self.agent} 常驻连接初始化超时") from None
                if raw is None:
                    raise RuntimeError(f"{self.agent} 常驻连接初始化时关闭")
                parsed = json.loads(raw)
                if str(parsed.get("id")) == str(rid) and "method" not in parsed:
                    event = parsed
                    break

            if self.agent == "pi":
                if not event.get("success", False):
                    raise RuntimeError(str(event.get("error") or "Pi RPC 初始化失败"))
                self.initialized = event.get("data") or {}
            else:
                if "error" in event:
                    raise RuntimeError(str(event["error"]))
                self.initialized = event.get("result") or {}
                if self.agent in ACP_AGENTS and self.initialized.get("protocolVersion") != 1:
                    raise RuntimeError(f"{self.agent.upper()} ACP 协议版本不兼容")
                if self.agent == "codex":
                    self.process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
                    self.process.stdin.flush()
            self.release(inbox)
        except Exception:
            self.shutdown()
            raise

    @property
    def busy(self):
        with self.lock:
            return bool(self.active_turns)

    def allocate_id(self, inbox=None):
        with self.lock:
            self.next_id += 1
            if inbox is not None:
                self.request_inboxes[self.next_id] = inbox
                self.request_inboxes[str(self.next_id)] = inbox
            return self.next_id

    def bind_session(self, sid, inbox):
        with self.lock:
            previous = self.session_inboxes.get(sid)
            if previous is not None and previous is not inbox:
                return False
            if inbox not in self.active_turns:
                return False
            self.session_inboxes[sid] = inbox
            return True

    def acquire(self, session_id=""):
        with self.lock:
            if self.closed or self.retire_after_turn or self.process.poll() is not None:
                return None
            # Pi RPC maintains one active session state per process; do not interleave turns.
            if self.agent == "pi" and self.active_turns:
                return None
            if session_id and session_id in self.session_inboxes:
                return None
            inbox = queue.Queue()
            self.active_turns.append(inbox)
            if session_id:
                self.session_inboxes[session_id] = inbox
            self.last_used_monotonic = time.monotonic()
            return inbox

    def _pop_request_inbox(self, rid):
        owner = self.request_inboxes.pop(rid, None)
        s_rid = str(rid)
        o2 = self.request_inboxes.pop(s_rid, None)
        if owner is None:
            owner = o2
        try:
            i_rid = int(s_rid)
            o3 = self.request_inboxes.pop(i_rid, None)
            if owner is None:
                owner = o3
        except (ValueError, TypeError):
            pass
        return owner

    def _read(self):
        try:
            for raw in self.process.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                with self.lock:
                    owner = None
                    if self.agent == "pi":
                        if "id" in event and event.get("type") == "response":
                            owner = self._pop_request_inbox(event["id"])
                        if owner is None and self.active_turns:
                            owner = self.active_turns[0]
                    elif "id" in event and "method" not in event:
                        owner = self._pop_request_inbox(event["id"])
                        result = event.get("result") or {}
                        sid = (
                            (result.get("thread") or {}).get("id")
                            or result.get("sessionId")
                            if isinstance(result, dict)
                            else None
                        )
                        if owner is not None and sid:
                            if not self.bind_session(sid, owner):
                                owner.put(
                                    json.dumps(
                                        {
                                            "id": event["id"],
                                            "error": {"message": "服务端返回了已占用的会话 ID"},
                                        }
                                    )
                                )
                                continue
                    else:
                        params = event.get("params") or {}
                        sid = (
                            params.get("sessionId") or params.get("threadId")
                            if isinstance(params, dict)
                            else None
                        )
                        owner = self.session_inboxes.get(sid) if sid else None
                        if owner is None and event.get("method") == "serverRequest/resolved":
                            owner = self.server_request_inboxes.pop(
                                params.get("requestId"), None
                            )
                        if owner is None and not sid and len(self.active_turns) == 1:
                            owner = self.active_turns[0]
                        if owner is not None and "id" in event:
                            self.server_request_inboxes[event["id"]] = owner
                    if owner is not None:
                        owner.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self.closed = True
                for inbox in self.active_turns:
                    inbox.put(None)

    def release(self, inbox, *, healthy=True):
        with self.lock:
            self.last_used_monotonic = time.monotonic()
            callback = self.release_callbacks.pop(inbox, None)
            if callback:
                callback()
            self.active_turns = [q for q in self.active_turns if q is not inbox]
            self.session_inboxes = {
                sid: q for sid, q in self.session_inboxes.items() if q is not inbox
            }
            self.request_inboxes = {
                rid: q for rid, q in self.request_inboxes.items() if q is not inbox
            }
            self.server_request_inboxes = {
                rid: q for rid, q in self.server_request_inboxes.items() if q is not inbox
            }
            if not healthy:
                self.retire_after_turn = True
            retire = not self.active_turns and (self.retire_after_turn or self.closed)
            retire_cb = None
            if retire:
                retire_cb, self.after_retire = self.after_retire, None
        if retire:
            self.shutdown()
            if retire_cb:
                retire_cb()

    def shutdown(self):
        with self.lock:
            self.retire_after_turn = True
            self.closed = True
            callbacks = list(self.release_callbacks.values())
            self.release_callbacks.clear()
            for inbox in self.active_turns:
                inbox.put(None)
            self.active_turns.clear()
            self.session_inboxes.clear()
            self.request_inboxes.clear()
            self.server_request_inboxes.clear()
            retire_callbacks, self.retire_callbacks = self.retire_callbacks, []
        for callback in callbacks:
            callback()
        terminate_proc_group(self.process)
        for callback in retire_callbacks:
            callback()
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass

