"""Isolate the sidecar's cost state for scan and aggregation tests."""
from unittest.mock import patch

import server
from droid_ledger import DroidLedger


def patch_cost_state(stack, *, path, runtime, events, ledger=None, **overrides):
    values = {
        "_COST_RUNTIME": runtime, "_COST_CACHE_FILE": path, "_COST_FILE_EVENTS": events,
        "_COST_CACHE_LOADED": True, "_COST_GENERATION": 0, "_LAST_COST_SCAN_TS": 0,
        "_DROID_LEDGER": ledger if ledger is not None else DroidLedger(), "_PENDING_REBUILD": set(),
        "_DB_APPLIED_BUILTIN_ENUM": {}, "_APPLIED_PRICING_EPOCH": server.PRICING_EPOCH,
        "_APPLIED_MODEL_MAP_EPOCH": server.get_model_map_epoch(),
        "_QUOTA_CACHE": {}, "_WINDOW_COSTS": {}, "_WINDOW_ESTIMATES": {},
        "_CALENDAR_CACHE": {}, "_CALENDAR_DAYS": {},
        **overrides,
    }
    for name, value in values.items():
        stack.enter_context(patch.object(server, name, value))
