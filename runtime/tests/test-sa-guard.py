#!/usr/bin/env python3
"""Test guard decisions; command fixtures are classified, never executed."""
import unittest

from sa_guard_adapter_cases import CALLS, SANDBOX, GuardAdapterCases, blocked
from sa_guard_file_cases import GuardFileCases
from sa_guard_heredoc_cases import run_heredoc_and_perm_cases


class GuardTests(GuardAdapterCases, GuardFileCases, unittest.TestCase):
    def test_literal_file_data(self):
        for quote in ("'", '"'):
            command = f"cat > tmp/test.mjs <<{quote}EOF{quote}\nconst pattern = /(?:env|pi)(?:/|$)/;\nconst example = 'codex exec test';\n$(grok run)\nEOF\n"
            with self.subTest(quote=quote):
                self.assertFalse(blocked('agy', command))

    def test_executables_still_blocked(self):
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
            with self.subTest(command=command):
                self.assertTrue(blocked('agy', command))

    def test_shell_boundaries_and_inline_interpreter_options(self):
        # These are strings sent to the guard for classification, never run.
        for expected, examples in (
            (True, (
                "{ agy --help; }",
                "! git push",
                "if ! agy -p hi; then :; fi",
                "! agy -p hi",
                "if agy --help; then true; fi",
                "while agy --help; do true; done",
                "until agy --help; do true; done",
                "if true; then agy --help; fi",
                "if false; then true; elif agy --help; then true; fi",
                "if false; then true; else agy --help; fi",
                "for x in y; do agy --help; done",
                'sh -c"agy --help"',
                'bash -lc"agy --help"',
                *(f'{exe} -cx "agy --help"' for exe in ('sh', 'bash', 'zsh', 'dash', 'ksh')),
                'bash > /dev/null -lo pipefail -cx "agy --help"',
                """python3 -W ignore -c 'import os; os.popen("agy --help")'""",
                """python3 -c'import os; os.popen("agy --help")'""",
                """python3 -c 'import os, os.path; os.popen("agy --help")'""",
            )),
            (False, (
                'echo "do rm -rf /"',
                'echo "then agy --help; { agy --help; }"',
                'todo agy --help',
                'sudo-do agy --help',
                'ifelse agy --help',
                'while_loop agy --help',
                'until-done agy --help',
                'echo "if agy --help; then true; fi"',
                'echo "! git push; if ! agy -p hi; then :; fi"',
                'echo ! git push',
                '! git status',
                '! command -v agy',
                'bash tool.sh -c',
                'dash -- tool.sh -c',
                'python3 tool.py -c',
                'python3 -m tool -c',
                'python3 -- tool.py -c',
                'node tool.js -p',
                'node -- tool.js -e',
                'node -r preload.js tool.js -p',
                'node --input-type commonjs tool.js -e',
                '{ git status; }',
                'sh -c"printf ok"',
                """python3 -c'print("ok")'""",
            )),
        ):
            for command in examples:
                with self.subTest(command=command):
                    self.assertEqual(blocked('agy', command), expected)

    def test_node_inline_options_and_template_paths(self):
        js = 'require("child_process").execFileSync("agy", ["-p", "hi"])'
        for option in ('-e ', '-e', '--eval ', '--eval=', '-p ', '--print=', '-pe ', '-ep ', '-iep ',
                       '-pie ', '-r preload.js -e ', '--input-type module -e ',
                       '--require preload.js --import preload.mjs --loader loader.mjs -e ',
                       '-C dev --conditions test --env-file fixture.conf -e ',
                       '--redirect-warnings warnings.log --title test --max-old-space-size 512 -e '):
            with self.subTest(option=option):
                self.assertTrue(blocked('agy', f"node {option}'{js}'"))
                self.assertFalse(blocked('agy', f"node {option}'1 + 2'"))
        for path, expected in (
            ("${fixture}/.ssh/${name}", True),
            ("${fixture}/id_rsa", True),
            ("${fixture}/.env", True),
            ("${fixture}/hooks.json", True),
            ("${fixture}/sa-guard-test.py", True),
            ("${fixture}/.env.example", False),
            ("${fixture}/notes.txt", False),
            ('${".ssh/id_rsa"}', True),
            ('${".e"}${"nv"}', True),
            ('${".ssh"}/id_rsa', True),
            ('${fixture}/${"hoo"}ks.json', True),
            ('${fixture}/${".e"}nv/${name}', True),
            ('${"note"}${"s"}.txt', False),
            ('${".e"}${middle}${"nv"}', False),
            ('${flag ? "docs.txt" : "fixtures/hooks.json"}', True),
            (r'${"fixtures/.e\x6ev"}', True),
            ('${flag ? "docs.txt" : "notes.txt"}', False),
            ('${"fixtures/.env.example"}', False),
        ):
            with self.subTest(path=path):
                body = f'require("fs").readFileSync(`{path}`)'
                self.assertEqual(blocked('agy', f"node <<'JS'\n{body}\nJS\n"), expected)

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
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(blocked('agy', command))
                self.assertFalse(blocked('agy', command, subagent=False))

    def test_safe_commands(self):
        commands = (
            'git status --short', 'git -C /workspace diff HEAD',
            'git log -n 3', 'git rev-parse --show-toplevel', 'git show HEAD', 'git grep test',
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
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(blocked('agy', command))

    def test_git_branch_operations(self):
        mutations = (
            'git checkout -b x', 'git checkout -B x', 'git checkout --orphan x',
            'git checkout -t origin/x', 'git checkout --track origin/x', 'git checkout main',
            'git switch -c x', 'git switch -C x', 'git switch --create x',
            'git switch --force-create x', 'git switch --orphan x', 'git switch main',
            'git branch new-feat', 'git branch new-feat main', 'git branch -- new-feat',
            'git branch --delete x', 'git branch --move a b', 'git branch -c a b',
            'git branch -C a b', 'git branch --copy a b', 'git branch --force x',
            'git branch -f x', 'git branch -u origin/main', 'git branch --set-upstream-to=origin/main',
            'git branch --unset-upstream', 'git branch --edit-description',
            'git branch -avd x', 'git branch --list x --delete', 'git branch -v new-feat',
            'git branch --color always', 'git branch --sort -refname new-feat',
            'git branch --format "--list" new-feat',
            'git branch --format "(team)" new-feat',
            'git branch 2>&1 new-feat', 'git branch >&2 new-feat',
            'git branch <&0 new-feat', 'git branch &>/dev/null new-feat',
            'git branch >|/dev/null new-feat', 'git branch new-feat>/dev/null',
            'git branch ">evil"', "git branch '2>evil'", r'git branch \>evil',
            'git branch 2>"/dev/null" ">evil"', 'git branch 2&>/dev/null',
            'git worktree lock x', 'git worktree unlock x', 'git worktree repair',
        )
        queries = (
            'git branch', 'git branch -a', 'git branch --all', 'git branch -r',
            'git branch --remotes', 'git branch -vv', 'git branch --verbose', 'git branch --no-verbose',
            'git branch -vv 2>/dev/null', 'git branch -vv 2>&1',
            'git branch -vv >&2 <&0', 'git branch -vv &>> /dev/null',
            'git branch -vv 2> "/dev/null" & echo ok',
            'git branch -vv 2>&1 && echo ok', 'git branch -vv >|/dev/null | cat',
            'git branch --list "(team)"',
            'git branch --show-current', 'git branch --list "feat/*"',
            'git branch -ril "feat/*"', 'git branch --list -- "-d"',
            'git branch --contains HEAD "feat/*"', 'git branch --no-contains=HEAD',
            'git branch --merged main', 'git branch --no-merged=main', 'git branch --points-at HEAD',
            'git branch --sort -refname --format "%(refname:short)"',
            'git branch --sort=refname --format="%(refname:short)"',
            'git branch --color=auto --abbrev=8 --ignore-case',
            'git branch --no-color --no-abbrev --no-column', 'git branch --column=plain',
            'git checkout -h', 'git checkout --help', 'git switch -h', 'git switch --help',
            'git worktree list', 'git worktree list --porcelain',
        )
        for expected, commands in ((True, mutations), (False, queries)):
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(blocked('agy', command), expected)

    def test_heredoc_ast_lookup_and_redirect_cases(self):
        run_heredoc_and_perm_cases(self, blocked)

if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        SANDBOX.cleanup()
        print(f'Guard calls: {CALLS}', flush=True)
