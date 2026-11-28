"""Discover and cache deterministic bindings from parent steps to subagents."""
from __future__ import annotations

import contextlib
import re
import threading
import time
import zlib
from pathlib import Path

from analyzer import RE_INVOKE_ID, RE_PROTO_SUB_UUID
from cockpit_io import APP_DIRS, BoundedCache, connect_readonly, dependency_signature, get_db_mtime, serialized
from proto_wire import decode_proto, parse_proto_duration
from subagent_commands import bind_dispatch, parse_dispatch
from subagent_labels import format_native_label
from subagent_payload import _parse_step127_subagents, _parse_step132_payload
from subagent_session_index import SessionMatcher
from swarm_subagents import discover_swarm_stubs, merge_swarm_stub, swarm_dir_signature

_DISPATCH_BYTES = (
    b"sa-run.sh",
    b"sa-codex",
    b"sa-agy",
    b"sa-droid",
    b"sa-grok",
    b"sa-pi",
    b"codex exec",
    b"agy -p",
    b"agy --conversation",
    b"pi -p",
    b"droid exec",
    b"grok -p",
    b"grok --resume",
    b"grok -r",
    b"grok -s",
    b"grok --session-id",
    b"invoke_subagent",
    b"send_message",
)

_SUBAGENT_STUBS_CACHE = BoundedCache(128)
_COMPLETED_STEP_STUBS = BoundedCache(8000)
_STUBS_PARSE_LOCK = threading.Lock()
_STEP_DEPENDENCIES = BoundedCache(8000)


def _stub_dependencies(conv_id, bdir, stubs):
    paths = [st['raw_file'] for st in stubs if st.get('raw_file')]
    for st in stubs:
        if st.get('step_idx'):
            paths.append(bdir / 'steps' / f"{st['step_idx']}" / 'output.txt')
    for key in _STEP_DEPENDENCIES:
        if key[0] == conv_id:
            paths.extend(_STEP_DEPENDENCIES.get(key, ()))
    return dependency_signature(paths)


def _dispatch_rows(db_path):
    clauses = " OR ".join(["instr(step_payload, ?) > 0"] * len(_DISPATCH_BYTES))
    with contextlib.closing(connect_readonly(db_path, timeout=0.5)) as conn:
        return conn.execute(
            f"""
            SELECT idx, step_type, status, metadata, step_payload FROM steps
            WHERE step_type = 127 OR (step_type = 132 AND ({clauses}))
            ORDER BY idx
            """,
            _DISPATCH_BYTES,
        ).fetchall()


def _combined_output(tlog, sout, inline_txt):
    texts = [inline_txt]
    for path in (tlog, sout):
        if path.exists():
            try:
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
    return "\n".join(texts)


def _new_stub(sid, agent, label, model_hint, effort_hint, status, idx, started,
              raw_file="", *, has_done=False, has_error=False):
    return {
        "id": sid, "agent": agent, "label": label, "model_hint": model_hint, "effort_hint": effort_hint,
        "step_status": status, "step_idx": idx, "step_ts": started, "prompt_idx": idx, "prompt_ts": started,
        "has_done_marker": has_done, "has_err_marker": has_error, "raw_file": raw_file,
    }


def _apply_native(found, conv_id, row, args, output, sout, started):
    idx, step_type, status, _, payload = row
    if step_type == 127:
        spawned = _parse_step127_subagents(payload, args)
        for sid, role, model, transcript in spawned:
            if sid and sid != conv_id:
                found[sid] = _new_stub(sid, "builtin", role, model, "", status, idx, started,
                                       transcript, has_error=status in (4, 6, 7))
        return not spawned

    _STEP_DEPENDENCIES[conv_id, idx] = (str(sout),)
    match = RE_INVOKE_ID.search(output)
    sid = match[1] if match else None
    if not sid:
        match = RE_PROTO_SUB_UUID.search(payload)
        if match:
            sid = match[1].decode("ascii")
    if not sid and status in (1, 2, 8):
        sid = f"pending-{idx}"
    if sid and sid != conv_id:
        role = str(args.get("Role") or args.get("TypeName") or "subagent")
        label = format_native_label(role, str(args.get("Prompt") or args.get("Task") or ""))
        found[sid] = _new_stub(sid, "builtin", label, str(args.get("Model") or ""), "", status, idx, started,
                               has_error=status in (4, 6, 7))
    return not sid or sid.startswith("pending-")


