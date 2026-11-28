#!/usr/bin/env python3
"""Deterministic official CLI quota and daemon tests; no real engine access."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from math import isclose
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, running_daemon, signal_socket, write_rpc_server
from agent_runtime import agy_quota as q, daemon as d, session_handoff as sh, session_files as sf, quota_guard as guard

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
    entered, release = threading.Event(), threading.Event()
    flight = None
    def stale_fetch():
        entered.set()
        assert release.wait(TIMEOUT)
        return q._parse_usage(json.dumps(usage(0)))
    results = []
    try:
        reset()
        with patch.object(q, "_fetch_snapshot", side_effect=stale_fetch) as mocked:
            t1 = threading.Thread(target=lambda: results.append(pf()["should_fallback"]))
            t1.start()
            assert entered.wait(TIMEOUT)
            flight = guard._FLIGHT
            q.invalidate_agy_quota_cache()
            results.append(pf()["should_fallback"])
            release.set()
            t1.join(TIMEOUT)
            assert not t1.is_alive()
            assert mocked.call_count == 1
            assert results == [False, False] and q._CACHE is None
    finally:
        release.set()
        if flight is not None:
            assert flight["done"].wait(TIMEOUT)
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


@contextlib.contextmanager
def virtual_quota_wait(budget, on_wait):
    """Check the real flight's timeout argument without scheduling to that deadline."""
    clock = SimpleNamespace(elapsed=0.0)
    done = threading.Event()
    real_wait = done.wait
    events = iter((done, threading.Event()))  # Flight completion and cancellation.

    def wait(timeout):
        assert isclose(timeout, budget), (timeout, budget)
        return on_wait(clock, real_wait)

    with (
        patch.object(guard, "time", SimpleNamespace(perf_counter=lambda: clock.elapsed)),
        patch.object(guard, "threading", SimpleNamespace(Event=lambda: next(events), Thread=threading.Thread)),
        patch.object(done, "wait", side_effect=wait) as observed,
    ):
        yield clock, real_wait
        observed.assert_called_once()


def cli_cases(root):
    script = root / "agy"
    script.write_text("#!/usr/bin/env python3\nimport sys\nassert sys.argv[1:] == ['-p', '/usage', '--output-format', 'json']\nprint(" + repr(json.dumps(usage(0))) + ")\n")
    script.chmod(0o755)
    with patch.object(q, "find_cli_binary", return_value=str(script)):
        reset(); assert pf()["should_fallback"]
        script.write_text("#!/usr/bin/env python3\nraise SystemExit(7)\n")
        reset(); assert not pf()["should_fallback"]
        script.write_text("#!/usr/bin/env python3\nimport threading\nthreading.Event().wait(30)\n")
        real_popen, procs, slices = q.subprocess.Popen, [], []
        caller_waiting = threading.Event()

        def await_worker(clock, wait):
            caller_waiting.set()
            assert wait(TIMEOUT), "Bounded quota worker failed to finish"
            return True

        def spawn(*a, **kw):
            assert kw["start_new_session"] is True
            proc = real_popen(*a, **kw)
            procs.append(proc)

            def stalled_communicate(*, timeout):
                assert caller_waiting.wait(TIMEOUT)
                slices.append(timeout)
                assert 0 < timeout <= .05 and clock.elapsed + timeout <= .1, slices
                clock.elapsed += timeout
                raise q.subprocess.TimeoutExpired(proc.args, timeout)

            proc.communicate = stalled_communicate
            return proc

        with (
            patch.object(q.subprocess, "Popen", side_effect=spawn),
            patch.object(q, "terminate_proc_group", wraps=q.terminate_proc_group) as cleanup,
            patch.dict(os.environ, SA_AGY_QUOTA_TIMEOUT_SEC=".1"),
            virtual_quota_wait(.1, await_worker) as (clock, _),
        ):
            reset()
            assert not pf()["should_fallback"]
            assert guard._FLIGHT is None and len(procs) == 1
            assert procs[0].poll() is not None and procs[0].stdout.closed
            assert isclose(sum(slices), .1) and isclose(clock.elapsed, .1), slices
            cleanup.assert_called_once()
            grace = cleanup.call_args.kwargs["wait_timeout"]
            assert 0 <= grace <= .05 and clock.elapsed + grace < .4
    reset()
    print("PASS: official argv, nonzero exit, bounded timeout and process reaping")

