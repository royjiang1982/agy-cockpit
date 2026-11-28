"""Read the local dispatch policy without depending on its producer."""
import stat
import threading
from typing import NamedTuple, Optional, Tuple

from .policy_contract import (
    EFFORTS, agy_model_family, resolve_effective_effort, resolve_policy_path, strict_json_loads, valid_model,
)

_LOCK = threading.Lock()
_CACHE = None


def _read_policy():
    global _CACHE
    with _LOCK:
        try:
            path = resolve_policy_path()
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                _CACHE = None
                return {}
            key = (path, info.st_mtime_ns, info.st_size)
            if _CACHE is not None and _CACHE[0] == key:
                return _CACHE[1]
            raw = path.read_bytes()
        except (OSError, RuntimeError, ValueError):
            _CACHE = None
            return {}
        try:
            doc = strict_json_loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            doc = {}
        version = doc.get("schema_version") if isinstance(doc, dict) else None
        if type(version) is not int or version != 1 or ("enabled" in doc and doc["enabled"] is not True):
            doc = {}
        _CACHE = (key, doc)
        return doc


def read_codex_fast() -> Optional[bool]:
    """Return the specified preference, or None to preserve the CLI default."""
    value = _read_policy().get("codex_fast")
    return value if isinstance(value, bool) else None


class AgyPolicy(NamedTuple):
    enabled: Optional[bool]
    threshold_frac: Optional[float]
    fallback: Optional[Tuple[str, str]]
    has_fallback_override: bool = False


_DROID_FAMILY_DEFAULTS = {
    "hydrogen": "gemini-4-hydrogen", "neon": "gemini-4-neon", "carbon": "gemini-4-carbon",
    "flash": "gemini-3.8-flash", "argon": "gemini-4-argon",
    "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
    "sol": "gpt-6.2-sol", "astra": "gpt-6.2-astra", "terra": "gpt-6.1-terra", "luna": "gpt-6-luna",
    "grok": "grok-4.7", "grok-fast": "grok-4.7-build-fast",
}


def default_agy_fallback(model: str, effort: str = "") -> Tuple[str, str]:
    """Use Cockpit's same-family, same-effort Droid default without importing the sidecar."""
    family = agy_model_family(model)
    target = _DROID_FAMILY_DEFAULTS[family] if family in ("hydrogen", "neon", "carbon", "opus") else ""
    return target, resolve_effective_effort(model, effort)


def _droid_fallback(policy, model):
    fallbacks = policy.get("fallbacks")
    if not isinstance(fallbacks, dict) or not valid_model(model):
        return False, None
    chain = fallbacks.get(f"agy:{model}")
    if not isinstance(chain, list):
        fam = agy_model_family(model)
        chain = fallbacks.get(f"agy:{fam}") if fam else None
    if not isinstance(chain, list):
        return False, None
    for entry in chain:
        if not isinstance(entry, dict) or entry.get("enabled", True) is not True:
            continue
        if entry.get("channel") != "cli" or entry.get("engine") != "droid":
            continue
        target, effort = entry.get("model"), entry.get("effort")
        if isinstance(target, str):
            target = _DROID_FAMILY_DEFAULTS.get(target.lower(), target)
        if valid_model(target) and isinstance(effort, str) and effort in EFFORTS:
            return True, (target, effort)
    return True, None


def read_agy_policy(model: str) -> AgyPolicy:
    """Read settings and distinguish an absent chain from one with no usable Droid target."""
    policy = _read_policy()
    quota = policy.get("quota_fallback")
    enabled, pct = (quota.get("enabled"), quota.get("threshold_pct")) if isinstance(quota, dict) else (None, None)
    has_override, target = _droid_fallback(policy, model)
    return AgyPolicy(
        enabled if isinstance(enabled, bool) else None,
        pct / 100.0 if type(pct) is int and 0 <= pct <= 50 else None,
        target,
        has_override,
    )
