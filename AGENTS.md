# AGY Cockpit 插件源码仓

`~/Dev/agy-cockpit/` 是 `agy-cockpit` Antigravity 插件的唯一 Git 源码仓。

## 目录结构
- `plugin.json` / `hooks.json` / `assets/` / `README.md`：插件清单、Antigravity 原生 `PreToolUse` 防套娃钩子与资源
- `rules/AGENTS.md`：常驻极短触发规则（≤5 行；开启「接管子代理编排」时接管派发路由，关闭时切回用户/项目原有设置）
- `skills/cockpit-dispatch/`：通用 5 角色派发 Skill 与零端口本地解析脚本 `scripts/resolve.py`
- `runtime/`：自包含常驻子代理运行时（`agent_runtime/`、`bin/sa-*`、`bin/sa-run.sh`、`guard/sa-guard*` 与单测 `tests/`）
- `sidecars/cockpit/`：驾驶舱 Python Sidecar、设置抽屉静态资源与单测
- `install.sh`：带内存语法预检的单向发布脚本，同步到 `~/.gemini/config/plugins/agy-cockpit/`（绝不触碰 `~/.gemini/antigravity/sidecar_data/`）

## 开发与验证约束
- 遵守 `~/Dev/AGENTS.md`：新增业务逻辑放独立叶子（≤400 行）；`server.py`、`subagents.py`、`index.html` 等历史大文件仅做 ≤20 行浅层接线；新增 `.py` 须同步登记到 `server.py` 的 `WATCHED_PY_FILES`。
- 策略配置文件路径统一复用 `~/.gemini/antigravity/sidecar_data/statusline/dispatch-policy.json`；严禁在配置中写入可执行路径或命令模板。
- 运行单测：
  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s sidecars/cockpit -p "test_*.py"
  ```
- 发布到本机插件目录：
  ```bash
  ./install.sh
  ```
