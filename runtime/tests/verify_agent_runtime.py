#!/usr/bin/env python3
"""Comprehensive verification suite for `_shared/agent-runtime` across all 5 CLI engines."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

os.environ.setdefault("SA_AGY_QUOTA_PREFLIGHT", "0")
os.environ["SA_DISPATCH_POLICY_PATH"] = os.devnull
PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))

from agent_runtime import (
    AgyAcpStream,
    CodexStream,
    DroidAcpStream,
    GrokAcpStream,
    PiRpcStream,
    ResidentServer,
    SubagentDaemon,
    codex_server_command,
    droid_server_command,
    find_cli_binary,
    grok_server_command,
    pi_server_command,
    terminate_proc_group,
)


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
        ["/Users/royjiang/.local/bin/droid", "exec", "-m", "gpt-6.2-sol", "--append-system-prompt-file", "/tmp/a.md", "hi"]
    )
    assert droid_cmd == [
        "/Users/royjiang/.local/bin/droid",
        "exec",
        "--auto",
        "high",
        "-o",
        "acp",
        "--append-system-prompt-file",
        "/tmp/a.md",
    ], f"Unexpected droid_cmd: {droid_cmd}"

    grok_cmd = grok_server_command(["/Users/royjiang/.local/bin/grok", "-p", "hi"])
    assert grok_cmd == ["/Users/royjiang/.local/bin/grok", "agent", "--always-approve", "stdio"]

    pi_cmd = pi_server_command(["/opt/homebrew/bin/pi", "-p", "--mode", "json", "hi"])
    assert pi_cmd == ["/opt/homebrew/bin/pi", "--mode", "rpc"], f"Unexpected pi_cmd: {pi_cmd}"
    pi_cmd_flags = pi_server_command(["/opt/homebrew/bin/pi", "--no-skills", "--no-mcp"])
    assert pi_cmd_flags == ["/opt/homebrew/bin/pi", "--mode", "rpc", "--no-skills", "--no-mcp"]
    print("PASS: command builders for codex, droid, grok, pi")


def _make_fake_acp_script(tmp_dir: Path, engine_name: str) -> Path:
    script = tmp_dir / f"fake_{engine_name}.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys, time

# Emit a startup notification without id to verify ResidentServer.__init__ ignores it
print(json.dumps({"jsonrpc": "2.0", "method": "_startup/notice", "params": {"info": "booting"}}), flush=True)

seq = 0
for raw in sys.stdin:
    msg = json.loads(raw)
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True}},
            }
        }), flush=True)
    elif method in ("session/new", "session/load"):
        seq += 1
        sid = params.get("sessionId") or f"sid-{seq}"
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "sessionId": sid,
                "models": {"availableModels": [
                    {"modelId": "gemini-4-carbon-medium"},
                    {"modelId": "gpt-6.2-sol"},
                    {"modelId": "grok-4.7"},
                ]},
                "configOptions": [
                    {"id": "reasoning_effort", "options": [{"value": "medium"}, {"value": "high"}, {"value": "xhigh"}]}
                ]
            }
        }), flush=True)
    elif method in ("session/set_model", "session/set_mode", "session/set_config_option"):
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}), flush=True)
    elif method == "session/prompt":
        sid = params["sessionId"]
        prompt_text = params["prompt"][0]["text"]
        if "WAIT_FOR_CANCEL" in prompt_text:
            print(json.dumps({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "working..."}
                    }
                }
            }), flush=True)
            # Wait until session/cancel arrives
            continue
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": f"reply:{sid}"}
                }
            }
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"stopReason": "end_turn"}
        }), flush=True)
    elif method == "session/cancel":
        with open(sys.argv[1], "w") as cf:
            cf.write(params.get("sessionId", "cancelled"))
        print(json.dumps({"jsonrpc": "2.0", "id": 999, "result": {"stopReason": "cancelled"}}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _make_fake_pi_script(tmp_dir: Path) -> Path:
    script = tmp_dir / "fake_pi.py"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys

sid = "pi-sid-1"
for raw in sys.stdin:
    msg = json.loads(raw)
    rid = msg.get("id")
    ctype = msg.get("type")
    if ctype == "get_state":
        print(json.dumps({
            "id": rid, "type": "response", "command": "get_state", "success": True,
            "data": {"sessionId": sid, "sessionFile": f"/tmp/{sid}.jsonl"}
        }), flush=True)
    elif ctype in ("new_session", "switch_session", "set_thinking_level", "set_model"):
        if ctype == "new_session":
            sid = "pi-sid-2"
        print(json.dumps({
            "id": rid, "type": "response", "command": ctype, "success": True,
            "data": {"cancelled": False}
        }), flush=True)
    elif ctype == "get_available_models":
        print(json.dumps({
            "id": rid, "type": "response", "command": "get_available_models", "success": True,
            "data": {"models": [{"provider": "anthropic", "id": "claude-opus-5-5", "name": "Claude Opus 5.5"}]}
        }), flush=True)
    elif ctype == "prompt":
        print(json.dumps({
            "id": rid, "type": "response", "command": "prompt", "success": True,
            "data": {"disposition": "started"}
        }), flush=True)
        print(json.dumps({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "pi-ok"}
        }), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_acp_and_pi_streams_and_daemon_cancel():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cancel_marker = tmp / "cancelled.txt"
        fake_acp = _make_fake_acp_script(tmp, "acp")
        fake_pi = _make_fake_pi_script(tmp)

        prev_markers = os.environ.get("ZOMI_SUBAGENT_SESSIONS_DIR")
        os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = str(tmp / "markers")
        env = os.environ.copy()
        try:
            # 1. Test ResidentServer multiplexed across AgyAcpStream, DroidAcpStream, GrokAcpStream
            for agent, cls, model, effort in [
                ("agy", AgyAcpStream, "gemini-4-carbon-medium", "medium"),
                ("droid", DroidAcpStream, "gpt-6.2-sol", "xhigh"),
                ("grok", GrokAcpStream, "grok-4.7", "xhigh"),
            ]:
                host = ResidentServer(
                    [sys.executable, str(fake_acp), str(cancel_marker)],
                    cwd=str(tmp),
                    env=env,
                    agent=agent,
                )
                try:
                    turn1 = cls(
                        [sys.executable, str(fake_acp), str(cancel_marker)],
                        cwd=str(tmp),
                        env=env,
                        bot={"model": model, "reasoning_effort": effort},
                        prompt="hello 1",
                        resident=host,
                        is_subagent=True,
                    )
                    events1 = [json.loads(line) for line in turn1]
                    turn1.close()
                    assert any(e.get("type") == "text_delta" and "reply:sid-1" in e.get("delta", "") for e in events1), events1

                    # Reuse same ResidentServer for a second turn
                    turn2 = cls(
                        [sys.executable, str(fake_acp), str(cancel_marker)],
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
                pturn = PiRpcStream(
                    [sys.executable, str(fake_pi)],
                    cwd=str(tmp),
                    env=env,
                    bot={"model": "claude-opus-5-5", "reasoning_effort": "high"},
                    prompt="hello pi",
                    resident=pi_host,
                    is_subagent=True,
                )
                pevents = [json.loads(line) for line in pturn]
                pturn.close()
                assert any(e.get("type") == "text_delta" and e.get("delta") == "pi-ok" for e in pevents), pevents
            finally:
                pi_host.shutdown()

            # 3. Test SubagentDaemon client EOF immediate cancellation
            sock_path = tmp / "daemon.sock"
            daemon = SubagentDaemon(socket_path=sock_path, idle_timeout=60)
            import agent_runtime.daemon as daemon_mod

            orig_builder = daemon_mod.build_server_cmd_and_stream_cls
            daemon_mod.build_server_cmd_and_stream_cls = lambda ag, cfg=None: (
                [sys.executable, str(fake_acp), str(cancel_marker)],
                AgyAcpStream,
            )
            srv_thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_thread.start()
            try:
                deadline = time.monotonic() + 3.0
                while not sock_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)

                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(str(sock_path))
                req = {
                    "action": "run",
                    "agent": "agy",
                    "model": "gemini-4-carbon-medium",
                    "cwd": str(tmp),
                    "prompt": "WAIT_FOR_CANCEL",
                }
                conn.sendall((json.dumps(req) + "\n").encode("utf-8"))
                # Read until thinking event so we know session/prompt is active on server
                with conn.makefile("r", encoding="utf-8") as rf:
                    first_frame = json.loads(rf.readline())
                    assert first_frame["event"]["event"] == "init"
                    second_frame = json.loads(rf.readline())
                    assert second_frame["event"]["type"] == "thinking"
                # Abruptly close client socket (simulating manage_task kill)
                conn.close()

                deadline = time.monotonic() + 3.0
                while not cancel_marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert cancel_marker.exists(), "Daemon did not send session/cancel on client EOF!"
            finally:
                daemon_mod.build_server_cmd_and_stream_cls = orig_builder
                daemon.shutdown_all()
                srv_thread.join(timeout=2.0)
        finally:
            if prev_markers is None:
                os.environ.pop("ZOMI_SUBAGENT_SESSIONS_DIR", None)
            else:
                os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = prev_markers

    print("PASS: ResidentServer multiplexing (agy, droid, grok, pi) & SubagentDaemon EOF cancellation")


def test_review_regressions():
    """Verify all 8 items caught during adversarial review by sa-codex and sa-grok."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prev_markers = os.environ.get("ZOMI_SUBAGENT_SESSIONS_DIR")
        os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = str(tmp / "markers")
        try:
            # 1. Cross-pool & same-pool active session_id collision rejection in SubagentDaemon
            fake_acp = _make_fake_acp_script(tmp, "collision")
            cancel_marker = tmp / "cancel_col.txt"
            daemon = SubagentDaemon(socket_path=tmp / "col.sock", idle_timeout=60)
            env = os.environ.copy()
            cmd = [sys.executable, str(fake_acp), str(cancel_marker)]
            dir_a = tmp / "dir_a"
            dir_b = tmp / "dir_b"
            dir_a.mkdir()
            dir_b.mkdir()
            try:
                h1, inbox1 = daemon._acquire_resident("agy", str(dir_a), cmd, "shared-sid-1", env)
                # Same session_id in same cwd or different cwd must be rejected instead of spawning 2nd server
                for target_dir in (dir_a, dir_b):
                    try:
                        daemon._acquire_resident("agy", str(target_dir), cmd, "shared-sid-1", env)
                        raise AssertionError("Expected duplicate active session_id to be rejected!")
                    except RuntimeError as exc:
                        assert "占用" in str(exc), exc
                h1.release(inbox1)
            finally:
                daemon.shutdown_all()

            # 2. Codex multi-segment agent_message concatenation in SubagentDaemon._handle_run
            sock_path = tmp / "codex_multi.sock"
            daemon2 = SubagentDaemon(socket_path=sock_path, idle_timeout=60)
            import agent_runtime.daemon as daemon_mod

            class FakeMultiMsgCodexStream:
                def __init__(self, *args, **kwargs):
                    self.thread_id = "th-multi"
                    self.is_resident_turn = True
                    self._inbox = kwargs.get("inbox")
                    self.resident = kwargs.get("resident")

                def __iter__(self):
                    yield json.dumps({"type": "thread.started", "thread_id": "th-multi"}) + "\n"
                    yield json.dumps({
                        "type": "item.completed",
                        "delta": True,
                        "item": {"id": "msg-1", "type": "agent_message", "text": "First segment. "},
                    }) + "\n"
                    yield json.dumps({
                        "type": "item.completed",
                        "item": {"id": "msg-1", "type": "agent_message", "text": "First segment. "},
                    }) + "\n"
                    yield json.dumps({
                        "type": "item.completed",
                        "delta": True,
                        "item": {"id": "msg-2", "type": "agent_message", "text": "Second segment."},
                    }) + "\n"
                    yield json.dumps({
                        "type": "item.completed",
                        "item": {"id": "msg-2", "type": "agent_message", "text": "Second segment."},
                    }) + "\n"
                    yield json.dumps({"type": "turn.completed"}) + "\n"

                def poll(self):
                    return 0

                def close(self):
                    if self.resident and self._inbox:
                        self.resident.release(self._inbox)

            orig_builder = daemon_mod.build_server_cmd_and_stream_cls
            daemon_mod.build_server_cmd_and_stream_cls = lambda ag, cfg=None: (
                cmd,
                FakeMultiMsgCodexStream,
            )
            srv_thread = threading.Thread(target=daemon2.serve_forever, daemon=True)
            srv_thread.start()
            try:
                deadline = time.monotonic() + 3.0
                while not sock_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(str(sock_path))
                conn.sendall(
                    (json.dumps({"action": "run", "agent": "codex", "cwd": str(tmp), "prompt": "hi"}) + "\n").encode()
                )
                frames = [json.loads(line) for line in conn.makefile("r", encoding="utf-8") if line.strip()]
                conn.close()
                res_frame = [f for f in frames if f.get("type") == "result"][-1]
                assert res_frame["result"]["response"] == "First segment. Second segment.", res_frame
            finally:
                daemon_mod.build_server_cmd_and_stream_cls = orig_builder
                daemon2.shutdown_all()
                srv_thread.join(timeout=2.0)

            # 3. PiRpcStream error / extension_ui_request / missing session file failure propagation
            fake_pi_err = tmp / "fake_pi_err.py"
            fake_pi_err.write_text(
                """#!/usr/bin/env python3
import json, sys
for raw in sys.stdin:
    msg = json.loads(raw)
    rid = msg.get("id")
    ctype = msg.get("type")
    if ctype == "get_state":
        print(json.dumps({"id": rid, "type": "response", "success": True, "data": {"sessionId": "pi-err-1", "sessionFile": "/tmp/pi-err-1.jsonl"}}), flush=True)
    elif ctype == "new_session":
        print(json.dumps({"id": rid, "type": "response", "success": True, "data": {"cancelled": False}}), flush=True)
    elif ctype == "set_thinking_level":
        print(json.dumps({"id": rid, "type": "response", "success": True, "data": {}}), flush=True)
    elif ctype == "prompt":
        print(json.dumps({"id": rid, "type": "response", "success": True, "data": {"disposition": "started"}}), flush=True)
        print(json.dumps({"type": "message_end", "message": {"stopReason": "error", "errorMessage": "Upstream quota exceeded"}}), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
""",
                encoding="utf-8",
            )
            fake_pi_err.chmod(0o755)
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

            # 4. terminate_proc_group reaps SIGTERM-ignoring grandchild after leader exits
            grandchild_pid_file = tmp / "grandchild.pid"
            leader_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os, signal, subprocess, sys, time; "
                        "gc = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
                        f"open({str(grandchild_pid_file)!r}, 'w').write(str(gc.pid)); "
                        "time.sleep(30)"
                    ),
                ],
                start_new_session=True,
            )
            deadline = time.monotonic() + 3.0
            while not grandchild_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            gc_pid = int(grandchild_pid_file.read_text().strip())
            terminate_proc_group(leader_proc, wait_timeout=0.2)
            time.sleep(0.1)
            try:
                os.kill(gc_pid, 0)
                raise AssertionError(f"Grandchild {gc_pid} survived terminate_proc_group!")
            except ProcessLookupError:
                pass
        finally:
            if prev_markers is None:
                os.environ.pop("ZOMI_SUBAGENT_SESSIONS_DIR", None)
            else:
                os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = prev_markers

    print("PASS: all 8 adversarial review regression tests")


