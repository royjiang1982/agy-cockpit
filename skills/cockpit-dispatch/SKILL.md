---
name: cockpit-dispatch
description: >-
  AGY Cockpit 通用 5 角色子代理派发、常驻执行与 Fallback 调度手册（覆盖软件研发、视频制作、亚马逊选品与运营分析）。
  主代理不确定接管状态、超出直做边界或命中高危逻辑时，先运行 `resolve.py` 查询路由。覆盖跨目录检索（scout）、UI/视觉/图文（standard）、后端/逻辑/跨文件修改（deep）、疑难排错/高危验收（expert）、多方评审（panel）。`mode == "disabled"` 切回用户/项目原有设置；`mode == "degraded"` 停止派单并报告策略损坏。
  被派出子代理与 Swarm Worker 严禁激活或执行本 Skill。
---

# AGY Cockpit 通用子代理派发手册

> **作用域与优先级守卫**：本 Skill 仅限主代理使用；子代理 / Swarm Worker（含 `SA_SUBAGENT=1` 或带任务书/brief）严禁派单。
> 接管正常时，子代理引擎/模型/席位路由与常驻执行均以 Cockpit 为准。用户与工作区的安全、凭据、Git 提交与验收红线始终优先，不得放宽；用户明确要求「不用子代理」时不派单。

---

## 一、第一步：先判分级，再零端口查路由

### 1. 主代理直做 vs 必派子代理（按顺序判断）
- **仅以下情况由主代理直做**（无需派单）：
  1. 纯问答、概念讨论或常规方案自审；
  2. 已知位置的定点核查，累计 **≤2 轮查读且 ≤5 个文件/页面**；**触及上限仍未定位，立刻转 `scout`**；
  3. 位置明确且**不触及高危逻辑**（权限、计费/口径、加密、持久化状态机/Schema、核心公开契约）的局部小修（**≤3 文件且净增删 ≤60 行**）、确定性小替换，或简单 UI 样式/排版/文案微调。
- **超出直做边界或命中高危逻辑，接管正常时主动派单**（不为“是否派单”弹窗请示）：
  - **高危改动禁直做**：凡实际改变权限、计费/口径、加密、持久化状态机/Schema 或核心公开契约，不论行数多少，均派 `deep` 实现并在定点检查通过后以 `--role expert --read-only` 补 1 轮独立验收。
  - **角色消歧与拆单**：前端页面/组件/视觉交互优先 `standard`，涉及复杂状态机、跨模块数据流或后端核心逻辑走 `deep`；大任务可先派 `scout` 摸底定位，再拆分独立方向并发派单。
  - **多方会审触发**：用户点名「会审/多方/盲审」直接派 `panel`；未点名但涉及架构选型、跨模块大重构或重大商业/创意方向取舍时，经用户同意后派 `panel`。

### 2. 零端口查询当前生效路由

不确定接管状态时及每次派单前，运行本插件自带的本地解析器（不依赖 Cockpit Sidecar 端口，Sidecar 未启动时同样有效）：

```bash
python3 ~/.gemini/config/plugins/agy-cockpit/skills/cockpit-dispatch/scripts/resolve.py --role <role>
```

可用 `<role>` 固定为 5 个通用角色：

| 角色 | 权限 | 典型适用场景（研发 / 视频制作 / 亚马逊选品与运营） |
|---|---|---|
| `scout`（快搜侦察） | **只读** | 找代码与调用点、核对多处配置、自查超限 / 抓竞品 ASIN、参数与评论 / 搜参考素材与行业资料 |
| `standard`（视觉任务） | 可写 | UI/前端页面与组件、视觉排版、看图分析 / 视频脚本与分镜 / 选品图文与 Listing 文案 |
| `deep`（逻辑任务） | 可写 | 后端/业务逻辑研发、>3 文件或 >60 行修改、状态机与契约 / 自动化剪辑与流水线脚本 / 多维选品测算与归因建模 |
| `expert`（专家验收） | 可写/验收 | 疑难排错攻坚、高危改动后的只读独立验收、安全与计费审计 / 成片脚本与合规把关 / 商业方案挑刺、数据口径复核 |
| `panel`（多方会审） | **只读** | 架构选型与重构评审 / 视频创意方向多视角评审 / 选品立项与定价策略多方辩论 |

- `mode == "disabled"`：立即退出 Cockpit 规则，切回用户/项目原有设置。 `mode == "degraded"`：停止派单，报告策略文件损坏（含 `warning`）；不套用默认路由、不擅自外发。
- `--all` 一次查看 5 个角色的解析结果与原因（`reason`）。`--read-only` 仅用于单角色，不能与 `--all` 同用；高危独立验收用 `--role expert --read-only`，该路由及兜底均为只读，不走 Swarm，内置固定 `research`。
- 盲审必须用 `--role panel --blind`（仅限 `panel`），排除席位首选与兜底中的 Swarm 共享板；无席位时 `blind_isolated` 为假。
- `--role panel --count N` 取前 N 个可用席位（`1 ≤ N ≤ 12`）；不可用则顺延，配置席位不足时用内置只读席位补足。可与 `--blind` 合用，如 `--role panel --blind --count 2`。

