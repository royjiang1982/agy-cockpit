"""Unit and end-to-end tests for normal-mode AGY ACP delayed auto-approval."""
from contextlib import contextmanager
import json
from math import isclose
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, isolated_runtime, write_rpc_server
from agent_runtime import acp_approvals
from agent_runtime.acp_approvals import (
    AUTO_APPROVE_DELAY_RANGE,
    CANCELLED,
    pick_auto_allow_option,
    resolve_agy_mode,
)
from agent_runtime.acp_stream import AgyAcpStream, DroidAcpStream
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
perm_seq = 1000

def record(entry):
    events_log.append(entry)
    with open(log_file, "w", encoding="utf-8") as out:
        json.dump(events_log, out)

def option(id, kind):
    return {"optionId": id, "kind": kind, "name": id}

def request_permission(rid, sid, tool_id, title, options, *, prompt_id=None, remaining=False):
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


@contextmanager
def virtual_approval_timer(gate=None):
    """Advance only the approval worker's clock; optionally block at its first wait."""
    clock = threading.local()
    samples, selected_at = [], []
    approve = acp_approvals.AcpAutoApprover._approve_after_delay

    def monotonic():
        return clock.now

    def uniform(lo, hi):
        samples.append((lo, hi))
        return (lo + hi) / 2

    def worker(approver, record):
        clock.now = 0.0
        send = approver._write
        done = record["done"]

        def advance(seconds):
            assert seconds > 0
            if gate is not None:
                entered, release, _ = gate
                entered.set()
                assert release.wait(TIMEOUT)
            if not done.is_set():
                clock.now += seconds
            return done.is_set()

        def observe(frame):
            if frame["result"]["outcome"]["outcome"] == "selected":
                selected_at.append(clock.now)
            send(frame)

        try:
            with patch.object(done, "wait", side_effect=advance), patch.object(approver, "_write", side_effect=observe):
                approve(approver, record)
        finally:
            if gate is not None:
                gate[2].set()

    with (
        patch.object(acp_approvals, "time", SimpleNamespace(monotonic=monotonic)),
        patch.object(acp_approvals, "random", SimpleNamespace(uniform=uniform)),
        patch.object(acp_approvals.AcpAutoApprover, "_approve_after_delay", worker),
    ):
        yield samples, selected_at


def acp_approval_cases():
    assert AUTO_APPROVE_DELAY_RANGE == (1.0, 5.0), AUTO_APPROVE_DELAY_RANGE
    def modes(*ids):
        return {"modes": {"availableModes": [{"id": mode} for mode in ids]}}

    with_auto = modes("default", "accept-edits", "auto", "plan")
    for mode, expected in (("", "auto"), ("turbo", "auto"), ("plan", "plan"), ("accept-edits", "accept-edits")):
        assert resolve_agy_mode({"mode": mode} if mode else {}, with_auto) == expected
    assert resolve_agy_mode({}, modes("default", "accept-edits", "plan")) == "accept-edits"

    always, once, reject = (dict(optionId=key, kind=kind) for key, kind in (
        ("a2", "allow_always"), ("a1", "allow_once"), ("r1", "reject_once"),
    ))
    assert pick_auto_allow_option([always, once]) == once
    for options in ([always, reject], [always], [reject], [once, dict(reject, optionId="a1")]):
        assert pick_auto_allow_option(options) is None

    with isolated_runtime() as tmp:
        server = write_rpc_server(tmp, "fake_agy_perm.py", setup=PERMISSION_SCRIPT)
        env = os.environ.copy()

        @contextmanager
        def peer(name, agent="agy"):
            log = tmp / f"{name}.json"
            cmd = [sys.executable, str(server), str(log)]
            host = ResidentServer(cmd, cwd=str(tmp), env=env, agent=agent)
            try:
                yield host, cmd, log
            finally:
                host.shutdown()

        def turn(host, cmd, prompt, **kwargs):
            cls, model = ((AgyAcpStream, "gemini-4-hydrogen-high") if host.agent == "agy"
                          else (DroidAcpStream, "gpt-6.2-astra"))
            return cls(cmd, cwd=str(tmp), env=env, bot={"model": model}, prompt=prompt,
                       resident=host, is_subagent=True, **kwargs)

        def permission_replies(log):
            return [entry for entry in json.loads(log.read_text(encoding="utf-8")) if entry["type"] == "perm_reply"]

        # 3a. Two one-time approvals keep the ResidentServer healthy
        with peer("multi") as (host, cmd, log):
            with virtual_approval_timer() as (samples, selected_at):
                multi = turn(host, cmd, "MULTI_PERM")
                try:
                    evs = [json.loads(line) for line in multi]
                finally:
                    multi.close()
            assert samples == [(1.0, 5.0)] * 2, samples
            assert len(selected_at) == 2 and all(isclose(t, 3.0) for t in selected_at), selected_at
            assert any(e.get("delta") == "approved:second-once-id" for e in evs), evs
            assert not host.closed and not host.retire_after_turn and host.process.poll() is None

            entries = json.loads(log.read_text(encoding="utf-8"))
            modes = [e["modeId"] for e in entries if e["type"] == "set_mode"]
            assert modes == ["auto"], modes
            replies = [e for e in entries if e["type"] == "perm_reply"]
            assert len(replies) == 2, replies
            assert replies[0]["result"] == {"outcome": {"outcome": "selected", "optionId": "allow-once-id"}}
            assert replies[1]["result"] == {"outcome": {"outcome": "selected", "optionId": "second-once-id"}}

            # 3b. interaction_* still emits native_question and is NEVER auto-approved
            turn_q = turn(host, cmd, "INTERACTION_QUESTION")
            saw_question = False
            for raw in turn_q:
                ev = json.loads(raw)
                if ev.get("type") == "native_question":
                    saw_question = True
                    break
            turn_q.cancel()
            turn_q.close()
            assert saw_question, "interaction_* must still surface as native_question"

        # Cancelling during the delay window sends cancelled and never sends selected.
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        with peer("cancel") as (host, cmd, _):
            with virtual_approval_timer((entered, release, finished)) as (samples, selected_at):
                turn_c = turn(host, cmd, "SINGLE_PERM", rpc_timeout=TIMEOUT)
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
            assert samples == [(1.0, 5.0)] and not selected_at
            replies2 = [e for e in entries2 if e["type"] == "perm_reply"]
            assert len(replies2) == 1, replies2
            assert replies2[0]["result"] == CANCELLED, replies2

        for prompt in ("NO_ALLOW_OPTION", "ALWAYS_ONLY"):
            with peer(prompt) as (host, cmd, log):
                turn_bad = turn(host, cmd, prompt)
                raised = ""
                try:
                    list(turn_bad)
                except RuntimeError as exc:
                    raised = str(exc)
                finally:
                    done = turn_bad._protocol_done.wait(TIMEOUT)
                    turn_bad.close()
                assert "未提供可自动批准的权限选项" in raised, (prompt, raised)
                assert done, f"No prompt reply after permission cancellation: {prompt}"
                replies = permission_replies(log)
                assert len(replies) == 1 and replies[0]["result"] == CANCELLED, (prompt, replies)

        # Other ACP agents fail closed on unexpected permission requests.
        with peer("droid", "droid") as (host, cmd, _):
            turn_d = turn(host, cmd, "SINGLE_PERM")
            raised = ""
            try:
                list(turn_d)
            except RuntimeError as exc:
                raised = str(exc)
            finally:
                turn_d.close()
            assert "请求了额外权限" in raised, raised
    print("PASS: acp_approval_cases")


if __name__ == "__main__":
    acp_approval_cases()
