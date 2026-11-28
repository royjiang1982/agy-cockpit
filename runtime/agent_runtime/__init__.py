"""Shared multi-engine resident agent protocol & multiplexing runtime (`agent_runtime`)."""
from .acp_stream import (
    AcpStream,
    AgyAcpStream,
    DroidAcpStream,
    GrokAcpStream,
    agy_server_command,
    droid_server_command,
    grok_server_command,
)
from .codex_stream import CodexStream, codex_server_command
from .daemon import SubagentDaemon
from .daemon_client import ensure_daemon_running, query_daemon, run_subagent_client
from .daemon_config import get_daemon_dir, get_socket_path
from .history_merge import build_forced_handoff_prompt
from .native_questions import NativeQuestions
from .pi_stream import PiRpcStream, pi_server_command
from .proc_utils import (
    EXTRA_BIN_DIRS,
    _terminate_proc_group,
    find_cli_binary,
    split_model_effort,
    terminate_proc_group,
)
from .resident import ACP_AGENTS, ResidentServer, ResidentTurn, SteeringRejected, SteeringUnknown
from .session_handoff import (
    format_handoff_prompt,
    get_sessions_ledger_dir,
    inspect_local_session,
    load_session_ledger,
    prepare_session_handoff,
    record_turn_completion,
    save_session_ledger,
    seed_handoff_ledger,
)

__all__ = [
    "ACP_AGENTS",
    "AcpStream",
    "AgyAcpStream",
    "CodexStream",
    "DroidAcpStream",
    "EXTRA_BIN_DIRS",
    "GrokAcpStream",
    "NativeQuestions",
    "PiRpcStream",
    "ResidentServer",
    "ResidentTurn",
    "SteeringRejected",
    "SteeringUnknown",
    "SubagentDaemon",
    "_terminate_proc_group",
    "agy_server_command",
    "build_forced_handoff_prompt",
    "codex_server_command",
    "droid_server_command",
    "ensure_daemon_running",
    "find_cli_binary",
    "format_handoff_prompt",
    "get_daemon_dir",
    "get_sessions_ledger_dir",
    "get_socket_path",
    "grok_server_command",
    "inspect_local_session",
    "load_session_ledger",
    "pi_server_command",
    "prepare_session_handoff",
    "query_daemon",
    "record_turn_completion",
    "run_subagent_client",
    "save_session_ledger",
    "split_model_effort",
    "terminate_proc_group",
]
