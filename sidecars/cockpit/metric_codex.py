"""Read codex usage and performance from offline session records."""
from __future__ import annotations
import json
from cockpit_io import finite_number, log_lines, parse_iso_ts
from cost_schema import CostEvent
from metric_items import add_model_usage
from pricing import calc_call_cost, canonical_model_label, estimate_source, model_quota_family, short_model_hint
from metric_rates import speed, split
from metric_logs import _log_record


def read_codex(rollout, mtime):
    model = "gpt-6.1-sol"
    effort = ""
    tot_in = 0
    tot_cache = 0
    tot_out = 0
    tot_think = 0
    tot_cw = 0
    tot_cost = 0.0
    cost_events: list[CostEvent] = []
    per_model_map: dict[str, dict] = {}
    legacy_tc_list: list[tuple[float, str, str, dict, dict, int, int]] = []
    last_legacy_tot_sig: tuple[int, int, int] | None = None
    calls = 0
    gen_calls = 0
    first_tokens = []
    split_ok = True
    gen_out = 0
    gen_dur = 0.0
    last_req_ts: float | None = None
    last_task_event = ""
    saw_modern = False
    task_serial = 0
    event_positions: list[tuple[int, int]] = []

    def _add_pm(m_raw: str, eff: str, c_p: int, c_i: int, c_c: int, c_o: int, c_cost: float) -> str:
        m_key = f"{m_raw} ({eff})" if eff else m_raw
        add_model_usage(per_model_map, m_key, calls=1, prompt=c_p, input_tokens=c_i,
                        cache=c_c, out=c_o, cost=c_cost, cache_write=max(0, c_p - c_i - c_c))
        return canonical_model_label(m_key)

    try:
        for record_index, line in enumerate(log_lines(rollout)):
            try:
                obj = _log_record(line)
            except Exception:
                continue
            ts = parse_iso_ts(obj.get("timestamp"))
            typ = obj.get("type")
            pl = obj.get("payload") or {}
            if typ == "turn_context":
                if pl.get("model"):
                    model = str(pl["model"])
                if pl.get("effort"):
                    effort = str(pl["effort"])
                if ts and (last_req_ts is None or ts >= last_req_ts):
                    last_req_ts = ts
            elif typ == "event_msg":
                ev_type = str(pl.get("type") or "")
                if ev_type == "task_started":
                    task_serial += 1
                    last_req_ts = ts
                    last_task_event = "task_started"
                elif ev_type in ("task_complete", "turn_aborted", "error"):
                    last_task_event = ev_type
                    ms = finite_number(pl.get("time_to_first_token_ms"), non_negative=True)
                    if ms is not None:
                        first_tokens.append(ms / 1000)
                elif ev_type == "token_count":
                    info = pl.get("info") or {}
                    tu_leg = info.get("total_token_usage") or {}
                    lu_leg = info.get("last_token_usage") or {}
                    sig = (
                        int(tu_leg.get("input_tokens") or 0),
                        int(tu_leg.get("cached_input_tokens") or 0),
                        int(tu_leg.get("output_tokens") or 0),
                    )
                    if not saw_modern and any(sig) and sig != last_legacy_tot_sig:
                        last_legacy_tot_sig = sig
                        legacy_tc_list.append((ts or mtime, model, effort, tu_leg, lu_leg, task_serial, record_index))
            elif typ == "response_item" and pl.get("type") in (
                "custom_tool_call_output",
                "function_call_output",
            ):
                last_req_ts = ts
            elif typ == "token_usage_record":
                saw_modern = True
                calls += 1
                u = pl.get("usage") or {}
                c_prompt = int(u.get("input_tokens") or 0)
                c_cache = int(u.get("cached_input_tokens") or 0)
                c_cw = int(u.get("cache_write_input_tokens") or 0)
                c_in = max(0, c_prompt - c_cache - c_cw)
                c_out = int(u.get("output_tokens") or 0)
                c_think = int(u.get("reasoning_output_tokens") or 0)
                tot_in += c_prompt
                tot_cw += c_cw
                tot_cache += c_cache
                tot_out += c_out
                split_ok &= (not c_out or u.get("reasoning_output_tokens") is not None) and 0 <= c_think <= c_out
                tot_think += c_think
                if c_prompt > 0 or c_out > 0:
                    c_cost = calc_call_cost(model, c_in, c_cache, c_out, c_cw, event_ts=ts or mtime)
                    tot_cost += c_cost
                    label = _add_pm(model, effort, c_prompt, c_in, c_cache, c_out, c_cost)
                    if c_cost > 0:
                        cost_events.append(CostEvent(round(ts or mtime, 3), round(c_cost, 8), model_quota_family(model), "",
                                            c_in + c_cw, c_cache, c_out, 0, c_cw, label))
                        event_positions.append((record_index, task_serial))
                if ts and last_req_ts and ts > last_req_ts and c_out > 0:
                    gen_calls += 1
                    gen_out += c_out
                    gen_dur += ts - last_req_ts
                    last_req_ts = ts
    except Exception:
        return None

    if legacy_tc_list:
        for l_ts, l_mod, l_eff, tu_leg, lu_leg, l_task, l_index in legacy_tc_list:
            calls += 1
            c_prompt = int(lu_leg.get("input_tokens") or 0)
            c_cache = int(lu_leg.get("cached_input_tokens") or 0)
            c_cw = int(lu_leg.get("cache_write_input_tokens") or 0)
            c_in = max(0, c_prompt - c_cache - c_cw)
            c_out = int(lu_leg.get("output_tokens") or 0)
            tot_in += c_prompt
            tot_cw += c_cw
            tot_cache += c_cache
            tot_out += c_out
            leg_think = lu_leg.get("reasoning_output_tokens")
            split_ok &= (not c_out or leg_think is not None) and 0 <= int(leg_think or 0) <= c_out
            tot_think += int(leg_think or 0)
            if c_prompt > 0 or c_out > 0:
                c_cost = calc_call_cost(l_mod, c_in, c_cache, c_out, c_cw, event_ts=l_ts or mtime)
                tot_cost += c_cost
                label = _add_pm(l_mod, l_eff, c_prompt, c_in, c_cache, c_out, c_cost)
                if c_cost > 0:
                    cost_events.append(CostEvent(round(l_ts, 3), round(c_cost, 8), model_quota_family(l_mod), "",
                                        c_in + c_cw, c_cache, c_out, 0, c_cw, label))
                    event_positions.append((l_index, l_task))

    # Legacy records are priced after the scan, so use their original file order
    # to select the first metered call of each task across both formats.
    first_by_task = {}
    for index, task in sorted(event_positions):
        first_by_task.setdefault(task, index)
    cost_events = [event._replace(turns=int(index == first_by_task[task]))
                   for event, (index, task) in zip(cost_events, event_positions)]
    uncached = max(0, tot_in - tot_cache - tot_cw)
    if tot_cost <= 0.0 and (uncached > 0 or tot_cache > 0 or tot_out > 0):
        tot_cost = calc_call_cost(model, uncached, tot_cache, tot_out, tot_cw, calls=max(1, calls), event_ts=mtime)
        if tot_cost > 0:
            label = _add_pm(model, effort, tot_in, uncached, tot_cache, tot_out, tot_cost)
            cost_events = [CostEvent(round(mtime, 3), round(tot_cost, 8), model_quota_family(model), "",
                            uncached + tot_cw, tot_cache, tot_out, max(1, calls), tot_cw, label)]
    hit_pct = (tot_cache * 100.0 / tot_in) if tot_in > 0 else None
    think, resp = split(tot_out, tot_think if split_ok else None)
    m_disp = f"{model} ({effort})" if effort else model
    res = {
        "model": m_disp,
        "calls": calls,
        "prompt": tot_in,
        "in": uncached,
        "cache_write": tot_cw,
        "cache": tot_cache,
        "hit_pct": hit_pct,
        "out": tot_out,
        "think": think,
        "resp": resp,
        **speed("request", gen_out, gen_dur, gen_calls),
        "ttft": sum(first_tokens)/len(first_tokens) if first_tokens else None, "ttft_calls": len(first_tokens), "ttft_kind": "turn",
        "cost_source": "unmatched_estimate" if any(estimate_source(pm["model"]) == "unmatched_estimate" for pm in per_model_map.values()) else estimate_source(model),
        "cost": tot_cost,
        "cost_events": cost_events,
        "per_model": list(per_model_map.values()),
        "last_task_event": last_task_event,
        "mtime": mtime,
    }
    return res
