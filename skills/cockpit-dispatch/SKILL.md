---
name: cockpit-dispatch
description: >-
  AGY Cockpit 通用 5 角色子代理派发与 Fallback 调度指南（覆盖软件研发、视频制作、亚马逊选品与运营分析）。
  当主代理需要将复杂任务拆分给子代理（快搜侦察、视觉任务、逻辑任务、专家验收、多方会审）时激活本 Skill，先按插件内置 `resolve.py` 查询是否开启「接管子代理编排」：开启时接管编排策略并覆盖用户/项目派发配置，关闭时切回用户/项目原有设置。
  被派出子代理与 Swarm Worker 严禁激活或执行本 Skill。
---

# AGY Cockpit 通用子代理派发手册

> **作用域与优先级守卫**：本 Skill 仅限主代理使用；子代理 / Swarm Worker（含 `SA_SUBAGENT=1` 或带任务书/brief）严禁派单。
> 当 `resolve.py` 返回 `mode != "disabled"`（即 Cockpit 已开启「接管子代理编排」）时，子代理引擎/模型/席位路由以 Cockpit 解析结果为准，覆盖用户或项目既有路由配置；当 `mode == "disabled"` 时切回用户或项目原有设置。用户与工作区的安全、凭据、Git 提交、小修/准入与验收红线始终最高优先，不得放宽。

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

也可运行 `--all` 一次查看 5 个角色的解析结果与原因（`reason`）。若 `mode == "disabled"`（未开启「接管子代理编排」），切回用户或项目原有派发配置（无配置则由主代理直接完成或按原生方式处理）。
多方会审支持按顺序取前 N 个可用席位（例如单方会审 `--role panel --count 1`、两方会审 `--role panel --count 2`）；若某席位不可用，解析器自动顺延至下一个可用席位，全部不可用时安全兜底到内置独立盲审。

---

## 二、第二步：按解析结果执行对应通道

严格遵循解析结果返回的 `channel`（不自行猜模型名、不静默升贵模型）：

### 1. `channel == "invoke_subagent"`（轻量内置，默认首选）
- 直接调用内置 `invoke_subagent`：
  - `TypeName` 使用解析返回的 `type`（`scout` 固定 `research` 纯只读；可写角色使用 `self`）；
  - `Model` 使用解析返回的 `model`（`flash` / `inherit` / `pro`）。
- 适合单步任务、快搜侦察，以及无外部引擎时的**真盲审独立多席位**（并发调用多个 `invoke_subagent`，各席互不可见）。

### 2. `channel == "swarm"`（可选公开协作增强）
- **明确授权**：当策略 JSON 中显式开启 `allow_swarm: true` 且 `resolve.py` 返回 `channel == "swarm"` 时，本 Skill 授权主代理按以下约束调用 `swarm` 工具：
  1. 先调用 `swarm(action:"models", all:true)` 核实目标模型名确在本会话可用列表内；若不可用则顺延下一备用候选，**不猜近似名**。
  2. 为单角色精确模型任务调用 `swarm(action:"add", name:<role>, model:<model>, brief:<任务书>)`；编辑代码时多 Worker 并行须用 `workspace:"share"`。
  3. **运行中 Fallback 热切**：仅在目标 Worker 状态为 `waiting` 且因配额/限流受阻时，调用 `swarm(action:"model", name:..., model:<fallback_model>)` 保留对话历史续跑。
  4. 任务交付验收完成后，调用 `swarm(action:"kill", name:...)` 清理不再需要的 Worker（先合并/取回产物再 kill）。
- **盲审边界诚实声明**：Swarm 共享板对所有成员公开，用于多视角公开辩论与协作制作；若任务明确要求**互不见面的严格独立盲审**，禁止用 Swarm 共享板冒充，改用并发独立 `invoke_subagent` 或外部 CLI，做不到则明确汇报缺口。

### 3. `channel == "cli"`（外部常驻引擎增强）
- 在 `resolve.py` 探测到 `PATH` 或常用包装目录（`SA_RUNTIME_BIN`、`~/Dev/_shared/agent-runtime/bin`、`~/.gemini/config/bin`、`~/.local/bin`）中已安装对应包装命令（`sa-agy` / `sa-codex` / `sa-droid` / `sa-grok` / `sa-pi`）时返回；若对应目录不在当前 shell `PATH`，请使用绝对路径调用。
- 主代理按解析返回的 `engine` 与参数后台执行并挂超时兜底（当 `effort` 为空串时省略 `--effort`/`-r`）：
  - `sa-agy` 仅传完整 `--model <model>`（不传独立 `--effort` 参数）；
  - `sa-codex` 传 `-m <model> --effort <effort>`（`codex_fast` 由 `sa-codex` 自动读取 Cockpit 策略生效，无需手传）；
  - `sa-droid` 传 `-m <model> -r <effort>`；
  - `sa-grok` 传 `-m <model> --effort <effort>`；
  - `sa-pi` 传 `-m <model>:<effort>`（无 `effort` 时仅 `-m <model>`）。
- **只读边界声明**：外部 CLI（`sa-agy` / `sa-codex` / `sa-droid` / `sa-grok` / `sa-pi`）与 `swarm` 的 `read_only: true` 属于**任务书与治理约束，并非 OS 只读沙箱**（其中 `sa-droid` 与 `sa-pi` 未挂 `sa-guard` 钩子）；若任务要求强系统只读隔离，优先使用内置 `invoke_subagent(TypeName="research")`，并在任务书中明写只读禁令。
- **失败态区分（禁止盲目重派）**：
  - **未启动即失败 / 预检不可用**：安全改用 `fallbacks` 列表中的下一可用候选（直至内置兜底）。
  - **已启动后中途失败 / 断线**：先核对现场产物与改动，优先同会话续接或明确交接，绝不把已产生写操作或外部副作用的任务当作全新单重发。

---

## 三、通用任务书模板

### A. 快搜短卡（仅限 `scout` 纯只读）
1. **只读禁令**：声明本任务为【被派出子代理 · 纯只读】，禁止写/改/删文件、派生子代理、读取凭据。
2. **问题**：1–3 个具体事实问题。
3. **范围**：限定目录、文档、页面或数据口径，命中即停。
4. **交付**：依据路径/出处 + ≤5 条结论 + 查过范围，不提改写方案。

### B. 完整任务书（`standard` / `deep` / `expert`）
- **身份声明**：开头显式标注「被派出子代理任务书（禁止派生子代理）」（对内置 `self` 与 `swarm` 尤为重要）。
- **Goal**：一句话写清要达成的可验证结果。
- **Context**：已知资料、路径、约束决策、目标受众或口径定义。
- **Constraints**：允许改动范围、禁区（禁止擅自提交/发布/写外部服务/读取凭据/派生子代理）、最小必要原则。
- **交付标准（DoD）**：
  - *软件研发*：指定定点单测/类型检查命令，附变更文件与真实命令输出。
  - *视频制作*：分镜/脚本/字幕/素材清单路径、时长与画幅口径、自检要点。
  - *亚马逊选品/运营分析*：数据来源与时间窗口、计算口径、结论与风险分离、禁止编造未核实数字。

### C. 多方盲审任务书（`panel`，纯只读并发）
- **盲审纪律**：主代理不充任席位；各席位拿到同一份材料并发独立作答、互不见面，任务书开头显式声明「被派出子代理 · 纯只读独立评审，禁止写文件或派生子代理」。
- **输出结构**：区分「已确认缺陷（附出处/行号）」「边界风险」「额外考虑」，主代理核实证据后统一汇总共识与分歧交用户拍板。
