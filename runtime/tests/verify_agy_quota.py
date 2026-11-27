#!/usr/bin/env python3
"""Deterministic official CLI quota and daemon tests; no real engine access."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
from agent_runtime import agy_quota as q, daemon as d, session_handoff as sh, quota_guard as guard

OPUS, CARBON, HYDROGEN, NEON = "claude-opus-5-5-high", "gemini-4-carbon-medium", "gemini-4-hydrogen-high", "gemini-4-neon-xhigh"


def usage(rem=.5, group=.8):
    return dict(status="SUCCESS", command=dict(name="usage", data=dict(groups=[
        dict(name=name, description=desc, buckets=[dict(id=prefix + "-weekly", remaining_fraction=group, reset_time=""),
             dict(id=prefix + "-5h", remaining_fraction=rem, reset_time="")])
        for name, desc, prefix in [
            ("Gemini Models", "Models within this group: Gemini 4 Carbon, Gemini 4 Hydrogen, Gemini 4 Neon, Gemini 3.8 Flash", "gemini"),
            ("Claude and GPT models", "Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B", "3p"),
        ]])))


class UsageFixture:
    def __init__(self):
        self.calls, self.gate, self.entered = [], None, threading.Event()
        self.set(.5)

    def set(self, rem, group=.8):
        self.doc = usage(rem, group)
        reset()

    def fetch(self):
        self.calls.append("usage")
        if self.gate is not None:
            self.entered.set()
            assert self.gate.wait(5)
        return q._parse_usage(json.dumps(self.doc))


def reset():
    q.invalidate_agy_quota_cache()


def pf(model=OPUS, **kwargs):
    return q.preflight_agy_request(model=model, prompt=kwargs.pop("prompt", "continue"), **kwargs)


def unit(good):
    with patch.object(q, "_fetch_snapshot", side_effect=good.fetch):
        for frac, expected in [(0, True), (.05, True), (.051, False), (.5, False)]:
            good.set(frac)
            assert pf()["should_fallback"] == expected
        good.set(0)
        for model, prompt, target, effort in [
            (OPUS, "task", "claude-opus-5-5", "high"),
            ("claude-opus-5-5-max", "task", "claude-opus-5-5", "max"),
            (CARBON, "task", "gemini-4-carbon", "medium"),
            ("gemini-4-carbon-ultra", "task", "gemini-4-carbon", "ultra"),
            (NEON, "task", "gemini-4-neon", "xhigh"),
            ("gemini-4-neon", "task", "gemini-4-neon", "medium"),
            ("gemini-4-neon-medium", "task", "gemini-4-neon", "medium"),
            (HYDROGEN, "只读禁令", "gemini-4-hydrogen", "high"),
            (HYDROGEN, "纯只读侦察", "gemini-4-hydrogen", "high"),
            (HYDROGEN, "task", "gemini-4-hydrogen", "high"),
        ]:
            for mode, translated in [("plan", "spec"), ("", "auto-high")]:
                result = pf(model, prompt=prompt, mode=mode)
                assert (result["agent"], result["model"], result["effort"], result["mode"]) == ("droid", target, effort, translated)
        with patch.dict(os.environ, SA_AGY_FALLBACK_MODEL="override", SA_AGY_FALLBACK_EFFORT="xhigh"):
            assert (pf()["model"], pf()["effort"]) == ("override", "xhigh")
            assert not pf("custom-unknown")["should_fallback"]
        with patch.dict(os.environ, SA_AGY_QUOTA_PREFLIGHT="0"):
            assert not pf()["should_fallback"]
        with patch.dict(os.environ, SA_AGY_MIN_QUOTA_FRAC="0"):
            assert pf()["should_fallback"]
        assert q._quota_for_model("gemini-unknown-future") is None
        assert not pf("gemini-unknown-future")["should_fallback"]
    # Exact version matching in group descriptions + optional omitted description field.
    assert q._resolve("claude-opus-5-5-high", [dict(bid="3p-5h", desc="Models within this group: Claude Opus 5.5.1", rem_frac=0, reset_ts=0)]) is None
    assert q._resolve("gemini-4-hydrogen-high", [dict(bid="gemini-5h", desc="Models within this group: Gemini 4 Hydrogen (Low)", rem_frac=0, reset_ts=0)]) is None
    no_desc_buckets = q._parse_usage(json.dumps({"status": "SUCCESS", "command": {"data": {"groups": [{"name": "Gemini", "buckets": [{"id": "gemini-5h", "remaining_fraction": 0}]}]}}}))
    assert len(no_desc_buckets) == 1 and no_desc_buckets[0]["rem_frac"] == 0.0
    assert q._resolve("gemini-4-hydrogen-high", no_desc_buckets) is None
    assert q._resolve("gemini-4-carbon-unknown", [dict(bid="gemini-5h", desc="Models within this group: Gemini 4 Carbon", rem_frac=0, reset_ts=0)]) is None
    # In-flight invalidation must reject stale snapshot for both in-flight waiters and post-invalidation callers.
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    def stale_fetch():
        entered.set()
        release.wait(2)
        done.set()
        return q._parse_usage(json.dumps(usage(0)))
    results = []
    try:
        reset()
        with patch.object(q, "_fetch_snapshot", side_effect=stale_fetch) as mocked:
            t1 = threading.Thread(target=lambda: results.append(pf()["should_fallback"]))
            t1.start()
            assert entered.wait(1)
            q.invalidate_agy_quota_cache()
            results.append(pf()["should_fallback"])
            release.set()
            t1.join(2)
            assert done.wait(1)
            assert mocked.call_count == 1
            assert results == [False, False] and q._CACHE is None
    finally:
        release.set()
        deadline = time.perf_counter() + 2
        while guard._FLIGHT is not None and time.perf_counter() < deadline:
            time.sleep(.005)
        reset()
    for invalid in (True, None, "0", float("nan"), float("inf"), -.1, 1.5):
        doc = usage(); doc["command"]["data"]["groups"][0]["buckets"][0]["remaining_fraction"] = invalid
        with patch.object(q, "_fetch_snapshot", side_effect=lambda: q._parse_usage(json.dumps(doc))):
            reset(); assert not pf()["should_fallback"]
    for raw in ('{}', '{', json.dumps(dict(usage(), status="ERROR"))):
        with patch.object(q, "_fetch_snapshot", side_effect=lambda: q._parse_usage(raw)):
            reset(); assert not pf()["should_fallback"]
    doc = usage(0); doc["command"]["data"]["groups"][0]["buckets"][0]["reset_time"] = "bad"
    try:
        q._parse_usage(json.dumps(doc))
        raise AssertionError("bad date accepted")
    except ValueError:
        pass
    reset()
    with patch.object(q, "find_cli_binary", return_value=None):
        assert not pf()["should_fallback"]
    print("PASS: strict JSON, zero, boundaries, mappings, overrides and fail-open")


def reset_window_cases():
    reset_at = 1_800_000_000
    family = dict(bid="3p-5h", desc="Claude Opus 5.5", rem_frac=0, reset_ts=reset_at)
    global_ = dict(family, bid="global-weekly", desc="")
    healthy = dict(family, bid="3p-weekly", rem_frac=.8, reset_ts=0)
    still_low = dict(healthy, rem_frac=.03, reset_ts=reset_at + 100)
    for expired, remaining, after in (
        (family, [], None),
        (family, [healthy], healthy),
        (global_, [healthy], healthy),
        (family, [still_low], still_low),
    ):
        reset()
        with patch.object(q, "_fetch_snapshot", return_value=[expired, *remaining]) as fetch, \
                patch.object(q.time, "time", return_value=reset_at - 1) as wall, \
                patch.object(q.time, "monotonic", return_value=100):
            assert pf()["event"]["rem_frac"] == 0
            assert pf()["event"]["reset_ts"] == reset_at
            for now in (reset_at, reset_at + 1):
                wall.return_value = now
                assert q._quota_for_model(OPUS) == after
                result = pf()
                if after is still_low:
                    assert result["should_fallback"] and result["event"]["rem_frac"] == .03
                else:
                    assert result["agent"] == "agy" and not result["should_fallback"] and result["event"] is None
            fetch.assert_called_once()
    reset()
    print("PASS: reset boundary ignores expired cached model/global buckets and keeps active limits")


def cli_cases(root):
    script = root / "agy"
    script.write_text("#!/usr/bin/env python3\nimport sys\nassert sys.argv[1:] == ['-p', '/usage', '--output-format', 'json']\nprint(" + repr(json.dumps(usage(0))) + ")\n")
    script.chmod(0o755)
    with patch.object(q, "find_cli_binary", return_value=str(script)):
        reset(); assert pf()["should_fallback"]
        script.write_text("#!/usr/bin/env python3\nraise SystemExit(7)\n")
        reset(); assert not pf()["should_fallback"]
        script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
        real_popen, procs = q.subprocess.Popen, []
        def spawn(*a, **kw):
            assert kw["start_new_session"] is True
            proc = real_popen(*a, **kw); procs.append(proc); return proc
        with patch.object(q.subprocess, "Popen", side_effect=spawn), patch.dict(os.environ, SA_AGY_QUOTA_TIMEOUT_SEC=".1"):
            reset(); start = time.perf_counter()
            assert not pf()["should_fallback"] and time.perf_counter() - start < .4
            deadline = time.perf_counter() + .6
            while guard._FLIGHT is not None and time.perf_counter() < deadline: time.sleep(.01)
            assert guard._FLIGHT is None and procs[0].poll() is not None and procs[0].stdout.closed
    reset()
    print("PASS: official argv, nonzero exit, bounded timeout and process reaping")

MOCK = '''import json, os, sys, time
engine, log = sys.argv[1:]
def record(value):
    with open(log, "a") as out: out.write(json.dumps(value) + "\\n")
def send(value): print(json.dumps(value), flush=True)
record(dict(kind="spawn", engine=engine))
seq = 0
for line in sys.stdin:
    msg = json.loads(line); method = msg.get("method"); rid = msg.get("id"); p = msg.get("params") or {}
    result = {}
    if method == "initialize": result = dict(protocolVersion=1, agentCapabilities=dict(loadSession=True))
    elif method in ("session/new", "session/load"):
        seq += 1
        result = dict(sessionId=p.get("sessionId") or f"{engine}-sid-{seq}", configOptions=[dict(id="reasoning_effort")])
    elif method == "session/prompt":
        text = p["prompt"][0]["text"]; record(dict(kind="prompt", engine=engine, text=text, sid=p["sessionId"]))
        while "HOLD_FOR_RELEASE" in text and not os.path.exists(log + ".release"): time.sleep(.01)
        if "RUNTIME_LIMIT" in text:
            send(dict(jsonrpc="2.0", id=rid, error=dict(code=429, message="RESOURCE_EXHAUSTED quota"))); continue
        send(dict(jsonrpc="2.0", method="session/update", params=dict(sessionId=p["sessionId"], update=dict(sessionUpdate="agent_message_chunk", content=dict(type="text", text="reply:" + engine)))))
        result = dict(stopReason="end_turn")
    if rid is not None: send(dict(jsonrpc="2.0", id=rid, result=result))
'''

def e2e(good, root):
    sock, home, log = root / "daemon" / "daemon.sock", root / "home", root / "mock.jsonl"
    mock = root / "mock.py"
    mock.write_text(MOCK)
    mock.chmod(0o755)
    env = dict(os.environ, SA_SUBAGENTD_DIR=str(sock.parent), SA_SESSIONS_LEDGER_DIR=str(root / "ledger"),
               ZOMI_SUBAGENT_SESSIONS_DIR=str(root / "markers"), SA_RAW_LOG="", SA_SUBAGENT="1")
    with patch.dict(os.environ, env, clear=True), patch.object(Path, "home", return_value=home), \
            patch.object(q, "_fetch_snapshot", side_effect=good.fetch), \
            patch.object(d, "build_server_cmd_and_stream_cls", side_effect=lambda ag, cfg=None: ([sys.executable, str(mock), ag, str(log)], d.DroidAcpStream if ag == "droid" else d.AgyAcpStream)):
        daemon = d.SubagentDaemon(socket_path=sock)
        server = threading.Thread(target=daemon.serve_forever, daemon=True)
        server.start()
        deadline = time.monotonic() + 5
        while not sock.exists() and time.monotonic() < deadline: time.sleep(.01)
        assert sock.exists()
        def client(sid="", prompt="task"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.object(d, "ensure_daemon_running", return_value=sock), patch.object(d.signal, "signal"):
                rc = d.run_subagent_client(agent="agy", model=OPUS, mode="plan", session_id=sid, cwd=str(root), prompt=prompt)
            return rc, [json.loads(line) for line in out.getvalue().splitlines()]
        def records(): return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        def direct(prompt="task", sid=""):
            left, right = socket.socketpair()
            left.settimeout(10)
            def run():
                try: daemon._handle_run(right, dict(agent="agy", model=OPUS, session_id=sid, cwd=str(root), prompt=prompt))
                finally: right.close()
            turn = threading.Thread(target=run)
            turn.start()
            with left, left.makefile() as stream: frames = [json.loads(line) for line in stream]
            turn.join(5)
            assert not turn.is_alive()
            return frames
        try:
            reset(); good.set(0)
            start = len(good.calls)
            rc, frames = client()
            assert rc == 0 and frames[-1]["agent"] == "droid" and "thread_id" not in frames[-1]
            assert any(f.get("event") == "quota_fallback" for f in frames)
            assert len(good.calls) - start == 1
            assert all(r["engine"] == "droid" for r in records())
            assert sh._strip_subagent_prefix(records()[-1]["text"]) == "task"
            db = home / ".gemini/antigravity-cli/conversations/legacy.db"
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, metadata BLOB, step_payload TEXT)")
                con.executemany("INSERT INTO steps VALUES (?, ?, NULL, ?)", [(1, 14, "old-A"), (2, 15, "answer-A"), (3, 14, "old-B"), (4, 15, "answer-B")])
            wrapped = sh.format_handoff_prompt("older", [dict(prompt="old-A", response="answer-A")], "old-B")
            sh.save_session_ledger(dict(agent="agy", session_id="legacy", turns=[dict(prompt=wrapped, response="answer-B"), dict(prompt="old-C", response="answer-C")]))
            good.set(.03)
            rc, frames = client("legacy", "continue-task")
            assert rc == 0 and frames[-1]["agent"] == "droid"
            sid = frames[-1]["conversation_id"]
            text = records()[-1]["text"]
            assert text.count("<previous_session_history") == 1 and all(t in text for t in ("old-A", "old-B", "old-C", "continue-task"))
            assert text.index("old-A") < text.index("old-B") < text.index("old-C")
            ledger = sh.load_session_ledger(sid)
            assert ledger["agent"] == "droid" and ledger["handoff_from"] == "legacy" and len(ledger["turns"]) == 4
            assert ledger["turns"][-1]["prompt"] == "continue-task"
            good.set(0); before = len(records())
            rc, frames = client("missing-sid")
            assert rc == 1 and "context could not be loaded" in frames[-1]["result"]["error"]
            assert len(records()) == before
            frames = direct(sid="legacy")
            assert frames[-1]["agent"] == "droid" and not daemon.active_sessions
            before = len(records())
            assert direct(sid="missing-direct")[-1]["result"]["status"] == "ERROR" and len(records()) == before
            daemon.active_sessions.add("busy")
            with patch.object(d, "preflight_agy_request", side_effect=AssertionError("must not inspect an active session")):
                assert direct(sid="busy")[-1]["result"]["status"] == "ERROR"
            daemon.active_sessions.discard("busy")
            good.set(0); good.gate, good.entered = threading.Event(), threading.Event()
            raced, before = [], len(records())
            first = threading.Thread(target=lambda: raced.append(client("legacy", "HOLD_FOR_RELEASE")))
            first.start()
            try:
                assert good.entered.wait(3) and daemon.is_session_busy("legacy")
                assert "拒绝并发重入" in direct(sid="legacy")[-1]["result"]["error"] and len(records()) == before
                good.gate.set()
                deadline = time.monotonic() + 3
                while not any("HOLD_FOR_RELEASE" in r.get("text", "") for r in records()[before:]) and time.monotonic() < deadline: time.sleep(.01)
                assert daemon.is_session_busy("legacy") and "拒绝并发重入" in direct(sid="legacy")[-1]["result"]["error"]
            finally:
                good.gate.set(); (root / "mock.jsonl.release").touch(); first.join(5); good.gate = None
            assert not first.is_alive() and raced[0][0] == 0 and not daemon.active_sessions
            failure = root / "failed.py"
            failure.write_text("raise SystemExit(7)" + chr(10))
            good.set(0)
            with patch.object(d, "build_server_cmd_and_stream_cls", return_value=([sys.executable, str(failure)], d.DroidAcpStream)):
                rc, frames = client("legacy")
            assert rc == 1 and frames[-1]["agent"] == "droid" and frames[-1]["conversation_id"] == ""
            before = len(records())
            with patch.object(daemon, "_acquire_resident", side_effect=RuntimeError("mock acquire failure")):
                rc, frames = client("legacy")
            assert rc == 1 and frames[-1]["agent"] == "droid" and frames[-1]["conversation_id"] == ""
            assert frames[-1]["result"]["error"] == "mock acquire failure" and not daemon.is_session_busy("legacy") and len(records()) == before
            good.set(.5)
            assert direct("RUNTIME_LIMIT")[-1]["result"]["status"] == "ERROR" and q._CACHE is None
        finally:
            daemon.shutdown_all(); server.join(3)
        print("PASS: daemon + client fallback, merged SQLite/ledger migration, missing-context fast fail and runtime invalidation")

def history_merge_test():
    turns = [dict(prompt=f"question-{i}", response=f"answer-{i}", ts=i) for i in range(300)]
    def inspect(*_):
        assert not sh._LEDGER_LOCK._is_owned()
        return dict(turns=turns[:200])
    original = sh.format_handoff_prompt
    def format_prompt(*args):
        assert not sh._LEDGER_LOCK._is_owned()
        return original(*args)
    start = time.perf_counter()
    with patch.object(sh, "load_session_ledger", return_value=dict(turns=turns[100:])), \
            patch.object(sh, "inspect_local_session", side_effect=inspect), patch.object(sh, "format_handoff_prompt", side_effect=format_prompt):
        prompt = sh.build_forced_handoff_prompt("agy", "mock", "", "next")
    assert time.perf_counter() - start < .5
    carried, clean = sh._unwrap_handoff_prompt(prompt)
    assert [t["prompt"] for t in carried] == [t["prompt"] for t in turns] and clean == "next"
    assert prompt.count("<previous_session_history") == 1
    print("PASS: linear 300-turn merge, complete ordered history, unlocked SQLite/formatting")

def main():
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SA_AGY_")}
    with tempfile.TemporaryDirectory() as td, \
            patch.dict(os.environ, dict(clean, SA_DISPATCH_POLICY_PATH=str(Path(td) / "dispatch-policy.json")), clear=True), \
            patch.object(q, "find_cli_binary", return_value="/mock/droid"):
        good = UsageFixture()
        unit(good)
        reset_window_cases()
        cli_cases(Path(td))
        from performance_cases import quota_cache_cases
        quota_cache_cases(q, good, pf)
        from p2_cases import quota_cases
        quota_cases(q, pf)
        history_merge_test()
        e2e(good, Path(td))
        reset()
    print("ALL AGY QUOTA VERIFICATIONS PASSED")

if __name__ == "__main__":
    main()
