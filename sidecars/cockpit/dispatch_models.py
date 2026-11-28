"""Auto-resolve versionless model family names to the latest concrete CLI model IDs."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

_RUNTIME_DIR = str(Path(__file__).resolve().parents[2] / "runtime")
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)
from agent_runtime.policy_contract import (
    AGY_CLAUDE_RE as _RE_AGY_CLAUDE, AGY_GEMINI_RE as _RE_AGY_GEMINI,
    AGY_DEFAULT_EFFORT as _AGY_DEFAULT_EFFORT, agy_model_family,
)

_CODEX_CACHE = Path.home() / ".codex/models_cache.json"
_DEFAULT_CODEX_CACHE = _CODEX_CACHE
_GROK_CACHE = Path.home() / ".grok/models_cache.json"
_AGY_STATE = Path.home() / ".gemini/antigravity/sidecar_data/statusline/view_state.json"
_FACTORY_CACHE_DIR = Path.home() / ".factory/cache/connected-model-provider-models"
_LOCK = threading.Lock()
_FILE_CACHE: dict[Path, tuple[tuple[int, int], object]] = {}
_CODEX_RPC_CACHE: tuple[float, list[str] | None] | None = None
_CODEX_RPC_TTL_SEC = 300.0
_CODEX_RPC_FAIL_TTL_SEC = 60.0

_CODEX_DEFAULTS = {"sol": "gpt-6.2-sol", "astra": "gpt-6.2-astra", "terra": "gpt-6.1-terra", "luna": "gpt-6-luna"}
_GROK_DEFAULTS = {"grok": "grok-4.7", "grok-fast": "grok-4.7-build-fast"}
_AGY_DEFAULTS = {
    "hydrogen": "gemini-4-hydrogen", "neon": "gemini-4-neon", "argon": "gemini-4-argon",
    "carbon": "gemini-4-carbon", "flash": "gemini-3.8-flash",
    "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
}
_RE_CODEX = re.compile(r"^gpt-(\d+(?:\.\d+)*)-([a-z]+)$")
_RE_GROK = re.compile(r"^grok-(\d+(?:\.\d+)*)$")
_RE_GROK_FAST = re.compile(r"^grok-(\d+(?:\.\d+)*)-(?:build-)?fast$")


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


def _find_codex_bin() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    for d in (Path.home() / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        cand = d / "codex"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _fetch_codex_models_rpc(timeout: float = 5.0) -> list[str] | None:
    global _CODEX_RPC_CACHE
    now = time.monotonic()
    with _LOCK:
        if _CODEX_RPC_CACHE is not None:
            cached_ts, cached_val = _CODEX_RPC_CACHE
            ttl = _CODEX_RPC_TTL_SEC if cached_val is not None else _CODEX_RPC_FAIL_TTL_SEC
            if (now - cached_ts) < ttl:
                return list(cached_val) if cached_val is not None else None
    codex_bin = _find_codex_bin()
    if not codex_bin:
        with _LOCK:
            _CODEX_RPC_CACHE = (time.monotonic(), None)
        return None
    proc = None
    result: list[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="agy-cockpit-codex-models-") as td:
            proc = subprocess.Popen(
                [codex_bin, "app-server", "--stdio"],
                cwd=td,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            out_q: "queue.Queue[str | None]" = queue.Queue()

            def _pump() -> None:
                try:
                    if proc.stdout is not None:
                        for ln in proc.stdout:
                            out_q.put(ln)
                finally:
                    out_q.put(None)

            threading.Thread(target=_pump, daemon=True).start()
            assert proc.stdin is not None
            deadline = time.monotonic() + max(2.0, float(timeout))

            def _wait_id(target_id: int) -> dict | None:
                while True:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        return None
                    try:
                        ln = out_q.get(timeout=rem)
                    except queue.Empty:
                        return None
                    if ln is None:
                        return None
                    try:
                        msg = json.loads(ln)
                    except Exception:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == target_id:
                        return msg

            def _send(obj: dict) -> None:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()

            _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"clientInfo": {"name": "agy-cockpit", "version": "1.0"}, "capabilities": {"experimentalApi": True}}})
            init = _wait_id(1)
            if init and "error" not in init:
                _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                _send({"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}})
                rep = _wait_id(2)
                res = rep.get("result") if isinstance(rep, dict) and isinstance(rep.get("result"), dict) else None
                data = res.get("data") if isinstance(res, dict) else None
                if isinstance(data, list):
                    slugs = [
                        str(item.get("id") or item.get("model") or "").strip()
                        for item in data
                        if isinstance(item, dict) and item.get("hidden") is not True
                    ]
                    slugs = [s for s in slugs if s]
                    if slugs:
                        result = slugs
    except Exception:
        result = None
    finally:
        if proc is not None:
            for p in (proc.stdin, proc.stdout):
                if p:
                    with contextlib.suppress(Exception):
                        p.close()
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                proc.wait(timeout=1.0)
        with _LOCK:
            _CODEX_RPC_CACHE = (time.monotonic(), list(result) if result is not None else None)
    return result


def _latest_codex(cache_path: Path | None = None, *, allow_rpc: bool = True) -> dict[str, str]:
    best: dict[str, tuple[tuple[int, ...], str]] = {
        fam: (_ver_tuple(slug), slug) for fam, slug in _CODEX_DEFAULTS.items()
    }
    slugs: list[str] = []
    if allow_rpc and cache_path is None and _CODEX_CACHE == _DEFAULT_CODEX_CACHE:
        rpc_slugs = _fetch_codex_models_rpc()
        if rpc_slugs:
            slugs = rpc_slugs
    if not slugs:
        doc = _read_json_cached(cache_path or _CODEX_CACHE)
        models = doc.get("models") if isinstance(doc, dict) else None
        if isinstance(models, list):
            slugs = [item.get("slug") for item in models if isinstance(item, dict) and isinstance(item.get("slug"), str)]
    for slug in slugs:
        m = _RE_CODEX.fullmatch(slug)
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


def _latest_droid(codex_cache_path: Path | None = None, agy_state_path: Path | None = None,
                  grok_cache_path: Path | None = None, factory_cache_dir: Path | None = None,
                  *, allow_codex_rpc: bool = True) -> dict[str, str]:
    merged = {
        **_latest_agy(agy_state_path),
        **_latest_codex(codex_cache_path, allow_rpc=allow_codex_rpc),
        **_latest_grok(grok_cache_path),
    }
    fdir = factory_cache_dir if factory_cache_dir is not None else _FACTORY_CACHE_DIR
    if fdir.is_dir():
        for path in sorted(fdir.glob("*.json"))[:4]:
            doc = _read_json_cached(path)
            avail = doc.get("availableModels") if isinstance(doc, dict) else None
            if not isinstance(avail, dict):
                continue
            for slug in avail:
                if not isinstance(slug, str):
                    continue
                low = slug.lower()
                for regex, fam_idx, ver_idx, fixed in (
                    (_RE_CODEX, 2, 1, None), (_RE_AGY_GEMINI, 2, 1, None),
                    (_RE_AGY_CLAUDE, 1, 2, None), (_RE_GROK, 0, 1, "grok"), (_RE_GROK_FAST, 0, 1, "grok-fast"),
                ):
                    m = regex.fullmatch(low)
                    if m:
                        fam = fixed or m.group(fam_idx)
                        if fam in merged and _ver_tuple(m.group(ver_idx)) > _ver_tuple(merged[fam]):
                            merged[fam] = slug
                        break
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
        return agy_model_family(low)
    if engine == "droid":
        return family_of("codex", low) or family_of("agy", low) or family_of("grok", low)
    return None


def resolve_model_id(
    engine: str,
    model: str,
    effort: str = "",
    *,
    codex_cache_path: Path | None = None,
    grok_cache_path: Path | None = None,
    agy_state_path: Path | None = None,
    allow_codex_rpc: bool = True,
) -> str:
    """Resolve a versionless family name to the latest concrete CLI model ID; keep explicit IDs verbatim."""
    low = (model or "").strip().lower()
    if engine == "codex" and low in _CODEX_DEFAULTS:
        return _latest_codex(codex_cache_path, allow_rpc=allow_codex_rpc)[low]
    if engine == "grok" and low in _GROK_DEFAULTS:
        return _latest_grok(grok_cache_path)[low]
    if engine == "agy" and low in _AGY_DEFAULTS:
        base = _latest_agy(agy_state_path)[low]
        eff = effort if effort and effort != "same" else _AGY_DEFAULT_EFFORT[low]
        return f"{base}-{eff}"
    if engine == "droid" and (low in _CODEX_DEFAULTS or low in _AGY_DEFAULTS or low in _GROK_DEFAULTS):
        return _latest_droid(codex_cache_path, agy_state_path, grok_cache_path, allow_codex_rpc=allow_codex_rpc)[low]
    return model


def fallback_lookup_keys(engine: str, model: str, resolved_model: str, effort: str = "") -> tuple[str, ...]:
    """Ordered fallback dictionary keys: exact, resolved concrete ID, offline default ID, and versionless family."""
    fam = family_of(engine, model) or family_of(engine, resolved_model)
    offline_id = None
    if fam and model.strip().lower() == fam and resolved_model == model:
        if engine == "codex":
            offline_id = _CODEX_DEFAULTS[fam]
        elif engine == "grok":
            offline_id = _GROK_DEFAULTS[fam]
        elif engine == "agy":
            eff = effort if effort and effort != "same" else _AGY_DEFAULT_EFFORT[fam]
            offline_id = f"{_AGY_DEFAULTS[fam]}-{eff}"
        elif engine == "droid":
            offline_id = _CODEX_DEFAULTS.get(fam) or _AGY_DEFAULTS.get(fam) or _GROK_DEFAULTS.get(fam)
    keys: list[str] = []
    for cand in (model, resolved_model, offline_id, fam):
        if cand:
            key = f"{engine}:{cand}"
            if key not in keys:
                keys.append(key)
    return tuple(keys)


def latest_family_map(*, codex_cache_path: Path | None = None, grok_cache_path: Path | None = None,
                      agy_state_path: Path | None = None, factory_cache_dir: Path | None = None) -> dict[str, dict[str, str]]:
    return {
        "agy": _latest_agy(agy_state_path),
        "codex": _latest_codex(codex_cache_path),
        "grok": _latest_grok(grok_cache_path),
        "droid": _latest_droid(codex_cache_path, agy_state_path, grok_cache_path, factory_cache_dir),
    }
