"""Model metadata, canonical labeling, quota family classification, and API pricing."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from swarm_subagents import models_board_subagents

CTX_WINDOW_MAX = 300_000
PRICING_EPOCH = 6
_CARBON_STANDARD_TS = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()
_FLASH38_STANDARD_TS = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()


def needs_reprice(cached: tuple[float, list] | None, mtime: float, since_ts: float | None) -> bool:
    """Return True when a cached file entry touches the active reprice window."""
    if since_ts is None or cached is None:
        return False
    return mtime >= since_ts or any(float(it[0]) >= since_ts for it in cached[1])

MODEL_MAP: dict[int, tuple[str, str, int]] = {
    1427: ("Gemini 4 Carbon (High)", "Carbon High", CTX_WINDOW_MAX),
    1437: ("Gemini 4 Carbon (Medium)", "Carbon Med", CTX_WINDOW_MAX),
    1438: ("Gemini 4 Carbon (Low)", "Carbon Low", CTX_WINDOW_MAX),
    1667: ("Orchestrator (Carbon)", "Orchestrator (Carbon)", CTX_WINDOW_MAX),
    326: ("Gemini 4 Argon", "Argon", CTX_WINDOW_MAX),
    1417: ("Gemini 4 Argon (High)", "Argon High", CTX_WINDOW_MAX),
    1418: ("Gemini 4 Argon (Medium)", "Argon Med", CTX_WINDOW_MAX),
    1419: ("Gemini 4 Argon (Low)", "Argon Low", CTX_WINDOW_MAX),
    1681: ("Orchestrator (Argon)", "Orchestrator (Argon)", CTX_WINDOW_MAX),
    1525: ("Gemini 4 Hydrogen (High)", "Hydrogen High", CTX_WINDOW_MAX),
    1526: ("Gemini 4 Hydrogen (Medium)", "Hydrogen Med", CTX_WINDOW_MAX),
    1527: ("Gemini 4 Hydrogen (Low)", "Hydrogen Low", CTX_WINDOW_MAX),
    1528: ("Gemini 4 Hydrogen (XHigh)", "Hydrogen XHigh", CTX_WINDOW_MAX),
    1530: ("Gemini 4 Hydrogen (High)", "Hydrogen High", CTX_WINDOW_MAX),
    1531: ("Gemini 4 Hydrogen (XHigh)", "Hydrogen XHigh", CTX_WINDOW_MAX),
    1532: ("Gemini 4 Neon (High)", "Neon High", CTX_WINDOW_MAX),
    1533: ("Gemini 4 Neon (Medium)", "Neon Med", CTX_WINDOW_MAX),
    1534: ("Gemini 4 Neon (Low)", "Neon Low", CTX_WINDOW_MAX),
    1535: ("Gemini 4 Neon (XHigh)", "Neon XHigh", CTX_WINDOW_MAX),
    1537: ("Orchestrator (Neon)", "Orchestrator (Neon)", CTX_WINDOW_MAX),
    1318: ("Gemini 3.8 Flash (High)", "Flash High", CTX_WINDOW_MAX),
    1319: ("Gemini 3.8 Flash (Medium)", "Flash Med", CTX_WINDOW_MAX),
    1320: ("Gemini 3.8 Flash (Low)", "Flash Low", CTX_WINDOW_MAX),
    1322: ("Gemini 3.8 Flash", "Flash", CTX_WINDOW_MAX),
    1298: ("Gemini 3.7 Flash (High)", "3.7 Flash High", CTX_WINDOW_MAX),
    1299: ("Gemini 3.7 Flash (Medium)", "3.7 Flash Med", CTX_WINDOW_MAX),
    1300: ("Gemini 3.7 Flash (Low)", "3.7 Flash Low", CTX_WINDOW_MAX),
    1071: ("Gemini 3.6 Flash (High)", "3.6 Flash High", CTX_WINDOW_MAX),
    1072: ("Gemini 3.6 Flash (Medium)", "3.6 Flash Med", CTX_WINDOW_MAX),
    1073: ("Gemini 3.6 Flash (Low)", "3.6 Flash Low", CTX_WINDOW_MAX),
    1036: ("Gemini 3.1 Pro (Low)", "3.1 Pro Low", CTX_WINDOW_MAX),
    1037: ("Gemini 3.1 Pro (High)", "3.1 Pro High", CTX_WINDOW_MAX),
    1050: ("Gemini 3.1 Flash Lite", "3.1 Flash Lite", CTX_WINDOW_MAX),
    1198: ("Gemini 3.5 Flash Lite", "3.5 Flash Lite", CTX_WINDOW_MAX),
    1368: ("Claude Opus 5.5", "Opus 5.5", CTX_WINDOW_MAX),
    1400: ("Claude Opus 5.5 (Low)", "Opus 5.5 Low", CTX_WINDOW_MAX),
    1401: ("Claude Opus 5.5 (Medium)", "Opus 5.5 Med", CTX_WINDOW_MAX),
    1402: ("Claude Opus 5.5 (High)", "Opus 5.5 High", CTX_WINDOW_MAX),
    1406: ("Claude Opus 5.5 (Max)", "Opus 5.5 Max", CTX_WINDOW_MAX),
    1403: ("Claude Sonnet 5.5 (Low)", "Sonnet 5.5 Low", CTX_WINDOW_MAX),
    1404: ("Claude Sonnet 5.5 (Medium)", "Sonnet 5.5 Med", CTX_WINDOW_MAX),
    1405: ("Claude Sonnet 5.5 (High)", "Sonnet 5.5 High", CTX_WINDOW_MAX),
    1407: ("Claude Sonnet 5.5 (Max)", "Sonnet 5.5 Max", CTX_WINDOW_MAX),
    342: ("GPT-OSS 120B (Medium)", "GPT-OSS 120B", CTX_WINDOW_MAX),
    1250: ("GLM 5.2 (FP8)", "GLM 5.2", CTX_WINDOW_MAX),
}

SHORT_MODEL_HINTS: dict[str, str] = {
    "gemini-4-carbon-high": "Carbon High",
    "gemini-4-carbon-medium": "Carbon Med",
    "gemini-4-carbon-low": "Carbon Low",
    "gemini-4-carbon": "Carbon",
    "gemini-4-argon-high": "Argon High",
    "gemini-4-argon-medium": "Argon Med",
    "gemini-4-argon-low": "Argon Low",
    "gemini-4-argon": "Argon",
    "gemini-4-hydrogen-xhigh": "Hydrogen XHigh",
    "gemini-4-hydrogen-high": "Hydrogen High",
    "gemini-4-hydrogen-tiered-high": "Hydrogen High",
    "gemini-4-hydrogen-tiered-xhigh": "Hydrogen XHigh",
    "gemini-4-hydrogen-medium": "Hydrogen Med",
    "gemini-4-hydrogen-low": "Hydrogen Low",
    "gemini-4-hydrogen": "Hydrogen",
    "gemini-4-neon-xhigh": "Neon XHigh",
    "gemini-4-neon-high": "Neon High",
    "gemini-4-neon-medium": "Neon Med",
    "gemini-4-neon-low": "Neon Low",
    "gemini-4-neon": "Neon",
    "gemini-pro-orchestrator": "Orchestrator (Carbon)",
    "gemini-pro-orchestrator-argon": "Orchestrator (Argon)",
    "gemini-pro-orchestrator-neon": "Orchestrator (Neon)",
    "gemini-3.8-flash-high": "Flash High",
    "gemini-3.8-flash-medium": "Flash Med",
    "gemini-3.8-flash-low": "Flash Low",
    "gemini-3.8-flash": "Flash",
    "gemini-3.5-flash-lite": "3.5 Flash Lite",
    "claude-opus-5-5-max": "Opus 5.5 Max",
    "claude-opus-5-5-high": "Opus 5.5 High",
    "claude-opus-5-5-medium": "Opus 5.5 Med",
    "claude-opus-5-5-low": "Opus 5.5 Low",
    "claude-sonnet-5-5-max": "Sonnet 5.5 Max",
    "claude-sonnet-5-5-high": "Sonnet 5.5 High",
    "claude-sonnet-5-5-medium": "Sonnet 5.5 Med",
    "claude-sonnet-5-5-low": "Sonnet 5.5 Low",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "anthropic/claude-opus-5-5:xhigh": "Opus 5.5 (xhigh)",
    "anthropic/claude-opus-5-5:high": "Opus 5.5 (high)",
    "claude-opus-5-5": "Opus 5.5",
    "gpt-oss-120b-medium": "GPT-OSS 120B",
    "gpt-6-luna": "gpt-6-luna",
    "kimi-k3": "kimi-k3",
    "grok-4.7": "grok-4.7",
    "grok-4.7-build": "grok-4.7",
    "grok-4.7-build-fast": "grok-4.7-fast",
    "grok-4.6": "grok-4.6",
    "grok-4.6-build": "grok-4.6",
    "grok-4.5": "grok-4.5",
    "grok-4.5-build": "grok-4.5",
}

_STATIC_MODEL_ENUMS = frozenset(MODEL_MAP)
_STATIC_SHORT_HINTS = frozenset(SHORT_MODEL_HINTS)
_DYNAMIC_MODEL_LOCK = threading.RLock()
_DYNAMIC_MODELS: dict[int, tuple[str, str]] = {}
_OBSERVED_MODEL_CTX_LIMITS: dict[int, tuple[int, float]] = {}
_MODEL_MAP_EPOCH = 0


def derive_short_label(label: str) -> str:
    """Derive a concise display name from a server `ClientModelConfig.label`."""
    short = (label or "").strip()
    for prefix in ("Gemini 4 ", "Gemini ", "Claude "):
        if short.startswith(prefix) and len(short) > len(prefix):
            short = short[len(prefix):].strip()
            break
    for raw_sfx, compact_sfx in (
        (" (XHigh)", " XHigh"),
        (" (Medium)", " Med"),
        (" (High)", " High"),
        (" (Low)", " Low"),
        (" (Max)", " Max"),
    ):
        if short.endswith(raw_sfx):
            short = short[: -len(raw_sfx)].strip() + compact_sfx
            break
    return short or label


def register_dynamic_models(entries: list[tuple[int, str, str]]) -> bool:
    """Register `(enum_id, label, model_id)` tuples discovered at runtime without overriding curated static entries."""
    global _MODEL_MAP_EPOCH
    changed = False
    with _DYNAMIC_MODEL_LOCK:
        for enum_id, label, model_id in entries:
            clean_label = (label or "").strip()
            if not clean_label or int(enum_id or 0) <= 0:
                continue
            eid = int(enum_id)
            short = derive_short_label(clean_label)
            clean_mid = (model_id or "").strip().lower()
            if eid not in _STATIC_MODEL_ENUMS:
                new_val = (clean_label, short, CTX_WINDOW_MAX)
                if MODEL_MAP.get(eid) != new_val:
                    MODEL_MAP[eid] = new_val
                    changed = True
                _DYNAMIC_MODELS[eid] = (clean_label, clean_mid)
            if clean_mid and clean_mid not in _STATIC_SHORT_HINTS:
                target_short = MODEL_MAP[eid][1] if eid in MODEL_MAP else short
                if SHORT_MODEL_HINTS.get(clean_mid) != target_short:
                    SHORT_MODEL_HINTS[clean_mid] = target_short
                    changed = True
        if changed:
            _MODEL_MAP_EPOCH += 1
    return changed


def get_model_map_epoch() -> int:
    return _MODEL_MAP_EPOCH


def get_dynamic_models_snapshot() -> dict[int, tuple[str, str]]:
    with _DYNAMIC_MODEL_LOCK:
        return dict(_DYNAMIC_MODELS)


def export_dynamic_models() -> dict[str, list[str]]:
    with _DYNAMIC_MODEL_LOCK:
        return {str(eid): [lbl, mid] for eid, (lbl, mid) in _DYNAMIC_MODELS.items()}


def import_dynamic_models(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    entries: list[tuple[int, str, str]] = []
    for k, v in raw.items():
        try:
            eid = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, list) and len(v) >= 1 and isinstance(v[0], str):
            mid = str(v[1]) if len(v) >= 2 and isinstance(v[1], str) else ""
            entries.append((eid, v[0], mid))
    return register_dynamic_models(entries) if entries else False


def record_model_ctx_limit(enum_id: int | None, max_ctx: int | None, observed_ts: float = 0.0) -> bool:
    """Remember the newest observed `max_context_tokens` for a model enum."""
    try:
        eid = int(enum_id or 0)
        lim = int(max_ctx or 0)
        ts = max(0.0, float(observed_ts or 0.0))
    except (TypeError, ValueError):
        return False
    if eid <= 0 or lim <= 0:
        return False
    with _DYNAMIC_MODEL_LOCK:
        prev = _OBSERVED_MODEL_CTX_LIMITS.get(eid)
        if prev is not None:
            prev_lim, prev_ts = prev
            if ts > 0.0 and prev_ts > 0.0 and ts < prev_ts:
                return False
            if ts <= 0.0 and prev_ts > 0.0:
                return False
            if prev_lim == lim and ts <= prev_ts:
                return False
            _OBSERVED_MODEL_CTX_LIMITS[eid] = (lim, max(ts, prev_ts))
            return True
        _OBSERVED_MODEL_CTX_LIMITS[eid] = (lim, ts)
        return True


def get_observed_model_ctx_limit(enum_id: int | None) -> int | None:
    try:
        eid = int(enum_id or 0)
    except (TypeError, ValueError):
        return None
    if eid <= 0:
        return None
    with _DYNAMIC_MODEL_LOCK:
        entry = _OBSERVED_MODEL_CTX_LIMITS.get(eid)
        return entry[0] if entry is not None else None


def export_model_ctx_limits() -> dict[str, list[float]]:
    with _DYNAMIC_MODEL_LOCK:
        return {str(eid): [lim, ts] for eid, (lim, ts) in _OBSERVED_MODEL_CTX_LIMITS.items()}


def import_model_ctx_limits(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    changed = False
    for k, v in raw.items():
        try:
            eid = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, list) and len(v) >= 1:
            lim = v[0]
            ts = v[1] if len(v) >= 2 else 0.0
        else:
            lim = v
            ts = 0.0
        if record_model_ctx_limit(eid, lim, ts):
            changed = True
    return changed


def short_model_hint(raw_model: str) -> str:
    return SHORT_MODEL_HINTS.get((raw_model or "").lower(), raw_model)


def canonical_model_label(raw_model: str) -> str:
    """Normalize a model display name into a clean canonical label for the Models Board (ignoring thinking effort tiers)."""
    s = (raw_model or "").strip()
    low = s.lower()
    if "carbon" in low or low == "gemini-pro-orchestrator":
        return "Carbon"
    if "argon" in low:
        return "Argon"
    if "hydrogen" in low:
        return "Hydrogen"
    if "neon" in low:
        return "Neon"
    if ("3.8" in low and "flash" in low) or low in ("flash", "flash high", "flash med", "flash low"):
        return "Flash"
    if "3.7" in low and "flash" in low:
        return "3.7 Flash"
    if "3.6" in low and "flash" in low:
        return "3.6 Flash"
    if "flash lite" in low or "flash-lite" in low:
        return "3.5 Flash Lite" if "3.5" in low else "3.1 Flash Lite"
    if "3.1 pro" in low or "3.1-pro" in low:
        return "3.1 Pro"
    if "opus" in low and ("5.5" in low or "5-5" in low or low == "opus"):
        return "Opus 5.5"
    if "sonnet" in low and ("5.5" in low or "5-5" in low or low == "sonnet"):
        return "Sonnet 5.5"
    if "gpt-oss" in low:
        return "GPT-OSS 120B"
    if "gpt-6.2-sol" in low:
        return "gpt-6.2-sol"
    if "gpt-6.1-sol" in low:
        return "gpt-6.1-sol"
    if "gpt-6-sol" in low:
        return "gpt-6-sol"
    if "gpt-6-astra" in low:
        return "gpt-6-astra"
    if "gpt-6-luna" in low:
        return "gpt-6-luna"
    if "grok-4.7" in low and "fast" in low:
        return "grok-4.7-fast"
    if "grok-4.7" in low:
        return "grok-4.7"
    if "grok-4.6" in low:
        return "grok-4.6"
    if "grok-4.5" in low:
        return "grok-4.5"
    if "glm-5.2" in low or "glm 5.2" in low:
        return "GLM 5.2"
    base = short_model_hint(s) or "Unknown"
    for sfx in (
        " (xhigh)",
        " (high)",
        " (medium)",
        " (med)",
        " (low)",
        " (max)",
        " (XHigh)",
        " (High)",
        " (Medium)",
        " (Med)",
        " (Low)",
        " (Max)",
        " XHigh",
        " High",
        " Med",
        " Low",
        " Max",
    ):
        if base.endswith(sfx):
            base = base[: -len(sfx)].strip()
            break
    return base


def model_quota_family(raw_model: str) -> str:
    """Classify a model name into quota bar family: 'gemini', 'claude', 'grok', or 'other'."""
    low = (raw_model or "").lower()
    if any(k in low for k in ("claude", "opus", "sonnet", "gpt-oss")):
        return "claude"
    if any(k in low for k in ("gemini", "carbon", "argon", "hydrogen", "neon", "flash", "3.1 pro", "3.1-pro", "orchestrator")):
        return "gemini"
    if "grok" in low:
        return "grok"
    return "other"


def _model_rates(raw_model: str, prompt: int, event_ts: float | None = None) -> tuple[float, float, float, float]:
    """Return (in_rate, cache_write_rate, cache_read_rate, out_rate) per 1M tokens."""
    low = (raw_model or "").lower()
    eff_ts = float(event_ts) if (event_ts is not None and event_ts > 0) else time.time()
    if "opus" in low:
        return (5.0, 6.25, 0.50, 25.0)
    if "sonnet" in low:
        return (3.0, 3.75, 0.30, 15.0)
    if "astra" in low:
        if prompt > 272_000:
            return (20.0, 25.0, 2.0, 75.0)
        return (10.0, 12.5, 1.0, 50.0)
    # 6.2 Sol: local estimate using the existing Sol tiers, not an invoice.
    if "gpt-6.2-sol" in low or "gpt-6.1-sol" in low or "gpt-6-sol" in low or "codex" in low:
        c_rate = 0.10 if "6.2" in low or "6.1" in low or "codex" in low else 0.20
        if prompt > 272_000:
            return (4.0, 5.0, c_rate * 2.0, 15.0)
        return (2.0, 2.5, c_rate, 10.0)
    if "luna" in low:
        if prompt > 272_000:
            return (0.20, 0.25, 0.02, 0.75)
        return (0.10, 0.125, 0.01, 0.50)
    if "kimi" in low:
        return (3.0, 3.0, 0.30, 15.0)
    if "grok" in low:
        fast = "fast" in low
        cached_rate = 0.30 if "4.5" in low else 0.50
        if prompt > 200_000:
            mult = 1.5 if fast else 1.0
            return (4.0 * mult, 4.0 * mult, cached_rate * 2 * mult, 12.0 * mult)
        mult = 2.0 if fast else 1.0
        return (2.0 * mult, 2.0 * mult, cached_rate * mult, 6.0 * mult)
    # Gemini 4 Neon: standard $3/$15 (cache read $0.15, cache write $3.75).
    if "neon" in low:
        return (3.0, 3.75, 0.15, 15.0)
    # Gemini 4 Carbon & Argon: introductory $2/$10 (cache read $0.10) through 2026-11-30 UTC; standard $4/$20 ($0.20) from 2026-12-01 UTC.
    if "carbon" in low or "argon" in low or "gemini-pro-orchestrator" in low:
        if eff_ts >= _CARBON_STANDARD_TS:
            return (4.0, 4.0, 0.20, 20.0)
        return (2.0, 2.0, 0.10, 10.0)
    if "hydrogen" in low:
        return (1.50, 1.50, 0.075, 7.50)
    if "gpt-oss" in low:
        return (0.15, 0.15, 0.075, 0.60)
    if "flash lite" in low or "flash-lite" in low:
        return (0.25, 0.25, 0.025, 1.50)
    # Gemini 3.8 Flash: introductory $0.75/$3.75 ($0.075) through 2026-12-31 UTC; standard $1.50/$7.50 ($0.15) from 2027-01-01 UTC.
    if "flash" in low and ("gemini" in low or low.startswith(("flash", "3."))):
        is_flash_38 = "3.8" in low or not any(ver in low for ver in ("3.7", "3.6", "3.1", "2.5", "2.0"))
        if is_flash_38 and eff_ts >= _FLASH38_STANDARD_TS:
            return (1.50, 1.50, 0.15, 7.50)
        return (0.75, 0.75, 0.075, 3.75)
    if "3.1 pro" in low or "3.1-pro" in low:
        if prompt > 200_000:
            return (4.0, 4.0, 0.40, 18.0)
        return (2.0, 2.0, 0.20, 12.0)
    if "glm" in low:
        return (1.40, 1.40, 0.26, 4.40)
    return _UNMATCHED


class _FallbackRates(tuple):
    pass


_UNMATCHED = _FallbackRates((2.0, 2.0, 0.10, 10.0))


def estimate_source(model):
    return "unmatched_estimate" if _model_rates(model, 0) is _UNMATCHED else "estimated"


def calc_call_cost(
    raw_model: str,
    inp: int,
    cache_read: int,
    out: int,
    cache_write: int = 0,
    calls: int = 1,
    event_ts: float | None = None,
) -> float:
    """Calculate API cost in USD for a single call/turn (or cumulative session when calls > 1)."""
    # A cumulative aggregate cannot recover the per-request tier distribution.
    # Use base rates for an explicitly estimated aggregate, never an average tier.
    prompt = inp + cache_read + cache_write if calls <= 1 else 0
    in_r, cw_r, cr_r, out_r = _model_rates(raw_model, prompt, event_ts=event_ts)
    return (inp * in_r + cache_write * cw_r + cache_read * cr_r + out * out_r) / 1_000_000.0


def calc_call_cost_breakdown(
    raw_model: str,
    inp: int,
    cache_read: int,
    out: int,
    think: int = 0,
    resp: int = 0,
    event_ts: float | None = None,
) -> dict[str, float]:
    """Return itemized API cost breakdown (in, cache, prompt, out, think, resp, total) for a single call."""
    prompt = inp + cache_read
    in_r, _, cr_r, out_r = _model_rates(raw_model, prompt, event_ts=event_ts)
    c_in = (inp * in_r) / 1_000_000.0
    c_cache = (cache_read * cr_r) / 1_000_000.0
    c_prompt = c_in + c_cache
    c_out = (out * out_r) / 1_000_000.0
    sub_tot = think + resp
    if sub_tot > 0 and out > 0:
        c_think = c_out * (think / sub_tot)
        c_resp = c_out * (resp / sub_tot)
    else:
        c_think = (think * out_r) / 1_000_000.0
        c_resp = (resp * out_r) / 1_000_000.0
    return {
        "in": c_in,
        "cache": c_cache,
        "prompt": c_prompt,
        "out": c_out,
        "think": c_think,
        "resp": c_resp,
        "total": c_prompt + c_out,
    }


def build_models_board(main_models: list[dict], sub_list: list[dict]) -> dict:
    """Aggregate token usage & API cost across main conversation and all subagents by canonical model."""
    buckets: dict[str, dict] = {}

    for mm in main_models:
        key = canonical_model_label(str(mm.get("model") or ""))
        b = buckets.setdefault(
            key,
            {
                "model": key,
                "has_main": False,
                "sub_count": 0,
                "calls": 0,
                "prompt": 0,
                "in": 0,
                "cache": 0,
                "out": 0,
                "cost": 0.0,
            },
        )
        b["has_main"] = True
        b["calls"] += int(mm.get("calls") or 0)
        b["prompt"] += int(mm.get("prompt") or 0)
        b["in"] += int(mm.get("in") or 0)
        b["cache"] += int(mm.get("cache") or 0)
        b["cache_write"] = b.get("cache_write", 0) + int(mm.get("cache_write") or 0)
        b["out"] += int(mm.get("out") or 0)
        b["cost"] += float(mm.get("cost") or 0.0)

    active_sub_count = 0
    for s in models_board_subagents(sub_list, canonical_model_label):
        if s.get("metrics_unavailable"):
            continue
        s_calls = int(s.get("calls") or 0)
        s_prompt = int(s.get("prompt") or 0)
        s_out = int(s.get("out") or 0)
        if s_calls <= 0 and s_prompt <= 0 and s_out <= 0:
            continue
        active_sub_count += 1
        per_model = s.get("per_model")
        if isinstance(per_model, list) and per_model:
            seen_keys: set[str] = set()
            for pm in per_model:
                if not isinstance(pm, dict):
                    continue
                pm_calls = int(pm.get("calls") or 0)
                pm_prompt = int(pm.get("prompt") or 0)
                pm_out = int(pm.get("out") or 0)
                if pm_calls <= 0 and pm_prompt <= 0 and pm_out <= 0:
                    continue
                key = canonical_model_label(str(pm.get("model") or s.get("model") or s.get("agent") or ""))
                b = buckets.setdefault(
                    key,
                    {
                        "model": key,
                        "has_main": False,
                        "sub_count": 0,
                        "calls": 0,
                        "prompt": 0,
                        "in": 0,
                        "cache": 0,
                        "out": 0,
                        "cost": 0.0,
                    },
                )
                if key not in seen_keys:
                    b["sub_count"] += 1
                    seen_keys.add(key)
                b["calls"] += pm_calls
                b["prompt"] += pm_prompt
                b["in"] += int(pm.get("in") or 0)
                b["cache"] += int(pm.get("cache") or 0)
                b["cache_write"] = b.get("cache_write", 0) + int(pm.get("cache_write") or 0)
                b["out"] += pm_out
                b["cost"] += float(pm.get("cost") or 0.0)
            continue

        key = canonical_model_label(str(s.get("model") or s.get("agent") or ""))
        b = buckets.setdefault(
            key,
            {
                "model": key,
                "has_main": False,
                "sub_count": 0,
                "calls": 0,
                "prompt": 0,
                "in": 0,
                "cache": 0,
                "out": 0,
                "cost": 0.0,
            },
        )
        b["sub_count"] += 1
        b["calls"] += s_calls
        b["prompt"] += s_prompt
        b["in"] += int(s.get("in") or 0)
        b["cache"] += int(s.get("cache") or 0)
        b["cache_write"] = b.get("cache_write", 0) + int(s.get("cache_write") or 0)
        b["out"] += s_out
        s_cost = float(s.get("cost") or 0.0)
        if s.get("cost") is None and (s_prompt > 0 or s_out > 0):
            s_cost = calc_call_cost(
                str(s.get("model") or key),
                int(s.get("in") or 0),
                int(s.get("cache") or 0),
                s_out,
            )
        b["cost"] += s_cost

    tot_prompt = sum(b["prompt"] for b in buckets.values())
    tot_in = sum(b["in"] for b in buckets.values())
    tot_cache = sum(b["cache"] for b in buckets.values())
    tot_out = sum(b["out"] for b in buckets.values())
    tot_calls = sum(b["calls"] for b in buckets.values())
    tot_cost = sum(b["cost"] for b in buckets.values())
    tot_hit = (tot_cache * 100.0 / tot_prompt) if tot_prompt > 0 else None
    tot_subs = active_sub_count

    items: list[dict] = []
    for b in buckets.values():
        p = b["prompt"]
        hit = (b["cache"] * 100.0 / p) if p > 0 else None
        c_pct = (b["cost"] * 100.0 / tot_cost) if tot_cost > 0 else 0.0
        if b["has_main"] and b["sub_count"] > 0:
            src_tag = f"主+{b['sub_count']}子"
        elif b["has_main"]:
            src_tag = "主"
        else:
            src_tag = f"{b['sub_count']}子"
        items.append({
            "model": b["model"],
            "source_tag": src_tag,
            "calls": b["calls"],
            "prompt": p,
            "in": b["in"], "cache_write": b["cache_write"],
            "cache": b["cache"],
            "hit_pct": hit,
            "out": b["out"],
            "cost": b["cost"],
            "cost_pct": c_pct,
        })

    items.sort(key=lambda x: (x["cost"], x["prompt"]), reverse=True)
    return {
        "metrics_incomplete": any(s.get("metrics_unavailable") for s in sub_list),
        "summary": {
            "count": len(items),
            "sub_count": tot_subs,
            "calls": tot_calls,
            "prompt": tot_prompt,
            "in": tot_in, "cache_write": sum(b["cache_write"] for b in buckets.values()),
            "cache": tot_cache,
            "hit_pct": tot_hit,
            "out": tot_out,
            "cost": tot_cost,
        },
        "items": items,
    }