QUOTA_ACP_SCRIPT = r'''
engine, log, gate = sys.argv[1:]
sid_prefix = f"{engine}-sid"
session_info = {"configOptions": [{"id": "reasoning_effort"}]}

def record(value):
    with open(log, "a", encoding="utf-8") as out:
        out.write(json.dumps(value) + "\n")

def boot():
    record(dict(kind="spawn", engine=engine))

def handle_prompt(rid, sid, text):
    record(dict(kind="prompt", engine=engine, text=text, sid=sid))
    if "HOLD_FOR_RELEASE" in text:
        child_signal(gate, hold=True)
    if "RUNTIME_LIMIT" in text:
        error(rid, 429, "RESOURCE_EXHAUSTED quota")
    else:
        update(sid, "reply:" + engine)
        reply(rid, {"stopReason": "end_turn"})
'''


def e2e(good, root):
    sock, home, log = root / "daemon" / "daemon.sock", root / "home", root / "mock.jsonl"
    gate = root / "prompt.sock"
    script = write_rpc_server(root, "mock.py", setup=QUOTA_ACP_SCRIPT)
    env = dict(os.environ, SA_SUBAGENTD_DIR=str(sock.parent), SA_SESSIONS_LEDGER_DIR=str(root / "ledger"),
               ZOMI_SUBAGENT_SESSIONS_DIR=str(root / "markers"), SA_RAW_LOG="", SA_SUBAGENT="1")

    def build(ag, cfg=None):
        return [sys.executable, str(script), ag, str(log), str(gate)], d.DroidAcpStream if ag == "droid" else d.AgyAcpStream

    with (
        patch.dict(os.environ, env, clear=True), patch.object(Path, "home", return_value=home),
        patch.object(q, "_fetch_snapshot", side_effect=good.fetch),
        patch.object(d, "build_server_cmd_and_stream_cls", side_effect=build),
        signal_socket(gate) as listener, running_daemon(d.SubagentDaemon(socket_path=sock)) as daemon,
    ):
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
        first = threading.Thread(target=lambda: raced.append(client("legacy", "HOLD_FOR_RELEASE")), daemon=True)
        first.start()
        try:
            assert good.entered.wait(3) and daemon.is_session_busy("legacy")
            assert "拒绝并发重入" in direct(sid="legacy")[-1]["result"]["error"] and len(records()) == before
            good.gate.set()
            with listener.accept()[0]:
                assert any("HOLD_FOR_RELEASE" in r.get("text", "") for r in records()[before:])
                assert daemon.is_session_busy("legacy") and "拒绝并发重入" in direct(sid="legacy")[-1]["result"]["error"]
        finally:
            good.gate.set()
            first.join(TIMEOUT)
            good.gate = None
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
        print("PASS: daemon + client fallback, merged SQLite/ledger migration, missing-context fast fail and runtime invalidation")

