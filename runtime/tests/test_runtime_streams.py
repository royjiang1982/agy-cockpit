"""Transport lifecycle and drop-in compatibility checks."""
import json
import os
import sys
import shutil
import time

from _runtime_fixture import TIMEOUT, isolated_runtime, write_rpc_server
from agent_runtime import AgyAcpStream, DroidAcpStream, GrokAcpStream, PiRpcStream, ResidentServer
from agent_runtime import terminate_proc_group


def test_acp_and_pi_resident_streams():
    with isolated_runtime() as tmp:
        fake_acp = write_rpc_server(tmp, "fake_acp.py", setup='''
            startup = True
            pending_roots = {}
            original_prompt = handle_prompt
            def handle_prompt(rid, sid, text):
                if text.endswith("hello 1"):
                    pending_roots[sid] = rid
                    update(sid, text, "user_message_chunk")
                    return  # Keep the root prompt open until steering is received.
                if text == "follow up":
                    update(sid, text, "user_message_chunk")
                    original_prompt(rid, sid, text)
                    reply(pending_roots.pop(sid), {"stopReason": "end_turn"})
                else:
                    original_prompt(rid, sid, text)
        ''')
        fake_pi = write_rpc_server(tmp, "fake_pi.py", protocol="pi", setup='''
            original_handle = handle
            def handle(message):
                if message.get("type") in ("set_model", "set_thinking_level"):
                    with open("pi-wire.jsonl", "a") as wire:
                        wire.write(json.dumps(message) + chr(10))
                return original_handle(message)
        ''')
        pi_wire = tmp / "pi-wire.jsonl"

        env = os.environ.copy()
        # 1. Test ResidentServer multiplexed across AgyAcpStream, DroidAcpStream, GrokAcpStream
        for agent, cls, model, effort in [
            ("agy", AgyAcpStream, "gemini-4-carbon-medium", "medium"),
            ("droid", DroidAcpStream, "gpt-6.2-sol", "xhigh"),
            ("grok", GrokAcpStream, "grok-4.7", "xhigh"),
        ]:
            host = ResidentServer(
                [sys.executable, str(fake_acp)],
                cwd=str(tmp),
                env=env,
                agent=agent,
            )
            try:
                turn1 = cls(
                    [sys.executable, str(fake_acp)],
                    cwd=str(tmp),
                    env=env,
                    bot={"model": model, "reasoning_effort": effort},
                    prompt="hello 1",
                    resident=host,
                    is_subagent=True,
                    rpc_timeout=TIMEOUT,
                )
                assert turn1._ready.wait(TIMEOUT), "ACP did not deliver the root echo"
                assert turn1.steer("follow-up-id", "follow up")
                turn1.wait(TIMEOUT)
                events1 = [json.loads(line) for line in turn1]
                turn1.close()
                assert {"type": "item.started", "item": {"type": "user_message", "clientId": "follow-up-id"}} in events1, events1
                assert any(e.get("type") == "text_delta" and "reply:sid-1" in e.get("delta", "") for e in events1), events1

                # Reuse same ResidentServer for a second turn
                turn2 = cls(
                    [sys.executable, str(fake_acp)],
                    cwd=str(tmp),
                    env=env,
                    bot={"model": model, "reasoning_effort": effort},
                    prompt="hello 2",
                    resident=host,
                    is_subagent=True,
                )
                events2 = [json.loads(line) for line in turn2]
                turn2.close()
                assert any(e.get("type") == "text_delta" and "reply:sid-2" in e.get("delta", "") for e in events2), events2
                assert not host.busy
            finally:
                host.shutdown()

        # 2. Test PiRpcStream over ResidentServer
        pi_host = ResidentServer(
            [sys.executable, str(fake_pi)],
            cwd=str(tmp),
            env=env,
            agent="pi",
        )
        try:
            for bot, model_id, level in (
                ({"model": "claude-opus-5-5", "reasoning_effort": "high"}, "claude-opus-5-5", "high"),
                ({"model": "provider/model:free"}, "model:free", "high"),
                ({"model": "provider/model:ultra"}, "model:ultra", "high"),
                ({"model": "provider/model:max:minimal"}, "model:max", "minimal"),
                ({"model": "provider/model:free:xhigh", "reasoning_effort": "low"}, "model:free", "low"),
                ({"model": "provider/model:free", "reasoning_effort": "ultra"}, "model:free", None),
            ):
                pi_wire.unlink(missing_ok=True)
                pturn = PiRpcStream(
                    [sys.executable, str(fake_pi)],
                    cwd=str(tmp),
                    env=env,
                    bot=bot,
                    prompt="hello pi",
                    resident=pi_host,
                    is_subagent=True,
                )
                pevents = [json.loads(line) for line in pturn]
                pturn.close()
                assert any(e.get("type") == "text_delta" and e.get("delta") == "pi-ok" for e in pevents), pevents
                init = next(e["init"] for e in pevents if e.get("event") == "init")
                provider, separator, _ = bot["model"].partition("/")
                expected_model = f"{provider}/{model_id}" if separator else model_id
                assert init["model"] == (f"{expected_model}:{level}" if level else expected_model), pevents
                set_model, *thinking = map(json.loads, pi_wire.read_text().splitlines())
                assert (set_model["type"], set_model["modelId"]) == ("set_model", model_id)
                assert [(m["type"], m["level"]) for m in thinking] == (
                    [("set_thinking_level", level)] if level else []
                )
        finally:
            pi_host.shutdown()

    print("PASS: ResidentServer multiplexing (agy, droid, grok, pi)")


