"""Linear, ordered merge of local and ledger history for forced engine handoff."""
from collections import defaultdict, deque
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
        # SQLite inserts paragraph breaks between steps; the stream joins deltas directly.
        response = str(turn.get("response") or "").replace("\r", "").replace("\n", "")
        return str(turn.get("prompt") or "").strip(), response

    def expand(source):
        for turn in source:
            if not isinstance(turn, dict):
                continue
            carried, clean = session_handoff._unwrap_handoff_prompt(str(turn.get("prompt") or ""), turn.get("ts", 0))
            for item in carried + [dict(turn, prompt=clean)]:
                if item["prompt"] or str(item.get("response") or "").strip():
                    yield item

    left = list(expand(local.get("turns") or []))
    positions = defaultdict(deque)
    for i, turn in enumerate(left):
        positions[key(turn)].append(i)
    turns, pending, cursor = [], [], 0

    # Pair only forward occurrences; earlier indices already went out with a local gap.
    for item in expand(ledger.get("turns") or []):
        matches = positions.get(key(item))
        while matches and matches[0] < cursor:
            matches.popleft()
        if not matches:
            pending.append(item)
            continue
        index = matches.popleft()
        turns.extend(left[cursor:index])
        turns.extend(pending)
        pending.clear()
        turns.append(left[index])
        cursor = index + 1
    turns.extend(left[cursor:])
    turns.extend(pending)
    _, clean_prompt = session_handoff._unwrap_handoff_prompt(prompt)
    return session_handoff.format_handoff_prompt(session_id, turns, clean_prompt) if turns else None
