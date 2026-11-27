#!/bin/sh
# 子代理防派生与高危命令 PreToolUse 钩子。用法：sa-guard.sh <agy|codex|grok>
# 主代理无活跃子代理标记时免启 Python/jq 快速放行；命中子代理或会话标记时单次 Python 完成全部守卫。
engine=$1
case "$engine" in
  agy|codex|grok) ;;
  *) exit 0 ;;
esac

in=$(cat)
if [ -n "${SA_GUARD_LOG:-}" ] && [ "${SA_GUARD_LOG}" != "/dev/null" ]; then
  printf '%s\n' "$in" >> "$SA_GUARD_LOG"
fi

if [ "${SA_SUBAGENT:-}" != "1" ]; then
  sess_dir=${ZOMI_SUBAGENT_SESSIONS_DIR:-/tmp/zomi-subagent-sessions}
  has_marker=0
  if [ -d "$sess_dir" ]; then
    for f in "$sess_dir"/*; do
      [ -e "$f" ] || break
      sid=${f##*/}
      case "$sid" in
        ''|*[!A-Za-z0-9._-]*) continue ;;
        *) has_marker=1; break ;;
      esac
    done
  fi
  if [ "$has_marker" -eq 0 ]; then
    [ "$engine" = "agy" ] && printf '{"decision":"ask"}\n'
    exit 0
  fi
fi

case "$0" in
  */*) dir=${0%/*} ;;
  *) dir=. ;;
esac
rc=0
printf '%s\n' "$in" | python3 "$dir/sa-guard-hook.py" "$engine" || rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ] && [ "${SA_SUBAGENT:-}" = "1" ]; then
  if [ "$engine" = "agy" ]; then
    printf '{"decision":"deny","reason":"sa-guard internal error"}\n'
    exit 0
  fi
  exit 2
fi
exit "$rc"
