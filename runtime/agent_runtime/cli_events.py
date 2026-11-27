"""CLI-only emission policy; stream adapters and in-memory callbacks stay lossless."""
import json
import os


def should_emit(event, emit_deltas=None):
    if emit_deltas is None:
        emit_deltas = os.environ.get("SA_CODEX_EMIT_DELTAS") == "1"
    return bool(emit_deltas or not (isinstance(event, dict) and
                event.get("type") == "item.completed" and event.get("delta") is True and
                isinstance(event.get("item"), dict) and
                event["item"].get("type") in ("agent_message", "reasoning")))


def emit_event(event):
    if should_emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)


class CliOutput:
    """Filtered stdout and optional private local JSONL log, independent of transport."""
    def __init__(self, log_path=None):
        self.log = None
        if log_path:
            from pathlib import Path
            path = Path(log_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchmod(fd, 0o600)
            self.log = os.fdopen(fd, "a", encoding="utf-8")

    def __enter__(self):
        return self

    def emit(self, event):
        if should_emit(event):
            line = json.dumps(event, ensure_ascii=False)
            if self.log:
                self.log.write(line + "\n")
                self.log.flush()
            print(line, flush=True)

    def __exit__(self, *_):
        if self.log:
            self.log.close()
