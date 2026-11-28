"""Antigravity theme seeds for the dashboard; no quota probe side effects."""
from __future__ import annotations

import json
import re

from cockpit_io import HOME, serialized

_AGY_CONFIG_FILE = HOME / ".gemini" / "config" / "config.json"
_THEME_CFG_MTIME_NS: int = -1
_THEME_CFG_CACHE: dict = {
    "mode": "dark",
    "dark": {"background": "#101010", "foreground": "#cccccc", "primary": "#007acc"},
    "light": {"background": "#F9F9F9", "foreground": "#101010", "primary": "#007acc"},
}


def _norm_hex_color(val: object, default: str) -> str:
    if not isinstance(val, str):
        return default
    s = val.strip()
    if not s:
        return default
    if not s.startswith("#"):
        s = "#" + s
    if len(s) == 4:
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return s if re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", s) else default


@serialized(lambda: "live-theme")
def get_live_theme() -> dict:
    """Read Antigravity userSettings theme seeds from ~/.gemini/config/config.json with mtime_ns caching."""
    global _THEME_CFG_MTIME_NS, _THEME_CFG_CACHE
    try:
        st = _AGY_CONFIG_FILE.stat()
        if st.st_mtime_ns == _THEME_CFG_MTIME_NS:
            return _THEME_CFG_CACHE
        raw = json.loads(_AGY_CONFIG_FILE.read_text(encoding="utf-8"))
        us = (raw.get("userSettings") if isinstance(raw, dict) else None) or {}
        raw_mode = us.get("themeMode")
        mode = "dark"
        if raw_mode == 2 or (isinstance(raw_mode, str) and "LIGHT" in raw_mode.upper()):
            mode = "light"
        elif raw_mode == 3 or (
            isinstance(raw_mode, str) and any(k in raw_mode.upper() for k in ("INHERIT", "SYSTEM", "AUTO"))
        ):
            mode = "system"

        d_seeds = us.get("customThemeSeedsDark") if isinstance(us.get("customThemeSeedsDark"), dict) else {}
        l_seeds = us.get("customThemeSeedsLight") if isinstance(us.get("customThemeSeedsLight"), dict) else {}

        _THEME_CFG_CACHE = {
            "mode": mode,
            "dark": {
                "background": _norm_hex_color(d_seeds.get("background"), "#101010"),
                "foreground": _norm_hex_color(d_seeds.get("foregroundOverride"), "#cccccc"),
                "primary": _norm_hex_color(d_seeds.get("primary"), "#007acc"),
            },
            "light": {
                "background": _norm_hex_color(l_seeds.get("background"), "#F9F9F9"),
                "foreground": _norm_hex_color(l_seeds.get("foregroundOverride"), "#101010"),
                "primary": _norm_hex_color(l_seeds.get("primary"), "#007acc"),
            },
        }
        _THEME_CFG_MTIME_NS = st.st_mtime_ns
    except Exception:
        pass
    return _THEME_CFG_CACHE
