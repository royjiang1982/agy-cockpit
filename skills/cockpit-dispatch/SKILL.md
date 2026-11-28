---
name: cockpit-dispatch
description: >-
  主代理按 Cockpit 规则查路由、派单、续接及验收时使用；被派出子代理与 Swarm Worker 禁激活。
---

# Cockpit 派发与交接

## 路由

直做/必派、角色、接管开关与红线见 [常驻规则](../../rules/AGENTS.md)。前端偏视觉交互选 `standard`，复杂状态/跨模块数据流选 `deep`；需定位再拆单先用 `scout`。

- 每次派单前查本地 `resolve.py`，无需 Sidecar 端口。`--all` 看 5 角色与 `reason`；`--read-only` 只配单角色，首选与兜底均只读、不走 Swarm，内置固定 `research`。降级报 `warning`，不套默认路由。
- `--role panel --blind` 从首选及兜底排除 Swarm；无席则 `blind_isolated=false`，报停。`--count N`（1–12，仅 panel）将首选和备选均不可用的席记入 `skipped_seats`、顺延，不足用内置只读补齐；省略则按序返回所有启用席，不可用直接落 `builtin_fallback`，空列表不补。
- `mode=panel` 展开 `seats[]` 并发，逐席按自身 `channel`/`fallbacks`，不用外层 `channel`。按解析值执行，不猜模型或静默升贵。预检不可用/未启动失败才顺序降级；启动后失败/断线先核对 `git status`，优先原会话，禁把已写任务当新单重发。

## 执行与结果

`cli` 用常驻包装器（禁用 `agy -p` 派单，内部配额探测除外）：

```bash
PLUGIN_BIN=~/.gemini/config/plugins/agy-cockpit/runtime/bin
"$PLUGIN_BIN/sa-run.sh" tmp/<任务>/<名>.jsonl "$PLUGIN_BIN/sa-<engine>" <参数> --prompt "<任务书>"
```

前台包装器覆盖同名日志；续接换新文件，建议核心名 ≤10 字。同工作线续接约 ≤3 轮，膨胀新开。由宿主工具后台/超时兜底（轻侦察 120s、慢轨 300s）；同回合先报分工，再等唤醒。

| 包装器 | 模型 / effort | 续接 |
|---|---|---|
| `sa-agy` | `--model <model>`（完整 ID，不另传 effort） | `--conversation <id>` |
| `sa-codex` | `-m <model> --effort <effort>` | `resume <id>` |
| `sa-droid` | `-m <model> -r <effort>` | `-s <id>` |
| `sa-grok` | `-m <model> --effort <effort>` | `-r <id>` |
| `sa-pi` | `-m <model> --thinking <effort>`；也接受 `-m <model>:<effort>` | `--session <session_file>` |

effort 为空省略对应参数。Pi 仅识别受支持的 effort 后缀，显式 `--thinking` 优先。**续接**优先取 stdout 首条 `▶ <engine> 会话 <id>` 的实际引擎和 ID（配额切换不用旧值）。Pi 会话行带 `…`、路径不存在或需确认路径完整时，不用卡片值，直接定向读 `.jsonl` 头部 `init.session_file`；核实文件存在才续接，禁读全文。

**正文** ≤8,000 B 只读 stdout；有 `完整报告：<path>.md` 只 `read_file` 该文件一次。除上述 Pi 例外，禁为取正文或 ID 翻 `.jsonl`；仅异常/断线排障查末行。发布失败看 stderr，无可靠 ID 不猜。

- `invoke_subagent`：`TypeName`/`Model` 取解析的 `type`/`model`（`research`/`self`；`flash_lite`/`flash`/`inherit`/`pro`）。可并发多个 `research` 做独立席。
- `swarm` 仅策略显式 `allow_swarm: true`：先 `swarm(action:"models", all:true)` 核实模型；并行编辑用 `workspace:"share"`，取回产物后 `swarm(action:"kill", name:...)`。
- 运维、调试及手挂守卫见 [README](../../README.md#runtime-operations)。

## 任务书 Schema

开头标「被派出子代理任务书」；只读标「被派出子代理 · 纯只读」。主代理必填：

```md
## Title
≤10 字核心任务名（Sidecar 标签）
## Goal
一句可验结果；scout 限 1–3 个事实问题
## Anchors
path:Lstart-Lend / 符号 / 已核实状态；必要口径
## Skip
排除范围、已排除路径、禁扫生成物（如 node_modules/、dist/）
## Scope / DoD
读写边界、允许文件、定点命令或产物验收口径
```

禁内联大段代码/diff/日志/他席报告，给锚点。`read_only: true` 明禁写/改/删文件；只读验收不列改动/自修授权。`expert --read-only` 以 `git diff -- <允许文件>` 为主入口，只按行段补读直接调用方。独立盲审主代理不充任席位，各席同材料、互不可见、禁共享板；可预先给客观 `facts.md`（不得含主代理/他席判断或结论）。知情复验另标，可传短 diff / 锚点及首错，不冒充盲审。

**读取与反查（跨模型）**：已有锚点并发按行段读；围绕待证问题批搜字段/事件/符号的实际消费者，不按文件表通读。拟删/合/截断前必反查生产者、字面/正则消费者、是否反写状态与失败/缺失替代路径；验收再从变更处反走同一链，核实仍闭环。证据闭环即停，缺口直报，不设机械文件数/轮次上限。

### 最小安全须知（按通道附任务书）

- `invoke_subagent(research)` 快卡：纯只读，禁派生、读凭据；强只读隔离优先此通道。
- 所有慢轨或可写任务（已挂 guard 也须附）：禁派生、Git 写（commit/push/pull/merge，建删分支/worktree）、部署及读 `.env`/凭据。仅在授权范围，先 `git status --short`，他人改动勿动并列出；缺信息/矛盾/超权/遇拒停报，不绕过。仅跑 DoD；可写任务仅因本改动局部笔误/格式/类型首错可原会话自修 ≤1 次，否则停。
- `droid`/`pi`/`invoke_subagent(self)`/`swarm` 及未挂钩子的 `codex/grok` 无 `sa-guard` 运行时拦截；其只读仅为治理约束，非 OS 沙箱。

## 报告与闭环

`状态｜已证结论(path:line)｜检查命令与退出码/首错｜未验证/阻断`。评审分已证缺陷/边界风险/额外考虑；事实与推测分开，禁背景铺垫或复述任务书。侦察 ≤5 条，列查过范围、不提改写。

主代理派前留 `git status` 基线，验收核对实际路径/diff/检查；越界即止损，仅撤回确认属本任务的越界改动。复核各席证据、汇总共识/分歧交用户拍板。返工只发定位/现象/首错给原会话、只重跑失败项；连续两轮失败停下诊断。