def _apply_cli(found, conv_id, row, args, output, sout, started, matcher):
    idx, _, status, _, _ = row
    dispatch = parse_dispatch(args)
    if dispatch is None:
        return False
    sid, output = bind_dispatch(dispatch, output, conv_id, started, matcher)
    if not sid and status in (1, 2, 8):
        sid = f"pending-{idx}"

    raw_file = str(dispatch.raw_file) if dispatch.raw_file else ""
    _STEP_DEPENDENCIES[conv_id, idx] = (str(sout), raw_file) if raw_file else (str(sout),)
    incomplete = not sid or sid.startswith("pending-")
    if not sid or sid == conv_id:
        return incomplete

    has_done = any(line.startswith("■ ") and not line.startswith("■ ERROR") for line in output.splitlines())
    has_err = any(line.startswith(("■ ERROR", "✗ ")) for line in output.splitlines())
    stub = found.get(sid)
    if stub is None:
        found[sid] = _new_stub(sid, dispatch.agent, dispatch.label, dispatch.model_hint, dispatch.effort_hint,
                               status, idx, started, raw_file, has_done=has_done, has_error=has_err)
        return incomplete

    if stub["label"].endswith("-cli") and not dispatch.label.endswith("-cli"):
        stub["label"] = dispatch.label
    if dispatch.model_hint and not stub["model_hint"]:
        stub["model_hint"] = dispatch.model_hint
    if dispatch.effort_hint and not stub["effort_hint"]:
        stub["effort_hint"] = dispatch.effort_hint
    stub["step_status"] = status
    stub["step_idx"] = idx
    stub["prompt_idx"] = max(int(stub.get("prompt_idx") or 0), int(idx or 0))
    if started > 0:
        stub["step_ts"] = started
        stub["prompt_ts"] = max(float(stub.get("prompt_ts") or 0.0), started)
    stub["has_done_marker"] = has_done
    stub["has_err_marker"] = has_err
    if raw_file:
        stub["raw_file"] = raw_file
    return incomplete


def _apply_step(found, conv_id, row, tlog, sout, matcher):
    """Return (step incomplete, discovery incomplete); unresolved sends only affect the former."""
    idx, step_type, _, meta, payload = row
    if not payload or (step_type == 132 and not any(mark in payload for mark in _DISPATCH_BYTES)):
        return False, False
    tool, args, inline_txt = _parse_step132_payload(payload)
    if step_type == 127:
        tool = "invoke_subagent"
    if tool not in ("run_command", "invoke_subagent", "send_message"):
        return False, False

    fields = {k: v for k, t, v in decode_proto(meta) if t == "bytes" and isinstance(v, bytes)} if meta else {}
    started = parse_proto_duration(fields.get(1, b""))
    if tool == "send_message":
        recipient = str(args.get("Recipient") or "").strip()
        if not recipient or recipient not in found:
            return True, False
        stub = found[recipient]
        if started > 0:
            stub["prompt_ts"] = max(float(stub.get("prompt_ts") or stub.get("step_ts") or 0.0), started)
        stub["prompt_idx"] = max(int(stub.get("prompt_idx") or stub.get("step_idx") or 0), int(idx or 0))
        return False, False

    output = _combined_output(tlog, sout, inline_txt)
    if tool == "invoke_subagent":
        incomplete = _apply_native(found, conv_id, row, args, output, sout, started)
    else:
        incomplete = _apply_cli(found, conv_id, row, args, output, sout, started, matcher)
    return incomplete, incomplete


def _log_mtime(tlog, sout):
    stamp = 0
    try:
        if tlog.exists():
            stamp = tlog.stat().st_mtime_ns
        if sout.exists():
            stamp = max(stamp, sout.stat().st_mtime_ns)
    except Exception:
        pass
    return stamp


def _step_cache_key(prefix, dependency_key):
    return (*prefix, dependency_signature(_STEP_DEPENDENCIES.get(dependency_key, ())))