def test_zomibot_drop_in_compatibility():
    """Verify drop-in contracts required by zomi-bot without importing external repositories."""
    from agent_runtime.native_questions import NativeQuestions, handle_card, publish

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prev_markers = os.environ.get("ZOMI_SUBAGENT_SESSIONS_DIR")
        markers_dir = tmp / "markers"
        os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = str(markers_dir)
        try:
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
                    self._q = []
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
        finally:
            if prev_markers is None:
                os.environ.pop("ZOMI_SUBAGENT_SESSIONS_DIR", None)
            else:
                os.environ["ZOMI_SUBAGENT_SESSIONS_DIR"] = prev_markers

    print("PASS: zomi-bot drop-in contract compatibility (tokens, markers, init order, resident terminate, NativeQuestions)")


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val >= 0x80:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _encode_proto_bytes(fnum: int, data: bytes) -> bytes:
    return _encode_varint((fnum << 3) | 2) + _encode_varint(len(data)) + data


def _encode_proto_varint(fnum: int, val: int) -> bytes:
    return _encode_varint((fnum << 3) | 0) + _encode_varint(val)


def test_session_auto_handoff():
    """Verify auto-handoff on expired cache (>600s) + context > 200k across multi-turn ledger & SQLite/JSONL fallbacks."""
    import sqlite3
    import agent_runtime.daemon as daemon_mod
    from agent_runtime.session_handoff import (
        inspect_local_session,
        load_session_ledger,
        prepare_session_handoff,
        save_session_ledger,
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ledger_dir = tmp / "ledger"
        fake_home = tmp / "home"
        fake_home.mkdir(parents=True, exist_ok=True)
        sock_path = tmp / "handoff.sock"

        prev_env = {
            k: os.environ.get(k)
            for k in (
                "SA_SESSIONS_LEDGER_DIR",
                "SA_HANDOFF_TTL_SEC",
                "SA_HANDOFF_TOKEN_THRESHOLD",
                "SA_DISABLE_AUTO_HANDOFF",
                "HOME",
            )
        }
        os.environ["SA_SESSIONS_LEDGER_DIR"] = str(ledger_dir)
        os.environ["HOME"] = str(fake_home)
        os.environ.pop("SA_HANDOFF_TTL_SEC", None)
        os.environ.pop("SA_HANDOFF_TOKEN_THRESHOLD", None)
        os.environ.pop("SA_DISABLE_AUTO_HANDOFF", None)

        captured_calls = []
        next_tokens = [80_000]
        next_reply = ["R1"]
        sid_seq = [0]
        stream_delay = [0.0]
        fail_in_init = [False]
        fail_mid_stream = [False]

        class FakeHandoffStream:
            def __init__(self, cmd, *, cwd, env, bot, prompt, **kwargs):
                if fail_in_init[0]:
                    raise RuntimeError("simulated init failure before session assigned")
                req_sid = str(bot.get("session_id") or "")
                if req_sid:
                    self.session_id = req_sid
                else:
                    sid_seq[0] += 1
                    self.session_id = f"sid-{sid_seq[0]}"
                captured_calls.append({"session_id": req_sid, "assigned_sid": self.session_id, "prompt": prompt})
                self.is_resident_turn = True
                self.resident = kwargs.get("resident")
                self._inbox = kwargs.get("inbox")
                self._tok = next_tokens[0]
                self._reply = next_reply[0]
                self._delay = stream_delay[0]
                self._fail_mid = fail_mid_stream[0]

            def __iter__(self):
                yield json.dumps({"event": "init", "agent": "agy", "conversation_id": self.session_id}) + "\n"
                if self._delay > 0:
                    time.sleep(self._delay)
                if self._fail_mid:
                    yield json.dumps({"type": "native_question", "questions": []}) + "\n"
                    return
                yield json.dumps({"type": "text_delta", "delta": self._reply}) + "\n"
                yield json.dumps({"type": "turn.completed", "usage": {"total_tokens": self._tok}}) + "\n"

            def poll(self):
                return 0

            def close(self):
                if self.resident and self._inbox:
                    self.resident.release(self._inbox)

        fake_acp = _make_fake_acp_script(tmp, "handoff_host")
        cancel_marker = tmp / "cancel_ho.txt"
        daemon = SubagentDaemon(socket_path=sock_path, idle_timeout=60)
        orig_builder = daemon_mod.build_server_cmd_and_stream_cls
        daemon_mod.build_server_cmd_and_stream_cls = lambda ag, cfg=None: (
            [sys.executable, str(fake_acp), str(cancel_marker)],
            FakeHandoffStream,
        )
        srv_thread = threading.Thread(target=daemon.serve_forever, daemon=True)
        srv_thread.start()

        def run_req(sid: str, prompt_text: str, tok: int, reply_text: str):
            next_tokens[0] = tok
            next_reply[0] = reply_text
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(str(sock_path))
            conn.sendall(
                (
                    json.dumps(
                        {
                            "action": "run",
                            "agent": "agy",
                            "model": "gemini-4-carbon-medium",
                            "session_id": sid,
                            "cwd": str(tmp),
                            "prompt": prompt_text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            frames = [json.loads(line) for line in conn.makefile("r", encoding="utf-8") if line.strip()]
            conn.close()
            return frames

        try:
            deadline = time.monotonic() + 3.0
            while not sock_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)

            # Turn 1: initial new session -> sid-1 (80k tokens)
            f1 = run_req("", "Prompt Turn 1", 80_000, "Response Turn 1")
            assert f1[-1]["conversation_id"] == "sid-1"
            assert captured_calls[-1]["session_id"] == ""

            # Turn 2: resume sid-1 while cache warm & tokens < 200k -> no handoff, updates tokens to 210k
            f2 = run_req("sid-1", "Prompt Turn 2", 210_000, "Response Turn 2")
            assert f2[-1]["conversation_id"] == "sid-1"
            assert captured_calls[-1]["session_id"] == "sid-1"
            assert captured_calls[-1]["prompt"] == "Prompt Turn 2"
            assert not any(f.get("type") == "event" and f["event"].get("event") == "session_handoff" for f in f2)

            # Turn 3: resume sid-1 while tokens > 200k (210k) but cache still warm (idle < 600s) -> no handoff
            long_r3 = "Response Turn 3 " + ("LONG_PAYLOAD_" * 1500)
            f3 = run_req("sid-1", "Prompt Turn 3", 215_000, long_r3)
            assert f3[-1]["conversation_id"] == "sid-1"
            assert captured_calls[-1]["session_id"] == "sid-1"
            assert captured_calls[-1]["prompt"] == "Prompt Turn 3"

            # Verify warm/not-yet-10m (<=600s) with tokens > 200k does NOT handoff,
            # and expired cache (>600s) at exact boundary tokens == 200k does NOT handoff
            rec1 = load_session_ledger("sid-1")
            assert rec1 is not None and len(rec1["turns"]) == 3
            rec1["updated_at"] = time.time() - 500.0
            rec1["used_tokens"] = 215_000
            save_session_ledger(rec1)
            assert prepare_session_handoff("agy", "sid-1", "Check warm 500s") is None

            rec1["updated_at"] = time.time() - 700.0
            rec1["used_tokens"] = 200_000
            save_session_ledger(rec1)
            assert prepare_session_handoff("agy", "sid-1", "Check exact 200k boundary") is None

            # Age sid-1 (>600s) with used_tokens=215,000 (>200k) -> Turn 4 MUST trigger auto-handoff to sid-2
            rec1["updated_at"] = time.time() - 700.0
            rec1["used_tokens"] = 215_000
            save_session_ledger(rec1)

            f4 = run_req("sid-1", "Prompt Turn 4", 55_000, "Response Turn 4")
            assert f4[-1]["conversation_id"] == "sid-2", f4
            handoff_evts = [f["event"] for f in f4 if f.get("type") == "event" and f["event"].get("event") == "session_handoff"]
            assert len(handoff_evts) == 1, f4
            assert handoff_evts[0]["from_session_id"] == "sid-1"
            assert handoff_evts[0]["turns_carried"] == 3
            assert handoff_evts[0]["used_tokens"] == 215_000

            call4 = captured_calls[-1]
            assert call4["session_id"] == "", "Handoff must clear session_id passed to underlying stream!"
            p4 = call4["prompt"]
            assert '<previous_session_history from_session="sid-1" turns="3">' in p4
            assert "Prompt Turn 1" in p4 and "Response Turn 1" in p4
            assert "Prompt Turn 2" in p4 and "Response Turn 2" in p4
            assert "Prompt Turn 3" in p4 and long_r3 in p4, "Long response must not be truncated!"
            assert p4.endswith("【本轮新指令】：\nPrompt Turn 4")

            # Verify sid-2 ledger inherited all 3 prior turns + raw Turn 4 (no nested tags in ledger)
            rec2 = load_session_ledger("sid-2")
            assert rec2 is not None
            assert rec2["handoff_from"] == "sid-1"
            assert len(rec2["turns"]) == 4
            assert rec2["turns"][3]["prompt"] == "Prompt Turn 4"
            assert "<previous_session_history" not in rec2["turns"][3]["prompt"]

            # Age sid-2 and set tokens=220,000 -> Turn 5 triggers 2nd handoff (sid-2 -> sid-3) without nesting tags
            rec2["updated_at"] = time.time() - 750.0
            rec2["used_tokens"] = 220_000
            save_session_ledger(rec2)

            f5 = run_req("sid-2", "Prompt Turn 5", 60_000, "Response Turn 5")
            assert f5[-1]["conversation_id"] == "sid-3"
            call5 = captured_calls[-1]
            assert call5["session_id"] == ""
            p5 = call5["prompt"]
            assert '<previous_session_history from_session="sid-2" turns="4">' in p5
            assert p5.count("<previous_session_history") == 1, "Must never nest previous_session_history blocks!"
            for idx in range(1, 5):
                assert f"=== 第 {idx} 轮 ===" in p5
                assert f"Prompt Turn {idx}" in p5
            assert long_r3 in p5

            # Part 1b: Verify all 7 adversarial review fixes on daemon + ledger lifecycle
            # Fix 4 (Medium 5): Token compaction / lower token update must NOT be pinned to old max high-water mark
            rec3 = load_session_ledger("sid-3")
            assert rec3 is not None and rec3["used_tokens"] == 60_000
            rec3["used_tokens"] = 220_000
            save_session_ledger(rec3)
            # Subsequent warm turn reports compacted 28,000 tokens -> ledger must drop to 28,000 (not stay at 220,000)
            f5_compact = run_req("sid-3", "Prompt Turn 5b after compact", 28_000, "Response Turn 5b")
            assert f5_compact[-1]["result"]["status"] == "SUCCESS"
            rec3_after = load_session_ledger("sid-3")
            assert rec3_after is not None and rec3_after["used_tokens"] == 28_000, rec3_after
            rec3_after["updated_at"] = time.time() - 700.0
            save_session_ledger(rec3_after)
            assert prepare_session_handoff("agy", "sid-3", "Should not handoff after compact") is None

            # Fix 6 (Medium 7): Handoff triggered, but stream initialization fails BEFORE new sid is assigned -> returns requested_sid ("sid-3")
            rec3_after["updated_at"] = time.time() - 700.0
            rec3_after["used_tokens"] = 230_000
            save_session_ledger(rec3_after)
            fail_in_init[0] = True
            try:
                f_init_err = run_req("sid-3", "Prompt Init Fail", 50_000, "Never")
            finally:
                fail_in_init[0] = False
            assert f_init_err[-1]["result"]["status"] == "ERROR"
            assert f_init_err[-1]["conversation_id"] == "sid-3", f"Expected fallback to requested_sid 'sid-3', got {f_init_err[-1]}"

            # Fix 1 (High 1 + Medium 3): Concurrent requests with the same expired requested_sid ("sid-3") must be mutually exclusive
            calls_before_conc = len(captured_calls)
            stream_delay[0] = 0.25
            conc_results = [None, None]

            def _conc_worker(idx: int):
                conc_results[idx] = run_req("sid-3", f"Concurrent Prompt {idx}", 50_000, f"Concurrent Reply {idx}")

            t_c1 = threading.Thread(target=_conc_worker, args=(0,))
            t_c2 = threading.Thread(target=_conc_worker, args=(1,))
            t_c1.start()
            time.sleep(0.05)
            t_c2.start()
            t_c1.join(timeout=3.0)
            t_c2.join(timeout=3.0)
            stream_delay[0] = 0.0
            assert len(captured_calls) == calls_before_conc + 1, "Only ONE new session may be spawned for concurrent same-sid requests!"
            statuses = sorted(r[-1]["result"]["status"] for r in conc_results if r)
            assert statuses == ["ERROR", "SUCCESS"], conc_results
            err_frame = next(r[-1] for r in conc_results if r[-1]["result"]["status"] == "ERROR")
            assert "正在运行中，拒绝并发重入" in err_frame["result"]["error"]
            assert err_frame["conversation_id"] == "sid-3"
            ok_frame = next(r[-1] for r in conc_results if r[-1]["result"]["status"] == "SUCCESS")
            assert ok_frame["conversation_id"] == "sid-4"

            # Fix 2a (High 2): Mid-turn failure during handoff still seeds carried_turns to new session ledger (sid-5)
            rec4 = load_session_ledger("sid-4")
            assert rec4 is not None
            rec4["updated_at"] = time.time() - 700.0
            rec4["used_tokens"] = 240_000
            save_session_ledger(rec4)
            fail_mid_stream[0] = True
            try:
                f_mid_err = run_req("sid-4", "Prompt Turn Aborted Mid-Handoff", 50_000, "Partial")
            finally:
                fail_mid_stream[0] = False
            assert f_mid_err[-1]["result"]["status"] == "ERROR"
            assert f_mid_err[-1]["conversation_id"] == "sid-5"
            seeded_rec5 = load_session_ledger("sid-5")
            assert seeded_rec5 is not None, "Seeded ledger for sid-5 must exist even when turn aborted mid-stream!"
            assert seeded_rec5["handoff_from"] == "sid-4"
            assert len(seeded_rec5["turns"]) == len(rec4["turns"])
            # Resuming sid-5 on next turn must preserve all inherited turns + append the new turn
            f_recover5 = run_req("sid-5", "Prompt Turn Recovered on sid-5", 62_000, "Response Recovered")
            assert f_recover5[-1]["result"]["status"] == "SUCCESS"
            rec5_final = load_session_ledger("sid-5")
            assert rec5_final is not None and len(rec5_final["turns"]) == len(rec4["turns"]) + 1
            assert rec5_final["turns"][-1]["prompt"] == "Prompt Turn Recovered on sid-5"

            # Part 2: Verify fallback parsing of pre-existing local session files (SQLite & JSONL) without ledger
            old_ts = time.time() - 8000.0

            # 2a. agy SQLite DB fallback (WAL mode without -shm/-wal, multi-step assistant reply concatenation, and step_type=23 compaction)
            agy_db_dir = fake_home / ".gemini" / "antigravity-cli" / "conversations"
            agy_db_dir.mkdir(parents=True, exist_ok=True)
            agy_db = agy_db_dir / "legacy-agy-sid.db"
            con = sqlite3.connect(str(agy_db))
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB)")
            con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
            ts_blob = _encode_proto_bytes(8, _encode_proto_varint(1, int(old_ts)))
            u1_blob = _encode_proto_bytes(
                19,
                _encode_proto_bytes(
                    2,
                    "你是被派出的子代理（SA_SUBAGENT=1）。只遵守任务书，不派生子代理、不提交代码。\n\nLegacy Agy Q1".encode("utf-8"),
                ),
            )
            a1_part1_blob = _encode_proto_bytes(20, _encode_proto_bytes(1, b"Legacy Agy Finding A"))
            a1_tool_blob = _encode_proto_bytes(20, _encode_proto_bytes(7, b"tool_call_intermediate"))
            a1_final_blob = _encode_proto_bytes(20, _encode_proto_bytes(1, b"Legacy Agy A1"))
            u2_blob = _encode_proto_bytes(19, _encode_proto_bytes(3, _encode_proto_bytes(1, b"Legacy Agy Q2")))
            a2_final_blob = _encode_proto_bytes(20, _encode_proto_bytes(8, b"Legacy Agy A2"))
            con.executemany(
                "INSERT INTO steps VALUES (?, ?, 3, ?, ?)",
                [
                    (0, 14, ts_blob, u1_blob),
                    (1, 15, ts_blob, a1_part1_blob),
                    (2, 15, ts_blob, a1_tool_blob),
                    (3, 15, ts_blob, a1_final_blob),
                    (4, 14, ts_blob, u2_blob),
                    (5, 15, ts_blob, a2_final_blob),
                ],
            )
            usage_blob = _encode_proto_bytes(
                1,
                _encode_proto_bytes(4, _encode_proto_varint(2, 45_000) + _encode_proto_varint(5, 165_000)),
            )
            con.execute("INSERT INTO gen_metadata VALUES (1, ?)", (usage_blob,))
            con.commit()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            for suffix in ("-shm", "-wal"):
                p_side = Path(f"{agy_db}{suffix}")
                if p_side.exists():
                    p_side.unlink()
            assert not Path(f"{agy_db}-shm").exists() and not Path(f"{agy_db}-wal").exists()

            inspected_agy = inspect_local_session("agy", "legacy-agy-sid")
            assert inspected_agy is not None, "inspect_local_session failed on WAL-mode DB without -shm/-wal"
            assert inspected_agy["used_tokens"] == 210_000
            assert len(inspected_agy["turns"]) == 2

            ho_agy = prepare_session_handoff("agy", "legacy-agy-sid", "Agy Q3")
            assert ho_agy is not None
            assert ho_agy["used_tokens"] == 210_000
            assert ho_agy["event"]["turns_carried"] == 2
            assert ho_agy["carried_turns"][0]["prompt"] == "Legacy Agy Q1"
            assert ho_agy["carried_turns"][0]["response"] == "Legacy Agy Finding A\n\nLegacy Agy A1"
            assert ho_agy["carried_turns"][1]["prompt"] == "Legacy Agy Q2"
            assert ho_agy["carried_turns"][1]["response"] == "Legacy Agy A2"

            # Verify agy step_type=23 (compaction/checkpoint) ignores pre-compaction gen_metadata rows
            con = sqlite3.connect(str(agy_db))
            con.execute("INSERT INTO steps VALUES (6, 23, 3, ?, ?)", (ts_blob, b""))
            con.commit()
            con.close()
            inspected_agy_compact = inspect_local_session("agy", "legacy-agy-sid")
            assert inspected_agy_compact is not None and inspected_agy_compact["used_tokens"] <= 200_000
            assert prepare_session_handoff("agy", "legacy-agy-sid", "Agy Q3 after compact") is None

            # 2b. codex JSONL fallback (2-hour TTL = 7200s) + unwrapping handoff-wrapped prompt from JSONL without ledger
            codex_dir = fake_home / ".codex" / "sessions" / "2026" / "11" / "10"
            codex_dir.mkdir(parents=True, exist_ok=True)
            codex_jl = codex_dir / "rollout-legacy-codex-sid.jsonl"
            wrapped_codex_u2 = ho_agy["effective_prompt"].replace("Agy Q3", "Codex Unwrapped Q3")
            codex_jl.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for repo"}]}}),
                        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": wrapped_codex_u2}]}}),
                        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Codex Unwrapped A3"}]}}),
                        json.dumps({"type": "token_usage_record", "payload": {"usage": {"input_tokens": 205_000, "output_tokens": 2_000}}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(codex_jl, (old_ts, old_ts))
            ho_codex = prepare_session_handoff("codex", "legacy-codex-sid", "Codex Q4")
            assert ho_codex is not None and ho_codex["used_tokens"] == 207_000
            assert len(ho_codex["carried_turns"]) == 3, ho_codex["carried_turns"]
            assert ho_codex["carried_turns"][0]["prompt"] == "Legacy Agy Q1"
            assert ho_codex["carried_turns"][1]["prompt"] == "Legacy Agy Q2"
            assert ho_codex["carried_turns"][2] == {"prompt": "Codex Unwrapped Q3", "response": "Codex Unwrapped A3", "ts": old_ts}
            assert ho_codex["effective_prompt"].count("<previous_session_history") == 1

            # Verify JSONL compaction event resets used_tokens below threshold
            with open(codex_jl, "a", encoding="utf-8") as wf:
                wf.write(json.dumps({"type": "compaction", "usage": {"input_tokens": 165_000, "output_tokens": 1_200}}) + "\n")
            os.utime(codex_jl, (old_ts, old_ts))
            inspected_codex_compact = inspect_local_session("codex", "legacy-codex-sid")
            assert inspected_codex_compact is not None and inspected_codex_compact["used_tokens"] == 15_200
            assert prepare_session_handoff("codex", "legacy-codex-sid", "Codex Q4 after compact") is None

            # 2c. droid settings + JSONL fallback (lastCallTokenUsage priority & multi-part assistant reply)
            droid_dir = fake_home / ".factory" / "sessions" / "ws"
            droid_dir.mkdir(parents=True, exist_ok=True)
            droid_s = droid_dir / "legacy-droid-sid.settings.json"
            droid_j = droid_dir / "legacy-droid-sid.jsonl"
            droid_s.write_text(
                json.dumps(
                    {
                        "tokenUsage": {"inputTokens": 300_000, "cacheReadTokens": 100_000, "outputTokens": 10_000},
                        "lastCallTokenUsage": {"inputTokens": 105_000, "cacheReadTokens": 100_000, "outputTokens": 1_000},
                    }
                ),
                encoding="utf-8",
            )
            droid_j.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "<system-reminder>sys</system-reminder>"}, {"type": "text", "text": "Droid Q1"}]}}),
                        json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Droid Finding 1"}]}}),
                        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool out"}]}}),
                        json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "done"}, {"type": "text", "text": "Droid A1"}]}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(droid_s, (old_ts, old_ts))
            os.utime(droid_j, (old_ts, old_ts))
            ho_droid = prepare_session_handoff("droid", "legacy-droid-sid", "Droid Q2")
            assert ho_droid is not None and ho_droid["used_tokens"] == 206_000
            assert ho_droid["carried_turns"][0]["prompt"] == "Droid Q1"
            assert ho_droid["carried_turns"][0]["response"] == "Droid Finding 1\n\nDroid A1"

            # Verify Droid lastCallTokenUsage <= 200k prevents false handoff even when cumulative tokenUsage is 410k
            droid_s.write_text(
                json.dumps(
                    {
                        "tokenUsage": {"inputTokens": 300_000, "cacheReadTokens": 100_000, "outputTokens": 10_000},
                        "lastCallTokenUsage": {"inputTokens": 20_000, "cacheReadTokens": 25_000, "outputTokens": 500},
                    }
                ),
                encoding="utf-8",
            )
            os.utime(droid_s, (old_ts, old_ts))
            assert inspect_local_session("droid", "legacy-droid-sid")["used_tokens"] == 45_500
            assert prepare_session_handoff("droid", "legacy-droid-sid", "Droid Q2 low lastCall") is None

            # Part 3: Verify SubagentDaemon.shutdown_all unlinks socket BEFORE slow host shutdown & restart race-free
            restart_dir = tmp / "restart_daemon"
            restart_dir.mkdir(parents=True, exist_ok=True)
            prev_sub_dir = os.environ.get("SA_SUBAGENTD_DIR")
            os.environ["SA_SUBAGENTD_DIR"] = str(restart_dir)
            r_sock = restart_dir / "daemon.sock"
            slow_daemon = SubagentDaemon(socket_path=r_sock, idle_timeout=60)

            class SlowHost:
                closed = False
                agent = "agy"
                busy = False
                active_turns = set()
                session_inboxes = {}

                class process:
                    pid = 99999

                    @staticmethod
                    def poll():
                        return None

                def shutdown(self):
                    time.sleep(0.45)
                    self.closed = True

            slow_host = SlowHost()
            slow_daemon.all_hosts.add(slow_host)
            r_thread = threading.Thread(target=slow_daemon.serve_forever, daemon=True)
            r_thread.start()
            try:
                deadline = time.monotonic() + 3.0
                while not r_sock.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                rc = daemon_mod.main(["restart"])
                assert rc == 0
                # Wait for old slow_host.shutdown() to complete and verify new daemon socket was NOT unlinked
                r_thread.join(timeout=2.0)
                close_deadline = time.monotonic() + 2.0
                while not slow_host.closed and time.monotonic() < close_deadline:
                    time.sleep(0.02)
                assert slow_host.closed
                snap_after = daemon_mod.query_daemon("status")
                assert snap_after.get("ok") is True and snap_after.get("daemon_pid") != os.getpid(), snap_after
            finally:
                daemon_mod.query_daemon("shutdown")
                if prev_sub_dir is None:
                    os.environ.pop("SA_SUBAGENTD_DIR", None)
                else:
                    os.environ["SA_SUBAGENTD_DIR"] = prev_sub_dir
        finally:
            daemon_mod.build_server_cmd_and_stream_cls = orig_builder
            daemon.shutdown_all()
            srv_thread.join(timeout=2.0)
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    print("PASS: transparent cross-session auto-handoff (multi-turn ledger, no nesting, SQLite & JSONL fallbacks)")


