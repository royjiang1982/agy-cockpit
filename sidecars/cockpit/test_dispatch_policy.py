"""Dispatch policy validation, capability probing, and route selection."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dispatch_models as dm
import dispatch_policy as dp

NOW = 1_700_000_000


class DispatchFixture(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.policy_path = self.root / "dispatch-policy.json"
        self.state_path = self.root / "view_state.json"
        self.codex_cache = self.root / "codex_models.json"
        self.grok_cache = self.root / "grok_models.json"
        self.agy_state = self.root / "agy_state.json"
        stack.enter_context(patch.dict(os.environ, {
            "HOME": str(self.root), "SA_DISPATCH_POLICY_PATH": "", "SA_RUNTIME_BIN": "",
        }))
        # These paths were bound at import time, before redirecting HOME.
        stack.enter_context(patch.multiple(dp, STATE_PATH=self.state_path,
                                           _CLI_BIN_DIRS=(), _EXTRA_WRAPPER_DIRS=()))
        stack.enter_context(patch.object(dp.shutil, "which", return_value=None))
        stack.enter_context(patch.multiple(
            dm, _CODEX_CACHE=self.codex_cache, _DEFAULT_CODEX_CACHE=self.codex_cache,
            _GROK_CACHE=self.grok_cache, _AGY_STATE=self.agy_state, _FACTORY_CACHE_DIR=self.root / "factory",
            _FILE_CACHE={}, _CODEX_RPC_CACHE=None,
        ))
        # Model discovery also has system-wide fallback locations and can launch app-server.
        stack.enter_context(patch.object(dm, "_find_codex_bin", return_value=None))


class DispatchPolicyTests(DispatchFixture):
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

    def test_corrupted_policy_stops_single_and_all_routes_without_probing(self):
        self.policy_path.write_text("{")
        warning = dp.load_policy(self.policy_path)["read_only_warning"]
        stopped = dict(mode="degraded", channel="direct", read_only=True, degraded=True,
                       reason="policy_corrupted", warning=warning)
        with patch.object(dp, "probe_capabilities") as probe:
            self.assertEqual(dp.resolve_role("expert", read_only=True), stopped)
            self.assertEqual(dp.resolve_all(policy_path=self.policy_path), {
                role: dict(stopped, read_only=read_only) for role, read_only in dp.ROLE_READ_ONLY.items()
            })
            probe.assert_not_called()

    def test_path_probe_and_safe_native_fallbacks(self):
        caps = dp.probe_capabilities(self.state_path)
        self.assertEqual(caps["available_engines"], [])
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

    def test_disabled_or_unavailable_cli_skips_model_resolution(self):
        p = dp.default_policy()
        p["roles"]["deep"]["primary"] = dp._target("codex", "sol", "high")
        p["fallbacks"]["codex:sol"].append(dp._target("agy", "inherit", channel="invoke_subagent"))
        for external_enabled, engines, reason in (
            (False, ["codex", "droid"], "external_disabled"),
            (True, [], "cli_unavailable"),
        ):
            with self.subTest(reason=reason), patch.object(dp.dispatch_models, "resolve_model_id") as resolve:
                p["external_enabled"] = external_enabled
                route = dp.resolve_role("deep", p, {"available_engines": engines})
                resolve.assert_not_called()
                self.assertEqual((route["channel"], route["model"], route["reason"]),
                                 ("invoke_subagent", "inherit", "fallback"))
                self.assertEqual([(item["engine"], item["reason"]) for item in route["skipped"]],
                                 [("codex", reason), ("droid", reason)])

    def test_panel_fills_orders_after_both_skipped_and_selected_seats(self):
        p = dp.default_policy()
        local = dp._target("agy", "inherit", channel="invoke_subagent")
        p["roles"]["panel"]["seats"] = [local, dp._target("agy", "hydrogen"), dp._target("droid", "sol")]
        base = p["roles"]["panel"]["seats"]
        for config, seats, skipped in ((base, [1, 4, 5], [2, 3]), (base[1:] + base[:1], [3, 4, 5], [1, 2])):
            p["roles"]["panel"]["seats"] = config
            route = dp.resolve_role("panel", p, {"available_engines": []}, count=3)
            self.assertEqual([seat["order"] for seat in route["seats"]], seats)
            self.assertEqual([seat["order"] for seat in route["skipped_seats"]], skipped)
            self.assertTrue(all(seat["reason"] == "builtin_fallback" for seat in route["seats"][1:]))

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

    def test_read_only_expert_rejects_swarm_and_keeps_entire_chain_read_only(self):
        p = dict(dp.default_policy(), allow_swarm=True)
        caps = {"available_engines": ["grok"]}
        expert = p["roles"]["expert"]
        swarm = dp._target("agy", "Gemini 4 Neon (High)", channel="swarm")
        cli = dp._target("grok", "grok-4.7", "high")
        native = dp._target("agy", "inherit", channel="invoke_subagent")
        p["fallbacks"]["grok:grok-4.7"] = [swarm, native]
        p["fallbacks"][f"agy:{swarm['model']}"] = [native]
        for primary, routes in ((cli, [("cli", None), ("invoke_subagent", "research"), ("invoke_subagent", "research")]),
                                (swarm, [("invoke_subagent", "research"), ("invoke_subagent", "research")])):
            with self.subTest(primary=primary["channel"]):
                expert["primary"] = primary
                resolved = dp.resolve_role("expert", p, caps, read_only=True)
                chain = [resolved, *resolved["fallbacks"]]
                self.assertTrue(all(item["read_only"] for item in chain))
                self.assertEqual([(item["channel"], item.get("type")) for item in chain], routes)
                self.assertEqual(resolved["skipped"], [
                    dict(channel="swarm", engine="agy", model=swarm["model"], reason="read_only_requires_isolated")
                ])

    def test_blind_panel_excludes_swarm_primaries_and_fallbacks(self):
        p = dict(dp.default_policy(), allow_swarm=True)
        panel = p["roles"]["panel"]
        swarm = dp._target("agy", "Gemini 4 Neon (High)", channel="swarm")
        panel["seats"] = [swarm, dp._target("agy", "inherit", channel="invoke_subagent")]
        p["fallbacks"]["agy:inherit"] = [swarm]
        caps = {"available_engines": []}
        ordinary = dp.resolve_role("panel", p, caps)
        self.assertEqual(ordinary["seats"][0]["channel"], "swarm")
        self.assertEqual(ordinary["seats"][1]["fallbacks"][0]["channel"], "swarm")
        blind_skip = dict(channel="swarm", engine="agy", model=swarm["model"], reason="blind_requires_isolated")
        for count in (None, 2):
            with self.subTest(count=count):
                result = dp.resolve_role("panel", p, caps, blind=True, count=count)
                self.assertTrue(result["blind_isolated"])
                self.assertEqual(len(result["seats"]), 2)
                for seat in result["seats"]:
                    self.assertTrue(seat["blind_isolated"])
                    self.assertTrue(all(item["channel"] != "swarm" for item in [seat, *seat["fallbacks"]]))
                self.assertEqual([seat for seat in result["seats"] if seat["model"] == "inherit"][0]["skipped"],
                                 [blind_skip])
                if count is None:
                    self.assertEqual(result["seats"][0]["skipped"], [blind_skip])
                else:
                    self.assertEqual(result["skipped_seats"][0]["reason"], "blind_requires_isolated")
                    self.assertEqual(result["seats"][-1]["reason"], "builtin_fallback")
        panel["seats"] = []
        empty = dp.resolve_role("panel", p, caps, blind=True)
        self.assertEqual(empty["seats"], [])
        self.assertFalse(empty["blind_isolated"])

    def test_codex_fallback_inherits_source_effort_even_when_primary_is_unavailable(self):
        p = dp.default_policy()
        for effort, expected in (("", "high"), ("xhigh", "xhigh")):
            with self.subTest(effort=effort):
                p["roles"]["deep"]["primary"] = dp._target("codex", "sol", effort)
                resolved = dp.resolve_role("deep", p, {"available_engines": ["droid"]})
                self.assertEqual((resolved["engine"], resolved["effort"], resolved["reason"]),
                                 ("droid", expected, "fallback"))

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
                binaries = {exe: exe for exe in ("sa-codex", "codex", "sa-droid", "droid")}
                with patch.object(dp.shutil, "which", side_effect=binaries.get):
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

    def test_wrapper_requires_executable_cli_and_is_not_discovered_with_empty_path(self):
        wrappers = self.root / "wrapper-bin"
        wrappers.mkdir()
        wrapper = wrappers / "sa-codex"
        wrapper.touch()
        wrapper.chmod(0o755)
        cli = self.root / "codex"

        # Restore the real lookup only here, with PATH and all bin directories in the fixture.
        with (
            patch.object(dp.shutil, "which", dp._REAL_WHICH),
            patch.object(dp, "_CLI_BIN_DIRS", (self.root,)),
        ):
            for path in (wrappers, self.root / "empty-path"):
                with self.subTest(path=path.name), patch.dict(
                    os.environ, {"PATH": str(path), "SA_RUNTIME_BIN": str(wrappers)}
                ):
                    cli.unlink(missing_ok=True)
                    self.assertEqual(dp.probe_capabilities(self.state_path)["available_engines"], [])
                    cli.touch()
                    cli.chmod(0o644)
                    self.assertEqual(dp.probe_capabilities(self.state_path)["available_engines"], [])
                    cli.chmod(0o755)
                    self.assertEqual(dp.probe_capabilities(self.state_path)["available_engines"], ["codex"])
            with patch.dict(os.environ, {"PATH": "", "SA_RUNTIME_BIN": str(wrappers)}):
                self.assertEqual(dp.probe_capabilities(self.state_path)["available_engines"], [])


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
        self.assertEqual(len(dp.resolve_role("panel", p, caps, count=12)["seats"]), 12)
        for invalid in (0, 13, 2.0, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                dp.resolve_role("panel", p, caps, count=invalid)
