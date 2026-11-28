"""CLI argument routing, legacy wire compatibility, and event emission."""
import contextlib
import io
import inspect
import json
import os
from pathlib import Path
import queue
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from unittest.mock import Mock, call, patch

from _runtime_fixture import isolated_env, isolated_runtime, write_rpc_server
from agent_runtime import AgyAcpStream, CodexStream, DroidAcpStream, daemon, split_model_effort
from agent_runtime import cli_wrappers, daemon_client
from agent_runtime import codex_server_command, droid_server_command, grok_server_command, pi_server_command


def test_command_builders():
    codex_cmd = codex_server_command(
        [
            "/opt/homebrew/bin/codex",
            "exec",
            "-m",
            "gpt-6.2-sol",
            "-c",
            'model_reasoning_effort="xhigh"',
            "-c",
            'model_instructions_file="/tmp/sys.md"',
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--json",
            "hello",
        ]
    )
    assert codex_cmd == [
        "/opt/homebrew/bin/codex",
        "--dangerously-bypass-hook-trust",
        "app-server",
        "--stdio",
        "-c",
        'model_instructions_file="/tmp/sys.md"',
    ], f"Unexpected codex_cmd: {codex_cmd}"

    droid_cmd = droid_server_command(
        ["/test/bin/droid", "exec", "-m", "gpt-6.2-sol", "--append-system-prompt-file", "/tmp/a.md", "hi"]
    )
    assert droid_cmd == [
        "/test/bin/droid",
        "exec",
        "--auto",
        "high",
        "-o",
        "acp",
        "--append-system-prompt-file",
        "/tmp/a.md",
    ], f"Unexpected droid_cmd: {droid_cmd}"

    grok_cmd = grok_server_command(["/test/bin/grok", "-p", "hi"])
    assert grok_cmd == ["/test/bin/grok", "agent", "--always-approve", "stdio"]

    pi_cmd = pi_server_command(["/opt/homebrew/bin/pi", "-p", "--mode", "json", "hi"])
    assert pi_cmd == ["/opt/homebrew/bin/pi", "--mode", "rpc"], f"Unexpected pi_cmd: {pi_cmd}"
    pi_cmd_flags = pi_server_command(["/opt/homebrew/bin/pi", "--no-skills", "--no-mcp"])
    assert pi_cmd_flags == ["/opt/homebrew/bin/pi", "--mode", "rpc", "--no-skills", "--no-mcp"]
    print("PASS: command builders for codex, droid, grok, pi")


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
    with isolated_runtime() as home:
        _check_jq(home)
    print("PASS: CLI delta filtering, debug opt-in and complete final/result")


def _check_jq(home):
    jq = shutil.which("jq")
    if jq:
        frames = [dict(type="item.completed", item=dict(type="error", **fields)) for fields in (
            {}, {"message": None}, {"message": "bypass-hook-trust notice"}, {"message": "visible warning"},
        )] + [dict(type="turn.completed")]
        view = Path(__file__).resolve().parents[1] / "bin/sa-view.jq"
        output = subprocess.run([jq, "-Rrf", str(view)], text=True, capture_output=True, check=True, timeout=10,
                                env=isolated_env(home), input="".join(json.dumps(frame) + "\n" for frame in frames))
        assert output.stdout.splitlines() == ["⚠ null", "⚠ null", "⚠ visible warning", "■ 完成"], output
        assert not output.stderr
    else:
        print("SKIP: sa-view.jq stream regression (jq not installed)")


