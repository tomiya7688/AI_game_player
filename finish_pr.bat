@echo off
setlocal EnableExtensions EnableDelayedExpansion

if "%~1"=="" (
  echo Usage: finish_pr.bat "commit message" next-branch
  exit /b 2
)
if "%~2"=="" (
  echo Usage: finish_pr.bat "commit message" next-branch
  exit /b 2
)

set "COMMIT_MSG=%~1"
set "NEXT_BRANCH=%~2"

where git >nul 2>&1 || (echo [ERROR] git was not found in PATH. & exit /b 1)
where gh >nul 2>&1 || (echo [ERROR] GitHub CLI ^(gh^) was not found in PATH. & exit /b 1)
gh auth status >nul 2>&1 || (echo [ERROR] GitHub CLI is not authenticated. Run: gh auth login & exit /b 1)

for /f "delims=" %%B in ('git branch --show-current') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (echo [ERROR] Could not determine current branch. & exit /b 1)
if /i "%CURRENT_BRANCH%"=="main" (echo [ERROR] Refusing to run from main. Work on a feature branch first. & exit /b 1)

echo [1/9] Running completion checks...
call finish_task.bat
if errorlevel 1 (echo [STOP] finish_task.bat failed. Nothing will be committed. & exit /b 1)

echo [2/9] Staging changes except generated Codex context...
git add -A
if exist .codex git reset -- .codex >nul 2>&1
git diff --cached --quiet
if not errorlevel 1 (echo [STOP] No staged changes to commit. & exit /b 2)

echo [3/9] Validating compact diff summary...
git diff --cached --check
if errorlevel 1 (
  echo [STOP] git diff --cached --check found whitespace/errors. Inspect the affected diff manually.
  exit /b 1
)
echo Changed files:
git diff --cached --name-only
echo.
echo Diff stat:
git diff --cached --shortstat

echo [4/9] Committing...
git commit -m "%COMMIT_MSG%"
if errorlevel 1 exit /b 1

echo [5/9] Verifying committed summary...
git show --check --stat --oneline HEAD
if errorlevel 1 (echo [STOP] Commit diff check failed. & exit /b 1)

echo [6/9] Pushing branch...
git push -u origin "%CURRENT_BRANCH%"
if errorlevel 1 exit /b 1

echo [7/9] Creating Pull Request...
set "PR_URL="
for /f "delims=" %%U in ('gh pr create --fill --base main --head "%CURRENT_BRANCH%" 2^>^&1') do (
  echo %%U
  echo %%U | findstr /b /c:"https://github.com/" >nul && set "PR_URL=%%U"
)
if not defined PR_URL (
  for /f "delims=" %%U in ('gh pr view --json url --jq ".url" 2^>nul') do set "PR_URL=%%U"
)
if not defined PR_URL (echo [STOP] Pull Request URL could not be resolved. & exit /b 1)

echo PR: %PR_URL%
set "MERGEABLE="
for /f "delims=" %%M in ('gh pr view "%PR_URL%" --json mergeable --jq ".mergeable"') do set "MERGEABLE=%%M"
if /i "%MERGEABLE%"=="CONFLICTING" (echo [STOP] Pull Request has conflicts. Inspect only the conflicting files/diff. & exit /b 3)
if /i not "%MERGEABLE%"=="MERGEABLE" (echo [STOP] Pull Request mergeability is "%MERGEABLE%". Inspect PR state before merging. & exit /b 3)

echo [8/9] Merging Pull Request...
gh pr merge "%PR_URL%" --merge --delete-branch
if errorlevel 1 (echo [STOP] PR merge failed. Inspect checks/conflicts. & exit /b 3)

echo [9/9] Returning to main and creating next branch...
git switch main
if errorlevel 1 exit /b 1
git pull --ff-only origin main
if errorlevel 1 (echo [STOP] main could not be fast-forwarded. Inspect local/main state. & exit /b 3)

git show-ref --verify --quiet "refs/heads/%NEXT_BRANCH%"
if not errorlevel 1 (echo [STOP] Local branch "%NEXT_BRANCH%" already exists. & exit /b 3)
git switch -c "%NEXT_BRANCH%"
if errorlevel 1 exit /b 1

echo [DONE] PR merged, main updated, and branch "%NEXT_BRANCH%" created.
exit /b 0
