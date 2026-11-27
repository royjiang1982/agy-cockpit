"""Codex stdio app-server transport with native in-turn steering and ResidentServer support."""
import json
import queue
import subprocess
import threading

from .native_questions import NativeQuestions
from .proc_utils import split_model_effort
from .resident import ResidentServer, ResidentTurn, SteeringRejected, SteeringUnknown


def codex_server_command(cmd):
    top_flags = []
    if "--dangerously-bypass-hook-trust" in cmd:
        top_flags.append("--dangerously-bypass-hook-trust")
    config_args = []
    for i, arg in enumerate(cmd[:-1]):
        if arg == "-c" and not cmd[i + 1].startswith("model_reasoning_effort="):
            config_args.extend(["-c", cmd[i + 1]])
    return [cmd[0], *top_flags, "app-server", "--stdio", *config_args]


class CodexStream(ResidentTurn):
    def __init__(
        self,
        cmd,
        *,
        cwd,
        env,
        bot,
        prompt,
        image_paths=None,
        popen=subprocess.Popen,
        rpc_timeout=30,
        resident=None,
        inbox=None,
        is_subagent=False,
        tool_token=None,
        client_name="agent-runtime",
        emit_cumulative=True,
    ):
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
        self._emit_cumulative = not is_subagent or emit_cumulative
        self._marker_lock = threading.Lock()
        self._marker = None
        self._close_lock = threading.RLock()
        self._released = False
        self._finished = threading.Event()
        self._protocol_done = threading.Event()
        self.process = (
            resident.process
            if resident
            else popen(
                codex_server_command(cmd),
                cwd=cwd,
                env=env,
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
        self._lock = threading.RLock()
        self._write_lock = resident.write_lock if resident else threading.Lock()
        self._pending = {}
        self.delivery_state = "failed"
        self._turn_request_id = None
        self.rpc_timeout = rpc_timeout
        self.terminal_error = ""
        self._message_text = {}
        self._next_id = 0
        self._events = queue.Queue()
        self.questions = NativeQuestions("codex", self._write, self._events.put)
        self._closed = threading.Event()
        self._ready = threading.Event()
        self.thread_id = ""
        self.turn_id = ""
        self._turn_result = None
        self._steered = set()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._starter = threading.Thread(
            target=self._start, args=(bot, prompt, image_paths or [], cwd), daemon=True
        )
        self._starter.start()

    @property
    def returncode(self):
        return self._turn_result if self._turn_result is not None else self.process.returncode

    def poll(self):
        return self.returncode if self.is_resident_turn else self.process.poll()

    def wait(self, timeout=None):
        if self.is_resident_turn:
            return self._resident_wait(timeout)
        self.process.wait(timeout=timeout)
        return self.returncode

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

    def cancel(self):
        if self.thread_id and self.turn_id:
            self._rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

    def close(self):
        if self.is_resident_turn:
            return self._resident_close()
        self.questions.close()
        self._clear_subagent_marker()
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass

    def __iter__(self):
        for event in self.iter_events():
            yield json.dumps(event, ensure_ascii=False) + "\n"

    def iter_events(self):
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
            if self.tool_token:
                event = json.loads(json.dumps(event, ensure_ascii=False).replace(self.tool_token, "<本轮授权>"))
            yield event

    def _write(self, message):
        if not self._write_lock.acquire(timeout=self.rpc_timeout):
            raise SteeringUnknown("控制通道忙，发送结果未确认；未终止任务或自动重发")
        done = queue.Queue(maxsize=1)

        def write():
            try:
                if self._closed.is_set():
                    raise SteeringUnknown("底层连接已关闭，不能确认追加消息是否送达")
                if message.get("method") == "turn/start":
                    self._delivery("unknown")
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                done.put(None)
            except Exception as exc:
                done.put(SteeringUnknown(f"控制通道写入失败，结果未知：{exc}"))
            finally:
                self._write_lock.release()

        threading.Thread(target=write, daemon=True).start()
        try:
            error = done.get(timeout=self.rpc_timeout)
        except queue.Empty:
            raise SteeringUnknown("控制通道写入超时，结果未知；未终止任务或自动重发") from None
        if error:
            raise error

    def _delivery(self, state):
        with self._lock:
            if self.delivery_state != "delivered":
                self.delivery_state = state

    def _rpc(self, method, params):
        result = queue.Queue(maxsize=1)
        with self._lock:
            request_id = self._allocate_id()
            self._pending[request_id] = result
            if method == "turn/start":
                self._turn_request_id = request_id
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                response = result.get(timeout=self.rpc_timeout)
            except queue.Empty:
                raise SteeringUnknown("控制请求确认超时，结果未知；未终止任务或自动重发") from None
            if response.get("transport_closed"):
                raise SteeringUnknown("底层连接已关闭，追加消息结果未知；未自动重发")
            if "error" in response:
                raise SteeringRejected(str(response["error"].get("message") or response["error"]))
            return response.get("result") or {}
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    @staticmethod
    def _input(text, images):
        return [{"type": "text", "text": text}] + [
            {"type": "localImage", "path": p} for p in images
        ]

    def _start(self, bot, prompt, images, cwd):
        starting_turn = False
        try:
            if not self.is_resident_turn:
                self._rpc(
                    "initialize",
                    {
                        "clientInfo": {"name": self.client_name, "version": "1.0.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                self._write({"method": "initialized"})
            model, effort = split_model_effort(
                bot.get("model"), bot.get("reasoning_effort"), default_model="", default_effort="xhigh"
            )
            service_tier = str(bot.get("service_tier") or "").strip().lower()
            params = {
                "cwd": cwd,
                "model": model or None,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
            }
            if service_tier:
                params["serviceTier"] = service_tier
            session = str(bot.get("session_id") or "")
            if self.is_subagent:
                params["baseInstructions"] = (
                    "你是被派出的子代理（SA_SUBAGENT=1）。只遵守任务书，不派生子代理、不提交代码。"
                )
            if session:
                if self.is_resident_turn and not self.resident.bind_session(session, self._inbox):
                    raise RuntimeError("会话已被另一个回合占用")
                params["threadId"] = session
            thread = self._rpc("thread/resume" if session else "thread/start", params)["thread"]
            self.thread_id = thread["id"]
            self._mark_subagent(self.thread_id)
            self._events.put({"type": "thread.started", "thread_id": self.thread_id})
            starting_turn = True
            turn_params = {
                "threadId": self.thread_id,
                "input": self._input(prompt, images),
                "effort": effort,
            }
            if service_tier:
                turn_params["serviceTier"] = service_tier
            result = self._rpc("turn/start", turn_params)
            with self._lock:
                if self._turn_result is None:
                    self.turn_id = result["turn"]["id"]
            self._ready.set()
            self._delivery("delivered")
            self._events.put({"type": "control_delivery", "state": "delivered"})
        except Exception as exc:
            if starting_turn and isinstance(exc, SteeringUnknown) and self.delivery_state != "failed":
                self._events.put({"type": "control_delivery", "state": "unknown"})
                self._events.put({"type": "control_warning", "message": str(exc)})
                return
            self._delivery("failed")
            self._events.put({"type": "control_delivery", "state": self.delivery_state})
            self.terminal_error = str(exc)
            self._events.put({"type": "error", "message": str(exc)})
            self._turn_result = 1
            self._finished.set()
            self._clear_subagent_marker()
            self._events.put(None)

    def can_steer(self):
        return (
            self._ready.is_set()
            and bool(self.turn_id)
            and not self._closed.is_set()
            and not self.questions.waiting()
        )

    def steer(self, message_id, text, images=()):
        with self._lock:
            if message_id in self._steered:
                return True
            if not self.can_steer():
                raise SteeringRejected("当前执行轮已结束")
            turn_id = self.turn_id
        result = self._rpc(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": turn_id,
                "clientUserMessageId": message_id,
                "input": self._input(text, images),
            },
        )
        if result.get("turnId") != turn_id:
            raise SteeringUnknown("追加消息确认与执行轮不匹配，未自动重发")
        with self._lock:
            self._steered.add(message_id)
        return True

    def _read(self):
        try:
            for raw in self._lines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if "id" in event and "method" not in event:
                    with self._lock:
                        waiter = self._pending.get(event["id"])
                        if event["id"] == self._turn_request_id:
                            self._delivery("failed" if "error" in event else "delivered")
                    if waiter:
                        try:
                            waiter.put_nowait(event)
                        except queue.Full:
                            pass
                    continue
                method, params = event.get("method"), event.get("params") or {}
                if "id" in event:
                    if method == "item/tool/requestUserInput":
                        if any(q.get("isSecret") for q in params.get("questions", [])):
                            self._write(
                                {
                                    "id": event["id"],
                                    "error": {
                                        "code": -32602,
                                        "message": "Secret input is not supported by this client",
                                    },
                                }
                            )
                        else:
                            self.questions.add(
                                event["id"],
                                {
                                    **params,
                                    "questions": [
                                        {
                                            **q,
                                            "allowOther": bool(
                                                q.get("isOther") or not q.get("options")
                                            ),
                                        }
                                        for q in params.get("questions", [])
                                    ],
                                },
                            )
                    else:
                        self._write(
                            {
                                "id": event["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "Client does not support this server request",
                                },
                            }
                        )
                    continue
                self._notification(method, params)
        finally:
            self._maybe_clear_subagent_marker()
            self._flush_redaction()
            self.questions.close()
            if self._turn_result is None:
                self.terminal_error = self.terminal_error or "执行连接意外关闭，任务结果未确认"
            self._closed.set()
            if self._turn_result is None:
                self._turn_result = 1
            self._finished.set()
            with self._lock:
                for waiter in self._pending.values():
                    try:
                        waiter.put_nowait({"transport_closed": True})
                    except queue.Full:
                        pass
            self._events.put(None)

    def _notification(self, method, p):
        if method == "serverRequest/resolved":
            self.questions.expire_request(p.get("requestId"))
        elif method == "turn/started":
            self._delivery("delivered")
            with self._lock:
                self.turn_id = p["turn"]["id"]
                self._ready.set()
            self._events.put({"type": "control_delivery", "state": "delivered"})
        elif method == "item/agentMessage/delta":
            if not getattr(self, "_emit_cumulative", True):
                return
            item_id = p["itemId"]
            chunk = self._redact_stream_chunk(p.get("delta") or "")
            self._message_text[item_id] = self._message_text.get(item_id, "") + chunk
            self._events.put(
                {
                    "type": "item.completed",
                    "delta": True,
                    "item": {
                        "id": item_id,
                        "type": "agent_message",
                        "text": self._message_text[item_id],
                    },
                }
            )
        elif method in ("item/started", "item/completed"):
            item = dict(p.get("item") or {})
            kind = item.get("type")
            names = {
                "userMessage": "user_message",
                "agentMessage": "agent_message",
                "commandExecution": "command_execution",
                "mcpToolCall": "mcp_tool_call",
                "dynamicToolCall": "tool_call",
                "fileChange": "file_change",
            }
            item["type"] = names.get(kind, kind)
            if (
                kind == "agentMessage"
                and isinstance(item.get("text"), str)
                and method == "item/completed"
            ):
                self._redact_buf = ""
            if "aggregatedOutput" in item:
                item["aggregated_output"] = item["aggregatedOutput"]
            if "exitCode" in item:
                item["exit_code"] = item["exitCode"]
            self._events.put(
                {
                    "type": "item.completed" if method.endswith("completed") else "item.started",
                    "item": item,
                }
            )
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            chunk = self._redact_stream_chunk(p.get("delta") or "")
            if chunk:
                self._events.put(
                    {"type": "item.updated", "item": {"type": "reasoning", "text": chunk}}
                )
        elif method == "turn/completed":
            self.questions.close()
            turn = p.get("turn") or {}
            with self._lock:
                self.turn_id = ""
                self._turn_result = 0 if turn.get("status") == "completed" else 1
            if self._turn_result != 0:
                self.terminal_error = str(
                    turn.get("error") or "执行未正常完成：" + str(turn.get("status"))
                )
                self._events.put({"type": "error", "message": self.terminal_error})
            self._flush_redaction()
            self._protocol_done.set()
            self._finished.set()
            self._clear_subagent_marker()
            completed_evt = {"type": "turn.completed"}
            if isinstance(turn.get("usage"), dict):
                completed_evt["usage"] = turn["usage"]
            self._events.put(completed_evt)
            self._events.put(None)
        elif method == "error":
            self._events.put({"type": "error", "message": str(p.get("error") or p)})
