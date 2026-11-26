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

DATA_DIR = Path.home() / ".gemini/antigravity/sidecar_data/statusline"
STATE_PATH = DATA_DIR / "state.json"
SCHEMA_VERSION = 1
ROLE_READ_ONLY = {"scout": True, "standard": False, "deep": False, "expert": False, "panel": True}
ENGINES = ("agy", "codex", "droid", "grok", "pi")
CHANNELS = ("cli", "invoke_subagent", "swarm")
EFFORTS = ("", "low", "medium", "high", "xhigh", "same")
NATIVE_MODELS = {"flash": "research", "inherit": "self", "pro": "self"}
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/() -]{0,80}", re.ASCII)
_NAMESPACED_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:-]*")
_LABEL = re.compile(r"[^()]*(?: \([A-Za-z0-9._ -]+\))?")
MAX_QUOTA_AGE_SEC = 300
_LOCK = threading.Lock()


class PolicyConflictError(Exception):
    """The revision has changed or the on-disk policy cannot be safely edited."""


def _target(engine, model, effort="", channel="cli", **extras):
    return dict(channel=channel, engine=engine, model=model, effort=effort, **extras)


def default_policy():
    primaries = {
        "scout": _target("agy", "gemini-4-hydrogen-high", "high"),
        "standard": _target("agy", "gemini-4-neon-medium", "medium"),
        "deep": _target("codex", "gpt-6.2-sol", "high"),
        "expert": _target("codex", "gpt-6.2-astra", "xhigh"),
    }
    roles = {name: dict(read_only=ROLE_READ_ONLY[name], primary=primary)
             for name, primary in primaries.items()}
    roles["panel"] = dict(read_only=True, seats=[
        dict(primaries["expert"], enabled=True),
        _target("agy", "gemini-4-neon-xhigh", "xhigh", enabled=True),
        _target("agy", "claude-opus-5-5-high", "high", enabled=False),
        _target("grok", "grok-4.7", "xhigh", enabled=False),
    ])
    fallback_specs = {
        "agy:gemini-4-hydrogen-high": ("gemini-3.8-flash", "high"),
        "agy:gemini-4-neon-medium": ("gemini-4-argon", "medium"),
        "agy:gemini-4-neon-high": ("gemini-4-argon", "medium"),
        "codex:gpt-6.2-sol": ("gpt-6.2-sol", "same"),
        "codex:gpt-6.2-astra": ("gpt-6.2-astra", "xhigh"),
        "agy:claude-opus-5-5-high": ("claude-opus-5-5", "high"),
    }
    return dict(schema_version=SCHEMA_VERSION, revision=1, enabled=True, external_enabled=True,
                allow_swarm=False, codex_fast=True, quota_fallback=dict(enabled=True, threshold_pct=5),
                roles=roles, fallbacks={key: [_target("droid", *spec, enabled=True)]
                                        for key, spec in fallback_specs.items()})


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
    # Existing Cockpit installations persist state as view_state.json; use it
    # when a standalone state.json does not exist. Explicit paths never fall back.
    paths = (Path(state_path),) if state_path is not None else (STATE_PATH, STATE_PATH.with_name("view_state.json"))
    for path in paths:
        try:
            state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
            cache = state.get("quota_cache") if isinstance(state, dict) else None
            return cache if isinstance(cache, dict) else {}
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError):
            break
    return {}


def _number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def _quota_hint(row, now):
    observed = _number(row.get("observed_at"))
    reset = _number(row.get("reset_ts"))
    fraction = _number(row.get("rem_frac"))
    pct = _number(row.get("pct", None if fraction is None or not 0 <= fraction <= 1 else fraction * 100))
    if pct is not None and not 0 <= pct <= 100:
        pct = None
    fresh = (pct is not None and observed is not None and observed > 0
             and 0 <= now - observed <= MAX_QUOTA_AGE_SEC and not row.get("stale")
             and reset is not None and 0 < reset and now < reset)
    return dict(pct=pct, observed_at=observed, reset_ts=reset, fresh=fresh)


def _limiting_hint(rows):
    fresh = [row for row in rows if row["fresh"]]
    return (min(fresh, key=lambda row: (row["pct"], -row["observed_at"])) if fresh else
            max(rows, key=lambda row: row["observed_at"] or 0, default=_quota_hint({}, 0)))


def probe_capabilities(state_path=None, *, now=None):
    """PATH-only wrapper discovery plus persisted (never polled) quota samples."""
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
    return dict(available_engines=[engine for engine in ENGINES if shutil.which(f"sa-{engine}")],
                models={engine: sorted(names) for engine, names in models.items()},
                quota={engine: _limiting_hint(rows) for engine, rows in quota.items()},
                agy_quota={family: _limiting_hint(rows) for family, rows in agy_families.items()
                           if rows or any(agy_families.values())})


