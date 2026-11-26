"""Shared session-log readers and conservative allocation helpers."""
from __future__ import annotations
import json
from datetime import datetime

def _conserve(total, weights):
    if not weights:
        return []
    denom = sum(weights)
    values = [total * (weight / denom if denom else 1 / len(weights)) for weight in weights[:-1]]
    values.append(total - sum(values))
    return values


def _reconcile_costs(total, events, models):
    event_amounts = _conserve(total, [max(0.0, event[1]) for event in events])
    events = [(event[0], amount, *event[2:]) for event, amount in zip(events, event_amounts)]
    amounts = _conserve(total, [max(0.0, item['cost']) for item in models.values()])
    for item, amount in zip(models.values(), amounts):
        item['cost'] = amount
    return events


def _log_record(line):
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError('log record must be an object')
    numeric = {'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
               'reasoning_output_tokens', 'inputTokens', 'cachedReadTokens', 'cacheCreationTokens',
               'outputTokens', 'reasoningTokens', 'modelCalls', 'numTurns', 'costUsdTicks', 'apiDurationMs'}
    def validate(value, depth=0):
        if depth > 12:
            raise ValueError('log record nesting too deep')
        if isinstance(value, dict):
            for key, item in value.items():
                if key in numeric and item is not None:
                    if float(item) < 0:
                        raise ValueError('negative usage')
                    int(item)
                if isinstance(item, (dict, list)):
                    validate(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    validate(item, depth + 1)
    validate(obj)
    return obj


def parse_iso_ts(ts_str: str | None) -> float | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


