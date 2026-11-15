# Builds QwenChat.exe on your own PC (instead of GitHub Actions). Result: dist\QwenChat\QwenChat.exe
#
#   cd D:\PythonProject\QwenChat-Windows
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# Uses its own clean virtual environment (.venv-build), so packages from other projects don't end
# up inside the .exe. chatbridge.dll must exist in libs\ - build it once with
# native\build_windows.ps1 (needs Visual Studio Build Tools), or copy it from a GitHub build.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path "libs\chatbridge.dll")) {
    Write-Host "libs\chatbridge.dll not found - building it (needs Visual Studio Build Tools)..."
    & "$PSScriptRoot\native\build_windows.ps1"
}

if (-not (Test-Path ".venv-build\Scripts\python.exe")) {
    py -3 -m venv .venv-build
    if ($LASTEXITCODE -ne 0) { python -m venv .venv-build }
}
$python = ".venv-build\Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& $python -m PyInstaller --noconfirm qwenchat.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

New-Item -ItemType Directory -Force "dist\QwenChat\models" | Out-Null
Write-Host ""
Write-Host "Done: dist\QwenChat\QwenChat.exe  (copy the whole dist\QwenChat folder to use it elsewhere)"