---

## 二、第二步：按解析结果执行对应通道

先判断 `mode == "panel"`：展开 `seats[]`，逐席按各自的 `channel` 并发执行，预检或未启动即失败时按该席的 `fallbacks` 顺序降级；不使用外层 `channel` 派单。盲审须保持各席互不可见，无席位则报告并停派。

再按以下 3 种通道执行单角色或已展开的单个席位，遵循返回的 `channel`（不自行猜模型名、不静默升贵模型）：

### 1. `channel == "cli"`（插件内置常驻引擎，已装对应官方 CLI 时首选）
插件自包含常驻子代理运行时（`~/.gemini/config/plugins/agy-cockpit/runtime/bin/`），通过 `sa-subagentd`（Unix Socket `/tmp/dev-subagentd-$UID/daemon.sock`）复用常驻连接。不得用 `agy -p` 执行任务；该限制不含运行时内部的配额探测。

**统一执行格式**：`sa-run.sh` 是前台包装器，打印进度，每次截断覆盖同名 `.jsonl`；续接须换新日志文件名。后台与超时兜底由宿主工具负责（轻侦察 `120s` / 慢轨 `270s`），同回合先向用户简述分工，再切后台等待唤醒。

`.jsonl` 日志文件名建议使用不超过 10 字的核心任务名，方便在 Cockpit 面板辨认。

```bash
PLUGIN_BIN=~/.gemini/config/plugins/agy-cockpit/runtime/bin
"$PLUGIN_BIN/sa-run.sh" tmp/<任务>/<名>.jsonl "$PLUGIN_BIN/sa-<engine>" <参数> --prompt "<任务书>"
```

各引擎参数约定（`effort` 为空串时省略 `--effort`/`-r`）：
- `sa-agy`：`--model <model>`（完整模型 ID，不传独立 `--effort`；低配额时可按策略预检切换引擎）。
- `sa-codex`：`-m <model> --effort <effort>`（`codex_fast` 由 `sa-codex` 自动读取 Cockpit 策略生效，单次可用 `--fast`/`--no-fast` 覆盖）。
- `sa-droid`：`-m <model> -r <effort>`。
- `sa-grok`：`-m <model> --effort <effort>`。
- `sa-pi`：`-m <model>:<effort>`（无 `effort` 时仅 `-m <model>`）。

**会话续接与守护运维**（同一工作线最多续接 ~3 轮，上下文膨胀后新开）：
- 从 `.jsonl` 最终 `event: "result"` 记录读取 `agent`（实际的 `effective_agent`）与 `conversation_id`（Codex 用 `thread_id`）。低配额触发引擎切换后，按该记录的实际引擎选续接包装器，不用最初派发的包装器或旧会话 ID。最终报告取 `result.response`（或 Codex 最后一条非 delta 的 `agent_message`）。
- 续接参数：`sa-agy --conversation <id>`、`sa-codex resume <id>`、`sa-grok -r <id>`、`sa-droid -s <id>`、`sa-pi --session <session_file>`。Pi 必须传实际存在的会话文件路径（优先取该会话 `init` 记录的非空 `session_file`，或确认最终 `conversation_id` 为存在的文件路径；无文件路径则不可续接）。
- 守护进程管理：`"$PLUGIN_BIN/sa-subagentd" status|restart|stop`。

**安全与只读边界**：
- Antigravity 宿主通过本插件根目录 `hooks.json` 自动启用 `runtime/guard/sa-guard.sh agy` 防套娃与高危命令拦截；若在 Codex / Grok 中也需挂守卫，可将 `~/.codex/hooks.json` / `~/.grok/config.toml` 的 PreToolUse 指向 `~/.gemini/config/plugins/agy-cockpit/runtime/guard/sa-guard.sh <codex|grok>`。
- `read_only: true` 时任务书须明确禁止写文件。外部 CLI（含 `sa-droid`/`sa-pi`，后两者未挂 `sa-guard`）与 `swarm` 的只读属于**任务书与治理约束，并非 OS 只读沙箱**；强只读隔离优先用内置 `invoke_subagent(TypeName="research")`。
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
- **Title**：可在 `## Title` 下一行写不超过 10 字的核心任务名，供 Cockpit 面板展示。
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

---

## 四、验收与返工闭环

- **主代理核对**：以派发前 `git status` 为基线核对实际变更路径、diff 与检查结果，不轻信口头报告；发现范围外改动立即止损并仅撤回确认属本任务的越界改动。
- **高危改动独立验收**：触及权限、计费/口径、加密、持久化状态机/Schema 或核心公开契约的实现，在定点检查通过后用 `--role expert --read-only` 查路由，接管正常时自动派 1 轮独立验收审阅 diff（免弹窗），常规改动由主代理自审收尾。
- **返工与熔断**：需修复时仅把明确现象、失败输出与定位发回原会话并只重跑失败项；连续 2 轮失败先停下诊断再决定下一步。
