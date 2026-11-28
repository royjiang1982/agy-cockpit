"""Shared on-disk dispatch policy contract; standard library only."""
import json
import os
from pathlib import Path
import re

EFFORTS = ("", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "same")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/() -]{0,80}", re.ASCII)
NAMESPACED_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:-]*")
LABEL_RE = re.compile(r"[^()]*(?: \([A-Za-z0-9._ -]+\))?")
_DEFAULT_PATH = ".gemini/antigravity/sidecar_data/statusline/dispatch-policy.json"

AGY_DEFAULT_EFFORT = {
    "hydrogen": "high", "neon": "medium", "argon": "medium", "carbon": "medium",
    "flash": "high", "opus": "high", "sonnet": "high",
}
# "extra" is a legacy concrete model suffix, not a dispatch policy effort value.
_AGY_MODEL_EFFORTS = (*EFFORTS[1:-1], "extra")
_AGY_SUFFIXES = "|".join(_AGY_MODEL_EFFORTS)
AGY_GEMINI_RE = re.compile(rf"^gemini-(\d+(?:\.\d+)*)-(hydrogen|neon|argon|carbon|flash)(?:-({_AGY_SUFFIXES}))?$")
AGY_CLAUDE_RE = re.compile(rf"^claude-(opus|sonnet)-(\d+(?:[.-]\d+)*)(?:-({_AGY_SUFFIXES}))?$")


def agy_model_family(model):
    low = (model or "").strip().lower()
    if low in AGY_DEFAULT_EFFORT:
        return low
    gemini = AGY_GEMINI_RE.fullmatch(low)
    if gemini:
        return gemini[2]
    claude = AGY_CLAUDE_RE.fullmatch(low)
    return claude[1] if claude else None


def agy_model_effort(model):
    _, sep, suffix = (model or "").strip().lower().rpartition("-")
    return suffix if sep and suffix in _AGY_MODEL_EFFORTS else ""


def resolve_effective_effort(model, explicit_effort="", *, engine="agy"):
    """Resolve same-effort inheritance from the source, not the fallback target."""
    explicit = (explicit_effort or "").strip().lower()
    if explicit and explicit != "same":
        return explicit
    if engine != "agy":
        return ""
    return agy_model_effort(model) or AGY_DEFAULT_EFFORT.get(agy_model_family(model), "")


def valid_model(value):
    return (isinstance(value, str) and MODEL_RE.fullmatch(value) is not None
            and ".." not in value and value == value.strip() and not re.search(r"(?:^| )--?\S", value)
            and (NAMESPACED_MODEL_RE if "/" in value else LABEL_RE).fullmatch(value) is not None)


def unique_json_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def strict_json_loads(raw):
    return json.loads(raw, object_pairs_hook=unique_json_object, parse_constant=_reject_constant)


def resolve_policy_path(policy_path=None, *, state_path=None):
    if policy_path is None:
        policy_path = os.environ.get("SA_DISPATCH_POLICY_PATH") or (
            Path(state_path).with_name("dispatch-policy.json") if state_path else Path.home() / _DEFAULT_PATH
        )
    return Path(policy_path).expanduser().absolute()
