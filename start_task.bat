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

set "TMP_FILE=%TEMP%\kadoka_priority_issue_%RANDOM%_%RANDOM%.txt"

for %%P in (P0 P1 P2 P3) do (
  >"!TMP_FILE!" gh issue list --state open --label %%P --limit 1 --search "sort:created-asc" --template "{{range .}}{{printf \"Priority: %%P\nIssue: #%%v %%s\nURL: %%s\n\n%%s\n\" .number .title .url .body}}{{end}}" 2>nul
  for %%A in ("!TMP_FILE!") do if %%~zA GTR 0 (
    type "!TMP_FILE!"
    del /q "!TMP_FILE!" >nul 2>&1
    exit /b 0
  )
)

del /q "%TMP_FILE%" >nul 2>&1
echo [INFO] No open P0-P3 Issue was found.
exit /b 2
