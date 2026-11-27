"""Isolated performance regressions, shared by the verification entry points."""
import contextlib
import io
import inspect
import json
import os
import runpy
from pathlib import Path
import socket
import tempfile
import threading
import sys
from unittest.mock import patch


def cli_emission_cases():
    from agent_runtime.cli_events import emit_event
    delta = dict(type="item.completed", delta=True, item=dict(type="agent_message", text="cumulative"))
    final = dict(type="item.completed", item=dict(type="agent_message", text="complete"))
    events = [dict(type="thread.started"), dict(type="item.started"), delta, final,
              dict(type="item.completed", item=dict(type="command_execution")), dict(type="turn.completed"),
              dict(event="result", result=dict(response="complete"))]
    for debug in ("0", "1"):
        out = io.StringIO()
        with patch.dict(os.environ, SA_CODEX_EMIT_DELTAS=debug), contextlib.redirect_stdout(out):
            for event in events:
                emit_event(event)
        emitted = [json.loads(line) for line in out.getvalue().splitlines()]
        assert emitted == (events if debug == "1" else [e for e in events if e is not delta])
    print("PASS: CLI delta filtering, debug opt-in and complete final/result")


def quota_cache_cases(q, good, pf):
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


def client_transition_cases():
    from agent_runtime import daemon as d
    core = {"agent", "model", "effort", "service_tier", "mode", "session_id", "cwd", "prompt", "config", "image_paths", "log_file"}
    ignored_args = {key: "ignored" for key in inspect.signature(d.run_subagent_client).parameters if key not in core}
    package = Path(__file__).resolve().parents[1]
    # Every CLI still accepts legacy no-op arguments without passing them through.
    for engine in ("agy", "codex", "droid", "grok", "pi"):
        argv = ["sa-" + engine, "--prompt", "exact prompt"]
        for key, value in ignored_args.items():
            argv.extend(["--" + key, value])
        with patch.object(sys, "argv", argv), patch.object(d, "run_subagent_client", return_value=0) as call:
            entry = runpy.run_path(str(package / "bin" / ("sa-" + engine)))
            assert entry["main"]() == 0
            assert not (ignored_args.keys() & call.call_args.kwargs.keys())
            assert call.call_args.kwargs["prompt"] == "exact prompt"
    delta = dict(type="item.completed", delta=True, item=dict(type="agent_message", text="partial"))
    final = dict(type="item.completed", item=dict(type="agent_message", text="complete"))
    legacy = [dict(type="event", event="legacy_handoff", source="worker"),
              dict(type="event", event=None), dict(type="event")]
    for debug in ("0", "1"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sock = root / "daemon.sock"
            requests, failures = [], []
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(sock)); server.listen(1); server.settimeout(5)
                def peer():
                    try:
                        with server.accept()[0] as conn, conn.makefile() as stream:
                            requests.append(json.loads(stream.readline()))
                            frames = legacy + [dict(type="event", event=e) for e in (delta, final)]
                            frames += [dict(type="result", agent="codex", conversation_id="old-sid",
                                            result=dict(status="SUCCESS", response="complete"), legacy_metadata=True)]
                            conn.sendall("".join(json.dumps(frame) + "\n" for frame in frames).encode())
                    except Exception as exc:
                        failures.append(exc)
                worker = threading.Thread(target=peer, daemon=True)
                worker.start()
                out, log = io.StringIO(), root / "events.jsonl"
                with patch.object(d, "ensure_daemon_running", return_value=sock), patch.object(d.signal, "signal"), \
                        patch.dict(os.environ, SA_CODEX_EMIT_DELTAS=debug), contextlib.redirect_stdout(out):
                    rc = d.run_subagent_client(agent="codex", model="mock", prompt="exact prompt",
                                               log_file=str(log), **ignored_args)
                worker.join(5)
            assert not worker.is_alive() and not failures and rc == 0
            request = requests[0]
            assert request["action"] == request["op"] == "run" and request["prompt"] == "exact prompt"
            assert set(request) == core - {"log_file"} | {"action", "op", "emit_deltas"}
            frames = [json.loads(line) for line in out.getvalue().splitlines()]
            assert frames[:3] == legacy
            assert (delta in frames) == (debug == "1") and final in frames
            assert frames[-1]["thread_id"] == "old-sid" and frames[-1]["result"]["response"] == "complete"
            assert log.read_text() == out.getvalue() and log.stat().st_mode & 0o777 == 0o600
    # New dispatch accepts op-only as well as the old action key, without HTTP setup.
    with tempfile.TemporaryDirectory() as td:
        daemon = d.SubagentDaemon(socket_path=Path(td) / "unused.sock")
        for key in ("action", "op"):
            left, right = socket.socketpair()
            left.sendall((json.dumps({key: "run", "prompt": "native"}) + "\n").encode())
            with left, patch.object(daemon, "_handle_run") as run:
                daemon._handle_client(right)
                assert run.call_count == 1 and run.call_args.args[1]["prompt"] == "native"
    print("PASS: five CLI legacy arguments, old-peer events, new action/op dispatch, local filtered logs")
