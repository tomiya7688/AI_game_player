@rem SPDX-License-Identifier: GPL-3.0-or-later
@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\test_games\launch.ps1" %*
if errorlevel 1 pause
