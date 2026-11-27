"""Unit and end-to-end tests for normal-mode AGY ACP delayed auto-approval."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime import acp_approvals
from agent_runtime.acp_approvals import (
    AUTO_APPROVE_DELAY_RANGE,
    CANCELLED,
    pick_auto_allow_option,
    resolve_agy_mode,
)
from agent_runtime.acp_stream import AgyAcpStream, DroidAcpStream, agy_server_command
from agent_runtime.proc_utils import DEFAULT_AGY_APP_DATA_DIR
from agent_runtime.resident import ResidentServer


def _write_fake_perm_server(tmp: Path) -> Path:
    script = tmp / "fake_agy_perm.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys, threading, time

log_file = sys.argv[1]
lock = threading.Lock()
pending_prompts = {}
pending_perms = {}
seq = 0
perm_seq = 1000
events_log = []


def send(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()


def record(entry):
    with lock:
        events_log.append(entry)
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(events_log, f)


for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    if "method" not in msg and "id" in msg:
        pid = msg["id"]
        info = pending_perms.pop(pid, None)
        if not info:
            continue
        elapsed = time.monotonic() - info["sent_at"]
        record({"type": "perm_reply", "perm_id": pid, "result": msg.get("result"), "elapsed": elapsed})
        sid = info["sid"]
        prompt_id = info["prompt_id"]
        outcome = ((msg.get("result") or {}).get("outcome") or {})
        if outcome.get("outcome") == "selected":
            remaining = info.get("remaining", 0)
            if remaining > 0:
                perm_seq += 1
                next_id = perm_seq
                pending_perms[next_id] = {
                    "sid": sid,
                    "prompt_id": prompt_id,
                    "sent_at": time.monotonic(),
                    "remaining": remaining - 1,
                }
                send({
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": sid,
                        "toolCall": {"toolCallId": f"tc-{next_id}", "name": "run_command", "title": "step2"},
                        "options": [
                            {"optionId": "always-id", "kind": "allow_always", "name": "Always allow"},
                            {"optionId": "reject-id", "kind": "reject_once", "name": "Reject"},
                        ],
                    },
                })
            else:
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": f"approved:{outcome.get('optionId')}"},
                        },
                    },
                })
                send({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "cancelled"}})
        continue

    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
    elif method == "session/new":
        seq += 1
        sid = f"sid-{seq}"
        send({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "sessionId": sid,
                "models": {"availableModels": [
                    {"modelId": "gemini-4-hydrogen-high"},
                    {"modelId": "gpt-6.2-astra"},
                ]},
                "modes": {
                    "currentModeId": "default",
                    "availableModes": [
                        {"id": "default", "name": "Default"},
                        {"id": "accept-edits", "name": "Accept edits"},
                        {"id": "auto", "name": "Auto"},
                        {"id": "plan", "name": "Plan"},
                    ],
                },
            },
        })
    elif method == "session/set_mode":
        record({"type": "set_mode", "modeId": params.get("modeId")})
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method in ("session/set_model", "session/set_config_option"):
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/cancel":
        record({"type": "session_cancel", "sessionId": params.get("sessionId")})
    elif method == "session/prompt":
        sid = params.get("sessionId", "")
        text = params["prompt"][0]["text"]
        perm_seq += 1
        pid = perm_seq
        if "INTERACTION_QUESTION" in text:
            send({
                "jsonrpc": "2.0",
                "id": pid,
                "method": "session/request_permission",
                "params": {
                    "sessionId": sid,
                    "toolCall": {"toolCallId": "interaction_1", "title": "Pick one"},
                    "options": [{"optionId": "opt_a", "name": "Option A"}],
                },
            })
            continue
        if "NO_ALLOW_OPTION" in text:
            pending_perms[pid] = {"sid": sid, "prompt_id": rid, "sent_at": time.monotonic(), "remaining": 0}
            send({
                "jsonrpc": "2.0",
                "id": pid,
                "method": "session/request_permission",
                "params": {
                    "sessionId": sid,
                    "toolCall": {"toolCallId": "tc-bad", "name": "run_command", "title": "bad"},
                    "options": [{"optionId": "reject-id", "kind": "reject_once", "name": "Reject"}],
                },
            })
            continue
        if "MULTI_PERM" in text:
            pending_perms[pid] = {"sid": sid, "prompt_id": rid, "sent_at": time.monotonic(), "remaining": 1}
        else:
            pending_perms[pid] = {"sid": sid, "prompt_id": rid, "sent_at": time.monotonic(), "remaining": 0}
        send({
            "jsonrpc": "2.0",
            "id": pid,
            "method": "session/request_permission",
            "params": {
                "sessionId": sid,
                "toolCall": {"toolCallId": f"tc-{pid}", "name": "run_command", "title": "echo ok"},
                "options": [
                    {"optionId": "allow-once-id", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "allow-always-id", "kind": "allow_always", "name": "Allow always"},
                    {"optionId": "reject-id", "kind": "reject_once", "name": "Reject"},
                ],
            },
        })
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def acp_approval_cases():
    # 1. Command builder never injects --dangerously-skip-permissions & default delay matches zomi-bot
    assert AUTO_APPROVE_DELAY_RANGE == (1.0, 5.0), AUTO_APPROVE_DELAY_RANGE
    prev_app_dir = os.environ.get("SA_AGY_APP_DATA_DIR")
    try:
        os.environ.pop("SA_AGY_APP_DATA_DIR", None)
        cmd = agy_server_command(["agy"])
        assert cmd == ["agy", "acp", "--app-data-dir", DEFAULT_AGY_APP_DATA_DIR], cmd
        assert "--dangerously-skip-permissions" not in cmd
        os.environ["SA_AGY_APP_DATA_DIR"] = ""
        assert agy_server_command(["agy"]) == ["agy", "acp"]
    finally:
        if prev_app_dir is None:
            os.environ.pop("SA_AGY_APP_DATA_DIR", None)
        else:
            os.environ["SA_AGY_APP_DATA_DIR"] = prev_app_dir

    # 2. Pure helpers: mode resolution & option selection
    session_with_auto = {"modes": {"availableModes": [{"id": "default"}, {"id": "accept-edits"}, {"id": "auto"}, {"id": "plan"}]}}
    session_without_auto = {"modes": {"availableModes": [{"id": "default"}, {"id": "accept-edits"}, {"id": "plan"}]}}
    assert resolve_agy_mode({}, session_with_auto) == "auto"
    assert resolve_agy_mode({"mode": "turbo"}, session_with_auto) == "auto"
    assert resolve_agy_mode({"mode": "plan"}, session_with_auto) == "plan"
    assert resolve_agy_mode({"mode": "accept-edits"}, session_with_auto) == "accept-edits"
    assert resolve_agy_mode({}, session_without_auto) == "accept-edits"

    assert pick_auto_allow_option([
        {"optionId": "a2", "kind": "allow_always"},
        {"optionId": "a1", "kind": "allow_once"},
    ])["optionId"] == "a1"
    assert pick_auto_allow_option([
        {"optionId": "a2", "kind": "allow_always"},
        {"optionId": "r1", "kind": "reject_once"},
    ])["optionId"] == "a2"
    assert pick_auto_allow_option([{"optionId": "r1", "kind": "reject_once"}]) is None
    assert pick_auto_allow_option([
        {"optionId": "dup", "kind": "allow_once"},
        {"optionId": "dup", "kind": "reject_once"},
    ]) is None

    # 3. End-to-end AgyAcpStream auto-approval with ResidentServer
    orig_range = acp_approvals.AUTO_APPROVE_DELAY_RANGE
    prev_markers = os.environ.get("ZOMI_SUBAGENT_SESSIONS_DIR")
    try:
        acp_approvals.AUTO_APPROVE_DELAY_RANGE = (0.12, 0.20)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = str(tmp / "markers")
            server = _write_fake_perm_server(tmp)
            env = os.environ.copy()

            # 3a. Sequential multi-permission turn (allow_once then allow_always) keeps ResidentServer healthy
            log1 = tmp / "log1.json"
            cmd1 = [sys.executable, str(server), str(log1)]
            host = ResidentServer(cmd1, cwd=str(tmp), env=env, agent="agy")
            try:
                turn = AgyAcpStream(
                    cmd1, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high"},
                    prompt="MULTI_PERM",
                    resident=host, is_subagent=True,
                )
                evs = [json.loads(line) for line in turn]
                turn.close()
                assert any(e.get("delta") == "approved:always-id" for e in evs), evs
                assert not host.closed and not host.retire_after_turn and host.process.poll() is None

                entries = json.loads(log1.read_text(encoding="utf-8"))
                modes = [e["modeId"] for e in entries if e["type"] == "set_mode"]
                assert modes == ["auto"], modes
                replies = [e for e in entries if e["type"] == "perm_reply"]
                assert len(replies) == 2, replies
                assert replies[0]["result"] == {"outcome": {"outcome": "selected", "optionId": "allow-once-id"}}
                assert replies[0]["elapsed"] >= 0.10, replies[0]
                assert replies[1]["result"] == {"outcome": {"outcome": "selected", "optionId": "always-id"}}
                assert replies[1]["elapsed"] >= 0.10, replies[1]

                # 3b. interaction_* still emits native_question and is NEVER auto-approved
                turn_q = AgyAcpStream(
                    cmd1, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high"},
                    prompt="INTERACTION_QUESTION",
                    resident=host, is_subagent=True,
                )
                saw_question = False
                for raw in turn_q:
                    ev = json.loads(raw)
                    if ev.get("type") == "native_question":
                        saw_question = True
                        break
                turn_q.cancel()
                turn_q.close()
                assert saw_question, "interaction_* must still surface as native_question"
            finally:
                host.shutdown()

            # 3c. Cancelling during the delay window sends cancelled and NEVER sends selected
            acp_approvals.AUTO_APPROVE_DELAY_RANGE = (0.40, 0.50)
            log2 = tmp / "log2.json"
            cmd2 = [sys.executable, str(server), str(log2)]
            host2 = ResidentServer(cmd2, cwd=str(tmp), env=env, agent="agy")
            try:
                turn_c = AgyAcpStream(
                    cmd2, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high"},
                    prompt="SINGLE_PERM",
                    resident=host2, is_subagent=True,
                )
                deadline = time.monotonic() + 3.0
                while not turn_c.approvals.waiting() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert turn_c.approvals.waiting(), "Expected pending approval"
                turn_c.cancel()
                try:
                    list(turn_c)
                except RuntimeError:
                    pass
                turn_c.close()
                time.sleep(0.55)
                entries2 = json.loads(log2.read_text(encoding="utf-8"))
                replies2 = [e for e in entries2 if e["type"] == "perm_reply"]
                assert len(replies2) == 1, replies2
                assert replies2[0]["result"] == CANCELLED, replies2
            finally:
                host2.shutdown()

            # 3d. Missing allow option fails closed with cancelled and error
            acp_approvals.AUTO_APPROVE_DELAY_RANGE = (0.05, 0.08)
            log3 = tmp / "log3.json"
            cmd3 = [sys.executable, str(server), str(log3)]
            host3 = ResidentServer(cmd3, cwd=str(tmp), env=env, agent="agy")
            try:
                turn_bad = AgyAcpStream(
                    cmd3, cwd=str(tmp), env=env,
                    bot={"model": "gemini-4-hydrogen-high"},
                    prompt="NO_ALLOW_OPTION",
                    resident=host3, is_subagent=True,
                )
                raised = ""
                try:
                    list(turn_bad)
                except RuntimeError as exc:
                    raised = str(exc)
                finally:
                    turn_bad.close()
                assert "未提供可自动批准的权限选项" in raised, raised
                entries3 = json.loads(log3.read_text(encoding="utf-8"))
                replies3 = [e for e in entries3 if e["type"] == "perm_reply"]
                assert len(replies3) == 1 and replies3[0]["result"] == CANCELLED, replies3
            finally:
                host3.shutdown()

            # 3e. Non-AGY ACP agents (e.g. droid) still fail closed on unexpected permission requests
            log4 = tmp / "log4.json"
            cmd4 = [sys.executable, str(server), str(log4)]
            host4 = ResidentServer(cmd4, cwd=str(tmp), env=env, agent="droid")
            try:
                turn_d = DroidAcpStream(
                    cmd4, cwd=str(tmp), env=env,
                    bot={"model": "gpt-6.2-astra"},
                    prompt="SINGLE_PERM",
                    resident=host4, is_subagent=True,
                )
                raised = ""
                try:
                    list(turn_d)
                except RuntimeError as exc:
                    raised = str(exc)
                finally:
                    turn_d.close()
                assert "请求了额外权限" in raised, raised
            finally:
                host4.shutdown()
    finally:
        acp_approvals.AUTO_APPROVE_DELAY_RANGE = orig_range
        if prev_markers is None:
            os.environ.pop("ZOMI_SUBAGENT_SESSIONS_DIR", None)
        else:
            os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = prev_markers

    print("PASS: acp_approval_cases")


if __name__ == "__main__":
    acp_approval_cases()
