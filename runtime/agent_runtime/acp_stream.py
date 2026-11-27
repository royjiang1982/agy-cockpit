"""Unified ACP (Agent Client Protocol) stream client for agy, droid, and grok."""
import base64
import json
import mimetypes
from pathlib import Path
import queue
import subprocess
import threading

from .native_questions import NativeQuestions
from .proc_utils import agy_app_data_dir, split_model_effort
from .resident import ResidentServer, ResidentTurn, SteeringRejected, SteeringUnknown


def agy_server_command(cmd):
    binary = cmd[0] if cmd else "agy"
    base = [binary, "acp", "--dangerously-skip-permissions"]
    app_dir = agy_app_data_dir()
    if app_dir:
        base.extend(["--app-data-dir", app_dir])
    return base


def droid_server_command(cmd):
    binary = cmd[0] if cmd else "droid"
    extra = []
    i = 1
    while i < len(cmd):
        arg = cmd[i]
        if arg in ("--append-system-prompt", "--append-system-prompt-file", "--settings") and i + 1 < len(cmd):
            extra.extend([arg, cmd[i + 1]])
            i += 2
            continue
        i += 1
    return [binary, "exec", "--auto", "high", "-o", "acp", *extra]


def grok_server_command(cmd):
    binary = cmd[0] if cmd else "grok"
    return [binary, "agent", "--always-approve", "stdio"]


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
        self.client_name = str(
            getattr(resident, "client_name", "") or client_name or "agent-runtime"
        )
        self.resident = resident
        self.is_resident_turn = resident is not None
        self._inbox = inbox if resident else None
        if resident and self._inbox is None:
            self._inbox = resident.acquire(str(bot.get("session_id") or ""))
            if self._inbox is None:
                self.resident = resident = None
                self.is_resident_turn = False
        self.tool_token = tool_token
        self._redact_buf = ""
        self._redact_lock = threading.RLock()
        self._retain_marker = False
        self.is_subagent = is_subagent
        self._marker_lock = threading.Lock()
        self._marker = None
        self._close_lock = threading.RLock()
        self._released = False
        self._protocol_done = threading.Event()

        spawn_env = dict(env)
        if self.is_subagent and self.agent == "grok":
            spawn_env["GROK_SUBAGENTS"] = "0"

        server_cmd = self._normalize_server_command(cmd)
        self.process = (
            resident.process
            if resident
            else popen(
                server_cmd,
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
        )
        self.pid = self.process.pid
        self.stdout = self
        self.rpc_timeout = rpc_timeout
        self.delivery_state = "failed"
        self.terminal_error = ""
        self.session_id = ""
        self._result = None
        self._pending = {}
        self._next_id = 0
        self._lock = threading.RLock()
        self._write_lock = resident.write_lock if resident else threading.Lock()
        self._events = queue.Queue()
        self.questions = NativeQuestions(self.agent, self._write, self._events.put)
        self._closed = threading.Event()
        self._finished = threading.Event()
        self._prompt_sent = False
        self._prompt_id = None
        self._active_prompts = set()
        self._steer_requests = {}
        self._root_error = ""
        self._tools = {}
        self._ready = threading.Event()
        self._steered = set()
        self._steering = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._starter = threading.Thread(
            target=self._start, args=(bot, prompt, image_paths, cwd), daemon=True
        )
        self._starter.start()

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

    @property
    def returncode(self):
        return self._result if self._result is not None else self.process.returncode

    def poll(self):
        return self.returncode if self.is_resident_turn else self.process.poll()

    def wait(self, timeout=None):
        if self.is_resident_turn:
            return self._resident_wait(timeout)
        self.process.wait(timeout=timeout)
        return self.returncode

    def cancel(self):
        if self.session_id and self._prompt_sent and not self._finished.is_set():
            self._write({"method": "session/cancel", "params": {"sessionId": self.session_id}})

    def can_steer(self):
        return (
            self._ready.is_set()
            and not self._finished.is_set()
            and not self._closed.is_set()
            and not self.questions.waiting()
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

    def terminate(self):
        if self.is_resident_turn:
            try:
                self.cancel()
            except Exception:
                pass
            self.close()
            return
        self.process.terminate()

    def kill(self):
        if self.is_resident_turn:
            try:
                self.cancel()
            except Exception:
                pass
            self.close()
            return
        self.process.kill()

    def close(self):
        if self.is_resident_turn:
            return self._resident_close()
        self.questions.close()
        self._clear_subagent_marker()
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass

    def __iter__(self):
        while True:
            event = self._events.get()
            if event is None:
                if self.terminal_error:
                    raise RuntimeError(
                        self.terminal_error.replace(self.tool_token, "<本轮授权>")
                        if self.tool_token
                        else self.terminal_error
                    )
                return
            line = json.dumps(event, ensure_ascii=False) + "\n"
            yield line.replace(self.tool_token, "<本轮授权>") if self.tool_token else line

    def _write(self, message):
        if not self._write_lock.acquire(timeout=self.rpc_timeout):
            raise RuntimeError("ACP 控制通道忙，发送结果未确认")
        done = queue.Queue(maxsize=1)

        def write():
            try:
                if self._closed.is_set():
                    raise RuntimeError("ACP 连接已关闭，结果未确认")
                if message.get("method") == "session/prompt":
                    self.delivery_state = "unknown"
                    self._prompt_sent = True
                self.process.stdin.write(
                    json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n"
                )
                self.process.stdin.flush()
                done.put(None)
            except Exception as exc:
                done.put(exc)
            finally:
                self._write_lock.release()

        threading.Thread(target=write, daemon=True).start()
        try:
            error = done.get(timeout=self.rpc_timeout)
        except queue.Empty:
            raise RuntimeError("ACP 写入超时，发送结果未确认，未自动重发") from None
        if error:
            raise error

    def _rpc(self, method, params, *, prompt=False):
        result = queue.Queue(maxsize=1)
        with self._lock:
            rid = self._allocate_id()
            self._pending[rid] = result
            if prompt:
                self._active_prompts.add(rid)
                if self._prompt_id is None:
                    self._prompt_id = rid
        try:
            self._write({"id": rid, "method": method, "params": params})
            try:
                reply = result.get(timeout=None if prompt else self.rpc_timeout)
            except queue.Empty:
                raise RuntimeError(f"ACP {method} 确认超时，未自动重发") from None
            if "error" in reply:
                err_obj = reply["error"]
                msg = err_obj.get("message", "请求失败") if isinstance(err_obj, dict) else str(err_obj)
                raise RuntimeError(f"ACP {method}: {msg}")
            return reply.get("result") or {}
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    def _finish(self, error=""):
        with self._lock:
            if self._finished.is_set():
                return
            self.terminal_error = error
            self._result = 1 if error else 0
            self._flush_redaction()
            self._finished.set()
            self._maybe_clear_subagent_marker()
            self.questions.close()
            self._events.put({"type": "control_delivery", "state": self.delivery_state})
            if not error:
                self._events.put({"type": "turn.completed"})
            self._events.put(None)

    @staticmethod
    def model_id(bot):
        model = str(bot.get("model") or "gemini-4-carbon")
        if model.endswith(("-low", "-medium", "-high", "-xhigh", "-extra", "-max")):
            return model
        effort = str(bot.get("reasoning_effort") or "high")
        return model + "-" + (effort if effort in ("low", "medium", "high", "xhigh", "extra", "max") else "high")

    def _configure_session(self, bot, session):
        available = {
            m.get("modelId")
            for m in (session.get("models") or {}).get("availableModels", [])
            if isinstance(m, dict) and m.get("modelId")
        }
        if self.agent == "agy":
            model = self.model_id(bot)
            if available and model not in available:
                raise RuntimeError(f"当前 ACP 客户端不可使用模型 {model}，请选择可用模型")
            self._rpc("session/set_model", {"sessionId": self.session_id, "modelId": model})
            mode = str(bot.get("mode") or "accept-edits")
            self._rpc("session/set_mode", {"sessionId": self.session_id, "modeId": mode})
            return model

        if self.agent == "droid":
            raw_model, effort = split_model_effort(
                bot.get("model"), bot.get("reasoning_effort"), default_model="gpt-6.2-astra", default_effort=""
            )
            if available and raw_model not in available:
                for suffix in ("-low", "-medium", "-high", "-xhigh", "-max"):
                    if raw_model.endswith(suffix) and raw_model[: -len(suffix)] in available:
                        if not effort:
                            effort = suffix[1:]
                        raw_model = raw_model[: -len(suffix)]
                        break
            if available and raw_model not in available:
                raise RuntimeError(f"当前 Droid ACP 不可使用模型 {raw_model}")
            self._rpc("session/set_model", {"sessionId": self.session_id, "modelId": raw_model})
            effort = (effort or "xhigh").lower()
            cfg_options = session.get("configOptions") or []
            if any(isinstance(c, dict) and c.get("id") == "reasoning_effort" for c in cfg_options):
                self._rpc(
                    "session/set_config_option",
                    {"sessionId": self.session_id, "configId": "reasoning_effort", "value": effort},
                )
            mode = str(bot.get("mode") or "auto-high")
            self._rpc("session/set_mode", {"sessionId": self.session_id, "modeId": mode})
            return f"{raw_model}:{effort}"

        if self.agent == "grok":
            raw_model = str(bot.get("model") or "grok-4.7").strip()
            effort = str(bot.get("reasoning_effort") or "xhigh").strip().lower()
            if available and raw_model not in available:
                raise RuntimeError(f"当前 Grok ACP 不可使用模型 {raw_model}")
            self._rpc("session/set_model", {"sessionId": self.session_id, "modelId": raw_model})
            cfg_options = session.get("configOptions") or []
            if any(isinstance(c, dict) and c.get("id") == "reasoning_effort" for c in cfg_options):
                self._rpc(
                    "session/set_config_option",
                    {"sessionId": self.session_id, "configId": "reasoning_effort", "value": effort},
                )
            return f"{raw_model}:{effort}"

        return str(bot.get("model") or "")

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
                prompt = (
                    "你是被派出的子代理（SA_SUBAGENT=1）。只遵守任务书，不派生子代理、不提交代码。\n\n"
                    + prompt
                )
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
            for raw in self._lines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
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
                    if waiter:
                        waiter.put_nowait(event)
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
                for waiter in self._pending.values():
                    try:
                        waiter.put_nowait(
                            {"error": {"message": "连接已关闭，任务结果未确认，未自动重发"}}
                        )
                    except queue.Full:
                        pass
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
