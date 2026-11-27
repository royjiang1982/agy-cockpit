#!/usr/bin/env python3
"""Mask only leading literal cat-to-file heredocs before the existing CLI guard.

Deliberately not a shell parser: uncertain syntax is left untouched. Python,
unquoted heredocs (which expand commands), substitutions and shell wrappers must
still reach the guard. Never execute the supplied command.
"""
import ast
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import sys


@lru_cache(maxsize=4)
def _load_mod(filename):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_').removesuffix('.py'), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mask_literal_cat(command):
    return _load_mod('sa-guard-heredoc.py').mask_literal_heredocs(command, kinds=('data',))[0]


_AGENT_CLIS = {
    'agy', 'sa-agy', 'sa-codex', 'sa-droid', 'sa-grok', 'sa-pi', 'sa-subagentd', 'sa-board',
    'zomi-agy-acp', 'zomi-codex-exec', 'sa-run.sh', 'zomi-document', 'zomi-browser',
    'zomi-tool', 'codex', 'droid', 'claude', 'orca', 'pi', 'grok',
}
_READ_CLIS = {'cat', 'head', 'tail', 'less', 'more', 'bat', 'sed', 'awk', 'jq', 'source', '.', 'python', 'python3', 'node', 'ruby', 'perl'}
_WRITE_CLIS = {'tee', 'cp', 'mv', 'ln', 'install', 'chmod', 'chown', 'truncate', 'touch'}
_AGENT_CLI_PAT = '|'.join(re.escape(x) for x in sorted(_AGENT_CLIS, key=len, reverse=True))
_FILE_CLI_PAT = '|'.join(re.escape(x) for x in sorted(_READ_CLIS | _WRITE_CLIS, key=len, reverse=True))

# Match only command positions, retaining quotes in arguments for shlex.
_START = re.compile(
    r"""(?:^|[;&|`\n]|(?<![A-Za-z0-9_=.:"'/\\-])\(|\$\()[ \t]*"""
    r"""(?:(?:sudo|env|exec|nohup|command|time|xargs|npx)[ \t]+"""
    r"""(?:(?:-[uUgnLIPs]|--user|--group)[ \t]+[^\s;&|]+[ \t]+|--?[^\s;&|]+[ \t]+)*"""
    r"""|[A-Za-z_][A-Za-z0-9_]*=(?:[^\s;&|"']+|"[^"]*"|'[^']*')[ \t]+)*"""
    rf"""(?P<q>['"]?)(?:[^\s;&|"'()]*/)?(?:@[^\s;&|"'()/]+/)?(?P<cmd>git|gh|rm|publish\.sh|railway|gcloud|sh|bash|zsh|eval|{_AGENT_CLI_PAT}|{_FILE_CLI_PAT})(?P=q)(?=\s|[;&|)`]|$)""",
    re.MULTILINE,
)


_TAMPER_CMD_RE = re.compile(
    r'(?:^|[;&|(`"\']|\$\()[ \t]*(?:rm|unlink|mv|rmdir)[ \t][^;&|]*zomi-subagent-sessions',
    re.MULTILINE,
)


def _git_branch_mutates(rest):
    """Allow known query options, but not bare operands without a query mode."""
    if rest in (['-h'], ['--help']):
        return False
    query_flags = {'--list', '--show-current'}
    query_filters = {'--contains', '--no-contains', '--merged', '--no-merged', '--points-at'}
    read_flags = {'--all', '--remotes', '--verbose', '--no-verbose', '--no-color', '--no-abbrev',
                  '--no-column', '--ignore-case', '--quiet', '--omit-empty'}
    args = iter(rest)
    query = operand = False
    for arg in args:
        if re.match(r'^(?:\d*[<>]+|&>)', arg):
            if re.fullmatch(r'(?:\d*[<>]+|&>)', arg):
                next(args, None)
            continue
        if arg == '--':
            operand = operand or next(args, None) is not None
            break
        if arg == '-' or not arg.startswith('-'):
            operand = True
        elif not arg.startswith('--'):
            if any(flag not in 'arlviq' for flag in arg[1:]):
                return True
            query |= 'l' in arg[1:]
        else:
            option, eq, _ = arg.partition('=')
            if option in query_filters or (option in query_flags and not eq):
                query = True
            elif option in ('--sort', '--format'):
                if not eq:
                    next(args, None)  # Required value can itself start with '-'.
            elif option not in ('--color', '--abbrev', '--column') and (eq or option not in read_flags):
                return True
    return operand and not query


