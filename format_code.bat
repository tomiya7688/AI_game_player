@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" goto usage
if "%~1"=="--check" goto check
if not "%~2"=="" goto usage
python -m ruff format --force-exclude -- "%~1"
exit /b %errorlevel%

:check
if "%~2"=="" goto usage
if not "%~3"=="" goto usage
python -m ruff format --check --force-exclude -- "%~2"
exit /b %errorlevel%

:usage
echo Usage: format_code.bat [--check] path
exit /b 2
