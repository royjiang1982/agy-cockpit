#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_TARGET="${HOME}/.gemini/config/plugins/agy-cockpit"
ALLOW_CUSTOM=0
TARGET_DIR="${DEFAULT_TARGET}"

while [ $# -gt 0 ]; do
  case "$1" in
    --allow-custom-target)
      ALLOW_CUSTOM=1
      shift
      ;;
    -*)
      echo "错误：未知参数 $1" >&2
      exit 2
      ;;
    *)
      TARGET_DIR="$1"
      shift
      ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未检测到 python3" >&2
  exit 1
fi

echo "==> [1/4] 校验目标路径边界..."
TARGET_REAL="$(python3 - "${TARGET_DIR}" "${DEFAULT_TARGET}" "${ALLOW_CUSTOM}" <<'PY'
import json, os, sys
from pathlib import Path

raw_target, raw_default, allow_custom = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
target = Path(raw_target).expanduser()
default = Path(raw_default).expanduser()
home = Path.home().resolve()
plugins_root = (home / ".gemini" / "config" / "plugins").resolve()
sidecar_data = (home / ".gemini" / "antigravity" / "sidecar_data").resolve()

if target.is_symlink():
    raise SystemExit(f"拒绝安装：目标路径不能是符号链接 ({target})")
resolved = target.resolve(strict=False)
if resolved in (Path("/"), home, plugins_root, sidecar_data) or sidecar_data in resolved.parents:
    raise SystemExit(f"拒绝安装：目标路径命中受保护目录 ({resolved})")
if not allow_custom and resolved != default.resolve(strict=False):
    if resolved.parent != plugins_root or resolved.name != "agy-cockpit":
        raise SystemExit(f"拒绝安装：默认仅允许安装到 {default}（测试自定义目录请显式加 --allow-custom-target）")

if resolved.exists():
    if not resolved.is_dir():
        raise SystemExit(f"拒绝安装：目标不是目录 ({resolved})")
    manifest = resolved / "plugin.json"
    if any(resolved.iterdir()):
        if not manifest.is_file():
            raise SystemExit(f"拒绝安装：非空目标目录缺少 plugin.json ({resolved})")
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"拒绝安装：目标 plugin.json 无法解析 ({exc})")
        if not isinstance(meta, dict) or meta.get("name") != "agy-cockpit":
            raise SystemExit(f"拒绝安装：目标不属于 agy-cockpit 插件 (name={meta.get('name')!r})")

print(resolved)
PY
)"

echo "==> [2/4] 预检 Python 语法..."
python3 - "${SCRIPT_DIR}" <<'PY'
import py_compile, sys
from pathlib import Path

root = Path(sys.argv[1])
targets = []
for sub in ("sidecars/cockpit", "skills"):
    base = root / sub
    if base.exists():
        targets.extend(sorted(base.rglob("*.py")))
for path in targets:
    if "__pycache__" in path.parts:
        continue
    py_compile.compile(str(path), doraise=True)
print(f"    py_compile OK ({len(targets)} files)")
PY

echo "==> [3/4] 仅同步受管插件清单至 ${TARGET_REAL}（不删除外部文件，不触碰 sidecar_data）..."
mkdir -p "${TARGET_REAL}"

stage_dir="$(mktemp -d "${TARGET_REAL}/.stage.XXXXXX")"
trap 'rm -rf "${stage_dir}"' EXIT

for file_item in "plugin.json" "README.md"; do
  if [ -f "${SCRIPT_DIR}/${file_item}" ]; then
    cp -f "${SCRIPT_DIR}/${file_item}" "${stage_dir}/${file_item}"
    mv -f "${stage_dir}/${file_item}" "${TARGET_REAL}/${file_item}"
  fi
done

for dir_item in "assets" "rules" "skills/cockpit-dispatch" "sidecars/cockpit"; do
  src="${SCRIPT_DIR}/${dir_item}"
  dst="${TARGET_REAL}/${dir_item}"
  if [ -d "${src}" ]; then
    mkdir -p "$(dirname "${dst}")" "${stage_dir}/${dir_item}"
    rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' "${src}/" "${stage_dir}/${dir_item}/"
    mkdir -p "${dst}"
    rsync -a --delay-updates --delete \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      "${stage_dir}/${dir_item}/" "${dst}/"
  fi
done

chmod +x "${TARGET_REAL}/sidecars/cockpit/server.py"
if [ -f "${TARGET_REAL}/skills/cockpit-dispatch/scripts/resolve.py" ]; then
  chmod +x "${TARGET_REAL}/skills/cockpit-dispatch/scripts/resolve.py"
fi

echo "==> [4/4] 核对已安装清单..."
python3 - "${SCRIPT_DIR}" "${TARGET_REAL}" <<'PY'
import hashlib, sys
from pathlib import Path

src_root, dst_root = Path(sys.argv[1]), Path(sys.argv[2])
checked = 0
for rel in ("plugin.json", "README.md", "assets", "rules", "skills/cockpit-dispatch", "sidecars/cockpit"):
    src = src_root / rel
    if not src.exists():
        continue
    files = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != ".DS_Store"]
    for sp in files:
        dp = dst_root / sp.relative_to(src_root)
        if not dp.exists() or hashlib.sha256(sp.read_bytes()).digest() != hashlib.sha256(dp.read_bytes()).digest():
            raise SystemExit(f"校验失败: {dp}")
        checked += 1
print(f"    manifest verified ({checked} files match)")
PY

echo "✅ 安装完成：${TARGET_REAL}"
