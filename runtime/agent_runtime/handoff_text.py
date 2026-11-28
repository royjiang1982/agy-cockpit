"""Shared subagent instructions and handoff envelope normalization."""
import re
from typing import Any, Dict, List, Optional, Tuple


SUBAGENT_PREFIX = "你是被派出的子代理（SA_SUBAGENT=1）。只遵守任务书，不派生子代理、不提交代码。"


def _strip_subagent_prefix(text: str) -> str:
    out = (text or "").strip()
    while out.startswith(SUBAGENT_PREFIX):
        out = out[len(SUBAGENT_PREFIX) :].lstrip()
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
