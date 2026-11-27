"""Silent delayed auto-approval and mode resolution for normal-mode AGY ACP."""
import random
import threading
import time

VALID_KINDS = ("allow_once", "allow_always", "reject_once", "reject_always")
CANCELLED = {"outcome": {"outcome": "cancelled"}}
AUTO_APPROVE_DELAY_RANGE = (1.0, 5.0)
_ALLOWED_MODES = ("auto", "accept-edits", "default", "plan")


def valid_options(options):
    return [
        o
        for o in (options or ())
        if isinstance(o, dict)
        and isinstance(o.get("optionId"), str)
        and o["optionId"]
        and isinstance(o.get("kind"), str)
        and o["kind"] in VALID_KINDS
    ]


def pick_auto_allow_option(options):
    valid = valid_options(options)
    if not valid or len({o["optionId"] for o in valid}) != len(valid):
        return None
    return next((o for o in valid if o["kind"] == "allow_once"), None)


def _available_modes(session):
    modes = set()
    for entry in (session.get("modes") or {}).get("availableModes") or ():
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
            modes.add(entry["id"])
    for cfg in session.get("configOptions") or ():
        if isinstance(cfg, dict) and cfg.get("id") == "mode":
            for opt in cfg.get("options") or ():
                if isinstance(opt, dict) and isinstance(opt.get("value"), str) and opt["value"]:
                    modes.add(opt["value"])
    return modes


def resolve_agy_mode(bot, session):
    raw = str((bot or {}).get("mode") or "").strip().lower()
    available = _available_modes(session or {})
    if raw in ("", "auto", "turbo"):
        if not available or "auto" in available:
            return "auto"
        return "accept-edits" if "accept-edits" in available else "default"
    if raw in _ALLOWED_MODES:
        return raw
    return "auto" if (not available or "auto" in available) else "default"


class AcpAutoApprover:
    """Handle AGY ACP permission requests with cancellable random delay."""

    def __init__(self, write, is_inactive, on_error, *, delay_range=None):
        self._write = write
        self._is_inactive = is_inactive
        self._on_error = on_error
        self._delay_range = delay_range
        self._lock = threading.Lock()
        self._records = {}
        self._closed = False

    def waiting(self):
        with self._lock:
            return any(r["state"] in ("pending", "sending") for r in self._records.values())

    def _safe_cancel_write(self, rid):
        try:
            self._write({"id": rid, "result": CANCELLED})
        except Exception:
            pass

    def handle_permission(self, event, *, session_id, prompt_sent):
        rid = event["id"]
        params = event.get("params") or {}
        tool = params.get("toolCall")
        options = params.get("options") or []

        if session_id and params.get("sessionId") != session_id:
            self._safe_cancel_write(rid)
            if not params.get("sessionId"):
                return "AGY ACP 权限请求缺少会话 ID"
            return None

        if self._closed or self._is_inactive():
            self._safe_cancel_write(rid)
            return None

        if not prompt_sent or not isinstance(tool, dict):
            self._safe_cancel_write(rid)
            return "AGY ACP 权限请求不属于当前执行轮"

        option = pick_auto_allow_option(options)
        if option is None:
            self._safe_cancel_write(rid)
            return "AGY ACP 未提供可自动批准的权限选项"

        with self._lock:
            if self._closed or rid in self._records:
                return None
            record = {
                "rpc_id": rid,
                "option_id": option["optionId"],
                "state": "pending",
                "done": threading.Event(),
            }
            self._records[rid] = record

        try:
            threading.Thread(
                target=self._approve_after_delay,
                args=(record,),
                name=f"agy-acp-approve-{rid}",
                daemon=True,
            ).start()
        except RuntimeError as exc:
            with self._lock:
                record["state"] = "error"
                record["done"].set()
            self._safe_cancel_write(rid)
            return f"AGY ACP 自动批准线程启动失败：{exc}"
        return None

    def _approve_after_delay(self, record):
        lo, hi = self._delay_range if self._delay_range is not None else AUTO_APPROVE_DELAY_RANGE
        delay = random.uniform(lo, hi) if hi > lo else max(0.0, float(lo))
        deadline = time.monotonic() + delay
        while delay > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._is_inactive() or record["done"].is_set():
                return
            record["done"].wait(min(0.1, remaining))

        with self._lock:
            if self._closed or self._is_inactive() or record["state"] != "pending":
                return
            record["state"] = "sending"

        try:
            self._write(
                {
                    "id": record["rpc_id"],
                    "result": {
                        "outcome": {"outcome": "selected", "optionId": record["option_id"]}
                    },
                }
            )
        except Exception as exc:
            with self._lock:
                record["state"] = "error"
                record["done"].set()
            if not self._is_inactive():
                self._on_error(f"AGY ACP 自动批准写入失败：{exc}")
            return

        with self._lock:
            record["state"] = "approved"
            record["done"].set()

    def cancel_all(self, *, send_cancel=True):
        with self._lock:
            self._closed = True
            pending = []
            for record in self._records.values():
                if record["state"] == "pending":
                    record["state"] = "cancelled"
                    record["done"].set()
                    pending.append(record)
        if send_cancel:
            for record in pending:
                self._safe_cancel_write(record["rpc_id"])
