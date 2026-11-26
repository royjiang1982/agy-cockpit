"""Timing and token metrics. Only observations sharing a denominator are pooled."""
import math


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) and n >= 0 else None


def speed(kind, out, dur, calls=0):
    dur = number(dur)
    return dict(tps_kind=kind, tps_out=out if dur else 0, tps_dur=dur or 0,
                tps=out / dur if dur else None, tps_calls=calls if dur else 0)


def db_perf(calls):
    streams = [c for c in calls if c["is_streamed"]]
    first = [c["ttft"] for c in calls if c["ttft"] is not None]
    rate = speed("decode", sum(c["out"] for c in streams), sum(c["stream"] for c in streams), len(streams))
    measured = [c for c in calls if c.get("end_ts", 0) > c.get("start_ts", 0) > 0 and not c.get("is_estimated")]
    request = speed("request", sum(c["out"] for c in measured),
                    sum(c["end_ts"] - c["start_ts"] for c in measured), len(measured))
    return dict(**rate, request_speed=request, ttft_kind="first_token", ttft=sum(first)/len(first) if first else None, ttft_calls=len(first))


def split(out, think):
    if think is None or not 0 <= think <= out:
        return None, None
    return think, out - think


def sum_known(rows, key):
    return None if any(r.get(key) is None for r in rows) else sum(r[key] for r in rows)


def grouped_speeds(rows):
    totals = {}
    for row in rows:
        dur = row.get("tps_dur")
        if not dur or row.get("metrics_unavailable"):
            continue
        group = totals.setdefault(row["tps_kind"], [0, 0., 0])
        group[0] += row["tps_out"]
        group[1] += dur
        group[2] += row.get("tps_calls", 0)
    return {kind: speed(kind, *args) for kind, args in totals.items()}


def proto_split(usage, out):
    # Proto3 omits zero scalar fields: accept their zero defaults only when
    # the two components exactly explain the observed total.
    think, resp = usage.get(10, 0), usage.get(9, 0)
    return (think, resp) if think + resp == out else (None, None)


def grok_remaining(raw):
    used = number(raw)
    return (100 - used) / 100 if used is not None and used <= 100 else None
