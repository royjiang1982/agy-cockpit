"""Auto-resolve versionless model family names to the latest concrete CLI model IDs."""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading

_CODEX_CACHE = Path.home() / ".codex/models_cache.json"
_GROK_CACHE = Path.home() / ".grok/models_cache.json"
_AGY_STATE = Path.home() / ".gemini/antigravity/sidecar_data/statusline/view_state.json"
_FACTORY_CACHE_DIR = Path.home() / ".factory/cache/connected-model-provider-models"
_LOCK = threading.Lock()
_FILE_CACHE: dict[Path, tuple[tuple[int, int], object]] = {}

_CODEX_DEFAULTS = {"sol": "gpt-6.2-sol", "astra": "gpt-6.2-astra", "terra": "gpt-6.1-terra", "luna": "gpt-6-luna"}
_GROK_DEFAULTS = {"grok": "grok-4.7", "grok-fast": "grok-4.7-build-fast"}
_AGY_DEFAULTS = {
    "hydrogen": "gemini-4-hydrogen", "neon": "gemini-4-neon", "argon": "gemini-4-argon",
    "carbon": "gemini-4-carbon", "flash": "gemini-3.8-flash",
    "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
}
_AGY_DEFAULT_EFFORT = {
    "hydrogen": "high", "neon": "medium", "argon": "medium", "carbon": "medium",
    "flash": "high", "opus": "high", "sonnet": "high",
}

_RE_CODEX = re.compile(r"^gpt-(\d+(?:\.\d+)*)-([a-z]+)$")
_RE_GROK = re.compile(r"^grok-(\d+(?:\.\d+)*)$")
_RE_GROK_FAST = re.compile(r"^grok-(\d+(?:\.\d+)*)-(?:build-)?fast$")
_RE_AGY_GEMINI = re.compile(r"^gemini-(\d+(?:\.\d+)*)-(hydrogen|neon|argon|carbon|flash)(?:-(?:low|medium|high|xhigh|max|ultra))?$")
_RE_AGY_CLAUDE = re.compile(r"^claude-(opus|sonnet)-(\d+(?:[.-]\d+)*)(?:-(?:low|medium|high|xhigh|max|ultra))?$")


def _ver_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)) or (0,)


def _read_json_cached(path: Path):
    with _LOCK:
        try:
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size)
            cached = _FILE_CACHE.get(path)
            if cached is not None and cached[0] == sig:
                return cached[1]
            data = json.loads(path.read_text(encoding="utf-8"))
            _FILE_CACHE[path] = (sig, data)
            return data
        except (OSError, ValueError, RecursionError):
            return None


def _latest_codex(cache_path: Path | None = None) -> dict[str, str]:
    best: dict[str, tuple[tuple[int, ...], str]] = {
        fam: (_ver_tuple(slug), slug) for fam, slug in _CODEX_DEFAULTS.items()
    }
    doc = _read_json_cached(cache_path or _CODEX_CACHE)
    models = doc.get("models") if isinstance(doc, dict) else None
    if isinstance(models, list):
        for item in models:
            slug = item.get("slug") if isinstance(item, dict) else None
            m = _RE_CODEX.fullmatch(slug) if isinstance(slug, str) else None
            if not m:
                continue
            ver, fam = _ver_tuple(m.group(1)), m.group(2)
            if fam in best and ver > best[fam][0]:
                best[fam] = (ver, slug)
    return {fam: slug for fam, (_, slug) in best.items()}


def _latest_grok(cache_path: Path | None = None) -> dict[str, str]:
    best: dict[str, tuple[tuple[int, ...], str]] = {
        fam: (_ver_tuple(slug), slug) for fam, slug in _GROK_DEFAULTS.items()
    }
    doc = _read_json_cached(cache_path or _GROK_CACHE)
    models = doc.get("models") if isinstance(doc, dict) else None
    keys = models.keys() if isinstance(models, dict) else (
        [m.get("id") for m in models if isinstance(m, dict)] if isinstance(models, list) else ()
    )
    for slug in keys:
        if not isinstance(slug, str):
            continue
        m = _RE_GROK.fullmatch(slug)
        if m and _ver_tuple(m.group(1)) > best["grok"][0]:
            best["grok"] = (_ver_tuple(m.group(1)), slug)
            continue
        mf = _RE_GROK_FAST.fullmatch(slug)
        if mf and _ver_tuple(mf.group(1)) > best["grok-fast"][0]:
            best["grok-fast"] = (_ver_tuple(mf.group(1)), slug)
    return {fam: slug for fam, (_, slug) in best.items()}


def _scan_agy_candidates(state_path: Path | None = None) -> list[str]:
    found: list[str] = []
    doc = _read_json_cached(state_path or _AGY_STATE)
    if isinstance(doc, dict):
        for bucket in (doc.get("quota_cache") or {}).values():
            if isinstance(bucket, dict) and isinstance(bucket.get("desc"), str):
                found.extend(re.findall(r"(?:gemini-\d+(?:\.\d+)*-[a-z0-9-]+|claude-(?:opus|sonnet)-\d+(?:[.-]\d+)*[a-z0-9-]*)", bucket["desc"].lower()))
        for item in (doc.get("dynamic_models") or {}).values():
            if isinstance(item, list) and item and isinstance(item[0], str):
                text = item[0].lower()
                mg = re.search(r"gemini\s+(\d+(?:\.\d+)*)\s+(hydrogen|neon|argon|carbon|flash)", text)
                if mg:
                    found.append(f"gemini-{mg.group(1)}-{mg.group(2)}")
                mc = re.search(r"claude\s+(opus|sonnet)\s+(\d+(?:\.\d+)*)", text)
                if mc:
                    found.append(f"claude-{mc.group(1)}-{mc.group(2).replace('.', '-')}")
    return found


