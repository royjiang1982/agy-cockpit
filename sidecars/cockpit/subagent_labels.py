"""Pure parsing and task-label formatting for Cockpit subagents."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePath


def dispatch_patterns(uuid_pat: str) -> tuple[re.Pattern[str], ...]:
    return (
        re.compile(
            r"""(?<![\w.-])sa-run\.sh["']?\s+(?:(\d+)\s+([12])\s+)?"""
            r"""("[^"\r\n]*\.jsonl"|'[^'\r\n]*\.jsonl'|[^\s"';&|]+\.jsonl)\s+(?:--\s+)?"""
            r"""("[^"\r\n]+"|'[^'\r\n]+'|[^\s"';&|]+)"""
        ),
        re.compile(rf"""\b(?:agy|sa-agy)["']?\s+--conversation\s+({uuid_pat})"""),
        re.compile(rf"""\b(?:codex\s+exec\s+resume|sa-codex["']?\s+resume)\s+({uuid_pat})"""),
        re.compile(rf"""\b(?:droid\s+exec|sa-droid)\b["']?(?=\s).*?\s-s\s+({uuid_pat})"""),
        re.compile(rf"""\b(?:grok|sa-grok)\b["']?(?=\s).*?\s(?:-r|--resume|-s|--session-id)\s+({uuid_pat})"""),
        re.compile(rf"""\b(?:pi|sa-pi)\b["']?(?=\s).*?\s(?:-s|--session)\s+({uuid_pat})"""),
    )


def dispatch_paths(match: re.Match[str], home: PurePath) -> tuple[str, str]:
    raw, wrapper = match.group(3, 4)
    return raw.strip("\"'").replace("~", str(home)), PurePath(wrapper.strip("\"'")).name


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_EMPTY_WORDS = frozenset((
    "的", "与", "和", "及", "对", "进行", "做", "任务", "目标", "问题", "请",
    "a", "an", "the", "and", "or", "of", "to", "for", "task", "goal", "please",
))
_ACTION_ONLY = frozenset(("修复", "完成", "补齐", "优化", "实现", "排查", "fix", "review", "implement", "check", "investigate"))
_TRAILING = re.compile(r"(?:\b(?:and|or|of|in|for|to|with|the)|[与和及的并且以把将从向])$", re.I)
_GENERIC_ROLES = frozenset(("subagent", "self", "research"))


def _trim(text: str) -> str:
    return re.sub(r"^[\W_]+|[\W_]+$", "", text)


def clamp_task_label(text: str, max_chars: int = 10) -> str:
    """Use integer twentieths: ASCII costs 11 units, a CJK character costs 20."""
    text = re.sub(r"\[([^\[\]]+)\]\([^)]+\)", r"\1", text.strip())
    text = re.sub(r"^(?:\s*(?:[-+*>]|\d+[.)])\s+)+", "", text)
    text = re.sub(r"[*`~]|__+", "", text)
    text = _trim(re.sub(r"\s+", " ", text))
    text = re.sub(r"([,，、;；:：!?！？])[,，、;；:：!?！？]+", r"\1", text)
    tokens = re.findall(r"[A-Za-z0-9]+(?:[.'_][A-Za-z0-9]+)*|[^A-Za-z0-9]", text)
    width = 0
    end = 0
    for token in tokens:
        width += sum(11 if ch.isascii() else 20 for ch in token)
        if width > max_chars * 20:
            break
        end += len(token)
    label = _trim(text[:end])
    if end < len(text):
        while match := _TRAILING.search(label):
            label = _trim(label[:match.start()])
    return label if label.casefold() not in _EMPTY_WORDS else ""


def _prompt_arg(cmd: str) -> str:
    if "--prompt" not in cmd:
        return ""
    args = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    args.whitespace_split = True
    prompt = ""
    try:
        for arg in args:
            if arg == "--prompt":
                prompt = next(args, "")
            elif arg.startswith("--prompt="):
                prompt = arg.partition("=")[2]
    except ValueError:
        # Malformed shell syntax: only use a prompt already parsed in full.
        pass
    return prompt


def _section(prompt: str, *names: str) -> str:
    active = False
    for line in prompt.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if active:
                break
            header = re.fullmatch(r"#{2,6}\s+(.+?)\s*#*", line)
            active = bool(header and header[1].casefold() in names)
        elif active:
            return line
    return ""