def test_pi_failures():
    with isolated_runtime() as tmp:
        env = os.environ.copy()
        fake_pi_err = write_rpc_server(
            tmp, "fake_pi_error.py", protocol="pi",
            setup='sid = "pi-err-1"\nprompt_error = "Upstream quota exceeded"',
        )
        pi_err_turn = PiRpcStream(
            [sys.executable, str(fake_pi_err)],
            cwd=str(tmp),
            env=env,
            bot={"reasoning_effort": "high"},
            prompt="trigger error",
        )
        try:
            for _ in pi_err_turn:
                pass
            raise AssertionError("Expected PiRpcStream to raise RuntimeError on message_end error!")
        except RuntimeError as exc:
            assert "Upstream quota exceeded" in str(exc), exc
        finally:
            pi_err_turn.close()
            terminate_proc_group(pi_err_turn.process)
        assert pi_err_turn.returncode == 1

        # Missing Pi session file should fail immediately instead of silently creating a new session
        pi_missing_turn = PiRpcStream(
            [sys.executable, str(fake_pi_err)],
            cwd=str(tmp),
            env=env,
            bot={"session_id": str(tmp / "nonexistent_session.jsonl")},
            prompt="resume missing",
        )
        try:
            for _ in pi_missing_turn:
                pass
            raise AssertionError("Expected missing Pi session file to raise RuntimeError!")
        except RuntimeError as exc:
            assert "不存在" in str(exc), exc
        finally:
            pi_missing_turn.close()
            terminate_proc_group(pi_missing_turn.process)
        assert pi_missing_turn.returncode == 1

    print("PASS: Pi stream failure propagation and missing resume file")


