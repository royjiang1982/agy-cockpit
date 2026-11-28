"""Subagent discovery, handoff identity, and last-prompt ordering."""
from contextlib import ExitStack
import json
import os
import shlex
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cockpit_io import BoundedCache
import droid_acp_fallback
import metric_agy
import metric_session
import pricing
import subagent_commands
import subagent_discovery
import subagent_session_index
import subagents
from subagent_payload import _parse_step132_payload
from test_orchestrator_recognition import _build_db, _encode_gen_metadata, _field_bytes, _field_varint


class SubagentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.app_dir = self.root / "app"
        (self.app_dir / "conversations").mkdir(parents=True)
        self.app_dirs = [("App", self.app_dir)]
        stack.enter_context(patch.dict(os.environ, {"HOME": str(self.root)}))
        stack.enter_context(patch.multiple(
            subagents, SUBAGENT_APP_DIRS=self.app_dirs,
            _BUILTIN_SUBAGENT_MODEL_ENUM=BoundedCache(16), _ANALYSIS_CACHE=BoundedCache(16),
        ))
        stack.enter_context(patch.multiple(
            subagent_discovery, APP_DIRS=self.app_dirs, _SUBAGENT_STUBS_CACHE=BoundedCache(16),
            _COMPLETED_STEP_STUBS=BoundedCache(16), _STEP_DEPENDENCIES=BoundedCache(16),
        ))
        stack.enter_context(patch.object(subagent_commands, "HOME", self.root))
        stack.enter_context(patch.multiple(
            metric_agy, SUBAGENT_APP_DIRS=self.app_dirs,
            _BUILTIN_SUBAGENT_MODEL_ENUM=subagents._BUILTIN_SUBAGENT_MODEL_ENUM,
        ))
        roots = dict(PI_SESSIONS_DIR=self.root / "pi", FACTORY_SESSIONS_DIR=self.root / "factory",
                     GROK_SESSIONS_DIR=self.root / "grok")
        stack.enter_context(patch.multiple(subagent_session_index, **roots))
        stack.enter_context(patch.multiple(metric_session, CODEX_SESSIONS_DIR=self.root / "codex", **roots))
        stack.enter_context(patch.object(pricing, "_OBSERVED_MODEL_CTX_LIMITS", {}))

    def test_discovery_hints_resolves_new_handoff_session_ids_for_all_engines(self):
        old_id = "11111111-1111-4111-8111-111111111111"
        new_id = "22222222-2222-4222-8222-222222222222"
        parent_id = "99999999-9999-4999-8999-999999999999"

        for agent, init_line in (
            ("agy", json.dumps({"event": "init", "agent": "agy", "conversation_id": new_id})),
            ("grok", json.dumps({"event": "init", "agent": "grok", "session_id": new_id})),
            ("droid", json.dumps({"event": "init", "agent": "droid", "session_id": new_id, "model": "claude-opus-5-5"})),
            ("codex", json.dumps({"type": "thread.started", "thread_id": new_id})),
        ):
            handoff_txt = json.dumps({"event": "session_handoff", "from_session_id": old_id}) + "\n" + init_line + "\n"
            prog, sid, _, _ = droid_acp_fallback.discovery_hints(handoff_txt, agent, None, "", "", parent_id)
            self.assertEqual(prog, agent)
            self.assertEqual(sid, new_id)

        self.assertIsNone(droid_acp_fallback.acp_metrics(None))

    def test_discover_subagent_stubs_tracks_handoff_new_id_and_pending_without_clobbering_old(self):
        parent_cid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        old_sid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        new_sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

        proj_dir = self.root / "proj"
        bdir = self.app_dir / "brain" / parent_cid / ".system_generated"
        (bdir / "steps" / "1").mkdir(parents=True, exist_ok=True)
        (bdir / "steps" / "2").mkdir(parents=True, exist_ok=True)
        (proj_dir / "tmp").mkdir(parents=True, exist_ok=True)

        rf1 = proj_dir / "tmp" / "opus.jsonl"
        rf1.write_text(
            json.dumps({"event": "init", "agent": "agy", "conversation_id": old_sid}) + "\n",
            encoding="utf-8",
        )
        rf2 = proj_dir / "tmp" / "opus-r2.jsonl"
        rf2.write_text(
            json.dumps({"event": "session_handoff", "from_session_id": old_sid})
            + "\n"
            + json.dumps({"event": "init", "agent": "agy", "conversation_id": new_sid})
            + "\n",
            encoding="utf-8",
        )
        (bdir / "steps" / "1" / "output.txt").write_text("■ 完成\n", encoding="utf-8")

        cd = shlex.quote(str(proj_dir))
        cmd1 = f"cd {cd} && sa-run.sh tmp/opus.jsonl sa-agy --model claude-opus-5-5-high --prompt hi"
        cmd2 = f"cd {cd} && sa-run.sh tmp/opus-r2.jsonl sa-agy --conversation {old_sid} --model claude-opus-5-5-high --prompt more"
        p1 = _field_bytes(1, b"run_command") + _field_bytes(2, json.dumps({"CommandLine": cmd1}).encode("utf-8"))
        p2 = _field_bytes(1, b"run_command") + _field_bytes(2, json.dumps({"CommandLine": cmd2}).encode("utf-8"))

        db_path = self.app_dir / "conversations" / f"{parent_cid}.db"
        _build_db(db_path, [], [(1, 132, 3, b"", p1), (2, 132, 2, b"", p2)])

        # Case 1: Handoff with init written -> keeps old_sid (done) AND adds new_sid (running)
        stubs = {s["id"]: s for s in subagent_discovery.discover_subagent_stubs(parent_cid)}
        self.assertIn(old_sid, stubs)
        self.assertIn(new_sid, stubs)
        self.assertTrue(stubs[old_sid]["has_done_marker"])
        self.assertEqual(stubs[new_sid]["step_status"], 2)
        self.assertFalse(stubs[new_sid]["has_done_marker"])

        # Case 2: Handoff announced before init arrives -> pending-2, does NOT clobber old_sid
        rf2.write_text(
            json.dumps({"event": "session_handoff", "from_session_id": old_sid}) + "\n",
            encoding="utf-8",
        )
        stubs_pending = {s["id"]: s for s in subagent_discovery.discover_subagent_stubs(parent_cid)}
        self.assertIn(old_sid, stubs_pending)
        self.assertIn("pending-2", stubs_pending)
        self.assertTrue(stubs_pending[old_sid]["has_done_marker"])

        # Case 3: Normal in-place resume (no session_handoff, no init yet) -> binds to old_sid
        rf2.write_text("", encoding="utf-8")
        stubs_resume = {s["id"]: s for s in subagent_discovery.discover_subagent_stubs(parent_cid)}
        self.assertEqual(list(stubs_resume.keys()), [old_sid])
        self.assertEqual(stubs_resume[old_sid]["step_status"], 2)

    def test_large_raw_tail_binds_terminal_without_stdout(self):
        parent = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        agy_sid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        codex_sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        droid_sid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        project = self.root / "project"
        (project / "tmp").mkdir(parents=True)
        bulk = b'{"type":"text_delta","delta":"' + "宽".encode() * 667_000 + b'"}\n'

        def event(**fields):
            return json.dumps(fields, ensure_ascii=False).encode() + b"\r\n"

        rows = []
        for idx, (engine, head, tail) in enumerate((
            ("agy", event(event="init", agent="agy", model="head-model"),
             event(event="result", agent="agy", conversation_id=agy_sid, result={"status": "SUCCESS"})),
            ("codex", b"", event(type="thread.started", thread_id=codex_sid) + event(type="turn.completed")),
            ("droid", event(event="init", agent="droid", conversation_id=droid_sid),
             event(type="turn.completed") + event(event="result", agent="droid", conversation_id=droid_sid,
                                                  result={"status": "ERROR", "error": "failed\u2028after\u2029turn"})),
        ), 1):
            raw = project / "tmp" / f"{engine}.jsonl"
            unfinished = b'{"event":"result","result":{"status":"ERROR"}}'
            raw.write_bytes(head + bulk + b"\xff\n" + tail + unfinished)
            cmd = f"sa-run.sh tmp/{engine}.jsonl sa-{engine} --prompt hi"
            args = json.dumps({"CommandLine": cmd, "Cwd": str(project)}).encode()
            rows.append((idx, 132, 2, b"", _field_bytes(1, b"run_command") + _field_bytes(2, args)))

        _build_db(self.app_dir / "conversations" / f"{parent}.db", [], rows)
        stubs = {stub["id"]: stub for stub in subagent_discovery.discover_subagent_stubs(parent)}
        self.assertEqual(set(stubs), {agy_sid, codex_sid, droid_sid})
        self.assertEqual(stubs[agy_sid]["model_hint"], "head-model")
        for sid in (agy_sid, codex_sid):
            self.assertTrue(stubs[sid]["has_done_marker"])
            self.assertFalse(stubs[sid]["has_err_marker"])
        self.assertTrue(stubs[droid_sid]["has_err_marker"])
        self.assertFalse(stubs[droid_sid]["has_done_marker"])

    def test_pi_timestamp_matches_nearest_unused_session_in_cwd(self):
        root = self.root / "pi"
        cwd = self.root / "project"
        slug = "--" + str(cwd).strip("/").replace("/", "-") + "--"
        parent = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        nearest = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        second = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        for directory, seconds, sid in (
            (slug, "11", nearest), (slug, "20", second), ("another-project", "10", parent),
        ):
            target = root / directory / f"2026-01-01T00-00-{seconds}-000Z_{sid}.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()

        started = 1767225610  # 2026-01-01T00:00:10Z
        payload = _field_bytes(1, b"run_command") + _field_bytes(
            2, json.dumps({"CommandLine": "pi -p hi", "Cwd": str(cwd)}).encode(),
        )
        _build_db(
            self.app_dir / "conversations" / f"{parent}.db", [],
            [(idx, 132, 2, _field_bytes(1, _field_varint(1, started)), payload) for idx in (1, 2)],
        )
        stubs = subagent_discovery.discover_subagent_stubs(parent)
        self.assertEqual([(stub["step_idx"], stub["id"]) for stub in stubs], [(1, nearest), (2, second)])

    def test_subagents_sorted_by_last_prompt_time_and_send_message_bumps_order(self):
        parent_cid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        sub_a = "11111111-aaaa-4aaa-8aaa-111111111111"
        sub_b = "22222222-bbbb-4bbb-8bbb-222222222222"

        def _meta_ts(sec: int) -> bytes:
            return _field_bytes(1, _field_varint(1, sec))

        bdir = self.app_dir / "brain" / parent_cid / ".system_generated"
        (bdir / "steps" / "1").mkdir(parents=True, exist_ok=True)
        (bdir / "steps" / "2").mkdir(parents=True, exist_ok=True)
        (bdir / "steps" / "1" / "output.txt").write_text(
            json.dumps({"conversationId": sub_a}) + "\n", encoding="utf-8"
        )
        (bdir / "steps" / "2" / "output.txt").write_text(
            json.dumps({"conversationId": sub_b}) + "\n", encoding="utf-8"
        )

        # sub_a launched earlier (t=100) but finished much later (t=500)
        _build_db(
            self.app_dir / "conversations" / f"{sub_a}.db",
            [_encode_gen_metadata(1, 1532, 1000, 2000, 300)],
            [(0, 14, 3, _meta_ts(100), b""), (1, 15, 3, _meta_ts(500), b"")],
        )
        # sub_b launched later (t=200) and finished earlier (t=250)
        _build_db(
            self.app_dir / "conversations" / f"{sub_b}.db",
            [_encode_gen_metadata(1, 1532, 1000, 2000, 300)],
            [(0, 14, 3, _meta_ts(200), b""), (1, 15, 3, _meta_ts(250), b"")],
        )

        p_a = _field_bytes(1, b"invoke_subagent") + _field_bytes(2, json.dumps({"Role": "SlowA"}).encode("utf-8"))
        p_b = _field_bytes(1, b"invoke_subagent") + _field_bytes(2, json.dumps({"Role": "FastB"}).encode("utf-8"))
        p_msg_a = _field_bytes(1, b"send_message") + _field_bytes(
            2, json.dumps({"Recipient": sub_a, "Message": "follow up"}).encode("utf-8")
        )

        db_path = self.app_dir / "conversations" / f"{parent_cid}.db"
        _build_db(db_path, [], [(1, 132, 3, _meta_ts(100), p_a), (2, 132, 3, _meta_ts(200), p_b)])

        # Use explicit mtimes so invalidation never depends on fixture write timing.
        os.utime(db_path, (1000, 1000))
        _, items1 = subagents.collect_subagents(parent_cid)
        self.assertEqual([x["id"] for x in items1], [sub_b, sub_a])

        # Sending a follow-up prompt via send_message at t=300 bumps sub_a above sub_b
        db_path.unlink()
        _build_db(
            db_path,
            [],
            [
                (1, 132, 3, _meta_ts(100), p_a),
                (2, 132, 3, _meta_ts(200), p_b),
                (3, 132, 3, _meta_ts(300), p_msg_a),
            ],
        )
        os.utime(db_path, (1001, 1001))
        _, items2 = subagents.collect_subagents(parent_cid)
        self.assertEqual([x["id"] for x in items2], [sub_a, sub_b])
        self.assertEqual(items2[0]["last_prompt_ts"], 300.0)

    def test_compacted_status5_run_command_recovers_command_line_from_terminal_payload(self):
        proj_dir = self.root / "proj"
        cmd = f"cd {shlex.quote(str(proj_dir))} && sa-run.sh tmp/backend.jsonl sa-codex --model gpt-6.2-sol --prompt hi"
        out = "▶ codex 会话 33333333-cccc-4ccc-8ccc-333333333333（gpt-6.2-sol）\n■ 完成\n"
        inner = _field_bytes(1, cmd.encode("utf-8")) + _field_bytes(2, str(proj_dir).encode()) + _field_bytes(4, out.encode("utf-8"))
        term = _field_bytes(10, _field_bytes(3, inner))
        f140 = _field_bytes(2, _field_bytes(3, term))
        f5 = _field_bytes(4, _field_bytes(1, b"call_1") + _field_bytes(2, b"run_command"))
        payload = _field_varint(1, 132) + _field_varint(4, 5) + _field_bytes(5, f5) + _field_bytes(140, f140)

        tname, args, inline_txt = _parse_step132_payload(payload)
        self.assertEqual(tname, "run_command")
        self.assertEqual(args.get("CommandLine"), cmd)
        self.assertEqual(args.get("Cwd"), str(proj_dir))
        self.assertIn("33333333-cccc-4ccc-8ccc-333333333333", inline_txt)


if __name__ == "__main__":
    unittest.main()
