#!/usr/bin/env python3
"""Mask only leading literal cat-to-file heredocs before the existing CLI guard.

Deliberately not a shell parser: uncertain syntax is left untouched. Python,
unquoted heredocs (which expand commands), substitutions and shell wrappers must
still reach the guard. Never execute the supplied command.
"""
import ast
from importlib import import_module
import json
from pathlib import Path
import re
import shlex
import sys


# Also support callers loading this script via spec_from_file_location.
_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
_heredoc = import_module('sa-guard-heredoc')
_shell = import_module('sa-guard-shell')
_paths = import_module('sa-guard-paths')
normalize_path = _paths.normalize_path
is_sensitive_path = _paths.is_sensitive_path
is_guard_path = _paths.is_guard_path
tool_paths_restricted = _paths.tool_paths_restricted


def mask_literal_cat(command):
    return _heredoc.mask_literal_heredocs(command, kinds=('data',))[0]


_AGENT_CLIS = {
    'agy', 'sa-agy', 'sa-codex', 'sa-droid', 'sa-grok', 'sa-pi', 'sa-subagentd', 'sa-board',
    'zomi-agy-acp', 'zomi-codex-exec', 'sa-run.sh', 'zomi-document', 'zomi-browser',
    'zomi-tool', 'codex', 'droid', 'claude', 'orca', 'pi', 'grok',
}
_READ_CLIS = {'cat', 'head', 'tail', 'less', 'more', 'bat', 'sed', 'awk', 'jq', 'source', '.', 'python', 'python3', 'node', 'ruby', 'perl'}
_SHELL_CLI_PAT = '|'.join(sorted(_shell.SHELLS))
_WRITE_CLIS = {'tee', 'cp', 'mv', 'ln', 'install', 'chmod', 'chown', 'truncate', 'touch'}
_AGENT_CLI_PAT = '|'.join(re.escape(x) for x in sorted(_AGENT_CLIS, key=len, reverse=True))
_FILE_CLI_PAT = '|'.join(re.escape(x) for x in sorted(_READ_CLIS | _WRITE_CLIS, key=len, reverse=True))

# Match only command positions, retaining quotes in arguments for shlex.
_START = re.compile(
    r"""(?:^|[;&|`\n]|(?<![A-Za-z0-9_=.:"'/\\-])\(|\$\()[ \t]*"""
    r"""(?:(?:\{|if|while|until|then|else|elif|do|!)[ \t]+)*"""
    r"""(?:(?:sudo|env|exec|nohup|command|time|xargs|npx)[ \t]+"""
    r"""(?:(?:-[uUgnLIPs]|--user|--group)[ \t]+[^\s;&|]+[ \t]+|--?[^\s;&|]+[ \t]+)*"""
    r"""|[A-Za-z_][A-Za-z0-9_]*=(?:[^\s;&|"']+|"[^"]*"|'[^']*')[ \t]+)*"""
    rf"""(?P<q>['"]?)(?:[^\s;&|"'()]*/)?(?:@[^\s;&|"'()/]+/)?(?P<cmd>git|gh|rm|publish\.sh|railway|gcloud|{_SHELL_CLI_PAT}|eval|{_AGENT_CLI_PAT}|{_FILE_CLI_PAT})(?P=q)(?=\s|[;&|)`]|$)""",
    re.MULTILINE,
)

# Operate on the boundary view, where quoted/escaped redirect operators are masked.
_COMMAND_END = re.compile(r'[;()\n]|(?<![<>])&(?!>)|(?<!>)\|')
_COMMAND_REDIRECT = re.compile(
    r"""(?:&>>?|(?:(?<!\S)[0-9]+)?(?:<<[-<]?|<>|[<>]&|>>|>\||[<>]))[ \t]*"""
    r"""(?:[^ \t\r\n;&|()<>"'\\]+|\\[^\n]|"[^"]*"|'[^']*')+"""
)


