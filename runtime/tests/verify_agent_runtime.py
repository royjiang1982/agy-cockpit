#!/usr/bin/env python3
"""Entry point for the core suites covering all five CLI engines."""
import os

from acp_approval_cases import acp_approval_cases
from agy_session_new_cases import agy_session_new_cases
import test_runtime_cli as cli
import test_runtime_daemon as daemon
import test_runtime_streams as streams
import test_session_handoff as handoff


def main():
    os.environ["SA_AGY_QUOTA_PREFLIGHT"] = "0"
    os.environ["SA_DISPATCH_POLICY_PATH"] = os.devnull
    for suite in (
        handoff.session_cases, cli.codex_cases, agy_session_new_cases, acp_approval_cases,
        cli.cli_emission_cases, cli.client_transition_cases,
        cli.model_effort_cases, cli.test_command_builders, streams.test_acp_and_pi_resident_streams,
        *daemon.CASES, streams.test_pi_failures,
        streams.test_zomibot_drop_in_compatibility, handoff.test_session_auto_handoff,
        handoff.test_legacy_session_handoff,
        streams.test_real_cli_resident_handshakes,
    ):
        suite()
    print("ALL AGENT-RUNTIME VERIFICATIONS PASSED.")


if __name__ == "__main__":
    main()
