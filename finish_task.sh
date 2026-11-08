#!/usr/bin/env bash
# finish_task.bat の Linux/クラウド環境向け同等スクリプト。いずれか失敗で停止する。
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="src:."

# GUI系テストはtkinterを読み込むため、無い環境ではテストが途中で失敗する。先に分かりやすく止める。
python3 -c "import tkinter" 2>/dev/null || {
  echo "[ERROR] python3 に tkinter がありません。GUI系テストを実行できません。" >&2
  echo "        例: apt-get install -y python3-tk （導入できない環境では、その旨をPRにUnverifiedとして記録）" >&2
  exit 1
}

python3 tools/generate_docs.py
./format_code.sh --check tests/test_fail_safe_initial_observation_grace.py
python3 -m ruff check src tools tests
python3 -m mypy
mkdir -p build/coverage
python3 -m coverage run -m unittest discover -s tests -v
python3 -m coverage report --show-missing
python3 -m coverage xml
python3 -m coverage html
python3 -m compileall -q src tools tests
python3 tools/bug_check.py src tools tests --output build/bug-check/local-report.json
python3 tools/runtime_boundary_check.py
python3 tools/performance_check.py --budget-multiplier 1.5 --output build/performance/local-report.json
python3 tools/workflow_security_check.py .github/workflows
python3 tools/ruleset_check.py
git diff --check
git status --short
