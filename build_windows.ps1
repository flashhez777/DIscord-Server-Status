[CmdletBinding()]
param(
    [switch]$NoToolchainInstall,
    [switch]$SkipSigning,
    [switch]$ElevatedRetry
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildRoot = Join-Path $Root '.build\windows'
$Venv = Join-Path $BuildRoot 'venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$Dist = Join-Path $Root 'dist\windows'
$ExeDist = Join-Path $Dist 'exe'
$DllDist = Join-Path $Dist 'dll'
$SigningDir = Join-Path $Dist 'signing'
$PackageStage = Join-Path $Dist 'package-stage'
$PackagePath = Join-Path $Dist 'DiscordGlobalStatusMonitor-Windows-x64.zip'

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-WingetPath {
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidate = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

function ConvertTo-WindowsArgument([string]$Value) {
    # Quote an argument using the Windows CRT command-line rules. This keeps
    # spaces, quotes and trailing backslashes safe for ProcessStartInfo.
    if ($null -eq $Value) { $Value = '' }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $slashes++
            continue
        }
        if ($character -eq [char]34) {
            if ($slashes -gt 0) { [void]$builder.Append(('\' * (2 * $slashes + 1))) }
            else { [void]$builder.Append('\') }
            [void]$builder.Append('"')
        } else {
            if ($slashes -gt 0) { [void]$builder.Append(('\' * $slashes)) }
            [void]$builder.Append($character)
        }
        $slashes = 0
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * (2 * $slashes))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-CapturedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$RawArguments = $null,
        [int]$TimeoutSeconds = 180
    )
    # Never invoke a missing Python runtime with PowerShell's call operator:
    # Windows PowerShell 5.1 can promote native stderr into NativeCommandError
    # when ErrorActionPreference=Stop. ProcessStartInfo captures stderr as data.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    if ($PSBoundParameters.ContainsKey('RawArguments')) {
        $psi.Arguments = $RawArguments
    } else {
        $psi.Arguments = (($Arguments | ForEach-Object { ConvertTo-WindowsArgument ([string]$_) }) -join ' ')
    }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    try {
        if (-not $process.Start()) { throw "Could not start process: $FilePath" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $process.Kill() } catch { }
            throw "Process timed out after $TimeoutSeconds seconds: $FilePath"
        }
        $process.WaitForExit()
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            StdOut = $stdoutTask.Result
            StdErr = $stderrTask.Result
        }
    } finally {
        $process.Dispose()
    }
}

function Refresh-ProcessPath {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $windowsApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    # Python install manager places the global aliases (py, python, pip) here
    # by default. Include it even when the installer has not refreshed PATH.
    $pythonManagerBin = Join-Path $env:LOCALAPPDATA 'Python\bin'
    $pathEntries = New-Object 'System.Collections.Generic.List[string]'
    $seenPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($sourcePath in @($userPath, $machinePath, $windowsApps, $pythonManagerBin, $env:PATH)) {
        if ([string]::IsNullOrWhiteSpace($sourcePath)) { continue }
        foreach ($entry in ([string]$sourcePath -split ';')) {
            $normalized = $entry.Trim()
            if ($normalized -and $seenPaths.Add($normalized)) {
                [void]$pathEntries.Add($normalized)
            }
        }
    }
    $env:PATH = ($pathEntries -join ';')
}

function Test-Python312Path([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $null
    }
    try {
        $check = Invoke-CapturedProcess -FilePath $Candidate -Arguments @(
            '-c', 'import sys; print(sys.executable); print("DGSM_PY312=" + str(sys.version_info[:2] == (3, 12)))'
        ) -TimeoutSeconds 20
        if ($check.ExitCode -eq 0 -and $check.StdOut -match 'DGSM_PY312=True') {
            return $Candidate
        }
    } catch {
        Write-Verbose "Python candidate check failed for '$Candidate': $_"
    }
    return $null
}

function Get-Python312 {
    $candidates = New-Object 'System.Collections.Generic.List[string]'
    $addCandidate = {
        param([string]$Candidate)
        if (-not [string]::IsNullOrWhiteSpace($Candidate) -and -not $candidates.Contains($Candidate)) {
            [void]$candidates.Add($Candidate)
        }
    }

    # Traditional CPython installation registry locations (per-user and machine).
    $registryKeys = @(
        'HKCU:\Software\Python\PythonCore\3.12\InstallPath',
        'HKLM:\Software\Python\PythonCore\3.12\InstallPath',
        'HKLM:\Software\WOW6432Node\Python\PythonCore\3.12\InstallPath',
        'HKCU:\Software\Python\PythonCore\3.12-64\InstallPath',
        'HKLM:\Software\Python\PythonCore\3.12-64\InstallPath'
    )
    foreach ($key in $registryKeys) {
        if (Test-Path -LiteralPath $key) {
            try {
                $item = Get-Item -LiteralPath $key
                $installPath = [string]$item.GetValue('')
                $exePath = [string]$item.GetValue('ExecutablePath')
                if ($exePath) { & $addCandidate $exePath }
                if ($installPath) { & $addCandidate (Join-Path $installPath 'python.exe') }
            } catch { }
        }
    }

    # Known layouts for classic CPython and the newer Python install manager.
    $knownPaths = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.12-64\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.12\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\PythonManager\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python 3.12\python.exe'),
        'C:\Python312\python.exe'
    )
    foreach ($path in $knownPaths) { & $addCandidate $path }

    # Search command aliases without invoking them yet; Windows Store aliases
    # are harmless because Test-Python312Path validates the actual interpreter.
    foreach ($name in @('python3.12.exe', 'python.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source) { & $addCandidate $command.Source }
    }

    foreach ($candidate in $candidates) {
        $valid = Test-Python312Path $candidate
        if ($valid) { return $valid }
    }

    # Try both the legacy Python launcher and the current Python install manager.
    # Their failure text is captured; "No suitable Python runtime found" is a
    # discovery result, not a fatal PowerShell error.
    $launchers = @()
    foreach ($name in @('pymanager.exe', 'py.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source) { $launchers += $command.Source }
    }
    $localApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    foreach ($name in @('pymanager.exe', 'py.exe')) {
        $path = Join-Path $localApps $name
        if (Test-Path -LiteralPath $path) { $launchers += $path }
    }
    # Python's MSIX manager may live in a package-specific WindowsApps folder
    # even when the app execution aliases are not yet visible in PATH.
    if (Test-Path -LiteralPath $localApps) {
        $managerPackages = Get-ChildItem -LiteralPath $localApps -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'PythonSoftwareFoundation.PythonManager_*' }
        foreach ($package in $managerPackages) {
            foreach ($name in @('pymanager.exe', 'py.exe')) {
                $path = Join-Path $package.FullName $name
                if (Test-Path -LiteralPath $path -PathType Leaf) { $launchers += $path }
            }
        }
    }
    foreach ($launcher in ($launchers | Select-Object -Unique)) {
        # Prefer the install manager's explicit runtime listing. `py list` can
        # return the exact executable path even when PATH aliases are stale.
        # The documented syntax is: py list -1 --format=exe 3.12.
        try {
            $listResult = Invoke-CapturedProcess -FilePath $launcher -Arguments @(
                'list', '-1', '--format=exe', '3.12'
            ) -TimeoutSeconds 30
            if ($listResult.ExitCode -eq 0) {
                foreach ($line in ($listResult.StdOut -split "`r?`n")) {
                    $listedPath = $line.Trim().Trim([char]34)
                    if ($listedPath -and (Test-Path -LiteralPath $listedPath -PathType Leaf)) {
                        $validListedPath = Test-Python312Path $listedPath
                        if ($validListedPath) { return $validListedPath }
                    }
                }
            }
        } catch {
            Write-Verbose "Runtime list probe failed for '$launcher': $_"
        }

        $probeCode = 'import sys; print(sys.executable); print("DGSM_PY312=" + str(sys.version_info[:2] == (3, 12)))'
        $attempts = @(
            @('-V:3.12', '-c', $probeCode),
            @('-3.12', '-c', $probeCode),
            @('exec', '-V:3.12', '-c', $probeCode)
        )
        foreach ($arguments in $attempts) {
            try {
                $result = Invoke-CapturedProcess -FilePath $launcher -Arguments $arguments -TimeoutSeconds 30
                if ($result.ExitCode -eq 0 -and $result.StdOut -match 'DGSM_PY312=True') {
                    $line = ($result.StdOut -split "`r?`n" | Where-Object { $_ -and (Test-Path -LiteralPath $_.Trim() -PathType Leaf) } | Select-Object -First 1)
                    if ($line) { return $line.Trim() }
                }
            } catch {
                Write-Verbose "Launcher probe failed for '$launcher': $_"
            }
        }
    }
    return $null
}

function Invoke-WingetInstall {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [string]$Override = $null,
        [string]$Source = 'winget',
        [switch]$UserScope
    )
    $winget = Get-WingetPath
    if (-not $winget) {
        throw 'WinGet is unavailable. Install Microsoft App Installer, reopen PowerShell, and run this build again.'
    }
    $wingetArgs = @('install', '--exact', '--id', $Id, '--source', $Source,
        '--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity')
    if ($UserScope) { $wingetArgs += @('--scope', 'user') }
    if ($Override) { $wingetArgs += @('--override', $Override) }
    Write-Host "Installing $Id through WinGet ..." -ForegroundColor Yellow
    $result = Invoke-CapturedProcess -FilePath $winget -Arguments $wingetArgs -TimeoutSeconds 3600
    if ($result.StdOut) { $result.StdOut -split "`r?`n" | ForEach-Object { if ($_){ Write-Host $_ } } }
    if ($result.StdErr) { $result.StdErr -split "`r?`n" | ForEach-Object { if ($_){ Write-Host $_ -ForegroundColor DarkYellow } } }
    if ($result.ExitCode -ne 0) {
        throw "WinGet installation failed for $Id (exit $($result.ExitCode)). Check the output above and rerun."
    }
}

function Ensure-Python312 {
    $python = Get-Python312
    if ($python) { return $python }
    if ($NoToolchainInstall) {
        throw 'Python 3.12 is not installed. Remove -NoToolchainInstall to install it automatically.'
    }

    Write-Step 'No Python 3.12 runtime detected; installing the official Python install manager'
    # Current Python.org guidance uses this Store/WinGet package ID. It installs
    # py/pymanager; the runtime itself is provisioned explicitly with `py install`.
    $managerInstalled = $false
    try {
        Invoke-WingetInstall -Id '9NQ7512CXL7T' -Source 'msstore'
        $managerInstalled = $true
    } catch {
        Write-Warning "Python install manager installation failed: $_"
    }

    # MSIX registration and app-execution aliases can appear a moment after
    # WinGet exits. Refresh and retry discovery before concluding the install
    # manager is missing.
    $python = $null
    for ($attempt = 0; $attempt -lt 6; $attempt++) {
        Refresh-ProcessPath
        $python = Get-Python312
        if ($python) { return $python }
        if ($attempt -lt 5) { Start-Sleep -Seconds 2 }
    }

    $managerCommands = @()
    foreach ($name in @('pymanager.exe', 'py.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source) { $managerCommands += $command.Source }
    }
    $localApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    foreach ($name in @('pymanager.exe', 'py.exe')) {
        $path = Join-Path $localApps $name
        if (Test-Path -LiteralPath $path) { $managerCommands += $path }
    }
    if (Test-Path -LiteralPath $localApps) {
        $managerPackages = Get-ChildItem -LiteralPath $localApps -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'PythonSoftwareFoundation.PythonManager_*' }
        foreach ($package in $managerPackages) {
            foreach ($name in @('pymanager.exe', 'py.exe')) {
                $path = Join-Path $package.FullName $name
                if (Test-Path -LiteralPath $path -PathType Leaf) { $managerCommands += $path }
            }
        }
    }

    foreach ($manager in ($managerCommands | Select-Object -Unique)) {
        $managerArgs = if ([IO.Path]::GetFileName($manager).ToLowerInvariant() -eq 'pymanager.exe') {
            @('install', '3.12')
        } else {
            @('install', '3.12')
        }
        try {
            Write-Step "Provisioning CPython 3.12 through $([IO.Path]::GetFileName($manager))"
            $installResult = Invoke-CapturedProcess -FilePath $manager -Arguments $managerArgs -TimeoutSeconds 900
            if ($installResult.StdOut) { Write-Host $installResult.StdOut }
            if ($installResult.StdErr) { Write-Host $installResult.StdErr -ForegroundColor DarkYellow }
            if ($installResult.ExitCode -eq 0) { break }
        } catch {
            Write-Warning "Runtime provisioning via '$manager' failed: $_"
        }
    }

    Refresh-ProcessPath
    $python = Get-Python312
    if ($python) { return $python }

    # Compatibility fallback for systems still using the traditional 3.12 installer.
    Write-Step 'Falling back to the standalone CPython 3.12 package'
    try {
        Invoke-WingetInstall -Id 'Python.Python.3.12' -UserScope
    } catch {
        Write-Warning "Standalone Python package installation failed: $_"
    }
    Refresh-ProcessPath
    $python = Get-Python312
    if ($python) { return $python }

    $status = if ($managerInstalled) { 'Python install manager was installed, but no 3.12 runtime was discovered.' } else { 'Python install manager could not be installed.' }
    throw "$status Open a new PowerShell window and run: winget install 9NQ7512CXL7T -e; py install 3.12; py -V:3.12 -c `"import sys; print(sys.executable)`". Then rerun BUILD_AND_SIGN_WINDOWS.bat."
}

function Get-VsWherePath {
    $pf86 = ${env:ProgramFiles(x86)}
    if (-not $pf86) { $pf86 = $env:ProgramFiles }
    $path = Join-Path $pf86 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path -LiteralPath $path) { return $path }
    return $null
}

function Get-VsInstallPath([switch]$RequireCpp) {
    $vswhere = Get-VsWherePath
    if (-not $vswhere) { return $null }
    $vswhereArgs = @('-latest', '-products', '*', '-property', 'installationPath')
    if ($RequireCpp) {
        $vswhereArgs = @('-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath')
    }
    try {
        $result = Invoke-CapturedProcess -FilePath $vswhere -Arguments $vswhereArgs -TimeoutSeconds 30
        if ($result.ExitCode -eq 0) {
            $installPath = ($result.StdOut -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
            if ($installPath -and (Test-Path -LiteralPath $installPath.Trim())) {
                return $installPath.Trim()
            }
        }
        if ($result.StdErr) { Write-Verbose "vswhere: $($result.StdErr.Trim())" }
    } catch {
        Write-Verbose "vswhere discovery failed: $_"
    }
    return $null
}

function Get-SignToolPath {
    $pf86 = ${env:ProgramFiles(x86)}
    if (-not $pf86) { $pf86 = $env:ProgramFiles }
    $roots = @(
        (Join-Path $pf86 'Windows Kits\10\bin'),
        (Join-Path $env:ProgramFiles 'Windows Kits\10\bin')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $foundTools = foreach ($root in $roots) {
        Get-ChildItem -Path $root -Filter signtool.exe -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '[\\/]x64[\\/]signtool\.exe$' }
    }
    return ($foundTools | Sort-Object FullName -Descending | Select-Object -First 1)
}

function Get-ResourceCompilerPath {
    $pf86 = ${env:ProgramFiles(x86)}
    if (-not $pf86) { $pf86 = $env:ProgramFiles }
    $roots = @(
        (Join-Path $pf86 'Windows Kits\10\bin'),
        (Join-Path $env:ProgramFiles 'Windows Kits\10\bin')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $foundTools = foreach ($root in $roots) {
        Get-ChildItem -Path $root -Filter rc.exe -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName.Replace([char]92, [char]47).ToLowerInvariant().EndsWith('/x64/rc.exe') }
    }
    return ($foundTools | Sort-Object FullName -Descending | Select-Object -First 1)
}

function Restart-Elevated {
    if ($ElevatedRetry) {
        throw 'The build still lacks elevated Windows toolchain installation permissions.'
    }
    $scriptPath = '"' + $PSCommandPath + '"'
    $argumentLine = '-NoProfile -ExecutionPolicy Bypass -File ' + $scriptPath + ' -ElevatedRetry'
    if ($NoToolchainInstall) { $argumentLine += ' -NoToolchainInstall' }
    if ($SkipSigning) { $argumentLine += ' -SkipSigning' }
    Write-Host 'Windows C++/SDK components must be installed. Requesting UAC elevation ...' -ForegroundColor Yellow
    $child = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList $argumentLine
    exit $child.ExitCode
}

function Ensure-WindowsToolchain {
    $vsCpp = Get-VsInstallPath -RequireCpp
    $signtool = Get-SignToolPath
    $resourceCompiler = Get-ResourceCompilerPath
    if ($vsCpp -and $resourceCompiler -and ($SkipSigning -or $signtool)) {
        $readyMessage = if ($SkipSigning) { 'MSVC and Windows SDK resource compiler found.' } else { 'MSVC, Windows SDK resource compiler and SignTool found.' }
        Write-Host "$readyMessage Visual Studio: $vsCpp" -ForegroundColor Green
        return $vsCpp
    }
    if ($NoToolchainInstall) {
        throw 'Required MSVC x64 build tools or Windows SDK resource compiler (rc.exe) are missing. SignTool is additionally required unless -SkipSigning is used. Remove -NoToolchainInstall to install them automatically.'
    }
    if (-not (Test-IsAdministrator)) { Restart-Elevated }

    $winget = Get-WingetPath
    if (-not $winget) {
        throw 'WinGet is required for automatic toolchain installation. Install Microsoft App Installer and rerun.'
    }

    $anyVs = Get-VsInstallPath
    $components = @(
        'Microsoft.VisualStudio.Workload.VCTools',
        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
        'Microsoft.VisualStudio.Component.Windows11SDK.26100'
    )

    if ($anyVs) {
        $pf86 = ${env:ProgramFiles(x86)}
        if (-not $pf86) { $pf86 = $env:ProgramFiles }
        $setup = Join-Path $pf86 'Microsoft Visual Studio\Installer\setup.exe'
        if (-not (Test-Path -LiteralPath $setup)) {
            throw "Visual Studio exists at $anyVs but setup.exe was not found to add required components. Repair it with Visual Studio Installer."
        }
        Write-Step 'Adding MSVC x64 and Windows 11 SDK components to the existing Visual Studio installation'
        $setupArgs = @('modify', '--installPath', $anyVs)
        foreach ($component in $components) { $setupArgs += @('--add', $component) }
        $setupArgs += @('--quiet', '--wait', '--norestart')
        $setupResult = Invoke-CapturedProcess -FilePath $setup -Arguments $setupArgs -TimeoutSeconds 7200
        if ($setupResult.StdOut) { Write-Host $setupResult.StdOut }
        if ($setupResult.StdErr) { Write-Host $setupResult.StdErr -ForegroundColor DarkYellow }
        if ($setupResult.ExitCode -ne 0) { throw "Visual Studio component installation failed (exit $($setupResult.ExitCode))." }
    } else {
        Write-Step 'Installing Visual Studio Build Tools with MSVC x64 and Windows 11 SDK'
        $override = '--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.26100 --includeRecommended'
        Invoke-WingetInstall -Id 'Microsoft.VisualStudio.BuildTools' -Override $override
    }

    $vsCpp = Get-VsInstallPath -RequireCpp
    if (-not $vsCpp) {
        throw 'MSVC x64 component is still not detected after installation. Reboot if Windows Installer requested it, then rerun.'
    }

    if (-not $SkipSigning) {
        $signtool = Get-SignToolPath
        if (-not $signtool) {
            Write-Step 'Installing Windows SDK Signing Tools'
            Invoke-WingetInstall -Id 'Microsoft.WindowsSDK.10.0.22621'
            $signtool = Get-SignToolPath
        }
        if (-not $signtool) {
            throw 'SignTool is still missing after Windows SDK installation. Install the Windows SDK Signing Tools for Desktop Apps.'
        }
    }
    $resourceCompiler = Get-ResourceCompilerPath
    if (-not $resourceCompiler) {
        throw 'Windows SDK rc.exe was not found. Modify the Windows SDK installation and add the MSVC/Windows desktop resource compiler tools.'
    }
    Write-Host "MSVC ready: $vsCpp" -ForegroundColor Green
    Write-Host "Resource compiler ready: $($resourceCompiler.FullName)" -ForegroundColor Green
    if ($SkipSigning) {
        Write-Host 'SignTool check skipped.' -ForegroundColor DarkYellow
    } else {
        Write-Host "SignTool ready: $($signtool.FullName)" -ForegroundColor Green
    }
    return $vsCpp
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This script must run on Windows. Windows PE binaries cannot be built natively by this script on Linux.'
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw 'This release pipeline currently targets Windows x64 only.'
}

Write-Step 'Checking source files and platform prerequisites'
$Source = Join-Path $Root 'discord_server_global_status.py'
$DllBuilder = Join-Path $Root 'discord_monitor_dll_build.py'
$Header = Join-Path $Root 'discord_global_status.h'
$IconPath = Join-Path $Root 'assets\DGSM.ico'
$IconPng = Join-Path $Root 'assets\DGSM-icon.png'
$IconRc = Join-Path $Root 'assets\DGSM.rc'
$BackendsDir = Join-Path $Root 'backends'
foreach ($file in @($Source, $DllBuilder, $Header, $IconPath, $IconPng, $IconRc)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required source file is missing: $file" }
}
if (-not (Test-Path -LiteralPath (Join-Path $BackendsDir '__init__.py') -PathType Leaf)) { throw "Required telemetry backend package is missing: $BackendsDir" }

Write-Step 'Checking Python 3.12 build runtime'
$PythonBase = Ensure-Python312
$versionCheck = Invoke-CapturedProcess -FilePath $PythonBase -Arguments @('-c', 'import sys; print(".".join(map(str, sys.version_info[:3])))') -TimeoutSeconds 20
if ($versionCheck.ExitCode -ne 0) { throw "Python 3.12 failed its startup check: $($versionCheck.StdErr)" }
$pyVersion = ($versionCheck.StdOut -split "`r?`n" | Select-Object -First 1).Trim()
Write-Host "Using Python $pyVersion at $PythonBase" -ForegroundColor Green

$VsPath = Ensure-WindowsToolchain
$VsDevCmd = Join-Path $VsPath 'Common7\Tools\VsDevCmd.bat'
if (-not (Test-Path -LiteralPath $VsDevCmd)) { throw "VsDevCmd.bat was not found under $VsPath" }
$SignTool = Get-SignToolPath
if (-not $SignTool -and -not $SkipSigning) { throw 'SignTool disappeared after prerequisite installation.' }

Write-Step 'Creating isolated build environment and installing all Python build/runtime dependencies'
New-Item -ItemType Directory -Force -Path $BuildRoot, $ExeDist, $DllDist, $SigningDir | Out-Null
$rebuildVenv = -not (Test-Path -LiteralPath $VenvPython)
if (-not $rebuildVenv) {
    $venvCheck = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @('-c', 'import sys; assert sys.version_info[:2] == (3, 12)') -TimeoutSeconds 20
    if ($venvCheck.ExitCode -ne 0) { $rebuildVenv = $true }
}
if ($rebuildVenv) {
    if (Test-Path -LiteralPath $Venv) { Remove-Item -Recurse -Force $Venv }
    $venvCreate = Invoke-CapturedProcess -FilePath $PythonBase -Arguments @('-m', 'venv', $Venv) -TimeoutSeconds 180
    if ($venvCreate.StdOut) { Write-Host $venvCreate.StdOut }
    if ($venvCreate.StdErr) { Write-Host $venvCreate.StdErr -ForegroundColor DarkYellow }
    if ($venvCreate.ExitCode -ne 0) { throw "Could not create the isolated build virtual environment: $($venvCreate.StdErr)" }
}
$pipBase = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel') -TimeoutSeconds 900
if ($pipBase.StdOut) { Write-Host $pipBase.StdOut }
if ($pipBase.StdErr) { Write-Host $pipBase.StdErr -ForegroundColor DarkYellow }
if ($pipBase.ExitCode -ne 0) { throw 'pip/setuptools/wheel installation failed.' }
$pipDeps = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pyinstaller', 'cffi', 'certifi', 'psutil', 'dnspython', 'plyer', 'colorama') -TimeoutSeconds 1800
if ($pipDeps.StdOut) { Write-Host $pipDeps.StdOut }
if ($pipDeps.StdErr) { Write-Host $pipDeps.StdErr -ForegroundColor DarkYellow }
if ($pipDeps.ExitCode -ne 0) { throw 'One or more build/runtime Python dependencies could not be installed.' }
$dependencyCheck = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @('-c', 'import PyInstaller, cffi, certifi, psutil, dns, plyer, colorama; print("All Python dependencies imported successfully.")') -TimeoutSeconds 60
if ($dependencyCheck.ExitCode -ne 0) { throw 'Dependency import validation failed.' }
Write-Host $dependencyCheck.StdOut -ForegroundColor Green

Write-Step 'Building standalone Windows x64 executable'
$ExePath = Join-Path $ExeDist 'DiscordGlobalStatusMonitor.exe'
if (Test-Path -LiteralPath $ExePath) { Remove-Item -Force $ExePath }
$Work = Join-Path $BuildRoot 'pyinstaller-work'
$Spec = Join-Path $BuildRoot 'spec'
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
if (Test-Path $Spec) { Remove-Item -Recurse -Force $Spec }
$pyInstallerArgs = @(
    '-m', 'PyInstaller', '--clean', '--noconfirm', '--onefile', '--console',
    '--name', 'DiscordGlobalStatusMonitor', '--icon', $IconPath,
    '--distpath', $ExeDist, '--workpath', $Work, '--specpath', $Spec,
    '--collect-all', 'certifi', '--collect-all', 'psutil', '--collect-all', 'dns',
    '--collect-all', 'plyer', '--collect-submodules', 'backends', '--hidden-import', 'colorama', $Source
)
$pyInstallerRun = Invoke-CapturedProcess -FilePath $VenvPython -Arguments $pyInstallerArgs -TimeoutSeconds 1800
if ($pyInstallerRun.StdOut) { Write-Host $pyInstallerRun.StdOut }
if ($pyInstallerRun.StdErr) { Write-Host $pyInstallerRun.StdErr -ForegroundColor DarkYellow }
if ($pyInstallerRun.ExitCode -ne 0) { throw 'PyInstaller failed to build the Windows executable.' }
if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) { throw 'PyInstaller returned success but the EXE output is missing.' }
$exeSmoke = Invoke-CapturedProcess -FilePath $ExePath -Arguments @('--help') -TimeoutSeconds 60
if ($exeSmoke.ExitCode -ne 0) { throw "The generated EXE failed its --help smoke test: $($exeSmoke.StdErr)" }

Write-Step 'Embedding the product icon into the native DLL resource section'
$ResourceCompiler = Get-ResourceCompilerPath
if (-not $ResourceCompiler) { throw 'Windows SDK rc.exe is required to embed the DLL icon resource.' }
$IconResource = Join-Path $BuildRoot 'DGSM-icon.res'
Push-Location (Split-Path -Parent $IconRc)
try {
    $resourceResult = Invoke-CapturedProcess -FilePath $ResourceCompiler.FullName -Arguments @('/nologo', '/fo', $IconResource, (Split-Path -Leaf $IconRc)) -TimeoutSeconds 60
    if ($resourceResult.StdOut) { Write-Host $resourceResult.StdOut }
    if ($resourceResult.StdErr) { Write-Host $resourceResult.StdErr -ForegroundColor DarkYellow }
    if ($resourceResult.ExitCode -ne 0) { throw "Windows resource compilation failed (exit $($resourceResult.ExitCode))." }
} finally {
    Pop-Location
}
if (-not (Test-Path -LiteralPath $IconResource -PathType Leaf)) {
    throw 'Resource compiler reported success but DGSM-icon.res was not created.'
}

Write-Step 'Compiling the native C ABI DLL with the Visual Studio x64 compiler'
Get-ChildItem -Path $DllDist -Filter '*.dll' -File -ErrorAction SilentlyContinue | Remove-Item -Force
$DllCommand = 'call "' + $VsDevCmd + '" -no_logo -arch=amd64 -host_arch=amd64 && "' + $VenvPython + '" "' + $DllBuilder + '" --output-dir "' + $DllDist + '" --resource-file "' + $IconResource + '"'
$cmdRawArguments = '/d /s /c "' + $DllCommand + '"'
$dllBuildRun = Invoke-CapturedProcess -FilePath $env:ComSpec -RawArguments $cmdRawArguments -TimeoutSeconds 1800
if ($dllBuildRun.StdOut) { Write-Host $dllBuildRun.StdOut }
if ($dllBuildRun.StdErr) { Write-Host $dllBuildRun.StdErr -ForegroundColor DarkYellow }
if ($dllBuildRun.ExitCode -ne 0) { throw "CFFI native DLL compilation failed (exit $($dllBuildRun.ExitCode))." }
$DllPath = Join-Path $DllDist 'discord_global_status.dll'
if (-not (Test-Path -LiteralPath $DllPath -PathType Leaf)) {
    $foundDll = Get-ChildItem -Path $DllDist -Filter '*.dll' -File | Select-Object -First 1
    if ($foundDll) { $DllPath = $foundDll.FullName }
    else { throw 'CFFI compilation returned success but no DLL was produced.' }
}
Copy-Item -LiteralPath $Source -Destination (Join-Path $DllDist 'discord_server_global_status.py') -Force
Copy-Item -LiteralPath $Header -Destination (Join-Path $DllDist 'discord_global_status.h') -Force
$DllBackends = Join-Path $DllDist 'backends'
if (Test-Path -LiteralPath $DllBackends) { Remove-Item -LiteralPath $DllBackends -Recurse -Force }
New-Item -ItemType Directory -Force -Path $DllBackends | Out-Null
Get-ChildItem -LiteralPath $BackendsDir -Filter '*.py' -File | Copy-Item -Destination $DllBackends -Force

if (-not $SkipSigning) {
    Write-Step 'Signing and verifying the EXE and DLL'
    $Signer = Join-Path $Root 'sign_windows.ps1'
    if (-not (Test-Path -LiteralPath $Signer)) { throw 'sign_windows.ps1 is missing.' }
    $Artifacts = @($ExePath, $DllPath)
    try {
        & $Signer -Paths $Artifacts
    } catch {
        throw "Authenticode signing script failed: $_"
    }
    foreach ($artifact in $Artifacts) {
        $signature = Get-AuthenticodeSignature -FilePath $artifact
        if ($signature.Status -ne 'Valid') {
            throw "Authenticode verification failed for $artifact : $($signature.Status) $($signature.StatusMessage)"
        }
    }
} else {
    Write-Warning 'Signing was explicitly skipped. EXE and DLL are unsigned.'
}

Write-Step 'Preparing Windows release package'
if (Test-Path $PackageStage) { Remove-Item -Recurse -Force $PackageStage }
New-Item -ItemType Directory -Force -Path $PackageStage | Out-Null
$StageFiles = @(
    @{ Source = $ExePath; Name = 'DiscordGlobalStatusMonitor.exe' },
    @{ Source = $DllPath; Name = 'discord_global_status.dll' },
    @{ Source = (Join-Path $DllDist 'discord_server_global_status.py'); Name = 'discord_server_global_status.py' },
    @{ Source = (Join-Path $DllDist 'discord_global_status.h'); Name = 'discord_global_status.h' },
    @{ Source = (Join-Path $Root 'RUN_DISCORD_STATUS.bat'); Name = 'RUN_DISCORD_STATUS.bat' },
    @{ Source = (Join-Path $Root 'README_Discord_Global_Status.txt'); Name = 'README.txt' },
    @{ Source = $IconPath; Name = 'DGSM.ico' },
    @{ Source = $IconPng; Name = 'DGSM-icon.png' },
    @{ Source = (Join-Path $Root 'GITHUB_README.txt'); Name = 'GITHUB_README.txt' },
    @{ Source = (Join-Path $Root 'README.md'); Name = 'README.md' },
    @{ Source = (Join-Path $Root '.env.example'); Name = '.env.example' },
    @{ Source = (Join-Path $Root 'BACKENDS.txt'); Name = 'BACKENDS.txt' },
    @{ Source = (Join-Path $Root 'CONTRIBUTING.md'); Name = 'CONTRIBUTING.md' },
    @{ Source = (Join-Path $Root 'SECURITY.md'); Name = 'SECURITY.md' },
    @{ Source = (Join-Path $Root '.gitattributes'); Name = '.gitattributes' },
    @{ Source = (Join-Path $Root 'BUILD_TROUBLESHOOTING.txt'); Name = 'BUILD_TROUBLESHOOTING.txt' },
    @{ Source = (Join-Path $Root 'Discord_Global_Status_Monitor_BUILD_INFO.txt'); Name = 'BUILD_INFO.txt' },
    @{ Source = (Join-Path $Root 'pyproject.toml'); Name = 'pyproject.toml' },
    @{ Source = (Join-Path $Root 'requirements-optional.txt'); Name = 'requirements-optional.txt' }
)
foreach ($entry in $StageFiles) {
    if (Test-Path -LiteralPath $entry.Source -PathType Leaf) {
        Copy-Item -LiteralPath $entry.Source -Destination (Join-Path $PackageStage $entry.Name) -Force
    }
}
$PackageBackends = Join-Path $PackageStage 'backends'
New-Item -ItemType Directory -Force -Path $PackageBackends | Out-Null
Get-ChildItem -LiteralPath $BackendsDir -Filter '*.py' -File | Copy-Item -Destination $PackageBackends -Force
$CertificatePath = Join-Path $SigningDir 'DGSM-Development-CodeSigning.cer'
if (Test-Path -LiteralPath $CertificatePath) {
    Copy-Item -LiteralPath $CertificatePath -Destination (Join-Path $PackageStage 'DGSM-Development-CodeSigning.cer') -Force
}
$SigningReportPath = Join-Path $SigningDir 'signing-report.txt'
if (Test-Path -LiteralPath $SigningReportPath) {
    Copy-Item -LiteralPath $SigningReportPath -Destination (Join-Path $PackageStage 'signing-report.txt') -Force
}
$signToolDisplay = if ($SignTool) { $SignTool.FullName } else { 'Not used (signing skipped)' }
$ManifestLines = @(
    'Discord Global Status Monitor - Windows x64 release',
    "Built: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')",
    "Python: $pyVersion",
    "Visual Studio: $VsPath",
    "SignTool: $signToolDisplay",
    "Signed: $([bool](-not $SkipSigning))",
    ''
)
foreach ($file in Get-ChildItem -LiteralPath $PackageStage -File) {
    $hash = Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
    $ManifestLines += ("{0}  {1}" -f $hash.Hash.ToLowerInvariant(), $file.Name)
}
$ManifestLines | Set-Content -LiteralPath (Join-Path $PackageStage 'SHA256SUMS.txt') -Encoding ASCII
if (Test-Path -LiteralPath $PackagePath) { Remove-Item -Force $PackagePath }
Compress-Archive -Path (Join-Path $PackageStage '*') -DestinationPath $PackagePath -CompressionLevel Optimal
$PackageHash = Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256
Set-Content -LiteralPath (Join-Path $Dist 'DiscordGlobalStatusMonitor-Windows-x64.zip.sha256') -Value ("{0}  {1}" -f $PackageHash.Hash.ToLowerInvariant(), (Split-Path -Leaf $PackagePath)) -Encoding ASCII

Write-Step 'Build, signing, verification and packaging completed'
Get-Item -LiteralPath $ExePath, $DllPath, $PackagePath |
    Select-Object FullName, Length, LastWriteTime | Format-Table -AutoSize
if (-not $SkipSigning) {
    Write-Host 'EXE and DLL have valid Authenticode signatures on this Windows user profile.' -ForegroundColor Green
    Write-Host 'A self-signed development certificate is not publicly trusted; see README.txt.' -ForegroundColor Yellow
}
Write-Host "Release package: $PackagePath" -ForegroundColor Green
