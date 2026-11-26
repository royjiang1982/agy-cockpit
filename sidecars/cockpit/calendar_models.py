"""Aggregate the calendar's usage and model detail from the same cost events."""
from __future__ import annotations

from cost_archive import calendar_cost

USAGE_FIELDS = ("turns", "in", "cache_write", "cache", "out")
_HISTORICAL = {"gemini": "Gemini（历史）", "claude": "Claude（历史）", "grok": "Grok（历史）"}
_HISTORICAL_SOURCE = {"codex": "Codex（历史）"}


def _empty_usage():
    return dict.fromkeys((*USAGE_FIELDS, "_cost_ticks"), 0)


def _add_usage(row, turns, inp, cw, cache, out, ticks):
    row["turns"] += turns
    row["in"] += inp
    row["cache_write"] += cw
    row["cache"] += cache
    row["out"] += out
    row["_cost_ticks"] += ticks


def add_event_usage(daily, day, family, model, turns, inp, cw, cache, out, ticks, source=""):
    row = daily.get(day)
    if row is None:
        row = daily[day] = dict(_empty_usage(), model_usage={})
    _add_usage(row, turns, inp, cw, cache, out, ticks)
    if turns or inp or cw or cache or out or ticks:
        name = model or _HISTORICAL.get(family) or _HISTORICAL_SOURCE.get(source) or "其他模型（历史）"
        models = row["model_usage"]
        cell = models.get(name)
        if cell is None:
            cell = models[name] = _empty_usage()
        _add_usage(cell, turns, inp, cw, cache, out, ticks)


def calendar_summary(rows) -> dict:
    rows = tuple(rows)
    totals = {field: sum(row.get(field, 0) for row in rows) for field in USAGE_FIELDS}
    for field in ("turns", "in", "cache", "out"):
        totals[field] = int(totals[field])
    prompt = totals["in"] + totals["cache_write"] + totals["cache"]
    return {**totals, "prompt": prompt, "total": prompt + totals["out"],
            "hit_pct": round(totals["cache"] * 100.0 / prompt, 1) if prompt > 0 else None,
            "cost": calendar_cost(rows)}


def calendar_item(rows) -> dict:
    rows = tuple(rows)
    groups = {}
    for row in rows:
        for name, usage in row.get("model_usage", {}).items():
            groups.setdefault(name, []).append(usage)
    models = [{"model": name, **calendar_summary(usages)} for name, usages in groups.items()]
    models.sort(key=lambda row: (-row["cost"], -row["total"], -row["turns"], row["model"]))
    return {**calendar_summary(rows), "models": models}
