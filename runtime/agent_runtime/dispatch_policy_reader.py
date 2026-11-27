"""Read the local dispatch policy without depending on its producer."""
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import NamedTuple, Optional, Tuple

_DEFAULT_PATH = ".gemini/antigravity/sidecar_data/statusline/dispatch-policy.json"
# Keep this grammar in sync with the Cockpit policy writer, without importing it.
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/() -]{0,80}", re.ASCII)
_NAMESPACED_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:-]*")
_LABEL = re.compile(r"[^()]*(?: \([A-Za-z0-9._ -]+\))?")
_EFFORTS = frozenset(("", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "same"))
_LOCK = threading.Lock()
_CACHE = None


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _read_policy():
    global _CACHE
    with _LOCK:
        try:
            override = os.environ.get("SA_DISPATCH_POLICY_PATH")
            path = Path(override).expanduser().absolute() if override else Path.home() / _DEFAULT_PATH
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
            doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
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


def _valid_model(value):
    return (isinstance(value, str) and _MODEL.fullmatch(value) is not None
            and ".." not in value and value == value.strip() and not re.search(r"(?:^| )--?\S", value)
            and (_NAMESPACED_MODEL if "/" in value else _LABEL).fullmatch(value) is not None)


_AGY_FAMILY_RE = re.compile(r"^(?:gemini-[\d.]+-(hydrogen|neon|argon|carbon|flash)|claude-(opus|sonnet)-[\d.-]+)(?:-[a-z]+)?$")
_DROID_FAMILY_DEFAULTS = {
    "hydrogen": "gemini-4-hydrogen", "neon": "gemini-4-neon", "carbon": "gemini-4-carbon",
    "flash": "gemini-3.8-flash", "argon": "gemini-4-argon",
    "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
    "sol": "gpt-6.2-sol", "astra": "gpt-6.2-astra", "terra": "gpt-6.1-terra", "luna": "gpt-6-luna",
    "grok": "grok-4.7", "grok-fast": "grok-4.7-build-fast",
}


def _agy_family(model: str) -> Optional[str]:
    low = model.lower()
    if low in ("hydrogen", "neon", "argon", "carbon", "flash", "opus", "sonnet"):
        return low
    hit = _AGY_FAMILY_RE.fullmatch(low)
    return (hit.group(1) or hit.group(2)) if hit else None


def _droid_fallback(policy, model):
    fallbacks = policy.get("fallbacks")
    if not isinstance(fallbacks, dict) or not _valid_model(model):
        return False, None
    chain = fallbacks.get(f"agy:{model}")
    if not isinstance(chain, list):
        fam = _agy_family(model)
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
        if _valid_model(target) and isinstance(effort, str) and effort in _EFFORTS:
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
