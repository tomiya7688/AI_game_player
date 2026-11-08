#!/usr/bin/env bash
# start_task.bat の Linux/クラウド環境向け同等スクリプト。
set -euo pipefail
cd "$(dirname "$0")"

command -v gh >/dev/null || { echo "[ERROR] gh was not found in PATH." >&2; exit 1; }
command -v python3 >/dev/null || { echo "[ERROR] python3 was not found in PATH." >&2; exit 1; }
# gh auth status はGH_TOKEN環境で誤判定しうるため、実APIで疎通確認する。
gh api repos/tomiya7688/AI_game_player --jq .name >/dev/null 2>&1 || {
  echo "[ERROR] GitHub API is not reachable or not authenticated." >&2; exit 1; }

git fetch origin main || {
  echo "[ERROR] git fetch origin main failed. Do not start work on a stale base; report this and retry." >&2
  exit 1; }

python3 tools/issue_context.py "$@"

echo
echo "Read AI_CONTEXT.md first, then AGENTS.md and .codex/next_issue.md."
echo "Stop broad exploration once Goal / Required / Acceptance are sufficient."
