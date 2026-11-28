"""Publish cached metrics while deriving live idle status at read time."""
import time


def _live_metrics(value):
    out = dict(value)
    if '_saw_terminal' in out:
        out['is_done'] = bool(out.get('_terminal_done', True)) if out['_saw_terminal'] else time.time() - out['mtime'] >= 120.0
    return out


def _publish_metrics(cache, key, mtime, value):
    cache.publish(key, (mtime, value), lambda entry: entry[0])
    return _live_metrics(value)
