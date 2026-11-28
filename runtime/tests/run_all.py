#!/usr/bin/env python3
"""Run the complete runtime verification suite with only the Python standard library."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from _runtime_fixture import isolated_env

SUITES = (
    "test_runtime_architecture.py",
    "test-sa-guard.py",
    "verify_dispatch_policy.py",
    "test_installer.py",
    "test_agy_quota.py",
    "test_agy_quota_e2e.py",
    "verify_agent_runtime.py",
    "test_sa_run_view.py",
)


def main():
    tests = Path(__file__).resolve().parent
    # A subprocess per suite isolates environment, mocks and module-level caches.
    flags = {flag: os.environ[flag] for flag in ("SA_TEST_LIVE_CLI", "SA_GUARD_TEST_SUBPROCESS_ALL")
             if flag in os.environ}
    for suite in SUITES:
        print(f"\n=== {suite} ===", flush=True)
        with tempfile.TemporaryDirectory(prefix="cockpit-test-home-") as home:
            completed = subprocess.run([sys.executable, "-B", "-u", str(tests / suite)],
                                       cwd=tests.parents[1], env=isolated_env(home, flags))
        if completed.returncode:
            print(f"FAILED: {suite} (exit {completed.returncode})", file=sys.stderr)
            return 1
    print(f"ALL {len(SUITES)} RUNTIME SUITES PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
