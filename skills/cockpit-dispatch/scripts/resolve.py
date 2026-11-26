#!/usr/bin/env python3
"""Inspect the effective Cockpit route without connecting to the sidecar."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sidecars" / "cockpit"))
import dispatch_policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--role", choices=dispatch_policy.ROLE_READ_ONLY)
    action.add_argument("--all", action="store_true")
    action.add_argument("--capabilities", action="store_true")
    parser.add_argument("--policy-path", type=Path)
    parser.add_argument("--state-path", type=Path)
    args = parser.parse_args(argv)
    if args.capabilities:
        result = dispatch_policy.probe_capabilities(state_path=args.state_path)
    elif args.all:
        result = dispatch_policy.resolve_all(policy_path=args.policy_path, state_path=args.state_path)
    else:
        result = dispatch_policy.resolve_role(args.role, policy_path=args.policy_path, state_path=args.state_path)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
