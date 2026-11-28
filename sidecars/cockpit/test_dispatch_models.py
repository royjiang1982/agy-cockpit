"""Model discovery, fallback keys, and the standalone dispatch resolver."""
from contextlib import redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

import dispatch_models as dm
import dispatch_policy as dp
from test_dispatch_policy import DispatchFixture


RESOLVER = Path(__file__).resolve().parents[2] / "skills/cockpit-dispatch/scripts/resolve.py"


class DispatchModelsTests(DispatchFixture):
    def resolve_cli(self, *args):
        cmd = [sys.executable, "-IB", str(RESOLVER), "--policy-path", str(self.policy_path),
               "--state-path", str(self.state_path)]
        return subprocess.run([*cmd, *args], cwd=self.root, env=dict(os.environ, PATH=""),
                              capture_output=True, text=True, timeout=10)

    def assert_cli_usage_error(self, args, message):
        # Exercise the actual entry point; malformed arguments need no new interpreter.
        stderr = StringIO()
        with (
            patch.object(sys, "argv", [str(RESOLVER), *args]),
            patch.object(sys, "path", sys.path.copy()),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_status,
        ):
            runpy.run_path(str(RESOLVER), run_name="__main__")
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn(message, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_subprocess_works_from_unrelated_cwd_with_empty_path(self):
        self.policy_path.write_text(json.dumps(dict(dp.default_policy(), allow_swarm=True)))
        for action in (("--all",), ("--capabilities",), ("--role", "scout")):
            with self.subTest(action=action):
                child = self.resolve_cli(*action)
                self.assertEqual(child.returncode, 0, child.stderr)
                body = json.loads(child.stdout)
                if action[0] == "--all":
                    self.assertEqual(set(body), set(dp.ROLE_READ_ONLY))
                    self.assertEqual(body["deep"]["channel"], "swarm")
                elif action[0] == "--capabilities":
                    self.assertEqual(body["available_engines"], [])
                else:
                    self.assertEqual(body["type"], "research")

    def test_cli_read_only_and_blind_flags(self):
        p = dict(dp.default_policy(), allow_swarm=True)
        p["roles"]["panel"]["seats"] = [dp._target("agy", "Gemini 4 Neon (High)", channel="swarm")]
        self.policy_path.write_text(json.dumps(p))
        expert = self.resolve_cli("--role", "expert", "--read-only")
        self.assertEqual(expert.returncode, 0, expert.stderr)
        self.assertEqual(json.loads(expert.stdout)["type"], "research")
        panel = self.resolve_cli("--role", "panel", "--blind", "--count", "2")
        self.assertEqual(panel.returncode, 0, panel.stderr)
        self.assertEqual([seat["channel"] for seat in json.loads(panel.stdout)["seats"]], ["invoke_subagent"] * 2)

    def test_cli_rejects_misplaced_flags_and_unsupported_counts(self):
        for args, message in (
            (("--all", "--read-only"), "--read-only can only be used with --role"),
            (("--capabilities", "--read-only"), "--read-only can only be used with --role"),
            (("--all", "--blind"), "--blind can only be used with --role panel"),
            (("--role", "expert", "--blind"), "--blind can only be used with --role panel"),
            (("--role", "scout", "--count", "1"), "--count can only be used with --role panel"),
            (("--role", "panel", "--count", "0"), "--count must be an integer from 1 to 12"),
            (("--role", "panel", "--count", "13"), "--count must be an integer from 1 to 12"),
        ):
            with self.subTest(args=args):
                self.assert_cli_usage_error(args, message)

    def test_fallback_lookup_keys_keep_exact_and_concrete_ids_ahead_of_family(self):
        for engine, model, resolved, effort, ids in (
            ("agy", "hydrogen", "hydrogen", "high", ("hydrogen", "gemini-4-hydrogen-high")),
            ("droid", "sol", "sol", "", ("sol", "gpt-6.2-sol")),
            ("codex", "sol", "gpt-6.4-sol", "", ("sol", "gpt-6.4-sol")),
            ("codex", "gpt-6.1-sol", "gpt-6.1-sol", "", ("gpt-6.1-sol", "sol")),
            ("grok", "unknown", "unknown", "", ("unknown",)),
        ):
            with self.subTest(engine=engine, model=model, resolved=resolved):
                self.assertEqual(dm.fallback_lookup_keys(engine, model, resolved, effort),
                                 tuple(f"{engine}:{item}" for item in ids))

    def test_offline_agy_family_uses_effort_specific_fallback(self):
        p = dp.default_policy()
        p["roles"]["scout"]["primary"] = dp._target("agy", "hydrogen", "high")
        p["fallbacks"] = {"agy:gemini-4-hydrogen-high": [dp._target("agy", "pro", channel="invoke_subagent")]}
        route = dp.resolve_role("scout", dp.validate_policy(p), {"available_engines": []})
        self.assertEqual((route["model"], route["reason"]), ("pro", "fallback"))
        self.assertEqual(route["skipped"][0]["reason"], "cli_unavailable")

    def test_same_fallback_inherits_concrete_agy_effort_if_unspecified(self):
        p = dp.default_policy()
        model = "gemini-4-hydrogen-xhigh"
        p["fallbacks"][f"agy:{model}"] = [dp._target("droid", "fixture-model", "same")]
        for effort, expected in (("", "xhigh"), ("low", "low")):
            with self.subTest(effort=effort):
                p["roles"]["scout"]["primary"] = dp._target("agy", model, effort)
                route = dp.resolve_role("scout", p, {"available_engines": ["droid"]})
                self.assertEqual((route["engine"], route["effort"], route["reason"]), ("droid", expected, "fallback"))

    def test_offline_droid_family_uses_concrete_fallback(self):
        p = dp.default_policy()
        p["roles"]["scout"]["primary"] = dp._target("droid", "sol")
        p["fallbacks"] = {"droid:gpt-6.2-sol": [dp._target("agy", "pro", channel="invoke_subagent")]}
        route = dp.resolve_role("scout", p, {"available_engines": []})
        self.assertEqual((route["model"], route["reason"]), ("pro", "fallback"))

    def test_explicit_codex_version_does_not_use_default_versions_fallback(self):
        p = dp.default_policy()
        p["roles"]["scout"]["primary"] = dp._target("codex", "gpt-6.1-sol", "high")
        p["fallbacks"] = {
            "codex:gpt-6.2-sol": [dp._target("agy", "pro", channel="invoke_subagent")],
            "codex:sol": [dp._target("agy", "flash_lite", channel="invoke_subagent")],
        }
        route = dp.resolve_role("scout", dp.validate_policy(p), {"available_engines": []})
        self.assertEqual((route["model"], route["reason"]), ("flash_lite", "fallback"))
        self.assertEqual(route["skipped"][0]["reason"], "cli_unavailable")

    def test_versionless_families_auto_resolve_to_latest_cached_models(self):
        self.codex_cache.write_text(json.dumps({"models": [{"slug": "gpt-6.2-astra"}, {"slug": "gpt-6.5-astra"}, {"slug": "gpt-6.4-sol"}]}))
        self.grok_cache.write_text(json.dumps({"models": {"grok-4.7": {}, "grok-5.1": {}, "grok-5.1-build-fast": {}}}))
        self.agy_state.write_text(json.dumps({"dynamic_models": {"1900": ["Gemini 4.5 Neon (High)"], "1901": ["Claude Opus 6.0"]}}))

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

    def test_codex_model_discovery_prefers_app_server_rpc_and_caches_failure(self):
        self.codex_cache.write_text(json.dumps({"models": [{"slug": "gpt-6.6-sol"}]}))
        with patch.object(dm, "_fetch_codex_models_rpc", return_value=["gpt-6.8-sol", "gpt-6.7-astra"]):
            self.assertEqual(dm.resolve_model_id("codex", "sol"), "gpt-6.8-sol")
            self.assertEqual(dm.resolve_model_id("codex", "astra"), "gpt-6.7-astra")
        with (
            patch.object(dm, "_find_codex_bin", return_value=str(self.root / "codex")),
            patch.object(dm.subprocess, "Popen", side_effect=OSError("boom")) as popen,
            patch.object(dm.time, "monotonic", return_value=1),
        ):
            for _ in range(2):
                self.assertEqual(dm.resolve_model_id("codex", "sol"), "gpt-6.6-sol")
            popen.assert_called_once()
