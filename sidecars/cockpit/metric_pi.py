"""Read pi usage and performance from offline session records."""
from __future__ import annotations
from analyzer import CostEvent, log_lines
from pricing import calc_call_cost, canonical_model_label, estimate_source, model_quota_family, short_model_hint
from metric_rates import number, speed, split
from metric_logs import _log_record, parse_iso_ts

PI_MTIME_UID = "pi-file-mtime"


def read_pi(sess_path, mtime):
    model = "claude-opus-5-5"
    effort = ""
    tot_in = 0
    tot_cache = 0
    tot_out = 0
    tot_think = 0
    tot_cost = 0.0
    cost_events: list[CostEvent] = []
    per_model_map: dict[str, dict] = {}
    calls = 0
    gen_calls = tot_cw = 0
    sources = set()
    split_ok = True
    gen_out = 0
    gen_dur = 0.0
    last_req_ts: float | None = None
    last_stop = ""
    new_turn = True

    try:
        for line in log_lines(sess_path):
            try:
                obj = _log_record(line)
            except Exception:
                continue
            ts = parse_iso_ts(obj.get("timestamp"))
            typ = obj.get("type")
            if typ == "model_change" and obj.get("modelId"):
                model = str(obj["modelId"])
            elif typ == "thinking_level_change" and obj.get("thinkingLevel"):
                effort = str(obj["thinkingLevel"])
            elif typ == "message":
                msg = obj.get("message") or {}
                role = str(msg.get("role") or "")
                if role == "user":
                    new_turn = True
                if role in ("user", "toolResult", "system"):
                    last_req_ts = ts
                elif role == "assistant":
                    last_stop = str(msg.get("stopReason") or "")
                    failed = last_stop in ("error", "aborted") or bool(msg.get("errorMessage"))
                    if failed:
                        last_req_ts = ts
                    if msg.get("model"):
                        model = str(msg["model"])
                    u = msg.get("usage") or {}
                    raw_inp = int(u.get("input") or 0)
                    cw = int(u.get("cacheWrite") or 0)
                    inp = raw_inp + cw
                    cache = int(u.get("cacheRead") or 0)
                    out = int(u.get("output") or 0)
                    think = int(u.get("reasoning") or 0)
                    if inp > 0 or cache > 0 or out > 0:
                        calls += 1
                        tot_in += raw_inp
                        tot_cw += cw
                        split_ok &= (out == 0 or u.get("reasoning") is not None) and 0 <= think <= out
                        tot_cache += cache
                        tot_out += out
                        tot_think += think
                        reported = u.get("cost")
                        c_cost = number(reported.get("total") if isinstance(reported, dict) else reported)
                        source = "reported" if c_cost is not None else estimate_source(model)
                        sources.add(source)
                        if c_cost is None:
                            c_cost = calc_call_cost(model, raw_inp, cache, out, cw, event_ts=ts or mtime)
                        tot_cost += c_cost
                        pm = per_model_map.setdefault(
                            model,
                            {"model": model, "calls": 0, "prompt": 0, "in": 0, "cache": 0, "out": 0, "cost": 0.0},
                        )
                        pm["calls"] += 1
                        pm["prompt"] += inp + cache
                        pm["in"] += raw_inp
                        pm["cache_write"] = pm.get("cache_write", 0) + cw
                        pm["cost_source"] = source if pm.get("cost_source", source) == source else "mixed"
                        pm["cache"] += cache
                        pm["out"] += out
                        pm["cost"] += c_cost
                        cost_events.append((round(ts or mtime, 3), c_cost, model_quota_family(model), "" if ts else PI_MTIME_UID,
                                            inp, cache, out, int(new_turn), cw, canonical_model_label(model)))
                        new_turn = False
                        seconds = (number(msg.get("durationMs")) or 0) / 1000
                        if not seconds and last_req_ts and ts and ts > last_req_ts:
                            seconds = ts - last_req_ts
                        if not failed and seconds > 0 and out > 0:
                            gen_out += out
                            gen_calls += 1
                            gen_dur += seconds
                        last_req_ts = ts
    except Exception:
        return None

    prompt = tot_in + tot_cw + tot_cache
    hit_pct = tot_cache * 100.0 / prompt if prompt else None
    think, resp = split(tot_out, tot_think if split_ok else None)
    base_short = short_model_hint(model)
    m_disp = f"{base_short} ({effort})" if effort and f"({effort})" not in base_short else base_short
    res = {
        "model": m_disp,
        "calls": calls,
        "prompt": prompt,
        "in": tot_in,
        "cache": tot_cache,
        "hit_pct": hit_pct,
        "out": tot_out,
        "think": think, "cache_write": tot_cw,
        "resp": resp,
        **speed("request", gen_out, gen_dur, gen_calls),
        "cost_source": next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else None),
        "cost": tot_cost,
        "cost_events": cost_events,
        "per_model": list(per_model_map.values()),
        "last_stop": last_stop,
        "mtime": mtime,
    }
    return res
