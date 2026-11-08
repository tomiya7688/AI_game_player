#!/usr/bin/env bash
# format_code.bat の Linux/クラウド環境向け同等スクリプト。
set -euo pipefail
cd "$(dirname "$0")"
usage() { echo "Usage: format_code.sh [--check] path" >&2; exit 2; }
case "${1:-}" in
  "") usage ;;
  --check) [ $# -eq 2 ] || usage; exec python3 -m ruff format --check --force-exclude -- "$2" ;;
  *) [ $# -eq 1 ] || usage; exec python3 -m ruff format --force-exclude -- "$1" ;;
esac
