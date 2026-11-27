[CmdletBinding()]
param(
    [string]$PfxPath = $env:DGSM_PFX_PATH,
    [string[]]$TimestampUrls = @('http://timestamp.digicert.com', 'http://timestamp.sectigo.com'),
    [string]$SdkBuildToolsVersion = '10.0.28000.2705',
    [switch]$NoToolchainInstall,
    [switch]$SkipSigning,
    [switch]$KeepBuildCache
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
$BuildLogDir = Join-Path $Root '.build\windows\logs'
New-Item -ItemType Directory -Force -Path $BuildLogDir | Out-Null
$BuildLog = Join-Path $BuildLogDir ('build-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
try { Start-Transcript -Path $BuildLog -Append | Out-Null } catch { }

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
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

function Invoke-ElevatedInstaller {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$Operation = 'installer'
    )
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        throw "Cannot start $Operation because the file does not exist: $FilePath"
    }
    $signature = Get-AuthenticodeSignature -FilePath $FilePath
    if ($signature.Status -ne 'Valid' -or
        -not $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
        throw "Refusing to elevate an installer without a valid Microsoft Authenticode signature: $FilePath"
    }
    $argumentLine = (($Arguments | ForEach-Object { ConvertTo-WindowsArgument ([string]$_) }) -join ' ')
    Write-Host "Requesting UAC only for $Operation. The main DGSM build remains in the current user session." -ForegroundColor Yellow
    $child = Start-Process -FilePath $FilePath -ArgumentList $argumentLine -Verb RunAs -Wait -PassThru
    if (-not $child) { throw "Windows did not return an installer process for $Operation." }
    return [pscustomobject]@{
        ExitCode = [int]$child.ExitCode
        StdOut = ''
        StdErr = ''
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

function Enable-Tls12ForDownloads {
    # Windows PowerShell 5.1 can default to obsolete TLS versions on older hosts.
    # Add TLS 1.2 without disabling certificate validation.
    try {
        [Net.ServicePointManager]::SecurityProtocol =
            ([Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12)
    } catch { Write-Verbose "Could not adjust TLS protocol flags: $_" }
}

function Download-VerifiedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string]$ExpectedSha256 = $null,
        [string]$ExpectedPublisher = $null
    )
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    Enable-Tls12ForDownloads
    $mustDownload = -not (Test-Path -LiteralPath $Destination -PathType Leaf)
    if (-not $mustDownload -and $ExpectedSha256) {
        $cachedHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
        if ($cachedHash -ne $ExpectedSha256.ToUpperInvariant()) { $mustDownload = $true }
    }
    if (-not $mustDownload -and $ExpectedPublisher) {
        $cachedSignature = Get-AuthenticodeSignature -FilePath $Destination
        if ($cachedSignature.Status -ne 'Valid' -or
            -not $cachedSignature.SignerCertificate.Subject.Contains($ExpectedPublisher)) { $mustDownload = $true }
    }
    if ($mustDownload) {
        if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
        Write-Host "Downloading official installer: $Uri" -ForegroundColor Yellow
        $downloadError = $null
        for ($downloadAttempt = 1; $downloadAttempt -le 3; $downloadAttempt++) {
            try {
                Invoke-WebRequest -Uri $Uri -OutFile $Destination -UseBasicParsing -TimeoutSec 900 -ErrorAction Stop
                if ((Test-Path -LiteralPath $Destination -PathType Leaf) -and (Get-Item -LiteralPath $Destination).Length -ge 100KB) {
                    $downloadError = $null
                    break
                }
                throw 'The downloaded file is missing or smaller than the minimum expected size.'
            } catch {
                $downloadError = $_
                Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
                if ($downloadAttempt -lt 3) {
                    $delay = [Math]::Min(2 * [Math]::Pow(2, $downloadAttempt - 1), 8)
                    Write-Warning "Download attempt $downloadAttempt/3 failed: $downloadError. Retrying in $delay seconds."
                    Start-Sleep -Seconds $delay
                }
            }
        }
        if ($downloadError) { throw "Could not download '$Uri' after three attempts: $downloadError" }
    }
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) { throw "Download did not produce: $Destination" }
    if ((Get-Item -LiteralPath $Destination).Length -lt 100KB) { throw "Downloaded file is unexpectedly small: $Destination" }
    if ($ExpectedSha256) {
        $actualHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
        if ($actualHash -ne $ExpectedSha256.ToUpperInvariant()) {
            Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
            throw "SHA-256 verification failed. Expected $ExpectedSha256, received $actualHash."
        }
    }
    if ($ExpectedPublisher) {
        $signature = Get-AuthenticodeSignature -FilePath $Destination
        if ($signature.Status -ne 'Valid') { throw "Authenticode validation failed for '$Destination': $($signature.Status) $($signature.StatusMessage)" }
        $subject = [string]$signature.SignerCertificate.Subject
        if (-not $subject.Contains($ExpectedPublisher)) { throw "Unexpected installer publisher '$subject'; expected '$ExpectedPublisher'." }
    }
    return $Destination
}

function Get-PythonManagerPaths {
    $found = New-Object 'System.Collections.Generic.List[string]'
    foreach ($command in @(Get-Command 'pymanager.exe' -All -ErrorAction SilentlyContinue)) {
        if ($command.Source -and -not $found.Contains($command.Source)) { [void]$found.Add($command.Source) }
    }
    $windowsApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    $aliasPath = Join-Path $windowsApps 'pymanager.exe'
    if ((Test-Path -LiteralPath $aliasPath -PathType Leaf) -and -not $found.Contains($aliasPath)) { [void]$found.Add($aliasPath) }
    try {
        foreach ($package in @(Get-AppxPackage -Name 'PythonSoftwareFoundation.PythonManager' -ErrorAction SilentlyContinue)) {
            $candidate = Join-Path $package.InstallLocation 'pymanager.exe'
            if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and -not $found.Contains($candidate)) { [void]$found.Add($candidate) }
        }
    } catch { Write-Verbose "Python Manager package discovery unavailable: $_" }
    return @($found.ToArray())
}