def restricted(command, base_dir=None):
    """Conservative static inspection; never evaluate shell input."""
    heredoc = _load_mod('sa-guard-heredoc.py')
    shell = _load_mod('sa-guard-shell.py')
    hd_positions, unmasked_spans = [], []
    command, heredocs = heredoc.mask_literal_heredocs(
        command, positions=hd_positions, unmasked_spans=unmasked_spans
    )
    if _TAMPER_CMD_RE.search(command):
        return True
    scan_cmd, backticks = heredoc.command_boundary_view(command, skip_spans=unmasked_spans)
    dir_at = shell.build_dir_resolver(scan_cmd, command, unmasked_spans, normalize_path, base_dir)

    def _bind(pos):
        dirs = dir_at(pos)
        if dirs == (None,):
            return is_sensitive_path, is_guard_path, restricted
        return (
            lambda p, ds=dirs: any(is_sensitive_path(p, base_dir=d) for d in ds),
            lambda p, ds=dirs: any(is_guard_path(p, base_dir=d) for d in ds),
            lambda c, ds=dirs: any(restricted(c, base_dir=d) for d in ds),
        )

    for idx, (kind, body) in enumerate(heredocs):
        sens, guard, sub_fn = _bind(hd_positions[idx])
        if shell.script_heredoc_restricted(kind, body, sub_fn, sens, guard):
            return True
    if any(_bind(0)[2](bt) for bt in backticks):
        return True
    # Inspect explicit execution strings, not arbitrary quoted search arguments.
    for call in re.finditer(r"""os\.system\(\s*((?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'))\s*\)""", command):
        try:
            if _bind(call.start())[2](ast.literal_eval(call[1])):
                return True
        except (ValueError, SyntaxError):
            return True
    if shell.redirection_restricted(scan_cmd, command, is_sensitive_path, is_guard_path, base_dir_for_pos=dir_at):
        return True
    skip_until = 0
    for match in _START.finditer(scan_cmd):
        if match.start('cmd') < skip_until:
            continue
        if shell.is_command_lookup_prefix(scan_cmd[match.start():match.start('cmd')]):
            continue
        name = match['cmd']
        if name in _AGENT_CLIS:
            return True
        sens, guard, sub_fn = _bind(match.start('cmd'))
        # Bound tail using scan_cmd (quoted separators are masked) so quoted tokens like "(team)" stay intact.
        m_end = re.search(r'[;&|()\n]', scan_cmd[match.end():])
        end_offset = m_end.start() if m_end else len(command) - match.end()
        tail = command[match.end():match.end() + end_offset]
        lexer = shlex.shlex(tail, posix=True)
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        args = []
        try:
            for token in lexer:
                args.append(token)
        except ValueError:
            # Malformed/uncertain quoting must not disable the guard.
            return True
        if name in ('sh', 'bash', 'zsh'):
            has_c = False
            for i, arg in enumerate(args[:-1]):
                if arg.startswith('-') and 'c' in arg:
                    has_c = True
                    if sub_fn(args[i + 1]):
                        return True
            if has_c:
                # Inner -c string was validated verbatim; skip re-scanning inside its outer quotes.
                skip_until = max(skip_until, match.end() + end_offset)
                continue
            sep_end = match.start() + (1 if match.start() < len(scan_cmd) and scan_cmd[match.start()] in '|&\n' else 0)
            pre_sep = scan_cmd[:sep_end].rstrip(' \t\r\n')
            if (pre_sep.endswith('|') and not pre_sep.endswith(('||', '>|'))) or pre_sep.endswith('|&') or any(a == '-s' or a.startswith('<<') for a in args):
                return True
        if name == 'eval' and sub_fn(' '.join(args)):
            return True
        if name == 'publish.sh':
            return True
        if name == 'rm':
            targets, options = [], True
            for arg in args:
                if options and arg == '--':
                    options = False
                elif options and arg.startswith('-'):
                    continue
                else:
                    targets.append(arg)
            if not targets or any(not shell.safe_tmp_target(t) for t in targets):
                return True
        elif name == 'git':
            while args and args[0].startswith('-'):
                if args.pop(0) in ('-C', '-c', '--git-dir', '--work-tree', '--namespace') and args:
                    args.pop(0)
            if not args:
                continue
            sub, rest = args[0], args[1:]
            if (sub in ('commit', 'push', 'pull', 'merge', 'rebase', 'cherry-pick', 'am', 'tag', 'restore')
                or sub in ('checkout', 'switch') and rest not in (['-h'], ['--help'])
                or sub == 'stash' and (not rest or rest[0] not in ('list', 'show'))
                or sub == 'worktree' and any(x in ('add', 'remove', 'prune', 'move', 'lock', 'unlock', 'repair') for x in rest)
                or sub == 'branch' and _git_branch_mutates(rest)
                or sub == 'reset' and '--hard' in rest
                or sub == 'clean' and any(x == '--force' or re.match(r'^-[^-]*f', x) for x in rest)):
                return True
        elif name == 'gh' and (args[:1] == ['release'] or
                              args[:1] == ['pr'] and len(args) > 1
                              and args[1] in ('create', 'merge', 'close', 'ready')):
            return True
        elif name == 'railway' and args[:1] in ([x] for x in ('up', 'down', 'delete', 'redeploy')):
            return True
        elif name == 'gcloud' and args[:2] == ['compute', 'instances'] and len(args) > 2 and args[2] in ('stop', 'delete', 'reset'):
            return True
        elif name in _READ_CLIS:
            if shell.read_cli_restricted(name, args, sub_fn, sens, guard):
                return True
        elif name in _WRITE_CLIS:
            if any(sens(arg) or guard(arg) for arg in args):
                return True
    if any(rx.search(command) for rx in (_CMD_CRED_RE, _CMD_INLINE_SECRET_RE, _CMD_COORD_TOKEN_RE)) or _CMD_GUARD_WRITE_RE.search(scan_cmd):
        return True
    return False


