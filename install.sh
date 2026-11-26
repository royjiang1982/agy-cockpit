#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-${HOME}/.gemini/config/plugins/agy-cockpit}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未检测到 python3" >&2
  exit 1
fi

echo "==> [1/3] 预检 Python 语法..."
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

echo "==> [2/3] 分阶段同步至 ${TARGET_DIR}（绝不触碰 sidecar_data）..."
mkdir -p "${TARGET_DIR}"

stage_dir="$(mktemp -d "${TARGET_DIR}/.stage.XXXXXX")"
trap 'rm -rf "${stage_dir}"' EXIT

copy_tree() {
  local rel="$1"
  local src="${SCRIPT_DIR}/${rel}"
  if [ -e "${src}" ]; then
    mkdir -p "${stage_dir}/$(dirname "${rel}")"
    if [ -d "${src}" ]; then
      rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' "${src}/" "${stage_dir}/${rel}/"
    else
      cp -f "${src}" "${stage_dir}/${rel}"
    fi
  fi
}

copy_tree "plugin.json"
copy_tree "README.md"
copy_tree "assets"
copy_tree "rules"
copy_tree "skills"
copy_tree "sidecars/cockpit"

# Synchronize staged artifacts into live plugin dir; rsync --delay-updates minimizes partial-read windows for hot reload.
rsync -a --delay-updates --delete \
  --exclude='.stage.*' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "${stage_dir}/" "${TARGET_DIR}/"

chmod +x "${TARGET_DIR}/sidecars/cockpit/server.py"
if [ -f "${TARGET_DIR}/skills/cockpit-dispatch/scripts/resolve.py" ]; then
  chmod +x "${TARGET_DIR}/skills/cockpit-dispatch/scripts/resolve.py"
fi

echo "==> [3/3] 核对已安装清单..."
python3 - "${SCRIPT_DIR}" "${TARGET_DIR}" <<'PY'
import hashlib, sys
from pathlib import Path

src_root, dst_root = Path(sys.argv[1]), Path(sys.argv[2])
checked = 0
for rel in ("plugin.json", "README.md", "assets", "rules", "skills", "sidecars/cockpit"):
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

echo "✅ 安装完成：${TARGET_DIR}"
