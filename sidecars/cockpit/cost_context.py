"""Explicit live bindings between the cost pipeline and its server host."""
from __future__ import annotations


class _Read:
    def __init__(self, name):
        self.name = name

    def __get__(self, host, owner=None):
        if host is None:
            return self
        return host._bindings[self.name]


class _Write(_Read):
    def __set__(self, host, value):
        host._bindings[self.name] = value


class CostContext:
    """Expose only declared cost state and hooks, not the server module.

    Bindings stay live when the server replaces a snapshot or an isolated test
    swaps out state. Access never acquires a lock: the cost pipeline owns lock
    ordering and chooses when to read or publish a group of related fields.
    """

    __slots__ = ("_bindings",)

    def __init__(self, bindings):
        self._bindings = bindings

    state_lock = _Read("_STATE_LOCK")
    cost_lock = _Read("_COST_LOCK")
    builtin_enum_lock = _Read("_BUILTIN_ENUM_LOCK")

    quota = _Write("_QUOTA_CACHE")
    window_costs = _Write("_WINDOW_COSTS")
    window_estimates = _Write("_WINDOW_ESTIMATES")
    last_quota_fetch = _Write("_LAST_QUOTA_FETCH_TS")

    files = _Read("_COST_FILE_EVENTS")
    pending_rebuild = _Read("_PENDING_REBUILD")
    generation = _Write("_COST_GENERATION")
    droid_ledger = _Read("_DROID_LEDGER")
    builtin_models = _Read("_BUILTIN_SUBAGENT_MODEL_ENUM")
    db_applied_builtin = _Read("_DB_APPLIED_BUILTIN_ENUM")
    applied_pricing_epoch = _Write("_APPLIED_PRICING_EPOCH")
    calendar_days = _Write("_CALENDAR_DAYS")
    calendar = _Write("_CALENDAR_CACHE")

    cache_loaded = _Write("_COST_CACHE_LOADED")
    cache_path = _Read("_COST_CACHE_FILE")
    cache_schema = _Read("_COST_CACHE_SCHEMA")
    pricing_epoch = _Read("PRICING_EPOCH")
    app_dirs = _Read("SUBAGENT_APP_DIRS")
    logger = _Read("_LOG")
    runtime = _Read("_COST_RUNTIME")

    normalize = _Read("_normalize_cost_event")
    build_calendar = _Read("_build_calendar")
    save = _Read("_save_cost_events_cache")
    analyze_droid = _Read("analyze_droid_session")
    snapshot_builtin_models = _Read("snapshot_builtin_model_enum")
