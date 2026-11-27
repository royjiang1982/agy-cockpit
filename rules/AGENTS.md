# AGY Cockpit 通用子代理派发触发规则

- **仅主代理生效**：若当前会话为被派出子代理或 Swarm Worker（含 `SA_SUBAGENT=1`、带任务书/brief 或子代理须知），完全忽略本规则。
- **接管与回退**：需派发子代理任务（单角色或多方会审）时，先运行 `python3 ~/.gemini/config/plugins/agy-cockpit/skills/cockpit-dispatch/scripts/resolve.py --role <role>`（或激活 `cockpit-dispatch` Skill）；当 Cockpit 已开启「接管子代理编排」（`mode != "disabled"`）时，以 Cockpit 返回的引擎、模型与席位路由覆盖用户/项目既有路由配置；关闭时（`mode == "disabled"`）切回用户或项目原有派发规范。
- **红线与流程优先**：用户与工作区 `AGENTS.md` / `dispatch.md` 中的安全、权限、凭据、提交、小修/准入与验收红线始终优先，不得放宽。
