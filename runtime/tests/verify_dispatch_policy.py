"""Offline tests for the read-only dispatch policy and its two consumers."""
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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
        env = {key: value for key, value in os.environ.items() if not key.startswith("SA_AGY_")}
        env["SA_DISPATCH_POLICY_PATH"] = str(self.path)
        sandbox = patch.dict(os.environ, env, clear=True)
        sandbox.start()
        self.addCleanup(sandbox.stop)

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

    def test_default_path_and_unusable_files(self):
        with patch.dict(os.environ, SA_DISPATCH_POLICY_PATH=""), patch.object(Path, "home", return_value=self.path.parent):
            default = self.path.parent / ".gemini/antigravity/sidecar_data/statusline/dispatch-policy.json"
            self.assertEqual(reader._read_policy(), {})
            default.parent.mkdir(parents=True)
            default.write_text('{"schema_version":1,"codex_fast":false}', encoding="utf-8")
            self.assertIs(reader.read_codex_fast(), False)
        for invalid in (b"{", b"\xff", b"[]", b"null", b'{"codex_fast":true,"n":NaN}',
                        b'{"schema_version":2,"codex_fast":false}'):
            self.write_raw(invalid)
            self.assertEqual(reader._read_policy(), {})
            self.assertEqual(self.path.read_bytes(), invalid)
        self.write(codex_fast=True)
        with patch.object(Path, "read_bytes", side_effect=PermissionError):
            self.assertEqual(reader._read_policy(), {})
        self.assertIs(reader.read_codex_fast(), True)
        with patch.object(Path, "stat", side_effect=PermissionError):
            self.assertEqual(reader._read_policy(), {})

    def test_strict_schema_and_duplicate_keys_invalidate_entire_policy(self):
        for raw in (b'{"codex_fast":false}',
                    *(b'{"schema_version":' + version + b',"codex_fast":false}'
                      for version in (b"0", b"-1", b"true", b"1.0", b'"1"', b"2")),
                    b'{"schema_version":1,"schema_version":1,"codex_fast":false}',
                    b'{"schema_version":1,"codex_fast":false,"codex_fast":true}',
                    b'{"schema_version":1,"codex_fast":false,"quota_fallback":{"enabled":true,"enabled":false}}'):
            with self.subTest(raw=raw):
                self.write_raw(raw)
                self.assertEqual(reader._read_policy(), {})
                self.assertIsNone(reader.read_codex_fast())
                self.assertFalse(reader.read_agy_policy(MODEL).has_fallback_override)

    def test_disabled_policy_reverts_codex_fast_and_agy_fallback(self):
        for flag in (False, 0, "true", None):
            with self.subTest(enabled=flag):
                self.write(enabled=flag, codex_fast=False,
                           quota_fallback=dict(enabled=False, threshold_pct=50),
                           fallbacks={f"agy:{MODEL}": [candidate()]})
                self.assertEqual(reader._read_policy(), {})
                self.assertIsNone(reader.read_codex_fast())
                self.assertEqual(self.codex_tier(), "fast")
                self.assertEqual(reader.read_agy_policy(MODEL), (None, None, None, False))

    def test_malformed_fields_do_not_override_defaults(self):
        for invalid in (-1, 51, True, "5", 5.0):
            self.write(codex_fast=0, quota_fallback=dict(enabled=1, threshold_pct=invalid))
            self.assertIsNone(reader.read_codex_fast())
            self.assertEqual(reader.read_agy_policy(MODEL), (None, None, None, False))

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
    def test_quota_exact_mapping_validation_and_env_precedence(self, _snapshot, _find):
        chain = [None, candidate(enabled=False), candidate(enabled=1), candidate(channel="builtin"),
                 candidate(engine="codex"), candidate(effort="xhigh\n"), candidate(effort="custom")]
        chain += [candidate(model=value) for value in (
            "a/b/c", "a\\b", "a..b", "a/../b", "a\x00b", "a\nb", "a ", "--model", "a --model",
            "a;b", "a|b", "a&b", "a>b", "a$(b)", "a\x60b",
        )]
        first = candidate("custom-droid", "same")
        del first["enabled"]
        self.write(fallbacks={f"agy:{MODEL}": [*chain, first, candidate("second")]})

        def target(model=MODEL):
            request = quota.preflight_agy_request(model=model, effort="low", prompt="task", mode="plan")
            self.assertEqual((request["agent"], request["mode"]), ("droid", "spec"))
            return request["model"], request["effort"]

        self.assertEqual(target(), ("custom-droid", "medium"))
        self.assertEqual(target("gemini-4-neon-xhigh"), ("gemini-4-argon", "medium"))
        with patch.dict(os.environ, SA_AGY_FALLBACK_MODEL="env-droid"):
            self.assertEqual(target(), ("env-droid", "medium"))
        self.write(fallbacks={f"agy:{MODEL}": [candidate(effort="xhigh")]})
        self.assertEqual(target(), ("custom-droid", "xhigh"))
        with patch.dict(os.environ, SA_AGY_FALLBACK_EFFORT="low"):
            self.assertEqual(target(), ("custom-droid", "low"))
        with patch.dict(os.environ, SA_AGY_FALLBACK_MODEL=""):
            self.assertEqual(target(), ("gemini-4-argon", "medium"))
        for model in ("provider/mo.del:v-2", "Model Name (Preview)"):
            with self.subTest(model=model):
                self.write(fallbacks={f"agy:{MODEL}": [candidate(model, "")]})
                self.assertEqual(target(), (model, ""))

    @patch.object(quota, "find_cli_binary", return_value="/mock/droid")
    @patch.object(quota, "_quota_for_model", return_value=dict(rem_frac=0, reset_ts=0))
    def test_same_uses_agy_model_suffix_or_historical_default(self, _snapshot, _find):
        models = [("gemini-4-neon-low", "low"), ("gemini-4-neon-high", "high"),
                  ("gemini-4-neon-xhigh", "xhigh"), ("gemini-4-neon", "medium"),
                  ("claude-opus-5-5", "high")]
        self.write(fallbacks={f"agy:{model}": [candidate(effort="same")] for model, _ in models})
        for model, effort in models:
            with self.subTest(model=model):
                result = quota.preflight_agy_request(model=model, prompt="task")
                self.assertEqual((result["agent"], result["model"], result["effort"]),
                                 ("droid", "custom-droid", effort))
                self.assertEqual(result["event"]["to_effort"], effort)

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
    def test_missing_or_invalid_chain_preserves_hardcoded_fallback(self, _snapshot, _find):
        for raw in (None, b'{"fallbacks":{}}', b'{"schema_version":1,"fallbacks":{}}',
                    json.dumps(dict(schema_version=1, fallbacks={f"agy:{MODEL}": False})).encode()):
            with self.subTest(raw=raw):
                if raw is not None:
                    self.write_raw(raw)
                policy = reader.read_agy_policy(MODEL)
                self.assertFalse(policy.has_fallback_override)
                self.assertIsNone(policy.fallback)
                result = quota.preflight_agy_request(model=MODEL, prompt="task")
                self.assertEqual((result["agent"], result["model"], result["effort"]),
                                 ("droid", "gemini-4-argon", "medium"))

    @patch.object(quota, "_quota_for_model", return_value=dict(rem_frac=0, reset_ts=0))
    def test_low_quota_without_droid_leaves_agy_and_session_unchanged(self, _snapshot):
        self.write(fallbacks={f"agy:{MODEL}": [candidate()]})
        original = dict(agent="agy", model=MODEL, effort="high", requested_sid="existing", mode="plan", prompt="task")
        with patch.object(quota, "find_cli_binary", return_value=None) as find, \
                patch.object(quota, "build_forced_handoff_prompt") as handoff:
            self.assertEqual(quota.preflight_agy_request(**original),
                             dict(original, should_fallback=False, event=None, handoff=None))
            find.assert_called_once_with("droid")
            handoff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
