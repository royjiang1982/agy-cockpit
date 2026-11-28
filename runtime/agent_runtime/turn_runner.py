"""Single-turn session claim, quota preflight, event forwarding and ledger writes."""
import json
import os
from pathlib import Path
import re
import select
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .acp_stream import (
    AgyAcpStream,
    DroidAcpStream,
    GrokAcpStream,
    agy_server_command,
    droid_server_command,
    grok_server_command,
)
from .codex_stream import CodexStream, codex_server_command
from .pi_stream import PiRpcStream, pi_server_command
from .agy_quota import invalidate_agy_quota_cache, preflight_agy_request
from .cli_events import should_emit
from .daemon_config import build_clean_subagent_env
from .handoff_text import _unwrap_handoff_prompt
from .proc_utils import find_cli_binary, terminate_proc_group
from .session_handoff import (
    extract_event_tokens, prepare_session_handoff, record_turn_completion, seed_handoff_ledger,
)


def build_server_cmd_and_stream_cls(
    agent: str, config_list: Optional[List[str]] = None
) -> Tuple[List[str], Any]:
    agent = agent.strip().lower()
    binary = find_cli_binary(agent)
    if not binary:
        raise RuntimeError(f"找不到官方 {agent} CLI 可执行文件")
    if agent == "agy":
        return agy_server_command([binary]), AgyAcpStream
    if agent == "codex":
        raw_cmd = [binary, "exec", "--dangerously-bypass-hook-trust"]
        for c in config_list or []:
            raw_cmd.extend(["-c", c])
        raw_cmd.append("prompt")
        return codex_server_command(raw_cmd), CodexStream
    if agent == "droid":
        return droid_server_command([binary]), DroidAcpStream
    if agent == "grok":
        return grok_server_command([binary]), GrokAcpStream
    if agent == "pi":
        return pi_server_command([binary]), PiRpcStream
    raise ValueError(f"不支持的子代理引擎：{agent}")