def test_zomibot_drop_in_compatibility():
    """Verify drop-in contracts required by zomi-bot without importing external repositories."""
    from agent_runtime.native_questions import NativeQuestions, handle_card, publish

    with isolated_runtime() as tmp:
        markers_dir = tmp / "markers"
        # 1. Default inject_document_token=True vs inject_document_token=False
        captured_envs = []
        captured_init = []

        class DummyProc:
            def __init__(self, cmd, **kw):
                captured_envs.append(dict(kw.get("env") or {}))
                self.pid = 424242
                self.returncode = None
                self.stdin = self
                self.stdout = self
                import queue as _q

                self._out = _q.Queue()

            def write(self, s):
                msg = json.loads(s)
                if msg.get("method") == "initialize":
                    captured_init.append(msg)
                    self._out.put(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {"protocolVersion": 1, "agentCapabilities": {}},
                            }
                        )
                    )
                elif msg.get("method") == "session/new":
                    self._out.put(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {
                                    "sessionId": "sid-res-1",
                                    "models": {"availableModels": [{"modelId": "gemini-4-carbon-high"}]},
                                },
                            }
                        )
                    )
                elif msg.get("method") in ("session/set_model", "session/set_mode"):
                    self._out.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}))
                elif msg.get("method") == "session/prompt":
                    self._out.put(
                        json.dumps(
                            {"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}
                        )
                    )

            def flush(self):
                pass

            def close(self):
                self._out.put(None)

            def __iter__(self):
                while True:
                    item = self._out.get()
                    if item is None:
                        return
                    yield item

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                self.returncode = -15
                self.close()

            def kill(self):
                self.returncode = -9
                self.close()

        # Default ResidentServer is business-neutral (strips ZOMI_DOCUMENT_TOKEN, client_name="agent-runtime")
        host_sa = ResidentServer(
            ["agy", "acp"],
            cwd=str(tmp),
            env={"ZOMI_DOCUMENT_TOKEN": "leaked-token"},
            agent="agy",
            popen=DummyProc,
        )
        assert "ZOMI_DOCUMENT_TOKEN" not in captured_envs[-1]
        assert captured_init[-1]["params"]["clientInfo"]["name"] == "agent-runtime"
        host_sa.shutdown()

        # Explicit zomi-bot mode injects ZOMI_DOCUMENT_TOKEN and uses client_name="zomi-bot"
        host_zb = ResidentServer(
            ["agy", "acp"],
            cwd=str(tmp),
            env={"ZOMI_DOCUMENT_TOKEN": "old-caller-token"},
            agent="agy",
            popen=DummyProc,
            inject_document_token=True,
            client_name="zomi-bot",
        )
        assert captured_envs[-1]["ZOMI_DOCUMENT_TOKEN"] == host_zb.lease_token
        assert captured_envs[-1]["ZOMI_DOCUMENT_TOKEN"] != "old-caller-token"
        assert captured_init[-1]["params"]["clientInfo"]["name"] == "zomi-bot"

        # Subagent resident turn creates marker with 0o700 dir; terminate() on resident turn does NOT kill host
        turn_res = AgyAcpStream(
            ["agy", "acp"],
            cwd=str(tmp),
            env={},
            bot={},
            prompt="hi",
            resident=host_zb,
            is_subagent=True,
            popen=DummyProc,
        )
        evts = [json.loads(x) for x in turn_res]
        assert evts[0]["event"] == "init" and evts[0]["conversation_id"] == "sid-res-1"
        turn_res.terminate()
        assert host_zb.process.poll() is None, "ResidentTurn.terminate() must not kill shared ResidentServer!"
        assert (markers_dir.stat().st_mode & 0o777) == 0o700
        host_zb.shutdown()

        # Non-resident turn with is_subagent=True must NOT create resident marker file and honors client_name
        non_res_turn = AgyAcpStream(
            ["agy", "acp"],
            cwd=str(tmp),
            env={},
            bot={},
            prompt="hi",
            is_subagent=True,
            popen=DummyProc,
            client_name="zomi-bot",
        )
        for _ in non_res_turn:
            pass
        non_res_turn.close()
        assert not (markers_dir / "sid-res-1").exists()
        assert captured_init[-1]["params"]["clientInfo"]["name"] == "zomi-bot"

        # NativeQuestions exports publish & handle_card
        assert callable(publish) and callable(handle_card) and NativeQuestions is not None
    print("PASS: zomi-bot drop-in contract compatibility (tokens, markers, init order, resident terminate, NativeQuestions)")


def test_real_cli_resident_handshakes():
    """Verify real local CLI binaries initialize cleanly in ResidentServer without LLM calls."""
    if os.environ.get("SA_TEST_LIVE_CLI") != "1":
        print("SKIP: live CLI handshakes (set SA_TEST_LIVE_CLI=1 explicitly)")
        return
    with isolated_runtime({"SA_SUBAGENT": "1", "GROK_SUBAGENTS": "0"}) as root:
        env = os.environ.copy()
        for agent, cmd_builder in [
            ("agy", lambda b: [b, "acp"]),
            ("codex", lambda b: [b, "--dangerously-bypass-hook-trust", "app-server", "--stdio"]),
            ("droid", lambda b: [b, "exec", "--auto", "high", "-o", "acp"]),
            ("grok", lambda b: [b, "agent", "--always-approve", "stdio"]),
            ("pi", lambda b: [b, "--mode", "rpc", "--no-context-files", "--no-skills", "--no-mcp", "--no-prompt-templates", "--no-themes"]),
        ]:
            binary = shutil.which(agent)
            assert binary, f"Missing CLI binary for {agent}"
            t0 = time.monotonic()
            host = ResidentServer(
                cmd_builder(binary),
                cwd=str(root),
                env=env,
                agent=agent,
                rpc_timeout=20,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            try:
                assert host.initialized is not None
                assert host.process.poll() is None
                print(f"PASS: live ResidentServer handshake for [{agent}] in {elapsed_ms:.0f}ms (pid={host.process.pid})")
            finally:
                host.shutdown()


if __name__ == "__main__":
    test_acp_and_pi_resident_streams()
    test_pi_failures()
    test_zomibot_drop_in_compatibility()
    test_real_cli_resident_handshakes()
