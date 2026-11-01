# SPDX-License-Identifier: GPL-3.0-or-later
param([switch]$Setup, [string]$Game, [int]$Seed = 0)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
Set-Location -LiteralPath $repoRoot
$gamePython = Join-Path $repoRoot 'build/test-games-venv/Scripts/python.exe'
if ($Setup) {
    $taskTemp = Join-Path $repoRoot 'build/test-games-temp'
    New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
    $env:TEMP = $taskTemp
    $env:TMP = $taskTemp
    if (-not (Test-Path -LiteralPath $gamePython)) {
        & python -m venv --without-pip (Join-Path $repoRoot 'build/test-games-venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create game environment. Install Python 3.10 or newer.' }
    }
    & python -m pip --python $gamePython install --no-cache-dir -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install game dependencies. Check network access and Python pip installation.' }
}
if (-not (Test-Path -LiteralPath $gamePython)) { throw 'Run run_test_games.bat -Setup once to install the game dependencies.' }
$gameArgs = @('-m', 'tools.test_games', '--seed', "$Seed")
if ($Game) { $gameArgs += @('--game', $Game) }
& $gamePython @gameArgs
exit $LASTEXITCODE
