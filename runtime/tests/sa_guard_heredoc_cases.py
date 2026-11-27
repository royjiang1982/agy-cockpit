"""Regression and security bypass tests for heredoc, AST, lookup, workdir, and redirection guards."""
from __future__ import annotations

import os
from pathlib import Path


def run_heredoc_and_perm_cases(test_case, blocked_fn, run_hook_fn=None, guard_mod=None) -> None:
    """Execute allowed/blocked regression cases across agy, codex, and grok."""
    allowed_cmds = (
        # Codex 01a1f462 pattern: cat + python3 - <<'PY' referencing bin/zomi-agy-acp and bin/zomi-subagent
        (
            "cat > backend/agy_admission.py <<'PY'\n"
            "DENIAL = 'personal agy only'\n"
            "PY\n"
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "def edit(path, a, b):\n"
            "    p = Path(path); p.write_text(p.read_text().replace(a, b))\n"
            "edit('bin/zomi-subagent', 'agy', 'codex')\n"
            "p = Path('bin/zomi-agy-acp')\n"
            "s = p.read_text()\n"
            "s = s[:s.index('\"\"\"One-shot')] + '''\"\"\"Legacy\"\"\"\n'''\n"
            "p.write_text(s)\n"
            "PY\n"
        ),
        # Multiple cat blocks after python3 - <<'PY' containing parens/dots
        (
            "cat > backend/a.py <<'PY'\nx = 1\nPY\n"
            "python3 - <<'PY'\nfrom pathlib import Path\nPath('backend/a.py').read_text()\nPY\n"
            "cat > backend/agy_installer.py <<'PY'\n"
            "p = home / (\"agy\" + suffix)\n"
            "def _files(source):\n"
            "    return source.is_dir() and not name.startswith(\".\")\n"
            "PY\n"
        ),
        # Read-only audit Python heredoc (01a1eeb3 pattern) & subprocess with sys.executable
        (
            "GIT_OPTIONAL_LOCKS=0 python3 -IB - <<'PY'\n"
            "import ast, subprocess\n"
            "from pathlib import Path\n"
            "tracked = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD', '--', '.', ':!tmp'.strip()], text=True)\n"
            "p = Path('bin/zomi-browser')\n"
            "if p == Path('bin/zomi-browser'):\n"
            "    source = ast.parse(Path('backend/agy_installer.py').read_bytes())\n"
            "PY\n"
        ),
        (
            "python3 - <<'PY'\n"
            "import subprocess, sys\n"
            "for s in ['scripts/verify_a.py']:\n"
            "    subprocess.run([sys.executable, s], check=True)\n"
            "PY\n"
        ),
        # Safe tempfile & explicit tmp deletion in Python/Node heredoc
        (
            "python3 - <<'PY'\n"
            "import tempfile\n"
            "from pathlib import Path\n"
            "with tempfile.TemporaryDirectory() as td:\n"
            "    p = Path(td) / 'x'\n"
            "    p.write_text('1')\n"
            "    p.unlink()\n"
            "Path('tmp/sample.txt').unlink()\n"
            "PY\n"
        ),
        # Harmless mention of zomi-subagent-sessions or restricted strings inside Python heredoc
        "python3 - <<'PY'\nprint('rm is blocked for zomi-subagent-sessions')\nPY\n",
        (
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "Path('tmp/note.py').write_text('os.system(\"git push\")\\nrm -rf /\\n')\n"
            "PY\n"
        ),
        # Tab-stripped quoted heredoc and reverse redirect cat <<'EOF' > path
        "cat <<'PY' > tmp/rev.py\nprint('codex')\nPY\n",
        "cat > tmp/strip.py <<-'PY'\n\tprint('agy')\n\tPY\n",
        # Non-executing command lookup
        "command -v agy || true",
        "command -V codex",
        "command -pv python3",
        # Repo plugin hooks.json edit & sed -Ei, quoted redirect text, safe python -c, JS log/regex
        "sed -Ei 's/a/b/' plugin/hooks.json",
        "echo '{}' > tmp/hooks.json",
        "echo 'A=1' > .env.example",
        "grep ' > .env ' tmp/a.txt",
        "python3 -c \"from pathlib import Path; print(Path('bin/zomi-agy-acp').exists())\"",
        "python3 -c 'print(\"scripts/sa-guard-ast.py\")'",
        "python3 -IBc \"from pathlib import Path; print(Path('tmp/x.txt').name)\"",
        (
            "node - <<'JS'\n"
            "const fs = require('fs');\n"
            "const m = /foo/.exec('foo');\n"
            "console.log('child_process');\n"
            "fs.writeFileSync('tmp/out.js', String(m));\n"
            "JS\n"
        ),
        "node - <<'JS'\nrequire('child_process').spawnSync('printf', ['ok'])\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('git', ['status'], { shell: false })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('printf', ['ok'], { cwd: 'tmp' })\nJS\n",
        "node - <<'JS'\nrequire('fs').unlinkSync('tmp/a.ts')\nJS\n",
        "node - <<'JS'\nrequire('fs').rmSync('tmp/a', { recursive: true, force: true })\nJS\n",
        "echo $'hello\\'world'",
        "bash scripts/run_tests.sh",
        "test -f x || bash scripts/run_tests.sh",
        "test -f x ||\n bash scripts/run_tests.sh",
        "printf x>'fixture'.txt",
        "cat <<'OUT:ER'\ncat > tmp/sample <<'true'\nOUT:ER\nprintf after_outer\ntrue\n",
        "cat > tmp/note.txt <<'true'\ntext\ntrue\nprintf kept\ntrue\n",
        "cat > tmp/note.txt <<'true'\ntext\ntrue\ncd sample && printf ok > fixture.txt\ntrue\n",
        "cat <<'TEXT'\nsh -s\nTEXT\n",
        "echo '{'\ncat <<'TEXT'\nsh -s\nTEXT\n",
        "cat > tmp/note.txt <<'EO''F'\ngit push\nEOF\n",
        "false || python3 - <<'PY'\nfrom pathlib import Path\nPath('bin/zomi-agy-acp').exists()\nPY\n",
        "(python3 - <<'PY'); echo ok\nfrom pathlib import Path\nPath('tmp/a.py').read_text()\nPY\n",
        "python3 - <<'PY'\nimport tempfile, shutil\ntd = tempfile.mkdtemp()\nshutil.rmtree(td)\nPY\n",
        "python3 - <<'PY'\nimport os\nos.open('~/.codex/hooks.json', os.O_RDONLY | os.O_CLOEXEC)\nPY\n",
        "python3 - <<'PY'\nimport os\nos.open('~/.codex/hooks.json', flags=os.O_RDONLY)\nPY\n",
        "cd sample && printf ok > fixture.txt",
        "cd >| /dev/null sample && printf ok > fixture.txt",
        "cd sample &>/dev/null; printf ok > fixture.txt",
        "cd sample || :; printf ok > fixture.txt",
        "(cd ~/.codex && echo ok); printf ok > tmp/out.json",
        ": >| fixture.txt",
        ": > ''fixture.txt",
        "echo 'text > tmp/sa-guard-example.py'",
        "cat <<EOF1\nit's fine\nEOF1\ncd sample && printf ok > fixture.txt\ncat <<EOF2\nit's also fine\nEOF2\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('pwd', [], { env: { cwd: 'metadata' }, cwd: 'tmp' })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('pwd', [], { cwd: 'sa\\x6dple' })\nJS\n",
    )

    blocked_cmds = (
        # Quoted heredoc piped to shell & |& shell pipe & unrecognized outer heredoc smuggling
        "cat <<'EOF' | bash\ngit push\nEOF\n",
        "cat <<'EOF' | sh\ncodex exec test\nEOF\n",
        "bash <(cat <<'TEXT'\nsh -s\nTEXT\n)",
        "bash <(\ncat <<'TEXT'\nsh -s\nTEXT\n)",
        "(cat <<'TEXT'\ngit push\nTEXT\n) | bash",
        "sh <(cat <<'EOF'\ngit push\nEOF\n)",
        "cat <<EOF1\nit's 1\nEOF1\ncd ~/.codex && printf ok > hooks.json\ncat <<EOF2\nit's 2\nEOF2\n",
        "python3 - <<'PY' | bash\nprint('git push')\nPY\n",
        "printf 'true' |& bash",
        "printf 'true' |&\n bash",
        "printf 'true' |&\n# comment line\n bash",
        "printf 'true' |\n bash",
        "printf 'git push' | sh",
        "cat <<'OUTER'\ncat > tmp/sample <<'true'\nOUTER\ngit push\ntrue\n",
        "cat <<'OUT:ER'\ncat > tmp/sample <<'true'\nOUT:ER\ngit push\ntrue\n",
        "cat > tmp/note.txt <<'true'\ntext\ntrue\ngit push\ntrue\n",
        "cat > tmp/note.txt <<'true'\ntext\ntrue\ncd ~/.codex\ntrue\nprintf ok > hooks.json\n",
        "false || python3 - <<'PY'\nimport subprocess\nsubprocess.run(['git', 'push'])\nPY\n",
        "echo $((1<<2))\ncd ~/.codex\ncat <<EOF\ncd /tmp\nEOF\nsed -Ei 's/a/b/' hooks.json",
        # Unquoted python heredoc expansion & trailing shell commands
        "python3 - <<PY\n$(codex exec test)\nPY\n",
        "python3 - <<'PY'\nprint(1)\nPY\ngit push\n",
        "python3 - <<'PY'; git push\nprint(1)\nPY\n",
        "echo ok && python3 - <<'PY'\nimport subprocess\nsubprocess.run(['git', 'push'])\nPY\n",
        # Direct or indirect Python subprocess/os execution & dynamic exec(compile(...))
        "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['git', 'push'])\nPY\n",
        "python3 - <<'PY'\nfrom os import system as s\ns('codex exec hi')\nPY\n",
        "python3 - <<'PY'\nfrom os import *\nsystem('git push')\nPY\n",
        "python3 - <<'PY'\nimport subprocess, sys\nsubprocess.run([sys.executable, '-c', 'import os; os.system(\"git push\")'])\nPY\n",
        "python3 - <<'PY'\nexec(\"import os; os.system('git push')\")\nPY\n",
        "python3 - <<'PY'\ndef content(): return 'x=1'\nexec(compile(content(), '<f>', 'exec'))\nPY\n",
        "python3 - <<'PY'\nimport os\ngetattr(os, 'system')('git push')\nPY\n",
        "python3 - <<'PY'\n__import__('subprocess').run(['git', 'status'])\nPY\n",
        "python3 - <<'PY'\ndef broken(:\nPY\n",
        # Python keyword/starred args, sensitive reads, guard writes, non-tmp deletes, marker tamper, os.open & mkdtemp
        "python3 - <<'PY'\nopen(file='.env')\nPY\n",
        "python3 - <<'PY'\nargs = ('~/.codex/hooks.json', 'w')\nopen(*args)\nPY\n",
        "python3 - <<'PY'\nopts = {'file': '.env', 'mode': 'r'}\nos.open(**opts)\nPY\n",
        "python3 - <<'PY'\nimport os\nos.open(**{'path': '~/.codex/hooks.json', 'flags': os.O_WRONLY})\nPY\n",
        "python3 - <<'PY'\nopen('~/.codex/hooks.json', **{'mode': 'w'}).write('x')\nPY\n",
        "python3 - <<'PY'\nimport tempfile, shutil\ntd = tempfile.mkdtemp(dir='src')\nshutil.rmtree(td)\nPY\n",
        "python3 - <<'PY'\nimport tempfile, shutil\ntd = tempfile.mkdtemp(**{'dir': 'src'})\nshutil.rmtree(td)\nPY\n",
        "python3 - <<'PY'\nimport tempfile, shutil\nwith tempfile.TemporaryDirectory(dir='src') as td:\n    shutil.rmtree(td)\nPY\n",
        "python3 - <<'PY'\nimport tempfile, shutil\nwith tempfile.TemporaryDirectory(**{'dir': 'src'}) as td:\n    shutil.rmtree(td)\nPY\n",
        "python3 - <<'PY'\nimport os\nos.open('~/.codex/hooks.json', os.O_WRONLY)\nPY\n",
        "python3 - <<'PY'\nimport os\nos.open('~/.codex/hooks.json', flags=os.O_RDWR)\nPY\n",
        "python3 - <<'PY'\nimport os\nos.open('~/.codex/hooks.json', os.O_RDONLY | os.O_CREAT)\nPY\n",
        "python3 - <<'PY'\nfrom pathlib import Path\n(Path.home() / '.ssh' / 'id_rsa').read_text()\nPY\n",
        "python3 - <<'PY'\nfrom pathlib import Path\nPath.home().joinpath('.codex', 'auth.json').read_text()\nPY\n",
        "python3 - <<'PY'\nimport os\nopen(os.environ['HOME'] + '/.ssh/id_rsa').read()\nPY\n",
        "python3 - <<'PY'\nopen('/Users/royjiang/Dev/app/.env').read()\nPY\n",
        "python3 - <<'PY'\nfrom pathlib import Path\np = Path('~/.codex/hooks.json')\np.write_text('{}')\nPY\n",
        "python3 - <<'PY'\nfrom pathlib import Path\ndef edit(p): Path(p).write_text('x')\nedit('~/.gemini/config/hooks.json')\nPY\n",
        "python3 - <<'PY'\nimport os\nos.remove('/tmp/zomi-subagent-sessions/abc')\nPY\n",
        "python3 - <<'PY'\nimport os\nos.remove(path='src/a.py')\nPY\n",
        "python3 - <<'PY'\nfrom pathlib import Path\nPath('src/a.py').unlink()\nPY\n",
        "python3 - <<'PY'\nimport os\nos.system('rm -rf /tmp/zomi-subagent-sessions/abc')\nPY\n",
        # Shell redirections (with and without spaces), cd chains, sed -Ei, Node child_process options & non-tmp delete
        "cat >.env <<'EOF'\nSECRET=1\nEOF\n",
        "cat <<'EOF' > ~/.codex/hooks.json\n{}\nEOF\n",
        "printf x>.env",
        "printf x>'.'env",
        "printf x>~/.codex/hooks.json",
        ": >| ~/.codex/hooks.json",
        ": > ''~/.codex/hooks.json",
        ": >| .env",
        "cd ~/.codex && printf ok > hooks.json",
        "cd >| /dev/null ~/.codex && printf ok > hooks.json",
        "cd ~/.codex &>/dev/null; printf ok > hooks.json",
        "cd ~/.codex || :; printf ok > hooks.json",
        "cd ~/.codex; cat auth.json",
        "cd ~/.codex\ncat <<EOF\ncd /tmp\nEOF\nsed -Ei 's/a/b/' hooks.json",
        "cd ~/.codex && python3 - <<'PY'\nfrom pathlib import Path\nPath('hooks.json').write_text('{}')\nPY\n",
        "command -p agy -p hi",
        "command agy -p hi",
        "command -v agy && agy -p hi",
        "echo ok; (codex exec test)",
        "cat <(codex exec test)",
        "((git push))",
        "node - <<'JS'\nrequire('child_process').execSync('git push')\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('git', ['push'])\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('git', ['push'], { shell: true })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('git push', { shell: true })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('git', ['push'], { env: { A: '1' }, shell: '/bin/bash' })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('git', ['status'], { shell: useShell })\nJS\n",
        "node - <<'JS'\nconst cwd = 'tmp'; require('child_process').spawnSync('pwd', [], { cwd })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('pwd', [], { ...opts })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('pwd', [], { cwd: 'samp' + 'le' })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('cat', ['auth.json'], { cwd: '~/.c\\x6fdex' })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('pwd', [], { cwd: 'sa\\xZZ' })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('g\\x69t', ['push'])\nJS\n",
        "node - <<'JS'\nrequire('fs').readFileSync('.e\\x6ev', 'utf8')\nJS\n",
        "node - <<'JS'\nrequire('fs').readFileSync('.\\env', 'utf8')\nJS\n",
        "node - <<'JS'\nrequire('fs').writeFileSync('~/.codex/hoo\\ks.json', '{}')\nJS\n",
        "node - <<'JS'\nrequire('fs').readFileSync('a\\141b.txt', 'utf8')\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('printf', ['ok'], { cwd: '~/.ssh' })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('cat', ['auth.json'], { cwd: '~/.codex' })\nJS\n",
        "node - <<'JS'\nrequire('child_process').spawnSync('sed', ['-i', 's/a/b/', 'hooks.json'], { cwd: '~/.codex' })\nJS\n",
        "node - <<'JS'\nconst cp = require('child_process'); cp.execSync('echo ok'); cp.execSync(cmdVar);\nJS\n",
        "node - <<'JS'\nrequire('fs').readFileSync('.env', 'utf8');\nJS\n",
        "node - <<'JS'\nrequire('fs').unlinkSync('src/a.ts')\nJS\n",
        "bash -c \"python3 - <<'PY'\\nimport os\\nos.system('git push')\\nPY\"",
        "sed -Ei 's/a/b/' ~/.codex/hooks.json",
    )

    for platform in ("agy", "codex", "grok"):
        for cmd in allowed_cmds:
            with test_case.subTest(platform=platform, allowed=cmd[:60]):
                test_case.assertFalse(blocked_fn(platform, cmd))
        for cmd in blocked_cmds:
            with test_case.subTest(platform=platform, blocked=cmd[:60]):
                test_case.assertTrue(blocked_fn(platform, cmd))
                test_case.assertFalse(blocked_fn(platform, cmd, subagent=False))

    # P1 #1 verification: mask_literal_cat must NOT mask outer lines after unrecognized heredocs
    if guard_mod is not None:
        literals = guard_mod._load_literals()
        for sample in (
            "cat <<'OUTER'\ncat > tmp/sample <<'true'\nOUTER\nprintf after_outer\ntrue\n",
            "cat <<'OUT:ER'\ncat > tmp/sample <<'true'\nOUT:ER\nprintf after_outer\ntrue\n",
        ):
            test_case.assertIn("printf after_outer", literals.mask_literal_cat(sample))

    # P1 #3 verification: tool workdir/cwd overrides hook process os.getcwd() across all 3 platforms
    if run_hook_fn is not None:
        codex_home = str(Path(os.path.expanduser("~/.codex")))
        repo_cwd = "/Users/royjiang/Dev/zomi-bot-agy"
        workdir_cases = (
            ("agy", {"toolCall": {"name": "run_command", "args": {"CommandLine": "printf ok > hooks.json", "Cwd": codex_home}}}, True),
            ("codex", {"tool_name": "exec_command", "tool_input": {"command": "printf ok > hooks.json", "workdir": codex_home}}, True),
            ("grok", {"toolName": "exec_command", "toolInput": {"command": "printf ok > hooks.json", "workdir": codex_home}}, True),
            ("codex", {"tool_name": "exec_command", "tool_input": {"command": "sed -Ei 's/a/b/' plugin/hooks.json", "workdir": repo_cwd}}, False),
        )
        for platform, payload, should_deny in workdir_cases:
            with test_case.subTest(platform=platform, workdir_payload=payload):
                res = run_hook_fn(platform, payload, subagent=True)
                denied = res.returncode == 2 or '"deny"' in res.stdout
                test_case.assertEqual(denied, should_deny)
