# AGY Cockpit 通用子代理派发触发规则

- **仅主代理生效**：若当前会话为被派出子代理或 Swarm Worker（含 `SA_SUBAGENT=1`、带任务书/brief 或子代理须知），完全忽略本规则。
- **接管与回退**：需拆分多角色子代理任务时，先运行插件内置 `resolve.py`（或激活 `cockpit-dispatch` Skill）查询当前配置；当 Cockpit 已开启「接管子代理编排」（`mode != "disabled"`）时，全面接管子代理编排策略，覆盖用户或项目既有派发配置；关闭时（`mode == "disabled"`）切回用户或项目原有派发规范。
- **红线优先**：用户与工作区 `AGENTS.md` 中的安全、权限、凭据、提交与验收红线始终优先，不得放宽。
