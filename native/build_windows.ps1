# Builds chatbridge.dll (llama.cpp + chat_bridge.cpp) for 64-bit Windows and copies it to ..\libs\.
#
# Needs: Visual Studio 2022 or "Build Tools for Visual Studio 2022" with the "Desktop development
# with C++" workload (it includes CMake), and Git. Run from any PowerShell window:
#
#   powershell -ExecutionPolicy Bypass -File native\build_windows.ps1
#
# Options:
#   -LlamaTag b11963     llama.cpp release to build (chat_bridge.cpp is written against it)
#   -Avx2:$false         build for CPUs without AVX2 (Intel before 2013) - slower
param(
    [string]$LlamaTag = "b11963",
    [bool]$Avx2 = $true
)
$ErrorActionPreference = "Stop"

$Here  = $PSScriptRoot
$Out   = Join-Path $Here "..\libs"
$Build = Join-Path $Here "build-windows"

# Find CMake: on PATH, or the copy that ships with Visual Studio
$cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
if (-not $cmake) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        $candidate = Join-Path $vs "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
        if ($vs -and (Test-Path $candidate)) { $cmake = $candidate }
    }
}
if (-not $cmake) {
    throw "CMake not found. Install 'Build Tools for Visual Studio 2022' with 'Desktop development with C++'."
}

if (-not (Test-Path (Join-Path $Here "llama.cpp"))) {
    git clone --depth 1 --branch $LlamaTag https://github.com/ggml-org/llama.cpp (Join-Path $Here "llama.cpp")
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
}

$avx2Flag = if ($Avx2) { "ON" } else { "OFF" }
& $cmake -S $Here -B $Build -G "Visual Studio 17 2022" -A x64 "-DCHATBRIDGE_AVX2=$avx2Flag"
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed" }

& $cmake --build $Build --config Release --target chatbridge --parallel
if ($LASTEXITCODE -ne 0) { throw "Build failed" }

New-Item -ItemType Directory -Force -Path $Out | Out-Null
Copy-Item (Join-Path $Build "Release\chatbridge.dll") $Out -Force
$dll = Get-Item (Join-Path $Out "chatbridge.dll")
Write-Host ("Built {0} ({1:N1} MB)" -f $dll.FullName, ($dll.Length / 1MB))
