"""Parse CLI dispatch commands and bind them to session IDs from their output."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from analyzer import UUID_PAT
from cockpit_io import HOME
from droid_acp_fallback import discovery_hints
from subagent_labels import dispatch_paths, dispatch_patterns, extract_cli_task_label

RE_SA_VIEW = re.compile(rf"▶\s*(agy|codex|claude|pi|droid|grok)\s+会话\s+({UUID_PAT})(?:（([^）]+)）)?")
RE_CONV_ID = re.compile(rf'"conversation_id"\s*:\s*"({UUID_PAT})"')
RE_SESSION_ID = re.compile(rf'"session_id"\s*:\s*"({UUID_PAT})"')
RE_GROK_SESSION_ID = re.compile(rf'"sessionId"\s*:\s*"({UUID_PAT})"')
RE_SA_RUN, RE_AGY_RESUME, RE_CODEX_RESUME, RE_DROID_RESUME, RE_GROK_RESUME, RE_PI_RESUME = dispatch_patterns(UUID_PAT)
RE_DIRECT_AGY = re.compile(r"(?:^|[\s/&;|])(?:agy\s+-p\b|sa-agy\b)")
RE_DIRECT_CODEX = re.compile(r"(?:^|[\s/&;|])(?:codex\s+exec\b|sa-codex\b)")
RE_DIRECT_PI = re.compile(r"(?:^|[\s/&;|])(?:pi\s+-p\b|sa-pi\b)")
RE_DIRECT_DROID = re.compile(r"(?:^|[\s/&;|])(?:droid\s+exec\b|sa-droid\b)")
RE_DIRECT_GROK = re.compile(r"(?:^|[\s/&;|])(?:grok\s+-p\b|sa-grok\b)")
RE_CD_DIR = re.compile(r"""(?:^|&&|;)\s*cd\s+((?:"[^"]*"|'[^']*'|[^\s;&|"']+))""")
RE_MODEL_LONG = re.compile(r"--model\s+([^\s\"']+)")
RE_MODEL_SHORT = re.compile(r"(?:^|\s)-m\s+([^\s\"']+)")
RE_EFFORT_ARG = re.compile(
    r"""(?:model_reasoning_effort=['"]?([a-zA-Z0-9_-]+)['"]?|--effort\s+([a-zA-Z0-9_-]+)|(?:^|\s)-r\s+(low|medium|high|xhigh)\b)"""
)
RE_CAT_SPEC = re.compile(r"\bcat\s+([^\s\"')]+)")
RE_DROID_OUT = re.compile(r">\s*([^\s;&|\"']+\.json)\b")


@dataclass
class Dispatch:
    agent: str
    label: str
    model_hint: str
    effort_hint: str
    cwd: str
    raw_file: Path | None
    resume_id: str | None


def parse_dispatch(args: dict) -> Dispatch | None:
    cmd = str(args.get("CommandLine") or "")
    stripped = cmd.lstrip()
    if (
        any(
            stripped.startswith(p)
            for p in ("python3", "grep", "rg", "cat", "ls", "jq", "ps ", "echo ", "PYTHONPATH=")
        )
        or "--help" in cmd
    ):
        return None

    m_cd = RE_CD_DIR.search(cmd)
    cwd = str(Path(args.get("Cwd") or HOME / "Dev") / (m_cd[1].strip("\"'").replace("~", str(HOME)) if m_cd else ""))
    m_long = list(RE_MODEL_LONG.finditer(cmd))
    m_short = list(RE_MODEL_SHORT.finditer(cmd))
    model_hint = m_long[-1].group(1) if m_long else (m_short[-1].group(1) if m_short else "")
    m_eff = list(RE_EFFORT_ARG.finditer(cmd))
    effort_hint = (m_eff[-1].group(1) or m_eff[-1].group(2) or m_eff[-1].group(3)) if m_eff else ""

    sa_matches = list(RE_SA_RUN.finditer(cmd.split("--prompt", 1)[0]))
    prog = ""
    label = ""
    raw_file: Path | None = None
    if sa_matches:
        m = sa_matches[-1]
        raw_path_str, prog = dispatch_paths(m, HOME)
        if prog.startswith("sa-"):
            prog = prog[3:]
        stem = Path(raw_path_str).stem
        for pfx in ("sa-", "subagent-", "tmp-"):
            if stem.startswith(pfx) and len(stem) > len(pfx):
                stem = stem[len(pfx) :]
        label = stem
        raw_file = Path(raw_path_str) if Path(raw_path_str).is_absolute() else (Path(cwd) / raw_path_str)
    elif RE_DIRECT_AGY.search(cmd) or RE_AGY_RESUME.search(cmd):
        prog = "agy"
    elif RE_DIRECT_CODEX.search(cmd) or RE_CODEX_RESUME.search(cmd):
        prog = "codex"
    elif RE_DIRECT_PI.search(cmd) or RE_PI_RESUME.search(cmd):
        prog = "pi"
    elif RE_DIRECT_DROID.search(cmd) or RE_DROID_RESUME.search(cmd):
        prog = "droid"
    elif RE_DIRECT_GROK.search(cmd) or RE_GROK_RESUME.search(cmd):
        prog = "grok"
    else:
        return None

    if not label:
        m_out = RE_DROID_OUT.search(cmd)
        m_cat = RE_CAT_SPEC.search(cmd)
        if m_out:
            out_rel = m_out.group(1).replace("~", str(HOME))
            label = Path(out_rel).stem
            raw_file = Path(out_rel) if Path(out_rel).is_absolute() else (Path(cwd) / out_rel)
        elif m_cat:
            cat_stem = Path(m_cat.group(1)).stem
            for pfx in ("spec_", "spec-", "task_", "task-", "brief_", "brief-"):
                if cat_stem.startswith(pfx) and len(cat_stem) > len(pfx):
                    cat_stem = cat_stem[len(pfx) :]
            label = f"{prog}_{cat_stem}"
        else:
            label = f"{prog}-cli"
    label = extract_cli_task_label(cmd, args, prog, label if raw_file else "", label.removeprefix(f"{prog}_") if not raw_file and label.startswith(f"{prog}_") else "")

    resume_id: str | None = None
    m_res_agy = RE_AGY_RESUME.search(cmd)
    m_res_codex = RE_CODEX_RESUME.search(cmd)
    m_res_droid = RE_DROID_RESUME.search(cmd)
    m_res_grok = RE_GROK_RESUME.search(cmd) if prog == "grok" else None
    m_res_pi = RE_PI_RESUME.search(cmd) if prog == "pi" else None
    if m_res_agy:
        resume_id = m_res_agy.group(1)
    elif m_res_codex:
        resume_id = m_res_codex.group(1)
    elif m_res_droid:
        resume_id = m_res_droid.group(1)
    elif m_res_grok:
        resume_id = m_res_grok.group(1)
    elif m_res_pi:
        resume_id = m_res_pi.group(1)

    return Dispatch(prog, label, model_hint, effort_hint, cwd, raw_file, resume_id)


def _probe_raw_file(raw_file, started_at):
    stat = raw_file.stat()
    if started_at > 0 and stat.st_mtime < started_at - 5.0:
        return None
    if stat.st_size < 2_000_000:
        return raw_file.read_text(encoding="utf-8", errors="replace"), True
    with raw_file.open("r", encoding="utf-8", errors="replace") as stream:
        return stream.read(16_384), False


def bind_dispatch(dispatch, combined_out, conv_id, t_start, matcher):
    prog, model_hint, effort_hint = dispatch.agent, dispatch.model_hint, dispatch.effort_hint
    sub_id = None
    for match in RE_SA_VIEW.finditer(combined_out):
        if match[2] != conv_id:
            sub_id, prog = match[2], match[1]
            if match[3] and match[3] != "acp":
                model_hint = match[3]
    if sub_id:
        matcher.remember_explicit(prog, sub_id)
    if not sub_id and prog == "agy":
        sub_id = next((m[1] for m in RE_CONV_ID.finditer(combined_out) if m[1] != conv_id), None)

    rf_probe = ""
    raw_file = dispatch.raw_file
    if raw_file and raw_file.exists():
        try:
            probe = _probe_raw_file(raw_file, t_start)
            if probe is not None:
                rf_probe, append = probe
                if append:
                    combined_out += "\n" + rf_probe
                prog, sub_id, model_hint, effort_hint = discovery_hints(
                    rf_probe, prog, sub_id, model_hint, effort_hint, conv_id)
                if sub_id:
                    matcher.remember_explicit(prog, sub_id)
                if not sub_id and prog == "agy":
                    match = RE_CONV_ID.search(rf_probe)
                    if match and match[1] != conv_id:
                        sub_id = match[1]
                elif not sub_id and prog == "codex":
                    match = re.search(r'"thread_id"\s*:\s*"(' + UUID_PAT + r')"', rf_probe)
                    if match:
                        sub_id = match[1]
                elif not sub_id and prog == "grok":
                    match = RE_GROK_SESSION_ID.search(rf_probe)
                    if match and match[1] != conv_id:
                        sub_id = match[1]
                        matcher.remember_explicit(prog, sub_id)
        except Exception:
            pass

    if not sub_id and prog in ("droid", "claude", "grok"):
        pattern = RE_GROK_SESSION_ID if prog == "grok" else RE_SESSION_ID
        match = pattern.search(combined_out)
        if match and match[1] != conv_id:
            sub_id = match[1]
            matcher.remember_explicit(prog, sub_id)
    saw_handoff = ('"session_handoff"' in combined_out) or ('"session_handoff"' in rf_probe)
    if not sub_id and dispatch.resume_id and not saw_handoff:
        sub_id = dispatch.resume_id
        matcher.remember_explicit(prog, sub_id)
    if not sub_id:
        sub_id = matcher.match(prog, dispatch.cwd, t_start)

    dispatch.agent, dispatch.model_hint, dispatch.effort_hint = prog, model_hint, effort_hint
    return sub_id, combined_out
