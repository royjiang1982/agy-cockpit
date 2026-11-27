#!/usr/bin/env python3
"""Single-process PreToolUse guard entrypoint for sa-guard.sh."""
import importlib.util
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sys

REASON = '子代理禁止派生子代理、越界访问凭据私钥、篡改护栏或执行受限高危操作（见 AGY Cockpit 子代理守卫规则）'

_AGY_SPAWN_RE = re.compile(r'^(?:define_subagent|invoke_subagent|manage_subagents|swarm|run_workflow)$')
_CODEX_SPAWN_RE = re.compile(r'spawn_agent|subagent', re.IGNORECASE)
_GROK_SPAWN_RE = re.compile(r'^(?:spawn_subagent|spawn_agent|Task|Agent)$|subagent', re.IGNORECASE)


@lru_cache(maxsize=1)
def _load_literals():
    lit_path = Path(__file__).with_name('sa-guard-literals.py')
    spec = importlib.util.spec_from_file_location('sa_guard_literals', lit_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load {lit_path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def _extract_tool_and_cmd(engine, data):
    if engine == 'agy':
        tc = data.get('toolCall') if isinstance(data.get('toolCall'), dict) else {}
        name = str(tc.get('name') or '')
        args = tc.get('args') if isinstance(tc.get('args'), dict) else {}
        cmd = str(args.get('CommandLine') or '')
        workdir = str(args.get('Cwd') or data.get('cwd') or '').strip() or None
        return name, cmd, bool(_AGY_SPAWN_RE.search(name)), workdir
    if engine == 'codex':
        name = str(data.get('tool_name') or '')
        ti = data.get('tool_input') if isinstance(data.get('tool_input'), dict) else {}
        workdir = str(ti.get('workdir') or ti.get('cwd') or data.get('cwd') or '').strip() or None
        # Codex passes apply_patch diffs in tool_input.command; payload_restricted validates targets.
        if name.strip().lower() == 'apply_patch':
            return name, '', False, workdir
        cmd = str(ti.get('command') or ti.get('cmd') or '')
        return name, cmd, bool(_CODEX_SPAWN_RE.search(name)), workdir
    name = str(data.get('toolName') or data.get('tool_name') or '')
    ti1 = data.get('toolInput') if isinstance(data.get('toolInput'), dict) else {}
    ti2 = data.get('tool_input') if isinstance(data.get('tool_input'), dict) else {}
    cmd = str(ti1.get('command') or ti1.get('cmd') or ti2.get('command') or ti2.get('cmd') or '')
    workdir = str(ti1.get('workdir') or ti1.get('cwd') or ti2.get('workdir') or ti2.get('cwd') or data.get('cwd') or '').strip() or None
    return name, cmd, bool(_GROK_SPAWN_RE.search(name)), workdir


def evaluate_guard(platform, payload, env_map=None) -> tuple[int, str, str]:
    """Return (exit code, stdout, stderr) for raw JSON or a decoded payload.

    Only the supplied environment is used when provided; never mutate process
    state or perform stream I/O. Session marker files are read, never written.
    """
    engine = platform
    if engine not in ('agy', 'codex', 'grok'):
        return 0, '', ''
    env_map = os.environ if env_map is None else env_map
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None

    if not _is_subagent(data, env_map):
        return _allow(engine, subagent=False)

    if not isinstance(data, dict):
        return _deny(engine)

    try:
        guard_lib = _load_literals()
        _, raw_cmd, spawn_blocked, workdir = _extract_tool_and_cmd(engine, data)
        if spawn_blocked or guard_lib.payload_restricted(engine, raw, base_dir=workdir):
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
