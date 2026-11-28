#!/usr/bin/env python3
"""Single-process PreToolUse guard entrypoint for sa-guard.sh."""
from importlib import import_module
import json
import os
from pathlib import Path
import re
import sys

REASON = '子代理禁止派生子代理、越界访问凭据私钥、篡改护栏或执行受限高危操作（见 AGY Cockpit 子代理守卫规则）'

_AGY_SPAWN_RE = re.compile(r'^(?:define_subagent|invoke_subagent|manage_subagents|swarm|run_workflow)$')
_CODEX_SPAWN_RE = re.compile(r'spawn_agent|subagent', re.IGNORECASE)
_GROK_SPAWN_RE = re.compile(r'^(?:spawn_subagent|spawn_agent|Task|Agent)$|subagent', re.IGNORECASE)


_SPAWN_RE = {'agy': _AGY_SPAWN_RE, 'codex': _CODEX_SPAWN_RE, 'grok': _GROK_SPAWN_RE}
_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)


def _load_literals():
    return import_module('sa-guard-literals')


def _extract_session_id(data):
    for key in ('conversationId', 'session_id', 'threadId'):
        val = data.get(key)
        if val is not None and val is not False and str(val) != '':
            cleaned = re.sub(r'[^A-Za-z0-9._-]', '', str(val))
            if cleaned:
                return cleaned
    return ''


def _is_swarm_worker(sid, env_map):
    brain_root = Path(env_map.get('ZOMI_SWARM_BRAIN_DIR') or (Path.home() / '.gemini' / 'antigravity' / 'brain'))
    if not brain_root.is_dir():
        return False
    try:
        rosters = list(brain_root.glob('*/.system_generated/swarm/members.json'))
    except OSError:
        return False
    for roster in rosters:
        coord_id = roster.parents[2].name
        if coord_id == sid:
            continue
        try:
            doc = json.loads(roster.read_text(encoding='utf-8'))
        except Exception:
            continue
        members = doc.get('members') if isinstance(doc, dict) else None
        if not isinstance(members, dict):
            continue
        for entry in members.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get('role') or '').lower() == 'coordinator':
                continue
            if str(entry.get('conversation_id') or '').strip() == sid:
                return True
    return False


def _is_subagent(data, env_map):
    if env_map.get('SA_SUBAGENT') == '1':
        return True
    if not isinstance(data, dict):
        return False
    sid = _extract_session_id(data)
    if not sid:
        return False
    sess_dir = env_map.get('ZOMI_SUBAGENT_SESSIONS_DIR') or '/tmp/zomi-subagent-sessions'
    if os.path.exists(os.path.join(sess_dir, sid)):
        return True
    return _is_swarm_worker(sid, env_map)


def _deny(engine):
    message = json.dumps({'decision': 'deny', 'reason': REASON}, ensure_ascii=False, separators=(',', ':')) + '\n'
    if engine == 'agy':
        return 0, message, ''
    if engine == 'codex':
        return 2, '', REASON + '\n'
    if engine == 'grok':
        return 2, message, ''
    return 0, '', ''


def _allow(engine, subagent):
    stdout = ('{"decision":"allow"}\n' if subagent else '{"decision":"ask"}\n') if engine == 'agy' else ''
    return 0, stdout, ''


def evaluate_guard(platform, payload, env_map=None) -> tuple[int, str, str]:
    """Return (exit code, stdout, stderr) for raw JSON or a decoded payload.

    Only the supplied environment is used when provided; never mutate process
    state or perform stream I/O. Session marker files are read, never written.
    """
    engine = platform
    if engine not in ('agy', 'codex', 'grok'):
        return 0, '', ''
    env_map = os.environ if env_map is None else env_map
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except (ValueError, TypeError, RecursionError):
        data = None

    if not isinstance(data, dict):
        if env_map.get('SA_SUBAGENT') == '1' or env_map.get('SA_GUARD_SUBAGENT_HINT') == '1':
            return _deny(engine)
        return _allow(engine, subagent=False)

    if not _is_subagent(data, env_map):
        return _allow(engine, subagent=False)

    try:
        guard_lib = _load_literals()
        name, raw_args, raw_cmd, workdir = guard_lib.unpack_tool(engine, data)
        if _SPAWN_RE[engine].search(name) or guard_lib.tool_paths_restricted(name, raw_args, base_dir=workdir):
            return _deny(engine)
        if raw_cmd.strip() and guard_lib.restricted(raw_cmd + '\n', base_dir=workdir):
            return _deny(engine)
    except Exception:
        return _deny(engine)

    return _allow(engine, subagent=True)


def main():
    engine = sys.argv[1] if len(sys.argv) > 1 else ''
    if engine not in ('agy', 'codex', 'grok'):
        return 0
    code, stdout, stderr = evaluate_guard(engine, sys.stdin.read())
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == '__main__':
    sys.exit(main())
