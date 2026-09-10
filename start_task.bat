@echo off
setlocal EnableExtensions EnableDelayedExpansion

where gh >nul 2>&1
if errorlevel 1 (
  echo [ERROR] GitHub CLI ^(gh^) was not found in PATH.
  exit /b 1
)

gh auth status >nul 2>&1
if errorlevel 1 (
  echo [ERROR] GitHub CLI is not authenticated. Run: gh auth login
  exit /b 1
)

set "ISSUE_NUMBER="
set "ISSUE_PRIORITY="

for %%P in (P0 P1 P2 P3) do (
  if not defined ISSUE_NUMBER (
    for /f "usebackq delims=" %%I in (`gh issue list --state open --label %%P --limit 20 --search "sort:created-asc" --json number --jq ".[].number" 2^>nul`) do (
      if not "%%I"=="19" if not defined ISSUE_NUMBER (
        set "ISSUE_NUMBER=%%I"
        set "ISSUE_PRIORITY=%%P"
      )
    )
  )
)

if not defined ISSUE_NUMBER (
  echo [INFO] No open P0-P3 work Issue was found.
  exit /b 2
)

echo Priority: !ISSUE_PRIORITY!
echo.
gh issue view !ISSUE_NUMBER!
exit /b %ERRORLEVEL%
