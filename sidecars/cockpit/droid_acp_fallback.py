"""Offline ACP JSONL metadata and usage-free Droid fallback metrics.

Factory settings remain authoritative for usage; ACP supplies identity and terminal
state only. Partial/malformed lines are ignored while a stream is being written.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pricing import short_model_hint

ENGINES = frozenset(('agy', 'codex', 'claude', 'pi', 'droid', 'grok'))
UUID = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')


def parse_acp(text: str) -> dict:
    """Reduce stream events in order, allowing result ERROR to beat turn.completed."""
    meta = {'calls': 0, 'recognized': False, 'terminal': False, 'is_err': False}
    # JSONL is LF-delimited; U+2028/U+2029 may appear literally inside JSON strings.
    for line in text.split('\n'):
        try:
            obj = json.loads(line.rstrip('\r'))
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        event = obj.get('event') or obj.get('type')
        if event not in ('quota_fallback', 'init', 'result', 'turn.completed', 'turn.failed', 'thread.started'):
            continue
        if event == 'thread.started':
            meta['agent'] = 'codex'
            tid = obj.get('thread_id')
            if isinstance(tid, str) and UUID.fullmatch(tid):
                meta['id'] = tid
            continue
        meta['recognized'] = True
        fallback = event == 'quota_fallback'
        agent = obj.get('to_agent') if fallback else obj.get('agent')
        if agent in ENGINES:
            meta['agent'] = agent
        for target, keys in (
            ('model', ('to_model',) if fallback else ('model', 'modelId')),
            ('effort', ('to_effort',) if fallback else ('reasoningEffort', 'effort')),
        ):
            value = next((obj[k] for k in keys if obj.get(k)), None)
            if isinstance(value, str) and value != 'acp':
                meta[target] = value
        sid = obj.get('conversation_id') or obj.get('session_id')
        if isinstance(sid, str) and UUID.fullmatch(sid):
            meta['id'] = sid
        if event in ('turn.completed', 'turn.failed'):
            meta['calls'] += 1
            meta['terminal'] = True
            meta['is_err'] |= event == 'turn.failed'
        if event == 'result':
            result = obj.get('result')
            result = result if isinstance(result, dict) else obj
            status = str(result.get('status') or '').upper()
            if status in ('SUCCESS', 'ERROR', 'FAILED', 'ABORTED'):
                meta['terminal'] = True
                meta['is_err'] |= status != 'SUCCESS'
    return meta


def discovery_hints(text, prog, sub_id, model, effort, parent_id):
    """Return actual engine/identity before legacy engine-specific dispatch."""
    meta = parse_acp(text)
    prog = meta.get('agent', prog)
    sid = meta.get('id')
    if sid and sid != parent_id and (not sub_id or prog == 'droid'):
        sub_id = sid
    return prog, sub_id, meta.get('model', model), meta.get('effort', effort)


def _read(path):
    try:
        return parse_acp(Path(path).read_bytes().decode('utf-8', errors='replace')) if path else {}
    except OSError:
        return {}


def supplement_acp(path, metrics):
    """Overlay ACP state without replacing measured Factory usage or real models."""
    meta = _read(path)
    if not meta.get('recognized'):
        return metrics
    metrics['calls'] = max(metrics['calls'], meta.get('calls', 0), 1)
    if meta.get('terminal'):
        metrics.update(is_done=True, _saw_terminal=True, _terminal_done=True)
    metrics['is_err'] |= meta.get('is_err', False)
    if metrics['model'].split(' (', 1)[0] in ('', 'auto', 'droid') and meta.get('model'):
        model = meta['model']
        effort = meta.get('effort', '')
        metrics['model'] = short_model_hint(model) + (f' ({effort})' if effort else '')
        for item in metrics.get('per_model', []):
            if item['model'] in ('', 'auto', 'droid'):
                item['model'] = model
    return metrics


def acp_metrics(path):
    """Build the analyzer contract when ACP has no usage or Factory settings.

    Zero usage is unmeasured, not a claim of free execution: cost is estimated,
    usage_available is false, and no synthetic cost events are emitted.
    """
    meta = _read(path)
    if not meta.get('recognized') or meta.get('agent') not in (None, 'droid'):
        return None
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    model = meta.get('model', 'droid')
    effort = meta.get('effort', '')
    calls = max(1, meta.get('calls', 0))
    return {
        'model': short_model_hint(model) + (f' ({effort})' if effort else ''),
        'calls': calls, 'prompt': 0, 'in': 0, 'cache': 0, 'hit_pct': None, 'cache_write': 0,
        'out': 0, 'think': None, 'resp': None, 'tps': None, 'tps_kind': None, 'tps_dur': 0, 'tps_out': 0, 'tps_calls': 0, 'cost': 0.0,
        'cost_estimated': True, 'usage_available': False, 'cost_events': [],
        'per_model': [], 'is_done': meta['terminal'], 'is_err': meta['is_err'],
        '_saw_terminal': meta['terminal'], '_terminal_done': meta['terminal'],
        'mtime': mtime,
    }
