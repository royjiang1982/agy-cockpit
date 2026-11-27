# AGY Cockpit 通用子代理派发触发规则

- **仅主代理生效**：若当前会话为被派出子代理或 Swarm Worker（含 `SA_SUBAGENT=1`、带任务书/brief 或子代理须知），完全忽略本规则。
- **接管与回退**：需派发子代理任务（单角色或多方会审）时，先运行 `python3 ~/.gemini/config/plugins/agy-cockpit/skills/cockpit-dispatch/scripts/resolve.py --role <role>` 并按 `cockpit-dispatch` Skill 执行；当 Cockpit 已开启「接管子代理编排」（`mode != "disabled"`）时，子代理路由、常驻包装与执行护栏统一由 Cockpit 接管；关闭时（`mode == "disabled"`）切回用户或项目原有设置。
- **红线优先**：用户与工作区 `AGENTS.md` 中的安全、权限、凭据、提交与验收红线始终优先，不得放宽。
