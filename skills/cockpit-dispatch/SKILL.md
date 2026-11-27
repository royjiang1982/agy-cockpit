---
name: cockpit-dispatch
description: >-
  AGY Cockpit 通用 5 角色子代理派发、常驻执行与 Fallback 调度手册（覆盖软件研发、视频制作、亚马逊选品与运营分析）。
  当主代理需要将任务拆分给子代理（快搜侦察、视觉任务、逻辑任务、专家验收、多方会审）时激活本 Skill，先按插件内置 `resolve.py` 查询是否开启「接管子代理编排」：开启时由 Cockpit 统一接管路由与执行，关闭时切回用户/项目原有设置。
  被派出子代理与 Swarm Worker 严禁激活或执行本 Skill。
---

# AGY Cockpit 通用子代理派发手册

> **作用域与优先级守卫**：本 Skill 仅限主代理使用；子代理 / Swarm Worker（含 `SA_SUBAGENT=1` 或带任务书/brief）严禁派单。
> 当 `resolve.py` 返回 `mode != "disabled"`（即 Cockpit 已开启「接管子代理编排」）时，子代理引擎/模型/席位路由与常驻执行均以 Cockpit 为准；当 `mode == "disabled"` 时切回用户或项目原有设置。用户与工作区的安全、凭据、Git 提交与验收红线始终最高优先，不得放宽。

---

## 一、第一步：零端口查询当前生效路由

每次派单前，运行本插件自带的本地解析器（不依赖 Cockpit Sidecar 端口，Sidecar 未启动时同样有效）：

```bash
python3 ~/.gemini/config/plugins/agy-cockpit/skills/cockpit-dispatch/scripts/resolve.py --role <role>
```

可用 `<role>` 固定为 5 个通用角色：

| 角色 | 权限 | 典型适用场景（研发 / 视频制作 / 亚马逊选品与运营） |
|---|---|---|
| `scout`（快搜侦察） | **只读** | 找代码与调用点、查文档 / 抓竞品 ASIN、参数与评论 / 搜参考素材与行业资料 |
| `standard`（视觉任务） | 可写 | UI/前端页面与组件、视觉排版、看图分析 / 视频脚本与分镜 / 选品图文与 Listing 文案 |
| `deep`（逻辑任务） | 可写 | 后端模块研发、状态机与契约 / 自动化剪辑与流水线脚本 / 多维选品测算与归因建模 |
| `expert`（专家验收） | 可写/验收 | 疑难排错、安全与计费审计 / 成片脚本与合规把关 / 商业方案挑刺、数据口径复核 |
| `panel`（多方会审） | **只读** | 架构选型与重构评审 / 视频创意方向多视角评审 / 选品立项与定价策略多方辩论 |

- 运行 `--all` 可一次查看 5 个角色的解析结果与原因（`reason`）。若 `mode == "disabled"`，切回用户或项目原有派发配置（无配置则由主代理直接完成或按原生方式处理）。
- 多方会审支持按顺序取前 N 个可用席位（例如单方 `--role panel --count 1`、两方 `--role panel --count 2`）；不可用自动顺延下一席位，全部不可用时安全兜底到内置独立盲审。
- **分级原则**：纯问答、已知位置的局部小修（不涉及权限/计费/持久化状态机/核心公开契约）与简单 UI 微调由主代理直接完成；其余复杂、跨文件、疑难或会审任务直接按解析结果派单，不为“是否派单”弹窗请示。

---

## 二、第二步：按解析结果执行对应通道

严格遵循解析结果返回的 `channel`（不自行猜模型名、不静默升贵模型）：

### 1. `channel == "cli"`（插件内置常驻引擎，已装对应官方 CLI 时首选）
插件自包含常驻子代理运行时（`~/.gemini/config/plugins/agy-cockpit/runtime/bin/`），通过 `sa-subagentd`（Unix Socket `/tmp/dev-subagentd-$UID/daemon.sock`）复用常驻连接，禁用 `agy -p` 无头模式。

**统一后台执行格式**（外层用插件自带 `sa-run.sh` 写 `.jsonl` 日志并打印进度，后台运行 + 挂超时兜底：轻侦察 `120s` / 慢轨 `270s`，同回合先向用户简述分工再切后台等待唤醒）：

```bash
PLUGIN_BIN=~/.gemini/config/plugins/agy-cockpit/runtime/bin
"$PLUGIN_BIN/sa-run.sh" tmp/<任务>/<名>.jsonl "$PLUGIN_BIN/sa-<engine>" <参数> --prompt "<任务书>"
```

各引擎参数约定（`effort` 为空串时省略 `--effort`/`-r`）：
- `sa-agy`：`--model <model>`（完整模型 ID，不传独立 `--effort`；低配额自动按策略预检切 Droid）。
- `sa-codex`：`-m <model> --effort <effort>`（`codex_fast` 由 `sa-codex` 自动读取 Cockpit 策略生效，单次可用 `--fast`/`--no-fast` 覆盖）。
- `sa-droid`：`-m <model> -r <effort>`。
- `sa-grok`：`-m <model> --effort <effort>`。
- `sa-pi`：`-m <model>:<effort>`（无 `effort` 时仅 `-m <model>`）。

