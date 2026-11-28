"""Pi CLI resident RPC mode stream transport (`pi --mode rpc`)."""
import base64
import mimetypes
from pathlib import Path
import subprocess

from .handoff_text import SUBAGENT_PREFIX
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
        server_cmd = (
            pi_server_command(cmd)
            if (cmd and Path(cmd[0]).name.lower() == "pi" and "rpc" not in cmd)
            else cmd
        )
        super().__init__(
            server_cmd, cwd=cwd, env=env, bot=bot, popen=popen, rpc_timeout=rpc_timeout,
            resident=resident, inbox=inbox, is_subagent=is_subagent, tool_token=tool_token,
        )
        self.session_id = ""
        self.session_file = ""
        self._root_error = ""
        self._start_workers(bot, prompt, image_paths or (), cwd)

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

    WRITE_BUSY = "Pi RPC 控制通道忙，发送结果未确认"
    WRITE_CLOSED = "Pi RPC 连接已关闭，结果未确认"
    WRITE_TIMEOUT = "Pi RPC 写入超时，发送结果未确认"

    def _prepare_write(self, message):
        if message.get("type") == "prompt":
            self.delivery_state = "unknown"
            self._prompt_sent = True
        return message

    def _request_id(self, method, prompt):
        return str(self._allocate_id())

    def _request_message(self, rid, method, params):
        return {"id": rid, "type": method, **(params or {})}

    def _request_timeout(self, method):
        return RuntimeError(f"Pi RPC {method} 确认超时")

    def _request_result(self, method, reply):
        if not reply.get("success", False):
            raise RuntimeError(f"Pi RPC {method}: {reply.get('error', '请求失败')}")
        return reply.get("data") or {}

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
        display_model = raw_model or "default"
        if effort in PI_THINKING_LEVELS:
            self._rpc("set_thinking_level", {"level": effort})
            display_model = f"{display_model}:{effort}"
        return display_model

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
                prompt = SUBAGENT_PREFIX + "\n\n" + prompt
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
            for event in self._incoming_events():
                etype = event.get("type")
                if etype == "response" and "id" in event:
                    with self._lock:
                        waiter = self._pending.get(str(event["id"]))
                    self._deliver(waiter, event)
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
            self._fail_pending({"success": False, "error": "连接已关闭，任务结果未确认"})
            self._finish("Pi RPC 连接意外关闭，任务结果未确认")
