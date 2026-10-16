@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem {
rem   責務: [
rem     finish_pr.bat: feature branchの変更を検証・公開し、CIとinline review確認後にPRをmergeする
rem   ],
rem   引数: [
rem     commit message: 新規commitに記録する変更概要,
rem     next branch: merge後に作成する次の作業branch名
rem   ],
rem   処理: [
rem     1: 引数・Git・GitHub CLI・branchを検証する,
rem     2: 完了検査後に変更をcommit・pushしてPRを特定する,
rem     3: merge gate成功後にmainを更新して次branchを作る
rem   ],
rem   停止条件: [
rem     認証・完了検査・PR状態・CI・inline review・merge・main更新のいずれかを確認できない場合
rem   ]
rem }

rem {
rem   処理: [
rem     1: commit messageと次branch名が両方指定されたことを確認する,
rem     2: GitHub操作に必要なgit・gh・pythonとgh認証が利用できることを確認する,
rem     3: main以外の現在branch名を取得する
rem   ],
rem   停止条件: [
rem     引数不足、command未導入、未認証、現在branchがmain、またはbranch名を取得できない場合
rem   ]
rem }
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
where python >nul 2>&1 || (echo [ERROR] Python was not found in PATH. & exit /b 1)
gh auth status >nul 2>&1 || (echo [ERROR] GitHub CLI is not authenticated. Run: gh auth login & exit /b 1)

for /f "delims=" %%B in ('git branch --show-current') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (echo [ERROR] Could not determine current branch. & exit /b 1)
if /i "%CURRENT_BRANCH%"=="main" (echo [ERROR] Refusing to run from main. Work on a feature branch first. & exit /b 1)

echo [1/9] Running completion checks...
call finish_task.bat
if errorlevel 1 (echo [STOP] finish_task.bat failed. Nothing will be committed. & exit /b 1)

rem {
rem   処理: [
rem     1: finish_task.bat成功後に変更をstageする,
rem     2: Codexのローカルcontextファイルをcommit対象から除外する
rem   ],
rem   分岐: [
rem     staged変更があればCOMMIT_CHANGESへ進む,
rem     staged変更がなければ現在branchの既存PRを検索しmerge gateを再開する
rem   ],
rem   停止条件: [
rem     staged状態を調べられない場合、またはstaged変更と再利用できるPRが両方ない場合
rem   ]
rem }
echo [2/9] Staging changes except generated Codex context...
git add -A
if exist .codex git reset -- .codex >nul 2>&1
if exist AGENTS.override.md git reset -- AGENTS.override.md >nul 2>&1
git diff --cached --quiet
if errorlevel 2 (echo [STOP] Could not inspect staged changes. & exit /b 1)
if errorlevel 1 goto COMMIT_CHANGES

echo [INFO] No staged changes; looking for an existing Pull Request to resume...
set "PR_URL="
for /f "delims=" %%U in ('gh pr view --json url --jq ".url" 2^>nul') do set "PR_URL=%%U"
if not defined PR_URL (echo [STOP] No staged changes and no Pull Request exists for the current branch. & exit /b 2)
echo Reusing PR: %PR_URL%
goto VALIDATE_PR

:COMMIT_CHANGES

rem {
rem   処理: [
rem     1: staged差分の空白を検査し、対象ファイルと変更量を表示する,
rem     2: commitとpushを行い、現在branchのPRを作成または取得する
rem   ],
rem   停止条件: [
rem     差分検査、commit、push、PR URL解決のいずれかが失敗した場合
rem   ]
rem }
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

:VALIDATE_PR
rem {
rem   処理: [
rem     1: PRがMERGEABLEであることを確認する,
rem     2: finish_pr_merge.pyへCI完了・全check成功・未解決inline reviewなしの確認とmergeを委ねる,
rem     3: merge gate成功後だけmainをfast-forwardし、未使用の次branchを作成する
rem   ],
rem   停止条件: [
rem     PRがmerge不可、check未成功、review thread未解決、API確認失敗、merge失敗、
rem     mainのfast-forward失敗、または次branchが既に存在する場合
rem   ]
rem }
echo PR: %PR_URL%
set "MERGEABLE="
for /f "delims=" %%M in ('gh pr view "%PR_URL%" --json mergeable --jq ".mergeable"') do set "MERGEABLE=%%M"
if /i "%MERGEABLE%"=="CONFLICTING" (echo [STOP] Pull Request has conflicts. Inspect only the conflicting files/diff. & exit /b 3)
if /i not "%MERGEABLE%"=="MERGEABLE" (echo [STOP] Pull Request mergeability is "%MERGEABLE%". Inspect PR state before merging. & exit /b 3)

echo [8/9] Waiting for successful CI and checking unresolved review comments...
python tools\finish_pr_merge.py "%PR_URL%"
if errorlevel 1 (echo [STOP] CI, review-thread, or merge validation failed. The script will not continue to main. & exit /b 3)

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
