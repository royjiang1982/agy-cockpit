"""Transparent cross-session Q&A auto-handoff when prompt cache expires and context > threshold."""
import contextlib, hashlib, json, os, re, sqlite3, threading, time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .proc_utils import agy_app_data_dir
from .session_files import cached_jsonl_inspection, ledger_lock

DEFAULT_TTL_SECONDS, CODEX_TTL_SECONDS, DEFAULT_TOKEN_THRESHOLD = 600.0, 7200.0, 200_000
_SUBAGENT_PREFIX = "你是被派出的子代理（SA_SUBAGENT=1）。只遵守任务书，不派生子代理、不提交代码。"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_LEDGER_LOCK = threading.RLock()

def get_sessions_ledger_dir(daemon_dir: Optional[Path] = None) -> Path:
    custom, sub_dir = os.environ.get("SA_SESSIONS_LEDGER_DIR"), os.environ.get("SA_SUBAGENTD_DIR")
    base = Path(custom) if custom else ((Path(daemon_dir) if daemon_dir else (Path(sub_dir) if sub_dir else Path(f"/tmp/dev-subagentd-{os.getuid()}"))) / "sessions")
    base.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(base, 0o700)
    return base

def _ledger_path(session_id: str, daemon_dir: Optional[Path] = None) -> Path:
    sid = session_id.strip()
    if _SAFE_ID_RE.fullmatch(sid) and sid not in (".", "..") and len(sid) <= 180:
        return get_sessions_ledger_dir(daemon_dir) / f"{sid}.json"
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", sid).strip("._")[:100] or "session"
    return get_sessions_ledger_dir(daemon_dir) / f"{clean}_{hashlib.sha256(sid.encode('utf-8')).hexdigest()[:12]}.json"

