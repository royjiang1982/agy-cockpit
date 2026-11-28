"""Guard path and file-tool policy, including adapter workdir resolution."""
import json

from sa_guard_adapter_cases import GUARD_HOME, PLATFORMS, blocked, denied, run_hook


def guard_blocks_payload(platform, payload, subagent=True):
    return denied(run_hook(platform, payload, subagent=subagent))


class GuardFileCases:
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
            '/workspace/app/.env',
            '/workspace/app/.env.production.local',
            '/workspace/app/certs/private.pem',
            '/workspace/app/certs/server.key',
            '/workspace/app/../app/../../.ssh/id_ed25519',
        )
        for platform in PLATFORMS:
            for path_val in sensitive:
                for tool_name in ('read_file', 'write_file', 'edit_file'):
                    with self.subTest(platform=platform, tool=tool_name, path=path_val):
                        self.assertTrue(guard_blocks_payload(platform, self._file_payload(platform, tool_name, path_val)))
                        self.assertFalse(guard_blocks_payload(platform, self._file_payload(platform, tool_name, path_val), subagent=False))

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
        for platform in PLATFORMS:
            for target in guard_targets:
                with self.subTest(platform=platform, target=target):
                    self.assertFalse(guard_blocks_payload(platform, self._file_payload(platform, 'read_file', target)))
                    self.assertTrue(guard_blocks_payload(platform, self._file_payload(platform, 'edit_file', target)))
                    self.assertTrue(guard_blocks_payload(platform, self._file_payload(platform, 'write_file', target)))

        # Codex apply_patch header targeting guard or sensitive file must be blocked (both patch and command fields)
        for key in ('patch', 'command'):
            patch_payload = {
                'tool_name': 'apply_patch',
                'tool_input': {key: '*** Begin Patch\n*** Update File: ~/.gemini/config/hooks.json\n@@\n*** End Patch'},
            }
            self.assertTrue(guard_blocks_payload('codex', patch_payload))
            move_payload = {
                'tool_name': 'apply_patch',
                'tool_input': {key: '*** Begin Patch\n*** Update File: tmp/a.py\n*** Move to: ~/Dev/_governance/scripts/sa-guard-hook.py\n@@\n*** End Patch'},
            }
            self.assertTrue(guard_blocks_payload('codex', move_payload))
            env_payload = {
                'tool_name': 'apply_patch',
                'tool_input': {key: '*** Begin Patch\n*** Update File: /workspace/app/.env\n+SECRET=1\n*** End Patch'},
            }
            self.assertTrue(guard_blocks_payload('codex', env_payload))

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
        self.assertFalse(guard_blocks_payload('codex', safe_patch_payload))
        # Real Bash commands masquerading with a Begin Patch header still run command checks
        fake_bash_patch = {
            'tool_name': 'Bash',
            'tool_input': {'command': '*** Begin Patch\ngit push'},
        }
        self.assertTrue(guard_blocks_payload('codex', fake_bash_patch))

    def test_safe_files_and_skills_allowed(self):
        safe_paths = (
            '/workspace/app/.env.example',
            '/workspace/app/.env.template',
            '/workspace/app/src/keyboard.ts',
            '/workspace/app/docs/api-key-guide.md',
            '~/.gemini/config/skills/ego-browser/SKILL.md',
            '~/.gemini/antigravity/builtin/skills/native_swarm/SKILL.md',
            '~/Dev/_governance/dispatch.md',
        )
        for platform in PLATFORMS:
            for path_val in safe_paths:
                for tool_name in ('read_file', 'edit_file'):
                    with self.subTest(platform=platform, tool=tool_name, path=path_val):
                        self.assertFalse(guard_blocks_payload(platform, self._file_payload(platform, tool_name, path_val)))

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
        for platform in PLATFORMS:
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
            'grep -rn "codex\\|agy\\|sa-board" /workspace/_governance',
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
        for platform in PLATFORMS:
            for cmd in allowed_cmds:
                with self.subTest(platform=platform, allowed=cmd):
                    self.assertFalse(blocked(platform, cmd))
            for cmd in blocked_cmds:
                with self.subTest(platform=platform, blocked=cmd):
                    self.assertTrue(blocked(platform, cmd))
                    self.assertFalse(blocked(platform, cmd, subagent=False))
            self.assertTrue(guard_blocks_payload(platform, self._file_payload(platform, 'read_file', token_path)))
            self.assertFalse(guard_blocks_payload(platform, self._file_payload(platform, 'read_file', token_path), subagent=False))

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
            for platform in PLATFORMS:
                self.assertTrue(blocked(platform, f'echo "{{}}" > {target}'), (platform, target))
            write_res = run_hook('agy', {'toolCall': {'name': 'write_file', 'args': {'Path': target, 'Content': '{}'}}}, subagent=True)
            self.assertEqual(json.loads(write_res.stdout)['decision'], 'deny', target)
            read_res = run_hook('agy', {'toolCall': {'name': 'read_file', 'args': {'Path': target}}}, subagent=True)
            self.assertEqual(json.loads(read_res.stdout)['decision'], 'allow', target)

    def test_workdir_overrides_for_each_adapter(self):
        codex_home = str(GUARD_HOME / ".codex")
        cases = (
            ("agy", {"toolCall": {"name": "run_command", "args": {"CommandLine": "printf ok > hooks.json", "Cwd": codex_home}}}, True),
            ("codex", {"tool_name": "exec_command", "tool_input": {"command": "printf ok > hooks.json", "workdir": codex_home}}, True),
            ("grok", {"toolName": "exec_command", "toolInput": {"command": "printf ok > hooks.json", "workdir": codex_home}}, True),
            ("codex", {"tool_name": "exec_command", "tool_input": {
                "command": "sed -Ei 's/a/b/' plugin/hooks.json", "workdir": "/workspace/repo",
            }}, False),
        )
        for platform, payload, should_deny in cases:
            with self.subTest(platform=platform, payload=payload):
                self.assertEqual(guard_blocks_payload(platform, payload), should_deny)
