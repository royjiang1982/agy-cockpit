"""Deterministic P2 regression cases; all files/endpoints are synthetic."""
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from unittest.mock import patch


def quota_cases(q, pf):
    model = 'gemini-4-hydrogen-high'
    def bucket(bid, frac, desc=''):
        return dict(bid=bid, desc=desc, rem_frac=frac, reset_ts=0)
    assert q._resolve(model, [bucket('shared-3p', 0)]) is None
    assert q._resolve('claude-opus-5-5', [bucket('shared-3p', 0)])['rem_frac'] == 0
    for unknown in (model, 'custom-unknown', 'gemini-unknown-future', 'gemini-4-carbon-unknown'):
        assert q._resolve(unknown, [bucket('global-weekly', 0)]) is None
        assert q._resolve(unknown, [bucket('gemini-5h', 0)]) is None
    assert q._resolve(model, [bucket('gemini-5h', .9, 'Gemini 4 Hydrogen'), bucket('global-weekly', .1)])['rem_frac'] == .1
    assert q._resolve(model, [bucket('shared-gemini', .2)])['rem_frac'] == .2
    q.invalidate_agy_quota_cache()
    with patch.object(q, '_fetch_snapshot', side_effect=OSError('offline')) as fetch:
        assert not pf()['should_fallback']
        assert not pf()['should_fallback']
        assert fetch.call_count == 1
    from agent_runtime import quota_guard as guard
    with patch.object(guard.time, 'perf_counter', return_value=guard._NEGATIVE_UNTIL + 1), \
            patch.object(q, '_fetch_snapshot', side_effect=OSError('offline')) as discover:
        assert not pf()['should_fallback'] and discover.call_count == 1
    q.invalidate_agy_quota_cache()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    def stalled():
        entered.set()
        release.wait(2)
        done.set()
        return []
    try:
        with patch.dict(os.environ, SA_AGY_QUOTA_TIMEOUT_SEC='.04'), \
                patch.object(q, '_fetch_snapshot', side_effect=stalled) as discover:
            start = time.perf_counter()
            assert not pf()['should_fallback'] and entered.is_set()
            assert time.perf_counter() - start < .4
            assert not pf()['should_fallback'] and discover.call_count == 1
            t0 = time.perf_counter()
            q.invalidate_agy_quota_cache()
            assert time.perf_counter() - t0 < .05 and guard._FLIGHT is not None
            release.set()
            assert done.wait(1)
    finally:
        release.set()
        # Wait for the isolated lookup to finish before restoring shared test state.
        deadline = time.perf_counter() + 1
        while guard._FLIGHT is not None and time.perf_counter() < deadline:
            time.sleep(.001)
        q.invalidate_agy_quota_cache()
    barrier = threading.Barrier(2)
    def slow_snap():
        time.sleep(.03)
        return [bucket('3p-5h', 0, 'Models within this group: Claude Opus 5.5, GPT-OSS 120B'),
                bucket('gemini-5h', 0, 'Models within this group: Gemini 4 Carbon, Gemini 4 Hydrogen')]
    concurrent_out = {}
    def worker(m):
        barrier.wait(1)
        concurrent_out[m] = q.preflight_agy_request(agent='agy', model=m, prompt='task')['should_fallback']
    with patch.object(q, '_fetch_snapshot', side_effect=slow_snap) as snap_mock:
        ts = [threading.Thread(target=worker, args=(m,)) for m in ('claude-opus-5-5-high', 'gemini-4-hydrogen-high')]
        for th in ts: th.start()
        for th in ts: th.join(2)
        assert concurrent_out == {'claude-opus-5-5-high': True, 'gemini-4-hydrogen-high': True}, concurrent_out
        assert snap_mock.call_count == 1
    q.invalidate_agy_quota_cache()
    print('PASS: family/global quota scope, unknown quota, negative cache expiry and total deadline')


