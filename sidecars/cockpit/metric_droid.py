"""Read droid usage and performance from offline session records."""
from __future__ import annotations
import json
from analyzer import log_lines
from pricing import calc_call_cost, canonical_model_label, estimate_source, model_quota_family, short_model_hint
from metric_rates import number, speed, split
from metric_logs import _conserve, _reconcile_costs, parse_iso_ts
from droid_acp_fallback import acp_metrics, supplement_acp

_FAILED_OUTCOMES = {"error", "aborted", "failed", "model_request_rejected"}


def allocate_tokens(total, weights):
    """Largest remainder on integer numerators; stable order breaks exact ties."""
    denominator = sum(weights)
    parts = [divmod(total * w, denominator) for w in weights]
    result = [q for q, _ in parts]
    for i in sorted(range(len(parts)), key=lambda i: -parts[i][1])[:total - sum(result)]:
        result[i] += 1
    return result


def read_droid(src, raw_path, jsonl_path, mtime):
    try:
        obj = json.loads(src.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return acp_metrics(src)

    cw = 0
    raw_inp = 0
    is_done = True
    saw_terminal = False
    is_err = False
    event_ts = mtime
    turn_rows = {}
    first_call_by_turn = {}
    rejected = set()
    reported_turns = 0
    first_turn_ts = None
    if "tokenUsage" in obj:
        tu = obj.get("tokenUsage") or {}
        raw_inp = int(tu.get("inputTokens") or 0)
        cw = int(tu.get("cacheCreationTokens") or 0)
        inp = raw_inp + cw
        cache = int(tu.get("cacheReadTokens") or 0)
        out = int(tu.get("outputTokens") or 0)
        think = tu.get("thinkingTokens")
        dur_s = float(obj.get("assistantActiveTimeMs") or 0.0) / 1000.0
        model = str(obj.get("model") or "droid")
        effort = str(obj.get("reasoningEffort") or "")
        if model in ("", "auto", "droid"):
            eff_router = obj.get("effectiveFactoryRouterModel")
            if isinstance(eff_router, dict) and eff_router.get("modelId"):
                model = str(eff_router["modelId"])
                if effort in ("", "none") and eff_router.get("reasoningEffort"):
                    effort = str(eff_router["reasoningEffort"])
            elif obj.get("toolExecutionModeModelId"):
                model = str(obj["toolExecutionModeModelId"])
        if effort == "none":
            effort = ""
        lock_ts = parse_iso_ts(obj.get("providerLockTimestamp"))
        if lock_ts:
            event_ts = lock_ts
        calls = 0
        turn_ts_models: list[tuple[float, str]] = []
        if raw_path and raw_path.exists():
            try:
                ro = json.loads(raw_path.read_text(encoding="utf-8", errors="replace"))
                reported_turns = int(ro.get("num_turns") or 0)
                if ro.get("is_error") is True:
                    is_err = True
            except Exception:
                pass
        if jsonl_path is not None:
            try:
                event_ts = jsonl_path.stat().st_mtime
                jl_lines = list(log_lines(jsonl_path))
                latest_stamp, latest_model = event_ts, model
                open_turn = first_call = None
                for index, l in enumerate(jl_lines):
                    try:
                        lo = json.loads(l)
                    except Exception:
                        continue
                    msg = lo.get("message") or {}
                    latest_stamp = parse_iso_ts(lo.get("timestamp")) or latest_stamp
                    turn_id = lo.get("turnId") or msg.get("turnId")
                    if msg.get("role") == "user":
                        open_turn, first_call, latest_model = None, None, model
                    if turn_id:
                        if open_turn is not None and turn_id != open_turn:
                            first_call = None
                        open_turn = turn_id
                    if msg.get("role") == "assistant" or lo.get("type") == "agent_turn_outcome":
                        latest_model = str(lo.get("modelId") or lo.get("model") or msg.get("modelId") or msg.get("model") or latest_model)
                    if lo.get("type") == "agent_turn_outcome":
                        key = turn_id or open_turn or ("outcome", index)
                        turn_rows[key] = (latest_stamp, latest_model)
                        if lo.get("reason") in _FAILED_OUTCOMES:
                            rejected.add(key)
                        if first_call is not None:
                            first_call_by_turn.setdefault(key, first_call)
                        open_turn = first_call = None
                    elif open_turn is not None:
                        turn_rows[open_turn] = (latest_stamp, latest_model)
                    if msg.get("role") != "assistant":
                        continue
                    msg_id = str(msg.get("id") or lo.get("id") or "")
                    if msg_id.startswith("repl-tool-call:"):
                        continue
                    stamped = parse_iso_ts(lo.get("timestamp"))
                    if not turn_ts_models:
                        first_turn_ts = stamped
                    t_ts = stamped or event_ts
                    t_mod = str(lo.get("modelId") or lo.get("model") or msg.get("modelId") or msg.get("model") or model)
                    latest_model = t_mod
                    turn_ts_models.append((t_ts, t_mod))
                    if first_call is None:
                        first_call = (len(turn_ts_models), stamped)
                    if open_turn is not None:
                        first_call_by_turn.setdefault(open_turn, first_call)
                if calls <= 0 and turn_ts_models:
                    calls = len(turn_ts_models)
                for l in reversed(jl_lines):
                    try:
                        lo = json.loads(l)
                        ts_val = parse_iso_ts(lo.get("timestamp"))
                        if ts_val:
                            event_ts = ts_val
                            break
                    except Exception:
                        continue
                if jl_lines:
                    last_o = json.loads(jl_lines[-1])
                    if last_o.get("type") == "agent_turn_outcome":
                        reason = str(last_o.get("reason") or "")
                        saw_terminal = True
                        is_done = True
                        if reason in _FAILED_OUTCOMES:
                            is_err = True
                    else:
                        is_done = False
            except Exception:
                pass
        if calls <= 0 and (inp > 0 or cache > 0 or out > 0):
            calls = 1
    elif "usage" in obj:
        u = obj.get("usage") or {}
        raw_inp = int(u.get("input_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0)
        inp = raw_inp + cw
        cache = int(u.get("cache_read_input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        think = u.get("thinking_tokens")
        dur_s = float(obj.get("duration_ms") or 0.0) / 1000.0
        model = str(obj.get("model") or "droid")
        effort = ""
        reported_turns = int(obj.get("num_turns") or 0)
        calls = reported_turns or 1
        is_err = bool(obj.get("is_error"))
        turn_ts_models = []
    else:
        return acp_metrics(src)


    turns = list(turn_rows.values()) or [(event_ts, model)] * reported_turns
    prompt = inp + cache
    hit_pct = (cache * 100.0 / prompt) if prompt > 0 else None
    think, resp = split(out, int(think) if think is not None else None)
    base_short = short_model_hint(model)
    m_disp = f"{base_short} ({effort})" if effort else base_short
    cost_events = []
    per_model_map = {}
    if turn_ts_models:
        counts = {}
        for _, name in turn_ts_models:
            counts[name or model] = counts.get(name or model, 0) + 1
        names = list(counts)
        weights = list(counts.values())
        allocations = [allocate_tokens(total, weights) for total in (raw_inp, cache, out, cw)]
        for i, name in enumerate(names):
            raw_part, cache_part, out_part, write_part = (values[i] for values in allocations)
            amount = calc_call_cost(name, raw_part, cache_part, out_part, write_part, calls=max(1, counts[name]), event_ts=event_ts)
            per_model_map[name] = {'model': name, 'calls': counts[name], 'prompt': raw_part + write_part + cache_part,
                                   'in': raw_part, 'cache_write': write_part, 'cache': cache_part,
                                   'out': out_part, 'cost': amount, 'is_estimated': True}
        tot_cost = sum(item['cost'] for item in per_model_map.values())
        per_call_weights = [per_model_map[name or model]['cost'] / counts[name or model] for _, name in turn_ts_models]
        amounts = _conserve(tot_cost, per_call_weights)
        def split_tokens(total):
            quotient, remainder = divmod(int(total), len(turn_ts_models))
            return [quotient + int(i < remainder) for i in range(len(turn_ts_models))]
        event_tokens = zip(split_tokens(raw_inp), split_tokens(cache), split_tokens(out), split_tokens(cw))
        cost_events = [(round(ts, 3), amount, model_quota_family(name or model), "",
                        i + w, c, o, 0, w, canonical_model_label(name or model))
                       for (ts, name), amount, (i, c, o, w) in zip(turn_ts_models, amounts, event_tokens)]
    else:
        tot_cost = calc_call_cost(model, raw_inp, cache, out, cw, calls=max(1, calls), event_ts=event_ts)
        cost_events = [(round(event_ts, 3), tot_cost, model_quota_family(model), "",
                        inp, cache, out, 0, cw, canonical_model_label(model))] if tot_cost > 0 else []
        per_model_map[model] = {'model': model, 'calls': calls, 'prompt': prompt, 'in': raw_inp,
                               'cache_write': cw, 'cache': cache, 'out': out, 'cost': tot_cost, 'is_estimated': True}
    cost_events = _reconcile_costs(tot_cost, cost_events, per_model_map)
    cost_events.extend((round(ts, 3), 0., model_quota_family(name), "", 0, 0, 0, 1, 0,
                        canonical_model_label(name)) for ts, name in turns)
    res = {
        "model": m_disp,
        "calls": calls,
        "prompt": prompt,
        "in": raw_inp, "cache_write": cw, "turns": len(turns),
        "cache": cache,
        "hit_pct": hit_pct,
        "out": out,
        "think": think,
        "resp": resp,
        **speed("active" if "tokenUsage" in obj else "turn", out, dur_s, calls),
        "cost_source": estimate_source(model),
        "cost": tot_cost,
        "cost_estimated": True,
        "_droid_usage": {"raw_inp": raw_inp, "cw": cw, "cache": cache, "out": out,
                         "first_assistant_calls": [first_call_by_turn.get(k) for k in turn_rows],
                         "turn_ids": [f"id:{k}" if not isinstance(k, tuple) else f"slot:{i}"
                                      for i, k in enumerate(turn_rows)],
                         "turns": turns, "rejected_indices": [i for i, key in enumerate(turn_rows) if key in rejected],
                         "first_turn_ts": first_turn_ts, "event_ts": event_ts, "model": model},
        "cost_events": cost_events,
        "per_model": list(per_model_map.values()),
        "is_done": is_done,
        "_saw_terminal": saw_terminal,
        "_terminal_done": is_done,
        "is_err": is_err,
        "mtime": mtime,
    }
    return supplement_acp(raw_path, res)