_HOME_DIR = posixpath.normpath(os.path.expanduser('~').replace('\\', '/'))

_HOME_CRED_DIRS = ('.ssh', '.aws', '.gnupg', '.kube', '.config/gh', '.config/gcloud')
_HOME_CRED_FILES = (
    '.netrc', '.npmrc', '.pypirc', '.git-credentials', '.docker/config.json',
    '.codex/auth.json', '.gemini/oauth_creds.json', '.gemini/google_accounts.json',
)
_HOME_AGENT_DIRS = ('.codex', '.claude', '.grok', '.gemini')
_SAFE_ENV_NAMES = {'.env.example', '.env.sample', '.env.template', '.env.schema', '.env.dist', '.env.types'}
_PRIVATE_KEY_NAMES = {'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa', 'id_ed25519_sk'}
_PRIVATE_KEY_EXTS = ('.pem', '.key', '.p12', '.pfx')
_CRED_JSON_RE = re.compile(
    r'^(?:credentials|service[-_]account|oauth_creds|google_accounts|client_secret.*)\.json$',
    re.IGNORECASE,
)

_HOME_PREFIX_RE = re.compile(
    r'(?:(?<![A-Za-z0-9_])(?:' + re.escape(_HOME_DIR)
    + r'|/(?:Users|home)/[^/\s"\'`;&|()]+|~|\$\{?HOME\}?))/'
)
_CRED_REL_PARTS = (
    r'\.ssh(?:/|$|[\s"\'`;&|)])'
    r'|\.aws(?:/|$|[\s"\'`;&|)])'
    r'|\.gnupg(?:/|$|[\s"\'`;&|)])'
    r'|\.kube(?:/|$|[\s"\'`;&|)])'
    r'|\.config/(?:gh|gcloud)(?:/|$|[\s"\'`;&|)])'
    r'|\.(?:netrc|npmrc|pypirc|git-credentials)(?:$|[\s"\'`;&|)])'
    r'|\.docker/config\.json(?:$|[\s"\'`;&|)])'
    r'|\.codex/(?:auth\.json|\.credentials[^/\s"\'`;&|)]*|credentials\.json)(?:$|[\s"\'`;&|)])'
    r'|\.gemini/(?:oauth_creds|google_accounts)\.json(?:$|[\s"\'`;&|)])'
    r'|\.(?:claude|grok)/(?:auth|credentials)[^/\s"\'`;&|)]*(?:$|[\s"\'`;&|)])'
)
_CMD_CRED_RE = re.compile(_HOME_PREFIX_RE.pattern + r'(?:' + _CRED_REL_PARTS + r')')

