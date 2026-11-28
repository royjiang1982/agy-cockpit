"""Pi CLI resident RPC mode stream transport (`pi --mode rpc`)."""
import base64
import json
import mimetypes
from pathlib import Path
import queue
import subprocess
import threading

from .native_questions import NativeQuestions
from .proc_utils import split_model_effort
from .resident import ResidentTurn, SteeringRejected, SteeringUnknown


PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


def pi_server_command(cmd):
    binary = cmd[0] if cmd else "pi"
    extra = []
    i = 1
    while i < len(cmd):
        arg = cmd[i]
        if arg in ("-p", "--print"):
            i += 1
            continue
        if arg == "--mode" and i + 1 < len(cmd):
            i += 2
            continue
        if arg in ("--session", "--model", "--thinking") and i + 1 < len(cmd):
            i += 2
            continue
        if arg in (
            "--no-context-files",
            "--no-skills",
            "--no-mcp",
            "--no-prompt-templates",
            "--no-themes",
        ):
            extra.append(arg)
            i += 1
            continue
        if arg in ("--system-prompt", "--append-system-prompt", "--tools", "-t", "-e", "--extension") and i + 1 < len(cmd):
            extra.extend([arg, cmd[i + 1]])
            i += 2
            continue
        i += 1
    return [binary, "--mode", "rpc", *extra]