def _inline(prompt: str, *names: str) -> str:
    fields = "|".join(names)
    match = re.search(
        rf"(?mi)^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?[*_]*(?:{fields})[*_]*[ \t]*[:：][*_ \t]*(.+)$",
        prompt,
    )
    return match[1] if match else ""


def _explicit_title(prompt: str) -> str:
    return clamp_task_label(_section(prompt, "title", "标题")) or clamp_task_label(
        _inline(prompt, "任务名", "title")
    )


_DISPATCH_HEADER = re.compile(r"^\s*【被派出子代理[^】]*】\s*")
_PARENS = re.compile(r"[（(][^()（）]*[）)]")
_PATH = re.compile(
    r"(?<![\w/])(?:https?://[^\s，。]+|(?:[.~]?/)[A-Za-z0-9._/-]+)"
    r"|(?<![A-Za-z0-9._/-])(?:[A-Za-z0-9._~-]+/)+[A-Za-z0-9._-]+"
)
_COMMIT = re.compile(r"(?<![\w-])(?:commit\s+)?[0-9a-f]{7,40}(?![\w-])", re.I)
_BOILERPLATE = re.compile(
    r"AGY[ -]+Cockpit(?:['’]s)?\s*的?|当前工作区(?:未提交)?(?:中的|的)?|新版|完整|全部"
    r"|\b(?:the|all|uncommitted|in (?:the )?current workspace)\b",
    re.I,
)


def _goal_label(prompt: str, allow_plain: bool = False) -> str:
    head = _DISPATCH_HEADER.match(prompt)
    goal = _section(prompt, "goal", "目标") or _inline(prompt, "问题")
    if not goal and (head or allow_plain):
        body = prompt[head.end():] if head else prompt
        goal = next((line for line in body.splitlines() if line.strip()), "")
    if not goal:
        return ""
    while _PARENS.search(goal):
        goal = _PARENS.sub(" ", goal)
    goal = _PATH.sub(" ", goal.replace("`", ""))
    goal = _COMMIT.sub(" ", goal)
    goal = re.split(r"[。！？；]|[.!?;](?=\s|$)", goal, maxsplit=1)[0]
    goal = _BOILERPLATE.sub(" ", goal)
    goal = re.sub(r"(?<=规则)\s*[/／]\s*Skill\s*文档", "", goal, flags=re.I)
    goal = goal.replace("路由解析层", "路由")
    goal = _trim(goal)
    action = re.match(r"^对.*?(?:进行|做(?:一次)?)\s*(.+)", goal)
    if action:
        goal = action[1]
    first_clause = re.split(r"[，,：:]", goal, maxsplit=1)[0].strip()
    if len(_HAN.findall(first_clause)) >= 4:
        goal = first_clause
    goal = re.sub(r"(?<=[\u3400-\u9fff])\s+|\s+(?=[\u3400-\u9fff])", "", goal)
    label = clamp_task_label(goal)
    return label if label.casefold() not in _ACTION_ONLY else ""


def _clean_stem(stem: str) -> str:
    stem = re.sub(r"^(?:(?:sa|subagent|tmp)[-_])+", "", stem.strip(), flags=re.I)
    return re.sub(r"(?:[-_]r\d+)+$", "", stem, flags=re.I)


def extract_cli_task_label(cmd: str, args: dict, prog: str, stem: str = "", cat_stem: str = "") -> str:
    prompt = _prompt_arg(cmd)
    stem = _clean_stem(stem)
    raw_title = _section(prompt, "title", "标题") or _inline(prompt, "任务名", "title")
    title = clamp_task_label(raw_title)
    han_stem = clamp_task_label(stem) if _HAN.search(stem) else ""
    if han_stem and title and len(title) < len(_trim(raw_title)):
        title = han_stem
    return (
        title
        or han_stem
        or _goal_label(prompt)
        or clamp_task_label(stem)
        or clamp_task_label(str(args.get("toolSummary") or ""))
        or clamp_task_label(cat_stem)
        or clamp_task_label(f"{prog}-cli")
        or "cli"
    )


def format_native_label(role: str, prompt: str = "") -> str:
    role = clamp_task_label(role)
    if prompt and (not role or role.casefold() in _GENERIC_ROLES):
        return _explicit_title(prompt) or _goal_label(prompt, allow_plain=True) or role or "subagent"
    return role or "subagent"