def test_daemon_restart_validation():
    from contextlib import redirect_stdout
    from io import StringIO
    from unittest.mock import patch

    import agent_runtime.daemon as daemon

    with tempfile.TemporaryDirectory(prefix="sa-") as tmp, patch.dict(os.environ, {"SA_SUBAGENTD_DIR": tmp}):
        assert daemon.query_daemon("status") == {"ok": False, "status": "stopped"}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(daemon.get_socket_path()))
        stale = daemon.query_daemon("status")
        assert stale["ok"] is False and stale["status"] == "error" and stale["stale_socket"] is True, stale
        with patch.object(daemon.socket, "socket") as client:
            conn = client.return_value.__enter__.return_value
            for response in ([], {"ok": 1}, {"ok": "true"}, {"ok": False, "status": "stopped", "stale_socket": True},
                             {"ok": True, "status": "stopped"}):
                conn.recv.return_value = (json.dumps(response) + "\n").encode()
                checked = daemon.query_daemon("status")
                assert checked["ok"] is False and checked["status"] == "error" and "stale_socket" not in checked, checked

        bad_pids = (None, True, 0, -1, "123", 1.5)
        with redirect_stdout(StringIO()), patch.object(daemon, "ensure_daemon_running") as start:
            for pid in bad_pids:
                with patch.object(daemon, "query_daemon", return_value={"ok": True, "daemon_pid": pid}):
                    assert daemon.main(["restart"]) == 1, pid
            with patch.object(daemon, "query_daemon", return_value={"ok": False, "status": "error"}):
                assert daemon.main(["restart"]) == 1
            start.assert_not_called()
            before = {"ok": True, "daemon_pid": 123}
            for after in ({"ok": 1, "daemon_pid": 456}, *({"ok": True, "daemon_pid": pid} for pid in (*bad_pids, 123))):
                with patch.object(daemon, "query_daemon", side_effect=(before, {"ok": False}, after)):
                    assert daemon.main(["restart"]) == 1, after
            assert start.call_count == len(bad_pids) + 2
        try:
            assert daemon.main(["restart"]) == 0
            healthy = daemon.query_daemon("status")
            assert healthy["ok"] is True and type(healthy["daemon_pid"]) is int and healthy["daemon_pid"] > 0, healthy
        finally:
            daemon.query_daemon("shutdown")


