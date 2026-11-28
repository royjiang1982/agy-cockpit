#!/bin/sh
# 子代理防派生与高危命令 PreToolUse 钩子。用法：sa-guard.sh <agy|codex|grok>
# 既无子代理标记也无 AGY Swarm 名单时免启 Python；其余交由 Python 核实身份。
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
  has_roster=0
  if [ "$has_marker" -eq 0 ] && [ "$engine" = "agy" ]; then
    brain_dir=${ZOMI_SWARM_BRAIN_DIR:-$HOME/.gemini/antigravity/brain}
    for roster in "$brain_dir"/*/.system_generated/swarm/members.json \
                  "$brain_dir"/.[!.]*/.system_generated/swarm/members.json \
                  "$brain_dir"/..?*/.system_generated/swarm/members.json; do
      if [ -f "$roster" ]; then
        has_roster=1
        break
      fi
    done
  fi
  if [ "$has_marker" -eq 0 ] && [ "$has_roster" -eq 0 ]; then
    [ "$engine" = "agy" ] && printf '{"decision":"ask"}\n'
    exit 0
  fi
fi

case "$0" in
  */*) dir=${0%/*} ;;
  *) dir=. ;;
esac
# Only explicit or potential subagents reach Python. The hint is not identity:
# it is used only when a payload can't be decoded to verify the session.
rc=0
out=$(printf '%s\n' "$in" | SA_GUARD_SUBAGENT_HINT=1 python3 "$dir/sa-guard-hook.py" "$engine") || rc=$?
case "$engine:$rc" in
  *:0|codex:2) ;;
  grok:2)
    case "$out" in
      '{"decision":"deny","reason":'*'}') ;;
      *) out='{"decision":"deny","reason":"sa-guard internal error"}' ;;
    esac
    ;;
  agy:*) rc=0; out='{"decision":"deny","reason":"sa-guard internal error"}' ;;
  grok:*) rc=2; out='{"decision":"deny","reason":"sa-guard internal error"}' ;;
  *) rc=2; out= ;;
esac
[ -z "$out" ] || printf '%s\n' "$out"
exit "$rc"