def history_merge_test():
    def measure(size):
        work = 0

        class CountedText(str):
            def __str__(self):
                return self

            def strip(self):
                nonlocal work
                work += 1
                return self  # Test strings have no outer whitespace.

            def __hash__(self):
                nonlocal work
                work += 1
                return super().__hash__()

            def __eq__(self, other):
                nonlocal work
                work += 1
                return super().__eq__(other)

        def rows(start, end):
            return [dict(prompt=CountedText(f"question-{i}"), response=CountedText(f"answer-{i}"), ts=i)
                    for i in range(start, end)]

        def can_enter():
            if lock.acquire(blocking=False):
                lock.release()
                return True
            return False

        def load(*_):
            observations.append(("load", peer.submit(can_enter).result(timeout=TIMEOUT)))
            return dict(turns=rows(size // 3, size))

        def inspect(*_):
            observations.append(("inspect", peer.submit(can_enter).result(timeout=TIMEOUT)))
            return dict(turns=rows(0, 2 * size // 3))

        def format_prompt(*args):
            observations.append(("format", peer.submit(can_enter).result(timeout=TIMEOUT)))
            return original_format(*args)

        observations = []
        with (
            tempfile.TemporaryDirectory() as directory,
            ThreadPoolExecutor(max_workers=1) as peer,
            patch.object(sh, "load_session_ledger", side_effect=load),
            patch.object(sh, "inspect_local_session", side_effect=inspect),
            patch.object(sh, "format_handoff_prompt", side_effect=format_prompt),
        ):
            state_dir = Path(directory)
            lock = sf.ledger_lock("mock", state_dir)
            prompt = sh.build_forced_handoff_prompt("agy", "mock", "", "next", state_dir)
        assert observations == [("load", False), ("inspect", True), ("format", True)], observations
        carried, clean = sh._unwrap_handoff_prompt(prompt)
        assert [(t["prompt"], t["response"]) for t in carried] == [
            (f"question-{i}", f"answer-{i}") for i in range(size)
        ] and clean == "next"
        assert prompt.count("<previous_session_history") == 1
        return work

    original_format = sh.format_handoff_prompt

    small, large = measure(150), measure(300)
    assert 0 < small and large <= 2.2 * small and large <= 24 * 300, (small, large)
    print("PASS: linear 300-turn merge, complete ordered history, unlocked SQLite/formatting")

def quota_cache_cases(good):
    with patch.object(q, "_fetch_snapshot", side_effect=good.fetch):
        for fraction, age, calls in ((.5, 299, 1), (.5, 300, 2), (.20, 59, 1), (.20, 60, 2), (.1, 60, 2)):
            good.set(fraction)
            before = len(good.calls)
            with patch.object(q.time, "monotonic", return_value=100): pf()
            with patch.object(q.time, "monotonic", return_value=100 + age): pf()
            assert len(good.calls) - before == calls, (fraction, age)
        for key, fraction in (("SA_AGY_QUOTA_TTL_SEC", .5), ("SA_AGY_QUOTA_LOW_TTL_SEC", .1)):
            good.set(fraction)
            with patch.dict(os.environ, {key: "1"}):
                with patch.object(q.time, "monotonic", return_value=100): pf()
                before = len(good.calls)
                with patch.object(q.time, "monotonic", return_value=101): pf()
                assert len(good.calls) == before + 1
    q.invalidate_agy_quota_cache()
    assert q._CACHE is None
    print("PASS: healthy 300s, low 60s, overrides and cache invalidation")


def quota_cases():
    model = 'gemini-4-hydrogen-high'
    def bucket(bid, frac, desc=''):
        return dict(bid=bid, desc=desc, rem_frac=frac, reset_ts=0)
    assert q._resolve(model, [bucket('shared-3p', 0)]) is None
    assert q._resolve('claude-opus-5-5', [bucket('shared-3p', 0)])['rem_frac'] == 0
    for unknown in (model, 'custom-unknown', 'gemini-unknown-future', 'gemini-4-carbon-unknown'):
        assert q._resolve(unknown, [bucket('global-weekly', 0)]) is None
        assert q._resolve(unknown, [bucket('gemini-5h', 0)]) is None
    assert q._resolve(model, [bucket('gemini-5h', .9, 'Gemini 4 Hydrogen'), bucket('global-weekly', .1)])['rem_frac'] == .1
    assert q._resolve(model, [bucket('shared-gemini', .2)])['rem_frac'] == .2
    q.invalidate_agy_quota_cache()
    with patch.object(q, '_fetch_snapshot', side_effect=OSError('offline')) as fetch:
        assert not pf()['should_fallback']
        assert not pf()['should_fallback']
        assert fetch.call_count == 1
    with patch.object(guard.time, 'perf_counter', return_value=guard._NEGATIVE_UNTIL + 1), \
            patch.object(q, '_fetch_snapshot', side_effect=OSError('offline')) as discover:
        assert not pf()['should_fallback'] and discover.call_count == 1
    q.invalidate_agy_quota_cache()
    entered, release = threading.Event(), threading.Event()

    def stalled():
        entered.set()
        assert release.wait(TIMEOUT)
        return []

    def reach_deadline(clock, _wait):
        assert entered.wait(TIMEOUT)
        clock.elapsed = .04
        return False

    with (
        patch.dict(os.environ, SA_AGY_QUOTA_TIMEOUT_SEC='.04'),
        patch.object(q, '_fetch_snapshot', side_effect=stalled) as discover,
        virtual_quota_wait(.04, reach_deadline) as (clock, wait_done),
    ):
        try:
            assert not pf()['should_fallback'] and clock.elapsed < .4
            flight = guard._FLIGHT
            assert flight['timed_out'] and flight['cancelled'].is_set()
            assert not pf()['should_fallback'] and discover.call_count == 1
            # Invalidation must return while the stalled lookup still owns its flight.
            def invalidate():
                q.invalidate_agy_quota_cache()
                invalidated.set()

            invalidated = threading.Event()
            invalidator = threading.Thread(target=invalidate, daemon=True)
            invalidator.start()
            assert invalidated.wait(TIMEOUT)
            assert guard._FLIGHT is flight and flight['invalidated']
        finally:
            release.set()
            if entered.is_set():
                assert wait_done(TIMEOUT)
            q.invalidate_agy_quota_cache()

    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    concurrent_out = {}

    def slow_snap():
        entered.set()
        assert release.wait(TIMEOUT)
        return [bucket('3p-5h', 0, 'Models within this group: Claude Opus 5.5, GPT-OSS 120B'),
                bucket('gemini-5h', 0, 'Models within this group: Gemini 4 Carbon, Gemini 4 Hydrogen')]

    def worker(model):
        concurrent_out[model] = q.preflight_agy_request(agent='agy', model=model, prompt='task')['should_fallback']

    with patch.object(q, '_fetch_snapshot', side_effect=slow_snap) as snap:
        first = threading.Thread(target=worker, args=('claude-opus-5-5-high',), daemon=True)
        second = threading.Thread(target=worker, args=('gemini-4-hydrogen-high',), daemon=True)
        first.start()
        try:
            assert entered.wait(TIMEOUT)
            done = guard._FLIGHT['done']
            wait = done.wait

            def observe(timeout):
                if threading.current_thread() is second:
                    joined.set()
                return wait(timeout)

            with patch.object(done, 'wait', side_effect=observe):
                second.start()
                assert joined.wait(TIMEOUT), "Second model did not join the original lookup"
        finally:
            release.set()
            first.join(TIMEOUT)
            if second.ident is not None:
                second.join(TIMEOUT)
        assert not first.is_alive() and not second.is_alive()
        assert concurrent_out == {'claude-opus-5-5-high': True, 'gemini-4-hydrogen-high': True}, concurrent_out
        snap.assert_called_once()
    q.invalidate_agy_quota_cache()
    print('PASS: family/global quota scope, unknown quota, negative cache expiry and total deadline')


def main():
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SA_AGY_")}
    with tempfile.TemporaryDirectory() as td, \
            patch.dict(os.environ, dict(clean, SA_DISPATCH_POLICY_PATH=str(Path(td) / "dispatch-policy.json")), clear=True), \
            patch.object(q, "find_cli_binary", return_value="/mock/droid"):
        good = UsageFixture()
        unit(good)
        reset_window_cases()
        cli_cases(Path(td))
        quota_cache_cases(good)
        quota_cases()
        history_merge_test()
        e2e(good, Path(td))
        reset()
    print("ALL AGY QUOTA VERIFICATIONS PASSED")

if __name__ == "__main__":
    main()
