"""Shared cost row shape and AGY event identities."""
from __future__ import annotations

import hashlib
from typing import NamedTuple

from proto_wire import decode_proto

COMPACTED_UID = "~d"
_FUTURE_GRACE = 2


class CostEvent(NamedTuple):
    """Legacy ten-position row; input tokens include cache writes, not reads."""

    ts: float
    cost: float
    family: str
    uid: str
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    turns: int
    cache_write_tokens: int
    model: str


def valid_event_time(ts, now):
    return 0 < ts <= now + _FUTURE_GRACE


def payload_id(blob):
    return hashlib.sha256(blob).hexdigest()


def call_uid(call, ts):
    legacy = f"{call['step_idx']}:{call['in']}:{call['cache']}:{call['out']}:{int(ts)}"
    if not call.get("payload_id"):
        return legacy
    # ts may be filesystem mtime. Only the raw step clock is identity evidence.
    clock = call.get("measured_clock")
    return legacy + "#" + call["payload_id"] + ("@" + clock if clock else "")


def decode_event_clock(raw):
    # v16 stores the original protobuf clocks in UIDs. Compare their values
    # (not wire encoding), without losing nanoseconds to epoch-float rounding.
    values = []
    for field in (raw.split(":") + ["", "", ""])[:3]:
        try:
            parts = {k: v for k, t, v in decode_proto(bytes.fromhex(field)) if t == "varint"}
            values.append(parts.get(1, 0) * 10**9 + parts.get(2, 0) or None)
        except ValueError:
            values.append(field or None)
    return tuple(values)


def clock_groups(variants):
    # Process the strongest evidence first, independently of DB discovery order.
    # A completely undated copy cannot choose between mutually conflicting calls.
    result = {}
    for payload, raws in variants.items():
        if len(raws) == 1:
            result[payload, next(iter(raws))] = (payload, 0)
            continue
        slots = []
        for clock, raw in sorted(((decode_event_clock(r), r) for r in raws),
                                 key=lambda pair: (-sum(v is not None for v in pair[0]), pair[1])):
            matches = [i for i, slot in enumerate(slots) if len(slot) == len(clock) and
                       all(a is None or b is None or a == b for a, b in zip(slot, clock))]
            if clock in slots:
                index = slots.index(clock)
            elif len(matches) == 1:
                index = matches[0]
                slots[index] = tuple(a if a is not None else b for a, b in zip(slots[index], clock))
            else:
                index = len(slots)
                slots.append(clock)
            result[payload, raw] = (payload, index)
    return result


def event_digest(row):
    if len(row) >= 4:
        digest = str(row[3]).partition("#")[2].partition("@")[0]
        return digest[:16].ljust(16, "!") if digest else ""
    return ""


def event_digest_index(rows):
    return "".join(digest for row in rows if (digest := event_digest(row)))


ACTIVE_STEP_STATUSES = (1, 2, 8, 9)
ASK_QUESTION_STEP_STATUSES = (1, 2, 6, 7, 8, 9)


def ask_question_step(payload_or_meta: bytes) -> bool:
    """Return True if a step_payload or step metadata belongs to an `ask_question` tool call."""
    if not payload_or_meta or b"ask_question" not in payload_or_meta:
        return False
    try:
        top = {k: v for k, t, v in decode_proto(payload_or_meta) if t == "bytes" and isinstance(v, bytes)}
        for meta_bytes in (top.get(5, b""), payload_or_meta):
            if not meta_bytes:
                continue
            m_map = {k: v for k, t, v in decode_proto(meta_bytes) if t == "bytes" and isinstance(v, bytes)}
            call_bytes = m_map.get(4, b"")
            if not call_bytes:
                continue
            c_map = {k: v for k, t, v in decode_proto(call_bytes) if t == "bytes" and isinstance(v, bytes)}
            if c_map.get(2) == b"ask_question":
                return True
    except Exception:
        return False
    return False
