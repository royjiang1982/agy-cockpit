#!/usr/bin/env python3
"""Offline AGY quota policy, CLI boundaries, caching and in-flight expiry."""
from contextlib import contextmanager, nullcontext
import json
from math import isclose
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

from _runtime_fixture import TIMEOUT, isolated_runtime
from agent_runtime import agy_quota as q, quota_guard as guard

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


def preflight_cases(good):
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
    with patch.object(q, "find_cli_binary", return_value=None) as find:
        assert not pf()["should_fallback"]
        find.assert_called_once_with("agy")
    original = dict(agent="agy", model=OPUS, effort="high", requested_sid="existing", mode="plan", prompt="task")
    good.set(0)
    with (patch.object(q, "_fetch_snapshot", side_effect=good.fetch),
          patch.object(q, "find_cli_binary", return_value=None) as find,
          patch.object(q, "build_forced_handoff_prompt") as handoff):
        assert q.preflight_agy_request(**original) == dict(original, should_fallback=False, event=None, handoff=None)
        find.assert_called_once_with("droid")
        handoff.assert_not_called()
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


@contextmanager
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
    print("PASS: family/global quota scope, unknown quota and negative cache expiry")


class GatedSnapshot:
    def __init__(self, snapshot):
        self.entered, self.release = threading.Event(), threading.Event()
        self.snapshot = snapshot

    def fetch(self):
        self.entered.set()
        assert self.release.wait(TIMEOUT)
        return self.snapshot


def invalidation_cases():
    for budget in (None, .04):
        reset()
        lookup = GatedSnapshot(q._parse_usage(json.dumps(usage(0))))
        callers, result = [], []

        def reach_deadline(clock, _wait):
            assert lookup.entered.wait(TIMEOUT)
            clock.elapsed = budget
            return False

        def fork(action):
            thread = threading.Thread(target=action, daemon=True)
            callers.append(thread)
            thread.start()

        timing = virtual_quota_wait(budget, reach_deadline) if budget is not None else nullcontext()
        with (
            patch.dict(os.environ, **({"SA_AGY_QUOTA_TIMEOUT_SEC": str(budget)} if budget is not None else {})),
            patch.object(q, "_fetch_snapshot", side_effect=lookup.fetch) as fetch, timing as timer,
        ):
            wait_done = None
            try:
                if timer is None:
                    fork(lambda: result.append(pf()["should_fallback"]))
                    assert lookup.entered.wait(TIMEOUT)
                    wait_done = guard._FLIGHT["done"].wait
                else:
                    clock, wait_done = timer
                    assert not pf()["should_fallback"] and clock.elapsed == budget
                    flight = guard._FLIGHT
                    assert flight["timed_out"] and flight["cancelled"].is_set()
                    assert not pf()["should_fallback"]
                flight = guard._FLIGHT
                invalidated = threading.Event()

                def invalidate():
                    reset()
                    invalidated.set()

                fork(invalidate)
                assert invalidated.wait(TIMEOUT), "Invalidation waited for the stalled quota lookup"
                assert guard._FLIGHT is flight and flight["invalidated"]
                assert not pf()["should_fallback"]  # No second lookup and no stale fallback.
                fetch.assert_called_once()
            finally:
                lookup.release.set()
                if wait_done is not None:
                    assert wait_done(TIMEOUT)
                for thread in callers:
                    thread.join(TIMEOUT)
                    assert not thread.is_alive()
            assert result == ([False] if timer is None else []) and q._CACHE is None
    reset()


def concurrent_lookup_case():
    lookup = GatedSnapshot(q._parse_usage(json.dumps(usage(0))))
    joined = threading.Event()
    concurrent_out = {}

    def worker(model):
        concurrent_out[model] = q.preflight_agy_request(agent='agy', model=model, prompt='task')['should_fallback']

    with patch.object(q, '_fetch_snapshot', side_effect=lookup.fetch) as snap:
        first = threading.Thread(target=worker, args=(OPUS,), daemon=True)
        second = threading.Thread(target=worker, args=(HYDROGEN,), daemon=True)
        first.start()
        try:
            assert lookup.entered.wait(TIMEOUT)
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
            lookup.release.set()
            first.join(TIMEOUT)
            if second.ident is not None:
                second.join(TIMEOUT)
        assert not first.is_alive() and not second.is_alive()
        assert concurrent_out == {OPUS: True, HYDROGEN: True}, concurrent_out
        snap.assert_called_once()
    q.invalidate_agy_quota_cache()
    print("PASS: concurrent lookup coalescing, deadline cancellation and in-flight invalidation")


def main():
    with isolated_runtime() as root, patch.object(q, "find_cli_binary", return_value="/mock/droid"):
        good = UsageFixture()
        preflight_cases(good)
        reset_window_cases()
        cli_cases(root)
        quota_cache_cases(good)
        quota_cases()
        invalidation_cases()
        concurrent_lookup_case()
    print("ALL OFFLINE AGY QUOTA VERIFICATIONS PASSED")


if __name__ == "__main__":
    main()
