"""Read grok usage and performance from offline session records."""
from __future__ import annotations
import json
from analyzer import CostEvent, log_lines
from pricing import calc_call_cost, canonical_model_label, estimate_source, model_quota_family, short_model_hint
from metric_rates import number, speed, split
from metric_logs import _log_record, _reconcile_costs, parse_iso_ts
from grok_timing import grok_perf, timed_turns
from grok_costs import summary_body, _billed_usd, BILLED_UID, lock_grok_turn, merge_grok_usage, reconcile_grok_events

def read_grok(summary_path, updates_path, events_path, usage_path, raw_path, mtime, usage_mtime):
    model = "grok-4.7"
    effort = ""
    if summary_path and summary_path.exists():
        try:
            so = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
            if so.get("current_model_id"):
                model = str(so["current_model_id"])
            if so.get("reasoning_effort"):
                effort = str(so["reasoning_effort"])
        except Exception:
            pass

    tot_in = 0
    tot_cache = 0
    tot_out = 0
    tot_think = 0
    tot_cost = 0.0
    turn_count = 0
    calls = 0
    timed_usage, boundaries = [], []
    split_ok = True
    official_total = None
    tot_cw = 0
    cost_events: list[CostEvent] = []
    per_model_map: dict[str, dict] = {}
    is_done = True
    saw_terminal = False
    is_err = False

    if updates_path and updates_path.exists():
        try:
            for line in log_lines(updates_path):
                if "turn_completed" not in line:
                    continue
                try:
                    obj = _log_record(line)
                except Exception:
                    continue
                params = obj.get("params") or {}
                upd = params.get("update") or {}
                if upd.get("sessionUpdate") != "turn_completed":
                    continue
                meta = params.get("_meta") or obj.get("_meta") or {}
                ts = float(meta.get("agentTimestampMs") or 0.0) / 1000.0
                if ts <= 0:
                    ts_raw = obj.get("timestamp")
                    ts = parse_iso_ts(ts_raw) if isinstance(ts_raw, str) else float(ts_raw or mtime)
                    ts = ts or mtime
                u = upd.get("usage") or {}
                prompt_tok = int(u.get("inputTokens") or 0)
                cr = int(u.get("cachedReadTokens") or 0)
                cw = int(u.get("cacheCreationTokens") or 0)
                raw_inp = max(0, prompt_tok - cr - cw)
                inp = raw_inp + cw
                out = int(u.get("outputTokens") or 0)
                think = int(u.get("reasoningTokens") or 0)
                t_calls = int(u.get("modelCalls") or u.get("numTurns") or 1)
                api_ms = number(u.get("apiDurationMs"))
                billed = _billed_usd(u)
                c_cost = billed if billed is not None else calc_call_cost(model, raw_inp, cr, out, cw, calls=max(1, t_calls), event_ts=ts)
                if inp > 0 or cr > 0 or out > 0 or c_cost > 0:
                    calls += max(1, t_calls)
                    turn_count += 1
                    tot_in += inp
                    tot_cache += cr
                    tot_out += out
                    tot_think += think
                    first_event = len(cost_events)
                    timed_usage.append((ts, out, t_calls, api_ms, number(upd.get("elapsed_ms"))))
                    tot_cw += cw
                    split_ok &= (not out or u.get("reasoningTokens") is not None) and think <= out
                    turn_inc = 1
                    mu = u.get("modelUsage")
                    if isinstance(mu, dict) and mu:
                        for m_key, m_val in mu.items():
                            if not isinstance(m_val, dict):
                                continue
                            m_prompt = int(m_val.get("inputTokens") or 0)
                            m_cr = int(m_val.get("cachedReadTokens") or 0)
                            m_cw = int(m_val.get("cacheCreationTokens") or 0)
                            m_raw_in = max(0, m_prompt - m_cr - m_cw)
                            m_in = m_raw_in + m_cw
                            m_out = int(m_val.get("outputTokens") or 0)
                            m_calls = int(m_val.get("modelCalls") or 1)
                            m_billed = _billed_usd(m_val)
                            m_cost = (
                                m_billed
                                if m_billed is not None
                                else calc_call_cost(m_key, m_raw_in, m_cr, m_out, m_cw, calls=max(1, m_calls), event_ts=ts)
                            )
                            pm = per_model_map.setdefault(
                                m_key,
                                {"model": m_key, "calls": 0, "prompt": 0, "in": 0, "cache": 0, "out": 0, "cost": 0.0},
                            )
                            pm["calls"] += m_calls
                            pm["prompt"] += m_in + m_cr
                            pm["in"] += m_raw_in
                            pm["cache_write"] = pm.get("cache_write", 0) + m_cw
                            pm["cache"] += m_cr
                            pm["out"] += m_out
                            pm["cost"] += m_cost
                            if m_in or m_cr or m_out or m_cost > 0:
                                cost_events.append((round(ts, 3), m_cost, model_quota_family(m_key),
                                                    BILLED_UID if m_billed is not None else "",
                                                    m_in, m_cr, m_out, turn_inc, m_cw, canonical_model_label(m_key)))
                                turn_inc = 0
                    else:
                        pm = per_model_map.setdefault(
                            model,
                            {"model": model, "calls": 0, "prompt": 0, "in": 0, "cache": 0, "out": 0, "cost": 0.0},
                        )
                        pm["calls"] += max(1, t_calls)
                        pm["prompt"] += inp + cr
                        pm["in"] += raw_inp
                        pm["cache_write"] = pm.get("cache_write", 0) + cw
                        pm["cache"] += cr
                        pm["out"] += out
                        pm["cost"] += c_cost
                        if inp or cr or out or c_cost > 0:
                            cost_events.append((round(ts, 3), round(c_cost, 8), model_quota_family(model), "",
                                                inp, cr, out, 1, cw, canonical_model_label(model)))
                    tot_cost += lock_grok_turn(cost_events, first_event, c_cost, billed is not None,
                                                (round(ts, 3), c_cost, model_quota_family(model), "",
                                                 inp, cr, out, 1, cw, canonical_model_label(model)),
                                                detailed=isinstance(mu, dict) and bool(mu), models=per_model_map)
        except Exception:
            pass

    if events_path and events_path.exists():
        try:
            ev_lines = [l for l in log_lines(events_path) if l.strip()]
            boundaries = timed_turns(ev_lines)
            if ev_lines:
                last_ev = json.loads(ev_lines[-1])
                ev_type = str(last_ev.get("type") or "")
                if ev_type == "turn_ended":
                    saw_terminal = True
                    is_done = True
                    outcome = str(last_ev.get("outcome") or "")
                    if outcome in ("error", "failed", "aborted"):
                        is_err = True
                else:
                    is_done = False
        except Exception:
            pass

    if calls == 0 and raw_path and raw_path.exists():
        try:
            for line in log_lines(raw_path):
                try:
                    ro = _log_record(line)
                except Exception:
                    continue
                if ro.get("type") == "usage":
                    u = ro.get("usage") or {}
                    raw_inp = int(u.get("input_tokens") or 0)
                    cr = int(u.get("cache_read_input_tokens") or 0)
                    cw = int(u.get("cache_creation_input_tokens") or 0)
                    inp = raw_inp + cw
                    out = int(u.get("output_tokens") or 0)
                    think = int(u.get("reasoning_tokens") or 0)
                    if inp > 0 or cr > 0 or out > 0:
                        calls += 1
                        tot_cw += cw
                        split_ok &= (not out or u.get("reasoning_tokens") is not None) and think <= out
                        tot_in += inp
                        tot_cache += cr
                        tot_out += out
                        tot_think += think
                        c_cost = calc_call_cost(model, raw_inp, cr, out, cw, event_ts=mtime)
                        tot_cost += c_cost
                        pm = per_model_map.setdefault(
                            model,
                            {"model": model, "calls": 0, "prompt": 0, "in": 0, "cache": 0, "out": 0, "cost": 0.0},
                        )
                        pm["calls"] += 1
                        pm["prompt"] += inp + cr
                        pm["in"] += raw_inp
                        pm["cache_write"] = pm.get("cache_write", 0) + cw
                        pm["cache"] += cr
                        pm["out"] += out
                        pm["cost"] += c_cost
                        if c_cost > 0:
                            cost_events.append((round(mtime, 3), round(c_cost, 8), model_quota_family(model), "",
                                                inp, cr, out, 1, cw, canonical_model_label(model)))
                elif ro.get("type") == "end":
                    saw_terminal = True
                    is_done = True
                    if ro.get("stopReason") == "error":
                        is_err = True
        except Exception:
            pass

    if usage_path is not None and usage_path.exists():
        try:
            usage_obj = json.loads(usage_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            usage_obj = None
        if isinstance(usage_obj, dict) and usage_mtime:
            body = summary_body(usage_obj)
            official_total = _billed_usd(body)
            if official_total is None:
                official_total = _billed_usd(usage_obj)
            old_out = tot_out
            tot_in, tot_cache, tot_out, tot_think, calls, tot_cost = merge_grok_usage(
                usage_obj, model, usage_mtime, (tot_in, tot_cache, tot_out, tot_think, calls),
                tot_cost, turn_count, cost_events, per_model_map,
            )

            tot_cw = sum(row[8] for row in cost_events)
            if tot_out > old_out:
                split_ok &= any(body.get(k) is not None for k in ("reasoningTokens", "reasoning_tokens", "thinkingTokens"))
    if per_model_map:
        _reconcile_costs(tot_cost, [], per_model_map)
    cost_events = reconcile_grok_events(tot_cost, cost_events)
    prompt = tot_in + tot_cache
    hit_pct = (tot_cache * 100.0 / prompt) if prompt > 0 else None
    think, resp = split(tot_out, tot_think if split_ok else None)
    measured = [e[3] == BILLED_UID for e in cost_events]
    source = ("reported" if (official_total is not None and abs(official_total-tot_cost) < 1e-9) or (measured and all(measured))
              else "mixed" if any(measured) else estimate_source(model))
    base_short = short_model_hint(model)
    m_disp = f"{base_short} ({effort})" if effort and f"({effort})" not in base_short else base_short
    res = {
        "model": m_disp,
        "calls": calls,
        "prompt": prompt,
        "in": tot_in - tot_cw, "cache_write": tot_cw,
        "cache": tot_cache,
        "hit_pct": hit_pct,
        "out": tot_out,
        "think": think,
        "resp": resp,
        **grok_perf(timed_usage, boundaries), "cost_source": source,
        "cost": tot_cost,
        "cost_events": cost_events,
        "per_model": list(per_model_map.values()),
        "is_done": is_done,
        "_saw_terminal": saw_terminal,
        "_terminal_done": is_done,
        "is_err": is_err,
        "mtime": mtime,
    }
    return res
