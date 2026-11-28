"""Official Antigravity CLI quota preflight; failures leave agy unchanged."""
from datetime import datetime
import json
import math
import os
import re
import subprocess
import threading
import time

from . import quota_guard
from .dispatch_policy_reader import default_agy_fallback, read_agy_policy
from .history_merge import build_forced_handoff_prompt
from .policy_contract import resolve_effective_effort
from .proc_utils import find_cli_binary, terminate_proc_group
from .session_handoff import _unwrap_handoff_prompt

_LOCK = threading.RLock()
_CACHE_PID = os.getpid()
_CACHE = None
_EPOCH = 0


def _parse_usage(raw):
    """Validate the entire sample before exposing any quota, including explicit zero."""
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("status", "SUCCESS") != "SUCCESS":
        raise ValueError("usage command unsuccessful")
    groups = doc["command"]["data"]["groups"]
    if not isinstance(groups, list):
        raise ValueError("invalid usage groups")
    buckets = []
    for group in groups:
        name = group.get("name", "")
        desc = group.get("description", "")
        if not isinstance(name, str) or not isinstance(desc, str) or not isinstance(group.get("buckets"), list):
            raise ValueError("invalid usage group")
        for entry in group["buckets"]:
            bid, rem = entry["id"], entry["remaining_fraction"]
            if not isinstance(bid, str) or not bid or isinstance(rem, bool) or not isinstance(rem, (int, float)):
                raise ValueError("invalid quota bucket")
            rem = float(rem)
            if not math.isfinite(rem) or not 0 <= rem <= 1:
                raise ValueError("invalid quota fraction")
            reset = entry.get("reset_time", "")
            if not isinstance(reset, str):
                raise ValueError("invalid reset time")
            ts = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp() if reset else 0.0
            buckets.append(dict(bid=bid, rem_frac=rem, reset_ts=ts, name=name, desc=desc))
    return buckets