def run_turn(daemon, conn: socket.socket, req: Dict[str, Any]) -> None:
    agent = str(req.get("agent") or "agy").strip().lower()
    model = str(req.get("model") or "").strip()
    effort = str(req.get("effort") or "").strip()
    service_tier = str(req.get("service_tier") or "").strip().lower()
    mode = str(req.get("mode") or "").strip()
    session_id = str(req.get("session_id") or "").strip()
    cwd = str(req.get("cwd") or os.getcwd()).strip()
    prompt = str(req.get("prompt") or "")
    config_list = req.get("config") or []
    image_paths = req.get("image_paths") or []
    preflight_event, preflight_handoff = None, None

    requested_sid = session_id
    discovered_sid = session_id
    claimed_sid = False

    with daemon.lock:
        daemon.active_runs += 1
        daemon.last_activity_mono = time.monotonic()

    proc = None
    host = None
    inbox = None
    client_eof = threading.Event()
    turn_done = threading.Event()

    def watch_client_disconnect() -> None:
        while not turn_done.is_set() and not client_eof.is_set():
            try:
                rlist, _, _ = select.select([conn], [], [], 0.2)
                if rlist:
                    data = conn.recv(1, socket.MSG_PEEK)
                    if not data:
                        client_eof.set()
                        if proc is not None:
                            terminate_proc_group(proc, wait_timeout=0.5)
                        break
            except Exception:
                client_eof.set()
                if proc is not None:
                    terminate_proc_group(proc, wait_timeout=0.5)
                break

    watcher = threading.Thread(target=watch_client_disconnect, daemon=True)
    watcher.start()

    def send_frame(obj: Dict[str, Any]) -> bool:
        if client_eof.is_set():
            return False
        if obj.get("type") == "event" and not should_emit(obj.get("event"), req.get("emit_deltas")):
            return True
        try:
            conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            return True
        except Exception:
            client_eof.set()
            return False

    pending_result: Optional[Dict[str, Any]] = None
    try:
        if requested_sid:
            claimed_sid = daemon.claim_session_id(requested_sid)
        if agent == "agy":
            pf = preflight_agy_request(agent=agent, model=model, effort=effort, mode=mode, requested_sid=requested_sid,
                                       cwd=cwd, prompt=prompt, state_dir=daemon.socket_path.parent)
            if pf["should_fallback"]:
                agent, model, effort, mode, prompt = (pf[k] for k in ("agent", "model", "effort", "mode", "prompt"))
                session_id, discovered_sid, preflight_event, preflight_handoff = "", "", pf["event"], pf["handoff"]
        if preflight_event:
            send_frame({"type": "event", "event": preflight_event})
        os.makedirs(cwd, exist_ok=True)
        env = build_clean_subagent_env()
        env["PATH"] = str(Path(__file__).resolve().parents[1] / "bin") + os.pathsep + env.get("PATH", "")
        server_cmd, stream_cls = build_server_cmd_and_stream_cls(agent, config_list)
        raw_prompt, d_dir = prompt, daemon.socket_path.parent
        if preflight_handoff:
            _, raw_prompt = _unwrap_handoff_prompt(prompt)
        handoff = preflight_handoff or prepare_session_handoff(agent, session_id, raw_prompt, d_dir)
        if handoff and not preflight_handoff:
            session_id, prompt = "", handoff["effective_prompt"]
            send_frame({"type": "event", "event": handoff["event"]})
        host, inbox = daemon._acquire_resident(agent, cwd, server_cmd, session_id, env)
        bot_cfg: Dict[str, Any] = {
            "model": model,
            "session_id": session_id,
            "reasoning_effort": effort,
        }
        if service_tier:
            bot_cfg["service_tier"] = service_tier
        if mode:
            bot_cfg["mode"] = mode

        stream_options = {}
        if agent == "codex":
            stream_options["emit_cumulative"] = bool(
                req.get("emit_deltas", os.environ.get("SA_CODEX_EMIT_DELTAS") == "1")
            )
        proc = stream_cls(
            server_cmd,
            cwd=cwd,
            env=env,
            bot=bot_cfg,
            prompt=prompt,
            image_paths=image_paths,
            resident=host,
            inbox=inbox,
            is_subagent=True,
            **stream_options,
        )
        init_sid = (
            getattr(proc, "session_file", "")
            or getattr(proc, "session_id", "")
            or getattr(proc, "thread_id", "")
        )
        handoff_seeded = False
        if handoff and init_sid:
            seed_handoff_ledger(agent, init_sid, handoff, d_dir)
            handoff_seeded = True
        if client_eof.is_set():
            terminate_proc_group(proc, wait_timeout=0.5)
            return

        chunks: List[str] = []
        codex_messages: Dict[str, str] = {}
        discovered_sid = init_sid or discovered_sid
        stream_tokens = 0

        for event in proc.iter_events():
            if client_eof.is_set():
                break
            stream_tokens = extract_event_tokens(event) or stream_tokens
            if event.get("event") == "init" and event.get("conversation_id"):
                discovered_sid = str(event["conversation_id"])
                if handoff and discovered_sid and not handoff_seeded:
                    seed_handoff_ledger(agent, discovered_sid, handoff, d_dir)
                    handoff_seeded = True
            elif event.get("type") == "thread.started" and event.get("thread_id"):
                discovered_sid = str(event["thread_id"])
                if handoff and discovered_sid and not handoff_seeded:
                    seed_handoff_ledger(agent, discovered_sid, handoff, d_dir)
                    handoff_seeded = True
            elif event.get("type") == "text_delta":
                chunks.append(event.get("delta") or "")
            elif (
                event.get("type") == "item.completed"
                and isinstance(event.get("item"), dict)
                and event["item"].get("type") == "agent_message"
            ):
                text_val = event["item"].get("text") or ""
                item_id = str(event["item"].get("id") or f"msg_{len(codex_messages)}")
                if event.get("delta") or text_val:
                    codex_messages[item_id] = text_val
            if not send_frame({"type": "event", "event": event}):
                break
            if event.get("type") == "native_question":
                raise RuntimeError("子代理触发了交互式提问（禁止子代理向用户提问），已中止该回合")

        final_sid = (
            getattr(proc, "session_file", "")
            or getattr(proc, "session_id", "")
            or getattr(proc, "thread_id", "")
            or discovered_sid
        )
        response_text = (
            "".join(codex_messages.values()) if codex_messages else "".join(chunks)
        )
        pending_result = {
            "type": "result", "agent": agent,
            "conversation_id": final_sid,
            "result": {"status": "SUCCESS", "response": response_text},
        }
        if not client_eof.is_set():
            try:
                record_turn_completion(agent, final_sid, raw_prompt, response_text, stream_tokens, handoff, d_dir)
            except Exception as ledger_exc:
                warning = str(ledger_exc)
                pending_result["result"]["ledger_warning"] = warning
                try:
                    print(f"session ledger write failed: {warning}", file=sys.stderr)
                except (OSError, ValueError):
                    pass  # Logging must not override the completed turn.
    except Exception as exc:
        if agent == "agy" and re.search(r"quota|exhausted|rate[ _-]?limit|\b429\b|配额|额度", str(exc), re.I):
            invalidate_agy_quota_cache()
        if isinstance(exc, RuntimeError) and "cross-engine handoff" in str(exc):
            send_frame({"type": "error", "error": str(exc)})
        final_sid = discovered_sid or ""
        pending_result = {
            "type": "result", "agent": agent,
            "conversation_id": final_sid,
            "result": {"status": "ERROR", "error": str(exc)},
        }
    finally:
        turn_done.set()
        if proc is not None:
            terminate_proc_group(proc, wait_timeout=0.5)
        elif host is not None and inbox is not None:
            try:
                host.release(inbox, healthy=False)
            except Exception:
                pass
        with daemon.lock:
            if claimed_sid:
                daemon.release_session_id(requested_sid)
            daemon.active_runs = max(0, daemon.active_runs - 1)
            daemon.last_activity_mono = time.monotonic()
        if pending_result is not None:
            send_frame(pending_result)
