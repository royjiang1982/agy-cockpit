# AGY Cockpit 插件源码仓

`~/Dev/agy-cockpit/` 是 `agy-cockpit` Antigravity 插件的唯一 Git 源码仓。

## 目录结构
- `plugin.json` / `hooks.json` / `assets/` / `README.md`：插件清单、Antigravity 原生 `PreToolUse` 防套娃钩子与资源
- `rules/AGENTS.md`：精简常驻派发触发规则（含主代理直做边界与 5 角色必派清单；开启「接管子代理编排」时接管派发路由，关闭时切回用户/项目原有设置）
- `skills/cockpit-dispatch/`：通用 5 角色派发 Skill 与零端口本地解析脚本 `scripts/resolve.py`
- `runtime/`：自包含常驻子代理运行时（`agent_runtime/`、`bin/sa-*`、`bin/sa-run.sh`、`guard/sa-guard*` 与单测 `tests/`）
- `sidecars/cockpit/`：驾驶舱 Python Sidecar、设置抽屉静态资源与单测
- `install.sh`：带内存语法预检的单向发布脚本，同步到 `~/.gemini/config/plugins/agy-cockpit/`（绝不触碰 `~/.gemini/antigravity/sidecar_data/`）

## 开发与验证约束
- 遵守 `~/Dev/AGENTS.md`：新增业务逻辑放独立叶子（≤400 行）；`server.py`、`subagents.py`、`index.html` 等历史大文件仅做 ≤20 行浅层接线；新增随 Sidecar 发布的 Python 文件须同步登记到 `server.py` 的 `WATCHED_PY_FILES`。
- 策略配置文件路径统一复用 `~/.gemini/antigravity/sidecar_data/statusline/dispatch-policy.json`；严禁在配置中写入可执行路径或命令模板。
- 运行单测：
  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s sidecars/cockpit -p "test_*.py" -v
  PYTHONDONTWRITEBYTECODE=1 python3 -B runtime/tests/run_all.py
  PYTHONDONTWRITEBYTECODE=1 python3 -B runtime/tests/test-sa-guard.py  # 只跑守卫
  ```
- 发布到本机插件目录：
  ```bash
  ./install.sh
  ```

## 测试治理
- 回归优先追加到同领域套组；仅职责或夹具明显独立时新建，大套组按职责拆开。新增运行时套组须接入 `runtime/tests/run_all.py`（可经 `verify_agent_runtime.py`）。
- 测可观察契约和边界：守卫拒绝、两端路由一致性、并发与取消、失败降级、隔离写回。不要重复测同一路径的同一契约，也不锁定无关实现细节。
- 禁用固定睡眠等就绪、毫秒级墙钟断言和个人绝对路径；用事件、锁、协议确认或注入时钟，等待设宽裕兜底超时，文件写入临时目录。
- 假 ACP/Pi 和本地 Daemon 共用 `runtime/tests/_runtime_fixture.py`。移动或移除已监控的 Python 文件时同步 `WATCHED_PY_FILES`；不要登记不随安装发布的 `runtime/tests/*`。