def _fetch_snapshot():
    binary = find_cli_binary("agy")
    if not binary:
        raise FileNotFoundError("agy CLI unavailable")
    quota_guard.remaining()
    proc = subprocess.Popen([binary, "-p", "/usage", "--output-format", "json"],
                            start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        while True:
            try:
                raw, _ = proc.communicate(timeout=min(.05, quota_guard.remaining()))
                break
            except subprocess.TimeoutExpired:
                continue
        quota_guard.remaining()
        if proc.returncode != 0:
            raise ValueError("usage CLI failed")
        return _parse_usage(raw)
    finally:
        # Reap quickly on abort/timeout instead of burning the full 0.8s SIGTERM grace period.
        terminate_proc_group(proc, wait_timeout=0.05)
        if proc.stdout is not None:
            proc.stdout.close()


def invalidate_agy_quota_cache():
    """Runtime quota failures must not reuse a recently healthy snapshot."""
    global _CACHE, _EPOCH
    quota_guard.invalidate()
    with _LOCK:
        _CACHE = None
        _EPOCH += 1


def _name(value):
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return re.sub(r"^(?:claude|gemini)-", "", value)


def _base(value):
    return re.sub(r"-(?:low|medium|med|high|xhigh|extra|max|ultra)$", "", _name(value))


def _desc_items(desc):
    body = re.sub(r"^\s*models\s+within\s+this\s+group\s*:\s*", "", desc or "", flags=re.I)
    return [_name(part) for part in body.split(",") if part.strip()]


def _resolve(model, buckets):
    wanted = _name(model)
    base = _base(model)
    vendor = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    family = ("3p" if re.match(r"(?:claude|opus|sonnet|gpt)(?:-|$)", vendor) else
              "gemini" if re.match(r"(?:gemini|carbon|argon|hydrogen|neon|flash)(?:-|$)", vendor) else "")
    values, globals_ = [], []
    now = time.time()
    for bucket in buckets:
        # A fresh cache entry can still contain a bucket from a finished quota window.
        if 0 < bucket["reset_ts"] <= now:
            continue
        bid = bucket["bid"].lower()
        if bid in ("global", "shared", "all") or bid.startswith(("global-", "all-")):
            globals_.append(bucket)
            continue
        group = re.match(r"^(?:shared-)?(3p|gemini)(?:-|$)", bid)
        if group and group[1] != family:
            continue
        described = any(
            item == wanted or (item == base and item == _base(item))
            for item in _desc_items(bucket["desc"])
        ) if wanted or base else False
        if described or (group and family and bid.startswith("shared-")) or (base and _base(bid) == base):
            values.append(bucket)
    return min(values + globals_, key=lambda q: q["rem_frac"]) if values else None


def _quota_for_model(model):
    global _CACHE, _CACHE_PID
    with _LOCK:
        if _CACHE_PID != os.getpid():
            _CACHE, _CACHE_PID = None, os.getpid()
        cached, epoch = _CACHE, _EPOCH
    if cached is not None:
        quota = _resolve(model, cached[1])
        healthy = quota is not None and quota["rem_frac"] > .20 and not math.isclose(quota["rem_frac"], .20, abs_tol=1e-8)
        ttl = quota_guard.number("SA_AGY_QUOTA_TTL_SEC", 300.0) if healthy else quota_guard.number("SA_AGY_QUOTA_LOW_TTL_SEC", 60.0)
        if time.monotonic() - cached[0] < ttl:
            return quota
    buckets = quota_guard.fetch(_fetch_snapshot)
    if buckets is None:
        return None
    with _LOCK:
        if _EPOCH == epoch:
            _CACHE = (time.monotonic(), buckets)
    return _resolve(model, buckets)


def _fallback(model, policy_target=None, effort=""):
    override = os.environ.get("SA_AGY_FALLBACK_MODEL")
    if override is None and policy_target is not None:
        target_model, target_effort = policy_target
        if target_effort == "same":
            target_effort = resolve_effective_effort(model, effort)
    else:
        target_model, target_effort = default_agy_fallback(model, effort=effort)
    return ((override or "").strip() or target_model,
            os.environ.get("SA_AGY_FALLBACK_EFFORT", "").strip() or target_effort)


def preflight_agy_request(agent="agy", model="", effort="", mode="", requested_sid="", cwd="",
                          prompt="", state_dir=None):
    """Return effective request fields plus an optional fallback event and handoff."""
    result = dict(agent=agent, model=model, effort=effort, mode=mode, requested_sid=requested_sid,
                  prompt=prompt, should_fallback=False, event=None, handoff=None)
    preflight = os.environ.get("SA_AGY_QUOTA_PREFLIGHT")
    if agent != "agy" or preflight == "0":
        return result
    policy = read_agy_policy(model)
    if preflight is None and policy.enabled is False:
        return result
    if policy.has_fallback_override and policy.fallback is None:
        return result
    target, target_effort = _fallback(model, policy.fallback, effort=effort)
    if not target:
        return result
    try:
        default_threshold = 0.05 if policy.threshold_frac is None else policy.threshold_frac
        threshold = float(os.environ.get("SA_AGY_MIN_QUOTA_FRAC", default_threshold))
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            return result
        quota = _quota_for_model(model)
        rem = (quota or {}).get("rem_frac")
        if rem is None or (rem > threshold and not math.isclose(rem, threshold, rel_tol=0, abs_tol=1e-8)):
            return result
        if not find_cli_binary("droid"):
            return result
    except Exception:
        return result  # CLI/parsing failures must not prevent normal agy execution.
    if requested_sid:
        carried = build_forced_handoff_prompt("agy", requested_sid, cwd, prompt, state_dir=state_dir)
        if carried is None:
            raise RuntimeError(f"agy model {model} quota <= {threshold:g}; prior session context could not be loaded for cross-engine handoff")
        turns, _ = _unwrap_handoff_prompt(carried)
        result["handoff"] = dict(from_session_id=requested_sid, carried_turns=turns, effective_prompt=carried)
        result["prompt"] = carried
    event = dict(type="quota_fallback", event="quota_fallback", from_agent="agy", from_model=model, to_agent="droid", to_model=target,
                 to_effort=target_effort, rem_frac=rem, reset_ts=quota["reset_ts"], threshold=threshold,
                 handoff_from_session=requested_sid)
    result.update(agent="droid", model=target, effort=target_effort, requested_sid="", should_fallback=True,
                  mode="spec" if mode == "plan" else "auto-high", event=event)
    return result
