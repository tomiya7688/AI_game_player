@echo off
setlocal
cd /d "%~dp0"

where gh >nul 2>&1 || (
  echo [ERROR] GitHub CLI ^(gh^) was not found in PATH.
  exit /b 1
)
gh auth status >nul 2>&1 || (
  echo [ERROR] GitHub CLI is not authenticated. Run: gh auth login
  exit /b 1
)
where python >nul 2>&1 || (
  echo [ERROR] python was not found in PATH.
  exit /b 1
)

python tools\issue_context.py
if errorlevel 1 exit /b 1

echo.
echo Read AGENTS.md, then .codex\next_issue.md. Do not scan unrelated Issues or docs.
endlocal