function Install-PythonManagerDirect {
    # Direct official python.org MSIX installation avoids a WinGet dependency.
    # SHA-256 is published on the Python install manager 26.4 release page.
    $uri = 'https://www.python.org/ftp/python/pymanager/python-manager-26.4.msix'
    $sha256 = '002CFE4F4B63E322E9C8170CEA4D7D97E3E2D18246887325F969BB75F482F4DC'
    $packagePath = Join-Path (Join-Path $Root '.build\downloads') 'python-manager-26.4.msix'
    Download-VerifiedFile -Uri $uri -Destination $packagePath -ExpectedSha256 $sha256 | Out-Null
    if (-not (Get-Command Add-AppxPackage -ErrorAction SilentlyContinue)) {
        throw 'Add-AppxPackage is unavailable; this Windows installation cannot deploy the official Python Manager MSIX.'
    }
    Write-Step 'Installing the official Python install manager directly from python.org (WinGet is not required)'
    Add-AppxPackage -Path $packagePath -ErrorAction Stop
    Refresh-ProcessPath
    return (Get-PythonManagerPaths)
}

function Install-Cpython313Standalone {
    # Emergency fallback for Windows hosts where AppX/MSIX deployment is blocked.
    # Prefer the Python Manager because it can install current security runtimes.
    $uri = 'https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe'
    $sha256 = '5DA012101BA2E17113A9A1CD901E9CE1441086F2B7ECB5DE06EC94163899A190'
    $installerPath = Join-Path (Join-Path $Root '.build\downloads') 'python-3.13.17-amd64.exe'
    Download-VerifiedFile -Uri $uri -Destination $installerPath -ExpectedSha256 $sha256 -ExpectedPublisher 'Python Software Foundation' | Out-Null
    $target = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313'
    $targetPython = Join-Path $target 'python.exe'
    if (Test-PythonBuildPath $targetPython) { return $targetPython }
    Write-Step 'Installing verified CPython 3.13.17 per-user from the official Python.org installer'
    $installerArgs = @('/quiet', 'InstallAllUsers=0', "TargetDir=$target", 'PrependPath=0', 'Include_launcher=0', 'Include_pip=1', 'Include_tcltk=0', 'Include_test=0', 'Shortcuts=0')
    $install = Invoke-CapturedProcess -FilePath $installerPath -Arguments $installerArgs -TimeoutSeconds 1800
    if ($install.StdOut) { Write-Host $install.StdOut }
    if ($install.StdErr) { Write-Host $install.StdErr -ForegroundColor DarkYellow }
    if ($install.ExitCode -notin @(0, 3010)) { throw "Official CPython installer failed (exit $($install.ExitCode))." }
    $verified = Test-PythonBuildPath $targetPython
    if (-not $verified) { throw "CPython installer completed without a supported runtime at $targetPython" }
    return $verified
}

function Test-PythonBuildPath([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $null
    }
    try {
        $checkCode = 'import sys, struct; ok=(sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] < (3, 16) and struct.calcsize("P") == 8); print("DGSM_BUILD_RUNTIME_OK=" + str(ok)); print("DGSM_BUILD_RUNTIME_VERSION=" + ".".join(map(str, sys.version_info[:3])))'
        $check = Invoke-CapturedProcess -FilePath $Candidate -Arguments @('-c', $checkCode) -TimeoutSeconds 20
        if ($check.ExitCode -eq 0 -and $check.StdOut -match 'DGSM_BUILD_RUNTIME_OK=True') {
            return $Candidate
        }
    } catch {
        Write-Verbose "Python candidate check failed for '$Candidate': $_"
    }
    return $null
}