def _latest_agy(state_path: Path | None = None) -> dict[str, str]:
    best: dict[str, tuple[tuple[int, ...], str]] = {
        fam: (_ver_tuple(base), base) for fam, base in _AGY_DEFAULTS.items()
    }
    for cand in _scan_agy_candidates(state_path):
        mg = _RE_AGY_GEMINI.fullmatch(cand)
        if mg:
            ver, fam = _ver_tuple(mg.group(1)), mg.group(2)
            base = f"gemini-{mg.group(1)}-{fam}"
            if ver > best[fam][0]:
                best[fam] = (ver, base)
            continue
        mc = _RE_AGY_CLAUDE.fullmatch(cand)
        if mc:
            fam, ver_raw = mc.group(1), mc.group(2)
            ver = _ver_tuple(ver_raw)
            base = f"claude-{fam}-{ver_raw.replace('.', '-')}"
            if ver > best[fam][0]:
                best[fam] = (ver, base)
    return {fam: base for fam, (_, base) in best.items()}


def _latest_droid(codex_cache_path: Path | None = None, agy_state_path: Path | None = None) -> dict[str, str]:
    merged = {**_latest_agy(agy_state_path), **_latest_codex(codex_cache_path)}
    if _FACTORY_CACHE_DIR.is_dir():
        for path in sorted(_FACTORY_CACHE_DIR.glob("*.json"))[:4]:
            doc = _read_json_cached(path)
            avail = doc.get("availableModels") if isinstance(doc, dict) else None
            if not isinstance(avail, dict):
                continue
            for slug in avail:
                if not isinstance(slug, str):
                    continue
                m = _RE_CODEX.fullmatch(slug)
                if m and m.group(2) in merged and _ver_tuple(m.group(1)) > _ver_tuple(merged[m.group(2)]):
                    merged[m.group(2)] = slug
    return merged


def family_of(engine: str, model: str) -> str | None:
    """Return canonical versionless family name if recognized, else None."""
    low = (model or "").strip().lower()
    if engine == "codex":
        if low in _CODEX_DEFAULTS:
            return low
        m = _RE_CODEX.fullmatch(low)
        return m.group(2) if m and m.group(2) in _CODEX_DEFAULTS else None
    if engine == "grok":
        if low in _GROK_DEFAULTS:
            return low
        if _RE_GROK_FAST.fullmatch(low):
            return "grok-fast"
        return "grok" if _RE_GROK.fullmatch(low) else None
    if engine == "agy":
        if low in _AGY_DEFAULTS:
            return low
        mg = _RE_AGY_GEMINI.fullmatch(low)
        if mg:
            return mg.group(2)
        mc = _RE_AGY_CLAUDE.fullmatch(low)
        return mc.group(1) if mc else None
    if engine == "droid":
        return family_of("codex", low) or family_of("agy", low)
    return None


def resolve_model_id(
    engine: str,
    model: str,
    effort: str = "",
    *,
    codex_cache_path: Path | None = None,
    grok_cache_path: Path | None = None,
    agy_state_path: Path | None = None,
) -> str:
    """Resolve a versionless family name to the latest concrete CLI model ID; keep explicit IDs verbatim."""
    low = (model or "").strip().lower()
    if engine == "codex" and low in _CODEX_DEFAULTS:
        return _latest_codex(codex_cache_path)[low]
    if engine == "grok" and low in _GROK_DEFAULTS:
        return _latest_grok(grok_cache_path)[low]
    if engine == "agy" and low in _AGY_DEFAULTS:
        base = _latest_agy(agy_state_path)[low]
        eff = effort if effort and effort != "same" else _AGY_DEFAULT_EFFORT[low]
        return f"{base}-{eff}"
    if engine == "droid" and (low in _CODEX_DEFAULTS or low in _AGY_DEFAULTS):
        return _latest_droid(codex_cache_path, agy_state_path)[low]
    return model


def fallback_lookup_keys(engine: str, model: str, resolved_model: str) -> tuple[str, ...]:
    """Ordered fallback dictionary keys: exact, resolved concrete ID, and versionless family."""
    keys: list[str] = []
    for cand in (model, resolved_model, family_of(engine, model) or family_of(engine, resolved_model)):
        if cand:
            key = f"{engine}:{cand}"
            if key not in keys:
                keys.append(key)
    return tuple(keys)


def latest_family_map(*, codex_cache_path: Path | None = None, grok_cache_path: Path | None = None,
                      agy_state_path: Path | None = None) -> dict[str, dict[str, str]]:
    return {
        "agy": _latest_agy(agy_state_path),
        "codex": _latest_codex(codex_cache_path),
        "grok": _latest_grok(grok_cache_path),
        "droid": _latest_droid(codex_cache_path, agy_state_path),
    }
