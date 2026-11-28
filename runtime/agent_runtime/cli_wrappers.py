"""Argument translation for the five standalone engine CLIs."""
import argparse

from .acp_approvals import resolve_agy_mode
from .daemon_client import run_subagent_client
from .dispatch_policy_reader import read_codex_fast
from .pi_stream import PI_THINKING_LEVELS
from .proc_utils import split_model_effort


DEFAULT_SERVICE_TIER = "fast"  # 未配置策略时使用


def _require_prompt(parser, explicit, positional, *, agent):
    prompt = explicit if explicit is not None else " ".join(positional)
    if not prompt or not prompt.strip():
        parser.error("任务书（--prompt）不能为空" if agent == "agy" else "任务书不能为空")
    return prompt


def _agy_flags(parser):
    parser.add_argument("-p", "--prompt", default=None)
    parser.add_argument("-m", "--model", default="gemini-4-hydrogen-high")
    parser.add_argument("--conversation", "-r", default="")
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--output-format", default="stream-json")
    parser.add_argument("--print-response-only", action="store_true")


def _codex_flags(parser):
    parser.add_argument("-m", "--model", default="gpt-6.2-sol")
    parser.add_argument("-c", "--config", action="append", default=[])
    parser.add_argument("--effort", default=None)
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--no-fast", action="store_true")
    parser.add_argument("--service-tier", default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--conversation", default="")
    parser.add_argument("-i", "--image", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dangerously-bypass-approvals-and-sandbox", action="store_true")
    parser.add_argument("--dangerously-bypass-hook-trust", action="store_true")


def _droid_flags(parser):
    parser.add_argument("-m", "--model", default="gpt-6.2-astra")
    parser.add_argument("-r", "--reasoning-effort", "--effort", dest="effort", default=None)
    parser.add_argument("-s", "--session-id", "--conversation", dest="session_id", default="")
    parser.add_argument("--auto", default="high")
    parser.add_argument("-o", "--output-format", default="stream-json")
    parser.add_argument("-p", "--prompt", default=None)


def _grok_flags(parser):
    parser.add_argument("-p", "--prompt", default=None)
    parser.add_argument("-r", "--resume", "-s", "--session-id", "--conversation", dest="session_id", default="")
    parser.add_argument("-m", "--model", default="grok-4.7")
    parser.add_argument("--effort", "--reasoning-effort", dest="effort", default="xhigh")
    parser.add_argument("--always-approve", action="store_true")
    parser.add_argument("--no-subagents", action="store_true")
    parser.add_argument("--output-format", default="streaming-json")


def _pi_flags(parser):
    parser.add_argument("-p", "--print", action="store_true")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("-m", "--model", default="claude-opus-5-5:high")
    parser.add_argument("--thinking", "--effort", dest="effort", default="")
    parser.add_argument("--session", "--conversation", dest="session_id", default="")
    parser.add_argument("--mode", default="json")


_ENGINE_FLAGS = {
    "agy": _agy_flags,
    "codex": _codex_flags,
    "droid": _droid_flags,
    "grok": _grok_flags,
    "pi": _pi_flags,
}


def _codex_request(parser, args, positional):
    sid = args.conversation
    if positional[:1] == ["resume"]:
        if len(positional) < 2:
            parser.error("resume 缺少会话 ID")
        sid = positional[1]
        positional = positional[2:]
    prompt = _require_prompt(parser, args.prompt, positional, agent="codex")
    effort = args.effort
    if args.fast and args.no_fast:
        parser.error("--fast 与 --no-fast 冲突")
    service_tier = None
    if args.service_tier is not None:
        service_tier = args.service_tier.strip().lower()
    if args.fast:
        if service_tier is not None and service_tier not in ("fast", "priority"):
            parser.error("--fast 与 --service-tier 冲突")
        service_tier = service_tier or "fast"
    elif args.no_fast:
        if service_tier is not None and service_tier in ("fast", "priority"):
            parser.error("--no-fast 与 --service-tier 冲突")
        if service_tier is None:
            service_tier = ""
    passthrough_config = []
    for cfg in args.config:
        if cfg.startswith("model_reasoning_effort="):
            if effort is None:
                effort = cfg.split("=", 1)[1].strip("'\"")
            passthrough_config.append(cfg)
        elif cfg.startswith("service_tier="):
            if service_tier is None:
                service_tier = cfg.split("=", 1)[1].strip().strip("'\"").lower()
        else:
            passthrough_config.append(cfg)
    if service_tier is None:
        fast = read_codex_fast()
        service_tier = DEFAULT_SERVICE_TIER if fast is None else ("fast" if fast else "")
    model, effort = split_model_effort(
        args.model, effort, default_model="gpt-6.2-sol", default_effort="high"
    )
    return dict(
        model=model, effort=effort, service_tier=service_tier, session_id=sid,
        prompt=prompt, config=passthrough_config, image_paths=args.image,
    )


def _request(parser, agent, args, positional):
    if agent in ("codex", "droid") and positional[:1] == ["exec"]:
        positional = positional[1:]
    if agent == "codex":
        return _codex_request(parser, args, positional)

    prompt = _require_prompt(parser, args.prompt, positional, agent=agent)
    if agent == "agy":
        return dict(
            model=args.model, mode=resolve_agy_mode({"mode": args.mode}, None),
            session_id=args.conversation, prompt=prompt,
        )

    options = dict(model=args.model, effort=args.effort, session_id=args.session_id, prompt=prompt)
    if agent == "droid":
        mode_map = {"high": "auto-high", "medium": "auto-medium", "low": "auto-low", "spec": "spec"}
        options["mode"] = mode_map.get(args.auto, "auto-high")
        options["model"], options["effort"] = split_model_effort(
            args.model, args.effort, default_model="gpt-6.2-astra", default_effort="xhigh",
        )
    elif agent == "pi":
        options["model"], options["effort"] = split_model_effort(
            args.model, args.effort, default_effort="high", valid_efforts=PI_THINKING_LEVELS,
        )
    return options


def run_cli(agent, description, argv=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--board", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--name", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--log-file", default=None)
    _ENGINE_FLAGS[agent](parser)
    positional = "positional" if agent == "agy" else "arguments"
    parser.add_argument(positional, nargs="*")
    args = parser.parse_intermixed_args(argv)
    request = _request(parser, agent, args, getattr(args, positional))
    return run_subagent_client(log_file=args.log_file, agent=agent, **request)
