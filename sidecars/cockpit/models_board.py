"""Aggregate the dashboard model view across main sessions and subagents."""
from __future__ import annotations

from metric_items import add_model_usage
from pricing import calc_call_cost, canonical_model_label


def _board_subagents(items):
    """Count each conversation once; its usage already excludes proven fork history."""
    seen = set()
    for item in items:
        cid = item.get("conversation_id") or item.get("id")
        if item.get("metrics_unavailable") or not any(item.get(k, 0) for k in ("calls", "prompt", "out")):
            continue
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        yield item


def _add_usage(buckets, key, metrics, calls, prompt, out, cost):
    return add_model_usage(buckets, key, calls=calls, prompt=prompt, out=out, cost=cost,
                           input_tokens=int(metrics.get("in") or 0), cache=int(metrics.get("cache") or 0),
                           cache_write=int(metrics.get("cache_write") or 0))


def build_models_board(main_models: list[dict], sub_list: list[dict]) -> dict:
    """Aggregate token usage & API cost across main conversation and all subagents by canonical model."""
    buckets: dict[str, dict] = {}

    for mm in main_models:
        key = canonical_model_label(str(mm.get("model") or ""))
        b = _add_usage(buckets, key, mm, int(mm.get("calls") or 0), int(mm.get("prompt") or 0),
                       int(mm.get("out") or 0), float(mm.get("cost") or 0.0))
        b["has_main"] = True

    active_sub_count = 0
    for s in _board_subagents(sub_list):
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
                b = _add_usage(buckets, key, pm, pm_calls, pm_prompt, pm_out, float(pm.get("cost") or 0.0))
                if key not in seen_keys:
                    b["sub_count"] = b.get("sub_count", 0) + 1
                    seen_keys.add(key)
            continue

        key = canonical_model_label(str(s.get("model") or s.get("agent") or ""))
        s_cost = float(s.get("cost") or 0.0)
        if s.get("cost") is None and (s_prompt > 0 or s_out > 0):
            s_cost = calc_call_cost(
                str(s.get("model") or key),
                int(s.get("in") or 0),
                int(s.get("cache") or 0),
                s_out,
            )
        b = _add_usage(buckets, key, s, s_calls, s_prompt, s_out, s_cost)
        b["sub_count"] = b.get("sub_count", 0) + 1

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
        sub_count = b.get("sub_count", 0)
        if b.get("has_main") and sub_count:
            src_tag = f"主+{sub_count}子"
        elif b.get("has_main"):
            src_tag = "主"
        else:
            src_tag = f"{sub_count}子"
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
