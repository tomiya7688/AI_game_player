@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=src;."

python tools\generate_docs.py || exit /b 1
python -m unittest discover -s tests -v || exit /b 1
python -m compileall -q src || exit /b 1
git diff --check || exit /b 1
git status --short

endlocal