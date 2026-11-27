"""Unit and end-to-end tests for normal-mode AGY ACP delayed auto-approval."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, write_rpc_server
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


PERMISSION_SCRIPT = '''
log_file = sys.argv[1]
session_info = {
    "models": {"availableModels": [
        {"modelId": "gemini-4-hydrogen-high"}, {"modelId": "gpt-6.2-astra"},
    ]},
    "modes": {"currentModeId": "default", "availableModes": [
        {"id": mode, "name": mode} for mode in ("default", "accept-edits", "auto", "plan")
    ]},
}
events_log = []
pending = {}
sent_times = {}
perm_seq = 1000

def record(entry):
    events_log.append(entry)
    with open(log_file, "w", encoding="utf-8") as out:
        json.dump(events_log, out)

def option(id, kind):
    return {"optionId": id, "kind": kind, "name": id}

def request_permission(rid, sid, tool_id, title, options, *, prompt_id=None, remaining=False):
    sent_times[rid] = time.monotonic()
    if prompt_id is not None:
        pending[rid] = (prompt_id, sid, remaining)
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {"toolCallId": tool_id, "title": title, "name": "run_command"},
        "options": options,
    }})

def handle_prompt(rid, sid, text):
    global perm_seq
    perm_seq += 1
    allow = option("allow-once-id", "allow_once")
    always = option("allow-always-id", "allow_always")
    reject = option("reject-id", "reject_once")
    if "INTERACTION_QUESTION" in text:
        request_permission(perm_seq, sid, "interaction_1", "Pick one", [{"optionId": "opt_a", "name": "Option A"}])
        return
    options = ([reject] if text.endswith("NO_ALLOW_OPTION") else [always] if text.endswith("ALWAYS_ONLY")
               else [allow, always, reject])
    request_permission(perm_seq, sid, f"tc-{perm_seq}", "echo ok", options,
                       prompt_id=rid, remaining="MULTI_PERM" in text)

def handle_cancel(params):
    record({"type": "session_cancel", "sessionId": params.get("sessionId")})

def override(method, rid, params, message):
    global perm_seq
    if method == "test/drain":
        reply(rid, {"events": events_log})
        return True
    if method == "session/set_mode":
        record({"type": "set_mode", "modeId": params.get("modeId")})
    if method is not None or "id" not in message:
        return False
    result = message.get("result")
    entry = {"type": "perm_reply", "perm_id": rid, "result": result}
    if rid in sent_times:
        entry["elapsed"] = time.monotonic() - sent_times[rid]
    record(entry)  # Record duplicates even after the first response consumed the pending entry.
    info = pending.pop(rid, None)
    if info is None:
        return True
    prompt_id, sid, remaining = info
    outcome = (result or {}).get("outcome") or {}
    if outcome.get("outcome") != "selected":
        reply(prompt_id, {"stopReason": "cancelled"})
    elif remaining:
        perm_seq += 1
        request_permission(perm_seq, sid, f"tc-{perm_seq}", "step2", [
            option("always-id", "allow_always"), option("second-once-id", "allow_once"),
            option("reject-id", "reject_once"),
        ], prompt_id=prompt_id)
    else:
        update(sid, f"approved:{outcome.get('optionId')}")
        reply(prompt_id, {"stopReason": "end_turn"})
    return True
'''


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
    ]) is None
    assert pick_auto_allow_option([{"optionId": "a2", "kind": "allow_always"}]) is None
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
            server = write_rpc_server(tmp, "fake_agy_perm.py", setup=PERMISSION_SCRIPT)
            env = os.environ.copy()

            # 3a. Two one-time approvals keep the ResidentServer healthy
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
                assert any(e.get("delta") == "approved:second-once-id" for e in evs), evs
                assert not host.closed and not host.retire_after_turn and host.process.poll() is None

                entries = json.loads(log1.read_text(encoding="utf-8"))
                modes = [e["modeId"] for e in entries if e["type"] == "set_mode"]
                assert modes == ["auto"], modes
                replies = [e for e in entries if e["type"] == "perm_reply"]
                assert len(replies) == 2, replies
                assert replies[0]["result"] == {"outcome": {"outcome": "selected", "optionId": "allow-once-id"}}
                assert replies[0]["elapsed"] >= 0.10, replies[0]
                assert replies[1]["result"] == {"outcome": {"outcome": "selected", "optionId": "second-once-id"}}
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
            log2 = tmp / "log2.json"
            cmd2 = [sys.executable, str(server), str(log2)]
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            approve = acp_approvals.AcpAutoApprover._approve_after_delay

            def gated_approval(self, record):
                try:
                    entered.set()
                    if release.wait(TIMEOUT):
                        approve(self, record)
                finally:
                    finished.set()

            host2 = ResidentServer(cmd2, cwd=str(tmp), env=env, agent="agy")
            try:
                with patch.object(acp_approvals.AcpAutoApprover, "_approve_after_delay", gated_approval):
                    turn_c = AgyAcpStream(
                        cmd2, cwd=str(tmp), env=env,
                        bot={"model": "gemini-4-hydrogen-high"},
                        prompt="SINGLE_PERM",
                        resident=host2, is_subagent=True, rpc_timeout=TIMEOUT,
                    )
                    try:
                        assert entered.wait(TIMEOUT) and turn_c.approvals.waiting(), "Expected pending approval"
                        turn_c.cancel()
                    finally:
                        release.set()
                    assert finished.wait(TIMEOUT) and turn_c._protocol_done.wait(TIMEOUT)
                    try:
                        # A wire round trip after the approval worker exits drains
                        # every earlier reply, even if the prompt already ended.
                        entries2 = turn_c._rpc("test/drain", {})["events"]
                    finally:
                        turn_c.close()
                replies2 = [e for e in entries2 if e["type"] == "perm_reply"]
                assert len(replies2) == 1, replies2
                assert replies2[0]["result"] == CANCELLED, replies2
            finally:
                host2.shutdown()

            # 3d. A permanent-only or reject-only request is cancelled and reports an error
            acp_approvals.AUTO_APPROVE_DELAY_RANGE = (0.05, 0.08)
            for prompt in ("NO_ALLOW_OPTION", "ALWAYS_ONLY"):
                log3 = tmp / f"{prompt}.json"
                cmd3 = [sys.executable, str(server), str(log3)]
                host3 = ResidentServer(cmd3, cwd=str(tmp), env=env, agent="agy")
                try:
                    turn_bad = AgyAcpStream(
                        cmd3, cwd=str(tmp), env=env,
                        bot={"model": "gemini-4-hydrogen-high"},
                        prompt=prompt,
                        resident=host3, is_subagent=True,
                    )
                    raised = ""
                    try:
                        list(turn_bad)
                    except RuntimeError as exc:
                        raised = str(exc)
                    finally:
                        done = turn_bad._protocol_done.wait(2)
                        turn_bad.close()
                    assert "未提供可自动批准的权限选项" in raised, (prompt, raised)
                    assert done, f"No prompt reply after permission cancellation: {prompt}"
                    entries3 = json.loads(log3.read_text(encoding="utf-8"))
                    replies3 = [e for e in entries3 if e["type"] == "perm_reply"]
                    assert len(replies3) == 1 and replies3[0]["result"] == CANCELLED, (prompt, replies3)
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
