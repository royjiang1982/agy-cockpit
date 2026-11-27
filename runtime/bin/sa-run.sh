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

raw_abs="${raw:A}"
raw_dir="${raw_abs:h}"

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
mkdir -p "$raw_dir" || exit 2
[[ ! -L "$raw_abs" ]] && (umask 077 && : > "$raw_abs") && chmod 600 "$raw_abs" || exit 2
"$@" < /dev/null 2>&1 | tee "$raw_abs" | jq -R -r --unbuffered -f "${0:A:h}/sa-view.jq"
rc=${pipestatus[1]}
tee_rc=${pipestatus[2]}
if (( rc == 0 && tee_rc != 0 )); then
  rc=$tee_rc
fi
exit $rc