def client_transition_cases():
    core = {"agent", "model", "effort", "service_tier", "mode", "session_id", "cwd", "prompt", "config", "image_paths", "log_file"}
    ignored_args = {key: "ignored" for key in inspect.signature(daemon.run_subagent_client).parameters if key not in core}
    package = Path(__file__).resolve().parents[1]
    # Every CLI still accepts legacy no-op arguments without passing them through.
    with isolated_runtime():
        for engine in ("agy", "codex", "droid", "grok", "pi"):
            argv = ["sa-" + engine, "--prompt", "exact prompt"]
            for key, value in ignored_args.items():
                argv.extend(["--" + key, value])
            with patch.object(sys, "argv", argv), patch.object(cli_wrappers, "run_subagent_client", return_value=0) as call:
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
                with patch.object(daemon_client, "ensure_daemon_running", return_value=sock), patch.object(daemon_client.signal, "signal"), \
                        patch.dict(os.environ, SA_CODEX_EMIT_DELTAS=debug), contextlib.redirect_stdout(out):
                    rc = daemon.run_subagent_client(agent="codex", model="mock", prompt="exact prompt",
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
        server_daemon = daemon.SubagentDaemon(socket_path=Path(td) / "unused.sock")
        for key in ("action", "op"):
            left, right = socket.socketpair()
            left.sendall((json.dumps({key: "run", "prompt": "native"}) + "\n").encode())
            with left, patch.object(server_daemon, "_handle_run") as run:
                server_daemon._handle_client(right)
                assert run.call_count == 1 and run.call_args.args[1]["prompt"] == "native"
    print("PASS: five CLI legacy arguments, old-peer events, new action/op dispatch, local filtered logs")


def _check_codex_payloads():
    with isolated_runtime() as root:
        script = write_rpc_server(root, "fake-codex", protocol="custom", setup=r'''
def handle(msg):
    if "id" not in msg:
        return
    method = msg["method"]
    with open("requests.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(msg) + "\n")
    result = {
        "initialize": {},
        "thread/start": {"thread": {"id": "thread-1"}},
        "turn/start": {"turn": {"id": "turn-1"}},
    }[method]
    if method == "initialize":
        send(None)
        send(["valid JSON, but not a protocol event"])
    send({"id": msg["id"], "result": result})
    if method == "turn/start":
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
        return False
''')
        for bot, model, effort, tier in (
            ({"model": "gpt-6.2-sol:xhigh"}, "gpt-6.2-sol", "xhigh", None),
            ({"model": "gpt-6.2-sol:xhigh", "service_tier": "fast"}, "gpt-6.2-sol", "xhigh", "fast"),
            ({"model": "gpt-6.2-astra:xhigh"}, "gpt-6.2-astra", "xhigh", None),
            ({"model": "gpt-6.2-sol:low"}, "gpt-6.2-sol", "low", None),
            ({"model": "gpt-6.2-sol:xhigh", "reasoning_effort": " MEDIUM "}, "gpt-6.2-sol", "medium", None),
            ({"model": "gpt-6.2-sol"}, "gpt-6.2-sol", "xhigh", None),
            ({}, None, "xhigh", None),
        ):
            (root / "requests.jsonl").write_text("")
            stream = CodexStream([str(script)], cwd=str(root), env=os.environ.copy(), bot=bot, prompt="hello", rpc_timeout=2)
            try:
                assert stream.process.wait(timeout=5) == 0, bot
                assert any(e["type"] == "turn.completed" for e in stream.iter_events()), bot
            finally:
                stream.close()
                if stream.process.poll() is None:
                    stream.process.kill()
                    stream.process.wait(timeout=5)
            requests = [json.loads(line) for line in (root / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
            assert [r["method"] for r in requests] == ["initialize", "thread/start", "turn/start"], requests
            assert requests[1]["params"]["model"] == model, (bot, requests[1])
            assert requests[2]["params"]["effort"] == effort, (bot, requests[2])
            if tier is None:
                assert "serviceTier" not in requests[1]["params"], (bot, requests[1])
                assert "serviceTier" not in requests[2]["params"], (bot, requests[2])
            else:
                assert requests[1]["params"]["serviceTier"] == tier, (bot, requests[1])
                assert requests[2]["params"]["serviceTier"] == tier, (bot, requests[2])


def _check_droid_config():
    stream = object.__new__(DroidAcpStream)
    stream.agent, stream.session_id = "droid", "droid-1"
    session = {"models": {"availableModels": [{"modelId": "gpt-6.2-sol"}, {"modelId": "gpt-6.2-astra"}]},
               "configOptions": [{"id": "reasoning_effort"}]}
    for bot, model, effort in (
        ({"model": "gpt-6.2-sol:xhigh"}, "gpt-6.2-sol", "xhigh"),
        ({"model": "gpt-6.2-sol-low"}, "gpt-6.2-sol", "low"),
        ({"model": "gpt-6.2-sol-xhigh"}, "gpt-6.2-sol", "xhigh"),
        ({"model": "gpt-6.2-sol-xhigh", "reasoning_effort": "MEDIUM"}, "gpt-6.2-sol", "medium"),
        ({}, "gpt-6.2-astra", "xhigh"),
    ):
        stream._rpc = Mock()
        assert stream._configure_session(bot, session) == f"{model}:{effort}", bot
        assert stream._rpc.call_args_list == [
            call("session/set_model", {"sessionId": "droid-1", "modelId": model}),
            call("session/set_config_option", {"sessionId": "droid-1", "configId": "reasoning_effort", "value": effort}),
            call("session/set_mode", {"sessionId": "droid-1", "modeId": "auto-high"}),
        ], bot


def _check_cli_dispatch():
    sol, astra = "gpt-6.2-sol", "gpt-6.2-astra"
    shared_cases = [
        (["-m", f"{sol}:xhigh"], sol, "xhigh"),
        (["--model", f"{astra}:xhigh"], astra, "xhigh"),
        (["-m", f"{sol}:low"], sol, "low"),
        (["-m", f"{sol}:xhigh", "--effort", "medium"], sol, "medium"),
    ]
    extras = {
        "codex": [
            ([], sol, "high"),
            (["-m", f"{sol}:xhigh", "-c", 'model_reasoning_effort="MEDIUM"'], sol, "medium"),
            (["-m", f"{sol}:xhigh", "-c", "model_reasoning_effort=low", "--effort", "high"], sol, "high"),
        ],
        "droid": [
            ([], astra, "xhigh"),
            (["-m", f"{astra}:xhigh", "-r", "high"], astra, "high"),
        ],
        "pi": [
            ([], "claude-opus-5-5", "high"),
            (["-m", "provider/model:free"], "provider/model:free", "high"),
            (["-m", "provider/model:ultra"], "provider/model:ultra", "high"),
            (["-m", "provider/model:max"], "provider/model:max", "high"),
            (["-m", "provider/model:ultra:minimal"], "provider/model:ultra", "minimal"),
            (["-m", "provider/model:free:low"], "provider/model:free", "low"),
            (["-m", "provider/model:free:low", "--thinking", "off"], "provider/model:free", "off"),
        ],
    }
    for engine, extra in extras.items():
        script = Path(__file__).resolve().parents[1] / "bin" / f"sa-{engine}"
        with patch.object(cli_wrappers, "run_subagent_client", return_value=0) as dispatch, patch.object(sys, "path", sys.path[:]):
            main = runpy.run_path(str(script))["main"]
            for flags, model, effort in shared_cases + extra:
                dispatch.reset_mock()
                with patch.object(sys, "argv", [str(script), *flags, "hello"]):
                    assert main() == 0
                dispatch.assert_called_once()
                sent = dispatch.call_args.kwargs
                assert (sent["agent"], sent["model"], sent["effort"]) == (engine, model, effort), (flags, sent)
            if engine == "codex":
                for flags, expected_tier, expected_cfg in (
                    (["-m", sol, "--effort", "xhigh", "--fast"], "fast", []),
                    (["-m", sol, "--no-fast"], "", []),
                    (["-m", sol, "--no-fast", "-c", 'service_tier="fast"'], "", []),
                    (["-m", sol, "-c", 'service_tier="fast"'], "fast", []),
                    (["-m", sol, "-c", 'service_tier=""'], "", []),
                    (["-m", sol, "--service-tier", "FLEX"], "flex", []),
                    (["-m", sol, "--service-tier", ""], "", []),
                    (["-m", sol, "--fast", "-c", "foo=bar"], "fast", ["foo=bar"]),
                    (["-m", sol], "fast", []),
                    (["-m", astra, "--effort", "xhigh"], "fast", []),
                ):
                    dispatch.reset_mock()
                    with patch.object(sys, "argv", [str(script), *flags, "hello"]):
                        assert main() == 0
                    sent = dispatch.call_args.kwargs
                    assert (sent["service_tier"], sent["config"]) == (expected_tier, expected_cfg), (flags, sent)
                for bad_flags in (
                    ["-m", sol, "--fast", "--service-tier", "flex"],
                    ["-m", sol, "--fast", "--no-fast"],
                    ["-m", sol, "--no-fast", "--service-tier", "fast"],
                ):
                    try:
                        with patch.object(sys, "argv", [str(script), *bad_flags, "hello"]):
                            main()
                        raise AssertionError(f"expected conflict error for {bad_flags}")
                    except SystemExit as exc:
                        assert exc.code == 2


def model_effort_cases():
    assert split_model_effort("  gpt-6.2-sol:XHIGH  ", " ") == ("gpt-6.2-sol", "xhigh")
    assert split_model_effort("gpt-6.2-sol:xhigh", " MeDiuM ") == ("gpt-6.2-sol", "medium")
    assert split_model_effort("vendor:gpt-6.2-sol:xhigh") == ("vendor:gpt-6.2-sol", "xhigh")
    assert split_model_effort("gpt-6.2-sol:custom", default_effort="HIGH") == ("gpt-6.2-sol:custom", "high")
    assert split_model_effort(":xhigh", default_model="gpt-6.2-sol") == ("gpt-6.2-sol", "xhigh")
    assert split_model_effort(None, default_model=" gpt-6.2-sol ", default_effort=" XHIGH ") == ("gpt-6.2-sol", "xhigh")
    assert split_model_effort("model:HUGE", valid_efforts=("huge",)) == ("model", "huge")
    assert AgyAcpStream.model_id({"model": "gpt-oss-120b", "reasoning_effort": "high"}) == "gpt-oss-120b-high"
    assert AgyAcpStream.model_id({"model": "gemini-4-neon-xhigh"}) == "gemini-4-neon-xhigh"
    assert AgyAcpStream.model_id({"model": "gemini-4-neon-medium"}) == "gemini-4-neon-medium"
    assert AgyAcpStream.model_id({"model": "gemini-4-neon", "reasoning_effort": "xhigh"}) == "gemini-4-neon-xhigh"
    _check_codex_payloads()
    _check_droid_config()
    with isolated_runtime():
        _check_cli_dispatch()
    print("PASS: model:effort parsing, Codex wire payloads, Droid/Agy ACP configuration and CLI dispatch")


def codex_cases():
    from agent_runtime.codex_stream import CodexStream
    from agent_runtime.cli_events import should_emit
    for enabled in (False, True):
        stream = CodexStream.__new__(CodexStream)
        stream._emit_cumulative = enabled
        stream._events, stream._message_text = queue.Queue(), {}
        stream.tool_token = None
        stream._redact_stream_chunk = lambda text: text
        for _ in range(1000):
            stream._notification('item/agentMessage/delta', dict(itemId='long', delta='x' * 100))
        assert stream._events.qsize() == (1000 if enabled else 0)
        assert bool(stream._message_text) == enabled
        stream._notification('item/completed', dict(item=dict(id='long', type='agentMessage', text='x' * 100000)))
        stream._notification('item/started', dict(item=dict(type='commandExecution', command='echo ok')))
        stream._notification('error', dict(error='synthetic error'))
        stream.terminal_error = ''
        stream._events.put(None)
        with patch.object(json, 'dumps', side_effect=AssertionError('dict path must not serialize')):
            events = list(stream.iter_events())
        assert len(events) == (1003 if enabled else 3)
        assert events[-3]['item']['text'] == 'x' * 100000 and 'delta' not in events[-3]
        assert events[-2]['item']['type'] == 'command_execution' and events[-1]['type'] == 'error'
    for kind in ('error', 'command_execution', 'future_item'):
        assert should_emit(dict(type='item.completed', delta=True, item=dict(type=kind)), False)
    print('PASS: suppressed quadratic Codex accumulation, lossless opt-in, dict transport, final/tool/error retention')


if __name__ == "__main__":
    codex_cases()
    cli_emission_cases()
    client_transition_cases()
    model_effort_cases()
    test_command_builders()
