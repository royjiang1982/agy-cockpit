"""Incremental cost scans for external CLI sessions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cost_context import CostContext
from cost_updates import scan_droid_file
from pricing import needs_reprice


@dataclass(frozen=True)
class CostScanSource:
    agent: str
    root: Path
    pattern: str
    recursive: bool
    analyze: Callable[[Path], dict | None] | None = None
    companion_files: tuple[str, ...] = ()


def _scan_metric_file(source, path, reusable, cutoff, reprice_since):
    mtime = path.stat().st_mtime
    target = path
    if source.companion_files:
        target = path.parent
        for name in source.companion_files:
            companion = target / name
            if companion.exists():
                mtime = max(mtime, companion.stat().st_mtime)
    if mtime < cutoff:
        return None
    key = f"{source.agent}:{target}"
    cached = reusable.get(key)
    if cached and abs(cached[0] - mtime) < 0.001 and not needs_reprice(cached, mtime, reprice_since):
        return None
    metrics = source.analyze(target)
    return (key, (mtime, list(metrics.get("cost_events") or []))) if metrics is not None else None


def scan_cli_session_costs(state: CostContext, sources, updates, reusable, previous,
                           cutoff, reprice_since, list_paths):
    for source in sources:
        if not source.root.exists():
            continue
        try:
            for path in list_paths(source.root, source.pattern, recursive=source.recursive):
                try:
                    if source.analyze is None:  # Droid retains its separate ledger/probe rules.
                        scanned = scan_droid_file(state, path, reusable, previous, cutoff)
                    else:
                        scanned = _scan_metric_file(source, path, reusable, cutoff, reprice_since)
                    if scanned is not None:
                        key, entry = scanned
                        updates[key] = entry
                except Exception:
                    continue
        except Exception:
            pass
