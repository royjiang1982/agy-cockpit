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
    parser.add_argument("--count", type=int, help=f"panel seat count (1–{dispatch_policy.MAX_PANEL_COUNT})")
    parser.add_argument("--read-only", action="store_true", help="require a read-only role route")
    parser.add_argument("--blind", action="store_true", help="exclude Swarm from panel seats and fallbacks")
    args = parser.parse_args(argv)
    if args.read_only and args.role is None:
        parser.error("--read-only can only be used with --role")
    if args.blind and args.role != "panel":
        parser.error("--blind can only be used with --role panel")
    if args.count is not None:
        if args.role != "panel":
            parser.error("--count can only be used with --role panel")
        if not 1 <= args.count <= dispatch_policy.MAX_PANEL_COUNT:
            parser.error(f"--count must be an integer from 1 to {dispatch_policy.MAX_PANEL_COUNT}")
    if args.capabilities:
        result = dispatch_policy.probe_capabilities(state_path=args.state_path)
    elif args.all:
        result = dispatch_policy.resolve_all(policy_path=args.policy_path, state_path=args.state_path)
    else:
        result = dispatch_policy.resolve_role(args.role, policy_path=args.policy_path, state_path=args.state_path,
                                              count=args.count, read_only=args.read_only, blind=args.blind)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
