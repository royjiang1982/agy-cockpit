"""Process and JSONL transport shared by resident and single-shot turns."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading

from .native_questions import NativeQuestions


class SteeringRejected(RuntimeError):
    """The engine explicitly rejected the request; it is safe to defer it."""


class SteeringUnknown(RuntimeError):
    """The connection failed after writing; do not replay an uncertain request."""


class ResidentTurn:
    """One turn; closing a leased turn releases its inbox, not the shared process."""

    _markers_lock = threading.Lock()
    _marker_counts = {}
    WRITE_ERROR = RuntimeError

    def __init__(
        self, cmd, *, cwd, env, bot, popen, rpc_timeout, resident, inbox,
        is_subagent, tool_token, client_name="agent-runtime", standalone_command=None,
    ):
        self.client_name = str(
            getattr(resident, "client_name", "") or client_name or "agent-runtime"
        )
        self._inbox = inbox if resident else None
        if resident and self._inbox is None:
            self._inbox = resident.acquire(str(bot.get("session_id") or ""))
            if self._inbox is None:
                resident = None
        self.resident = resident
        self.is_resident_turn = resident is not None
        self.process = resident.process if resident else popen(
            standalone_command(cmd) if standalone_command else cmd,
            cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            bufsize=1, start_new_session=True,
        )
        self.pid = self.process.pid
        self.stdout = self
        self.rpc_timeout = rpc_timeout
        self.is_subagent = is_subagent
        self.tool_token = tool_token
        self.delivery_state = "failed"
        self.terminal_error = ""
        self._result = None
        self._pending = {}
        self._next_id = 0
        self._lock = threading.RLock()
        self._write_lock = resident.write_lock if resident else threading.Lock()
        self._events = queue.Queue()
        self.questions = NativeQuestions(self.agent, self._write, self._events.put)
        self._closed = threading.Event()
        self._finished = threading.Event()
        self._protocol_done = threading.Event()
        self._ready = threading.Event()
        self._steered = set()
        self._prompt_sent = False
        self._released = False
        self._close_lock = threading.RLock()
        self._marker_lock = threading.Lock()
        self._marker = None
        self._retain_marker = False
        self._redact_lock = threading.RLock()
        self._redact_buf = ""

    def _start_workers(self, *args):
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._starter = threading.Thread(target=self._start, args=args, daemon=True)
        self._reader.start()
        self._starter.start()

    @property
    def returncode(self):
        return self._result if self._result is not None else self.process.returncode

    def poll(self):
        return self.returncode if self.is_resident_turn else self.process.poll()

    def wait(self, timeout=None):
        if self.is_resident_turn:
            if not self._finished.wait(timeout):
                raise subprocess.TimeoutExpired("resident turn", timeout)
        else:
            self.process.wait(timeout=timeout)
        return self.returncode

    def _stop(self, method):
        if not self.is_resident_turn:
            return getattr(self.process, method)()
        try:
            self.cancel()
        except Exception:
            pass
        self.close()

    def terminate(self):
        self._stop("terminate")

    def kill(self):
        self._stop("kill")

    def _close_protocol(self):
        """Hook to dismiss engine-specific controls before closing questions."""

    def close(self):
        self._close_protocol()
        if self.is_resident_turn:
            return self._resident_close()
        self.questions.close()
        self._clear_subagent_marker()
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass

    def _redact(self, text):
        return text.replace(self.tool_token, "<本轮授权>") if self.tool_token else text

    def __iter__(self):
        for event in self.iter_events():
            yield json.dumps(event, ensure_ascii=False) + "\n"

    def iter_events(self):
        """Consume the same redacted event queue as JSONL iteration, as dictionaries."""
        while True:
            event = self._events.get()
            if event is None:
                if self.terminal_error:
                    raise RuntimeError(self._redact(self.terminal_error))
                return
            if self.tool_token:
                event = json.loads(self._redact(json.dumps(event, ensure_ascii=False)))
            yield event

    def _incoming_events(self):
        for raw in self._lines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event

    def _write(self, message):
        lock = self._write_lock
        if not lock.acquire(timeout=self.rpc_timeout):
            raise self.WRITE_ERROR(self.WRITE_BUSY)
        done = queue.Queue(maxsize=1)

        def write():
            try:
                if self._closed.is_set():
                    raise self.WRITE_ERROR(self.WRITE_CLOSED)
                payload = self._prepare_write(message)
                self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                done.put(None)
            except Exception as exc:
                done.put(self._write_failure(exc))
            finally:
                lock.release()

        try:
            threading.Thread(target=write, daemon=True).start()
        except RuntimeError:
            lock.release()
            raise
        try:
            error = done.get(timeout=self.rpc_timeout)
        except queue.Empty:
            raise self.WRITE_ERROR(self.WRITE_TIMEOUT) from None
        if error:
            raise error

    def _write_failure(self, exc):
        return exc

    def _rpc(self, method, params=None, *, prompt=False):
        result = queue.Queue(maxsize=1)
        with self._lock:
            rid = self._request_id(method, prompt)
            self._pending[rid] = result
        try:
            self._write(self._request_message(rid, method, params))
            try:
                reply = result.get(timeout=None if prompt else self.rpc_timeout)
            except queue.Empty:
                raise self._request_timeout(method) from None
            return self._request_result(method, reply)
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    def _request_id(self, method, prompt):
        return self._allocate_id()

    def _request_message(self, rid, method, params):
        return {"id": rid, "method": method, "params": params}

    @staticmethod
    def _deliver(waiter, event):
        if waiter is not None:
            try:
                waiter.put_nowait(event)
            except queue.Full:
                pass

    def _fail_pending(self, reply):
        with self._lock:
            for waiter in self._pending.values():
                self._deliver(waiter, reply)

    def _finish(self, error=""):
        with self._lock:
            if self._finished.is_set():
                return
            self.terminal_error = error
            self._result = 1 if error else 0
            self._flush_redaction()
            self._finished.set()
            self._close_protocol()
            self._maybe_clear_subagent_marker()
            self.questions.close()
            self._events.put({"type": "control_delivery", "state": self.delivery_state})
            if not error:
                self._events.put({"type": "turn.completed"})
            self._events.put(None)

    def _allocate_id(self):
        if self.is_resident_turn:
            return self.resident.allocate_id(self._inbox)
        self._next_id += 1
        return self._next_id

    def _lines(self):
        if not self.is_resident_turn:
            yield from self.process.stdout
            return
        while True:
            raw = self._inbox.get()
            if raw is None:
                return
            yield raw

    def _resident_close(self):
        with self._close_lock:
            if self._released:
                return
            self._released = True
            self.questions.close()
            healthy = self._protocol_done.is_set() and not self._closed.is_set()
            if healthy:
                self._starter.join(timeout=0.4)
                if self._starter.is_alive():
                    healthy = False
            if not healthy and not self.resident.closed:
                self._retain_marker = True
                with self.resident.lock:
                    self.resident.retire_callbacks.append(self._clear_subagent_marker)
            self._closed.set()
            self._inbox.put(None)
            self._reader.join(timeout=0.4)
            self._maybe_clear_subagent_marker()
            self.resident.release(self._inbox, healthy=healthy)

    def _mark_subagent(self, sid):
        if not self.is_subagent or not self.is_resident_turn:
            return
        if not sid or Path(sid).name != sid or sid in (".", ".."):
            raise RuntimeError("子代理会话 ID 无效")
        directory = Path(
            os.environ.get("ZOMI_SUBAGENT_SESSIONS_DIR") or "/tmp/zomi-subagent-sessions"
        )
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        with self._marker_lock:
            if self._finished.is_set() or self._released:
                return
            self._marker = directory / sid
            with self._markers_lock:
                self._marker.touch(exist_ok=True)
                self._marker_counts[self._marker] = (
                    self._marker_counts.get(self._marker, 0) + 1
                )

    def _maybe_clear_subagent_marker(self):
        if (
            not getattr(self, "_retain_marker", False)
            or self._protocol_done.is_set()
            or not self.is_resident_turn
            or self.resident.closed
        ):
            self._clear_subagent_marker()

    def _redact_stream_chunk(self, text, *, final=False):
        token = self.tool_token
        if not token:
            return text
        with self._redact_lock:
            value = (self._redact_buf + text).replace(token, "<本轮授权>")
            self._redact_buf = ""
            if not final:
                for size in range(min(len(token) - 1, len(value)), 0, -1):
                    if value.endswith(token[:size]):
                        self._redact_buf = value[-size:]
                        return value[:-size]
            return value

    def _flush_redaction(self):
        tail = self._redact_stream_chunk("", final=True)
        if tail:
            self._events.put({"type": "text_delta", "delta": tail})

    def _clear_subagent_marker(self):
        with self._marker_lock:
            marker, self._marker = self._marker, None
            if marker is not None:
                with self._markers_lock:
                    count = self._marker_counts.get(marker, 1) - 1
                    if count > 0:
                        self._marker_counts[marker] = count
                    else:
                        self._marker_counts.pop(marker, None)
                        marker.unlink(missing_ok=True)
