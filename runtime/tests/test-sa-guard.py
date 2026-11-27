#!/usr/bin/env python3
"""In-process guard matrices plus real adapter smoke tests; inputs never execute."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from sa_guard_heredoc_cases import run_heredoc_and_perm_cases

HOOK = Path(__file__).resolve().parents[1] / 'guard' / 'sa-guard.sh'
SPEC = importlib.util.spec_from_file_location('sa_guard_hook', HOOK.with_name('sa-guard-hook.py'))
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)
SESSIONS = tempfile.TemporaryDirectory(prefix='sa-guard-test-')
CALLS = {'in_process': 0, 'subprocess': 0}


def run_hook(platform, payload, subagent=True, extra_env=None, real=False):
    env = {**os.environ, 'SA_SUBAGENT': '1' if subagent else '0',
           'ZOMI_SUBAGENT_SESSIONS_DIR': SESSIONS.name, 'SA_GUARD_LOG': '/dev/null'}
    env.update(extra_env or {})
    if subagent is None:
        env.pop('SA_SUBAGENT', None)
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    if real or os.environ.get('SA_GUARD_TEST_SUBPROCESS_ALL') == '1':
        CALLS['subprocess'] += 1
        return subprocess.run([str(HOOK), platform], input=raw, text=True,
                              capture_output=True, env=env, timeout=5)
    CALLS['in_process'] += 1
    code, stdout, stderr = GUARD.evaluate_guard(platform, raw, env)
    return subprocess.CompletedProcess([str(HOOK), platform], code, stdout, stderr)


def blocked(platform, command='', name='exec_command', subagent=True, cmd_key=False):
    args = {'cmd' if cmd_key else 'command': command}
    payload = {'tool_name': name, 'tool_input': args, 'toolName': name,
               'toolInput': args, 'toolCall': {'name': name, 'args': {'CommandLine': command}}}
    result = run_hook(platform, payload, subagent=subagent)
    return result.returncode == 2 or '"deny"' in result.stdout


class GuardTests(unittest.TestCase):
    def test_literal_file_data(self):
        for platform in ('agy', 'codex', 'grok'):
            for quote in ("'", '"'):
                command = f"cat > tmp/test.mjs <<{quote}EOF{quote}\nconst pattern = /(?:env|pi)(?:/|$)/;\nconst example = 'codex exec test';\n$(grok run)\nEOF\n"
                with self.subTest(platform=platform, quote=quote):
                    self.assertFalse(blocked(platform, command))

    def test_executables_still_blocked(self):
        for platform in ('agy', 'codex', 'grok'):
            for command in ('codex exec test', '/usr/local/bin/agy -p test',
                            'echo test | pi', 'env SA_SUBAGENT=1 grok test',
                            'sh -c "codex exec test"', 'echo "$(codex exec test)"',
                            '/bin/zsh -lc "\"bin/zomi-agy-acp\" --prompt test"',
                            'npx -y @anthropic/claude -p test',
                            'xargs -n 1 ~/Dev/_shared/agent-runtime/bin/sa-agy',
                            "python3 - <<'PY'\nimport os\nos.system('codex exec test')\nPY",
                            "cat > tmp/a <<EOF\n$(codex exec test)\nEOF",
                            "cat > tmp/a <<'EOF'\ntext\nEOF\ncodex exec test",
                            "cat > tmp/a <<'EOF'; codex exec test\ntext\nEOF",
                            "cat > $(codex exec test) <<'EOF'\ntext\nEOF",
                            "cat > tmp/a <<'EOF'\ncodex exec test\nMISSING",
                            "echo \"\ncat > tmp/a <<'EOF'\n\"; codex exec test\nEOF"):
                with self.subTest(platform=platform, command=command):
                    self.assertTrue(blocked(platform, command))

    def test_high_risk_commands(self):
        commands = (
            'git commit -m test', 'git -C ~/Dev/_governance push',
            'git -c a=b --no-pager --git-dir=.git --work-tree=. pull',
            'git branch -D feat', 'git worktree remove foo', 'gh pr create',
            'git merge main', 'git rebase main', 'git cherry-pick abc',
            'git am patch', 'git tag v1', 'git reset --hard', 'git clean -fx',
            'git restore src/a.ts', 'git restore --staged src/a.ts',
            'git checkout -- src/a.ts', 'git checkout HEAD -- src/a.ts',
            'git stash', 'git stash push -m test', 'git stash save test',
            'git stash drop', 'git stash clear', 'git stash pop', 'git stash apply',
            'git worktree add foo', 'git worktree prune', 'git worktree move a b',
            'git branch -m renamed', 'gh release list',
            'gh pr merge', 'gh pr close', 'gh pr ready',
            'rm -rf src/index.ts', 'rm -rf tmp/../src', 'rm -f README.md',
            'rm -rf /', 'rm -rf ~', 'rm -rf .', 'rm -rf ..', 'rm -rf .git',
            'rm -rf /tmp', 'rm -rf /tmp/',
            'rm -rf tmp/a src', 'rm -rf "/tmp/../src"', 'rm -rf tmp/$DIR', 'rm -rf',
            '~/Dev/infra-ops/remote-dev/scripts/publish.sh dist', 'railway up',
            'railway down', 'railway delete', 'railway redeploy',
            'gcloud compute instances stop vm', 'gcloud compute instances delete vm',
            'gcloud compute instances reset vm',
            'sudo env FOO=bar exec nohup command time git commit -m test',
            'env -i FOO=bar git push', 'sudo -u root git push',
            'echo ok; git push', 'true && rm src/a', 'echo "$(git push)"',
            'cat > tmp/a <<EOF\n$(rm -rf /)\nEOF',
            'sh -c "git commit -m test"', 'bash -c "git push"',
            'zsh -c "rm src/a"', 'eval "git push"',
            "python3 - <<'PY'\nimport os\nos.system('git push')\nPY",
        )
        for platform in ('agy', 'codex', 'grok'):
            for command in commands:
                with self.subTest(platform=platform, command=command):
                    self.assertTrue(blocked(platform, command))
                    self.assertFalse(blocked(platform, command, subagent=False))

    def test_safe_commands(self):
        commands = (
            'git status --short', 'git -C /Users/royjiang/Dev diff HEAD',
            'git log -n 3', 'git rev-parse --show-toplevel', 'git branch --show-current',
            'git branch', 'git worktree list', 'git show HEAD', 'git grep test',
            'git stash list', 'git stash show -p',
            'ego-browser open https://example.com', 'ego-browser snapshot -i',
            'rm -rf tmp/test-build', 'rm -f /tmp/zomi-test.json ./tmp/out.log',
            'rm -- "./tmp/a b"', 'rm -rf /some/project/tmp/build',
            'rm -rf tmp', 'rm -rf ./tmp',
            'echo git push', 'printf rm', 'echo railway up',
            'rg "git commit" dispatch.md', 'echo "publish.sh"',
            'rg -n "gemini-4-argon" bin/zomi-agy-acp server.py',
            '/bin/zsh -lc "rg -n \'gemini-4-argon\' \'agent-runtime/bin/sa-agy\' && sed -n \'1,30p\' \'agent-runtime/bin/sa-agy\'"',
            'git diff -- agent-runtime/bin/sa-agy bin/zomi-agy-acp',
            "cat > tmp/a <<'EOF'\ngit push\nrm -rf /\nEOF",
        )
        for platform in ('agy', 'codex', 'grok'):
            for command in commands:
                with self.subTest(platform=platform, command=command):
                    self.assertFalse(blocked(platform, command))

    def test_tool_adapters_and_parent(self):
        for platform, name in (('agy', 'invoke_subagent'), ('codex', 'spawn_agent'), ('grok', 'spawn_subagent')):
            self.assertTrue(blocked(platform, name=name))
            self.assertFalse(blocked(platform, 'printf hello > tmp/test'))
            self.assertFalse(blocked(platform, 'codex exec test', subagent=False))
            self.assertTrue(blocked(platform, 'codex exec test', cmd_key=True))

    def _run_payload(self, platform, payload, subagent=True):
        result = run_hook(platform, payload, subagent=subagent)
        return result.returncode == 2 or '"deny"' in result.stdout

    def _file_payload(self, platform, tool_name, path_val, extra_args=None):
        extra = dict(extra_args or {})
        if platform == 'agy':
            args = {'Path': path_val, **extra}
            return {'toolCall': {'name': tool_name, 'args': args}}
        if platform == 'codex':
            args = {'file_path': path_val, **extra}
            return {'tool_name': tool_name, 'tool_input': args}
        args = {'path': path_val, **extra}
        return {'toolName': tool_name, 'toolInput': args}

    def test_sensitive_paths_blocked_across_tools(self):
        sensitive = (
            '~/.ssh/id_rsa',
            '$HOME/.aws/credentials',
            '~/.gnupg/secring.gpg',
            '~/.kube/config',
            '~/.codex/auth.json',
            '~/.gemini/oauth_creds.json',
            '~/.gemini/google_accounts.json',
            '/Users/royjiang/Dev/app/.env',
            '/Users/royjiang/Dev/app/.env.production.local',
            '/Users/royjiang/Dev/app/certs/private.pem',
            '/Users/royjiang/Dev/app/certs/server.key',
            '/Users/royjiang/Dev/app/../app/../../.ssh/id_ed25519',
        )
        for platform in ('agy', 'codex', 'grok'):
            for path_val in sensitive:
                for tool_name in ('read_file', 'write_file', 'edit_file'):
                    with self.subTest(platform=platform, tool=tool_name, path=path_val):
                        self.assertTrue(self._run_payload(platform, self._file_payload(platform, tool_name, path_val)))
                        self.assertFalse(self._run_payload(platform, self._file_payload(platform, tool_name, path_val), subagent=False))

    def test_guard_files_read_allowed_write_blocked(self):
        guard_targets = (
            '~/Dev/_governance/scripts/sa-guard.sh',
            '~/Dev/_governance/scripts/sa-guard-hook.py',
            '~/Dev/_governance/scripts/sa-guard-literals.py',
            '~/Dev/_governance/adapters/GEMINI.md',
            '~/.gemini/config/hooks.json',
            '~/.gemini/config/rules/AGENTS.md',
            '~/.codex/hooks.json',
            '~/.grok/config.toml',
        )
        for platform in ('agy', 'codex', 'grok'):
            for target in guard_targets:
                with self.subTest(platform=platform, target=target):
                    self.assertFalse(self._run_payload(platform, self._file_payload(platform, 'read_file', target)))
                    self.assertTrue(self._run_payload(platform, self._file_payload(platform, 'edit_file', target)))
                    self.assertTrue(self._run_payload(platform, self._file_payload(platform, 'write_file', target)))

        # Codex apply_patch header targeting guard or sensitive file must be blocked (both patch and command fields)
        for key in ('patch', 'command'):
            patch_payload = {
                'tool_name': 'apply_patch',
                'tool_input': {key: '*** Begin Patch\n*** Update File: ~/.gemini/config/hooks.json\n@@\n*** End Patch'},
            }
            self.assertTrue(self._run_payload('codex', patch_payload))
            move_payload = {
                'tool_name': 'apply_patch',
                'tool_input': {key: '*** Begin Patch\n*** Update File: tmp/a.py\n*** Move to: ~/Dev/_governance/scripts/sa-guard-hook.py\n@@\n*** End Patch'},
            }
            self.assertTrue(self._run_payload('codex', move_payload))
            env_payload = {
                'tool_name': 'apply_patch',
                'tool_input': {key: '*** Begin Patch\n*** Update File: /Users/royjiang/Dev/app/.env\n+SECRET=1\n*** End Patch'},
            }
            self.assertTrue(self._run_payload('codex', env_payload))

        # Codex apply_patch adding Python code mentioning CLI names or shell syntax must be allowed
        safe_patch_payload = {
            'tool_name': 'apply_patch',
            'tool_input': {
                'command': (
                    '*** Begin Patch\n'
                    '*** Add File: backend/agy_send_policy.py\n'
                    '+def probe():\n'
                    '+    exe = shutil.which("agy") or "codex"\n'
                    '+    # example: git commit / rm -rf /\n'
                    "+    return exe + '\\''\n"
                    '*** End Patch'
                ),
            },
        }
        self.assertFalse(self._run_payload('codex', safe_patch_payload))
        # Real Bash commands masquerading with a Begin Patch header still run command checks
        fake_bash_patch = {
            'tool_name': 'Bash',
            'tool_input': {'command': '*** Begin Patch\ngit push'},
        }
        self.assertTrue(self._run_payload('codex', fake_bash_patch))

    def test_safe_files_and_skills_allowed(self):
        safe_paths = (
            '/Users/royjiang/Dev/app/.env.example',
            '/Users/royjiang/Dev/app/.env.template',
            '/Users/royjiang/Dev/app/src/keyboard.ts',
            '/Users/royjiang/Dev/app/docs/api-key-guide.md',
            '~/.gemini/config/skills/ego-browser/SKILL.md',
            '~/.gemini/antigravity/builtin/skills/native_swarm/SKILL.md',
            '~/Dev/_governance/dispatch.md',
        )
        for platform in ('agy', 'codex', 'grok'):
            for path_val in safe_paths:
                for tool_name in ('read_file', 'edit_file'):
                    with self.subTest(platform=platform, tool=tool_name, path=path_val):
                        self.assertFalse(self._run_payload(platform, self._file_payload(platform, tool_name, path_val)))

    def test_command_secret_and_guard_protection(self):
        blocked_cmds = (
            'cat ~/.ssh/id_rsa',
            'head -n 5 ~/.codex/auth.json',
            'cat .env',
            'python3 -c "print(open(\'.env.local\').read())"',
            'rg foo ~/.ssh/',
            'echo x > ~/.gemini/config/hooks.json',
            'tee ~/Dev/_governance/scripts/sa-guard.sh',
            '/bin/zsh -lc "echo ok"; git push',
            '/bin/zsh -lc "cat .env"',
        )
        safe_cmds = (
            'cat .env.example',
            'python3 -c "print(open(\'.env.example\').read())"',
            'cat ~/Dev/_governance/scripts/sa-guard.sh',
            'rg "sa-guard" ~/Dev/_governance/dispatch.md',
        )
        for platform in ('agy', 'codex', 'grok'):
            for cmd in blocked_cmds:
                with self.subTest(platform=platform, blocked=cmd):
                    self.assertTrue(blocked(platform, cmd))
                    self.assertFalse(blocked(platform, cmd, subagent=False))
            for cmd in safe_cmds:
                with self.subTest(platform=platform, safe=cmd):
                    self.assertFalse(blocked(platform, cmd))

    def test_sa_board_and_coord_token(self):
        allowed_cmds = (
            'rg "coord.token" router.py',
            'grep -rn "codex\\|agy\\|sa-board" /Users/royjiang/Dev/_governance',
            'rg -n "foo|codex|agy|sa-board|git push|(bar)" src/',
            'grep codex\\|agy dispatch.md',
            '/bin/zsh -lc "rg -n \'codex\\|agy\\|sa-board\' src && git status --short"',
        )
        blocked_cmds = (
            'sa-board post "done @verifier"',
            '~/Dev/_shared/agent-runtime/bin/sa-board --board b1 --channel main post hello',
            'sa-board init',
            'sa-board status',
            'cat /tmp/dev-subagentd-501/boards/b1/coord.token',
            'cat coord.token',
            'python3 -c "open(\'coord.token\').read()"',
            'rg . /tmp/boards/b1/coord.token',
            'rg -n "foo|$(codex exec bar)" src/',
            'rg -n "foo|`git push`" src/',
            'grep "codex|agy" file.txt && git push',
            'echo x\\ #; git push',
            'echo `echo \\`codex --version\\``',
            'echo `echo \\`git push\\``',
        )
        token_path = '/tmp/dev-subagentd-501/boards/b1/coord.token'
        for platform in ('agy', 'codex', 'grok'):
            for cmd in allowed_cmds:
                with self.subTest(platform=platform, allowed=cmd):
                    self.assertFalse(blocked(platform, cmd))
            for cmd in blocked_cmds:
                with self.subTest(platform=platform, blocked=cmd):
                    self.assertTrue(blocked(platform, cmd))
                    self.assertFalse(blocked(platform, cmd, subagent=False))
            self.assertTrue(self._run_payload(platform, self._file_payload(platform, 'read_file', token_path)))
            self.assertFalse(self._run_payload(platform, self._file_payload(platform, 'read_file', token_path), subagent=False))

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
        empty_env = {'ZOMI_SUBAGENT_SESSIONS_DIR': str(Path(SESSIONS.name) / 'missing')}
        agy_parent = run_hook('agy', {'conversationId': 'p1', 'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'git push'}}}, subagent=False, extra_env=empty_env)
        self.assertEqual(agy_parent.returncode, 0)
        self.assertEqual(json.loads(agy_parent.stdout), {'decision': 'ask'})
        codex_parent = run_hook('codex', {'threadId': 'p1', 'tool_name': 'exec_command', 'tool_input': {'command': 'git push'}}, subagent=False, extra_env=empty_env)
        self.assertEqual(codex_parent.returncode, 0)
        self.assertEqual(codex_parent.stdout, '')


    def test_real_adapter_smoke(self):
        with tempfile.TemporaryDirectory(prefix='sa-guard-markers-') as marked:
            (Path(marked) / 'sub-sid-123').touch()
            for platform in ('agy', 'codex', 'grok'):
                bad = json.dumps({'conversationId': 'sub-sid-123', 'threadId': 'sub-sid-123',
                                  'session_id': 'sub-sid-123', 'tool_name': 'spawn_agent',
                                  'toolName': 'spawn_subagent', 'toolCall': {'name': 'invoke_subagent'}})
                bad = bad.replace('sub-sid-123', r'sub\u002dsid-123')
                cases = ((True, SESSIONS.name, '{', True, True),
                         (True, SESSIONS.name, '{}', False, True),
                         (False, SESSIONS.name, bad, False, False),
                         (False, marked, bad, True, True),
                         (False, marked, bad.replace('123', '999'), False, False))
                for subagent, directory, raw, deny, is_child in cases:
                    with self.subTest(platform=platform, subagent=subagent, directory=directory, raw=raw):
                        env = {'SA_SUBAGENT': '1' if subagent else '0',
                               'ZOMI_SUBAGENT_SESSIONS_DIR': directory}
                        before = dict(os.environ)
                        expected = GUARD.evaluate_guard(platform, raw, env)
                        self.assertEqual(dict(os.environ), before)
                        result = run_hook(platform, raw, subagent, env, real=True)
                        self.assertEqual((result.returncode, result.stdout, result.stderr), expected)
                        self.assertEqual(result.returncode, 2 if deny and platform != 'agy' else 0)
                        self.assertEqual(result.stderr, GUARD.REASON + '\n' if deny and platform == 'codex' else '')
                        if platform == 'agy' or (deny and platform == 'grok'):
                            self.assertEqual(json.loads(result.stdout)['decision'],
                                             'deny' if deny else 'allow' if is_child else 'ask')
                        else:
                            self.assertEqual(result.stdout, '')

    def test_heredoc_ast_lookup_and_redirect_cases(self):
        run_heredoc_and_perm_cases(self, blocked, run_hook_fn=run_hook, guard_mod=GUARD)

    def test_blocks_swarm_workflow_and_cockpit_dispatch_policy_writes(self):
        for tool in ('swarm', 'run_workflow'):
            sub_res = run_hook('agy', {'toolCall': {'name': tool, 'args': {}}}, subagent=True)
            self.assertEqual(json.loads(sub_res.stdout)['decision'], 'deny', tool)
            main_res = run_hook('agy', {'toolCall': {'name': tool, 'args': {}}}, subagent=False)
            self.assertEqual(json.loads(main_res.stdout)['decision'], 'ask', tool)

        for target in (
            '~/.gemini/antigravity/sidecar_data/statusline/dispatch-policy.json',
            '~/.gemini/config/plugins/agy-cockpit/rules/AGENTS.md',
        ):
            for platform in ('agy', 'codex', 'grok'):
                self.assertTrue(blocked(platform, f'echo "{{}}" > {target}'), (platform, target))
            write_res = run_hook('agy', {'toolCall': {'name': 'write_file', 'args': {'Path': target, 'Content': '{}'}}}, subagent=True)
            self.assertEqual(json.loads(write_res.stdout)['decision'], 'deny', target)
            read_res = run_hook('agy', {'toolCall': {'name': 'read_file', 'args': {'Path': target}}}, subagent=True)
            self.assertEqual(json.loads(read_res.stdout)['decision'], 'allow', target)

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
                        result = run_hook('agy', payload, subagent=None, extra_env=env, real=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout)['decision'], decision)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        SESSIONS.cleanup()
        print(f'Guard calls: {CALLS}', flush=True)

