"""Read AGY session history and usage from SQLite and Protobuf steps."""
import contextlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .handoff_text import _append_unwrapped_user_turn, _strip_subagent_prefix
from .proc_utils import agy_app_data_dir


def _decode_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b, pos = buf[pos], pos + 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
    raise ValueError("varint overflow")

def _decode_proto(buf: bytes) -> List[Tuple[int, str, Any]]:
    pos, fields, n = 0, [], len(buf)
    while pos < n:
        tag, pos = _decode_varint(buf, pos)
        fnum, wire = tag >> 3, tag & 7
        if fnum <= 0:
            break
        if wire == 0:
            v, pos = _decode_varint(buf, pos)
            fields.append((fnum, "varint", v))
        elif wire == 2:
            ln, pos = _decode_varint(buf, pos)
            if ln < 0 or pos + ln > n:
                break
            fields.append((fnum, "bytes", buf[pos : pos + ln]))
            pos += ln
        elif wire in (1, 5) and pos + (8 if wire == 1 else 4) <= n:
            pos += 8 if wire == 1 else 4
        else:
            break
    return fields

def _proto_duration(sub: bytes) -> float:
    vals = {k: v for k, t, v in _decode_proto(sub) if t == "varint" and isinstance(v, int)} if sub else {}
    return float(vals.get(1, 0)) + float(vals.get(2, 0)) / 1e9

def _extract_agy_step_text(step_type: int, payload: Any) -> str:
    if isinstance(payload, str):
        return _strip_subagent_prefix(payload) if step_type == 14 else payload.strip()
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        return ""
    raw = bytes(payload)
    if step_type == 14:
        for k, t, v in _decode_proto(raw):
            if k == 19 and t == "bytes":
                f19 = _decode_proto(v)
                for k2, t2, v2 in f19:
                    if k2 == 2 and t2 == "bytes" and v2:
                        return _strip_subagent_prefix(v2.decode("utf-8", errors="replace"))
                chunks = [v3.decode("utf-8", errors="replace") for k2, t2, v2 in f19 if k2 == 3 and t2 == "bytes" and v2 for k3, t3, v3 in _decode_proto(v2) if k3 == 1 and t3 == "bytes" and v3]
                if chunks:
                    return _strip_subagent_prefix("".join(chunks))
    elif step_type == 15:
        for k, t, v in _decode_proto(raw):
            if k == 20 and t == "bytes":
                f20 = _decode_proto(v)
                for target_k in (1, 8):
                    for k2, t2, v2 in f20:
                        if k2 == target_k and t2 == "bytes" and v2:
                            return v2.decode("utf-8", errors="replace").strip()
    with contextlib.suppress(Exception):
        txt = raw.decode("utf-8").strip()
        if txt.startswith("{"):
            obj = json.loads(txt)
            txt = str(obj.get("text") or obj.get("prompt") or obj.get("response") or "") if isinstance(obj, dict) else txt
        return _strip_subagent_prefix(txt) if step_type == 14 else txt.strip()
    return ""

def _connect_ro(db_path: Path) -> sqlite3.Connection:
    p = db_path.resolve()
    uri, wal, shm, wal_nonempty = p.as_uri(), Path(f"{p}-wal"), Path(f"{p}-shm"), False
    with contextlib.suppress(OSError):
        wal_nonempty = wal.is_file() and wal.stat().st_size > 0
    # Immutable ignores WAL, so only use it after a checkpoint (or with no WAL).
    # Open all other cases in read-only mode, including when SQLite needs the WAL.
    read_only = f"{uri}?mode=ro"
    uris = ([read_only] if shm.exists() else [])
    if not wal_nonempty:
        uris.append(f"{read_only}&immutable=1")
    if not shm.exists():
        uris.append(read_only)
    for target in uris:
        con = None
        try:
            con = sqlite3.connect(target, uri=True, timeout=0.5)
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA schema_version").fetchone()
            return con
        except sqlite3.Error:
            if con is not None:
                con.close()
    raise sqlite3.OperationalError(f"unable to open database file: {p}")

def _inspect_agy_db(session_id: str) -> Optional[Dict[str, Any]]:
    home = Path.home()
    subdirs: List[str] = []
    for name in (agy_app_data_dir(), "antigravity-cli", "antigravity"):
        if name and name not in subdirs:
            subdirs.append(name)
    candidates = ([Path(session_id)] if session_id.endswith(".db") else []) + [
        home / ".gemini" / name / "conversations" / f"{session_id}.db" for name in subdirs
    ]
    db_path = next((p for p in candidates if p.is_file()), None)
    if db_path is None:
        return None
    db_mtime, wal_path = db_path.stat().st_mtime, Path(f"{db_path}-wal")
    with contextlib.suppress(OSError):
        if wal_path.is_file() and wal_path.stat().st_size > 0:
            db_mtime = max(db_mtime, wal_path.stat().st_mtime)
    step_ts, used_tokens, turns, last_compact_idx = 0.0, 0, [], -1
    try:
        with contextlib.closing(_connect_ro(db_path)) as con:
            cur = con.cursor()
            cur_u, cur_a, cur_ts = None, [], db_mtime
            for idx, st, meta, pl in cur.execute("SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx"):
                if isinstance(meta, (bytes, bytearray)) and meta:
                    m_map = {k: v for k, t, v in _decode_proto(bytes(meta)) if t == "bytes"}
                    ts_cand = _proto_duration(m_map.get(8, b"")) or _proto_duration(m_map.get(7, b"")) or _proto_duration(m_map.get(1, b""))
                    if ts_cand > 0:
                        step_ts, cur_ts = max(step_ts, ts_cand), ts_cand
                if st == 23 and idx is not None:
                    last_compact_idx = int(idx)
                elif st == 14:
                    u_txt = _extract_agy_step_text(14, pl)
                    if u_txt:
                        cur_u, cur_a = _append_unwrapped_user_turn(turns, cur_u, cur_a, u_txt, cur_ts)
                elif st == 15:
                    a_txt = _extract_agy_step_text(15, pl)
                    if a_txt and (not cur_a or cur_a[-1] != a_txt):
                        cur_a.append(a_txt)
            if cur_u is not None and cur_a:
                turns.append({"prompt": cur_u, "response": "\n\n".join(cur_a), "ts": cur_ts})
            with contextlib.suppress(sqlite3.Error):
                for idx, data in cur.execute("SELECT idx, data FROM gen_metadata ORDER BY idx"):
                    if last_compact_idx >= 0 and idx is not None and int(idx) <= last_compact_idx:
                        continue
                    if isinstance(data, (bytes, bytearray)) and data:
                        for k, t, v in _decode_proto(bytes(data)):
                            if k == 1 and t == "bytes":
                                f1 = {k2: v2 for k2, t2, v2 in _decode_proto(v) if t2 == "bytes"}
                                u = {k3: v3 for k3, t3, v3 in _decode_proto(f1.get(4, b"")) if t3 == "varint"}
                                used_tokens = (int(u.get(2, 0)) + int(u.get(5, 0))) or used_tokens
                    elif isinstance(data, str) and data.startswith("{"):
                        obj = json.loads(data)
                        used_tokens = int(obj.get("used_tokens") or obj.get("prompt") or used_tokens)
            if last_compact_idx >= 0 and used_tokens == 0:
                used_tokens = 14000
    except Exception:
        return None
    return {"last_active_ts": step_ts if step_ts > 0 else db_mtime, "used_tokens": used_tokens, "turns": turns}
