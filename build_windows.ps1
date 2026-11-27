param(
    [switch]$SkipSigning,
    [switch]$TrustSelfSignedForCurrentUser,
    [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildRoot = Join-Path $Root ".build\windows"
$Venv = Join-Path $BuildRoot "venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Dist = Join-Path $Root "dist\windows"
$DllDist = Join-Path $Dist "dll"
$ExeDist = Join-Path $Dist "exe"

if (-not (Test-Path $Venv)) {
    New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
    & $PythonLauncher -3 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create build virtual environment. Install Python 3 and the Python Launcher." }
}

New-Item -ItemType Directory -Force -Path $Dist, $DllDist, $ExeDist | Out-Null
$PythonBits = & $Python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0 -or $PythonBits.Trim() -ne "64") {
    throw "This build script targets Windows x64. Install 64-bit Python 3 and rerun."
}
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $Python -m pip install --upgrade pyinstaller cffi certifi psutil dnspython plyer colorama
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed" }

$Work = Join-Path $BuildRoot "pyinstaller-work"
$Spec = Join-Path $BuildRoot "spec"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
if (Test-Path $Spec) { Remove-Item -Recurse -Force $Spec }

& $Python -m PyInstaller `
    --clean --noconfirm --onefile --console `
    --name DiscordGlobalStatusMonitor `
    --distpath $ExeDist `
    --workpath $Work `
    --specpath $Spec `
    --collect-all certifi `
    --collect-all psutil `
    --collect-all dns `
    --collect-all plyer `
    --hidden-import colorama `
    (Join-Path $Root "discord_server_global_status.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed to build the Windows executable" }

# CFFI produces a real native DLL with C-exported entry points. It requires
# Visual Studio Build Tools (MSVC) on Windows; it is not a .pyd rename.
& $Python (Join-Path $Root "discord_monitor_dll_build.py") --output-dir $DllDist
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Native DLL compilation failed. Install Visual Studio Build Tools with Desktop development with C++ and rerun."
}
Copy-Item (Join-Path $Root "discord_server_global_status.py") $DllDist -Force
Copy-Item (Join-Path $Root "discord_global_status.h") $DllDist -Force

if (-not $SkipSigning) {
    $Paths = @()
    $ExePath = Join-Path $ExeDist "DiscordGlobalStatusMonitor.exe"
    $DllPath = Join-Path $DllDist "discord_global_status.dll"
    if (Test-Path $ExePath) { $Paths += $ExePath }
    if (Test-Path $DllPath) { $Paths += $DllPath }
    if ($Paths.Count -gt 0) {
        $Signer = Join-Path $Root "sign_windows.ps1"
        if ($TrustSelfSignedForCurrentUser) {
            & $Signer -Paths $Paths -TrustSelfSignedForCurrentUser
        } else {
            & $Signer -Paths $Paths
        }
        if ($LASTEXITCODE -ne 0) { throw "Windows signing failed" }
    }
}

# Assemble a convenient release bundle containing the standalone EXE and the
# optional C ABI DLL + its source/header contract.
$PackageItems = @(
    (Join-Path $ExeDist "DiscordGlobalStatusMonitor.exe"),
    (Join-Path $DllDist "discord_global_status.dll"),
    (Join-Path $DllDist "discord_server_global_status.py"),
    (Join-Path $DllDist "discord_global_status.h"),
    (Join-Path $Dist "signing\DGSM-Development-CodeSigning.cer")
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
$PackagePath = Join-Path $Dist "DiscordGlobalStatusMonitor-Windows-x64.zip"
if (Test-Path $PackagePath) { Remove-Item -Force $PackagePath }
if ($PackageItems.Count -gt 0) {
    Compress-Archive -LiteralPath $PackageItems -DestinationPath $PackagePath -CompressionLevel Optimal
}

Write-Host "`nWindows build complete:" -ForegroundColor Green
Get-ChildItem -Recurse $Dist | Where-Object { -not $_.PSIsContainer } | Select-Object FullName, Length
Write-Host "The standalone EXE does not need Python installed. The CFFI DLL needs a matching CPython runtime and the Python source beside it (or DGSM_HOME)."
