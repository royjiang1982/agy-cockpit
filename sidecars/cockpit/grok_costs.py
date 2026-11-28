"""Reconcile Grok's session total without moving known turns to the file mtime."""
from __future__ import annotations

import math

from cost_schema import CostEvent
from metric_items import add_model_usage
from metric_logs import _conserve
from pricing import calc_call_cost, canonical_model_label, model_quota_family

UNPLACED_UID = "grok-summary-unplaced"
BILLED_UID = "grok-turn-billed"
_INPUT = ("inputTokens", "input_tokens")
_CACHE = ("cachedReadTokens", "cached_read_tokens", "cache_read_input_tokens", "cacheReadTokens")
_WRITES = ("cacheCreationTokens", "cache_creation_tokens", "cache_creation_input_tokens", "cacheWriteTokens")
_OUTPUT = ("outputTokens", "output_tokens")


def _pick(record, *names):
    for key in names:
        try:
            return max(0, int(record[key]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return 0


def _usage(record):
    prompt = _pick(record, *_INPUT)
    cache = _pick(record, *_CACHE)
    writes = _pick(record, *_WRITES)
    return (max(0, prompt - cache - writes), cache, writes, _pick(record, *_OUTPUT),
            _pick(record, "reasoningTokens", "reasoning_tokens", "thinkingTokens"),
            _pick(record, "modelCalls", "model_calls", "numTurns", "num_turns"))


def _billed_usd(record):
    ticks = _pick(record, "costUsdTicks", "cost_usd_ticks")
    if ticks:
        return ticks / 1e10
    for key in ("costUsd", "cost_usd"):
        try:
            value = float(record[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= 0:
            return value
    return None


def _append_unplaced(events, models, model, ts, amount, inp, cache, out, calls, turns, writes=0):
    label = canonical_model_label(model) if model else ""
    events.append(CostEvent(round(ts, 3), amount, model_quota_family(model) if model else "grok", UNPLACED_UID,
                   inp, cache, out, turns, writes, label))
    _add_model_usage(models, model, amount, inp, cache, out, calls, writes)


def _add_model_usage(models, model, amount, inp, cache, out, calls, writes):
    add_model_usage(models, model or "Unknown", calls=calls, prompt=inp + cache, input_tokens=inp - writes,
                    cache=cache, out=out, cost=amount, cache_write=writes)


def _measured_cost(events):
    return sum(event[1] for event in events if event[3] == BILLED_UID)


def reconcile_grok_events(total, events):
    """Allocate only the remainder to events without a measured amount."""
    measured = _measured_cost(events)
    adjustable = [e for e in events if e[3] != BILLED_UID]
    if not adjustable:
        return list(events)
    remaining = max(0.0, total - measured)
    amounts = _conserve(remaining, [e[1] for e in adjustable])
    amounts[-1] = max(0.0, amounts[-1])
    assigned = iter(amounts)
    return [e if e[3] == BILLED_UID else CostEvent(e[0], next(assigned), *e[2:]) for e in events]


def _event_usage(row):
    cw = row[8] if len(row) > 8 else 0
    return row[4] - cw, row[5], cw, row[6]


def _sum_cols(rows, n):
    sums = [0] * n
    for row in rows:
        for i, value in enumerate(row):
            sums[i] += value
    return sums


def _subtract(total, observed):
    return [max(0, x - y) for x, y in zip(total, observed)]


def lock_grok_turn(events, start, cost, ticks, fallback, detailed=False, models=None):
    """Protect model ticks and, when available, the total billed for this turn."""
    rows = events[start:]
    if detailed:
        raw, cache, cw, out = _subtract(_event_usage(fallback), _sum_cols(map(_event_usage, rows), 4))
        residual = max(0.0, cost - sum(r[1] for r in rows))
        if raw or cache or cw or out or residual > 1e-12:
            turns = max(0, fallback[7] - sum(row[7] for row in rows))
            rows.append(CostEvent(fallback[0], residual, "grok", "", raw + cw, cache, out, turns, cw, ""))
            events[start:] = rows
            if models is not None:
                _add_model_usage(models, "", residual, raw + cw, cache, out, 0, cw)
    if not ticks:
        return max(cost, sum(e[1] for e in rows)) if _measured_cost(rows) else cost
    measured = _measured_cost(rows)
    target = max(cost, measured)
    if not rows:
        rows = [fallback]
    elif all(e[3] == BILLED_UID for e in rows) and target > measured:
        rows.append(CostEvent(*fallback[:3], "", 0, 0, 0, 0, 0, fallback[9]))
    rows = reconcile_grok_events(target, rows)
    events[start:] = [CostEvent(*e[:3], BILLED_UID, *e[4:]) for e in rows]
    return target


def summary_body(summary):
    return next((summary[k] for k in ("session", "usage") if isinstance(summary.get(k), dict)), summary)


def _named_remainders(model_usage, events, models):
    """Subtract only usage from the same canonical model."""
    reported = {}
    for name, item in model_usage.items():
        if not isinstance(item, dict) or not name.strip():
            continue
        label = canonical_model_label(name)
        usage = _usage(item)
        amount = _billed_usd(item)
        cell = reported.setdefault(label, (name, [0] * 6, True))
        for i, value in enumerate((*usage[:4], usage[5], amount or 0.0)):
            cell[1][i] += value
        if amount is None:
            reported[label] = (*cell[:2], False)
    placed = {label: [0] * 6 for label in reported}
    for row in events:
        if len(row) > 9 and row[9] in placed:
            for i, value in enumerate((*_event_usage(row), 0, row[1])):
                placed[row[9]][i] += value
    for model in models.values():
        label = canonical_model_label(model["model"])
        if label in placed:
            placed[label][4] += model["calls"]
    for label, (name, totals, billed) in reported.items():
        raw, cache, cw, out, calls, amount = _subtract(totals, placed[label])
        if raw or cache or cw or out or calls or amount > 1e-12:
            yield name, (raw, cache, cw, out), calls, amount, billed


def _estimate(model, usage, calls, mtime):
    raw, cache, cw, out = usage
    return calc_call_cost(model, raw, cache, out, cw, calls=max(1, calls), event_ts=mtime)


def _allocate_summary_cost(events, named, official, default_model, unassigned, calls, mtime):
    """Keep known bills first; a monetary surplus has no inferred model."""
    if official is None:
        return [item[3] for item in named], _estimate(default_model, unassigned, calls, mtime)
    budget = max(0.0, official - _measured_cost(events))
    amounts = [0.0] * len(named)
    excess = max(0.0, sum(amount for _, _, _, amount, billed in named if billed) - budget)
    for i, (_, _, _, amount, billed) in enumerate(named):
        if billed:
            amounts[i] = max(0.0, amount - excess)
            budget = max(0.0, budget - amounts[i])
    adjustable = [(i, row[1]) for i, row in enumerate(events) if row[3] != BILLED_UID]
    estimate = sum(amount for _, amount in adjustable) + sum(item[3] for item in named if not item[4])
    ratio = min(1.0, budget / estimate) if estimate > 0 else 1.0
    for i, amount in adjustable:
        row = events[i]
        events[i] = CostEvent(row[0], amount * ratio, *row[2:])
    for i, (_, _, _, amount, billed) in enumerate(named):
        if not billed:
            amounts[i] = amount * ratio
    return amounts, max(0.0, budget - estimate)


def _summary_usage_budget(body, observed, requested):
    def total(keys, index):
        if any(body.get(key) is not None for key in keys):
            return _pick(body, *keys)
        return observed[index] + requested[index]

    cache, writes, out = total(_CACHE, 1), total(_WRITES, 2), total(_OUTPUT, 3)
    raw = (max(0, _pick(body, *_INPUT) - cache - writes)
           if any(body.get(key) is not None for key in _INPUT) else observed[0] + requested[0])
    return _subtract((raw, cache, writes, out), observed)


def merge_grok_usage(summary, model, mtime, counts, cost, turn_count, events, models):
    """Keep measured amounts; use the usage file's mtime only for unplaced spend."""
    body = summary_body(summary)
    inp, cache, out, think, calls = counts
    _, _, _, _, u_think, u_calls = _usage(body)
    official = _billed_usd(body)
    if official is None and body is not summary:
        official = _billed_usd(summary)
    measured = _measured_cost(events)
    if official is not None and official + 1e-9 < measured:
        official = None  # An older/incomplete summary cannot replace observed billed usage.
    turn_list = summary.get("turns")
    u_turns = (_pick(body, "turnCount", "numTurns", "turn_count", "num_turns") or
               _pick(summary, "turnCount", "turn_count") or (len(turn_list) if isinstance(turn_list, list) else 0))
    model_usage = (body.get("modelUsage") or body.get("model_usage") or
                   summary.get("modelUsage") or summary.get("model_usage"))
    needed = list(_named_remainders(model_usage, events, models)) if isinstance(model_usage, dict) else []
    current_writes = sum(_event_usage(row)[2] for row in events)
    observed = (inp - current_writes, cache, current_writes, out)
    requested = _sum_cols((item[1] for item in needed), 4)
    budget = _summary_usage_budget(body, observed, requested)
    # When placed but unnamed usage overlaps the model totals, only assign the
    # portion of each remainder that cannot already belong to those dated rows.
    excess = _subtract(requested, budget)
    named = []
    for name, usage, m_calls, amount, billed in needed:
        safe_usage = _subtract(usage, excess)
        named.append((name, safe_usage, m_calls, amount if billed else _estimate(name, safe_usage, m_calls, mtime), billed))
    named_usage = _sum_cols((item[1] for item in named), 4)
    unknown = _subtract(budget, named_usage)
    named_calls = sum(item[2] for item in named)
    unknown_calls = max(0, u_calls - calls - named_calls)
    if not events and not (named or any(unknown) or official):
        return (*counts, cost)
    turns = max(0, u_turns - (turn_count or sum(row[7] for row in events))) if events else max(1, u_turns or u_calls)
    amounts, remainder = _allocate_summary_cost(events, named, official, model, unknown, unknown_calls, mtime)
    for (name, (m_raw, m_cache, m_cw, m_out), m_calls, _, _), amount in zip(named, amounts):
        if not (m_raw or m_cache or m_cw or m_out or m_calls or amount):
            continue
        take_turns = turns if (m_raw or m_cache or m_cw or m_out or m_calls) else 0
        _append_unplaced(events, models, name, mtime, amount, m_raw + m_cw, m_cache, m_out, m_calls, take_turns, m_cw)
        turns -= take_turns
    u_raw, u_cache, u_cw, u_out = unknown
    if any(unknown) or remainder > 1e-12 or turns or unknown_calls:
        _append_unplaced(events, models, "", mtime, remainder, u_raw + u_cw, u_cache, u_out, unknown_calls, turns, u_cw)
    return (inp + named_usage[0] + named_usage[2] + u_raw + u_cw, cache + named_usage[1] + u_cache,
            out + named_usage[3] + u_out, max(think, u_think), max(1, calls + named_calls + unknown_calls),
            max(official, measured) if official is not None else sum(row[1] for row in events))
