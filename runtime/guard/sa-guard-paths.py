"""Sensitive paths, guard targets and tool path extraction (no command execution)."""
import os
import posixpath
import re


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

def command_paths_restricted(command, scan_cmd):
    return (any(rx.search(command) for rx in (_CMD_CRED_RE, _CMD_INLINE_SECRET_RE, _CMD_COORD_TOKEN_RE))
            or bool(_CMD_GUARD_WRITE_RE.search(scan_cmd)))


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


def tool_paths_restricted(tool_name, raw_args, base_dir=None):
    lower_tool = tool_name.strip().lower()
    args = raw_args if isinstance(raw_args, dict) else {}
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
