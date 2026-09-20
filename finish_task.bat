@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=src;."

python tools\generate_docs.py || exit /b 1
python -m unittest discover -s tests -v || exit /b 1
python -m compileall -q src tools tests || exit /b 1
python tools\bug_check.py src tools tests --output build\bug-check\local-report.json || exit /b 1
python tools\performance_check.py --budget-multiplier 1.5 --output build\performance\local-report.json || exit /b 1
python tools\noisy_provider_ci.py --budget-multiplier 1.5 --output build\performance\noisy-provider-local-report.json || exit /b 1
git diff --check || exit /b 1
git status --short

endlocal