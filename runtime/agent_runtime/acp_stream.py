"""Unified ACP (Agent Client Protocol) stream client for agy, droid, and grok."""
import base64
import mimetypes
from pathlib import Path
import subprocess
import threading

from .acp_approvals import AcpAutoApprover
from .acp_setup import (
    agy_server_command, configure_session, droid_server_command, grok_server_command, model_id,
)
from .handoff_text import SUBAGENT_PREFIX
from .resident import ResidentServer, ResidentTurn, SteeringRejected, SteeringUnknown


class AcpStream(ResidentTurn):
    """Multiplexed ACP session turn supporting agy, droid, and grok."""

    AGENT_NAME = "agy"

    def __init__(
        self,
        cmd,
        *,
        cwd,
        env,
        bot,
        prompt,
        image_paths=(),
        popen=subprocess.Popen,
        rpc_timeout=30,
        resident=None,
        inbox=None,
        is_subagent=False,
        tool_token=None,
        agent=None,
        client_name="agent-runtime",
    ):
        self.agent = str(agent or self.AGENT_NAME or "agy").strip().lower()
        spawn_env = dict(env)
        if is_subagent and self.agent == "grok":
            spawn_env["GROK_SUBAGENTS"] = "0"
        super().__init__(
            self._normalize_server_command(cmd), cwd=cwd, env=spawn_env, bot=bot,
            popen=popen, rpc_timeout=rpc_timeout, resident=resident, inbox=inbox,
            is_subagent=is_subagent, tool_token=tool_token, client_name=client_name,
        )
        self.session_id = ""
        self.approvals = AcpAutoApprover(
            self._write, lambda: self._closed.is_set() or self._finished.is_set(), self._finish
        )
        self._prompt_id = None
        self._active_prompts = set()
        self._steer_requests = {}
        self._root_error = ""
        self._tools = {}
        self._start_workers(bot, prompt, image_paths, cwd)

    def _normalize_server_command(self, cmd):
        if not cmd:
            return cmd
        name = Path(cmd[0]).name.lower()
        if self.agent == "droid" and name == "droid" and "acp" not in cmd:
            return droid_server_command(cmd)
        if self.agent == "grok" and name == "grok" and "stdio" not in cmd:
            return grok_server_command(cmd)
        if self.agent == "agy" and name == "agy" and "acp" not in cmd:
            return agy_server_command(cmd)
        return cmd

    def cancel(self):
        self.approvals.cancel_all(send_cancel=not self._closed.is_set())
        if self.session_id and self._prompt_sent and not self._finished.is_set():
            self._write({"method": "session/cancel", "params": {"sessionId": self.session_id}})

    def can_steer(self):
        return (
            self._ready.is_set()
            and not self._finished.is_set()
            and not self._closed.is_set()
            and not self.questions.waiting()
            and not self.approvals.waiting()
        )

    def steer(self, message_id, text, images=()):
        if images:
            raise SteeringRejected("带图片的追加消息由下一轮处理")
        with self._lock:
            if message_id in self._steered:
                return True
            if not self.can_steer():
                raise SteeringRejected("当前执行轮已结束")
            rid = self._allocate_id()
            marker = dict(id=message_id, text=text, ack=threading.Event(), error=None)
            self._steering.append(marker)
            self._steer_requests[rid] = marker
            self._active_prompts.add(rid)
        try:
            self._write(
                {
                    "id": rid,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": self.session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                }
            )
        except Exception as exc:
            raise SteeringUnknown(str(exc)) from exc
        if not marker["ack"].wait(self.rpc_timeout):
            raise SteeringUnknown("ACP 追加消息尚未确认，继续等待原生事件，未自动重发")
        if marker["error"]:
            raise marker["error"]
        with self._lock:
            self._steered.add(message_id)
        return True

    WRITE_BUSY = "ACP 控制通道忙，发送结果未确认"
    WRITE_CLOSED = "ACP 连接已关闭，结果未确认"
    WRITE_TIMEOUT = "ACP 写入超时，发送结果未确认，未自动重发"

    def _close_protocol(self):
        self.approvals.cancel_all(send_cancel=False)

    def _prepare_write(self, message):
        if message.get("method") == "session/prompt":
            self.delivery_state = "unknown"
            self._prompt_sent = True
        return {"jsonrpc": "2.0", **message}

    def _request_id(self, method, prompt):
        rid = self._allocate_id()
        if prompt:
            self._active_prompts.add(rid)
            if self._prompt_id is None:
                self._prompt_id = rid
        return rid

    def _request_timeout(self, method):
        return RuntimeError(f"ACP {method} 确认超时，未自动重发")

    def _request_result(self, method, reply):
        if "error" in reply:
            error = reply["error"]
            msg = error.get("message", "请求失败") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"ACP {method}: {msg}")
        return reply.get("result") or {}

    model_id = staticmethod(model_id)

    _configure_session = configure_session

    def _open_session(self, method, params):
        return self._rpc(method, params)

    def _start(self, bot, prompt, images, cwd):
        try:
            init = (
                self.resident.initialized
                if self.is_resident_turn
                else self._rpc(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": self.client_name, "version": "1.0.0"},
                    },
                )
            )
            if init.get("protocolVersion") != 1:
                raise RuntimeError(f"{self.agent.upper()} ACP 协议版本不兼容")
            caps = init.get("agentCapabilities") or {}
            sid = str(bot.get("session_id") or "")
            params = {"cwd": cwd, "mcpServers": []}
            method = "session/new"
            if sid:
                if self.is_resident_turn and not self.resident.bind_session(sid, self._inbox):
                    raise RuntimeError("会话已被另一个回合占用")
                params["sessionId"] = sid
                if "resume" in (caps.get("sessionCapabilities") or {}):
                    method = "session/resume"
                elif caps.get("loadSession"):
                    method = "session/load"
                else:
                    raise RuntimeError(f"{self.agent.upper()} ACP 不支持续接既有会话，未创建替代会话")
            session = self._open_session(method, params)
            self.session_id = str(session.get("sessionId") or sid)
            if not self.session_id:
                raise RuntimeError(f"{self.agent.upper()} ACP 未返回会话 ID")
            self._mark_subagent(self.session_id)
            self._events.put(
                {
                    "event": "init",
                    "agent": self.agent,
                    "conversation_id": self.session_id,
                }
            )
            self._configure_session(bot, session)
            if self.is_subagent:
                prompt = SUBAGENT_PREFIX + "\n\n" + prompt
            content = [{"type": "text", "text": prompt}]
            if images and not (caps.get("promptCapabilities") or {}).get("image"):
                raise RuntimeError(f"当前 {self.agent.upper()} ACP 不支持图片输入")
            for path in images or ():
                mime = mimetypes.guess_type(path)[0] or ""
                if not mime.startswith("image/"):
                    raise RuntimeError("ACP 附件不是支持的图片类型")
                content.append(
                    {
                        "type": "image",
                        "mimeType": mime,
                        "data": base64.b64encode(Path(path).read_bytes()).decode("ascii"),
                    }
                )
            self._rpc(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": content},
                prompt=True,
            )
        except Exception as exc:
            if self._prompt_sent and not self._closed.is_set() and not self._finished.is_set():
                self._events.put({"type": "control_warning", "message": str(exc)})
                self._events.put({"type": "control_delivery", "state": self.delivery_state})
                return
            self._finish(str(exc))

    def _read(self):
        try:
            for event in self._incoming_events():
                if "id" in event and "method" not in event:
                    with self._lock:
                        waiter = self._pending.get(event["id"])
                    if event["id"] in self._active_prompts:
                        with self._lock:
                            self._active_prompts.discard(event["id"])
                            marker = self._steer_requests.pop(event["id"], None)
                            if "error" in event:
                                err_obj = event["error"]
                                error = (
                                    str(err_obj.get("message", "请求失败"))
                                    if isinstance(err_obj, dict)
                                    else str(err_obj)
                                )
                                if marker:
                                    marker["error"] = SteeringRejected(error)
                                    if marker in self._steering:
                                        self._steering.remove(marker)
                                else:
                                    self._root_error = f"ACP session/prompt: {error}"
                                    if self.delivery_state == "unknown":
                                        self.delivery_state = "failed"
                            else:
                                self.delivery_state = "delivered"
                                reason = (event.get("result") or {}).get("stopReason")
                                if reason and reason != "end_turn":
                                    self._root_error = f"{self.agent.upper()} ACP 执行未正常完成：{reason}"
                            if marker:
                                marker["ack"].set()
                            if not self._active_prompts:
                                self._protocol_done.set()
                                self._finish(self._root_error)
                    self._deliver(waiter, event)
                elif "id" in event:
                    if event.get("method") == "session/request_permission":
                        params = event.get("params") or {}
                        tool = params.get("toolCall") or {}
                        if str(tool.get("toolCallId") or "").startswith("interaction_"):
                            self.questions.add(
                                event["id"],
                                {
                                    "question": tool.get("title") or "请选择",
                                    "allowOther": False,
                                    "options": [
                                        {
                                            "label": o.get("name") or o.get("optionId", ""),
                                            "value": o["optionId"],
                                        }
                                        for o in params.get("options", [])
                                    ],
                                },
                            )
                            continue
                        if self.agent == "agy":
                            err = self.approvals.handle_permission(
                                event, session_id=self.session_id, prompt_sent=self._prompt_sent
                            )
                            if err:
                                self._finish(err)
                            continue
                        self._write({"id": event["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                        self._finish(
                            f"{self.agent.upper()} 请求了额外权限，操作未获批准；请检查本机权限规则后重试"
                        )
                    else:
                        self._write(
                            {
                                "id": event["id"],
                                "error": {"code": -32601, "message": "Unsupported ACP client request"},
                            }
                        )
                        self._finish(
                            f"{self.agent.upper()} 请求了当前客户端不支持的能力：" + str(event.get("method"))
                        )
                elif event.get("method") == "session/update":
                    self._update(event.get("params") or {})
        except Exception as exc:
            self._finish("ACP 读取失败：" + str(exc))
        finally:
            self._closed.set()
            with self._lock:
                for marker in self._steer_requests.values():
                    marker["error"] = SteeringUnknown("ACP 连接关闭，追加消息结果未确认")
                    marker["ack"].set()
            self._fail_pending({"error": {"message": "连接已关闭，任务结果未确认，未自动重发"}})
            self._finish("ACP 连接意外关闭，任务结果未确认，未自动重发")

    def _update(self, params):
        if not self._prompt_sent or params.get("sessionId") != self.session_id:
            return
        self.delivery_state = "delivered"
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        content = update.get("content") or {}
        if kind == "user_message_chunk" and content.get("type") == "text":
            self._flush_redaction()
            self._ready.set()
            with self._lock:
                marker = next(
                    (m for m in self._steering if m["text"] == content.get("text")), None
                )
                if marker:
                    self._steering.remove(marker)
                    self._events.put(
                        {
                            "type": "item.started",
                            "item": {"type": "user_message", "clientId": marker["id"]},
                        }
                    )
                    marker["ack"].set()
        elif kind == "agent_message_chunk" and content.get("type") == "text":
            chunk = self._redact_stream_chunk(content.get("text", ""))
            if chunk:
                self._events.put({"type": "text_delta", "delta": chunk})
        elif kind == "agent_thought_chunk" and content.get("type") == "text":
            chunk = self._redact_stream_chunk(content.get("text", ""))
            if chunk:
                self._events.put({"type": "thinking", "text": chunk})
        elif kind in ("tool_call", "tool_call_update"):
            self._flush_redaction()
            tid = update.get("toolCallId")
            if not tid:
                return
            tool = self._tools.setdefault(tid, {})
            tool.update(update)
            item = {
                "id": tid,
                "type": "tool",
                "name": tool.get("name") or tool.get("title") or tool.get("kind") or "tool",
                "arguments": tool.get("rawInput") or {},
                "status": tool.get("status", "pending"),
                "content": tool.get("content") or [],
                "output": tool.get("rawOutput"),
            }
            self._events.put({"type": "tool_call", "item": item})


class AgyAcpStream(AcpStream):
    AGENT_NAME = "agy"
    SESSION_NEW_RETRY_DELAYS = (0.15, 0.4)

    def _open_session(self, method, params):
        if method != "session/new":
            return self._rpc(method, params)
        lock = getattr(self.resident, "session_create_lock", None) if self.is_resident_turn else None
        delays = (0.0, *self.SESSION_NEW_RETRY_DELAYS)
        for idx, delay in enumerate(delays):
            if (delay and self._closed.wait(delay)) or self._closed.is_set():
                raise RuntimeError("ACP 连接已关闭，结果未确认")
            if lock and not lock.acquire(timeout=self.rpc_timeout):
                raise RuntimeError("ACP session/new 排队超时，未自动重发")
            try:
                return self._rpc(method, params)
            except RuntimeError as exc:
                if idx + 1 >= len(delays) or "failed to start antigravity conversation" not in str(exc).lower():
                    raise
            finally:
                if lock:
                    lock.release()


class DroidAcpStream(AcpStream):
    AGENT_NAME = "droid"


class GrokAcpStream(AcpStream):
    AGENT_NAME = "grok"
