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

# Link the MSVC runtime statically (/MT) so the end-user machine needs no VC++
# Redistributable: PyInstaller does not collect MSVCP140.dll for this DLL, and
# the C ABI returns no CRT-owned memory (Issue #149).
cmake -S native -B $NativeBuild -DCMAKE_BUILD_TYPE=Release -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded
if ($LASTEXITCODE -ne 0) {
    throw "Native configure failed with exit code $LASTEXITCODE"
}
cmake --build $NativeBuild --config Release --parallel
if ($LASTEXITCODE -ne 0) {
    throw "Native build failed with exit code $LASTEXITCODE"
}

# The build interpreter's pip and certifi must meet the trust floor (Issue #514).
& $Python tools/trust_gate.py
if ($LASTEXITCODE -ne 0) {
    throw "Build Python failed the trust gate; see the TRUST GATE lines above. Old pip/certifi: $Python -m pip install --upgrade -c requirements/python-trust-constraints.txt pip. An incident certificate needs the OS or bundle trust store updated."
}

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
    --add-data "$((Resolve-Path src/ai_game_player/assets).Path);ai_game_player/assets" `
    src/ai_game_player/__main__.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

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

# Every imported DLL must be bundled or part of Windows; the build machine's
# System32 (which usually has the VC++ runtime) does not count (Issue #149).
& $Python tools/windows_bundle_check.py layout $AppDir
if ($LASTEXITCODE -ne 0) {
    throw "Bundle layout check failed; see the BUNDLE LAYOUT ERROR lines above."
}

Write-Host "Built application bundle: $AppDir"
Write-Host "Executable: $Exe"
