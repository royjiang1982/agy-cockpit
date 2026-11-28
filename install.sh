#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_TARGET="${HOME}/.gemini/config/plugins/agy-cockpit"
ALLOW_CUSTOM=0
TARGET_DIR="${DEFAULT_TARGET}"
EXCLUDED_NAMES='__pycache__
*.pyc
.DS_Store
tests
test_*.py
*_test_fixture.py'

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
TARGET_REAL="$(python3 - "${TARGET_DIR}" "${DEFAULT_TARGET}" "${ALLOW_CUSTOM}" "${SCRIPT_DIR}" "${EXCLUDED_NAMES}" <<'PY'
import json, sys
from fnmatch import fnmatchcase
from pathlib import Path

raw_target, raw_default, allow_custom = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
src_real = Path(sys.argv[4]).resolve()
excluded = sys.argv[5].splitlines()
target = Path(raw_target).expanduser()
default = Path(raw_default).expanduser()
home = Path.home().resolve()
plugins_root = (home / ".gemini" / "config" / "plugins").resolve()
sidecar_data = (home / ".gemini" / "antigravity" / "sidecar_data").resolve()

if target.is_symlink():
    raise SystemExit(f"拒绝安装：目标路径不能是符号链接 ({target})")
resolved = target.resolve(strict=False)