function Get-PythonBuildRuntime {
    # PyInstaller 6.21+ supports CPython 3.15; CFFI 2.1+ is tested on 3.11-3.15.
    # Prefer the newest already-installed x64 CPython and avoid needless installers.
    $pythonTags = @('3.15', '3.14', '3.13', '3.12', '3.11')
    $candidates = New-Object 'System.Collections.Generic.List[string]'
    $addCandidate = {
        param([string]$Candidate)
        if (-not [string]::IsNullOrWhiteSpace($Candidate) -and -not $candidates.Contains($Candidate)) {
            [void]$candidates.Add($Candidate)
        }
    }

    # Discover registered CPython installs for both normal and 64-bit tags.
    $registryRoots = @(
        'HKCU:\Software\Python\PythonCore',
        'HKLM:\Software\Python\PythonCore',
        'HKLM:\Software\WOW6432Node\Python\PythonCore'
    )
    foreach ($registryRoot in $registryRoots) {
        foreach ($tag in $pythonTags) {
            foreach ($variant in @($tag, "$tag-64")) {
                $key = Join-Path (Join-Path $registryRoot $variant) 'InstallPath'
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
        }
    }

    # Standard per-user and machine layouts, including the user's existing Python315.
    foreach ($tag in $pythonTags) {
        $compact = $tag.Replace('.', '')
        foreach ($base in @(
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$compact"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$compact-64"),
            (Join-Path $env:ProgramFiles "Python$compact"),
            (Join-Path $env:ProgramFiles "Python $tag"),
            "C:\Python$compact"
        )) {
            & $addCandidate (Join-Path $base 'python.exe')
        }
    }

    # PATH may contain python.exe, python3.x.exe, or a Store execution alias.
    $commandNames = @('python.exe', 'python3.exe') + @($pythonTags | ForEach-Object { "python$_.exe" })
    foreach ($name in $commandNames) {
        foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($command.Source) { & $addCandidate $command.Source }
        }
    }

    foreach ($candidate in $candidates) {
        $valid = Test-PythonBuildPath $candidate
        if ($valid) { return $valid }
    }

    # Query the unambiguous Python Manager command, one supported runtime at a time.
    foreach ($manager in (Get-PythonManagerPaths)) {
        foreach ($tag in $pythonTags) {
            try {
                $listResult = Invoke-CapturedProcess -FilePath $manager -Arguments @('list', '-1', '--format=exe', $tag) -TimeoutSeconds 30
                if ($listResult.ExitCode -eq 0) {
                    foreach ($line in ($listResult.StdOut -split "`r?`n")) {
                        $listedPath = $line.Trim().Trim([char]34)
                        if ($listedPath -and (Test-Path -LiteralPath $listedPath -PathType Leaf)) {
                            $validListedPath = Test-PythonBuildPath $listedPath
                            if ($validListedPath) { return $validListedPath }
                        }
                    }
                }
            } catch { Write-Verbose "Python Manager runtime list probe failed for '$manager' ($tag): $_" }
        }
    }

    # Safe, non-installing compatibility probes for the legacy Python Launcher.
    # NEVER pass 'install' or 'list' to py.exe because it may be the legacy launcher.
    $legacy = Get-Command 'py.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($legacy -and $legacy.Source) {
        $probeCode = 'import sys, struct; ok=(sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] < (3, 16) and struct.calcsize("P") == 8); print("DGSM_BUILD_RUNTIME_OK=" + str(ok)); print(sys.executable)'
        foreach ($tag in $pythonTags) {
            foreach ($arguments in @(@("-V:$tag", '-c', $probeCode), @("-$tag", '-c', $probeCode))) {
                try {
                    $result = Invoke-CapturedProcess -FilePath $legacy.Source -Arguments $arguments -TimeoutSeconds 30
                    if ($result.ExitCode -eq 0 -and $result.StdOut -match 'DGSM_BUILD_RUNTIME_OK=True') {
                        $line = ($result.StdOut -split "`r?`n" | Where-Object { $_ -and (Test-Path -LiteralPath $_.Trim() -PathType Leaf) } | Select-Object -First 1)
                        if ($line) {
                            $valid = Test-PythonBuildPath $line.Trim()
                            if ($valid) { return $valid }
                        }
                    }
                } catch { Write-Verbose "Legacy Python launcher probe failed ($tag): $_" }
            }
        }
    }
    return $null
}

