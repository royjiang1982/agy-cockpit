"""Label priority, quoted runner parsing, and panel discovery regressions."""

import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import subagents
from subagent_labels import clamp_task_label, dispatch_paths, extract_cli_task_label, format_native_label
from test_orchestrator_recognition import _build_db, _field_bytes


def _cli_label(prompt="", stem="", cat_stem="", **args):
    return extract_cli_task_label("sa-codex --prompt " + shlex.quote(prompt), args, "codex", stem, cat_stem)


def _tool_payload(tool, **args):
    return _field_bytes(1, tool.encode()) + _field_bytes(2, json.dumps(args, ensure_ascii=False).encode())


class SubagentLabelTests(unittest.TestCase):
    def assertLabel(self, actual, expected):
        self.assertEqual(actual, expected)
        self.assertLessEqual(sum(11 if ch.isascii() else 20 for ch in actual), 200)

    def test_clamp_cleans_markdown_and_drops_partial_words_and_punctuation(self):
        self.assertLabel(clamp_task_label("  ## **修复接口**！！ "), "修复接口")
        self.assertLabel(clamp_task_label("一二三四五六七八九十十一"), "一二三四五六七八九十")
        self.assertLabel(clamp_task_label("修复接口、 LongIdentifierTooManyLetters"), "修复接口")
        self.assertLabel(clamp_task_label("repair config and route behavior"), "repair config")
        self.assertEqual(clamp_task_label("检查接口连接", max_chars=4), "检查接口")

    def test_explicit_title_overrides_stem_and_goal(self):
        cmd = 'sa-codex --prompt "## Title\n**\\"会话绑定修复\\"**\n\n## Goal\n修复日志读取"'
        self.assertLabel(extract_cli_task_label(cmd, {}, "codex", "子代理名称优化"), "会话绑定修复")
        self.assertLabel(_cli_label("## 标题\n复查配置", stem="规则脚本评审"), "复查配置")
        self.assertLabel(_cli_label("任务名：终审路由"), "终审路由")
        self.assertLabel(_cli_label("Title: Router's review"), "Router's review")

    def test_chinese_log_stem_overrides_goal_and_removes_round_suffix(self):
        self.assertLabel(_cli_label("## Goal\n分析调用路径", stem="sa-tmp-子代理名称优化-r2"), "子代理名称优化")

    def test_extracts_chinese_and_english_goals(self):
        cases = (
            ("## Goal\n修复 AGY Cockpit 的规则/Skill 文档与路由解析层缺陷。其次补单测。", "修复规则与路由缺陷"),
            ("## 目标\n对 `sidecars/cockpit/subagents.py`（不改文件）进行高危改动独立验收。", "高危改动独立验收"),
            ("## Goal\n对当前工作区未提交的 commit abc1234 做终审只读 Review。", "终审只读Review"),
            ("## Goal\nFix AGY Cockpit's parser defects in the current workspace. No edits.", "Fix parser defects"),
            ("修复", "backend-routing"),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                self.assertLabel(_cli_label(prompt, stem="subagent-backend-routing-r2"), expected)

    def test_extracts_short_card_and_dispatch_header(self):
        self.assertLabel(_cli_label("2. **问题**：排查新版登录路由失败？只读查看。"), "排查登录路由失败")
        self.assertLabel(_cli_label("【被派出子代理 · 只读】\n检查路由配置。列出证据。"), "检查路由配置")

    def test_fallback_priority(self):
        self.assertLabel(_cli_label(stem="sa-metrics-r3", toolSummary="统计成本", cat_stem="路由"), "metrics")
        self.assertLabel(_cli_label("## Goal\n修复 `/tmp/project/entry.py`。", toolSummary="**统计成本**", cat_stem="路由"), "统计成本")
        self.assertLabel(_cli_label(cat_stem="规则脚本评审"), "规则脚本评审")
        self.assertLabel(_cli_label(), "codex-cli")

    def test_generic_native_role_uses_prompt_but_specific_role_is_preserved(self):
        self.assertLabel(format_native_label("research", "## 目标\n排查缓存统计偏差。"), "排查缓存统计偏差")
        self.assertLabel(format_native_label("self", "## Title\n名称优化"), "名称优化")
        self.assertLabel(format_native_label("subagent", "查找面板入口。"), "查找面板入口")
        self.assertLabel(format_native_label("**Review**", "## Title\n路由评审"), "Review")
        self.assertLabel(format_native_label("subagent"), "subagent")

    def test_sa_run_matches_quoted_paths_and_optional_separator(self):
        for command, path in (
            ('"$PLUGIN_BIN/sa-run.sh" tmp/run/foo.jsonl "$PLUGIN_BIN/sa-codex" --prompt hi', "tmp/run/foo.jsonl"),
            ("'/plugin bin/sa-run.sh' 'tmp/run logs/foo.jsonl' -- '/plugin bin/sa-codex'", "tmp/run logs/foo.jsonl"),
            ("sa-run.sh 20 2 tmp/run/foo.jsonl -- sa-codex", "tmp/run/foo.jsonl"),
        ):
            with self.subTest(command=command):
                match = subagents.RE_SA_RUN.search(command)
                self.assertIsNotNone(match)
                self.assertEqual(dispatch_paths(match, Path("/home/example")), (path, "sa-codex"))

    def test_quoted_wrapper_resumes(self):
        sid = str(uuid4())
        for engine, option, pattern in (
            ("agy", "--conversation", subagents.RE_AGY_RESUME),
            ("codex", "resume", subagents.RE_CODEX_RESUME),
            ("droid", "-s", subagents.RE_DROID_RESUME),
            ("grok", "--resume", subagents.RE_GROK_RESUME),
            ("pi", "--session", subagents.RE_PI_RESUME),
        ):
            for quote in ("\"", "'", ""):
                with self.subTest(engine=engine, quote=quote):
                    match = pattern.search(f"{quote}$PLUGIN_BIN/sa-{engine}{quote} {option} {sid}")
                    self.assertIsNotNone(match)
                    self.assertEqual(match[1], sid)

    def test_discovery_binds_relative_log_and_returns_core_panel_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "app"
            (app / "conversations").mkdir(parents=True)
            first, resumed = str(uuid4()), str(uuid4())
            for cwd_args in ({"Cwd": str(root / "work")}, {}):
                with self.subTest(cwd_args=cwd_args):
                    parent = str(uuid4())
                    dev = Path(cwd_args.get("Cwd") or root / "Dev")
                    log = dev / "agy-cockpit/tmp/logs with spaces/规则脚本评审-r2.jsonl"
                    log.parent.mkdir(parents=True)
                    log.write_text(json.dumps({"type": "thread.started", "thread_id": first}) + "\n", encoding="utf-8")
                    run = 'cd agy-cockpit && "$PLUGIN_BIN/sa-run.sh" "tmp/logs with spaces/规则脚本评审-r2.jsonl" -- "$PLUGIN_BIN/sa-codex"'
                    resume = f'"$PLUGIN_BIN/sa-codex" resume {resumed} --prompt "## Title\n恢复执行"'
                    rows = [
                        (1, 132, 2, b"", _tool_payload("run_command", CommandLine=run, **cwd_args)),
                        (2, 132, 2, b"", _tool_payload("run_command", CommandLine=resume)),
                        (3, 132, 2, b"", _tool_payload("invoke_subagent", TypeName="research", Task="## 目标\n排查缓存统计偏差。")),
                    ]
                    _build_db(app / "conversations" / f"{parent}.db", [], rows)
                    with patch.multiple(
                        subagents,
                        APP_DIRS=[("App", app)],
                        SUBAGENT_APP_DIRS=[("App", app)],
                        HOME=root,
                    ), patch.object(subagents, "analyze_codex_thread", return_value=None):
                        stubs = {stub["id"]: stub for stub in subagents.discover_subagents(parent)}
                        _, items = subagents.collect_subagents(parent)
                    self.assertEqual(stubs[first]["raw_file"], str(log))
                    self.assertTrue(Path(stubs[first]["raw_file"]).is_absolute())
                    self.assertLabel(stubs[first]["label"], "规则脚本评审")
                    self.assertLabel(stubs[resumed]["label"], "恢复执行")
                    self.assertLabel(stubs["pending-3"]["label"], "排查缓存统计偏差")
                    self.assertEqual(
                        {item["id"]: item["label"] for item in items},
                        {first: "规则脚本评审", resumed: "恢复执行", "pending-3": "排查缓存统计偏差"},
                    )


if __name__ == "__main__":
    unittest.main()