def _read_steps(conv_id, bdir, tasks_dir, rows):
    found = {}
    matcher = SessionMatcher()
    incomplete = False
    for row in rows:
        idx, _, status, _, payload = row
        tlog = tasks_dir / f"task-{idx}.log"
        sout = bdir / "steps" / f"{idx}" / "output.txt"
        prefix = (conv_id, int(idx or 0), int(status or 0), int(_log_mtime(tlog, sout)), zlib.crc32(payload or b""))
        terminal = int(status or 0) not in (1, 2, 8)
        if terminal:
            key = _step_cache_key(prefix, (conv_id, idx))
            with _STUBS_PARSE_LOCK:
                cached = _COMPLETED_STEP_STUBS.get(key)
            if cached is not None:
                for sid, stub in cached:
                    found[sid] = dict(stub)
                continue
        before = {k: dict(v) for k, v in found.items()} if terminal else {}
        step_incomplete = False
        try:
            step_incomplete, discovery_incomplete = _apply_step(found, conv_id, row, tlog, sout, matcher)
            incomplete |= discovery_incomplete
        finally:
            if terminal and not step_incomplete:
                changed = [(sid, dict(stub)) for sid, stub in found.items() if before.get(sid) != stub]
                key = _step_cache_key(prefix, (conv_id, idx))
                with _STUBS_PARSE_LOCK:
                    if len(_COMPLETED_STEP_STUBS) > 8000:
                        _COMPLETED_STEP_STUBS.clear()
                    if not any(sid.startswith("pending-") for sid, _ in changed):
                        _COMPLETED_STEP_STUBS[key] = changed
    return found, incomplete


def _mark_killed_steps(db_path, found):
    try:
        with contextlib.closing(connect_readonly(db_path, timeout=0.5)) as conn:
            rows = conn.execute(
                "SELECT idx, step_payload FROM steps WHERE step_type = 132 AND instr(step_payload, ?) > 0 ORDER BY idx",
                (b"manage_task",),
            ).fetchall()
        killed_steps = set()
        for idx, payload in rows:
            tool, args, _ = _parse_step132_payload(payload or b"")
            if tool == "manage_task" and str(args.get("Action") or "") == "kill":
                match = re.search(r"/task-(\d+)\b", str(args.get("TaskId") or ""))
                if match and int(match[1]) < int(idx or 0):
                    killed_steps.add(int(match[1]))
        for stub in found.values():
            if int(stub.get("step_idx") or 0) in killed_steps:
                stub["has_done_marker"] = True
    except Exception:
        pass


@serialized(lambda first, *args, **kwargs: str(first))
def discover_subagent_stubs(conv_id: str) -> list[dict]:
    """Bind each parent tool step (type 127 or 132) to its own subagent and live status."""
    for _, base in APP_DIRS:
        db_path = base / "conversations" / f"{conv_id}.db"
        if db_path.exists():
            break
    else:
        return discover_swarm_stubs(conv_id, APP_DIRS)

    bdir = base / "brain" / conv_id / ".system_generated"
    tasks_dir = bdir / "tasks"
    db_mtime = tasks_mtime = 0.0
    try:
        db_mtime = get_db_mtime(db_path)
        if tasks_dir.exists():
            for path in tasks_dir.glob("*.log"):
                tasks_mtime = max(tasks_mtime, path.stat().st_mtime)
    except Exception:
        pass

    cache_key = (db_mtime, tasks_mtime, swarm_dir_signature(conv_id, APP_DIRS))
    cached = _SUBAGENT_STUBS_CACHE.get(conv_id)
    dependencies = _stub_dependencies(conv_id, bdir, cached[1] if cached else [])
    if cached and cached[0] == cache_key and cached[3] == dependencies:
        if not cached[4] or time.monotonic() - cached[2] < 0.5:
            return [dict(stub) for stub in cached[1]]
    try:
        rows = _dispatch_rows(db_path)
    except Exception:
        if cached:
            return [dict(stub) for stub in cached[1]]
        return discover_swarm_stubs(conv_id, APP_DIRS)

    found, incomplete = _read_steps(conv_id, bdir, tasks_dir, rows)
    if found:
        _mark_killed_steps(db_path, found)
    for stub in discover_swarm_stubs(conv_id, APP_DIRS):
        merge_swarm_stub(found, stub)
    stubs = list(found.values())
    dependencies = _stub_dependencies(conv_id, bdir, stubs)
    incomplete = incomplete or any(st["id"].startswith("pending-") for st in stubs)
    _SUBAGENT_STUBS_CACHE[conv_id] = (cache_key, stubs, time.monotonic(), dependencies, incomplete)
    return [dict(stub) for stub in stubs]
