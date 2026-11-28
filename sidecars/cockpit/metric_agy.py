"""AGY metrics fallback for subagents without a conversation database."""
from __future__ import annotations

import json
from pathlib import Path

from agy_cost_events import step_update_time, transcript_model
from analyzer import _BUILTIN_SUBAGENT_MODEL_ENUM
from cockpit_io import SUBAGENT_APP_DIRS, BoundedCache, log_lines, parse_iso_ts, serialized
from cost_schema import CostEvent
from metric_cache import _live_metrics, _publish_metrics
from metric_items import add_model_usage
from metric_rates import speed
from pricing import MODEL_MAP, canonical_model_label, calc_call_cost, estimate_source, model_quota_family, short_model_hint

_AGY_TRANSCRIPT_CACHE = BoundedCache(128)


@serialized(lambda first, *args, **kwargs: str(first))
def analyze_agy_transcript_fallback(sub_or_path: str | Path, model_hint: str = "") -> dict | None:
    """Fallback analyzer when an `agy` subagent saved `transcript.jsonl` without `.db`."""
    candidates: list[Path] = []
    if isinstance(sub_or_path, Path) or (
        isinstance(sub_or_path, str) and (sub_or_path.endswith(".jsonl") or "/" in sub_or_path)
    ):
        cand = Path(sub_or_path)
        if cand.exists():
            candidates.append(cand)
    else:
        sub_id = str(sub_or_path)
        for _, base in SUBAGENT_APP_DIRS:
            tf = base / "brain" / sub_id / ".system_generated" / "logs" / "transcript_full.jsonl"
            t = base / "brain" / sub_id / ".system_generated" / "logs" / "transcript.jsonl"
            src = tf if tf.exists() else (t if t.exists() else None)
            if src is not None:
                candidates.append(src)

    for src in candidates:
        try:
            mtime = src.stat().st_mtime
        except Exception:
            continue
        eff_hint = model_hint
        if not eff_hint:
            cand_cid = str(sub_or_path) if not ("/" in str(sub_or_path)) else ""
            if not cand_cid and len(src.parents) >= 3 and src.parents[1].name == ".system_generated":
                cand_cid = src.parents[2].name
            b_enum = _BUILTIN_SUBAGENT_MODEL_ENUM.get(cand_cid)
            if b_enum and b_enum in MODEL_MAP:
                eff_hint = MODEL_MAP[b_enum][0]
        cache_key = f"{src}:{eff_hint}"
        cached = _AGY_TRANSCRIPT_CACHE.get(cache_key)
        if cached and cached[0] == mtime:
            return _live_metrics(cached[1])
        tot_in = 0
        tot_cache = 0
        tot_out = 0
        calls = 0
        gen_out = 0
        gen_dur = 0.0
        timed_steps = 0
        objs: list[dict] = []
        try:
            for line in log_lines(src):
                try:
                    objs.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue
        if not objs:
            continue

        model = eff_hint
        tot_cost = 0.0
        cost_events: list[CostEvent] = []
        per_model_map: dict[str, dict] = {}

        new_turn = True
        for i, obj in enumerate(objs):
            model = transcript_model(obj) or model
            if obj.get("type") == "USER_INPUT":
                new_turn = True
            inp = 0
            cache = 0
            out = 0
            t_start = 0.0
            s_idx = 0
            dur_sec = 0.0

            if obj.get("type") == "PLANNER_RESPONSE":
                inp = int(obj.get("input_tokens") or 0)
                cache = int(obj.get("cache_read_tokens") or 0)
                out = int(obj.get("output_tokens") or 0)
                t_start = parse_iso_ts(obj.get("created_at"))
                s_idx = int(obj.get("step_index") or 0)
            elif obj.get("event") == "step_update":
                su = obj.get("step_update") or {}
                if su.get("step_type") == "user_input":
                    new_turn = True
                usage = su.get("usage") or {}
                inp = int(usage.get("input_tokens") or 0)
                cache = int(usage.get("cache_read_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                s_idx = int(su.get("step_index") or 0)
                dur_sec = float(su.get("duration_seconds") or 0.0)
                t_start = step_update_time(obj, su) or mtime

            if inp > 0 or cache > 0 or out > 0:
                calls += 1
                tot_in += inp
                tot_cache += cache
                tot_out += out
                s_ts = float(t_start or mtime)
                c_cost = calc_call_cost(model, inp, cache, out, event_ts=s_ts)
                tot_cost += c_cost
                m_label = short_model_hint(model) or "Unknown"
                add_model_usage(per_model_map, m_label, calls=1, prompt=inp + cache,
                                input_tokens=inp, cache=cache, out=out, cost=c_cost)
                if c_cost > 0:
                    uid = f"{s_idx}:{inp}:{cache}:{out}:{int(s_ts)}"
                    fam = model_quota_family(model)
                    cost_events.append(CostEvent(round(s_ts, 3), round(c_cost, 8), fam, uid, inp, cache, out, int(new_turn),
                                        0, canonical_model_label(m_label)))
                    new_turn = False
                if dur_sec > 0 and out > 0:
                    gen_out += out
                    timed_steps += 1
                    gen_dur += dur_sec
                elif obj.get("type") == "PLANNER_RESPONSE":
                    t_end: float | None = None
                    if t_start:
                        for j in range(i + 1, len(objs)):
                            t_next = parse_iso_ts(objs[j].get("created_at"))
                            if t_next and t_next >= t_start:
                                t_end = t_next
                                break
                        if out > 0 and t_end and t_end > t_start:
                            gen_out += out
                            timed_steps += 1
                            gen_dur += t_end - t_start

        last_obj = objs[-1]
        last_obj_ts = parse_iso_ts(last_obj.get("created_at")) or (mtime if last_obj.get("event") else 0.0)
        is_idle = bool(
            (last_obj.get("type") == "PLANNER_RESPONSE" and last_obj.get("status") == "DONE")
            or (last_obj.get("event") == "result")
        )
        prompt = tot_in + tot_cache
        hit_pct = (tot_cache * 100.0 / prompt) if prompt > 0 else None
        res = {
            "model": short_model_hint(model) or "Unknown",
            "calls": calls,
            "prompt": prompt,
            "in": tot_in,
            "cache": tot_cache,
            "hit_pct": hit_pct,
            "out": tot_out,
            "think": None, "resp": None, "cache_write": 0,
            **speed("step_est", gen_out, gen_dur, timed_steps), "cost_source": estimate_source(model),
            "cost": tot_cost,
            "cost_events": cost_events,
            "per_model": list(per_model_map.values()),
            "is_idle": is_idle,
            "last_ts": last_obj_ts,
        }
        return _publish_metrics(_AGY_TRANSCRIPT_CACHE, cache_key, mtime, res)
    return None