def _agy_quota_family(model):
    names = set(re.findall(r"[a-z0-9]+", model.lower()))
    if names.intersection(("claude", "opus", "sonnet", "haiku")):
        return "3p"
    if names.intersection(("gemini", "neon", "argon", "carbon", "hydrogen", "flash")):
        return "gemini"
    return None


def _selection(target, name, policy, caps):
    channel, engine, model = (target[key] for key in ("channel", "engine", "model"))
    if not target.get("enabled", True):
        return None, "candidate_disabled"
    if channel == "cli":
        if not policy["external_enabled"]:
            return None, "external_disabled"
        if engine not in caps.get("available_engines", ()):
            return None, "cli_unavailable"
        quota = caps.get("quota", {}).get(engine, {})
        if engine == "agy":
            quota = caps.get("agy_quota", {}).get(_agy_quota_family(model), {})
        cutoff = policy["quota_fallback"]
        if cutoff["enabled"] and quota.get("fresh") and quota["pct"] <= cutoff["threshold_pct"]:
            return None, "quota_low"
    elif channel == "swarm" and not policy["allow_swarm"]:
        return None, "swarm_disabled"
    selected = dict(target, mode=channel, read_only=ROLE_READ_ONLY[name])
    selected.pop("enabled", None)
    if channel == "cli" and engine == "codex":
        selected["codex_fast"] = policy["codex_fast"]
    if channel == "invoke_subagent":
        selected["type"] = "research" if ROLE_READ_ONLY[name] else "self"
        if NATIVE_MODELS[model] != selected["type"]:
            return None, "role_permission_mismatch"
    return selected, None


def _builtin_tails(name, policy):
    if name in ("deep", "expert") and policy["allow_swarm"]:
        tier = "Medium" if name == "deep" else "High"
        yield dict(mode="swarm", channel="swarm", engine="agy", model=f"Gemini 4 Neon ({tier})",
                   effort="", read_only=False, requires_model_check=True)
    model = {"scout": "flash", "standard": "inherit", "panel": "flash"}.get(name, "pro")
    yield dict(mode="invoke_subagent", channel="invoke_subagent", engine="agy",
               model=model, effort="", type=NATIVE_MODELS[model], read_only=ROLE_READ_ONLY[name])


def _resolve_target(primary, name, policy, caps):
    key = f"{primary['engine']}:{primary['model']}"
    available, skipped = [], []
    effort = "" if primary["effort"] == "same" else primary["effort"]
    for i, target in enumerate([primary, *policy["fallbacks"].get(key, [])]):
        candidate = dict(target, effort=effort) if target["effort"] == "same" else target
        result, why = _selection(candidate, name, policy, caps)
        if result is None:
            skipped.append(dict(channel=target["channel"], engine=target["engine"], model=target["model"], reason=why))
        elif not any(all(prior[key] == result[key] for key in ("channel", "engine", "model", "effort"))
                     for prior in available):
            available.append(dict(result, reason="primary" if i == 0 else "fallback"))
    for tail in _builtin_tails(name, policy):
        if not any(all(prior[key] == tail[key] for key in ("channel", "engine", "model", "effort"))
                   for prior in available):
            available.append(dict(tail, reason="builtin_fallback"))
    selected, *fallbacks = available
    return dict(selected, fallbacks=fallbacks, skipped=skipped)


def resolve_role(role, policy=None, capabilities=None, *, policy_path=None, state_path=None):
    if role not in ROLE_READ_ONLY:
        raise ValueError("unknown dispatch role")
    if policy is None:
        policy = load_policy(policy_path)["policy"]
    if not policy["enabled"]:
        return dict(mode="disabled", channel="direct", reason="dispatch_disabled")
    if capabilities is None:
        capabilities = probe_capabilities(state_path)
    config = policy["roles"][role]
    if role != "panel":
        return _resolve_target(config["primary"], role, policy, capabilities)
    seats = [_resolve_target(seat, role, policy, capabilities)
             for seat in config["seats"] if seat.get("enabled", True)]
    for seat in seats:
        seat["blind_isolated"] = seat["channel"] in ("cli", "invoke_subagent")
        seat["blind_reason"] = "independent_concurrent_call" if seat["blind_isolated"] else "shared_swarm_board"
    channels = {seat["channel"] for seat in seats}
    return dict(mode="panel", channel=next(iter(channels)) if len(channels) == 1 else "panel",
                read_only=True, seats=seats, blind_isolated=bool(seats) and all(s["blind_isolated"] for s in seats),
                reason=("shared_swarm_board" if "swarm" in channels else
                        "panel_seats" if seats else "no_enabled_seats"))


def resolve_all(policy=None, capabilities=None, *, policy_path=None, state_path=None):
    if policy is None:
        policy = load_policy(policy_path)["policy"]
    if capabilities is None:
        capabilities = probe_capabilities(state_path)
    return {role: resolve_role(role, policy, capabilities) for role in ROLE_READ_ONLY}
