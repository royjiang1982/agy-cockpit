"""Guard payloads, session detection, and real shell-adapter smoke tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from _runtime_fixture import isolated_env


HOOK = Path(__file__).resolve().parents[1] / 'guard' / 'sa-guard.sh'
SPEC = importlib.util.spec_from_file_location('sa_guard_hook', HOOK.with_name('sa-guard-hook.py'))
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)
SANDBOX = tempfile.TemporaryDirectory(prefix="sa-guard-test-")
GUARD_HOME = Path(SANDBOX.name)
PLATFORMS = ("agy", "codex", "grok")
# Path classifiers capture HOME on first use, so initialize their cache in the sandbox.
with patch.dict(os.environ, isolated_env(GUARD_HOME), clear=True):
    GUARD.evaluate_guard("agy", "{}", {"SA_SUBAGENT": "1"})
CALLS = {'in_process': 0, 'subprocess': 0}


def guard_env(subagent=True, extra_env=None):
    env = isolated_env(GUARD_HOME, {"SA_SUBAGENT": "1" if subagent else "0", "SA_GUARD_LOG": os.devnull})
    env.update(extra_env or {})
    if subagent is None:
        env.pop('SA_SUBAGENT', None)
    return env


def run_hook(platform, payload, subagent=True, extra_env=None, real=False):
    env = guard_env(subagent, extra_env)
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    if real or os.environ.get('SA_GUARD_TEST_SUBPROCESS_ALL') == '1':
        CALLS['subprocess'] += 1
        return subprocess.run([str(HOOK), platform], input=raw, text=True,
                              capture_output=True, env=env, timeout=5)
    CALLS['in_process'] += 1
    code, stdout, stderr = GUARD.evaluate_guard(platform, raw, env)
    return subprocess.CompletedProcess([str(HOOK), platform], code, stdout, stderr)


def denied(result):
    return result.returncode == 2 or '"deny"' in result.stdout


def blocked(platform, command='', name='exec_command', subagent=True, cmd_key=False):
    key = 'cmd' if cmd_key else 'command'
    if platform == 'agy':
        payload = {'toolCall': {'name': name, 'args': {'CommandLine': command}}}
    elif platform == 'codex':
        payload = {'tool_name': name, 'tool_input': {key: command}}
    else:
        payload = {'toolName': name, 'toolInput': {key: command}}
    return denied(run_hook(platform, payload, subagent=subagent))


class GuardAdapterCases:
    def test_tool_adapters_and_parent(self):
        for platform, name in (('agy', 'invoke_subagent'), ('codex', 'spawn_agent'), ('grok', 'spawn_subagent')):
            self.assertTrue(blocked(platform, name=name))
            self.assertFalse(blocked(platform, 'printf hello > tmp/test'))
            self.assertFalse(blocked(platform, 'codex exec test', subagent=False))
            self.assertTrue(blocked(platform, 'codex exec test', cmd_key=platform != 'agy'))
        self.assertTrue(denied(run_hook('grok', {
            'tool_name': 'exec_command', 'tool_input': {'cmd': 'codex exec test'},
        })))

    def test_session_markers_and_parent_fast_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker_sid = 'sub-sid-123'
            other_sid = 'other-sid-456'
            (Path(tmpdir) / marker_sid).write_text('')
            (Path(tmpdir) / other_sid).write_text('')
            env = {'ZOMI_SUBAGENT_SESSIONS_DIR': tmpdir}

            # Subagent detected via marker file when SA_SUBAGENT=0
            for platform, sid_key in (('agy', 'conversationId'), ('codex', 'threadId'), ('grok', 'session_id')):
                safe_payload = {
                    sid_key: marker_sid,
                    'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'git status --short'}},
                    'tool_name': 'exec_command', 'tool_input': {'command': 'git status --short'},
                    'toolName': 'exec_command', 'toolInput': {'command': 'git status --short'},
                }
                res = run_hook(platform, safe_payload, subagent=False, extra_env=env)
                self.assertEqual(res.returncode, 0)
                if platform == 'agy':
                    self.assertEqual(json.loads(res.stdout), {'decision': 'allow'})

                bad_payload = {
                    sid_key: marker_sid,
                    'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'git push'}},
                    'tool_name': 'exec_command', 'tool_input': {'command': 'git push'},
                    'toolName': 'exec_command', 'toolInput': {'command': 'git push'},
                }
                bad_res = run_hook(platform, bad_payload, subagent=False, extra_env=env)
                self.assertTrue(bad_res.returncode == 2 or '"deny"' in bad_res.stdout)

            # JSON unicode escape (\u002d) and stripped chars in raw payload must still match marker file
            for escaped_raw in (
                '{"conversationId":"sub\\u002dsid-123","toolCall":{"name":"run_command","args":{"CommandLine":"git push"}}}',
                '{"conversationId":"sub-sid -123!","toolCall":{"name":"run_command","args":{"CommandLine":"git push"}}}',
            ):
                esc_res = run_hook('agy', escaped_raw, subagent=False, extra_env=env)
                self.assertIn('"deny"', esc_res.stdout)

            # Parent mentioning subagent sid in CommandLine must NOT be falsely treated as subagent
            parent_cmd = f'~/Dev/_shared/agent-runtime/bin/sa-agy --conversation "{marker_sid}" --prompt "hi\\n\\\\test"'
            for platform, sid_key in (('agy', 'conversationId'), ('codex', 'threadId'), ('grok', 'session_id')):
                parent_payload = {
                    sid_key: 'parent-conv-999',
                    'toolCall': {'name': 'run_command', 'args': {'CommandLine': parent_cmd}},
                    'tool_name': 'exec_command', 'tool_input': {'command': parent_cmd},
                    'toolName': 'exec_command', 'toolInput': {'command': parent_cmd},
                }
                res = run_hook(platform, parent_payload, subagent=False, extra_env=env)
                self.assertEqual(res.returncode, 0)
                if platform == 'agy':
                    self.assertEqual(json.loads(res.stdout), {'decision': 'ask'})
                else:
                    self.assertEqual(res.stdout, '')

        # Empty/missing sessions dir fast-paths cleanly
        empty_env = {'ZOMI_SUBAGENT_SESSIONS_DIR': str(GUARD_HOME / 'missing')}
        agy_parent = run_hook('agy', {'conversationId': 'p1', 'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'git push'}}}, subagent=False, extra_env=empty_env, real=True)
        self.assertEqual(agy_parent.returncode, 0)
        self.assertEqual(json.loads(agy_parent.stdout), {'decision': 'ask'})
        codex_parent = run_hook('codex', {'threadId': 'p1', 'tool_name': 'exec_command', 'tool_input': {'command': 'git push'}}, subagent=False, extra_env=empty_env, real=True)
        self.assertEqual(codex_parent.returncode, 0)
        self.assertEqual(codex_parent.stdout, '')


    def test_real_adapter_smoke(self):
        with tempfile.TemporaryDirectory(prefix='sa-guard-markers-') as marked:
            (Path(marked) / 'sub-sid-123').touch()
            for platform in PLATFORMS:
                bad = json.dumps({'conversationId': 'sub-sid-123', 'threadId': 'sub-sid-123',
                                  'session_id': 'sub-sid-123', 'tool_name': 'spawn_agent',
                                  'toolName': 'spawn_subagent', 'toolCall': {'name': 'invoke_subagent'}})
                bad = bad.replace('sub-sid-123', r'sub\u002dsid-123')
                unmarked = str(GUARD_HOME / "markers")
                cases = (
                    ("error", True, unmarked, "{", True, True),
                    ("allow", True, unmarked, "{}", False, True),
                    ("deny", False, marked, bad, True, True),
                    ("no_marker", False, unmarked, bad, False, False),
                    ("other_marker", False, marked, bad.replace("123", "999"), False, False),
                )
                for scenario, subagent, directory, raw, deny, is_child in cases:
                    with self.subTest(platform=platform, scenario=scenario):
                        env = {"ZOMI_SUBAGENT_SESSIONS_DIR": directory}
                        before = dict(os.environ)
                        expected = GUARD.evaluate_guard(platform, raw, guard_env(subagent, env))
                        self.assertEqual(dict(os.environ), before)
                        if scenario in {"error", "allow", "deny"}:
                            result = run_hook(platform, raw, subagent, env, real=True)
                            self.assertEqual((result.returncode, result.stdout, result.stderr), expected)
                        result = subprocess.CompletedProcess([], *expected)
                        self.assertEqual(result.returncode, 2 if deny and platform != 'agy' else 0)
                        self.assertEqual(result.stderr, GUARD.REASON + '\n' if deny and platform == 'codex' else '')
                        if platform == 'agy' or (deny and platform == 'grok'):
                            self.assertEqual(json.loads(result.stdout)['decision'],
                                             'deny' if deny else 'allow' if is_child else 'ask')
                        else:
                            self.assertEqual(result.stdout, '')

    def test_adapter_failure_denies_potential_subagents(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            fake = root / 'python3'
            fake.touch()
            fake.chmod(0o700)
            brain = root / 'brain'
            roster = brain / 'coordinator' / '.system_generated/swarm/members.json'
            roster.parent.mkdir(parents=True)
            marker = root / 'markers' / 'worker'
            marker.parent.mkdir()
            env = {'PATH': f'{root}{os.pathsep}{os.defpath}', 'ZOMI_SWARM_BRAIN_DIR': str(brain),
                   'ZOMI_SUBAGENT_SESSIONS_DIR': str(marker.parent)}
            for code in (2, 7):
                fake.write_text(f'#!/bin/sh\nexit {code}\n')
                sources = ('none', 'env', 'marker', 'roster') if code == 2 else ('env',)
                for source in sources:
                    if source == 'marker':
                        marker.touch()
                    if source == 'roster':
                        marker.unlink()
                        roster.touch()
                    with self.subTest(code=code, source=source):
                        result = run_hook('agy', {}, source == 'env', env, real=True)
                        self.assertEqual(result.returncode, 0)
                        self.assertEqual(json.loads(result.stdout)['decision'], 'ask' if source == 'none' else 'deny')
                roster.unlink(missing_ok=True)
                marker.touch()
                for platform in ('codex', 'grok'):
                    with self.subTest(code=code, platform=platform):
                        result = run_hook(platform, {}, False, env, real=True)
                        self.assertEqual(result.returncode, 2)
                        if platform == 'grok':
                            self.assertEqual(json.loads(result.stdout)['decision'], 'deny')
                        else:
                            self.assertEqual(result.stdout, '')
                marker.unlink()

    def test_unreadable_payload_denied_with_marker_or_swarm_hint(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            marker = root / 'markers' / 'worker'
            marker.parent.mkdir()
            marker.touch()
            roster = root / 'brain' / 'coordinator' / '.system_generated/swarm/members.json'
            roster.parent.mkdir(parents=True)
            roster.write_text('{"members":{"worker":{"conversation_id":"worker"}}}')
            env = {'ZOMI_SUBAGENT_SESSIONS_DIR': str(marker.parent), 'ZOMI_SWARM_BRAIN_DIR': str(root / 'brain')}
            for source, platforms in (('marker', PLATFORMS), ('roster', ('agy',))):
                if source == 'roster':
                    marker.unlink()
                for platform in platforms:
                    for raw in ('', '{', 'null'):
                        with self.subTest(source=source, platform=platform, raw=raw):
                            # Only the shell can supply the hint; one malformed frame checks
                            # that wiring, and the hook matrix covers all payload shapes.
                            real = raw == '{'
                            hinted = env if real else {**env, 'SA_GUARD_SUBAGENT_HINT': '1'}
                            result = run_hook(platform, raw, False, hinted, real=real)
                            self.assertEqual(result.returncode, 0 if platform == 'agy' else 2)
                            if platform == 'codex':
                                self.assertEqual((result.stdout, result.stderr), ('', GUARD.REASON + '\n'))
                            else:
                                self.assertEqual(json.loads(result.stdout)['decision'], 'deny')
            # A valid request from a different session must not inherit the hint.
            parent = run_hook('agy', {'conversationId': 'parent'}, False, env, real=True)
            self.assertEqual(json.loads(parent.stdout), {'decision': 'ask'})

    def test_real_agy_swarm_worker_without_subagent_marker(self):
        with tempfile.TemporaryDirectory(prefix='sa-guard-swarm-') as brain_tmp:
            coord_id = 'coord-conv-001'
            worker_id = 'worker-conv-002'
            swarm_dir = Path(brain_tmp) / coord_id / '.system_generated' / 'swarm'
            swarm_dir.mkdir(parents=True)
            (swarm_dir / 'members.json').write_text(json.dumps({
                'title': 'test',
                'members': {
                    'coordinator': {'conversation_id': coord_id, 'role': 'coordinator'},
                    'worker-1': {'conversation_id': worker_id, 'role': 'worker'},
                },
            }), encoding='utf-8')
            tools = (
                {'name': 'swarm', 'args': {}},
                {'name': 'run_command', 'args': {'CommandLine': 'git push'}},
            )
            env = {'ZOMI_SWARM_BRAIN_DIR': brain_tmp}
            for sid, decision in ((worker_id, 'deny'), (coord_id, 'ask'), ('nonmember', 'ask')):
                for tool in tools:
                    with self.subTest(sid=sid, tool=tool['name']):
                        payload = {'conversationId': sid, 'toolCall': tool}
                        result = run_hook('agy', payload, subagent=None, extra_env=env, real=tool['name'] == 'swarm')
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout)['decision'], decision)