def _strip_redirects(tail, scan_tail):
    chars = list(tail)
    for match in _COMMAND_REDIRECT.finditer(scan_tail):
        chars[match.start():match.end()] = ' ' * (match.end() - match.start())
    return ''.join(chars)


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
    hd_positions, unmasked_spans = [], []
    command, heredocs = _heredoc.mask_literal_heredocs(
        command, positions=hd_positions, unmasked_spans=unmasked_spans
    )
    if _TAMPER_CMD_RE.search(command):
        return True
    scan_cmd, backticks = _heredoc.command_boundary_view(command, skip_spans=unmasked_spans)
    dir_at = _shell.build_dir_resolver(scan_cmd, command, unmasked_spans, normalize_path, base_dir)

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
        if _shell.script_heredoc_restricted(kind, body, sub_fn, sens, guard):
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
    if _shell.redirection_restricted(scan_cmd, command, is_sensitive_path, is_guard_path, base_dir_for_pos=dir_at):
        return True
    for match in _START.finditer(scan_cmd):
        if _shell.is_command_lookup_prefix(scan_cmd[match.start():match.start('cmd')]):
            continue
        name = match['cmd']
        if name in _AGENT_CLIS:
            return True
        sens, guard, sub_fn = _bind(match.start('cmd'))
        # Bound tail using scan_cmd (quoted separators are masked) so quoted tokens like "(team)" stay intact.
        scan_tail = scan_cmd[match.end():]
        m_end = _COMMAND_END.search(scan_tail)
        end_offset = m_end.start() if m_end else len(scan_tail)
        tail = command[match.end():match.end() + end_offset]
        scan_tail = scan_tail[:end_offset]
        if name in _shell.SHELLS or name in ('git', 'python', 'python3', 'node'):
            tail = _strip_redirects(tail, scan_tail)
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
        if name in _shell.SHELLS:
            inline = _shell.inline_script(args, 'shell')
            if inline:
                code, _ = inline
                if code is None or sub_fn(code):
                    return True
                continue
            sep_end = match.start() + (1 if match.start() < len(scan_cmd) and scan_cmd[match.start()] in '|&\n' else 0)
            pre_sep = scan_cmd[:sep_end].rstrip(' \t\r\n')
            if (pre_sep.endswith('|') and not pre_sep.endswith(('||', '>|'))) or pre_sep.endswith('|&') or '<<' in scan_tail or '-s' in args:
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
            if not targets or any(not _shell.safe_tmp_target(t) for t in targets):
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
            if _shell.read_cli_restricted(name, args, sub_fn, sens, guard):
                return True
        elif name in _WRITE_CLIS:
            if any(sens(arg) or guard(arg) for arg in args):
                return True
    return _paths.command_paths_restricted(command, scan_cmd)


def unpack_tool(platform, data):
    if platform == 'agy':
        tc = data.get('toolCall') or {}
        name = str(tc.get('name') or '')
        raw_args = tc.get('args')
    elif platform == 'codex':
        name = str(data.get('tool_name') or '')
        raw_args = data.get('tool_input')
    elif platform == 'grok':
        name = str(data.get('toolName') or data.get('tool_name') or '')
        raw_args = data.get('toolInput') or data.get('tool_input')
    else:
        raise ValueError('unknown guard platform')
    args = raw_args if isinstance(raw_args, dict) else {}
    if platform == 'agy':
        command = args.get('CommandLine')
        cwd = args.get('Cwd') or data.get('cwd')
    elif platform == 'codex':
        command = None if name.strip().lower() == 'apply_patch' else args.get('command') or args.get('cmd')
        cwd = args.get('workdir') or args.get('cwd') or data.get('cwd')
    else:
        primary = data.get('toolInput')
        secondary = data.get('tool_input')
        primary = primary if isinstance(primary, dict) else {}
        secondary = secondary if isinstance(secondary, dict) else {}
        command = primary.get('command') or primary.get('cmd') or secondary.get('command') or secondary.get('cmd')
        cwd = primary.get('workdir') or primary.get('cwd') or secondary.get('workdir') or secondary.get('cwd') or data.get('cwd')
    cwd = str(cwd or '').strip() or str(args.get('workdir') or args.get('cwd') or args.get('Cwd') or data.get('cwd') or '').strip()
    return name, raw_args, str(command or ''), cwd or None


def payload_restricted(platform, raw_json, base_dir=None):
    try:
        data = json.loads(raw_json)
        if not isinstance(data, dict):
            return True
        name, raw_args, _, cwd = unpack_tool(platform, data)
        return tool_paths_restricted(name, raw_args, base_dir or cwd)
    except (ValueError, TypeError, AttributeError):
        return True


if __name__ == '__main__':
    if '--check-payload' in sys.argv:
        idx = sys.argv.index('--check-payload')
        platform = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ''
        sys.exit(0 if payload_restricted(platform, sys.stdin.read()) else 10)
    command = sys.stdin.read()
    if '--restricted' in sys.argv:
        sys.exit(0 if restricted(command) else 10)
    sys.stdout.write(mask_literal_cat(command))