function Ensure-PythonBuildRuntime {
    $python = Get-PythonBuildRuntime
    if ($python) { return $python }
    if ($NoToolchainInstall) { throw 'No supported x64 CPython runtime (3.11-3.15) is installed. Remove -NoToolchainInstall to install it automatically.' }

    $managerCommands = @(Get-PythonManagerPaths)
    if ($managerCommands.Count -eq 0) {
        Write-Step 'No supported CPython runtime found; installing official Python Manager directly (without WinGet)'
        try { $managerCommands = @(Install-PythonManagerDirect) }
        catch { Write-Warning "Python Manager MSIX installation was unavailable: $_" }
    }

    # Python Manager uses 'pymanager.exe'; the legacy 'py.exe' is never used for installs.
    foreach ($manager in $managerCommands) {
        try {
            $configure = Invoke-CapturedProcess -FilePath $manager -Arguments @('install', '--configure', '-y') -TimeoutSeconds 120
            if ($configure.StdOut) { Write-Host $configure.StdOut }
            if ($configure.StdErr) { Write-Host $configure.StdErr -ForegroundColor DarkYellow }
            if ($configure.ExitCode -ne 0) { Write-Warning "Python Manager configuration returned exit code $($configure.ExitCode); continuing with runtime installation." }
        } catch { Write-Warning "Python Manager first-launch configuration was skipped: $_" }

        foreach ($tag in @('3.15', '3.14', '3.13', '3.12', '3.11')) {
            try {
                Write-Step "Provisioning CPython $tag through $([IO.Path]::GetFileName($manager))"
                $result = Invoke-CapturedProcess -FilePath $manager -Arguments @('install', $tag) -TimeoutSeconds 1200
                if ($result.StdOut) { Write-Host $result.StdOut }
                if ($result.StdErr) { Write-Host $result.StdErr -ForegroundColor DarkYellow }
                if ($result.ExitCode -notin @(0, 3010)) { Write-Warning "Python Manager returned exit code $($result.ExitCode) for $tag." }
            } catch { Write-Warning "Python Manager provisioning failed for $tag`: $_" }

            for ($attempt = 0; $attempt -lt 6; $attempt++) {
                Refresh-ProcessPath
                $python = Get-PythonBuildRuntime
                if ($python) { return $python }
                Start-Sleep -Seconds 2
            }
        }
    }

    # Last-resort signed standalone runtime for restricted hosts where AppX is blocked.
    Write-Step 'Falling back to the official SHA-256 and Authenticode-verified CPython 3.13.17 Windows installer'
    try {
        $python = Install-Cpython313Standalone
        if ($python) { return $python }
    } catch { Write-Warning "Standalone CPython installation failed: $_" }

    throw 'Unable to provision a supported x64 CPython runtime. Check .build\downloads, AppX deployment policy, proxy/TLS settings and Python.org installer signature. WinGet is not required and no legacy py.exe install command was used.'
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


function Ensure-WindowsToolchain {
    # The DGSM native C ABI DLL requires MSVC and the Windows SDK resource
    # compiler. SignTool is resolved separately by sign_windows.ps1: it uses
    # an installed SDK copy or the same temporary Microsoft NuGet fallback as
    # Destiny2DamageTracker, and never requires the full SDK just for signing.
    $vsCpp = Get-VsInstallPath -RequireCpp
    $resourceCompiler = Get-ResourceCompilerPath
    if ($vsCpp -and $resourceCompiler) {
        Write-Host "MSVC and Windows SDK resource compiler found. Visual Studio: $vsCpp" -ForegroundColor Green
        return $vsCpp
    }
    if ($NoToolchainInstall) {
        throw 'Required MSVC x64 compiler or Windows SDK rc.exe is missing. Remove -NoToolchainInstall to install these components automatically.'
    }
    # Elevate only the signed Microsoft installer below. Keep the build itself
    # in the invoking user's profile so its certificate and desktop shortcut
    # are created for the correct Windows account.

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
        Write-Step 'Adding MSVC x64 and Windows 11 SDK resource compiler to the existing Visual Studio installation'
        $setupArgs = @('modify', '--installPath', $anyVs)
        foreach ($component in $components) { $setupArgs += @('--add', $component) }
        $setupArgs += @('--quiet', '--wait', '--norestart', '--includeRecommended')
        $setupArgs += @('--log', (Join-Path $BuildLogDir 'vs-buildtools-modify.log'))
        $setupResult = Invoke-ElevatedInstaller -FilePath $setup -Arguments $setupArgs -Operation 'Visual Studio Build Tools component installation'
        if ($setupResult.StdOut) { Write-Host $setupResult.StdOut }
        if ($setupResult.StdErr) { Write-Host $setupResult.StdErr -ForegroundColor DarkYellow }
        if ($setupResult.ExitCode -notin @(0, 3010)) { throw "Visual Studio component installation failed (exit $($setupResult.ExitCode))." }
    } else {
        Write-Step 'Installing Microsoft Visual Studio Build Tools directly (WinGet is not required)'
        $vsInstaller = Join-Path (Join-Path $Root '.build\downloads') 'vs_BuildTools.exe'
        Download-VerifiedFile -Uri 'https://aka.ms/vs/17/release/vs_BuildTools.exe' -Destination $vsInstaller -ExpectedPublisher 'Microsoft Corporation' | Out-Null
        $pf86 = ${env:ProgramFiles(x86)}
        if (-not $pf86) { $pf86 = $env:ProgramFiles }
        $installPath = Join-Path $pf86 'Microsoft Visual Studio\2022\BuildTools'
        $vsArgs = @(
            '--quiet', '--wait', '--norestart', '--nocache',
            '--installPath', $installPath,
            '--add', 'Microsoft.VisualStudio.Workload.VCTools',
            '--add', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
            '--add', 'Microsoft.VisualStudio.Component.Windows11SDK.26100',
            '--includeRecommended',
            '--log', (Join-Path $BuildLogDir 'vs-buildtools-install.log')
        )
        $vsInstall = Invoke-ElevatedInstaller -FilePath $vsInstaller -Arguments $vsArgs -Operation 'Visual Studio Build Tools installation'
        if ($vsInstall.StdOut) { Write-Host $vsInstall.StdOut }
        if ($vsInstall.StdErr) { Write-Host $vsInstall.StdErr -ForegroundColor DarkYellow }
        if ($vsInstall.ExitCode -notin @(0, 3010)) {
            throw "Visual Studio Build Tools bootstrapper failed (exit $($vsInstall.ExitCode)). Review the Microsoft installer log and the DGSM build transcript."
        }
    }

    $vsCpp = Get-VsInstallPath -RequireCpp
    if (-not $vsCpp) {
        throw 'MSVC x64 component is still not detected after installation. Reboot if Windows Installer requested it, then rerun.'
    }
    $resourceCompiler = Get-ResourceCompilerPath
    if (-not $resourceCompiler) {
        throw 'Windows SDK rc.exe was not found after Visual Studio Build Tools setup. Add the Windows 11 SDK component and rerun.'
    }
    Write-Host "MSVC ready: $vsCpp" -ForegroundColor Green
    Write-Host "Resource compiler ready: $($resourceCompiler.FullName)" -ForegroundColor Green
    return $vsCpp
}

trap {
    Write-Host "`nBUILD ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Full build log: $BuildLog" -ForegroundColor Yellow
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This script must run on Windows. Windows PE binaries cannot be built natively by this script on Linux.'
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw 'This release pipeline currently targets Windows x64 only.'
}

if (($PSVersionTable.PSVersion.Major -lt 5) -or ($PSVersionTable.PSVersion.Major -eq 5 -and $PSVersionTable.PSVersion.Minor -lt 1)) {
    throw 'Windows PowerShell 5.1 or newer is required. Run this builder in Windows PowerShell 5.1 or PowerShell 7.'
}

Write-Host "PowerShell: $($PSVersionTable.PSVersion) | OS: $([Environment]::OSVersion.VersionString) | Process: $([IntPtr]::Size * 8)-bit" -ForegroundColor DarkGray
Write-Host "Build transcript: $BuildLog" -ForegroundColor DarkGray
Write-Step 'Checking source files and platform prerequisites'
$Source = Join-Path $Root 'discord_server_global_status.py'
$DllBuilder = Join-Path $Root 'discord_monitor_dll_build.py'
$Header = Join-Path $Root 'discord_global_status.h'
$BuildRequirements = Join-Path $Root 'requirements-build.txt'
$IconPath = Join-Path $Root 'assets\DGSM.ico'
$IconPng = Join-Path $Root 'assets\DGSM-icon.png'
$IconRcTemplate = Join-Path $Root 'assets\DGSM.rc'
$VersionTemplateFile = Join-Path $Root 'assets\version_info.txt'
$VersionModule = Join-Path $Root 'dgsm_version.py'
$DllSmokeTest = Join-Path $Root 'dll_smoke_test.py'
$BackendsDir = Join-Path $Root 'backends'
foreach ($file in @($Source, $DllBuilder, $Header, $BuildRequirements, $IconPath, $IconPng, $IconRcTemplate, $VersionTemplateFile, $VersionModule, $DllSmokeTest)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required source file is missing: $file" }
}
if (-not (Test-Path -LiteralPath (Join-Path $BackendsDir '__init__.py') -PathType Leaf)) { throw "Required telemetry backend package is missing: $BackendsDir" }

