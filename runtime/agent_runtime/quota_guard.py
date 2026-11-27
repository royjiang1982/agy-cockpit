"""Bounded, single-flight local quota lookup with short failure caching."""
import math
import os
import threading
import time

_LOCK = threading.Lock()
_PID = os.getpid()
_FLIGHT = None
_NEGATIVE_UNTIL = 0.0
_LOCAL = threading.local()


def number(name, default):
    try:
        value = float(os.environ.get(name, default))
        return value if math.isfinite(value) and value >= 0 else default
    except ValueError:
        return default


def remaining():
    left = getattr(_LOCAL, 'deadline', float('inf')) - time.perf_counter()
    if left <= 0:
        raise TimeoutError('quota preflight deadline')
    if getattr(_LOCAL, 'cancelled', None) is not None and _LOCAL.cancelled.is_set():
        raise TimeoutError('quota preflight abandoned')
    return left


def invalidate():
    global _NEGATIVE_UNTIL
    with _LOCK:
        _NEGATIVE_UNTIL = 0.0
        if _FLIGHT is not None:
            _FLIGHT['invalidated'] = True
            _FLIGHT['cancelled'].set()


def fetch(fn):
    global _PID, _FLIGHT, _NEGATIVE_UNTIL
    budget = number('SA_AGY_QUOTA_TIMEOUT_SEC', 10.0)
    now = time.perf_counter()
    deadline = now + budget
    with _LOCK:
        if _PID != os.getpid():
            _PID, _FLIGHT, _NEGATIVE_UNTIL = os.getpid(), None, 0.0
        if now < _NEGATIVE_UNTIL:
            return None
        # Do not attach new callers to an invalidated or already timed-out flight.
        if _FLIGHT is not None and (_FLIGHT.get('timed_out') or _FLIGHT.get('invalidated')):
            return None
        if _FLIGHT is None:
            flight = {'done': threading.Event(), 'deadline': deadline, 'cancelled': threading.Event()}
            _FLIGHT = flight
            def run():
                global _FLIGHT, _NEGATIVE_UNTIL
                _LOCAL.deadline = deadline
                _LOCAL.cancelled = flight['cancelled']
                try:
                    flight['value'] = fn()
                except Exception:
                    flight['failed'] = True
                finally:
                    with _LOCK:
                        if _FLIGHT is flight:
                            if ((flight.get('failed') or time.perf_counter() >= deadline)
                                    and not flight.get('invalidated') and not flight.get('timed_out')):
                                _NEGATIVE_UNTIL = time.perf_counter() + number('SA_AGY_QUOTA_NEGATIVE_TTL_SEC', 30.0)
                            _FLIGHT = None
                        flight['done'].set()
            threading.Thread(target=run, daemon=True).start()
        else:
            flight = _FLIGHT
    if not flight['done'].wait(max(0, deadline - time.perf_counter())):
        with _LOCK:
            flight['timed_out'] = True
            flight['cancelled'].set()
            if not flight.get('invalidated'):
                _NEGATIVE_UNTIL = time.perf_counter() + number('SA_AGY_QUOTA_NEGATIVE_TTL_SEC', 30.0)
        return None
    if flight.get('failed') or flight.get('invalidated') or time.perf_counter() >= deadline:
        return None
    return flight.get('value')


def lookup(fn, model):
    return fetch(lambda: fn(model))
