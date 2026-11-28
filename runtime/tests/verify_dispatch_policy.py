"""Offline tests for the read-only dispatch policy and its two consumers."""
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from _runtime_fixture import RUNTIME as ROOT, isolated_env
sys.path.insert(0, str(ROOT.parent / "sidecars/cockpit"))
import dispatch_policy as cockpit
from agent_runtime import agy_quota as quota, daemon, dispatch_policy_reader as reader

MODEL = "gemini-4-neon-medium"
CODEX = str(ROOT / "bin/sa-codex")


def candidate(model="custom-droid", effort="high", **overrides):
    return dict(dict(channel="cli", engine="droid", model=model, effort=effort, enabled=True), **overrides)


class DispatchPolicyTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.path = Path(work.name) / "policy.json"
        self.mtime = 1_700_000_000_000_000_000
        env = isolated_env(work.name, {"SA_DISPATCH_POLICY_PATH": str(self.path)})
        sandbox = patch.dict(os.environ, env, clear=True)
        sandbox.start()
        self.addCleanup(sandbox.stop)
        # The model catalog derives its default paths at import time.
        missing = self.path.with_name("no-model-cache")
        caches = patch.multiple(cockpit.dispatch_models, _CODEX_CACHE=missing, _GROK_CACHE=missing,
                                _FACTORY_CACHE_DIR=missing, _AGY_STATE=missing)
        caches.start()
        self.addCleanup(caches.stop)

    def write_raw(self, value, *, tick=True):
        if tick:
            self.mtime += 1_000_000_000
        self.path.write_bytes(value)
        os.utime(self.path, ns=(self.mtime, self.mtime))

    def write(self, **fields):
        self.write_raw(json.dumps(dict(schema_version=1, **fields)).encode())

    def codex_tier(self, *flags):
        with patch.object(daemon, "run_subagent_client", return_value=0) as call, \
                patch.object(sys, "path", sys.path[:]), patch.object(sys, "argv", [CODEX, *flags, "task"]):
            self.assertEqual(runpy.run_path(CODEX)["main"](), 0)
            call.assert_called_once()
            return call.call_args.kwargs["service_tier"]

    def test_stat_cache_reloads_on_mtime_size_and_path(self):
        on = b'{"schema_version":1,"codex_fast":true }'
        off = b'{"schema_version":1,"codex_fast":false}'
        self.assertEqual(len(on), len(off))
        with patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as reads, \
                patch.object(Path, "stat", autospec=True, side_effect=Path.stat) as stats:
            self.write_raw(on)
            self.assertIs(reader.read_codex_fast(), True)
            self.assertIs(reader.read_codex_fast(), True)
            self.assertEqual(stats.call_count, 2)
            self.assertEqual(reads.call_count, 1)
            self.write_raw(off)
            self.assertIs(reader.read_codex_fast(), False)
            self.write_raw(on + b" ", tick=False)
            self.assertIs(reader.read_codex_fast(), True)
            other = self.path.with_name("other.json")
            other.write_bytes(off + b" ")
            os.utime(other, ns=(self.mtime, self.mtime))
            with patch.dict(os.environ, SA_DISPATCH_POLICY_PATH=str(other)):
                self.assertIs(reader.read_codex_fast(), False)
            self.assertEqual(reads.call_count, 4)
        self.assertEqual(self.path.read_bytes(), on + b" ")
        self.path.unlink()
        self.assertIsNone(reader.read_codex_fast())

    def assert_unusable_policy(self):
        self.assertIsNone(reader.read_codex_fast())
        self.assertEqual(reader.read_agy_policy(MODEL), (None, None, None, False))

    def test_default_path_and_unusable_files(self):
        with patch.dict(os.environ, SA_DISPATCH_POLICY_PATH=""):
            default = self.path.parent / ".gemini/antigravity/sidecar_data/statusline/dispatch-policy.json"
            self.assert_unusable_policy()
            default.parent.mkdir(parents=True)
            default.write_text('{"schema_version":1,"codex_fast":false}', encoding="utf-8")
            self.assertIs(reader.read_codex_fast(), False)
        for invalid in (b"{", b"\xff", b"[]", b"null", b'{"codex_fast":true,"n":NaN}',
                        b'{"schema_version":2,"codex_fast":false}'):
            self.write_raw(invalid)
            self.assert_unusable_policy()
            self.assertEqual(self.path.read_bytes(), invalid)
        self.write(codex_fast=True)
        with patch.object(Path, "read_bytes", side_effect=PermissionError):
            self.assert_unusable_policy()
        self.assertIs(reader.read_codex_fast(), True)
        with patch.object(Path, "stat", side_effect=PermissionError):
            self.assert_unusable_policy()

    def test_strict_schema_and_duplicate_keys_invalidate_entire_policy(self):
        for raw in (b'{"codex_fast":false}',
                    *(b'{"schema_version":' + version + b',"codex_fast":false}'
                      for version in (b"0", b"-1", b"true", b"1.0", b'"1"', b"2")),
                    b'{"schema_version":1,"schema_version":1,"codex_fast":false}',
                    b'{"schema_version":1,"codex_fast":false,"codex_fast":true}',
                    b'{"schema_version":1,"codex_fast":false,"quota_fallback":{"enabled":true,"enabled":false}}'):
            with self.subTest(raw=raw):
                self.write_raw(raw)
                self.assert_unusable_policy()

    def test_writer_and_reader_share_efforts_and_expanded_override(self):
        p = cockpit.default_policy()
        with patch.dict(os.environ, SA_DISPATCH_POLICY_PATH="~/policy.json", HOME=str(self.path.parent)):
            for effort in ("", "off", "minimal", "high", "same"):
                p["fallbacks"][f"agy:{MODEL}"] = [candidate(effort=effort)]
                p = cockpit.save_policy(p, expected_revision=p["revision"])
                self.assertEqual(cockpit.load_policy()["policy"], p)
                self.assertEqual(reader.read_agy_policy(MODEL).fallback, ("custom-droid", effort))
            p["fallbacks"][f"agy:{MODEL}"] = [candidate(effort="extra")]
            with self.assertRaises(ValueError):
                cockpit.save_policy(p, expected_revision=p["revision"])
            self.write_raw(json.dumps(p).encode())
            self.assertTrue(reader.read_agy_policy(MODEL).has_fallback_override)
            self.assertIsNone(reader.read_agy_policy(MODEL).fallback)

    def test_agy_fallback_default_effort_matches_model_resolution(self):
        for family, model in (("flash", "gemini-3.8-flash"), ("sonnet", "claude-sonnet-5-5")):
            with self.subTest(family=family):
                self.assertEqual(reader.default_agy_fallback(model), ("", "high"))
                resolved = cockpit.dispatch_models.resolve_model_id("agy", family, agy_state_path=self.path)
                self.assertEqual(resolved, model + "-high")

    def test_disabled_policy_reverts_codex_fast_and_agy_fallback(self):
        for flag in (False, 0, "true", None):
            with self.subTest(enabled=flag):
                self.write(enabled=flag, codex_fast=False,
                           quota_fallback=dict(enabled=False, threshold_pct=50),
                           fallbacks={f"agy:{MODEL}": [candidate()]})
                self.assert_unusable_policy()
                self.assertEqual(self.codex_tier(), "fast")

    def test_malformed_fields_do_not_override_defaults(self):
        for invalid in (-1, 51, True, "5", 5.0):
            self.write(codex_fast=0, quota_fallback=dict(enabled=1, threshold_pct=invalid))
            self.assert_unusable_policy()

    def test_codex_default_and_explicit_precedence(self):
        self.assertEqual(self.codex_tier(), "fast")
        self.write(codex_fast=False)
        self.assertEqual(self.codex_tier(), "")
        with patch.object(reader, "read_codex_fast", side_effect=AssertionError("unexpected policy read")):
            self.assertEqual(self.codex_tier("--fast"), "fast")
            self.assertEqual(self.codex_tier("--no-fast"), "")
            self.assertEqual(self.codex_tier("--service-tier", "FLEX"), "flex")
            self.assertEqual(self.codex_tier("--service-tier", ""), "")
            self.assertEqual(self.codex_tier("-c", 'service_tier="priority"'), "priority")
            self.assertEqual(self.codex_tier("-c", 'service_tier=""'), "")
        self.write(codex_fast=True)
        self.assertEqual(self.codex_tier(), "fast")
        self.assertEqual(self.codex_tier("--no-fast"), "")
        for invalid in (b"{", b'{"schema_version":2,"codex_fast":false}'):
            self.write_raw(invalid)
            self.assertEqual(self.codex_tier(), "fast")

    @patch.object(quota, "find_cli_binary", return_value="/mock/droid")
    @patch.object(quota, "_quota_for_model", return_value=dict(rem_frac=.10, reset_ts=0))
    def test_quota_switch_and_threshold_hot_reload(self, snapshot, find):
        request = dict(model=MODEL, prompt="task")
        self.write(quota_fallback=dict(enabled=False, threshold_pct=50))
        self.assertFalse(quota.preflight_agy_request(**request)["should_fallback"])
        snapshot.assert_not_called()
        with patch.dict(os.environ, SA_AGY_QUOTA_PREFLIGHT="1"):
            self.assertTrue(quota.preflight_agy_request(**request)["should_fallback"])
        self.write(quota_fallback=dict(enabled=True, threshold_pct=10))
        self.assertEqual(quota.preflight_agy_request(**request)["event"]["threshold"], .10)
        with patch.dict(os.environ, SA_AGY_QUOTA_PREFLIGHT="0"):
            snapshot.reset_mock()
            self.assertFalse(quota.preflight_agy_request(**request)["should_fallback"])
            snapshot.assert_not_called()
        self.write(quota_fallback=dict(enabled=True, threshold_pct=9))
        find.reset_mock()
        self.assertFalse(quota.preflight_agy_request(**request)["should_fallback"])
        find.assert_not_called()
        with patch.dict(os.environ, SA_AGY_MIN_QUOTA_FRAC=".10"):
            self.assertTrue(quota.preflight_agy_request(**request)["should_fallback"])
        self.write(quota_fallback=dict(threshold_pct=0))
        self.assertFalse(quota.preflight_agy_request(**request)["should_fallback"])
        snapshot.return_value = dict(rem_frac=0, reset_ts=0)
        self.assertEqual(quota.preflight_agy_request(**request)["event"]["threshold"], 0)

    @patch.object(quota, "find_cli_binary", return_value="/mock/droid")
    @patch.object(quota, "_quota_for_model", return_value=dict(rem_frac=0, reset_ts=0))
    def test_quota_fallback_override_env_precedence(self, _snapshot, _find):
        self.write(fallbacks={f"agy:{MODEL}": [candidate(effort="same")]})

        def target(model=MODEL):
            request = quota.preflight_agy_request(model=model, effort="low", prompt="task", mode="plan")
            return request["model"], request["effort"]

        self.assertEqual(target(), ("custom-droid", "low"))
        with patch.dict(os.environ, SA_AGY_FALLBACK_MODEL="env-droid"):
            self.assertEqual(target(), ("env-droid", "low"))
        self.write(fallbacks={f"agy:{MODEL}": [candidate(effort="xhigh")]})
        self.assertEqual(target(), ("custom-droid", "xhigh"))
        with patch.dict(os.environ, SA_AGY_FALLBACK_EFFORT="low"):
            self.assertEqual(target(), ("custom-droid", "low"))
        with patch.dict(os.environ, SA_AGY_FALLBACK_MODEL=""):
            self.assertEqual(target(), ("gemini-4-neon", "low"))
    def test_quota_fallback_exact_chain_validation_and_first_match(self):
        chain = [None, candidate(enabled=False), candidate(enabled=1), candidate(channel="builtin"),
                 candidate(engine="codex"), candidate(effort="xhigh\n"), candidate(effort="custom")]
        chain += [candidate(model=value) for value in (
            "a/b/c", "a\\b", "a..b", "a/../b", "a\x00b", "a\nb", "a ", "--model", "a --model",
            "a;b", "a|b", "a&b", "a>b", "a$(b)", "a\x60b",
        )]
        first = candidate("custom-droid", "same")
        del first["enabled"]
        self.write(fallbacks={f"agy:{MODEL}": [*chain, first, candidate("second")]})

        self.assertEqual(reader.read_agy_policy(MODEL).fallback, ("custom-droid", "same"))
        self.assertFalse(reader.read_agy_policy("gemini-4-neon-xhigh").has_fallback_override)
        for model in ("provider/mo.del:v-2", "Model Name (Preview)"):
            with self.subTest(model=model):
                self.write(fallbacks={f"agy:{MODEL}": [candidate(model, "")]})
                self.assertEqual(reader.read_agy_policy(MODEL).fallback, (model, ""))

    @patch.object(quota, "find_cli_binary", return_value="/mock/droid")
    @patch.object(quota, "_quota_for_model", return_value=dict(rem_frac=0, reset_ts=0))
    def test_same_effort_is_identical_in_policy_routing_and_quota_preflight(self, _snapshot, _find):
        for model, effort, expected in (
            ("gemini-4-neon-low", "", "low"),
            ("gemini-4-neon-xhigh", "same", "xhigh"),
            ("gemini-4-neon-xhigh", "low", "low"),
            ("gemini-4-neon", "", "medium"),
            ("neon", "", "medium"),
            ("flash", "", "high"),
            ("claude-sonnet-5-5", "", "high"),
            ("claude-opus-5-5", "off", "off"),
            ("custom-agy", "", ""),
        ):
            with self.subTest(model=model, effort=effort):
                policy = cockpit.default_policy()
                policy["roles"]["scout"]["primary"] = cockpit._target("agy", model, effort)
                policy["fallbacks"] = {f"agy:{model}": [candidate(effort="same")]}
                self.write_raw(json.dumps(policy).encode())
                selected = cockpit.resolve_role("scout", capabilities={"available_engines": ["droid"]})
                result = quota.preflight_agy_request(model=model, effort=effort, prompt="task")
                self.assertEqual((selected["engine"], selected["model"], selected["effort"]),
                                 ("droid", "custom-droid", expected))
                self.assertEqual((result["agent"], result["model"], result["effort"]),
                                 ("droid", "custom-droid", expected))
                self.assertEqual(result["event"]["to_effort"], expected)

    @patch.object(quota, "find_cli_binary", return_value="/mock/droid")
    @patch.object(quota, "_quota_for_model", return_value=dict(rem_frac=0, reset_ts=0))
    def test_explicit_chain_without_usable_droid_leaves_agy_unchanged(self, snapshot, find):
        original = dict(agent="agy", model=MODEL, effort="low", requested_sid="existing", mode="plan", prompt="task")
        with patch.object(quota, "build_forced_handoff_prompt") as handoff:
            for chain in ([], [candidate(enabled=False), candidate("another", enabled=False)],
                          [candidate(engine="codex")], [candidate(enabled="false")]):
                with self.subTest(chain=chain):
                    self.write(fallbacks={f"agy:{MODEL}": chain})
                    policy = reader.read_agy_policy(MODEL)
                    self.assertTrue(policy.has_fallback_override)
                    self.assertIsNone(policy.fallback)
                    self.assertEqual(quota.preflight_agy_request(**original),
                                     dict(original, should_fallback=False, event=None, handoff=None))
            snapshot.assert_not_called()
            find.assert_not_called()
            handoff.assert_not_called()

    @patch.object(quota, "find_cli_binary", return_value="/mock/droid")
    @patch.object(quota, "_quota_for_model", return_value=dict(rem_frac=0, reset_ts=0))
    def test_missing_file_fallback_matches_cockpit_default_policy(self, _snapshot, _find):
        self.assertFalse(self.path.exists())
        policy = cockpit.default_policy()
        caps = {"available_engines": ["droid"]}
        models = cockpit.dispatch_models
        missing = self.path.with_name("no-cache")
        for family in ("hydrogen", "neon", "carbon", "opus"):
            base = models._AGY_DEFAULTS[family]
            for suffix in ("", "low", "medium", "high", "xhigh", "max", "ultra"):
                model = f"{base}-{suffix}" if suffix else base
                with self.subTest(model=model):
                    effort = suffix or models._AGY_DEFAULT_EFFORT[family]
                    policy["roles"]["scout"]["primary"] = cockpit._target("agy", model, effort)
                    selected = cockpit.resolve_role("scout", policy, caps, state_path=missing)
                    self.assertEqual(selected["engine"], "droid")
                    result = quota.preflight_agy_request(model=model, prompt="task")
                    self.assertEqual((result["agent"], result["model"], result["effort"]),
                                     (selected["engine"], selected["model"], selected["effort"]))

    def test_missing_or_invalid_chain_leaves_default_fallback_available(self):
        for raw in (None, b'{"fallbacks":{}}', b'{"schema_version":1,"fallbacks":{}}',
                    json.dumps(dict(schema_version=1, fallbacks={f"agy:{MODEL}": False})).encode()):
            with self.subTest(raw=raw):
                if raw is not None:
                    self.write_raw(raw)
                policy = reader.read_agy_policy(MODEL)
                self.assertFalse(policy.has_fallback_override)
                self.assertIsNone(policy.fallback)


if __name__ == "__main__":
    unittest.main()