def _same_path(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return a.exists() and b.exists() and a.samefile(b)
    except OSError:
        return False

def _nearest_existing(p: Path):
    ancestor = p
    while not ancestor.exists() and ancestor.parent != ancestor:
        ancestor = ancestor.parent
    return ancestor, p.relative_to(ancestor).parts

def _may_ignore_case(directory: Path) -> bool:
    # Probe lookup without writing to the target or the protected tree.
    try:
        for entry in directory.iterdir():
            swapped = entry.name.swapcase()
            if entry.name.isascii() and swapped != entry.name and not entry.is_symlink():
                return entry.samefile(directory / swapped)
    except FileNotFoundError:
        return False
    except OSError:
        pass
    # With no usable entry (e.g. first install into an empty directory), fail closed.
    return True

def _overlaps(a: Path, b: Path) -> bool:
    if a == b or a in b.parents or b in a.parents:
        return True
    a_root, a_tail = _nearest_existing(a)
    b_root, b_tail = _nearest_existing(b)
    for base, tail, other, other_tail in ((a_root, a_tail, b_root, b_tail), (b_root, b_tail, a_root, a_tail)):
        for ancestor in (other, *other.parents):
            if not _same_path(base, ancestor):
                continue
            rest = other.relative_to(ancestor).parts + other_tail
            n = min(len(tail), len(rest))
            left, right = tail[:n], rest[:n]
            return left == right or (
                tuple(s.casefold() for s in left) == tuple(s.casefold() for s in right)
                and _may_ignore_case(base)
            )
    return False

if _overlaps(resolved, src_real):
    raise SystemExit(f"拒绝安装：目标路径与源码仓重叠 ({resolved})")
if any(_same_path(resolved, p) for p in (Path("/"), home, plugins_root)):
    raise SystemExit(f"拒绝安装：目标路径命中受保护目录 ({resolved})")
if _overlaps(resolved, sidecar_data):
    raise SystemExit(f"拒绝安装：目标路径命中 sidecar_data ({resolved})")
if not allow_custom and not _same_path(resolved, default.resolve(strict=False)):
    if not _same_path(resolved.parent, plugins_root) or resolved.name != "agy-cockpit":
        raise SystemExit(f"拒绝安装：默认仅允许安装到 {default}（测试自定义目录请显式加 --allow-custom-target）")

if resolved.exists() and not resolved.is_dir():
    raise SystemExit(f"拒绝安装：目标不是目录 ({resolved})")
for parent_rel in ("skills", "sidecars"):
    parent = resolved / parent_rel
    if parent.is_symlink():
        raise SystemExit(f"拒绝安装：受管父目录不能是符号链接 ({parent})")
    if parent.exists():
        real_parent = parent.resolve(strict=False)
        if not _overlaps(resolved, real_parent) or _overlaps(real_parent, sidecar_data):
            raise SystemExit(f"拒绝安装：受管父目录越界或命中 sidecar_data ({parent})")
    source_parent = src_real / parent_rel
    if source_parent.is_symlink():
        raise SystemExit(f"拒绝安装：源树受管父目录不能是符号链接 ({source_parent})")
managed = ("plugin.json", "README.md", "hooks.json", "assets", "rules", "skills/cockpit-dispatch", "sidecars/cockpit", "runtime")
paths = [resolved / rel for rel in managed]
while paths:
    path = paths.pop()
    if path.is_symlink():
        raise SystemExit(f"拒绝安装：受管路径不能是符号链接 ({path})")
    real = path.resolve(strict=False)
    if resolved not in real.parents or _overlaps(real, sidecar_data):
        raise SystemExit(f"拒绝安装：受管路径越界或命中 sidecar_data ({path})")
    if path.is_dir():
        paths.extend(path.iterdir())
src_paths = [src_real / rel for rel in managed]
while src_paths:
    sp = src_paths.pop()
    if any(fnmatchcase(sp.name, pattern) for pattern in excluded):
        continue
    if sp.is_symlink():
        raise SystemExit(f"拒绝安装：源树受管路径包含符号链接 ({sp})")
    if src_real not in sp.resolve(strict=False).parents:
        raise SystemExit(f"拒绝安装：源树受管路径越出源码仓 ({sp})")
    if sp.is_dir():
        src_paths.extend(sp.iterdir())
if resolved.is_dir() and any(resolved.iterdir()):
    manifest = resolved / "plugin.json"
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

echo "==> [2/4] 预检 Python、JSON 和 Shell 语法..."
python3 - "${SCRIPT_DIR}" "${EXCLUDED_NAMES}" <<'PY'
import json, sys
from fnmatch import fnmatchcase
from pathlib import Path

root = Path(sys.argv[1])
excluded = sys.argv[2].splitlines()
targets = []
for sub in ("assets", "rules", "skills/cockpit-dispatch", "sidecars/cockpit", "runtime"):
    base = root / sub
    targets.extend(
        p for p in sorted(base.rglob("*.py"))
        if not any(fnmatchcase(part, pattern) for part in p.relative_to(base).parts for pattern in excluded)
    )
for path in sorted((root / "runtime/bin").glob("sa-*")):
    if not path.suffix and path.is_file():
        with path.open("rb") as script:
            shebang = script.readline()
        if shebang.startswith(b"#!") and b"python" in shebang:
            targets.append(path)
for path in targets:
    compile(path.read_bytes(), str(path), "exec")
print(f"    py_compile OK ({len(targets)} files)")
for name in ("plugin.json", "hooks.json"):
    try:
        json.loads((root / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"JSON 预检失败：{name} ({exc})")
PY
sh -n "${SCRIPT_DIR}/runtime/guard/sa-guard.sh"

echo "==> [3/4] 仅同步受管插件清单至 ${TARGET_REAL}（不删除外部文件，不触碰 sidecar_data）..."
mkdir -p "${TARGET_REAL}"

stage_dir="$(mktemp -d "${TARGET_REAL}/.stage.XXXXXX")"
trap 'rm -rf "${stage_dir}"' EXIT

for file_item in "plugin.json" "README.md" "hooks.json"; do
  if [ -f "${SCRIPT_DIR}/${file_item}" ]; then
    cp -f "${SCRIPT_DIR}/${file_item}" "${stage_dir}/${file_item}"
    mv -f "${stage_dir}/${file_item}" "${TARGET_REAL}/${file_item}"
  fi
done

for dir_item in "assets" "rules" "skills/cockpit-dispatch" "sidecars/cockpit" "runtime"; do
  src="${SCRIPT_DIR}/${dir_item}"
  dst="${TARGET_REAL}/${dir_item}"
  if [ -d "${src}" ]; then
    mkdir -p "$(dirname "${dst}")" "${stage_dir}/${dir_item}"
    rsync -a --exclude-from=- "${src}/" "${stage_dir}/${dir_item}/" <<EOF
${EXCLUDED_NAMES}
EOF
    mkdir -p "${dst}"
    # Delete stale test files from earlier installs; keep the destination's Python cache.
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
for exe in "${TARGET_REAL}/runtime/bin/"sa-* "${TARGET_REAL}/runtime/guard/sa-guard.sh" "${TARGET_REAL}/runtime/guard/sa-guard-hook.py"; do
  [ -f "${exe}" ] && chmod +x "${exe}"
done

echo "==> [4/4] 核对已安装清单..."
python3 - "${SCRIPT_DIR}" "${TARGET_REAL}" "${EXCLUDED_NAMES}" <<'PY'
import hashlib, sys
from fnmatch import fnmatchcase
from pathlib import Path

src_root, dst_root = Path(sys.argv[1]), Path(sys.argv[2])
excluded = sys.argv[3].splitlines()
checked = 0
for rel in ("plugin.json", "README.md", "hooks.json", "assets", "rules", "skills/cockpit-dispatch", "sidecars/cockpit", "runtime"):
    src = src_root / rel
    if not src.exists():
        continue
    files = [src] if src.is_file() else [
        p for p in src.rglob("*")
        if not any(
            fnmatchcase(part, pattern) for part in p.relative_to(src).parts for pattern in excluded
        ) and p.is_file()
    ]
    for sp in files:
        dp = dst_root / sp.relative_to(src_root)
        if not dp.exists() or hashlib.sha256(sp.read_bytes()).digest() != hashlib.sha256(dp.read_bytes()).digest():
            raise SystemExit(f"校验失败: {dp}")
        checked += 1
print(f"    manifest verified ({checked} files match)")
PY

echo "✅ 安装完成：${TARGET_REAL}"
