"""Isolated regressions for model IDs and reasoning effort across the CLI and transports."""
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
from unittest.mock import Mock, call, patch

from agent_runtime import AgyAcpStream, CodexStream, DroidAcpStream, daemon, split_model_effort


def _check_codex_payloads():
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "fake-codex"
        script.write_text(
            """#!/usr/bin/env python3
import json
import sys

with open("requests.jsonl", "w", encoding="utf-8") as log:
    for line in sys.stdin:
        msg = json.loads(line)
        method = msg["method"]
        if "id" not in msg:
            continue
        log.write(line)
        result = {
            "initialize": {},
            "thread/start": {"thread": {"id": "thread-1"}},
            "turn/start": {"turn": {"id": "turn-1"}},
        }[method]
        print(json.dumps({"id": msg["id"], "result": result}), flush=True)
        if method == "turn/start":
            print(json.dumps({"method": "turn/completed", "params": {"turn": {"status": "completed"}}}), flush=True)
            break
""",
            encoding="utf-8",
        )
        script.chmod(0o700)
        for bot, model, effort, tier in (
            ({"model": "gpt-6.2-sol:xhigh"}, "gpt-6.2-sol", "xhigh", None),
            ({"model": "gpt-6.2-sol:xhigh", "service_tier": "fast"}, "gpt-6.2-sol", "xhigh", "fast"),
            ({"model": "gpt-6.2-astra:xhigh"}, "gpt-6.2-astra", "xhigh", None),
            ({"model": "gpt-6.2-sol:low"}, "gpt-6.2-sol", "low", None),
            ({"model": "gpt-6.2-sol:xhigh", "reasoning_effort": " MEDIUM "}, "gpt-6.2-sol", "medium", None),
            ({"model": "gpt-6.2-sol"}, "gpt-6.2-sol", "xhigh", None),
            ({}, None, "xhigh", None),
        ):
            stream = CodexStream([str(script)], cwd=td, env=os.environ.copy(), bot=bot, prompt="hello", rpc_timeout=2)
            try:
                assert stream.process.wait(timeout=5) == 0, bot
                assert any(e["type"] == "turn.completed" for e in stream.iter_events()), bot
            finally:
                stream.close()
                if stream.process.poll() is None:
                    stream.process.kill()
                    stream.process.wait(timeout=5)
            requests = [json.loads(line) for line in (Path(td) / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
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
    }
    for engine, extra in extras.items():
        script = Path(__file__).resolve().parents[1] / "bin" / f"sa-{engine}"
        with patch.object(daemon, "run_subagent_client", return_value=0) as dispatch, patch.object(sys, "path", sys.path[:]):
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
    _check_cli_dispatch()
    print("PASS: model:effort parsing, Codex wire payloads, Droid/Agy ACP configuration and CLI dispatch")
