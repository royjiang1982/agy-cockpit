"""ACP engine launch commands and session model/mode configuration."""
from .acp_approvals import resolve_agy_mode
from .proc_utils import agy_app_data_dir, split_model_effort


def agy_server_command(cmd):
    binary = cmd[0] if cmd else "agy"
    base = [binary, "acp"]
    app_dir = agy_app_data_dir()
    if app_dir:
        base.extend(["--app-data-dir", app_dir])
    return base


def droid_server_command(cmd):
    binary = cmd[0] if cmd else "droid"
    extra = []
    i = 1
    while i < len(cmd):
        arg = cmd[i]
        if arg in ("--append-system-prompt", "--append-system-prompt-file", "--settings") and i + 1 < len(cmd):
            extra.extend([arg, cmd[i + 1]])
            i += 2
            continue
        i += 1
    return [binary, "exec", "--auto", "high", "-o", "acp", *extra]


def grok_server_command(cmd):
    binary = cmd[0] if cmd else "grok"
    return [binary, "agent", "--always-approve", "stdio"]


def model_id(bot):
    model = str(bot.get("model") or "gemini-4-carbon")
    if model.endswith(("-low", "-medium", "-high", "-xhigh", "-extra", "-max")):
        return model
    effort = str(bot.get("reasoning_effort") or "high")
    return model + "-" + (effort if effort in ("low", "medium", "high", "xhigh", "extra", "max") else "high")


def configure_session(stream, bot, session):
    available = {
        m.get("modelId")
        for m in (session.get("models") or {}).get("availableModels", [])
        if isinstance(m, dict) and m.get("modelId")
    }
    if stream.agent == "agy":
        model = stream.model_id(bot)
        if available and model not in available:
            raise RuntimeError(f"当前 ACP 客户端不可使用模型 {model}，请选择可用模型")
        stream._rpc("session/set_model", {"sessionId": stream.session_id, "modelId": model})
        mode = resolve_agy_mode(bot, session)
        stream._rpc("session/set_mode", {"sessionId": stream.session_id, "modeId": mode})
        return model

    if stream.agent == "droid":
        raw_model, effort = split_model_effort(
            bot.get("model"), bot.get("reasoning_effort"), default_model="gpt-6.2-astra", default_effort=""
        )
        if available and raw_model not in available:
            for suffix in ("-low", "-medium", "-high", "-xhigh", "-max"):
                if raw_model.endswith(suffix) and raw_model[: -len(suffix)] in available:
                    if not effort:
                        effort = suffix[1:]
                    raw_model = raw_model[: -len(suffix)]
                    break
        if available and raw_model not in available:
            raise RuntimeError(f"当前 Droid ACP 不可使用模型 {raw_model}")
        stream._rpc("session/set_model", {"sessionId": stream.session_id, "modelId": raw_model})
        effort = (effort or "xhigh").lower()
        cfg_options = session.get("configOptions") or []
        if any(isinstance(c, dict) and c.get("id") == "reasoning_effort" for c in cfg_options):
            stream._rpc(
                "session/set_config_option",
                {"sessionId": stream.session_id, "configId": "reasoning_effort", "value": effort},
            )
        mode = str(bot.get("mode") or "auto-high")
        stream._rpc("session/set_mode", {"sessionId": stream.session_id, "modeId": mode})
        return f"{raw_model}:{effort}"

    if stream.agent == "grok":
        raw_model = str(bot.get("model") or "grok-4.7").strip()
        effort = str(bot.get("reasoning_effort") or "xhigh").strip().lower()
        if available and raw_model not in available:
            raise RuntimeError(f"当前 Grok ACP 不可使用模型 {raw_model}")
        stream._rpc("session/set_model", {"sessionId": stream.session_id, "modelId": raw_model})
        cfg_options = session.get("configOptions") or []
        if any(isinstance(c, dict) and c.get("id") == "reasoning_effort" for c in cfg_options):
            stream._rpc(
                "session/set_config_option",
                {"sessionId": stream.session_id, "configId": "reasoning_effort", "value": effort},
            )
        return f"{raw_model}:{effort}"

    return str(bot.get("model") or "")