# Derive every Windows resource and smoke-test version from dgsm_version.py.
# This prevents the EXE, DLL, Python module and release package drifting apart.
$versionSourceText = Get-Content -LiteralPath $VersionModule -Raw
$versionMatch = [regex]::Match($versionSourceText, '(?m)^__version__\s*=\s*"(?<version>\d+\.\d+\.\d+)"')
if (-not $versionMatch.Success) { throw 'Could not parse a stable X.Y.Z version from dgsm_version.py.' }
$AppVersion = $versionMatch.Groups['version'].Value
$AppVersionParts = $AppVersion.Split('.') | ForEach-Object { [int]$_ }
$VersionTuple = ($AppVersionParts + 0) -join ', '
$VersionComma4 = ($AppVersionParts + 0) -join ','
$VersionDotted4 = "$AppVersion.0"
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
$VersionFile = Join-Path $BuildRoot 'version_info.generated.txt'
$IconRc = Join-Path $BuildRoot 'DGSM.generated.rc'
$versionTemplate = Get-Content -LiteralPath $VersionTemplateFile -Raw
$versionTemplate = $versionTemplate.Replace('@DGSM_VERSION_TUPLE@', $VersionTuple).Replace('@DGSM_VERSION_DOTTED4@', $VersionDotted4).Replace('@DGSM_VERSION@', $AppVersion)
if ($versionTemplate -match '@DGSM_[A-Z0-9_]+@') { throw 'PyInstaller version template contains unresolved DGSM placeholders.' }
[IO.File]::WriteAllText($VersionFile, $versionTemplate, ([System.Text.UTF8Encoding]::new($false)))
$rcTemplate = Get-Content -LiteralPath $IconRcTemplate -Raw
$rcTemplate = $rcTemplate.Replace('@DGSM_VERSION_COMMA4@', $VersionComma4).Replace('@DGSM_VERSION_DOTTED4@', $VersionDotted4).Replace('@DGSM_VERSION@', $AppVersion)
$absoluteIconPath = $IconPath.Replace([char]92, [char]47)
$absoluteIconResource = '1 ICON "' + $absoluteIconPath + '"'
$rcTemplate = $rcTemplate.Replace('1 ICON "DGSM.ico"', $absoluteIconResource)
if ($rcTemplate -match '@DGSM_[A-Z0-9_]+@') { throw 'Windows resource template contains unresolved DGSM placeholders.' }
# Use numeric VERSIONINFO constants to keep rc.exe independent of winver.h
# include-path initialization. These are VOS_NT_WINDOWS32 and VFT_DLL values.
if ($rcTemplate -notmatch '(?m)^\s*FILEOS\s+0x00040004L\s*$') {
    throw 'DLL RC template must use FILEOS 0x00040004L (VOS_NT_WINDOWS32).'
}
if ($rcTemplate -notmatch '(?m)^\s*FILETYPE\s+0x00000002L\s*$') {
    throw 'DLL RC template must use FILETYPE 0x00000002L (VFT_DLL).'
}
[IO.File]::WriteAllText($IconRc, $rcTemplate, ([System.Text.UTF8Encoding]::new($false)))
Write-Host "Canonical DGSM version: $AppVersion" -ForegroundColor Green

Write-Step 'Checking for an existing supported CPython x64 build runtime (3.11-3.15)'
$PythonBase = Ensure-PythonBuildRuntime
$versionCheck = Invoke-CapturedProcess -FilePath $PythonBase -Arguments @('-c', 'import sys; print(".".join(map(str, sys.version_info[:3])))') -TimeoutSeconds 20
if ($versionCheck.ExitCode -ne 0) { throw "Selected CPython runtime failed its startup check: $($versionCheck.StdErr)" }
$pyVersion = ($versionCheck.StdOut -split "`r?`n" | Select-Object -First 1).Trim()
$pyVersionParts = $pyVersion.Split('.')
if ([int]$pyVersionParts[0] -ne 3 -or [int]$pyVersionParts[1] -lt 11 -or [int]$pyVersionParts[1] -ge 16) { throw "Unsupported CPython build runtime: $pyVersion (supported: 3.11-3.15 x64)." }
Write-Host "Using Python $pyVersion at $PythonBase" -ForegroundColor Green

$VsPath = Ensure-WindowsToolchain
$VsDevCmd = Join-Path $VsPath 'Common7\Tools\VsDevCmd.bat'
if (-not (Test-Path -LiteralPath $VsDevCmd)) { throw "VsDevCmd.bat was not found under $VsPath" }
$SignTool = Get-SignToolPath
if ($SignTool) {
    Write-Host "Installed SignTool found: $($SignTool.FullName)" -ForegroundColor Green
} elseif (-not $SkipSigning) {
    Write-Host 'No installed SignTool found; sign_windows.ps1 will use the verified Microsoft SDK BuildTools NuGet fallback.' -ForegroundColor Yellow
}