def load_session_ledger(session_id: str, daemon_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = _ledger_path(session_id, daemon_dir) if (session_id and session_id.strip()) else None
    if path is not None and path.is_file():
        with contextlib.suppress(Exception):
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    return None

def save_session_ledger(record: Dict[str, Any], daemon_dir: Optional[Path] = None) -> Path:
    sid = str(record.get("session_id") or "").strip()
    if not sid:
        raise ValueError("session_id is required for ledger")
    target = _ledger_path(sid, daemon_dir)
    tmp = target.parent / f".tmp.{target.name}.{os.getpid()}.{threading.get_ident()}"
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    with ledger_lock(sid, daemon_dir):
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as wf:
                wf.write(payload)
                wf.flush()
            os.replace(tmp, target)
            with contextlib.suppress(OSError):
                os.chmod(target, 0o600)
        finally:
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()
    return target

def _strip_subagent_prefix(text: str) -> str:
    out = (text or "").strip()
    while out.startswith(_SUBAGENT_PREFIX):
        out = out[len(_SUBAGENT_PREFIX) :].lstrip()
    return out

def _unwrap_handoff_prompt(text: str, ts: float = 0.0) -> Tuple[List[Dict[str, Any]], str]:
    txt, carried = _strip_subagent_prefix(text), []
    open_tag, close_tag, next_turn = "<previous_session_history", "</previous_session_history>", "【本轮新指令】："
    while True:
        open_idx = txt.find(open_tag)
        if open_idx < 0:
            break
        close_idx = txt.find(close_tag, open_idx + len(open_tag))
        if close_idx < 0:
            break
        _, separator, new_prompt = txt[close_idx + len(close_tag):].partition(next_turn)
        if not separator:
            break
        body = txt[open_idx:close_idx]
        for seg in re.split(r"(?:^|\n+)=== 第 \d+ 轮 ===\n", body)[1:]:
            _, prompt_marker, turn = seg.partition("[主代理输入]:\n")
            p_raw, reply_marker, r_raw = turn.partition("\n[子代理回复]:")
            if prompt_marker and reply_marker:
                carried.append({"prompt": _strip_subagent_prefix(p_raw.strip()), "response": r_raw.lstrip("\n").rstrip(), "ts": ts})
        txt = _strip_subagent_prefix(new_prompt)
    return carried, txt

def _append_unwrapped_user_turn(turns: List[Dict[str, Any]], cur_u: Optional[str], cur_a: List[str], u_txt: str, cur_ts: float) -> Tuple[str, List[str]]:
    if cur_u is not None and cur_a:
        turns.append({"prompt": cur_u, "response": "\n\n".join(cur_a), "ts": cur_ts})
    carried, clean_u = _unwrap_handoff_prompt(u_txt, cur_ts)
    for ct in carried:
        if not any(t["prompt"] == ct["prompt"] and t["response"] == ct["response"] for t in turns):
            turns.append(ct)
    return clean_u, []

def _parse_iso_ts(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val) / 1000.0 if val > 1e11 else float(val)
    if isinstance(val, str) and val.strip():
        with contextlib.suppress(Exception):
            return datetime.fromisoformat(val.strip().replace("Z", "+00:00")).timestamp()
    return 0.0

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
    uris = ([f"{uri}?mode=ro"] if shm.exists() else []) + ([f"{uri}?mode=ro&immutable=1"] if not wal_nonempty else [])
    for target, is_uri in [(u, True) for u in uris] + [(str(p), False)]:
        con = None
        try:
            con = sqlite3.connect(target, uri=is_uri, timeout=0.5) if is_uri else sqlite3.connect(target, timeout=0.5)
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA schema_version").fetchone()
            return con
        except Exception:
            if con is not None:
                with contextlib.suppress(Exception):
                    con.close()
            if not is_uri:
                raise
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

def extract_event_tokens(event: Dict[str, Any]) -> int:
    if not isinstance(event, dict):
        return 0
    for top_k in ("used_tokens", "total_tokens", "totalTokens"):
        if isinstance(event.get(top_k), (int, float)) and int(event[top_k]) > 0:
            return int(event[top_k])
    candidates = (
        event.get("usage"), event.get("lastCallTokenUsage"), None if "lastCallTokenUsage" in event else event.get("tokenUsage"),
        (event.get("turn") or {}).get("usage") if isinstance(event.get("turn"), dict) else None,
        (event.get("message") or {}).get("usage") if isinstance(event.get("message"), dict) else None,
        (event.get("result") or {}).get("usage") if isinstance(event.get("result"), dict) else None,
        ((event.get("info") or {}).get("last_token_usage") if isinstance(event.get("info"), dict) else None),
    )
    for u in (c for c in candidates if isinstance(c, dict)):
        for k in ("total_tokens", "totalTokens", "used_tokens"):
            if isinstance(u.get(k), (int, float)) and int(u[k]) > 0:
                return int(u[k])
        tot = (
            int(u.get("input_tokens") or u.get("inputTokens") or u.get("input") or 0)
            + int(u.get("cache_read_input_tokens") or u.get("cacheReadTokens") or u.get("cachedReadTokens") or u.get("cacheRead") or 0)
            + int(u.get("cache_creation_input_tokens") or u.get("cacheCreationTokens") or u.get("cacheWrite") or 0)
            + int(u.get("output_tokens") or u.get("outputTokens") or u.get("output") or 0)
        )
        if tot > 0:
            return tot
    return 0

@cached_jsonl_inspection
def _inspect_jsonl_session(agent: str, session_id: str, paths) -> Optional[Dict[str, Any]]:
    jsonl_path, settings_path = paths
    if settings_path is None and jsonl_path is None:
        return None
    used_tokens, file_mtime, record_ts, turns = 0, 0.0, 0.0, []
    if settings_path and settings_path.is_file():
        file_mtime = max(file_mtime, settings_path.stat().st_mtime)
        with contextlib.suppress(Exception):
            used_tokens = max(used_tokens, extract_event_tokens(json.loads(settings_path.read_text(encoding="utf-8", errors="ignore"))))
    if jsonl_path and jsonl_path.is_file():
        file_mtime = max(file_mtime, jsonl_path.stat().st_mtime)
        cur_u, cur_a, cur_ts = None, [], file_mtime
        for raw_ln in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            ln = raw_ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            ts_val = _parse_iso_ts(obj.get("timestamp") or obj.get("createdAt"))
            if ts_val > 0:
                record_ts, cur_ts = max(record_ts, ts_val), ts_val
            otype, pl = str(obj.get("type") or ""), obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if otype in ("compaction", "compacted", "compaction_state") or pl.get("type") in ("context_compacted", "compaction") or obj.get("compactionSummaryId"):
                u_obj = obj.get("usage") if isinstance(obj.get("usage"), dict) else (pl.get("usage") if isinstance(pl.get("usage"), dict) else {})
                used_tokens = 14000 + int(u_obj.get("output") or u_obj.get("output_tokens") or u_obj.get("outputTokens") or 0)
                continue
            used_tokens = extract_event_tokens(obj) or extract_event_tokens(pl) or used_tokens
            msg = pl if (otype == "response_item" and pl.get("type") == "message") else ((obj.get("message") if isinstance(obj.get("message"), dict) else obj) if otype == "message" else None)
            if not isinstance(msg, dict):
                continue
            role, content = str(msg.get("role") or ""), msg.get("content")
            blks = [{"type": "text", "text": content}] if isinstance(content, str) else ([b for b in content if isinstance(b, dict)] if isinstance(content, list) else [])
            if role == "user" and not any(b.get("type") == "tool_result" for b in blks):
                txts = [str(b.get("text") or "") for b in blks if b.get("type") in ("text", "input_text") and not str(b.get("text") or "").lstrip().startswith(("<system-reminder>", "# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>"))]
                u_txt = _strip_subagent_prefix("".join(txts))
                if u_txt:
                    cur_u, cur_a = _append_unwrapped_user_turn(turns, cur_u, cur_a, u_txt, cur_ts)
            elif role == "assistant":
                used_tokens = extract_event_tokens(msg) or used_tokens
                a_txt = "".join(str(b.get("text") or "") for b in blks if b.get("type") in ("text", "output_text") and b.get("text")).strip()
                if a_txt and (not cur_a or cur_a[-1] != a_txt):
                    cur_a.append(a_txt)
        if cur_u is not None and cur_a:
            turns.append({"prompt": cur_u, "response": "\n\n".join(cur_a), "ts": cur_ts})
    return {"last_active_ts": record_ts if record_ts > 0 else file_mtime, "used_tokens": used_tokens, "turns": turns}

def inspect_local_session(agent: str, session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id or not session_id.strip():
        return None
    ag = (agent or "agy").strip().lower()
    return _inspect_agy_db(session_id.strip()) if ag == "agy" else _inspect_jsonl_session(ag, session_id.strip())

def format_handoff_prompt(old_sid: str, turns: List[Dict[str, Any]], current_prompt: str) -> str:
    lines = [f'<previous_session_history from_session="{old_sid}" turns="{len(turns)}">', "以下为前序子代理会话（因缓存过期或配额不足等原因已自动切换新会话）的全部历史问答记录，请直接基于这些前序上下文继续执行末尾的【本轮新指令】：", ""]
    for idx, t in enumerate(turns, 1):
        lines.extend([f"=== 第 {idx} 轮 ===", "[主代理输入]:", str(t.get("prompt") or ""), "[子代理回复]:", str(t.get("response") or ""), ""])
    if lines[-1] == "":
        lines.pop()
    return "\n".join(lines + ["</previous_session_history>", "", "【本轮新指令】：", current_prompt])

def prepare_session_handoff(agent: str, session_id: str, raw_prompt: str, daemon_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    sid = (session_id or "").strip()
    if not sid or os.environ.get("SA_DISABLE_AUTO_HANDOFF") == "1":
        return None
    ag = (agent or "agy").strip().lower()
    ttl_env, thresh_env = os.environ.get("SA_HANDOFF_TTL_SEC", "").strip(), os.environ.get("SA_HANDOFF_TOKEN_THRESHOLD", "").strip()
    ttl_sec = float(ttl_env) if ttl_env else (CODEX_TTL_SECONDS if ag == "codex" else DEFAULT_TTL_SECONDS)
    threshold = int(thresh_env) if thresh_env else DEFAULT_TOKEN_THRESHOLD
    ledger, local = load_session_ledger(sid, daemon_dir) or {}, inspect_local_session(ag, sid) or {}
    led_ts, loc_ts = float(ledger.get("updated_at") or 0.0), float(local.get("last_active_ts") or 0.0)
    led_tok, loc_tok = int(ledger.get("used_tokens") or 0), int(local.get("used_tokens") or 0)
    last_active_ts, used_tokens = max(led_ts, loc_ts), (loc_tok if (loc_tok > 0 and loc_ts >= led_ts - 1.0) else max(led_tok, loc_tok))
    turns = list(ledger.get("turns") or local.get("turns") or [])
    idle_sec = max(0.0, time.time() - last_active_ts) if last_active_ts > 0.0 else 0.0
    if last_active_ts <= 0.0 or not turns or idle_sec <= ttl_sec or used_tokens <= threshold:
        return None
    evt = {"event": "session_handoff", "from_session_id": sid, "idle_sec": round(idle_sec, 1), "used_tokens": used_tokens, "turns_carried": len(turns)}
    return {"from_session_id": sid, "effective_prompt": format_handoff_prompt(sid, turns, raw_prompt), "carried_turns": turns, "idle_sec": idle_sec, "used_tokens": used_tokens, "event": evt}

def seed_handoff_ledger(agent: str, new_sid: str, handoff_info: Optional[Dict[str, Any]], daemon_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    sid = (new_sid or "").strip()
    if not sid or not handoff_info:
        return None
    with ledger_lock(sid, daemon_dir):
        existing = load_session_ledger(sid, daemon_dir) or {}
        if existing.get("turns"):
            return existing
        record = {"agent": (agent or "agy").strip().lower(), "session_id": sid, "updated_at": time.time(), "used_tokens": int(existing.get("used_tokens") or 0), "turns": list(handoff_info.get("carried_turns") or []), "handoff_from": str(handoff_info.get("from_session_id") or "")}
        save_session_ledger(record, daemon_dir)
        return record

def record_turn_completion(agent: str, final_sid: str, raw_prompt: str, response_text: str, stream_tokens: int = 0, handoff_info: Optional[Dict[str, Any]] = None, daemon_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    sid = (final_sid or "").strip()
    if not sid:
        return None
    ag, now_ts = (agent or "agy").strip().lower(), time.time()
    local = inspect_local_session(ag, sid) or {}
    with ledger_lock(sid, daemon_dir):
        existing = load_session_ledger(sid, daemon_dir) or {}
        latest_tok = int(local.get("used_tokens") or 0) or int(stream_tokens or 0)
        _, clean_prompt = _unwrap_handoff_prompt(raw_prompt, now_ts)
        if handoff_info:
            turns, handoff_from, used_tokens = list(handoff_info.get("carried_turns") or []), str(handoff_info.get("from_session_id") or ""), latest_tok
        else:
            l_turns = list(local.get("turns") or [])
            if l_turns and str(l_turns[-1].get("prompt") or "").strip() == clean_prompt.strip():
                l_turns = l_turns[:-1]
            turns, handoff_from = list(existing["turns"]) if existing.get("turns") else l_turns, str(existing.get("handoff_from") or "")
            used_tokens = latest_tok if latest_tok > 0 else int(existing.get("used_tokens") or 0)
        turns.append({"prompt": clean_prompt, "response": response_text, "ts": now_ts})
        record = {"agent": ag, "session_id": sid, "updated_at": now_ts, "used_tokens": used_tokens, "turns": turns, "handoff_from": handoff_from}
        save_session_ledger(record, daemon_dir)
        return record

from .history_merge import build_forced_handoff_prompt
