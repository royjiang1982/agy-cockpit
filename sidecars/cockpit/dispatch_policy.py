"""Local dispatch preferences and capability-only routing (never runs a model)."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import dispatch_models

DATA_DIR = Path.home() / ".gemini/antigravity/sidecar_data/statusline"
STATE_PATH = DATA_DIR / "view_state.json"
SCHEMA_VERSION = 1
ROLE_READ_ONLY = {"scout": True, "standard": False, "deep": False, "expert": False, "panel": True}
ENGINES = ("agy", "codex", "droid", "grok", "pi")
CHANNELS = ("cli", "invoke_subagent", "swarm")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max", "ultra", "same")
NATIVE_MODELS = ("flash_lite", "flash", "inherit", "pro")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/() -]{0,80}", re.ASCII)
_NAMESPACED_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:-]*")
_LABEL = re.compile(r"[^()]*(?: \([A-Za-z0-9._ -]+\))?")
MAX_QUOTA_AGE_SEC = 300
MAX_PANEL_COUNT = 12
_LOCK, _REAL_WHICH = threading.Lock(), shutil.which
_BUNDLED_BIN = Path(__file__).resolve().parents[2] / "runtime" / "bin"
_EXTRA_WRAPPER_DIRS = (Path.home() / ".gemini/config/bin", Path.home() / ".local/bin")
_CLI_BIN_DIRS = (Path.home() / ".local/bin", Path.home() / ".grok/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin"))

def _has_wrapper(engine):
    exe = f"sa-{engine}"
    if not shutil.which(exe):
        if shutil.which is not _REAL_WHICH or not os.environ.get("PATH"):
            return False
        env_bin = os.environ.get("SA_RUNTIME_BIN")
        dirs = (Path(env_bin),) if env_bin else (*_EXTRA_WRAPPER_DIRS, _BUNDLED_BIN)
        if not any((d / exe).is_file() and os.access(d / exe, os.X_OK) for d in dirs):
            return False
    return bool(shutil.which(engine)) or any(
        (d / engine).is_file() and os.access(d / engine, os.X_OK) for d in _CLI_BIN_DIRS)

class PolicyConflictError(Exception):
    """The revision has changed or the on-disk policy cannot be safely edited."""

def _target(engine, model, effort="", channel="cli", **extras):
    return dict(channel=channel, engine=engine, model=model, effort=effort, **extras)

def default_policy():
    primaries = {
        "scout": _target("agy", "hydrogen", "high"),
        "standard": _target("agy", "neon", "high"),
        "deep": _target("agy", "hydrogen", "high"),
        "expert": _target("agy", "hydrogen", "xhigh"),
    }
    roles = {name: dict(read_only=ROLE_READ_ONLY[name], primary=primary) for name, primary in primaries.items()}
    roles["panel"] = dict(read_only=True, seats=[
        _target("agy", "neon", "xhigh", enabled=True),
        _target("agy", "carbon", "high", enabled=True),
        _target("agy", "opus", "high", enabled=True),
    ])
    fallback_specs = {"agy": ("hydrogen", "neon", "carbon", "opus"), "codex": ("sol", "astra")}
    return dict(schema_version=SCHEMA_VERSION, revision=1, enabled=True, external_enabled=True,
                allow_swarm=False, codex_fast=True, quota_fallback=dict(enabled=True, threshold_pct=5),
                roles=roles, fallbacks={f"{engine}:{model}": [_target("droid", model, "same", enabled=True)]
                                       for engine, models in fallback_specs.items() for model in models})

def _fields(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= obj.keys() or obj.keys() - set(required) - set(optional):
        raise ValueError("unexpected or missing policy fields")
def _bool(value):
    if type(value) is not bool:
        raise ValueError("policy switches must be boolean")
def _revision(value):
    if type(value) is not int or value < 1:
        raise ValueError("revision must be a positive integer")
def _model_name(name):
    return (isinstance(name, str) and _MODEL.fullmatch(name) and ".." not in name
            and name == name.strip() and not re.search(r"(?:^| )--?\S", name)
            and (_NAMESPACED_MODEL.fullmatch(name) if "/" in name else _LABEL.fullmatch(name)))
def _validate_target(target, allow_enabled=False):
    _fields(target, ("channel", "engine", "model", "effort"), ("enabled",) if allow_enabled else ())
    channel, engine, model, effort = (target[key] for key in ("channel", "engine", "model", "effort"))
    if channel not in CHANNELS or engine not in ENGINES or effort not in EFFORTS or not _model_name(model):
        raise ValueError("invalid dispatch target")
    if channel != "cli" and engine != "agy":
        raise ValueError("native targets must use agy")
    if channel == "invoke_subagent" and model not in NATIVE_MODELS:
        raise ValueError("invalid invoke_subagent model")
    if "enabled" in target:
        _bool(target["enabled"])
def validate_policy(payload):
    """Validate a complete v1 policy; normalize optional invariant read_only fields."""
    switches = ("enabled", "external_enabled", "allow_swarm", "codex_fast")
    _fields(payload, ("schema_version", "revision", "quota_fallback", "roles", "fallbacks", *switches))
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    _revision(payload["revision"])
    for name in switches:
        _bool(payload[name])
    quota = payload["quota_fallback"]
    _fields(quota, ("enabled", "threshold_pct"))
    _bool(quota["enabled"])
    threshold = quota["threshold_pct"]
    if type(threshold) is not int or not 0 <= threshold <= 50:
        raise ValueError("invalid quota threshold")
    _fields(payload["roles"], ROLE_READ_ONLY)
    roles = {}
    for name, read_only in ROLE_READ_ONLY.items():
        role = payload["roles"][name]
        field = "seats" if name == "panel" else "primary"
        _fields(role, (field,), ("read_only",))
        if "read_only" in role and role["read_only"] is not read_only:
            raise ValueError("read_only is fixed by role")
        targets = role[field] if field == "seats" else [role[field]]
        if not isinstance(targets, list):
            raise ValueError("panel seats must be a list")
        for target in targets:
            _validate_target(target, allow_enabled=field == "seats")
        copies = [dict(target) for target in targets]
        roles[name] = {field: copies if field == "seats" else copies[0], "read_only": read_only}
    fallbacks = payload["fallbacks"]
    if not isinstance(fallbacks, dict):
        raise ValueError("fallbacks must be an object")
    for key, targets in fallbacks.items():
        engine, sep, model = key.partition(":") if isinstance(key, str) else ("", "", "")
        if not sep or engine not in ENGINES or not _model_name(model) or not isinstance(targets, list):
            raise ValueError("invalid fallback key or list")
        for target in targets:
            _validate_target(target, allow_enabled=True)
    return dict(payload, roles=roles, quota_fallback=dict(quota),
                fallbacks={key: [dict(target) for target in targets] for key, targets in fallbacks.items()})

def unique_json_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out

def policy_file_path(policy_path=None, *, state_path=None):
    if policy_path is not None:
        return Path(policy_path)
    override = os.environ.get("SA_DISPATCH_POLICY_PATH")
    return Path(override) if override else Path(state_path or STATE_PATH).with_name("dispatch-policy.json")

def load_policy(policy_path=None):
    """Return policy and disk health without writing anything."""
    path = policy_file_path(policy_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
        return dict(policy=validate_policy(payload), exists=True, degraded=False, read_only_warning=None)
    except FileNotFoundError:
        return dict(policy=default_policy(), exists=False, degraded=False, read_only_warning=None)
    except (OSError, ValueError, RecursionError):
        return dict(policy=default_policy(), exists=True, degraded=True,
                    read_only_warning="Policy is unreadable, invalid, or uses an unsupported schema; it was not changed.")

@contextmanager
def _policy_file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep this inode stable across os.replace(policy) and across writers.
    fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)

def _write_policy(payload, expected_revision, policy_path):
    _revision(expected_revision)
    path = policy_file_path(policy_path)
    with _LOCK, _policy_file_lock(path):
        current = load_policy(path)
        if current["degraded"]:
            raise PolicyConflictError(current["read_only_warning"])
        if current["policy"]["revision"] != expected_revision:
            raise PolicyConflictError("policy revision changed; reload before saving")
        updated = dict(payload, revision=expected_revision + 1)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(updated, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return updated

def save_policy(payload, *, expected_revision, policy_path=None):
    policy = validate_policy(payload)
    if policy["revision"] != expected_revision:
        raise ValueError("payload and expected revision do not match")
    return _write_policy(policy, expected_revision, policy_path)

def reset_policy(*, expected_revision, policy_path=None):
    return _write_policy(default_policy(), expected_revision, policy_path)

def _quota_cache(state_path):
    if state_path is not None:
        paths = (Path(state_path),)
    else:
        alt = STATE_PATH.with_name("state.json" if STATE_PATH.name == "view_state.json" else "view_state.json")
        paths = (STATE_PATH, alt)
    for path in paths:
        try:
            state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
            cache = state.get("quota_cache") if isinstance(state, dict) else None
            return cache if isinstance(cache, dict) else {}
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError):
            continue
    return {}

def _number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None

def _quota_hint(row, now):
    observed, reset, fraction = _number(row.get("observed_at")), _number(row.get("reset_ts")), _number(row.get("rem_frac"))
    pct = _number(row.get("pct", None if fraction is None or not 0 <= fraction <= 1 else fraction * 100))
    if pct is not None and not 0 <= pct <= 100:
        pct = None
    fresh = (pct is not None and observed is not None and observed > 0 and 0 <= now - observed <= MAX_QUOTA_AGE_SEC
             and not row.get("stale") and reset is not None and 0 < reset and now < reset)
    return dict(pct=pct, observed_at=observed, reset_ts=reset, fresh=fresh)

def _limiting_hint(rows):
    fresh = [row for row in rows if row["fresh"]]
    return (min(fresh, key=lambda row: (row["pct"], -row["observed_at"])) if fresh else
            max(rows, key=lambda row: row["observed_at"] or 0, default=_quota_hint({}, 0)))
def probe_capabilities(state_path=None, *, now=None):
    """Executable discovery plus persisted (never polled) quota samples."""
    now = time.time() if now is None else now
    quota, agy_families = {engine: [] for engine in ENGINES}, {"gemini": [], "3p": []}
    for bucket, row in _quota_cache(state_path).items():
        if not isinstance(row, dict):
            continue
        prefix = bucket.split("-", 1)[0]
        hint = _quota_hint(row, now)
        if prefix in agy_families:
            quota["agy"].append(hint)
            agy_families[prefix].append(hint)
        elif prefix in ENGINES:
            if prefix == "codex" and row.get("limit_id") not in (None, "", "codex"):
                continue  # Alternate limit IDs need model-specific evidence.
            quota[prefix].append(hint)
    policy = default_policy()
    models = {engine: set() for engine in ENGINES}
    for role in policy["roles"].values():
        for target in role.get("seats") or [role["primary"]]:
            models[target["engine"]].add(target["model"])
    for chain in policy["fallbacks"].values():
        for target in chain:
            models[target["engine"]].add(target["model"])
    return dict(available_engines=[engine for engine in ENGINES if _has_wrapper(engine)],
                models={engine: sorted(names) for engine, names in models.items()},
                quota={engine: _limiting_hint(rows) for engine, rows in quota.items()},
                agy_quota={family: _limiting_hint(rows) for family, rows in agy_families.items()
                           if rows or any(agy_families.values())})

def _agy_quota_family(model):
    names = set(re.findall(r"[a-z0-9]+", model.lower()))
    if names.intersection(("claude", "opus", "sonnet", "haiku")):
        return "3p"
    return "gemini" if names.intersection(("gemini", "neon", "argon", "carbon", "hydrogen", "flash")) else None

def _selection(target, policy, caps, state_path, *, read_only):
    channel, engine, raw_model, effort = (target[key] for key in ("channel", "engine", "model", "effort"))
    if not target.get("enabled", True):
        return None, "candidate_disabled"
    model = raw_model
    if channel == "cli":
        if not policy["external_enabled"]:
            return None, "external_disabled"
        if engine not in caps.get("available_engines", ()):
            return None, "cli_unavailable"
        codex_rpc = policy["external_enabled"] and "codex" in caps.get("available_engines", ())
        model = dispatch_models.resolve_model_id(engine, raw_model, effort, agy_state_path=state_path, allow_codex_rpc=codex_rpc)
        quota = caps.get("agy_quota", {}).get(_agy_quota_family(model), {}) if engine == "agy" else caps.get("quota", {}).get(engine, {})
        cutoff = policy["quota_fallback"]
        if cutoff["enabled"] and quota.get("fresh") and quota["pct"] <= cutoff["threshold_pct"]:
            return None, "quota_low"
    elif channel == "swarm" and not policy["allow_swarm"]:
        return None, "swarm_disabled"
    selected = dict(target, model=model, mode=channel, read_only=read_only)
    selected.pop("enabled", None)
    if channel == "cli" and engine == "codex":
        selected["codex_fast"] = policy["codex_fast"]
    if channel == "invoke_subagent":
        selected["type"] = "research" if read_only else "self"
    return selected, None

def _builtin_tails(name, policy, read_only):
    if not read_only and name in ("deep", "expert") and policy["allow_swarm"]:
        yield dict(mode="swarm", channel="swarm", engine="agy", model=f"Gemini 4 Neon ({'Medium' if name == 'deep' else 'High'})",
                   effort="", read_only=False, requires_model_check=True)
    model = {"scout": "flash", "standard": "inherit", "panel": "flash"}.get(name, "pro")
    yield dict(mode="invoke_subagent", channel="invoke_subagent", engine="agy", model=model,
               effort="", type="research" if read_only else "self", read_only=read_only)

def _resolve_target(primary, name, policy, caps, state_path, *, read_only, blind=False):
    # Keep existing read-only Swarm routes for scout and non-blind panel.
    swarm_skip = ("blind_requires_isolated" if blind else
                  "read_only_requires_isolated" if read_only and not ROLE_READ_ONLY[name] else None)
    effort = "" if primary["effort"] == "same" else primary["effort"]
    engine, raw_model = primary["engine"], primary["model"]
    resolved_model = raw_model
    if primary["channel"] == "cli" and policy["external_enabled"] and engine in caps.get("available_engines", ()):
        resolved_model = dispatch_models.resolve_model_id(engine, raw_model, effort, agy_state_path=state_path, allow_codex_rpc="codex" in caps.get("available_engines", ()))
    keys = dispatch_models.fallback_lookup_keys(engine, raw_model, resolved_model)
    chain = next((policy["fallbacks"][k] for k in keys if k in policy["fallbacks"]), [])
    available, skipped = [], []
    for i, target in enumerate([primary, *chain]):
        candidate = dict(target, effort=effort) if target["effort"] == "same" else target
        result, why = ((None, swarm_skip) if swarm_skip and target["channel"] == "swarm" else
                       _selection(candidate, policy, caps, state_path, read_only=read_only))
        if result is None:
            skipped.append(dict(channel=target["channel"], engine=target["engine"], model=target["model"], reason=why))
        elif not any(all(prior[k] == result[k] for k in ("channel", "engine", "model", "effort")) for prior in available):
            available.append(dict(result, reason="primary" if i == 0 else "fallback"))
    for tail in _builtin_tails(name, policy, read_only):
        if not any(all(prior[k] == tail[k] for k in ("channel", "engine", "model", "effort")) for prior in available):
            available.append(dict(tail, reason="builtin_fallback"))
    selected, *fallbacks = available
    return dict(selected, fallbacks=fallbacks, skipped=skipped)


def _panel_seat_meta(seat, order):
    seat["order"], seat["blind_isolated"] = order, seat["channel"] in ("cli", "invoke_subagent")
    seat["blind_reason"] = "independent_concurrent_call" if seat["blind_isolated"] else "shared_swarm_board"
    return seat

def _resolve_panel(config, policy, caps, count, state_path, *, read_only, blind):
    enabled = [seat for seat in config["seats"] if seat.get("enabled", True)]
    seats, skipped = [], []
    for i, seat in enumerate(enabled, 1):
        res = _resolve_target(seat, "panel", policy, caps, state_path, read_only=read_only, blind=blind)
        if count is None or res["reason"] in ("primary", "fallback"):
            seats.append(_panel_seat_meta(res, i))
            if len(seats) == count:
                break
        else:
            why = res["skipped"][0]["reason"] if res["skipped"] else "unavailable"
            skipped.append(dict(order=i, channel=seat["channel"], engine=seat["engine"], model=seat["model"], reason=why))
            if blind:
                skipped[-1]["skipped"] = res["skipped"]
    next_order = seats[-1]["order"] + 1 if seats else 1
    for i in range(next_order, next_order + (count or 0) - len(seats)):
        tail = next(_builtin_tails("panel", policy, read_only))
        seats.append(_panel_seat_meta(dict(tail, reason="builtin_fallback", fallbacks=[], skipped=[]), i))
    channels = {seat["channel"] for seat in seats}
    out = dict(mode="panel", channel=next(iter(channels)) if len(channels) == 1 else "panel", read_only=True, seats=seats,
               blind_isolated=bool(seats) and all(s["blind_isolated"] for s in seats),
               reason=("shared_swarm_board" if "swarm" in channels else "panel_seats" if seats else "no_enabled_seats"))
    return dict(out, skipped_seats=skipped) if skipped else out

def _degraded_route(read_only, loaded):
    return dict(mode="degraded", channel="direct", read_only=read_only, degraded=True,
                reason="policy_corrupted", warning=loaded["read_only_warning"])

def resolve_role(role, policy=None, capabilities=None, *, policy_path=None, state_path=None, count=None, read_only=False, blind=False):
    if role not in ROLE_READ_ONLY:
        raise ValueError("unknown dispatch role")
    effective_ro = bool(ROLE_READ_ONLY[role] or read_only)
    if policy is None:
        if (loaded := load_policy(policy_path))["degraded"]:
            return _degraded_route(effective_ro, loaded)
        policy = loaded["policy"]
    if count is not None and (type(count) is not int or not 1 <= count <= MAX_PANEL_COUNT):
        raise ValueError(f"panel count must be an integer from 1 to {MAX_PANEL_COUNT}")
    if not policy["enabled"]:
        return dict(mode="disabled", channel="direct", reason="dispatch_disabled")
    caps = probe_capabilities(state_path) if capabilities is None else capabilities
    cfg = policy["roles"][role]
    if role == "panel":
        return _resolve_panel(cfg, policy, caps, count, state_path, read_only=effective_ro, blind=blind)
    return _resolve_target(cfg["primary"], role, policy, caps, state_path, read_only=effective_ro)

def resolve_all(policy=None, capabilities=None, *, policy_path=None, state_path=None):
    if policy is None:
        if (loaded := load_policy(policy_path))["degraded"]:
            return {role: _degraded_route(read_only, loaded) for role, read_only in ROLE_READ_ONLY.items()}
        policy = loaded["policy"]
    caps = probe_capabilities(state_path) if capabilities is None else capabilities
    return {role: resolve_role(role, policy, caps, state_path=state_path) for role in ROLE_READ_ONLY}