Write-Step 'Creating isolated build environment and installing all Python build/runtime dependencies'
New-Item -ItemType Directory -Force -Path $BuildRoot, $ExeDist, $DllDist, $SigningDir | Out-Null
# Remove prior release outputs before a new build so a failed run cannot leave
# an old ZIP beside new/partial binaries and look like a current release.
$staleReleasePaths = @(
    $PackagePath,
    (Join-Path $Dist 'DiscordGlobalStatusMonitor-Windows-x64.zip.sha256'),
    (Join-Path $SigningDir 'signing-report.txt'),
    (Join-Path $SigningDir 'DGSM-Development-CodeSigning.cer'),
    (Join-Path $SigningDir 'LOCAL_SIGNING_CERT_THUMBPRINT.txt')
)
foreach ($staleReleasePath in $staleReleasePaths) {
    if (Test-Path -LiteralPath $staleReleasePath) {
        Remove-Item -LiteralPath $staleReleasePath -Force -ErrorAction SilentlyContinue
    }
}
$rebuildVenv = -not (Test-Path -LiteralPath $VenvPython)
if (-not $rebuildVenv) {
    $expectedMinor = ($pyVersion.Split('.')[0..1] -join '.')
    $venvCheckCode = "import sys; assert '.'.join(map(str, sys.version_info[:2])) == '$expectedMinor'"
    $venvCheck = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @('-c', $venvCheckCode) -TimeoutSeconds 20
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
$pipDeps = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @('-m', 'pip', 'install', '--upgrade', '-r', $BuildRequirements) -TimeoutSeconds 1800
if ($pipDeps.StdOut) { Write-Host $pipDeps.StdOut }
if ($pipDeps.StdErr) { Write-Host $pipDeps.StdErr -ForegroundColor DarkYellow }
if ($pipDeps.ExitCode -ne 0) { throw 'One or more build/runtime Python dependencies could not be installed.' }
$dependencyCheck = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @('-c', 'import PyInstaller, cffi, certifi, psutil, dns, plyer, colorama, dgsm_version; print("All Python dependencies and version metadata imported successfully.")') -TimeoutSeconds 60
if ($dependencyCheck.ExitCode -ne 0) { throw 'Dependency import validation failed.' }
Write-Host $dependencyCheck.StdOut -ForegroundColor Green
$dependencyVersionCode = @'
from importlib.metadata import version
for name in ("pip", "setuptools", "wheel", "pyinstaller", "cffi", "certifi", "psutil", "dnspython", "plyer", "colorama"):
    try:
        print(f"{name}=={version(name)}")
    except Exception as exc:
        print(f"{name}=UNAVAILABLE ({exc})")
'@
$dependencyVersions = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @('-c', $dependencyVersionCode) -TimeoutSeconds 60
if ($dependencyVersions.ExitCode -ne 0) { throw 'Could not record build dependency versions.' }
$BuildDependencyVersions = $dependencyVersions.StdOut.Trim()
Write-Host "Build dependency versions:`n$BuildDependencyVersions" -ForegroundColor DarkGray

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
    '--version-file', $VersionFile,
    '--distpath', $ExeDist, '--workpath', $Work, '--specpath', $Spec,
    '--collect-all', 'certifi', '--collect-all', 'psutil', '--collect-all', 'dns',
    '--hidden-import', 'plyer.platforms.win.notification',
    '--collect-submodules', 'backends',
    '--hidden-import', 'colorama', '--hidden-import', 'dgsm_version', $Source
)
$pyInstallerRun = Invoke-CapturedProcess -FilePath $VenvPython -Arguments $pyInstallerArgs -TimeoutSeconds 1800
if ($pyInstallerRun.StdOut) { Write-Host $pyInstallerRun.StdOut }
if ($pyInstallerRun.StdErr) { Write-Host $pyInstallerRun.StdErr -ForegroundColor DarkYellow }
if ($pyInstallerRun.ExitCode -ne 0) { throw 'PyInstaller failed to build the Windows executable.' }
if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) { throw 'PyInstaller returned success but the EXE output is missing.' }
$exeVersionResource = (Get-Item -LiteralPath $ExePath).VersionInfo
if ($exeVersionResource.ProductVersion -notlike "$AppVersion*" -or
    $exeVersionResource.FileDescription -notmatch 'Discord Global Status Monitor') {
    throw "The EXE is missing expected DGSM version metadata. ProductVersion='$($exeVersionResource.ProductVersion)'; Description='$($exeVersionResource.FileDescription)'"
}
$exeSmoke = Invoke-CapturedProcess -FilePath $ExePath -Arguments @('--help') -TimeoutSeconds 60
if ($exeSmoke.ExitCode -ne 0) { throw "The generated EXE failed its --help smoke test: $($exeSmoke.StdErr)" }
$exeVersionSmoke = Invoke-CapturedProcess -FilePath $ExePath -Arguments @('--version') -TimeoutSeconds 60
if ($exeVersionSmoke.ExitCode -ne 0 -or $exeVersionSmoke.StdOut -notmatch [regex]::Escape($AppVersion)) {
    throw "The generated EXE failed its version smoke test. Output: $($exeVersionSmoke.StdOut) $($exeVersionSmoke.StdErr)"
}
Write-Host $exeVersionSmoke.StdOut.Trim() -ForegroundColor Green

