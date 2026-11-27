#!/usr/bin/env python3
"""Regression checks across the handoff parser, daemon and installer."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parent
sys.path.insert(0, str(RUNTIME))

from agent_runtime import daemon
from agent_runtime.session_handoff import _unwrap_handoff_prompt, format_handoff_prompt


class HandoffTests(unittest.TestCase):
    def test_out_of_order_and_isolated_markers_stay_in_prompt(self):
        opening = '<previous_session_history from_session="old">'
        closing = '</previous_session_history>'
        instruction = '【本轮新指令】：'
        for markers in (
            (opening,), (closing,), (instruction,), (opening, closing),
            (closing, opening, instruction),
            (instruction, opening, closing),
            (opening, instruction, closing),
        ):
            with self.subTest(markers=markers):
                prompt = '\n'.join(('任意正文', *markers, '继续保留'))
                self.assertEqual(_unwrap_handoff_prompt(prompt), ([], prompt))

    def test_unwraps_valid_handoff_and_preserves_markers_in_new_prompt(self):
        old_turn = {"prompt": "历史问题", "response": "两行\n历史回复", "ts": 12.5}
        prompt = "请解释 </previous_session_history> 这个标签"
        wrapped = format_handoff_prompt("old-sid", [old_turn], prompt)
        self.assertEqual(_unwrap_handoff_prompt(wrapped, 12.5), ([old_turn], prompt))

    def test_skips_round_with_reversed_prompt_and_response_markers(self):
        wrapped = format_handoff_prompt("old-sid", [{"prompt": "old", "response": "reply"}], "new")
        wrapped = wrapped.replace("[主代理输入]:\nold\n[子代理回复]:\nreply",
                                  "[子代理回复]:\nreply\n[主代理输入]:\nold")
        self.assertEqual(_unwrap_handoff_prompt(wrapped), ([], "new"))


class DaemonTests(unittest.TestCase):
    def test_query_distinguishes_missing_socket_from_connection_error(self):
        with mock.patch.object(daemon, "get_socket_path") as get_path:
            get_path.return_value.exists.return_value = False
            self.assertEqual(daemon.query_daemon("status"), {"ok": False, "status": "stopped"})
            get_path.return_value.exists.return_value = True
            with (
                mock.patch.object(daemon, "_verify_socket_security"),
                mock.patch.object(daemon.socket, "socket") as socket_factory,
            ):
                conn = socket_factory.return_value.__enter__.return_value
                conn.connect.side_effect = ConnectionRefusedError("refused")
                self.assertEqual(daemon.query_daemon("status"), {
                    "ok": False, "status": "error", "error": "refused",
                })

    def test_status_and_stop_exit_codes(self):
        for command, action in (("status", "status"), ("stop", "shutdown")):
            for status, code in (("error", 1), ("stopped", 0)):
                response = {"ok": False, "status": status}
                stdout = io.StringIO()
                with (
                    self.subTest(command=command, status=status),
                    mock.patch.object(daemon, "query_daemon", return_value=response) as query,
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(daemon.main([command]), code)
                    query.assert_called_once_with(action)
                    self.assertEqual(json.loads(stdout.getvalue()), response)

    def test_restart_checks_replacement_daemon(self):
        running = {"ok": True, "daemon_pid": 101}
        replacement = {"ok": True, "daemon_pid": 102}
        cases = (
            (running, running, 1),
            (running, replacement, 0),
            (running, {"ok": False, "status": "error"}, 1),
            ({"ok": False, "status": "stopped"}, replacement, 0),
        )
        for before, after, code in cases:
            stdout, calls = io.StringIO(), mock.Mock()
            still_running = after == running
            with (
                self.subTest(before=before, after=after),
                mock.patch.object(daemon, "get_socket_path") as socket_path,
                mock.patch.object(daemon, "_ping_daemon", return_value=still_running),
                mock.patch.object(daemon.time, "monotonic", side_effect=(0.0, 0.1, 3.0)),
                mock.patch.object(daemon.time, "sleep"),
                mock.patch.object(daemon, "ensure_daemon_running") as ensure,
                mock.patch.object(daemon, "query_daemon", side_effect=(before, {"ok": True}, after.copy())) as query,
                redirect_stdout(stdout),
            ):
                socket_path.return_value.exists.return_value = still_running
                calls.attach_mock(query, "query")
                calls.attach_mock(ensure, "ensure")
                self.assertEqual(daemon.main(["restart"]), code)
                self.assertEqual(calls.mock_calls, [
                    mock.call.query("status"), mock.call.query("shutdown"),
                    mock.call.ensure(), mock.call.query("status"),
                ])
                got = json.loads(stdout.getvalue())
                if code == 0:
                    self.assertEqual(got, after)
                else:
                    self.assertFalse(got["ok"])
                    self.assertTrue(got.get("error"))
        # Error before restart fails immediately without pretending daemon was stopped.
        stdout = io.StringIO()
        with (
            mock.patch.object(daemon, "query_daemon", return_value={"ok": False, "status": "error", "error": "boom"}),
            mock.patch.object(daemon, "ensure_daemon_running") as ensure,
            redirect_stdout(stdout),
        ):
            self.assertEqual(daemon.main(["restart"]), 1)
            ensure.assert_not_called()
            self.assertEqual(json.loads(stdout.getvalue())["error"], "boom")

    def test_failure_to_write_ledger_keeps_successful_result(self):
        def stream(*args, **kwargs):
            return iter((
                {"event": "init", "conversation_id": "fake-sid"},
                {"type": "text_delta", "delta": "第一行\n"},
                {"type": "text_delta", "delta": "second line", "used_tokens": 12},
            ))

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            instance = daemon.SubagentDaemon(state / "daemon.sock")
            conn, stderr = mock.Mock(), io.StringIO()
            with (
                mock.patch.object(daemon.threading.Thread, "start"),
                mock.patch.object(instance, "_acquire_resident", return_value=(None, None)),
                mock.patch.object(daemon, "build_server_cmd_and_stream_cls", return_value=([], stream)),
                mock.patch.object(daemon, "prepare_session_handoff", return_value=None),
                mock.patch.object(daemon, "terminate_proc_group"),
                mock.patch.object(daemon, "record_turn_completion", side_effect=OSError("ledger disk full")) as record,
                redirect_stderr(stderr),
            ):
                instance._handle_run(conn, {"agent": "codex", "prompt": "test", "cwd": tmp})
            response = "第一行\nsecond line"
            record.assert_called_once_with("codex", "fake-sid", "test", response, 12, None, state)
            frames = [json.loads(call.args[0]) for call in conn.sendall.call_args_list]
            self.assertEqual([frame for frame in frames if frame["type"] == "result"], [{
                "type": "result", "agent": "codex", "conversation_id": "fake-sid",
                "result": {"status": "SUCCESS", "response": response, "ledger_warning": "ledger disk full"},
            }])
            self.assertIn("ledger disk full", stderr.getvalue())


class InstallTests(unittest.TestCase):
    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def make_source(self, source):
        source.mkdir(parents=True)
        shutil.copy2(REPO / "install.sh", source)
        self.write(source / "plugin.json", '{"name": "agy-cockpit"}')
        self.write(source / "hooks.json", "{}")
        self.write(source / "rules/AGENTS.md", "rules")
        self.write(source / "sidecars/cockpit/server.py", "pass\n")
        self.write(source / "runtime/bin/sa-agy", "#!/usr/bin/env python3\npass\n")
        self.write(source / "runtime/guard/sa-guard.sh", "#!/bin/sh\n:\n")
        self.write(source / "runtime/tests/sentinel.py", "# keep source tests\n")
        return source

    @staticmethod
    def install(source, target, home):
        return subprocess.run(
            ["bash", str(source / "install.sh"), "--allow-custom-target", str(target)],
            env={**os.environ, "HOME": str(home)},
            capture_output=True, text=True, timeout=30,
        )

    def test_rejects_both_directions_of_source_target_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root / "source")
            child, home = source / "child", root / "home"
            for target in (root, child):
                self.write(target / "plugin.json", '{"name": "agy-cockpit"}')
            sentinel = source / "runtime/tests/sentinel.py"
            for target in (source, child, root):
                with self.subTest(target=target):
                    result = self.install(source, target, home)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("目标路径与源码仓重叠", result.stderr)
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "# keep source tests\n")
                    self.assertEqual(sorted(p.name for p in child.iterdir()), ["plugin.json"])

    def test_rejects_symlinks_in_managed_paths_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            source = self.make_source(root / "source")
            outside = self.write(root / "outside/keep", "untouched")
            data = self.write(home / ".gemini/antigravity/sidecar_data/keep", "untouched")
            for idx, (rel, destination) in enumerate((
                ("rules", outside.parent),
                ("runtime/bin", data.parent),
                ("skills/cockpit-dispatch/scripts/dangling", root / "absent"),
                ("sidecars/cockpit/nested/in-target", None),
            )):
                with self.subTest(rel=rel):
                    target = root / f"target-{idx}"
                    manifest = self.write(target / "plugin.json", '{"name": "agy-cockpit", "old": true}')
                    link = target / rel
                    link.parent.mkdir(parents=True, exist_ok=True)
                    destination = destination if destination is not None else target
                    link.symlink_to(destination, target_is_directory=destination.is_dir())
                    result = self.install(source, target, home)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("受管路径不能是符号链接", result.stderr)
                    self.assertTrue(link.is_symlink())
                    self.assertTrue(json.loads(manifest.read_text(encoding="utf-8"))["old"])
                    for sentinel in (outside, data):
                        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")

    def test_rejects_new_target_whose_managed_tree_resolves_to_sidecar_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, target = root / "home", root / "target"
            source = self.make_source(root / "source")
            data = home / ".gemini/antigravity/sidecar_data"
            data.parent.mkdir(parents=True)
            data.symlink_to(target / "sidecars/cockpit", target_is_directory=True)
            result = self.install(source, target, home)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("命中 sidecar_data", result.stderr)
            self.assertFalse(target.exists())

    def test_rejects_invalid_python_entrypoints_json_and_shell_before_writes(self):
        invalid = (
            ("runtime/bin/sa-agy", "#!/usr/bin/env python3\ndef broken(\n"),
            ("plugin.json", "{"),
            ("hooks.json", "{"),
            ("runtime/guard/sa-guard.sh", "#!/bin/sh\nif true; then\n"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            for idx, (rel, contents) in enumerate(invalid):
                with self.subTest(rel=rel):
                    source = self.make_source(root / f"source-{idx}")
                    self.write(source / rel, contents)
                    result = self.install(source, target, root / "home")
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("[2/4]", result.stdout)
                    self.assertIn(Path(rel).name, result.stderr)
                    self.assertFalse(target.exists())

    def test_installs_real_source_to_temporary_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            result = self.install(REPO, target, root / "home")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("manifest verified", result.stdout)
            self.assertEqual((target / "plugin.json").read_bytes(), (REPO / "plugin.json").read_bytes())
            self.assertTrue(os.access(target / "runtime/bin/sa-agy", os.X_OK))
            self.assertFalse((target / "runtime/tests").exists())

    def test_rejects_case_alias_overlap_and_allows_unrelated_sibling_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root / "source")
            alias = source.with_name("SOURCE")
            if alias.exists() and alias.samefile(source):
                res = self.install(source, alias, root / "home")
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn("与源码仓重叠", res.stderr)
            target = root / "target"
            outside = self.write(root / "outside/note.txt", "ok")
            sibling = target / "skills/other-skill"
            sibling.parent.mkdir(parents=True, exist_ok=True)
            sibling.symlink_to(outside.parent, target_is_directory=True)
            self.write(target / "plugin.json", '{"name": "agy-cockpit"}')
            ok = self.install(source, target, root / "home")
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)


if __name__ == "__main__":
    unittest.main()
