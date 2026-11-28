"""Linear, ordered union of local and ledger history for forced engine handoff."""
import contextlib
from typing import Optional

from . import session_handoff
from .session_files import ledger_lock


def build_forced_handoff_prompt(agent, session_id, cwd, prompt, state_dir=None) -> Optional[str]:
    sid = (session_id or "").strip()
    ledger, local = {}, {}
    with contextlib.suppress(Exception):
        with ledger_lock(sid, state_dir):
            ledger = session_handoff.load_session_ledger(sid, state_dir) or {}
    with contextlib.suppress(Exception):
        local = session_handoff.inspect_local_session(agent, session_id) or {}

    def key(turn):
        return str(turn.get("prompt") or "").strip(), str(turn.get("response") or "").strip()

    def normalize(source):
        unique = {}
        for turn in source:
            if not isinstance(turn, dict):
                continue
            carried, clean = session_handoff._unwrap_handoff_prompt(str(turn.get("prompt") or ""), turn.get("ts", 0))
            for item in carried + [dict(turn, prompt=clean)]:
                pair = key(item)
                if any(pair):
                    unique.setdefault(pair, item)
        return list(unique.values())

    left, right = normalize(local.get("turns") or []), normalize(ledger.get("turns") or [])
    positions = {key(turn): i for i, turn in enumerate(left)}
    turns, seen, pending, cursor = [], set(), [], 0

    def emit(items):
        for item in items:
            pair = key(item)
            if pair not in seen:
                seen.add(pair)
                turns.append(item)

    # Shared turns are anchors. Each source item is visited at most once;
    # missing prefixes/suffixes from either source retain their original order.
    for item in right:
        index = positions.get(key(item))
        if index is None:
            pending.append(item)
        elif index >= cursor:
            emit(left[cursor:index])
            emit(pending)
            pending.clear()
            emit([left[index]])
            cursor = index + 1
    emit(left[cursor:])
    emit(pending)
    _, clean_prompt = session_handoff._unwrap_handoff_prompt(prompt)
    return session_handoff.format_handoff_prompt(session_id, turns, clean_prompt) if turns else None