def session_cases():
    from agent_runtime import session_handoff as sh, session_files as sf
    with tempfile.TemporaryDirectory() as td, patch.object(Path, 'home', return_value=Path(td)):
        path = Path(td) / '.codex/sessions/2026/rollout-cache-sid.jsonl'
        path.parent.mkdir(parents=True)
        def turn(prompt, reply):
            return ''.join(json.dumps(dict(type='message', message=dict(role=role, content=text))) + '\n'
                           for role, text in [('user', prompt), ('assistant', reply)])
        path.write_text(turn('first', 'answer'))
        original_read = Path.read_text
        def read(p, *args, **kwargs):
            assert not sh._LEDGER_LOCK._is_owned()
            return original_read(p, *args, **kwargs)
        with patch.object(Path, 'rglob', autospec=True, wraps=Path.rglob) as scan:
            # autospec+wraps is not a real generator on every Python version.
            scan.side_effect = lambda root, pattern: original_glob(root, pattern)
            with patch.object(Path, 'read_text', autospec=True, side_effect=read) as reads:
                first = sh.inspect_local_session('codex', 'cache-sid')
                assert first['turns'][0]['prompt'] == 'first'
                assert sh.inspect_local_session('codex', 'cache-sid') == first
                assert scan.call_count == reads.call_count == 1
                first['turns'].clear()
                assert len(sh.inspect_local_session('codex', 'cache-sid')['turns']) == 1
                path.write_text(turn('first', 'answer') + turn('second', 'complete'))
                updated = sh.inspect_local_session('codex', 'cache-sid')
                assert len(updated['turns']) == 2 and scan.call_count == 1 and reads.call_count == 2
                sh.record_turn_completion('codex', 'cache-sid', 'second', 'complete', daemon_dir=Path(td))
                ledger = sh.load_session_ledger('cache-sid', Path(td))
                assert [(t['prompt'], t['response']) for t in ledger['turns']] == [('first', 'answer'), ('second', 'complete')]
        assert sf.ledger_lock('a', td) is not sf.ledger_lock('b', td)
        ephemeral = Path(td) / '.codex/sessions/2026/rollout-ephemeral.jsonl'
        ephemeral.write_text(turn('vanish', 'ok'))
        wrapped = sf.cached_jsonl_inspection(lambda a, s, ps: (ephemeral.unlink(), {'ok': True})[1])
        assert wrapped('codex', 'ephemeral') == {'ok': True}
    print('PASS: cached session path/content, changed-file refresh, isolated ledger locks and complete history')


original_glob = Path.rglob


def codex_cases():
    from agent_runtime.codex_stream import CodexStream
    from agent_runtime.cli_events import should_emit
    for enabled in (False, True):
        stream = CodexStream.__new__(CodexStream)
        stream._emit_cumulative = enabled
        stream._events, stream._message_text = queue.Queue(), {}
        stream.tool_token = None
        stream._redact_stream_chunk = lambda text: text
        for _ in range(1000):
            stream._notification('item/agentMessage/delta', dict(itemId='long', delta='x' * 100))
        assert stream._events.qsize() == (1000 if enabled else 0)
        assert bool(stream._message_text) == enabled
        stream._notification('item/completed', dict(item=dict(id='long', type='agentMessage', text='x' * 100000)))
        stream._notification('item/started', dict(item=dict(type='commandExecution', command='echo ok')))
        stream._notification('error', dict(error='synthetic error'))
        stream.terminal_error = ''
        stream._events.put(None)
        with patch('agent_runtime.codex_stream.json.dumps', side_effect=AssertionError('dict path must not serialize')):
            events = list(stream.iter_events())
        assert len(events) == (1003 if enabled else 3)
        assert events[-3]['item']['text'] == 'x' * 100000 and 'delta' not in events[-3]
        assert events[-2]['item']['type'] == 'command_execution' and events[-1]['type'] == 'error'
    for kind in ('error', 'command_execution', 'future_item'):
        assert should_emit(dict(type='item.completed', delta=True, item=dict(type=kind)), False)
    print('PASS: suppressed quadratic Codex accumulation, lossless opt-in, dict transport, final/tool/error retention')
