from contextlib import ExitStack
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch

import dispatch_policy as dp

NOW = 1_700_000_000


def _process_write(path, reset, sender):
    with sender:
        sender.send("ready")
        try:
            if reset:
                dp.reset_policy(expected_revision=1, policy_path=path)
            else:
                dp.save_policy(dict(dp.default_policy(), enabled=False), expected_revision=1, policy_path=path)
        except dp.PolicyConflictError:
            sender.send("PolicyConflictError")
        else:
            sender.send("saved")


class DispatchPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.policy_path = Path(tmp.name) / "dispatch-policy.json"
        self.state_path = Path(tmp.name) / "state.json"
        for p in (patch.object(dp, "STATE_PATH", self.state_path), patch.dict(os.environ, {"SA_DISPATCH_POLICY_PATH": ""})):
            p.start()
            self.addCleanup(p.stop)

    def test_environment_policy_path_applies_to_load_save_reset_and_resolve(self):
        override = self.policy_path.with_name("custom.json")
        with patch.dict(os.environ, {"SA_DISPATCH_POLICY_PATH": str(override)}):
            self.assertFalse(dp.load_policy()["exists"])
            disabled = dict(dp.default_policy(), enabled=False)
            self.assertEqual(dp.save_policy(disabled, expected_revision=1)["revision"], 2)
            self.assertEqual(dp.resolve_role("scout")["mode"], "disabled")
            self.assertEqual(dp.resolve_all()["panel"]["mode"], "disabled")
            self.assertFalse(dp.load_policy(self.policy_path)["exists"])
            dp.save_policy(dp.default_policy(), expected_revision=1, policy_path=self.policy_path)
            self.assertFalse(dp.load_policy()["policy"]["enabled"])
            self.assertEqual(dp.reset_policy(expected_revision=2)["revision"], 3)
        self.assertEqual(dp.load_policy()["policy"]["revision"], 2)
        self.assertEqual(dp.load_policy(override)["policy"]["revision"], 3)

    def test_defaults_and_immutable_role_permissions(self):
        p = dp.default_policy()
        self.assertEqual(dp.validate_policy(p), p)
        self.assertEqual({k: v["read_only"] for k, v in p["roles"].items()}, dp.ROLE_READ_ONLY)
        del p["roles"]["scout"]["read_only"]  # The v2 design's original JSON omitted this derived field.
        self.assertIs(dp.validate_policy(p)["roles"]["scout"]["read_only"], True)
        for value in (False, 1, "true", None):
            with self.subTest(value=value):
                p["roles"]["scout"]["read_only"] = value
                with self.assertRaises(ValueError):
                    dp.validate_policy(p)

    def test_schema_is_an_allowlist_at_every_level(self):
        for location, key, value in [
            ((), "command", "echo anything"), ((), "enabled", 1), ((), "revision", True),
            ((), "schema_version", True), (("quota_fallback",), "threshold_pct", 3.5),
            (("quota_fallback",), "threshold_pct", True), (("quota_fallback",), "threshold_pct", 51),
            (("quota_fallback",), "threshold_pct", -1), (("roles",), "other", {}),
            (("roles", "expert"), "command_template", "literal"), (("roles", "panel"), "seats", {}),
            (("roles", "scout", "primary"), "path", "/tmp/anything"),
            (("roles", "scout", "primary"), "engine", "custom"),
            (("roles", "scout", "primary"), "channel", "shell"),
            (("roles", "scout", "primary"), "effort", "highest"),
            (("roles", "panel", "seats", 0), "enabled", "false"),
            (("fallbacks",), "unknown:model", []),
        ]:
            with self.subTest(location=location, key=key, value=value):
                p = dp.default_policy()
                obj = p
                for segment in location:
                    obj = obj[segment]
                obj[key] = value
                with self.assertRaises(ValueError):
                    dp.validate_policy(p)

    def test_model_safety(self):
        p = dp.default_policy()
        target = p["roles"]["scout"]["primary"]
        for model in ("x; y", "x & y", "x|y", "x`foo`", "x$(foo)", "x\x00", "x\n", "x\rfoo",
                      "x/../z", "/bin/bash", "usr/bin/bash", "x --model y", "x > y", "x(y)",
                      "x" * 82, "x\\path", "https://host/model"):
            with self.subTest(model=model):
                target["model"] = model
                with self.assertRaises(ValueError):
                    dp.validate_policy(p)
        for model in ("provider/model-v1:alpha", "Gemini 4 Pro (High)", "gpt-6.2-sol", "x" * 81):
            target["model"] = model
            dp.validate_policy(p)
        for effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
            target["effort"] = effort
            dp.validate_policy(p)

    def test_degraded_files_are_immutable_for_save_and_reset(self):
        missing = dp.load_policy(self.policy_path)
        self.assertFalse(missing["exists"])
        self.assertFalse(missing["degraded"])
        for contents in (
            "{", "\n".join(["{", '  "revision": 1, "revision": 2', "}"]),
            json.dumps(dict(dp.default_policy(), schema_version=9)),
        ):
            with self.subTest(contents=contents[:15]):
                self.policy_path.write_text(contents)
                current = dp.load_policy(self.policy_path)
                self.assertTrue(current["exists"])
                self.assertTrue(current["degraded"])
                self.assertTrue(current["read_only_warning"])
                self.assertEqual(current["policy"], dp.default_policy())
                with self.assertRaises(dp.PolicyConflictError):
                    dp.save_policy(current["policy"], expected_revision=1, policy_path=self.policy_path)
                with self.assertRaises(dp.PolicyConflictError):
                    dp.reset_policy(expected_revision=1, policy_path=self.policy_path)
                self.assertEqual(self.policy_path.read_text(), contents)

    def test_path_probe_and_safe_native_fallbacks(self):
        with patch.object(dp.shutil, "which", return_value=None) as which:
            caps = dp.probe_capabilities(self.state_path)
        self.assertEqual([c.args[0] for c in which.call_args_list], [f"sa-{x}" for x in dp.ENGINES])
        all_roles = dp.resolve_all(capabilities=caps, policy_path=self.policy_path)
        for role, expected in {
            "scout": ("research", "flash"), "standard": ("self", "inherit"),
            "deep": ("self", "pro"), "expert": ("self", "pro"),
        }.items():
            self.assertEqual((all_roles[role]["type"], all_roles[role]["model"]), expected)
        self.assertTrue(all_roles["panel"]["blind_isolated"])
        self.assertEqual([(s["type"], s["model"], s["read_only"]) for s in all_roles["panel"]["seats"]],
                         [("research", "flash", True)] * 3)
        self.assertFalse(self.policy_path.exists())
        dp_off = dict(dp.default_policy(), enabled=False)
        self.assertEqual(dp.resolve_role("scout", dp_off, caps),
                         {"mode": "disabled", "channel": "direct", "reason": "dispatch_disabled"})

    def test_role_permissions_and_public_panel_seat(self):
        p = dict(dp.default_policy(), external_enabled=False, allow_swarm=True)
        cli = p["roles"]["scout"]["primary"].copy()
        self_target = dict(cli, channel="invoke_subagent", model="pro", effort="")
        research = dict(self_target, model="flash")
        swarm = dict(self_target, channel="swarm", model="Gemini 4 Pro (High)")
        p["roles"]["scout"]["primary"] = self_target
        p["roles"]["standard"]["primary"] = research
        p["roles"]["panel"]["seats"] = [swarm, research, dict(cli, enabled=False)]
        caps = {"available_engines": ["agy", "codex"]}
        dp.validate_policy(p)
        scout, standard, deep, expert, panel = dp.resolve_all(p, caps).values()
        self.assertEqual((scout["type"], scout["model"], standard["type"], standard["model"]),
                         ("research", "pro", "self", "flash"))
        self.assertEqual([deep["channel"], expert["channel"]], ["swarm", "swarm"])
        for resolved, tier in ((deep, "Medium"), (expert, "High")):
            self.assertEqual(resolved["model"], f"Gemini 4 Neon ({tier})")
            self.assertTrue(resolved["requires_model_check"])
            tail = resolved["fallbacks"][-1]
            self.assertEqual((tail["channel"], tail["type"], tail["model"]), ("invoke_subagent", "self", "pro"))
        self.assertEqual(len(panel["seats"]), 2)
        self.assertFalse(panel["blind_isolated"])
        self.assertEqual(panel["seats"][0]["blind_reason"], "shared_swarm_board")
        self.assertTrue(panel["seats"][1]["blind_isolated"])
        p["allow_swarm"] = False
        self.assertTrue(dp.resolve_role("panel", p, caps)["blind_isolated"])

    def test_fresh_quota_only_and_enabled_fallback_order(self):
        p = dp.default_policy()
        p["roles"]["deep"]["primary"] = dp._target("codex", "gpt-6.2-sol", "high")
        p["fallbacks"]["codex:gpt-6.2-sol"] = [
            dp._target("droid", "first", "same", enabled=False),
            dp._target("droid", "gpt-6.2-sol", "same", enabled=True),
        ]
        recently = NOW - 2
        for stamp, reset, stale, expected, fresh in (
            (recently, NOW + 60, False, "droid", True), (NOW - 300, NOW + 60, False, "droid", True),
            (recently, None, False, "codex", False), (recently, 0, False, "codex", False),
            (recently, -1, False, "codex", False), (recently, NOW, False, "codex", False),
            (recently, NOW + 60, True, "codex", False), (NOW - 301, NOW + 60, False, "codex", False),
            (NOW + 3, NOW + 60, False, "codex", False), (None, NOW + 60, False, "codex", False),
        ):
            with self.subTest(stamp=stamp, reset=reset, stale=stale):
                sample = dict(rem_frac=.05, observed_at=stamp, stale=stale)
                if reset is not None:
                    sample["reset_ts"] = reset
                self.state_path.write_text(json.dumps({"quota_cache": {
                    "codex-5h": sample,
                    "codex-weekly": dict(observed_at=recently, pct=99, reset_ts=NOW + 60, limit_id="other"),
                }}))
                with patch.object(dp.shutil, "which", side_effect=lambda exe: exe if exe in ("sa-codex", "sa-droid") else None):
                    caps = dp.probe_capabilities(self.state_path, now=NOW)
                self.assertEqual(caps["quota"]["codex"]["fresh"], fresh)
                self.assertIsNone(caps["quota"]["pi"]["pct"])
                resolved = dp.resolve_role("deep", p, caps)
                self.assertEqual((resolved["engine"], resolved["effort"]), (expected, "high"))
        p["quota_fallback"]["enabled"] = False
        caps["quota"]["codex"] = {"fresh": True, "pct": 0}
        self.assertEqual(dp.resolve_role("deep", p, caps)["engine"], "codex")
        p["external_enabled"] = False
        self.assertEqual(dp.resolve_role("deep", p, caps)["channel"], "invoke_subagent")

    def test_quota_uses_the_relevant_agy_family_and_does_not_invent_pct(self):
        self.state_path.write_text(json.dumps({"quota_cache": {
            "gemini-5h": dict(rem_frac=.8, observed_at=NOW, reset_ts=NOW + 60),
            "3p-5h": dict(rem_frac=0, observed_at=NOW, reset_ts=NOW + 60),
            "droid-5h": dict(observed_at=NOW, reset_ts=NOW + 60),
        }}))
        with patch.object(dp.shutil, "which", return_value="available"):
            caps = dp.probe_capabilities(self.state_path, now=NOW)
        self.assertTrue(caps["quota"]["agy"]["fresh"])
        self.assertEqual(caps["quota"]["agy"]["pct"], 0)
        p = dp.default_policy()
        p["fallbacks"] = {}  # Isolate AGY quota-family checks from Droid fallback chains.
        primary = p["roles"]["scout"]["primary"]
        names = {
            "gemini": ("gemini-4-orchestrator", "Neon High", "argon", "Carbon (Medium)", "hydrogen", "Flash"),
            "3p": ("claude-4", "Opus High", "Sonnet", "Haiku"),
            None: ("unknown-model", "provider/notclaude"),
        }
        for depleted in ("3p", "gemini"):
            for family, models in names.items():
                for model in models:
                    with self.subTest(model=model, depleted=depleted):
                        primary["model"] = model
                        resolved = dp.resolve_role("scout", p, caps)
                        self.assertEqual(resolved["channel"], "invoke_subagent" if family == depleted else "cli")
            families = caps["agy_quota"]
            families["gemini"], families["3p"] = families["3p"], families["gemini"]
        self.assertIsNone(caps["quota"]["droid"]["pct"])
        self.assertFalse(caps["quota"]["droid"]["fresh"])

    def test_concurrent_save_is_atomic_and_reset_advances_revision(self):
        gate = Barrier(2)

        def update(enabled):
            p = dict(dp.default_policy(), enabled=enabled)
            gate.wait(timeout=3)
            try:
                return dp.save_policy(p, expected_revision=1, policy_path=self.policy_path)
            except dp.PolicyConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, [True, False]))
        winner, = [result for result in results if result is not None]
        self.assertEqual(winner["revision"], 2)
        self.assertEqual(dp.load_policy(self.policy_path)["policy"], winner)
        self.assertEqual(stat.S_IMODE(self.policy_path.stat().st_mode), 0o600)
        self.assertEqual({p.name for p in self.policy_path.parent.iterdir()},
                         {self.policy_path.name, self.policy_path.name + ".lock"})
        with self.assertRaises(dp.PolicyConflictError):
            dp.reset_policy(expected_revision=1, policy_path=self.policy_path)
        self.assertEqual(dp.reset_policy(expected_revision=2, policy_path=self.policy_path),
                         dict(dp.default_policy(), revision=3))

    def test_concurrent_processes_share_file_lock_and_recheck_revision(self):
        ctx = multiprocessing.get_context("spawn")
        lock_path = self.policy_path.with_name(self.policy_path.name + ".lock")
        for second_resets, replace_schema in ((False, False), (True, False), (True, True)):
            with self.subTest(second_resets=second_resets, replace_schema=replace_schema), ExitStack() as stack:
                self.policy_path.unlink(missing_ok=True)
                procs, receivers = [], []
                with lock_path.open("a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    for reset in (False, second_resets):
                        recv, send = ctx.Pipe(duplex=False)
                        stack.enter_context(recv)
                        proc = ctx.Process(target=_process_write, args=(self.policy_path, reset, send))
                        with send:
                            proc.start()
                        stack.callback(self.stop_process, proc)
                        procs.append(proc)
                        receivers.append(recv)
                    for recv in receivers:
                        self.assertTrue(recv.poll(5), "writer failed to start")
                        self.assertEqual(recv.recv(), "ready")
                    for recv in receivers:
                        self.assertFalse(recv.poll(0.2), "writer bypassed another process's file lock")
                    self.assertFalse(self.policy_path.exists())
                    if replace_schema:
                        external_contents = json.dumps(dict(dp.default_policy(), schema_version=9))
                        incoming = self.policy_path.with_name("external.json")
                        incoming.write_text(external_contents)
                        incoming.replace(self.policy_path)
                results = []
                for proc, recv in zip(procs, receivers):
                    self.assertTrue(recv.poll(5), "writer stuck after releasing file lock")
                    results.append(recv.recv())
                    proc.join(5)
                    self.assertEqual(proc.exitcode, 0)
                if replace_schema:
                    self.assertEqual(results, ["PolicyConflictError"] * 2)
                    self.assertEqual(self.policy_path.read_text(), external_contents)
                else:
                    self.assertCountEqual(results, ["saved", "PolicyConflictError"])
                    self.assertEqual(dp.load_policy(self.policy_path)["policy"]["revision"], 2)

    @staticmethod
    def stop_process(proc):
        if proc.is_alive():
            proc.terminate()
        proc.join(2)
        proc.close()

    def test_cli_subprocess_works_from_unrelated_cwd_with_empty_path(self):
        script = Path(__file__).resolve().parents[2] / "skills/cockpit-dispatch/scripts/resolve.py"
        self.policy_path.write_text(json.dumps(dict(dp.default_policy(), allow_swarm=True)))
        cmd = [sys.executable, str(script), "--policy-path", str(self.policy_path), "--state-path", str(self.state_path)]
        env = dict(os.environ, PATH="")
        cwd = self.policy_path.parent
        for action in (("--all",), ("--capabilities",), ("--role", "scout")):
            with self.subTest(action=action):
                child = subprocess.run([*cmd, *action], cwd=cwd, env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(child.returncode, 0, child.stderr)
                body = json.loads(child.stdout)
                if action[0] == "--all":
                    self.assertEqual(set(body), set(dp.ROLE_READ_ONLY))
                    self.assertEqual(body["deep"]["channel"], "swarm")
                elif action[0] == "--capabilities":
                    self.assertEqual(body["available_engines"], [])
                else:
                    self.assertEqual(body["type"], "research")

    def test_wrapper_discovery_uses_runtime_bin_when_path_is_nonempty(self):
        fake_bin = self.policy_path.parent / "runtime-bin"
        fake_bin.mkdir()
        exe = fake_bin / "sa-codex"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        with patch.object(dp, "_EXTRA_WRAPPER_DIRS", ()), \
             patch.dict(os.environ, {"PATH": "/nonexistent", "SA_RUNTIME_BIN": str(fake_bin)}):
            caps = dp.probe_capabilities(self.state_path)
            self.assertEqual(caps["available_engines"], ["codex"])
        with patch.object(dp, "_EXTRA_WRAPPER_DIRS", ()), \
             patch.dict(os.environ, {"PATH": "", "SA_RUNTIME_BIN": str(fake_bin)}):
            caps = dp.probe_capabilities(self.state_path)
            self.assertEqual(caps["available_engines"], [])

    def test_panel_ordered_count_skips_unavailable_seats_and_backfills(self):
        p = dp.default_policy()
        p["roles"]["panel"]["seats"] = [
            dp._target("codex", "gpt-6.2-astra", "xhigh", enabled=True),
            dp._target("agy", "gemini-4-neon-xhigh", "xhigh", enabled=True),
            dp._target("agy", "claude-opus-5-5-high", "high", enabled=True),
            dp._target("grok", "grok-4.7", "xhigh", enabled=True),
        ]
        p["fallbacks"]["codex:astra"] = []  # Force seat #1 to be skipped when codex is unavailable
        caps = {"available_engines": ["agy", "grok"]}
        one = dp.resolve_role("panel", p, caps, count=1)
        self.assertEqual([(s["order"], s["engine"], s["model"]) for s in one["seats"]],
                         [(2, "agy", "gemini-4-neon-xhigh")])
        self.assertEqual(one["skipped_seats"][0]["order"], 1)
        two = dp.resolve_role("panel", p, caps, count=2)
        self.assertEqual([(s["order"], s["engine"], s["model"]) for s in two["seats"]],
                         [(2, "agy", "gemini-4-neon-xhigh"), (3, "agy", "claude-opus-5-5-high")])
        four = dp.resolve_role("panel", p, caps, count=4)
        self.assertEqual(len(four["seats"]), 4)
        self.assertEqual([(s["order"], s["channel"], s["engine"], s["model"]) for s in four["seats"]],
                         [(2, "cli", "agy", "gemini-4-neon-xhigh"), (3, "cli", "agy", "claude-opus-5-5-high"),
                          (4, "cli", "grok", "grok-4.7"), (5, "invoke_subagent", "agy", "flash")])
        with self.assertRaises(ValueError):
            dp.resolve_role("panel", p, caps, count=0)
        script = Path(__file__).resolve().parents[2] / "skills/cockpit-dispatch/scripts/resolve.py"
        for bad_args in (("--role", "scout", "--count", "1"), ("--role", "panel", "--count", "0")):
            child = subprocess.run([sys.executable, str(script), "--policy-path", str(self.policy_path), *bad_args],
                                   capture_output=True, text=True, timeout=5)
            self.assertEqual(child.returncode, 2)
            self.assertNotIn("Traceback", child.stderr)

    def test_versionless_families_auto_resolve_to_latest_cached_models(self):
        import dispatch_models as dm
        codex_cache = self.policy_path.with_name("codex_models.json")
        grok_cache = self.policy_path.with_name("grok_models.json")
        agy_state = self.policy_path.with_name("agy_state.json")
        codex_cache.write_text(json.dumps({"models": [{"slug": "gpt-6.2-astra"}, {"slug": "gpt-6.5-astra"}, {"slug": "gpt-6.4-sol"}]}))
        grok_cache.write_text(json.dumps({"models": {"grok-4.7": {}, "grok-5.1": {}, "grok-5.1-build-fast": {}}}))
        agy_state.write_text(json.dumps({"dynamic_models": {"1900": ["Gemini 4.5 Neon (High)"], "1901": ["Claude Opus 6.0"]}}))
        with patch.object(dm, "_CODEX_CACHE", codex_cache), patch.object(dm, "_GROK_CACHE", grok_cache), \
             patch.object(dm, "_AGY_STATE", agy_state), patch.object(dm, "_FACTORY_CACHE_DIR", self.policy_path.with_name("no-factory")):
            self.assertEqual(dm.resolve_model_id("codex", "astra"), "gpt-6.5-astra")
            self.assertEqual(dm.resolve_model_id("codex", "sol"), "gpt-6.4-sol")
            self.assertEqual(dm.resolve_model_id("grok", "grok"), "grok-5.1")
            self.assertEqual(dm.resolve_model_id("grok", "grok-fast"), "grok-5.1-build-fast")
            self.assertEqual(dm.resolve_model_id("agy", "neon", "high"), "gemini-4.5-neon-high")
            self.assertEqual(dm.resolve_model_id("agy", "opus", "max"), "claude-opus-6-0-max")
            self.assertEqual(dm.resolve_model_id("droid", "astra"), "gpt-6.5-astra")
            self.assertEqual(dm.resolve_model_id("codex", "gpt-6.2-sol"), "gpt-6.2-sol")
            p = dp.default_policy()
            p["roles"]["deep"]["primary"] = {"channel": "cli", "engine": "codex", "model": "sol", "effort": "high"}
            p["roles"]["standard"]["primary"] = {"channel": "cli", "engine": "agy", "model": "neon", "effort": "medium"}
            p["fallbacks"]["codex:sol"] = [{"channel": "cli", "engine": "droid", "model": "sol", "effort": "same", "enabled": True}]
            caps = {"available_engines": ["agy", "codex", "droid"], "quota": {"codex": {"fresh": True, "pct": 0}}}
            self.assertEqual(dp.resolve_role("standard", p, caps)["model"], "gemini-4.5-neon-medium")
            deep = dp.resolve_role("deep", p, caps)
            self.assertEqual((deep["engine"], deep["model"], deep["effort"]), ("droid", "gpt-6.4-sol", "high"))

    def test_codex_model_discovery_prefers_app_server_rpc(self):
        import dispatch_models as dm
        with patch.object(dm, "_fetch_codex_models_rpc", return_value=["gpt-6.8-sol", "gpt-6.7-astra"]):
            self.assertEqual(dm.resolve_model_id("codex", "sol"), "gpt-6.8-sol")
            self.assertEqual(dm.resolve_model_id("codex", "astra"), "gpt-6.7-astra")
        with patch.object(dm, "_CODEX_RPC_CACHE", None), \
             patch.object(dm, "_find_codex_bin", return_value="codex"), \
             patch.object(dm.subprocess, "Popen", side_effect=OSError("boom")) as popen:
            self.assertIsNone(dm._fetch_codex_models_rpc())
            self.assertIsNone(dm._fetch_codex_models_rpc())
            self.assertEqual(popen.call_count, 1)
