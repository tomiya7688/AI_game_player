@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=src;."

python tools\generate_docs.py || exit /b 1
call format_code.bat --check tests/test_fail_safe_initial_observation_grace.py || exit /b 1
python -m ruff check src tools tests || exit /b 1
python -m mypy || exit /b 1
if not exist build\coverage mkdir build\coverage
python -m coverage run -m unittest discover -s tests -v || exit /b 1
python -m coverage report --show-missing || exit /b 1
python -m coverage xml || exit /b 1
python -m coverage html || exit /b 1
python -m compileall -q src tools tests || exit /b 1
python tools\bug_check.py src tools tests --output build\bug-check\local-report.json || exit /b 1
python tools\performance_check.py --budget-multiplier 1.5 --output build\performance\local-report.json || exit /b 1
python tools\workflow_security_check.py .github\workflows || exit /b 1
python tools\ruleset_check.py || exit /b 1
git diff --check || exit /b 1
git status --short

endlocal
