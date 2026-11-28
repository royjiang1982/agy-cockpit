"""Transparent cross-session Q&A auto-handoff when prompt cache expires and context > threshold."""
import contextlib
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agy_session_db import _inspect_agy_db
from .handoff_text import _append_unwrapped_user_turn, _strip_subagent_prefix, _unwrap_handoff_prompt
from .session_files import cached_jsonl_inspection, ledger_lock

DEFAULT_TTL_SECONDS, CODEX_TTL_SECONDS, DEFAULT_TOKEN_THRESHOLD = 600.0, 7200.0, 200_000
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

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

def _parse_iso_ts(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val) / 1000.0 if val > 1e11 else float(val)
    if isinstance(val, str) and val.strip():
        with contextlib.suppress(Exception):
            return datetime.fromisoformat(val.strip().replace("Z", "+00:00")).timestamp()
    return 0.0

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
