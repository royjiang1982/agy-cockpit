"""Pair Grok request boundaries within loops; join complete turns to measured usage."""
import json
from metric_logs import parse_iso_ts
from metric_rates import speed


def timed_turns(lines):
    turns, waits, streams = [], [], []
    waiting = first = tool = None
    active = valid = False
    for line in lines:
        try:
            ev = json.loads(line)
            kind, phase, ts = ev["type"], ev.get("phase"), parse_iso_ts(ev.get("ts"))
        except (TypeError, KeyError, ValueError):
            valid = False
            continue
        if ts is None:
            valid = False
            continue
        if kind == "turn_started":
            active = valid = True
            waits, streams = [], []
            waiting = first = tool = None
        if not active:
            continue
        if first is not None and (kind in ("loop_started", "turn_ended") or phase == "tool_execution"):
            end = ts if phase == "tool_execution" or tool is None else tool
            if end > first and (kind != "loop_started" or tool is not None):
                streams.append(end - first)
            else:
                valid = False
            first = tool = None
        if kind == "loop_started":
            if waiting is not None:
                valid = False
            waiting = None
        elif phase == "waiting_for_model":
            if first is not None or waiting is not None:
                valid = False
            waiting = ts
        elif kind == "first_token":
            if waiting is not None and ts >= waiting:
                waits.append(ts - waiting)
                first = ts
            else:
                valid = False
            waiting = None
        elif kind == "tool_started" and first is not None and tool is None:
            tool = ts
        elif kind == "turn_ended":
            turns.append((ts, waits, streams, valid and waiting is None))
            active = False
    return turns


def grok_perf(usage_rows, turns):
    # events and updates use separate clocks/records. Reject ambiguous matches
    # in either direction; never divide a whole turn's output by a partial loop set.
    totals = {k: [0, 0., 0] for k in ("decode", "request", "turn")}
    waits = []
    candidates = [[i for i, (end, *_) in enumerate(turns) if abs(end-ts) <= 1]
                  for ts, *_ in usage_rows]
    for (_, out, calls, api, elapsed), ids in zip(usage_rows, candidates):
        if api or elapsed:
            bucket = totals["request" if api else "turn"]
            bucket[0] += out
            bucket[1] += (api or elapsed) / 1000
            bucket[2] += calls
        if len(ids) != 1 or sum(ids[0] in other for other in candidates) != 1:
            continue
        _, firsts, streams, valid = turns[ids[0]]
        waits.extend(firsts)
        if valid and calls == len(firsts) == len(streams) and calls:
            totals["decode"][0] += out
            totals["decode"][1] += sum(streams)
            totals["decode"][2] += calls
    speeds = {k: speed(k, *v) for k, v in totals.items() if v[1]}
    return dict(**next(iter(speeds.values()), speed("request", 0, 0)), speeds=speeds,
                ttft=sum(waits)/len(waits) if waits else None, ttft_calls=len(waits), ttft_kind="first_token")