class PiRpcStream(ResidentTurn):
    """Turn wrapper over a resident or single-shot `pi --mode rpc` subprocess."""

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
    ):
        self.agent = "pi"
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
        server_cmd = (
            pi_server_command(cmd)
            if (cmd and Path(cmd[0]).name.lower() == "pi" and "rpc" not in cmd)
            else cmd
        )
        self.process = (
            resident.process
            if resident
            else popen(
                server_cmd,
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
        self.rpc_timeout = rpc_timeout
        self.delivery_state = "failed"
        self.terminal_error = ""
        self.session_id = ""
        self.session_file = ""
        self._result = None
        self._pending = {}
        self._next_id = 0
        self._lock = threading.RLock()
        self._write_lock = resident.write_lock if resident else threading.Lock()
        self._events = queue.Queue()
        self.questions = NativeQuestions("pi", self._write, self._events.put)
        self._closed = threading.Event()
        self._finished = threading.Event()
        self._prompt_sent = False
        self._root_error = ""
        self._ready = threading.Event()
        self._steered = set()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._starter = threading.Thread(
            target=self._start, args=(bot, prompt, image_paths or (), cwd), daemon=True
        )
        self._starter.start()

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
        if self._prompt_sent and not self._finished.is_set():
            try:
                self._write({"type": "abort"})
            except Exception:
                pass

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
        self._rpc("steer", {"message": text})
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
            raise RuntimeError("Pi RPC 控制通道忙，发送结果未确认")
        done = queue.Queue(maxsize=1)

        def write():
            try:
                if self._closed.is_set():
                    raise RuntimeError("Pi RPC 连接已关闭，结果未确认")
                if message.get("type") == "prompt":
                    self.delivery_state = "unknown"
                    self._prompt_sent = True
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
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
            raise RuntimeError("Pi RPC 写入超时，发送结果未确认") from None
        if error:
            raise error

    def _rpc(self, cmd_type, params=None):
        result = queue.Queue(maxsize=1)
        with self._lock:
            rid = str(self._allocate_id())
            self._pending[rid] = result
        payload = {"id": rid, "type": cmd_type, **(params or {})}
        try:
            self._write(payload)
            try:
                reply = result.get(timeout=self.rpc_timeout)
            except queue.Empty:
                raise RuntimeError(f"Pi RPC {cmd_type} 确认超时") from None
            if not reply.get("success", False):
                raise RuntimeError(f"Pi RPC {cmd_type}: {reply.get('error', '请求失败')}")
            return reply.get("data") or {}
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

    def _resolve_and_set_model(self, bot):
        raw_model, effort = split_model_effort(
            bot.get("model"), bot.get("reasoning_effort"), default_effort="high", valid_efforts=PI_THINKING_LEVELS,
        )
        if raw_model:
            if "/" in raw_model:
                provider, model_id = raw_model.split("/", 1)
                self._rpc("set_model", {"provider": provider, "modelId": model_id})
            else:
                avail = self._rpc("get_available_models").get("models") or []
                match = next(
                    (
                        m
                        for m in avail
                        if isinstance(m, dict)
                        and (m.get("id") == raw_model or m.get("name") == raw_model)
                    ),
                    None,
                )
                if not match or not match.get("provider") or not match.get("id"):
                    raise RuntimeError(f"当前 Pi RPC 不可使用模型 {raw_model}")
                self._rpc(
                    "set_model",
                    {"provider": match["provider"], "modelId": match["id"]},
                )
        if effort in PI_THINKING_LEVELS:
            self._rpc("set_thinking_level", {"level": effort})
        return f"{raw_model or 'default'}:{effort}"

    def _start(self, bot, prompt, images, cwd):
        try:
            sid = str(bot.get("session_id") or "").strip()
            if sid:
                if not Path(sid).is_file():
                    raise RuntimeError(f"Pi 会话文件不存在：{sid}")
                sw = self._rpc("switch_session", {"sessionPath": sid})
                if sw.get("cancelled"):
                    raise RuntimeError("Pi 切换会话被拒绝或取消")
            else:
                ns = self._rpc("new_session")
                if ns.get("cancelled"):
                    raise RuntimeError("Pi 新建会话被拒绝或取消")
            state = self._rpc("get_state")
            self.session_id = str(state.get("sessionId") or "")
            self.session_file = str(state.get("sessionFile") or sid or "")
            marker_sid = Path(self.session_id or self.session_file).name
            if marker_sid:
                self._mark_subagent(marker_sid)
                if self.is_resident_turn:
                    self.resident.bind_session(self.session_file or marker_sid, self._inbox)
            model_display = self._resolve_and_set_model(bot)
            self._events.put(
                {
                    "event": "init",
                    "agent": "pi",
                    "conversation_id": self.session_file or self.session_id,
                    "session_id": self.session_id,
                    "session_file": self.session_file,
                    "init": {"model": model_display, "agent": "pi"},
                }
            )
            if self.is_subagent:
                prompt = (
                    "你是被派出的子代理（SA_SUBAGENT=1）。只遵守任务书，不派生子代理、不提交代码。\n\n"
                    + prompt
                )
            img_payload = []
            for path in images or ():
                mime = mimetypes.guess_type(path)[0] or ""
                if not mime.startswith("image/"):
                    raise RuntimeError("Pi RPC 附件不是支持的图片类型")
                img_payload.append(
                    {
                        "type": "image",
                        "mimeType": mime,
                        "data": base64.b64encode(Path(path).read_bytes()).decode("ascii"),
                    }
                )
            prompt_params = {"message": prompt}
            if img_payload:
                prompt_params["images"] = img_payload
            resp = self._rpc("prompt", prompt_params)
            self.delivery_state = "delivered"
            self._ready.set()
            if resp.get("disposition") == "handled":
                self._protocol_done.set()
                self._finish("")
        except Exception as exc:
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
                etype = event.get("type")
                if etype == "response" and "id" in event:
                    with self._lock:
                        waiter = self._pending.get(str(event["id"]))
                    if waiter:
                        waiter.put_nowait(event)
                    continue
                if not self._prompt_sent:
                    continue
                if etype == "message_update":
                    msg_evt = event.get("assistantMessageEvent") or {}
                    mtype = msg_evt.get("type")
                    if mtype == "text_delta":
                        chunk = self._redact_stream_chunk(msg_evt.get("delta") or "")
                        if chunk:
                            self._events.put({"type": "text_delta", "delta": chunk})
                    elif mtype == "thinking_delta":
                        chunk = self._redact_stream_chunk(msg_evt.get("delta") or "")
                        if chunk:
                            self._events.put({"type": "thinking", "text": chunk})
                    elif mtype == "error":
                        err_msg = str((msg_evt.get("error") or {}).get("errorMessage") or msg_evt)
                        self._root_error = err_msg
                        self._events.put({"type": "error", "message": err_msg})
                elif etype == "message_end":
                    msg = event.get("message") or {}
                    if isinstance(msg, dict) and (
                        msg.get("stopReason") in ("error", "aborted") or msg.get("errorMessage")
                    ):
                        err_msg = str(
                            msg.get("errorMessage") or f"Pi 执行中止：{msg.get('stopReason')}"
                        )
                        self._root_error = self._root_error or err_msg
                        self._events.put({"type": "error", "message": err_msg})
                elif etype == "extension_ui_request":
                    try:
                        self._write(
                            {
                                "type": "extension_ui_response",
                                "id": event.get("id"),
                                "cancelled": True,
                            }
                        )
                    except Exception:
                        pass
                    self._root_error = "Pi 触发了不受支持的交互式 UI 请求，已取消"
                    self._finish(self._root_error)
                elif etype == "tool_execution_start":
                    self._flush_redaction()
                    self._events.put(
                        {
                            "type": "tool_call",
                            "item": {
                                "id": event.get("toolCallId") or "tool",
                                "type": "tool",
                                "name": event.get("toolName") or "tool",
                                "arguments": event.get("args") or {},
                                "status": "in_progress",
                            },
                        }
                    )
                elif etype == "tool_execution_end":
                    self._flush_redaction()
                    is_err = bool(event.get("isError"))
                    res_obj = event.get("result") or {}
                    out_text = ""
                    if isinstance(res_obj, dict):
                        for blk in res_obj.get("content") or []:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                out_text += blk.get("text") or ""
                    self._events.put(
                        {
                            "type": "tool_call",
                            "item": {
                                "id": event.get("toolCallId") or "tool",
                                "type": "tool",
                                "name": event.get("toolName") or "tool",
                                "status": "failed" if is_err else "completed",
                                "output": out_text,
                            },
                        }
                    )
                elif etype == "agent_settled":
                    if event.get("aborted") and not self._root_error:
                        self._root_error = "Pi 执行已中止 (aborted)"
                    if not self._root_error:
                        self._protocol_done.set()
                    self._finish(self._root_error)
        except Exception as exc:
            self._finish("Pi RPC 读取失败：" + str(exc))
        finally:
            self._closed.set()
            with self._lock:
                for waiter in self._pending.values():
                    try:
                        waiter.put_nowait(
                            {"success": False, "error": "连接已关闭，任务结果未确认"}
                        )
                    except queue.Full:
                        pass
            self._finish("Pi RPC 连接意外关闭，任务结果未确认")
