#!/usr/bin/env bash
set -euo pipefail

branch="$(git branch --show-current)"
workflow="Analytics API"
interval=10
timeout=900
background=false

usage() {
  printf 'usage: %s [--branch NAME] [--workflow NAME] [--interval SECONDS] [--timeout SECONDS] [--background]\n' "$0"
}

while (($#)); do
  case "$1" in
    --branch) branch="$2"; shift 2 ;;
    --workflow) workflow="$2"; shift 2 ;;
    --interval) interval="$2"; shift 2 ;;
    --timeout) timeout="$2"; shift 2 ;;
    --background) background=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$interval" =~ ^[1-9][0-9]*$ ]] || { printf 'interval must be a positive integer\n' >&2; exit 2; }
[[ "$timeout" =~ ^[1-9][0-9]*$ ]] || { printf 'timeout must be a positive integer\n' >&2; exit 2; }

state_root="${XDG_STATE_HOME:-$HOME/.local/state}/llm-log/ci"
slug="${branch//\//-}"
state_dir="$state_root/$slug"

if $background; then
  mkdir -p "$state_dir"
  nohup "$0" --branch "$branch" --workflow "$workflow" --interval "$interval" --timeout "$timeout" \
    >"$state_dir/poll.log" 2>&1 </dev/null &
  pid=$!
  printf '%s\n' "$pid" >"$state_dir/pid"
  printf 'CI poller started: pid=%s state=%s\n' "$pid" "$state_dir"
  exit 0
fi

command -v gh >/dev/null || { printf 'gh is required\n' >&2; exit 2; }
mkdir -p "$state_dir"
deadline=$((SECONDS + timeout))

while ((SECONDS < deadline)); do
  row="$(gh run list --branch "$branch" --workflow "$workflow" --limit 1 \
    --json databaseId,status,conclusion,url \
    --jq '.[0] | [.databaseId, .status, (.conclusion // ""), .url] | @tsv')"
  if [[ -z "$row" ]]; then
    printf 'waiting branch=%s workflow=%s\n' "$branch" "$workflow" | tee "$state_dir/status"
    sleep "$interval"
    continue
  fi
  IFS=$'\t' read -r run_id status conclusion url <<<"$row"
  printf 'run=%s status=%s conclusion=%s url=%s\n' "$run_id" "$status" "${conclusion:-pending}" "$url" | tee "$state_dir/status"
  if [[ "$status" == "completed" ]]; then
    [[ "$conclusion" == "success" ]]
    exit
  fi
  sleep "$interval"
done

printf 'timeout branch=%s workflow=%s\n' "$branch" "$workflow" | tee "$state_dir/status" >&2
exit 124
