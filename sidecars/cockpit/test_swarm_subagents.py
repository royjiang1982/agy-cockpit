from contextlib import ExitStack
from datetime import datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import analyzer
from cost_runtime import CostRows, CostRuntime
from pricing import build_models_board
import server
import subagents
import swarm_subagents as swarm
from test_orchestrator_recognition import _build_db, _encode_gen_metadata, _field_bytes, _field_varint

PARENT = "ca8f4c0d-d284-4b76-9044-5cf1f200baec"
WORKER = "da8f4c0d-d284-4b76-9044-5cf1f200baec"
OTHER = "ea8f4c0d-d284-4b76-9044-5cf1f200baec"
NOW = 1_780_000_000
INVOKE = _field_bytes(1, b"invoke_subagent") + _field_bytes(2, b'{"Role":"original"}')
ASK = _field_bytes(4, _field_bytes(2, b"ask_question"))


def _meta_ts(sec):
    return _field_bytes(1, _field_varint(1, sec))


class SwarmSubagentsTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.app = Path(stack.enter_context(TemporaryDirectory()))
        self.dbs = self.app / "conversations"
        self.dbs.mkdir()
        self.app_dirs = [("App", self.app)]
        self.system = self.app / "brain" / PARENT / ".system_generated"
        self.folder = self.system / "swarm"
        self.folder.mkdir(parents=True)
        self.roster = self.folder / "members.json"
        self.board = self.folder / "board.jsonl"
        for module, values in (
            (server, {"APP_DIRS": self.app_dirs, "_CONVERSATION_INDEX_CACHE": None,
                      "_CURRENT_ACTIVE_ID": "", "_PREV_VTIMES": {}, "_PREV_PBTXT_MTIMES_NS": {},
                      "_PREV_MAX_VTIME": 0.0}),
            (subagents, {"APP_DIRS": self.app_dirs, "SUBAGENT_APP_DIRS": self.app_dirs,
                         "_SUBAGENT_STUBS_CACHE": analyzer.BoundedCache(16),
                         "_COMPLETED_STEP_STUBS": analyzer.BoundedCache(16),
                         "_STEP_DEPENDENCIES": analyzer.BoundedCache(16)}),
            (analyzer, {"_ANALYSIS_CACHE": analyzer.BoundedCache(16),
                        "_BUILTIN_SUBAGENT_MODEL_ENUM": {}}),
            (swarm, {"_SCAN_CACHE": None}),
        ):
            for name, value in values.items():
                stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.object(subagents, "_ANALYSIS_CACHE", analyzer._ANALYSIS_CACHE))
        stack.enter_context(patch.object(server, "_BUILTIN_SUBAGENT_MODEL_ENUM", analyzer._BUILTIN_SUBAGENT_MODEL_ENUM))
        stack.enter_context(patch.object(server, "_save_persisted_state"))
        stack.enter_context(patch.object(server, "read_annotations", return_value=({}, {WORKER: 42, PARENT: 20}, {})))

    def write_members(self, **members):
        self.roster.write_text(json.dumps({"title": "Review", "members": members}))
        os.utime(self.roster, (NOW, NOW))

    def append(self, author, text, when):
        with self.board.open("a") as stream:
            stream.write(json.dumps({"agent": author, "text": text, "time": when}) + "\n")

    def create_db(self, cid, cargo=(), rows=()):
        db = self.dbs / f"{cid}.db"
        _build_db(db, list(cargo), list(rows))
        return db

    def write_invoke_output(self, step_idx):
        step = self.system / f"steps/{step_idx}/output.txt"
        step.parent.mkdir(parents=True)
        step.write_text(json.dumps({"conversationId": WORKER}))

    def test_discovery_dedup_mentions_and_killed(self):
        self.write_members(
            boss={"role": "coordinator", "conversation_id": "coordinator-other-id"},
            self_ref={"role": "worker", "conversation_id": PARENT},
            blank={"role": "worker", "conversation_id": ""},
            path={"role": "worker", "conversation_id": "../escape"},
            alice={"role": "reviewer", "conversation_id": WORKER, "model": "Neon", "killed": True},
            alias={"role": "worker", "conversation_id": WORKER},
            bob={"role": "worker", "conversation_id": OTHER},
        )
        self.assertEqual({s["prompt_ts"] for s in swarm.discover_swarm_stubs(PARENT, self.app_dirs)}, {NOW})
        self.append("boss", "hello @all", NOW + 1)
        self.append("boss", "reply, @ALICE", NOW + 2)
        self.append("alice", "my answer", NOW + 3)
        self.append("boss", "alias mention, @alias", (NOW + 4) * 1000)
        self.append("boss", "mail alice@bob and @alice-next are not mentions", NOW + 5)
        self.append("nobody", [], "invalid")
        self.board.open("a").close()
        with self.board.open("a") as stream:
            stream.write("{unfinished")
        stubs = {s["id"]: s for s in swarm.discover_swarm_stubs(PARENT, self.app_dirs)}
        self.assertEqual(list(stubs), [WORKER, OTHER])
        self.assertEqual(stubs[WORKER]["label"], "alice (reviewer)")
        self.assertEqual(stubs[WORKER]["model_hint"], "Neon")
        self.assertEqual(stubs[WORKER]["prompt_ts"], NOW + 4)
        self.assertTrue(stubs[WORKER]["killed"])
        self.assertEqual(stubs[OTHER]["prompt_ts"], NOW + 1)
        with patch.object(swarm.time, "time", return_value=NOW + 5):
            summary, items = subagents.collect_subagents(PARENT)
        self.assertEqual([it["status"] for it in items], ["killed", "running"])
        self.assertEqual(summary["running_count"], 1)

    def test_board_and_roster_changes_invalidate_stub_cache(self):
        self.create_db(PARENT)
        self.write_members(alice={"conversation_id": WORKER})
        first = swarm.swarm_dir_signature(PARENT, self.app_dirs)
        self.assertEqual(subagents.discover_subagent_stubs(PARENT)[0]["prompt_ts"], NOW)
        stamp = datetime.fromtimestamp(NOW + 1).strftime("%Y-%m-%d %H:%M:%S")
        self.append("coordinator", "ping @alice", stamp)
        self.assertNotEqual(first, swarm.swarm_dir_signature(PARENT, self.app_dirs))
        self.assertEqual(subagents.discover_subagent_stubs(PARENT)[0]["prompt_ts"], NOW + 1)
        self.write_members(alice={"conversation_id": WORKER, "killed": True})
        self.assertTrue(subagents.discover_subagent_stubs(PARENT)[0]["killed"])
        self.roster.unlink()
        self.assertEqual(subagents.discover_subagent_stubs(PARENT), [])

    def test_worker_scan_cache_and_pinned_index(self):
        self.create_db(PARENT)
        self.create_db(WORKER)
        self.create_db(OTHER)
        self.write_members(alice={"conversation_id": WORKER}, main={"role": "coordinator", "conversation_id": PARENT})
        with patch.object(swarm.time, "monotonic", return_value=10.0) as clock:
            workers = swarm.list_swarm_worker_ids(self.app_dirs)
            self.assertEqual(workers, {WORKER})
            workers.clear()
            active, items = server.resolve_active_and_list()
            self.assertEqual(active, PARENT)
            self.assertNotIn(WORKER, [i["id"] for i in items])
            active, items = server.resolve_active_and_list(preferred_id=WORKER)
            self.assertEqual(active, PARENT)
            self.assertEqual(items[0]["id"], WORKER)
            self.write_members(bob={"conversation_id": OTHER})
            clock.return_value = 12.99
            self.assertEqual(swarm.list_swarm_worker_ids(self.app_dirs), {WORKER})
            clock.return_value = 13.0
            self.assertEqual(swarm.list_swarm_worker_ids(self.app_dirs), {OTHER})

    def test_existing_builtin_stub_retains_identity_and_swarm_metadata(self):
        self.create_db(PARENT, rows=[(1, 132, 3, _meta_ts(NOW), INVOKE)])
        self.write_invoke_output(1)
        self.write_members(alice={"conversation_id": WORKER, "role": "reviewer", "killed": True})
        self.append("boss", "@alice please review", NOW + 1)
        existing, = subagents.discover_subagent_stubs(PARENT)
        self.assertEqual((existing["id"], existing["agent"], existing["label"]), (WORKER, "builtin", "original"))
        self.assertTrue(existing["is_swarm"])
        self.assertTrue(existing["killed"])
        self.assertEqual((existing["swarm_role"], existing["prompt_ts"]), ("reviewer", NOW + 1))
        summary, items = subagents.collect_subagents(PARENT)
        self.assertEqual([item["status"] for item in items], ["killed"])
        self.assertEqual(summary["running_count"], 0)

    def test_roster_only_member_expires_and_board_mtime_reactivates_it(self):
        self.create_db(PARENT)
        self.write_members(alice={"conversation_id": WORKER})
        with patch.object(swarm.time, "time", return_value=NOW + 299) as clock:
            for age, status in ((299, "running"), (300, "done"), (900, "done")):
                clock.return_value = NOW + age
                summary, (child,) = subagents.collect_subagents(PARENT)
                self.assertEqual(child["status"], status)
                self.assertEqual(summary["running_count"], int(status == "running"))
            self.append("boss", "an unrelated board update with an old event timestamp", NOW)
            os.utime(self.board, (NOW + 850, NOW + 850))
            summary, (child,) = subagents.collect_subagents(PARENT)
            self.assertEqual(child["status"], "running")
            self.assertEqual(summary["running_count"], 1)

    def test_db_status_preserves_errors_and_distinguishes_waiting_killed_and_running(self):
        self.create_db(PARENT, rows=[(1, 132, 3, _meta_ts(NOW), INVOKE)])
        self.write_invoke_output(1)
        base = [(0, 14, 3, _meta_ts(NOW - 50), b""), (1, 15, 3, _meta_ts(NOW - 40), b"")]
        cases = [(4, b"", False, "error"), (6, ASK, False, "error"), (7, b"", False, "error"),
                 (2, ASK, False, "waiting"), (2, b"", False, "running"), (2, b"", True, "killed"),
                 (3, b"", False, "done")]
        sequence = 0
        for with_metrics in (True, False):
            for code, payload, killed, expected in cases:
                with self.subTest(metrics=with_metrics, code=code, expected=expected):
                    self.write_members(alice={"conversation_id": WORKER, "killed": killed})
                    db = self.dbs / f"{WORKER}.db"
                    db.unlink(missing_ok=True)
                    cargo = [_encode_gen_metadata(1, 1532, 1000, 2000, 300)] if with_metrics else []
                    kind = 132 if payload else 15
                    self.create_db(WORKER, cargo, [*base, (2, kind, code, _meta_ts(NOW), payload)])
                    sequence += 1
                    os.utime(db, (NOW + sequence, NOW + sequence))
                    summary, (child,) = subagents.collect_subagents(PARENT)
                    self.assertEqual((child["agent"], child["status"]), ("builtin", expected))
                    self.assertEqual(summary["running_count"], int(expected == "running"))

    def test_cost_source_and_calendar_unchanged_and_fork_board_dedup(self):
        cargo = _encode_gen_metadata(1, 1532, 1000, 2000, 300)
        base_rows = [(0, 14, 3, _meta_ts(NOW - 50), b""), (1, 15, 3, _meta_ts(NOW - 40), b"")]
        parent = self.create_db(PARENT, [cargo], [*base_rows, (2, 132, 3, _meta_ts(NOW - 39), INVOKE)])
        child = self.create_db(WORKER, [cargo, _encode_gen_metadata(2, 1532, 2200, 1200, 400)],
                               [*base_rows, (2, 15, 3, _meta_ts(NOW - 30), b"")])
        with ExitStack() as stack:
            for name, value in {"_COST_FILE_EVENTS": {}, "_COST_GENERATION": 0, "_COST_RUNTIME": CostRuntime(),
                                "_PENDING_REBUILD": set(), "_DB_APPLIED_BUILTIN_ENUM": {}}.items():
                stack.enter_context(patch.object(server, name, value))
            server._analyze_with_hooks(str(parent), PARENT, "main", "App", analyzer.get_db_mtime(parent))
            server._analyze_with_hooks(str(child), WORKER, "standalone", "App", analyzer.get_db_mtime(child))
            baseline = self.calendar()
            self.write_members(alice={"conversation_id": WORKER, "killed": True})
            for agent in ("swarm", "builtin"):
                with self.subTest(agent=agent):
                    if agent == "builtin":
                        self.write_invoke_output(2)
                    result = server._analyze_with_hooks(str(parent), PARENT, "main", "App", analyzer.get_db_mtime(parent))
                    self.assertEqual(set(server._COST_FILE_EVENTS), {f"db:{PARENT}", f"db:{WORKER}"})
                    self.assertEqual(self.calendar(), baseline)
                    child_item, = result["subagents"]
                    self.assertEqual((child_item["agent"], child_item["status"]), (agent, "killed"))
                    self.assertEqual(child_item["fork_parent_overlap"][0]["calls"], 1)
                    board = result["models_board"]
                    main_row, = result["main_models"]
                    for field, value in dict(calls=1, prompt=3400, **{"in": 2200, "cache": 1200, "out": 400}).items():
                        self.assertEqual(child_item[field], value)
                        self.assertEqual(child_item["per_model"][0][field], value)
                        self.assertEqual(result["subagents_summary"][field], value)
                        self.assertEqual(board["summary"][field], value + main_row[field])
                    self.assertAlmostEqual(child_item["hit_pct"], 1200 / 3400 * 100)
                    self.assertGreater(child_item["cost"], 0)
                    self.assertAlmostEqual(result["subagents_summary"]["cost"], child_item["cost"])
                    self.assertAlmostEqual(child_item["per_model"][0]["cost"], child_item["cost"])
                    self.assertAlmostEqual(board["summary"]["cost"], main_row["cost"] + child_item["cost"])
                    duplicate = dict(child_item, id="another-stub", conversation_id=WORKER)
                    self.assertEqual(build_models_board(result["main_models"], [child_item, duplicate]), board)

    def calendar(self):
        files = [(key, events) for key, (_, events) in server._COST_FILE_EVENTS.items()]
        daily, *_ = CostRows().aggregate(files, {}, NOW, server._normalize_cost_event)
        return server._build_calendar(daily, NOW)