Write-Step 'Embedding DGSM icon and version metadata into the native DLL resource section'
$ResourceCompiler = Get-ResourceCompilerPath
if (-not $ResourceCompiler) { throw 'Windows SDK rc.exe is required to embed the DLL icon resource.' }
$IconResource = Join-Path $BuildRoot 'DGSM-icon.res'
$resourceResult = Invoke-CapturedProcess -FilePath $ResourceCompiler.FullName -Arguments @('/nologo', '/fo', $IconResource, $IconRc) -TimeoutSeconds 60
if ($resourceResult.StdOut) { Write-Host $resourceResult.StdOut }
if ($resourceResult.StdErr) { Write-Host $resourceResult.StdErr -ForegroundColor DarkYellow }
if ($resourceResult.ExitCode -ne 0) { throw "Windows resource compilation failed (exit $($resourceResult.ExitCode))." }
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
$dllVersionResource = (Get-Item -LiteralPath $DllPath).VersionInfo
if ($dllVersionResource.ProductVersion -notlike "$AppVersion*" -or
    $dllVersionResource.FileDescription -notmatch 'Discord Global Status Monitor') {
    throw "The DLL is missing expected DGSM version/icon resource metadata. ProductVersion='$($dllVersionResource.ProductVersion)'; Description='$($dllVersionResource.FileDescription)'"
}
Copy-Item -LiteralPath $Source -Destination (Join-Path $DllDist 'discord_server_global_status.py') -Force
Copy-Item -LiteralPath $VersionModule -Destination (Join-Path $DllDist 'dgsm_version.py') -Force
Copy-Item -LiteralPath $Header -Destination (Join-Path $DllDist 'discord_global_status.h') -Force
$DllBackends = Join-Path $DllDist 'backends'
if (Test-Path -LiteralPath $DllBackends) { Remove-Item -LiteralPath $DllBackends -Recurse -Force }
New-Item -ItemType Directory -Force -Path $DllBackends | Out-Null
Get-ChildItem -LiteralPath $BackendsDir -Filter '*.py' -File | Copy-Item -Destination $DllBackends -Force
$DllSmoke = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @($DllSmokeTest, $DllPath, '--expected-version', $AppVersion) -TimeoutSeconds 60
if ($DllSmoke.StdOut) { Write-Host $DllSmoke.StdOut -ForegroundColor Green }
if ($DllSmoke.StdErr) { Write-Host $DllSmoke.StdErr -ForegroundColor DarkYellow }
if ($DllSmoke.ExitCode -ne 0) { throw 'The compiled DLL failed its C ABI smoke test. The DLL has not been signed.' }

if (-not $SkipSigning) {
    Write-Step 'Signing and verifying the EXE and DLL'
    $Signer = Join-Path $Root 'sign_windows.ps1'
    if (-not (Test-Path -LiteralPath $Signer)) { throw 'sign_windows.ps1 is missing.' }
    $Artifacts = @($ExePath, $DllPath)
    try {
        & $Signer -Paths $Artifacts -PfxPath $PfxPath -TimestampUrls $TimestampUrls -SdkBuildToolsVersion $SdkBuildToolsVersion
    } catch {
        throw "Authenticode signing script failed: $_"
    }
    foreach ($artifact in $Artifacts) {
        $signature = Get-AuthenticodeSignature -FilePath $artifact
        if ($signature.Status -ne 'Valid') {
            throw "Authenticode verification failed for $artifact : $($signature.Status) $($signature.StatusMessage)"
        }
    }
    # Verify that Authenticode signing did not break the native C ABI loader.
    $postSignDllSmoke = Invoke-CapturedProcess -FilePath $VenvPython -Arguments @($DllSmokeTest, $DllPath, '--expected-version', $AppVersion) -TimeoutSeconds 60
    if ($postSignDllSmoke.ExitCode -ne 0) {
        throw "The signed DLL failed its post-sign load test: $($postSignDllSmoke.StdErr) $($postSignDllSmoke.StdOut)"
    }
    Write-Host 'Post-sign DLL load test: PASS' -ForegroundColor Green
    $postSignExeSmoke = Invoke-CapturedProcess -FilePath $ExePath -Arguments @('--version') -TimeoutSeconds 60
    if ($postSignExeSmoke.ExitCode -ne 0 -or $postSignExeSmoke.StdOut -notmatch [regex]::Escape($AppVersion)) {
        throw 'The signed EXE failed its post-sign version smoke test.'
    }
} else {
    Write-Warning 'Signing was explicitly skipped. EXE and DLL are unsigned.'
    foreach ($staleArtifact in @('signing-report.txt', 'DGSM-Development-CodeSigning.cer', 'LOCAL_SIGNING_CERT_THUMBPRINT.txt')) {
        Remove-Item -LiteralPath (Join-Path $SigningDir $staleArtifact) -Force -ErrorAction SilentlyContinue
    }
}

# Match the Destiny 2 Damage Tracker delivery flow: create the shortcut only
# after artifact smoke tests and, when signing is enabled, signature verification.
$ShortcutPath = $null
try {
    $DesktopDir = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
    if ([string]::IsNullOrWhiteSpace($DesktopDir)) { $DesktopDir = Join-Path $HOME 'Desktop' }
    New-Item -ItemType Directory -Force -Path $DesktopDir | Out-Null
    $ShortcutPath = Join-Path $DesktopDir 'Discord Global Status Monitor.lnk'
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $ExePath
    $Shortcut.WorkingDirectory = $ExeDist
    $Shortcut.IconLocation = "$ExePath,0"
    $Shortcut.Description = 'Discord Global Status Monitor - worldwide Discord service health'
    $Shortcut.WindowStyle = 1
    $Shortcut.Save()
    if (-not (Test-Path -LiteralPath $ShortcutPath)) { throw 'Windows Script Host did not create the shortcut.' }
    Write-Host "Desktop shortcut: $ShortcutPath" -ForegroundColor Green
} catch {
    Write-Warning "The binaries built successfully, but desktop shortcut creation failed: $($_.Exception.Message)"
    Write-Warning "Create a shortcut manually to: $ExePath"
}