**会话续接与守护运维**（同一工作线最多续接 ~3 轮，上下文膨胀后新开）：
- 从 `.jsonl` 提取 `conversation_id`（`sa-agy`/`sa-droid`/`sa-grok`/`sa-pi`）或 `thread_id`（`sa-codex`）；最终报告取最后一条 `result.response`（或 `sa-codex` 最后一条非 delta 的 `agent_message`）。
- 续接参数：`sa-agy --conversation <id>`、`sa-codex resume <id>`、`sa-grok -r <id>`、`sa-droid -s <id>`、`sa-pi --session <id>`。
- 守护进程管理：`"$PLUGIN_BIN/sa-subagentd" status|restart|stop`。

**安全与只读边界**：
- Antigravity 宿主通过本插件根目录 `hooks.json` 自动启用 `runtime/guard/sa-guard.sh agy` 防套娃与高危命令拦截；若在 Codex / Grok 中也需挂守卫，可将 `~/.codex/hooks.json` / `~/.grok/config.toml` 的 PreToolUse 指向 `~/.gemini/config/plugins/agy-cockpit/runtime/guard/sa-guard.sh <codex|grok>`。
- 外部 CLI（含 `sa-droid`/`sa-pi`，后两者未挂 `sa-guard`）与 `swarm` 的 `read_only: true` 属于**任务书与治理约束，并非 OS 只读沙箱**；强只读隔离优先用内置 `invoke_subagent(TypeName="research")`。
- **失败态区分**：未启动即失败或预检不可用时安全改用 `fallbacks` 下一候选；已启动中途失败/断线时先用 `git status` 核对现场，优先续接原会话，绝不把已产生写操作的任务当作新单盲目重发。

### 2. `channel == "invoke_subagent"`（无外部 CLI 时的默认兜底）
- 调用内置 `invoke_subagent`：`TypeName` 用解析返回的 `type`（只读角色固定 `research`，可写角色 `self`），`Model` 用解析返回的 `model`（`flash_lite` / `flash` / `inherit` / `pro`）。
- 适合无外部 CLI 时的单步任务、快搜与**真盲审独立多席位**（并发调用多个 `invoke_subagent`，各席互不可见）。

### 3. `channel == "swarm"`（仅策略 JSON 显式开启 `allow_swarm: true` 时）
- 先调 `swarm(action:"models", all:true)` 核实精确模型名；编辑代码并行须用 `workspace:"share"`；完成后取回产物并 `swarm(action:"kill", name:...)`。
- **盲审边界**：Swarm 共享板对成员公开，禁止冒充互不见面的独立盲审。

---

## 三、通用任务书模板与子代理须知

### A. 快搜短卡（仅限 `scout` 纯只读，免附慢轨须知）
1. **只读禁令**：声明本任务为【被派出子代理 · 纯只读】，禁止写/改/删文件、派生子代理、提交推送或读取凭据；遇拦截立即停报。
2. **问题**：1–3 个具体事实问题。
3. **范围**：限定目录、文档、页面或数据口径，命中即停，禁扫 `node_modules/`、`dist/`。
4. **交付**：文件路径+行号、≤5 条结论、查过范围，不提改写方案。

### B. 完整任务书（`standard` / `deep` / `expert`，须附文末「子代理须知」）
- **身份声明**：开头显式标注「被派出子代理任务书（禁止派生子代理）」。
- **Goal**：一句话写清要达成的可验证结果。
- **Context**：已知资料、路径、约束决策、目标受众或口径定义。
- **Constraints**：允许改动范围、单文件 ≤400 行抽独立叶子（历史大文件 ≤20 行接线）、禁区与最小必要原则。
- **交付标准（DoD）**：只列目标单测/类型检查等定点命令（或视频/选品交付口径），附变更文件与真实结果。

### C. 多方盲审任务书（`panel`，纯只读并发）
- **盲审纪律**：主代理不充任席位；各席拿同一材料并发独立作答、互不见面，开头显式声明「被派出子代理 · 纯只读独立评审，禁止写文件或派生子代理」。
- **输出结构**：分「已确认缺陷（附出处/行号）」「边界风险」「额外考虑」，主代理按审查三问核实证据后汇总共识与分歧交用户拍板。

### 子代理须知（慢轨任务书附本节）
- 禁派生子代理；禁 `git commit/push/pull/merge`、建删分支或 worktree、线上部署及读取 `.env`/凭据。
- 仅改任务书允许范围；先跑 `git status --short`，非本任务改动勿动并列出；遇缺信息、矛盾、超授权或被拦截立即停报，严禁绕过。
- 仅跑 DoD 指定的定点检查；首轮仅因本改动局部笔误、格式或类型失败可原会话自修 1 次，否则即停。
- 报告区分事实/推测/建议并附行号或产物依据，列核查/修改范围、检查结果与未完成项。
