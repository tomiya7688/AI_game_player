param(
    [string]$Python = "python",
    [string]$BuildRoot = "build/app-bundle"
)

$ErrorActionPreference = "Stop"

$NativeBuild = Join-Path $BuildRoot "native"
$PyInstallerWork = Join-Path $BuildRoot "pyinstaller-work"
$DistRoot = Join-Path $BuildRoot "dist"
$SpecRoot = Join-Path $BuildRoot "spec"

New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

cmake -S native -B $NativeBuild -DCMAKE_BUILD_TYPE=Release
cmake --build $NativeBuild --config Release --parallel

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name Kadoka `
    --paths src `
    --workpath $PyInstallerWork `
    --distpath $DistRoot `
    --specpath $SpecRoot `
    src/ai_game_player/__main__.py

$NativeDll = Get-ChildItem -Path $NativeBuild -Recurse -Filter kadoka_native_runtime.dll | Select-Object -First 1
if (-not $NativeDll) {
    throw "kadoka_native_runtime.dll was not produced by the native build"
}

$AppDir = Join-Path $DistRoot "Kadoka"
Copy-Item $NativeDll.FullName (Join-Path $AppDir "kadoka_native_runtime.dll") -Force

$Exe = Join-Path $AppDir "Kadoka.exe"
if (-not (Test-Path $Exe)) {
    throw "Kadoka.exe was not produced"
}

Write-Host "Built application bundle: $AppDir"
Write-Host "Executable: $Exe"
