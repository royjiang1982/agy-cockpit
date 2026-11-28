"""Format metrics without reconstructing missing observations."""
from __future__ import annotations

from metric_rates import grouped_speeds, speed, sum_known
from pricing import estimate_source


def add_model_usage(models, model, *, calls, prompt, input_tokens, cache, out, cost, cache_write=0):
    """Accumulate usage by the caller's model key, including uncached input and writes."""
    row = models.setdefault(model, {
        "model": model, "calls": 0, "prompt": 0, "in": 0,
        "cache": 0, "cache_write": 0, "out": 0, "cost": 0.0,
    })
    row["calls"] += calls
    row["prompt"] += prompt
    row["in"] += input_tokens
    row["cache"] += cache
    row["cache_write"] += cache_write
    row["out"] += out
    row["cost"] += cost
    return row


def make_sub_item(
    sub_id: str,
    agent: str,
    label: str,
    status: str,
    model: str,
    step_idx: int,
    last_ts: float,
    metrics: dict | None = None,
    tps_key: str = "tps",
    per_model: list[dict] | None = None,
    last_prompt_ts: float | None = None,
    last_prompt_idx: int | None = None,
) -> dict:
    prompt_ts = float(last_prompt_ts) if last_prompt_ts is not None else float(last_ts or 0.0)
    prompt_idx = int(last_prompt_idx) if last_prompt_idx is not None else int(step_idx or 0)
    if metrics is None:
        return {
            "id": sub_id,
            "agent": agent,
            "label": label,
            "status": status,
            "model": model,
            "calls": 0,
            "prompt": 0,
            "in": 0,
            "cache": 0,
            "hit_pct": None, "cache_write": 0,
            "out": 0,
            "think": None,
            "resp": None,
            **speed(None, 0, 0),
            "cost": 0.0,
            "per_model": [],
            "last_ts": last_ts,
            "last_prompt_ts": prompt_ts,
            "last_prompt_idx": prompt_idx,
            "step_idx": step_idx,
            "metrics_unavailable": True,
        }
    pm_list = per_model if per_model is not None else list(metrics.get("per_model") or [])
    calls = int(metrics.get("calls") or 0)
    prompt = int(metrics.get("prompt") or 0)
    out = int(metrics.get("out") or 0)
    item = {
        "id": sub_id,
        "agent": agent,
        "label": label,
        "status": status,
        "model": model,
        "calls": calls,
        "prompt": prompt,
        "in": int(metrics.get("in") or 0),
        "cache": int(metrics.get("cache") or 0),
        "cache_write": int(metrics.get("cache_write") or 0),
        "hit_pct": metrics.get("hit_pct") if prompt else None,
        "out": out,
        "think": metrics.get("think"),
        "resp": metrics.get("resp"),
        "tps": metrics.get(tps_key) if metrics.get("tps_dur") else None,
        "cost": float(metrics.get("cost") or 0.0),
        "cost_source": metrics.get("cost_source") or estimate_source(model),
        **{k: metrics.get(k) for k in ("turns", "tps_kind", "tps_out", "tps_dur", "tps_calls", "ttft", "ttft_calls", "ttft_kind")},
        "per_model": pm_list,
        "last_ts": last_ts,
        "last_prompt_ts": prompt_ts,
        "last_prompt_idx": prompt_idx,
        "step_idx": step_idx,
    }
    if (metrics.get("usage_available") is False) or (status == "running" and calls == 0 and prompt == 0 and out == 0):
        item["metrics_unavailable"] = True
    return item


def summarize(items):
    tot_prompt = sum(x["prompt"] for x in items)
    tot_in = sum(x["in"] for x in items)
    tot_cache = sum(x["cache"] for x in items)
    tot_hit_pct = (tot_cache * 100.0 / tot_prompt) if tot_prompt > 0 else None
    tot_out = sum(x["out"] for x in items)
    tot_think = sum_known(items, "think")
    tot_resp = sum_known(items, "resp")
    tot_calls = sum(x["calls"] for x in items)
    tot_cost = sum(float(x.get("cost") or 0.0) for x in items)
    running_count = sum(1 for x in items if x.get("status") == "running")

    groups = grouped_speeds(items)

    summary = {
        "count": len(items),
        "metrics_incomplete": any(item.get("metrics_unavailable") for item in items),
        "running_count": running_count,
        "calls": tot_calls,
        "prompt": tot_prompt,
        "in": tot_in,
        "cache": tot_cache,
        "hit_pct": tot_hit_pct,
        "out": tot_out,
        "think": tot_think,
        "resp": tot_resp,
        "tps": next(iter(groups.values()))["tps"] if len(groups) == 1 else None,
        "speeds": groups, "cache_write": sum(x["cache_write"] for x in items),
        "cost": tot_cost,
    }
    return summary
