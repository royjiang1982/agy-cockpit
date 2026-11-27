"""Shared stdlib fixtures for black-box runtime protocol and daemon tests."""
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import sys
from textwrap import dedent
import threading
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1]
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

TIMEOUT = 10  # A deadlock guard, not a performance assertion.


@contextmanager
def running_daemon(instance):
    """Wait for the real accept loop, not merely creation of the socket file."""
    import atexit

    ready = threading.Event()
    failures = []
    accept = socket.socket.accept

    def accept_when_ready(sock):
        if sock is instance.server_sock:
            ready.set()
        return accept(sock)

    def serve():
        try:
            instance.serve_forever()
        except Exception as exc:
            failures.append(exc)
        finally:
            ready.set()

    worker = threading.Thread(target=serve, daemon=True)
    try:
        with patch.object(socket.socket, "accept", accept_when_ready):
            worker.start()
            assert ready.wait(TIMEOUT), f"Daemon not ready: {instance.socket_path}"
        if failures:
            raise failures[0]
        assert worker.is_alive(), f"Daemon exited before listening: {instance.socket_path}"
        yield instance
    finally:
        instance.shutdown_all()
        worker.join(TIMEOUT)
        atexit.unregister(instance.shutdown_all)
        assert not worker.is_alive(), f"Daemon still running: {instance.socket_path}"
        if failures:
            raise failures[0]


@contextmanager
def daemon_request(path, **request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(TIMEOUT)
        conn.connect(str(path))
        conn.sendall((json.dumps(request) + "\n").encode())
        with conn.makefile("r", encoding="utf-8") as stream:
            yield map(json.loads, stream)


@contextmanager
def signal_socket(path):
    """A fake child can acknowledge a wire event and wait for EOF as release."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(TIMEOUT)
        try:
            yield listener
        finally:
            path.unlink()


RPC_COMMON = '''
import json, socket, sys, threading, time

write_lock = threading.Lock()
def send(message):
    with write_lock:
        print(json.dumps(message), flush=True)

def child_signal(path, value=b"", *, hold=False):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(path)
        if value:
            conn.sendall(value)
        if hold:
            assert conn.recv(1) == b""
'''

ACP = '''
seq = 0
capabilities = {"loadSession": True, "promptCapabilities": {"image": True}}
session_info = {
    "models": {"availableModels": [
        {"modelId": model} for model in ("gemini-4-carbon-medium", "gpt-6.2-sol", "grok-4.7")
    ]},
    "configOptions": [
        {"id": "reasoning_effort", "options": [{"value": x} for x in ("medium", "high", "xhigh")]}
    ],
}
sid_prefix = "sid"
reply_prefix = "reply:"
startup = False

def reply(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})

def error(rid, code, message):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})

def update(sid, text, kind="agent_message_chunk"):
    notify("session/update", {"sessionId": sid, "update": {
        "sessionUpdate": kind, "content": {"type": "text", "text": text},
    }})

def handle_open(rid, params):
    global seq
    seq += 1
    reply(rid, {"sessionId": params.get("sessionId") or f"{sid_prefix}-{seq}", **session_info})

def handle_prompt(rid, sid, text):
    if "WAIT_FOR_CANCEL" in text:
        update(sid, "working...", "agent_thought_chunk")
    else:
        update(sid, reply_prefix + sid)
        reply(rid, {"stopReason": "end_turn"})

def handle_cancel(params):
    child_signal(sys.argv[1], params["sessionId"].encode())
    reply(999, {"stopReason": "cancelled"})

def override(method, rid, params, message):
    return False

def handle(message):
    method, rid, params = message.get("method"), message.get("id"), message.get("params") or {}
    if override(method, rid, params, message):
        return
    if method == "initialize":
        reply(rid, {"protocolVersion": 1, "agentCapabilities": capabilities})
    elif method in ("session/new", "session/load", "session/resume"):
        handle_open(rid, params)
    elif method == "session/prompt":
        handle_prompt(rid, params["sessionId"], params["prompt"][0]["text"])
    elif method == "session/cancel" and len(sys.argv) > 1:
        handle_cancel(params)
    elif method in ("session/set_mode", "session/set_model", "session/set_config_option"):
        reply(rid, {})

def boot():
    if startup:
        notify("_startup/notice", {"info": "booting"})
'''

PI = '''
sid = "pi-sid-1"
prompt_error = ""

def handle(message):
    global sid
    rid, command = message.get("id"), message.get("type")

    def respond(data):
        send({"id": rid, "type": "response", "command": command, "success": True, "data": data})

    if command == "get_state":
        respond({"sessionId": sid, "sessionFile": f"/tmp/{sid}.jsonl"})
    elif command in ("new_session", "switch_session", "set_thinking_level", "set_model"):
        if command == "new_session" and not prompt_error:
            sid = "pi-sid-2"
        respond({"cancelled": False})
    elif command == "get_available_models":
        respond({"models": [{"provider": "anthropic", "id": "claude-opus-5-5", "name": "Claude Opus 5.5"}]})
    elif command == "prompt":
        respond({"disposition": "started"})
        if prompt_error:
            send({"type": "message_end", "message": {"stopReason": "error", "errorMessage": prompt_error}})
        else:
            send({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "pi-ok"}})
        send({"type": "agent_settled"})

def boot():
    pass
'''

RPC_LOOP = '''
boot()
for line in sys.stdin:
    if line.strip():
        if handle(json.loads(line)) is False:
            break
'''


def write_rpc_server(root, name, *, protocol="acp", setup=""):
    """Build a fake child; setup can change data or override a protocol handler."""
    defaults = {"acp": ACP, "pi": PI, "custom": "def boot(): pass\n"}[protocol]
    path = root / name
    path.write_text(
        "#!/usr/bin/env python3\n" + RPC_COMMON + defaults + "\n" + dedent(setup) + "\n" + RPC_LOOP,
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path