_GUARD_TARGETS_PAT = (
    r'(?:sa-guard(?:-[A-Za-z0-9_.-]+|\.sh)?|(?:\.gemini/config/|\.codex/)hooks\.json|adapters/(?:GEMINI|codex-AGENTS|grok-AGENTS)\.md'
    r'|\.gemini/(?:GEMINI\.md|config/(?:GEMINI\.md|hooks\.json|rules(?:/AGENTS\.md)?|plugins/agy-cockpit/rules(?:/[^\s;&|]+)?)|antigravity/sidecar_data/statusline/dispatch-policy\.json)'
    r'|\.codex/(?:AGENTS\.md|hooks\.json)'
    r'|\.grok/(?:config\.toml|GROK\.md|AGENTS\.md))'
)
_CMD_GUARD_WRITE_RE = re.compile(
    r'(?:>>?\s*[^\s;&|]*' + _GUARD_TARGETS_PAT
    + r'|(?:sed\s+[^\n;&|]*-[Eernzsu]*i|perl\s+[^\n;&|]*-i|node\s+[^\n;&|]*-e)[^\n;&|]*'
    + _GUARD_TARGETS_PAT + r')'
)
_CMD_INLINE_SECRET_RE = re.compile(
    r'(?:python3?|node|ruby|perl)\s+[^\n;&|]*(?:-c|-e)[^\n;&|]*(?:'
    + _CRED_REL_PARTS
    + r'|(?:^|[/\s"\'])\.env(?:\.(?!example\b|sample\b|template\b|schema\b|dist\b|types\b)[A-Za-z0-9_-]+)?(?=["\'\s/]|$)'
    + r'|(?:^|[/\s"\'])coord\.token(?=["\'\s/]|$))'
)
_CMD_COORD_TOKEN_RE = re.compile(
    r'(?:<|>>?)\s*[^\s;&|]*coord\.token\b|(?:^|[\s"\'`=(])[^\s"\'`;&|()]*?/coord\.token(?=$|[\s"\'`;&|)])'
)

_WRITE_TOOLS = {
    'write_file', 'edit_file', 'multi_edit', 'replace_file_content', 'multi_replace_file_content',
    'write_to_file', 'apply_patch', 'create_file', 'delete_file', 'patch_file', 'remove_file', 'str_replace_editor', 'create_or_update_file',
}
_WRITE_PREFIXES = ('write_', 'edit_', 'delete_', 'patch_', 'replace_', 'create_', 'remove_')
_PATH_KEYS = (
    'Path', 'AbsolutePath', 'TargetFile', 'DirectoryPath', 'SearchPath', 'SearchDirectory', 'TargetDirectory', 'FilePath',
    'path', 'file_path', 'target_file', 'target_path', 'dir_path', 'directory', 'search_path', 'paths',
)
_PATCH_FILE_RE = re.compile(r'^\*\*\* (?:(?:Add|Update|Delete) File|Move to):\s*(.+?)\s*$', re.MULTILINE)


def normalize_path(raw, base_dir=None):
    text = str(raw or '').strip()
    if not text:
        return ''
    text = text.replace('\\', '/')
    if text == '~' or text.startswith('~/'):
        text = _HOME_DIR + text[1:]
    text = text.replace('${HOME}', _HOME_DIR).replace('$HOME', _HOME_DIR)
    if not text.startswith('/'):
        base = str(base_dir or os.getcwd()).replace('\\', '/')
        text = posixpath.join(base, text)
    return posixpath.normpath(text)


def _home_relative(norm_path):
    if norm_path == _HOME_DIR:
        return ''
    if norm_path.startswith(_HOME_DIR + '/'):
        return norm_path[len(_HOME_DIR) + 1:]
    m = re.match(r'^/(?:Users|home)/[^/]+/(.+)$', norm_path)
    return m.group(1) if m else None


def is_sensitive_path(raw, base_dir=None):
    norm = normalize_path(raw, base_dir=base_dir)
    if not norm:
        return False
    base = posixpath.basename(norm)
    lower_base = base.lower()
    if lower_base == 'coord.token':
        return True
    if base == '.env' or (base.startswith('.env.') and lower_base not in _SAFE_ENV_NAMES
                          and not lower_base.endswith('.d.ts')):
        return True
    if lower_base in _PRIVATE_KEY_NAMES or lower_base.endswith(_PRIVATE_KEY_EXTS):
        return True
    if _CRED_JSON_RE.match(base):
        return True

    rel = _home_relative(norm)
    if rel is None:
        return False
    for d in _HOME_CRED_DIRS:
        if rel == d or rel.startswith(d + '/'):
            return True
    if rel in _HOME_CRED_FILES:
        return True
    if rel.startswith('.codex/.credentials'):
        return True
    top = rel.split('/', 1)[0]
    if top in _HOME_AGENT_DIRS and not rel.startswith((
        '.gemini/config/skills/', '.gemini/antigravity/builtin/', '.gemini/antigravity/brain/',
    )):
        if any(tok in lower_base for tok in ('oauth', 'credential', 'auth.json', 'secret', '.key')):
            return True
    return False