def test_real_cli_resident_handshakes():
    """Verify real local CLI binaries initialize cleanly in ResidentServer without LLM calls."""
    if os.environ.get("SA_TEST_LIVE_CLI") != "1":
        print("SKIP: live CLI handshakes (set SA_TEST_LIVE_CLI=1 explicitly)")
        return
    env = os.environ.copy()
    env["SA_SUBAGENT"] = "1"
    env["GROK_SUBAGENTS"] = "0"
    cwd = "/Users/royjiang/Dev"

    for agent, cmd_builder in [
        ("agy", lambda b: [b, "acp"]),
        ("codex", lambda b: [b, "--dangerously-bypass-hook-trust", "app-server", "--stdio"]),
        ("droid", lambda b: [b, "exec", "--auto", "high", "-o", "acp"]),
        ("grok", lambda b: [b, "agent", "--always-approve", "stdio"]),
        ("pi", lambda b: [b, "--mode", "rpc", "--no-context-files", "--no-skills", "--no-mcp", "--no-prompt-templates", "--no-themes"]),
    ]:
        binary = find_cli_binary(agent)
        assert binary, f"Missing CLI binary for {agent}"
        t0 = time.monotonic()
        host = ResidentServer(
            cmd_builder(binary),
            cwd=cwd,
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
    from model_effort_cases import model_effort_cases
    from performance_cases import cli_emission_cases, client_transition_cases
    from p2_cases import session_cases, codex_cases
    from agy_session_new_cases import agy_session_new_cases
    from acp_approval_cases import acp_approval_cases
    from host_reaper_cases import host_reaper_cases
    session_cases()
    codex_cases()
    agy_session_new_cases()
    acp_approval_cases()
    host_reaper_cases()
    cli_emission_cases()
    client_transition_cases()
    model_effort_cases()
    test_command_builders()
    test_acp_and_pi_streams_and_daemon_cancel()
    test_review_regressions()
    test_zomibot_drop_in_compatibility()
    test_session_auto_handoff()
    test_daemon_restart_validation()
    test_real_cli_resident_handshakes()
    print("ALL AGENT-RUNTIME VERIFICATIONS PASSED.")