Write-Step 'Preparing Windows release package'
if (Test-Path $PackageStage) { Remove-Item -Recurse -Force $PackageStage }
New-Item -ItemType Directory -Force -Path $PackageStage | Out-Null
$StageFiles = @(
    @{ Source = $ExePath; Name = 'DiscordGlobalStatusMonitor.exe' },
    @{ Source = $DllPath; Name = 'discord_global_status.dll' },
    @{ Source = (Join-Path $DllDist 'discord_server_global_status.py'); Name = 'discord_server_global_status.py' },
    @{ Source = (Join-Path $DllDist 'dgsm_version.py'); Name = 'dgsm_version.py' },
    @{ Source = (Join-Path $DllDist 'discord_global_status.h'); Name = 'discord_global_status.h' },
    @{ Source = $DllSmokeTest; Name = 'dll_smoke_test.py' },
    @{ Source = $VersionFile; Name = 'version_info.txt' },
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
    @{ Source = (Join-Path $Root 'requirements-optional.txt'); Name = 'requirements-optional.txt' },
    @{ Source = (Join-Path $Root 'requirements-build.txt'); Name = 'requirements-build.txt' },
    @{ Source = (Join-Path $Root 'BUILD_SIGNING_WINDOWS.txt'); Name = 'BUILD_SIGNING_WINDOWS.txt' }
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
$signToolDisplay = if ($SkipSigning) { 'Not used (signing skipped)' } elseif ($SignTool) { $SignTool.FullName } else { 'Microsoft SDK BuildTools NuGet temporary fallback (see signing-report.txt)' }
$ManifestLines = @(
    'Discord Global Status Monitor - Windows x64 release',
    "Built: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')",
    "DGSM version: $AppVersion",
    "Python: $pyVersion",
    "Visual Studio: $VsPath",
    "SignTool: $signToolDisplay",
    "Signed: $([bool](-not $SkipSigning))",
    "Shortcut: $ShortcutPath",
    'Build dependency versions:',
    $BuildDependencyVersions,
    ''
)
foreach ($file in Get-ChildItem -LiteralPath $PackageStage -File -Recurse | Where-Object { $_.Name -ne 'SHA256SUMS.txt' }) {
    $hash = Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
    $relative = $file.FullName.Substring($PackageStage.Length).TrimStart('\')
    $ManifestLines += ("{0}  {1}" -f $hash.Hash.ToLowerInvariant(), $relative)
}
$ManifestLines | Set-Content -LiteralPath (Join-Path $PackageStage 'SHA256SUMS.txt') -Encoding ASCII
if (Test-Path -LiteralPath $PackagePath) { Remove-Item -Force $PackagePath }
Compress-Archive -Path (Join-Path $PackageStage '*') -DestinationPath $PackagePath -CompressionLevel Optimal
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipArchive = [System.IO.Compression.ZipFile]::OpenRead($PackagePath)
try {
    $requiredEntries = @('DiscordGlobalStatusMonitor.exe', 'discord_global_status.dll', 'discord_global_status.h', 'discord_server_global_status.py', 'dgsm_version.py', 'README.md', 'SHA256SUMS.txt')
    $zipNames = @($zipArchive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
    foreach ($requiredEntry in $requiredEntries) {
        if ($requiredEntry -notin $zipNames) { throw "Release ZIP is missing required file: $requiredEntry" }
    }
    if (-not $SkipSigning -and 'signing-report.txt' -notin $zipNames) {
        throw 'Signed release ZIP is missing signing-report.txt.'
    }
} finally {
    $zipArchive.Dispose()
}
$PackageHash = Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256
Set-Content -LiteralPath (Join-Path $Dist 'DiscordGlobalStatusMonitor-Windows-x64.zip.sha256') -Value ("{0}  {1}" -f $PackageHash.Hash.ToLowerInvariant(), (Split-Path -Leaf $PackagePath)) -Encoding ASCII

if (-not $KeepBuildCache) {
    Write-Step 'Cleaning temporary build dependencies and intermediates (signed outputs and logs are retained)'
    $cleanupPaths = @($Venv, $Work, $Spec, $PackageStage, $IconResource, $VersionFile, $IconRc, (Join-Path $BuildRoot 'downloads'))
    foreach ($cleanupPath in $cleanupPaths) {
        if (Test-Path -LiteralPath $cleanupPath) {
            try {
                Remove-Item -LiteralPath $cleanupPath -Recurse -Force -ErrorAction Stop
                Write-Host "Removed temporary build path: $cleanupPath" -ForegroundColor DarkGray
            } catch {
                Write-Warning "Could not remove temporary build path '$cleanupPath': $($_.Exception.Message)"
            }
        }
    }
} else {
    Write-Host 'Build cache retained because -KeepBuildCache was specified.' -ForegroundColor Yellow
}

Write-Step 'Build, signing, verification and packaging completed'
Get-Item -LiteralPath $ExePath, $DllPath, $PackagePath |
    Select-Object FullName, Length, LastWriteTime | Format-Table -AutoSize
if (-not $SkipSigning) {
    Write-Host 'EXE and DLL have valid Authenticode signatures on this Windows user profile.' -ForegroundColor Green
    Write-Host 'A self-signed development certificate is not publicly trusted; see README.txt.' -ForegroundColor Yellow
}
Write-Host "Release package: $PackagePath" -ForegroundColor Green
Write-Host "Desktop shortcut: $ShortcutPath" -ForegroundColor Green
Write-Host "Full build log: $BuildLog" -ForegroundColor Green
Write-Host 'The local development certificate is user-trusted only and does not create public publisher trust.' -ForegroundColor Yellow
try { Stop-Transcript | Out-Null } catch { }
