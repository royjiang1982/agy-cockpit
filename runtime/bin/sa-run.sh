#!/bin/zsh
# 命令行子代理包装：原始事件流写入 <raw.jsonl>，卡片显示可读进度，退出码取子代理的。
# 用法：sa-run.sh <tmp/.../raw.jsonl> [--] <agy|codex|grok|pi|droid ...>
set -o pipefail

if (( $# < 2 )); then
  echo "Usage: sa-run.sh <tmp/<task>/<name>.jsonl> [--] <command> [args...]" >&2
  exit 2
fi

raw=$1; shift
if [[ "${1:-}" == "--" ]]; then
  shift
fi

if (( $# < 1 )); then
  echo "sa-run.sh error: missing subagent command after log path '$raw'" >&2
  exit 2
fi

case "$raw" in
  -*|agy|sa-agy|sa-codex|sa-droid|sa-grok|sa-pi|sa-subagentd|zomi-agy-acp|zomi-codex-exec|codex|grok|pi|droid|claude|orca)
    echo "sa-run.sh error: invalid log path '$raw' (did you forget the first <raw.jsonl> argument?)" >&2
    exit 2
    ;;
esac

if [[ "$raw" != *.jsonl ]]; then
  echo "sa-run.sh error: first argument '$raw' must end with .jsonl and reside under a tmp/ directory" >&2
  exit 2
fi
if [[ -L "$raw" ]]; then
  echo "sa-run.sh error: log path '$raw' is a symlink" >&2
  exit 2
fi

raw_abs="${raw:A}"
raw_dir="${raw_abs:h}"
report_abs="${raw_abs%.jsonl}.md"

if [[ "$raw_abs" != /tmp/* && "$raw_abs" != */tmp/* ]]; then
  echo "sa-run.sh error: log path '$raw_abs' must be inside a 'tmp/' directory" >&2
  exit 2
fi

script_dir="${0:A:h}"
if [[ "$1" != */* && -x "$script_dir/$1" ]]; then
  set -- "$script_dir/$1" "${@:2}"
fi

if ! command -v "$1" >/dev/null 2>&1; then
  echo "sa-run.sh error: command not found: '$1'" >&2
  exit 2
fi

unset SA_RAW_LOG
case "${1:t}" in
  sa-agy|sa-codex|sa-droid|sa-grok|sa-pi) ;; # daemon.py 已为子进程注入 SA_SUBAGENT=1
  *) export SA_SUBAGENT=1 ;;
esac
if [[ -L "$report_abs" ]]; then
  echo "sa-run.sh error: report path '$report_abs' is a symlink" >&2
  exit 2
fi
if [[ -f "$report_abs" ]]; then
  rm -f -- "$report_abs" || exit 2
fi
mkdir -p "$raw_dir" || exit 2
[[ ! -L "$raw_abs" ]] && (umask 077 && : > "$raw_abs") && chmod 600 "$raw_abs" || exit 2

# jq -R 会在长行的内部缓冲边界切碎 UTF-8；只给视图分支套 ASCII JSON 字符串。
view_input() {
  python3 -u -c '
import json, sys
for line in sys.stdin.buffer:
    try:
        print(json.dumps(line.removesuffix(b"\n").decode("utf-8", errors="replace")))
    except BrokenPipeError:
        # jq 退出也继续读完，避免 tee 随之收到 SIGPIPE。
        while sys.stdin.buffer.read(65536):
            pass
        break
'
}
"$@" < /dev/null 2>&1 | tee "$raw_abs" | view_input | jq -n -R -r --unbuffered \
  --arg raw "$raw_abs" --argjson escaped true -f "$script_dir/sa-view.jq"
rc=${pipestatus[1]} tee_rc=${pipestatus[2]} view_rc=${pipestatus[4]}
if (( rc == 0 && tee_rc != 0 )); then
  rc=$tee_rc
elif (( rc == 0 && view_rc != 0 )); then
  echo "sa-run.sh error: view failed (exit=$view_rc); raw log: '$raw_abs'" >&2
  rc=$view_rc
fi

publish_report() (
  umask 077
  staged=$(mktemp "$raw_dir/.sa-report.XXXXXX") || return 1
  trap 'rm -f -- "$staged"' EXIT
  printf '%s' "$1" > "$staged" && chmod 600 "$staged" &&
    # 同目录硬链接原子发布；目标是文件、目录或符号链接时均不覆盖。
    python3 -c 'import os, sys; os.link(sys.argv[1], sys.argv[2])' "$staged" "$report_abs" 2>/dev/null
)

# -j 不额外换行；末尾哨兵保留命令替换原本会吃掉的正文尾部换行。
if report_text=$(view_input < "$raw_abs" |
    jq -n -R -j --arg mode report --argjson escaped true -f "$script_dir/sa-view.jq" && printf '.'); then
  if [[ "$report_text" != "." ]]; then
    if publish_report "${report_text%.}"; then
      printf '完整报告：%s\n' "$report_abs"
    else
      echo "sa-run.sh error: could not write report '$report_abs'; raw log: '$raw_abs'" >&2
      (( rc != 0 )) || rc=1
    fi
  fi
else
  echo "sa-run.sh error: could not read final report from '$raw_abs'" >&2
  (( rc != 0 )) || rc=1
fi
exit $rc
