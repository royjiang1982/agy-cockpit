#!/usr/bin/env python3
"""Black-box contracts for the shell wrapper's stdout, raw log and long report."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "sa-run.sh"


class Run:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.raw = self.root / "tmp" / "case.jsonl"
        self.report = self.raw.with_suffix(".md")
        self.raw.parent.mkdir()
        self.source = self.root / "source"

    def __call__(self, events, exit_code=0, *, block_report=False, expected_rc=None, path=None):
        wire = "".join((e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + "\n" for e in events)
        self.source.write_bytes(wire.encode())
        cmd = (
            "from pathlib import Path; import sys; "
            "if_block, report, source, rc = sys.argv[1:]; "
            "if_block == 'True' and Path(report).mkdir(); "
            "sys.stdout.buffer.write(Path(source).read_bytes()); sys.exit(int(rc))"
        )
        env = {**os.environ, "HOME": str(self.root), "ZDOTDIR": str(self.root)}
        if path is not None:
            env["PATH"] = path
        completed = subprocess.run(
            [str(WRAPPER), str(self.raw), "--", sys.executable, "-B", "-c", cmd,
             str(block_report), str(self.report), str(self.source), str(exit_code)],
            cwd=self.root, env=env, capture_output=True, timeout=10,
        )
        if completed.returncode != 2:
            assert completed.returncode == (exit_code if expected_rc is None else expected_rc), completed.stderr.decode()
            assert self.raw.read_bytes() == wire.encode()
        return completed


def item(kind, *, phase="completed", **fields):
    return {"type": f"item.{phase}", "item": {"type": kind, **fields}}


def result(body, status="SUCCESS", error=""):
    return {"event": "result", "result": {"status": status, "response": body, "error": error}}


def test_compact_view(run):
    tool_name = "检" * 40
    param = "parameter-not-for-view" * 200
    output = "output-not-for-view" * 200
    tool = {"type": "tool_call", "item": dict(name=tool_name + param + "\nsecond-line",
                                            status="pending", arguments=param)}
    finish = {"type": "tool_call", "item": {**tool["item"], "status": "completed", "output": output}}
    notice = {"type": "quota_fallback", "from_agent": "agy", "from_model": "a",
              "to_agent": "codex", "to_model": "c", "rem_frac": 0.02}
    completed = run([
        notice, {"type": "thread.started", "thread_id": "test-codex"}, "x" * 200 + "hidden-raw-tail",
        tool, {"type": "text_delta", "delta": "hidden-text-delta"}, tool, finish, finish,
        {"type": "thinking", "text": "hidden-thought"},
        item("command_execution", phase="started", command=param),
        item("command_execution", exit_code=0, aggregated_output=output),
        item("command_execution", exit_code=7, aggregated_output=output),
        item("file_change", changes=[output]), item("user_message", text="hidden-user-input"),
        item("agent_message", text="Only final body."), {"type": "turn.completed"}, result("Only final body."),
    ])
    assert completed.returncode == 0 and not completed.stderr, completed.stderr
    out = completed.stdout.decode()
    lines = out.splitlines()
    assert [line for line in lines if line.startswith("▶ ")] == ["▶ codex 会话 test-codex"]
    assert lines[0].startswith("⚡ 配额预检：") and "■ SUCCESS " in lines
    assert lines.count("→ " + tool_name) == lines.count("  ✓ " + tool_name) == 1
    assert "! " + "x" * 180 in lines
    assert "→ command" in lines and "  ✓ command (exit=0)" in lines and "  ✗ command (exit=7)" in lines
    assert "  ✓ file_change" in lines
    for hidden in ("parameter-not-for-view", "output-not-for-view", "second-line", "hidden-", "💬"):
        assert hidden not in out
    assert out.count("Only final body.") == 1 and not run.report.exists()


def test_tool_budget(run):
    init = {"event": "init", "agent": "grok", "conversation_id": "test-grok", "init": {"model": "grok-test"}}
    for prefix in ("t", "宽" * 30):
        calls = [{"type": "tool_call", "item": {"name": f"{prefix}{i}"}} for i in range(75)]
        body = "严" * 2666 + "ab"  # Exactly 8,000 UTF-8 bytes.
        completed = run([init, *calls, result(body)])
        assert completed.returncode == 0 and not completed.stderr, completed.stderr
        assert len(completed.stdout) < 9500, (prefix, len(completed.stdout))
        assert not run.report.exists(), (prefix, run.report.stat().st_size)
        lines = completed.stdout.decode().splitlines()
        tools = [line for line in lines if line.startswith("→ ")]
        assert 1 <= len(tools) <= 60
        if prefix == "t":
            assert len(tools) == 60
        fold = [line for line in lines if line.startswith("…（后续工具心跳已折叠")]
        assert fold == [f"…（后续工具心跳已折叠，完整过程见 {run.raw}）"]
        assert len("\n".join([*tools, *fold]).encode()) <= 1500
        assert lines[-1] == body


def test_long_report_and_cleanup(run):
    # Cross both cut points inside a multi-byte character; preserve file LF and NUL bytes.
    body = "开" * 1200 + " 中段👋\x00 " + "尾" * 6000 + "😊a\n\n"
    body_bytes = body.encode()
    event = {"event": "init", "agent": "pi", "conversation_id": "test-pi", "init": {"model": "pi-test"}}
    completed = run([event, result(body), "diagnostic after result"])
    assert completed.returncode == 0 and not completed.stderr, completed.stderr
    assert len(completed.stdout) < 9500
    out = completed.stdout.decode()
    assert out.startswith("▶ pi 会话 test-pi（pi-test）\n■ SUCCESS \n")
    assert out.splitlines()[-1] == f"完整报告：{run.report}"
    intro, preview = out.split(f"（正文共 {len(body_bytes)} 字节，已保留头尾）\n")
    assert intro == "▶ pi 会话 test-pi（pi-test）\n■ SUCCESS \n! diagnostic after result\n"
    head, tail = preview.split("\n…（已省略中间内容）…\n")
    assert head == body_bytes[:2500].decode(errors="ignore")
    assert tail.split("\n完整报告：", 1)[0] == body_bytes[-1500:].decode(errors="ignore")
    assert stat.S_IMODE(run.report.stat().st_mode) == 0o600 and run.report.read_bytes() == body_bytes
    assert set(run.raw.parent.iterdir()) == {run.raw, run.report}

    short = "替换后的短正文"
    completed = run([event, result(short)])
    assert completed.returncode == 0 and not completed.stderr, completed.stderr
    assert completed.stdout.decode().endswith(short + "\n") and not run.report.exists()


def test_codex_fallback_and_claude_result(run):
    codex_start = {"type": "thread.started", "thread_id": "direct"}
    turn_end = {"type": "turn.completed"}
    body = "最后一条报告"
    completed = run([
        codex_start, item("agent_message", text="interim"), item("agent_message", text=body),
        {**item("agent_message", text="delta-only"), "delta": True}, turn_end,
    ], 23)
    assert completed.returncode == 23 and not completed.stderr, completed.stderr
    assert completed.stdout.decode().splitlines() == ["▶ codex 会话 direct", "■ 完成", body]
    assert not run.report.exists()

    long_body = "报告\n" * 1601
    completed = run([codex_start, item("agent_message", text=long_body), turn_end], 23)
    assert completed.returncode == 23 and not completed.stderr, completed.stderr
    assert completed.stdout.decode().splitlines()[-1] == f"完整报告：{run.report}"
    assert run.report.read_bytes() == long_body.encode()
    sid = "12345678-1234-4abc-8abc-1234567890ab"
    session = f"▶ claude 会话 {sid}"
    for model in ("m" * 120, "模型" * 60):
        completed = run([{"type": "system", "subtype": "init", "session_id": sid, "model": model},
                         {"type": "result", "subtype": "error", "is_error": True, "result": long_body}], 9)
        assert completed.returncode == 9 and not completed.stderr, completed.stderr
        lines = completed.stdout.decode().splitlines()
        assert lines[0].startswith(session + "（") and len(lines[0].encode()) < 165
        if model.isascii():
            assert lines[0] == f"{session}（{model[:48]}）"
        assert lines[1] == "■ ERROR" and lines[-1] == f"完整报告：{run.report}"
        assert run.report.read_bytes() == long_body.encode()


def test_report_path_protection(run):
    target = run.root / "untouched"
    target.write_bytes(b"keep")
    run.report.symlink_to(target)
    rejected = run([result("x")])
    assert rejected.returncode == 2 and b"symlink" in rejected.stderr and not run.raw.exists()
    assert run.report.is_symlink() and target.read_bytes() == b"keep"
    run.report.unlink()

    for child_rc in (0, 23):
        failed = run([result("长" * 3000)], child_rc, block_report=True, expected_rc=child_rc or 1)
        assert b"could not write report" in failed.stderr and os.fsencode(run.raw) in failed.stderr
        for false_claim in ("完整报告：", "已写入"):
            assert false_claim not in failed.stdout.decode()
        assert run.report.is_dir() and set(run.raw.parent.iterdir()) == {run.raw, run.report}
        run.report.rmdir()

    other = run.raw.with_name("other.jsonl")
    other_report = other.with_suffix(".md")
    other.write_bytes(b"original raw")
    other_report.write_bytes(b"original report")
    run.raw.unlink()
    run.raw.symlink_to(other)
    rejected = run([result("must not run")])
    assert rejected.returncode == 2 and b"symlink" in rejected.stderr
    assert run.raw.is_symlink() and other.read_bytes() == b"original raw"
    assert other_report.read_bytes() == b"original report"


def test_malformed_and_broken_view_drain(run):
    malformed = [
        {"type": "item.completed", "item": "not-an-object"},
        {"type": "item.started", "item": 12},
        {"event": "result", "result": "not-an-object"},
        {"event": "step_update", "step_update": "not-an-object"},
        {"event": "step_update", "step_update": {"step_type": "tool", "tool_info": "not-an-object"}},
        {"type": "user", "message": "not-an-object"},
        {"type": "assistant", "message": {"content": ["not-an-object", {"type": "tool_use", "name": "read"}]}},
        {"type": "turn.completed", "usage": "not-an-object"},
    ]
    bulk = {"type": "text_delta", "delta": "x" * 65536}
    empty_tools = [dict(type="tool_call", item={"name": name}) for name in ("", None, "\nnot a tool name")]
    events = [*malformed, *empty_tools, *[bulk] * 64, result("rendered after malformed input")]
    completed = run(events)
    assert completed.returncode == 0 and not completed.stderr, completed.stderr
    out = completed.stdout.decode()
    assert out.count("→ tool\n") == 1 and "not a tool name" not in out
    assert out.endswith("■ SUCCESS \nrendered after malformed input\n")
    assert run.raw.stat().st_size > 4 * 1024**2

    # Fail only the live renderer; replay still uses the real jq.
    real_jq = shutil.which("jq")
    bin_dir = run.root / "bin"
    bin_dir.mkdir()
    jq = bin_dir / "jq"
    jq.write_text(f"#!{sys.executable}\n"
                  "import os, sys\n"
                  f"if 'report' in sys.argv: os.execv({real_jq!r}, [{real_jq!r}, *sys.argv[1:]])\n"
                  "sys.stdin.buffer.readline()\n"
                  "sys.exit(5)\n")
    jq.chmod(0o700)
    path = os.pathsep.join([str(bin_dir), os.environ["PATH"]])
    failed_view = run(events, path=path, expected_rc=5)
    assert failed_view.returncode == 5 and not failed_view.stdout
    assert b"view failed (exit=5)" in failed_view.stderr and os.fsencode(run.raw) in failed_view.stderr
    assert run.raw.stat().st_size > 4 * 1024**2  # Run also compares every byte.
    assert run([result("child failed")], 23, path=path).returncode == 23


def test_final_body_selection(run):
    final = "N" * 8001
    completed = run([
        result("old body"), item("agent_message", text="fallback"),
        result(final), result("", "ERROR", "late failure"),
    ], 9)
    lines = completed.stdout.decode().splitlines()
    assert lines[:3] == ["■ SUCCESS ", "■ SUCCESS ", "■ ERROR late failure"]
    assert "old body" not in lines and "fallback" not in lines
    assert lines[-1] == f"完整报告：{run.report}" and run.report.read_bytes() == final.encode()

    completed = run([
        result("stale result"), item("agent_message", text="draft"),
        item("agent_message", text="keep this body"), item("agent_message", text=""),
        {**item("agent_message", text="not final"), "delta": True},
        result("", "ERROR", "actual failure"),
    ], 9)
    assert completed.stdout.decode().splitlines() == ["■ SUCCESS ", "■ ERROR actual failure", "keep this body"]
    assert not run.report.exists()

    completed = run([result("earlier"), item("agent_message", text=final), result("")])
    assert completed.stdout.decode().splitlines()[-1] == f"完整报告：{run.report}"
    assert run.report.read_bytes() == final.encode()


def test_error_control_budget(run):
    session = {"type": "thread.started", "thread_id": "direct"}
    noisy = []
    for i in range(40):
        message = f"{i}: " + "错" * 100
        noisy.extend([item("error", message=message), {"type": "turn.failed", "message": message},
                      {"type": "turn.completed", "usage": {"input_tokens": i}}])
    tools = [dict(type="tool_call", item={"name": f"t{i}"}) for i in range(80)]
    for size in (8000, 9000):
        completed = run([
            session, *tools, result("B" * size, "ERROR", "E" * 11000),
            *noisy, result("", "SUCCESS"),
        ], 23)
        assert completed.returncode == 23 and not completed.stderr, completed.stderr
        assert len(completed.stdout) < 9500
        lines = completed.stdout.decode().splitlines()
        assert lines[0] == "▶ codex 会话 direct"
        error = next(line for line in lines if line.startswith("■ ERROR "))
        assert 0 < len(error.removeprefix("■ ERROR ")) <= 200
        assert "■ SUCCESS " in lines and any(line.startswith("■ 完成") for line in lines)
        for prefix in ("⚠ ", "✗ "):
            assert 0 < sum(line.startswith(prefix) for line in lines) < 40
        if size == 8000:
            assert lines[-1] == "B" * size and not run.report.exists()
        else:
            assert lines[-1] == f"完整报告：{run.report}" and run.report.read_bytes() == b"B" * size


if __name__ == "__main__":
    for case in (test_compact_view, test_tool_budget, test_long_report_and_cleanup,
                 test_codex_fallback_and_claude_result, test_report_path_protection,
                 test_malformed_and_broken_view_drain, test_final_body_selection, test_error_control_budget):
        with tempfile.TemporaryDirectory(prefix="cockpit-view-test-") as root:
            case(Run(root))
        print(f"PASS: {case.__name__}")
