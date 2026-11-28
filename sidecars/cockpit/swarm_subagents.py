"""Read only the local Swarm roster and board; no Swarm-specific ledger."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
import math
from os import fstat
from pathlib import Path
import re
import sqlite3
import threading
import time

from cockpit_io import connect_readonly, parse_iso_ts
from cost_schema import ACTIVE_STEP_STATUSES, ASK_QUESTION_STEP_STATUSES, ask_question_step, clock_groups

_RELATIVE_DIR = Path(".system_generated/swarm")
_MEMBER_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_MENTION = re.compile(r"(?<![a-zA-Z0-9_@-])@([a-zA-Z0-9_-]+)")
_SCAN_TTL = 3.0
_ROSTER_ACTIVE_SEC = 300
_SCAN_LOCK = threading.Lock()
_SCAN_CACHE = None


def _roots(app_dirs):
    return tuple(dict.fromkeys(Path(item[1] if isinstance(item, (list, tuple)) else item)
                               for item in app_dirs))


def _swarm_dirs(conv_id, app_dirs):
    if not isinstance(conv_id, str) or not _MEMBER_ID.fullmatch(conv_id):
        return ()
    return tuple(base / "brain" / conv_id / _RELATIVE_DIR for base in _roots(app_dirs))


def _read_roster(path):
    try:
        with path.open(encoding="utf-8") as stream:
            # Stamp the opened file, not a concurrently replaced pathname.
            stamp = fstat(stream.fileno()).st_mtime
            record = json.load(stream)
    except (OSError, ValueError, RecursionError):
        return {}, 0.0
    members = record.get("members") if isinstance(record, dict) else None
    return (members if isinstance(members, dict) else {}), stamp


def _workers(conv_id, members):
    for name, member in members.items():
        if not isinstance(member, dict):
            continue
        cid, role = member.get("conversation_id"), member.get("role")
        if (role == "coordinator" or not isinstance(cid, str)
                or cid == conv_id or not _MEMBER_ID.fullmatch(cid)):
            continue
        yield name, cid, member


def _board_timestamp(raw):
    if type(raw) in (int, float):
        try:
            ts = raw / 1000 if raw >= 1e11 else float(raw)
        except OverflowError:
            return None
    else:
        ts = parse_iso_ts(raw) if isinstance(raw, str) else None
    return ts if ts and ts > 0 and math.isfinite(ts) else None


def _board_prompts(path):
    touched = {}
    board_mtime = 0.0
    try:
        with path.open("rb") as stream:
            board_mtime = fstat(stream.fileno()).st_mtime
            for line in stream:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    continue  # Appends may leave an incomplete final line.
                if not isinstance(row, dict):
                    continue
                stamp = _board_timestamp(row.get("time"))
                if not stamp:
                    continue
                author, body = row.get("agent"), row.get("text")
                names = set(_MENTION.findall(body)) if isinstance(body, str) else set()
                if isinstance(author, str):
                    names.add(author)
                for name in names:
                    key = name.lower()
                    touched[key] = max(touched.get(key, 0.0), stamp)
    except OSError:
        pass
    return touched, board_mtime


def discover_swarm_stubs(conv_id, app_dirs):
    found, mentions = {}, {}
    for folder in _swarm_dirs(conv_id, app_dirs):
        members, stamp = _read_roster(folder / "members.json")
        workers = list(_workers(conv_id, members))
        if not workers:
            continue
        prompts, board_stamp = _board_prompts(folder / "board.jsonl")
        activity = max(stamp, board_stamp)
        for name, cid, member in workers:
            mentions[cid] = max(mentions.get(cid, 0.0), prompts.get(name.lower(), 0.0), prompts.get("all", 0.0))
            if cid in found:
                found[cid]["swarm_activity_ts"] = max(found[cid]["swarm_activity_ts"], activity)
                continue
            role = member.get("role") if isinstance(member.get("role"), str) else ""
            model = member.get("model") if isinstance(member.get("model"), str) else ""
            killed = member.get("killed") is True
            found[cid] = dict(id=cid, agent="swarm", label=f"{name} ({role})" if role else name,
                              model_hint=model, killed=killed, step_ts=stamp, is_swarm=True,
                              swarm_role=role, swarm_activity_ts=activity, step_status=1, has_done_marker=killed)
    return [dict(stub, prompt_ts=mentions[cid] or stub["step_ts"]) for cid, stub in found.items()]


def merge_swarm_stub(found, stub):
    if stub["id"] in found:
        found[stub["id"]].update({key: stub[key] for key in
                                  ("is_swarm", "killed", "swarm_role", "prompt_ts", "swarm_activity_ts")})
    else:
        found[stub["id"]] = stub


def is_swarm_worker(st):
    return bool(st.get("is_swarm") or st["agent"] == "swarm")


def native_worker_status(st, data, resolve_status):
    swarm = is_swarm_worker(st)
    running = not bool(data.get("is_idle", True))
    last_status = int(data.get("last_step_status") or 0)
    if swarm or st["agent"] == "builtin":
        if st.get("killed"):
            return "killed"
        if last_status in (4, 6, 7) or resolve_status(st) == "error":
            return "error"
        if data.get("waiting_ask_question"):
            return "waiting"
        if swarm:
            return "running" if running else "done"
    if not running and last_status == 3:
        st = dict(st, has_done_marker=True)
        if st["agent"] == "agy":
            st["has_err_marker"] = False  # Keep the legacy CLI retry behavior.
    return resolve_status(st, sub_running=running)


def native_stub_status(st, db_path, resolve_status):
    if st.get("killed"):
        return "killed"
    swarm = is_swarm_worker(st)
    if db_path is not None and (swarm or st["agent"] == "builtin"):
        # The analyzer returns an empty result before the first metered call.

        try:
            with closing(connect_readonly(str(db_path), timeout=0.5)) as conn:
                row = conn.execute("SELECT step_type, status, metadata, step_payload "
                                   "FROM steps ORDER BY idx DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            kind, status, metadata, payload = row
            waiting = (kind == 132 and status in ASK_QUESTION_STEP_STATUSES
                       and (ask_question_step(metadata or b"") or ask_question_step(payload or b"")))
            return native_worker_status(st, dict(last_step_status=status, waiting_ask_question=waiting,
                                                  is_idle=status not in ACTIVE_STEP_STATUSES), resolve_status)
    if swarm:
        stamp = st.get("swarm_activity_ts") or 0
        return "running" if stamp > 0 and time.time() - stamp < _ROSTER_ACTIVE_SEC else "done"
    return "running"


def list_swarm_worker_ids(app_dirs):
    global _SCAN_CACHE
    roots = _roots(app_dirs)
    with _SCAN_LOCK:
        if _SCAN_CACHE and _SCAN_CACHE[0] == roots and time.monotonic() < _SCAN_CACHE[1]:
            return set(_SCAN_CACHE[2])
        workers = set()
        for root in roots:
            try:
                for path in (root / "brain").glob(f"*/{_RELATIVE_DIR}/members.json"):
                    members, _ = _read_roster(path)
                    workers.update(cid for _, cid, _ in _workers(path.parents[2].name, members))
            except OSError:
                continue
        _SCAN_CACHE = (roots, time.monotonic() + _SCAN_TTL, frozenset(workers))
        return workers


def swarm_dir_signature(conv_id, app_dirs):
    signature = []
    for folder in _swarm_dirs(conv_id, app_dirs):
        for name in ("members.json", "board.jsonl"):
            path = folder / name
            try:
                stat = path.stat()
                signature.append((path, stat.st_ino, stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((path, None))
    return tuple(signature)


def fork_parent_overlap(parent_events, worker_events):
    """Match forked calls by payload/clock evidence, never by model or tokens alone."""

    def identified(events):
        for event in events:
            if len(event) >= 10:
                _, _, evidence = event[3].partition("#")
                digest, _, clock = evidence.partition("@")
                if digest:
                    yield digest, clock, event

    parent_rows = list(identified(parent_events))
    if not parent_rows:
        return []
    variants = {digest: set() for digest, _, _ in parent_rows}
    worker_rows = [(d, c, e) for d, c, e in identified(worker_events) if d in variants]
    for digest, clock, _ in (*parent_rows, *worker_rows):
        variants[digest].add(clock)
    groups = clock_groups(variants)
    remaining = Counter(groups[d, c] for d, c, _ in parent_rows)
    by_model = {}
    for digest, clock, event in worker_rows:
        key = groups[digest, clock]
        if not remaining[key]:
            continue
        remaining[key] -= 1
        row = by_model.setdefault(event[9], dict(model=event[9], calls=0, prompt=0, cost=0.0,
                                                  **{"in": 0, "cache": 0, "out": 0}))
        row["calls"] += 1
        row["cost"] += event[1]
        row["prompt"] += event[4] + event[5]
        for field, value in zip(("in", "cache", "out"), event[4:7]):
            row[field] += value
    return list(by_model.values())


_USAGE_FIELDS = ("calls", "prompt", "in", "cache", "cache_write", "out", "cost")


def _subtract_usage(row, delta):
    for key in _USAGE_FIELDS:
        remaining = (row.get(key) or 0) - (delta.get(key) or 0)
        # Persisted USD events are rounded to eight places per call.
        if key == "cost" and remaining < 5e-9 * delta.get("calls", 0):
            remaining = 0.0
        row[key] = max(0, remaining)
    if "hit_pct" in row:
        row["hit_pct"] = row["cache"] * 100.0 / row["prompt"] if row["prompt"] else None


def finish_swarm_item(item, st, parent_cache, parent_id, worker_data, canonical_model):
    """Subtract proven fork history once, before any consumer reads the item."""
    if not is_swarm_worker(st):
        return
    item.update(is_swarm=True, swarm_role=st.get("swarm_role", ""), killed=bool(st.get("killed")))
    if not worker_data:
        return
    parent = (parent_cache.get(parent_id) or (None, {}))[1]
    overlap = fork_parent_overlap(parent.get("cost_events", []), worker_data.get("cost_events", []))
    if not overlap:
        return
    item["fork_parent_overlap"] = overlap
    _subtract_usage(item, {key: sum(row.get(key, 0) for row in overlap) for key in _USAGE_FIELDS})
    subtract = {canonical_model(row["model"]): row for row in overlap}
    adjusted = []
    for row in item["per_model"]:
        net = dict(row)
        model = canonical_model(row.get("model") or item["model"])
        if model in subtract:
            _subtract_usage(net, subtract.pop(model))
        adjusted.append(net)
    item["per_model"] = adjusted
