# AGY Cockpit

[![Antigravity Plugin](https://img.shields.io/badge/Antigravity-Plugin-4285F4?logo=google&logoColor=white)](https://github.com/royjiang1982/agy-cockpit)
[![Version](https://img.shields.io/badge/version-1.0.0-0A7EA4)](./plugin.json)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

Antigravity 实时驾驶舱与子代理编排中心插件：在侧边栏实时监控上下文窗口占用、缓存命中率、TPS/TTFT、子代理消耗与模型配额，并统一接管多引擎子代理路由、常驻执行与安全守卫。

## 核心能力

- **实时指标驾驶舱**：侧边栏可视化展示上下文窗口、缓存命中、TPS/TTFT、Token 消耗与模型配额。
- **5 角色子代理编排**：内置 `cockpit-dispatch` Skill 与零端口本地路由解析器，按角色（`scout` / `standard` / `deep` / `expert` / `panel`）自动调度与回退。
- **自包含常驻运行时**：内置 `sa-subagentd` 守护进程与多引擎包装器（`sa-agy` / `sa-codex` / `sa-droid` / `sa-grok` / `sa-pi`），支持会话复用与续接。
- **原生安全守卫钩子**：通过 `hooks.json` 自动挂载 `PreToolUse` 守卫（`sa-guard`），防止子代理套娃派生与高危命令误执行。

## 快速开始

```bash
# 安装 / 同步到本机 Antigravity 插件目录
./install.sh

# 运行 Sidecar 与守卫单测
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s sidecars/cockpit -p "test_*.py"
python3 runtime/tests/test-sa-guard.py
```