def is_guard_path(raw, base_dir=None):
    norm = normalize_path(raw, base_dir=base_dir)
    if not norm:
        return False
    gov_scripts = posixpath.normpath(posixpath.join(_HOME_DIR, 'Dev/_governance/scripts'))
    gov_adapters = posixpath.normpath(posixpath.join(_HOME_DIR, 'Dev/_governance/adapters'))
    if norm.startswith(gov_scripts + '/sa-guard') or norm == gov_adapters or norm.startswith(gov_adapters + '/'):
        return True
    rel = _home_relative(norm)
    if rel is None:
        return False
    if rel in (
        '.gemini/config/hooks.json', '.gemini/config/GEMINI.md', '.gemini/GEMINI.md',
        '.codex/hooks.json', '.codex/AGENTS.md', '.grok/config.toml', '.grok/GROK.md', '.grok/AGENTS.md',
        '.gemini/antigravity/sidecar_data/statusline/dispatch-policy.json',
        '.gemini/config/plugins/agy-cockpit/hooks.json',
        '.gemini/config/plugins/agy-cockpit/rules/AGENTS.md',
    ):
        return True
    return (
        rel == '.gemini/config/rules'
        or rel.startswith('.gemini/config/rules/')
        or rel == '.gemini/config/plugins/agy-cockpit/rules'
        or rel.startswith('.gemini/config/plugins/agy-cockpit/rules/')
        or rel == '.gemini/config/plugins/agy-cockpit/runtime/guard'
        or rel.startswith('.gemini/config/plugins/agy-cockpit/runtime/guard/')
    )


def payload_restricted(platform, raw_json, base_dir=None):
    try:
        data = json.loads(raw_json)
    except (ValueError, TypeError):
        return True
    if not isinstance(data, dict):
        return True

    if platform == 'agy':
        tc = data.get('toolCall') or {}
        tool_name = str(tc.get('name') or '')
        args = tc.get('args')
    elif platform == 'codex':
        tool_name = str(data.get('tool_name') or '')
        args = data.get('tool_input')
    elif platform == 'grok':
        tool_name = str(data.get('toolName') or data.get('tool_name') or '')
        args = data.get('toolInput') or data.get('tool_input')
    else:
        return True

    lower_tool = tool_name.strip().lower()
    raw_args = args
    if not isinstance(args, dict):
        args = {}

    if not base_dir:
        raw_cwd = args.get('workdir') or args.get('cwd') or args.get('Cwd') or data.get('cwd')
        base_dir = str(raw_cwd).strip() or None if raw_cwd else None

    paths = []
    for key in _PATH_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            paths.append(val)
        elif isinstance(val, list):
            paths.extend(str(item) for item in val if isinstance(item, str) and item.strip())

    patch_keys = ('patch', 'input', 'raw_input', 'command') if lower_tool == 'apply_patch' else ('patch', 'input', 'raw_input')
    for patch_key in patch_keys:
        patch_text = args.get(patch_key)
        if isinstance(patch_text, str):
            paths.extend(_PATCH_FILE_RE.findall(patch_text))
    if lower_tool == 'apply_patch' and isinstance(raw_args, str):
        paths.extend(_PATCH_FILE_RE.findall(raw_args))

    is_write = lower_tool in _WRITE_TOOLS or lower_tool.startswith(_WRITE_PREFIXES)
    for p in paths:
        if is_sensitive_path(p, base_dir=base_dir):
            return True
        if is_write and is_guard_path(p, base_dir=base_dir):
            return True
    return False


if __name__ == '__main__':
    if '--check-payload' in sys.argv:
        idx = sys.argv.index('--check-payload')
        platform = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ''
        sys.exit(0 if payload_restricted(platform, sys.stdin.read()) else 10)
    command = sys.stdin.read()
    if '--restricted' in sys.argv:
        sys.exit(0 if restricted(command) else 10)
    sys.stdout.write(mask_literal_cat(command))

