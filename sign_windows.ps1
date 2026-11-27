[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Paths,
    [string]$CertThumbprint = $env:DGSM_CERT_THUMBPRINT
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function ConvertTo-WindowsArgument([string]$Value) {
    if ($null -eq $Value) { $Value = '' }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) { $slashes++; continue }
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
        [int]$TimeoutSeconds = 180
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = (($Arguments | ForEach-Object { ConvertTo-WindowsArgument ([string]$_) }) -join ' ')
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

function Invoke-SignTool {
    param([string[]]$Arguments, [string]$Operation)
    $result = Invoke-CapturedProcess -FilePath $signTool.FullName -Arguments $Arguments -TimeoutSeconds 180
    if ($result.StdOut) { Write-Host $result.StdOut.TrimEnd() }
    if ($result.StdErr) { Write-Host $result.StdErr.TrimEnd() -ForegroundColor DarkYellow }
    if ($result.ExitCode -ne 0) {
        throw "SignTool $Operation failed (exit $($result.ExitCode)). $($result.StdErr.Trim()) $($result.StdOut.Trim())"
    }
    return $result
}
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$SigningDir = Join-Path $Root 'dist\windows\signing'
New-Item -ItemType Directory -Force -Path $SigningDir | Out-Null

$pf86 = ${env:ProgramFiles(x86)}
if (-not $pf86) { $pf86 = $env:ProgramFiles }
$sdkRoot = Join-Path $pf86 'Windows Kits\10\bin'
$signTool = Get-ChildItem -Path $sdkRoot -Filter signtool.exe -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '[\\/]x64[\\/]signtool\.exe$' } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $signTool) { throw 'signtool.exe not found. Run build_windows.ps1 to install Windows SDK Signing Tools automatically.' }

$Cert = $null
$CreatedDevelopmentCert = $false
$IsDevelopmentCertificate = $false
if (-not [string]::IsNullOrWhiteSpace($CertThumbprint)) {
    $normalizedThumbprint = ($CertThumbprint -replace '\s', '').ToUpperInvariant()
    $Cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object {
        $_.Thumbprint.ToUpperInvariant() -eq $normalizedThumbprint -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date)
    } | Select-Object -First 1
    if (-not $Cert) { throw "DGSM_CERT_THUMBPRINT $normalizedThumbprint was not found in CurrentUser\My with a valid private key." }
} else {
    $IsDevelopmentCertificate = $true
    $Subject = 'CN=Discord Global Status Monitor Local Development'
    $Cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object {
        $_.Subject -eq $Subject -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date)
    } | Sort-Object NotAfter -Descending | Select-Object -First 1
    if (-not $Cert) {
        $Cert = New-SelfSignedCertificate `
            -Subject $Subject `
            -Type CodeSigningCert `
            -CertStoreLocation Cert:\CurrentUser\My `
            -KeyAlgorithm RSA `
            -KeyLength 3072 `
            -HashAlgorithm SHA256 `
            -KeyExportPolicy NonExportable `
            -NotAfter (Get-Date).AddYears(2)
        $CreatedDevelopmentCert = $true
    }
    $CertThumbprint = $Cert.Thumbprint
    Write-Warning 'This is a self-signed development certificate. It does not establish a publicly trusted publisher identity or SmartScreen reputation.'
}

$CerPath = Join-Path $SigningDir 'DGSM-Development-CodeSigning.cer'
Export-Certificate -Cert $Cert -FilePath $CerPath -Force | Out-Null
if ($IsDevelopmentCertificate) {
    # Trust only this public certificate in the current user stores. The
    # private key remains non-exportable in CurrentUser\My.
    Import-Certificate -FilePath $CerPath -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    Import-Certificate -FilePath $CerPath -CertStoreLocation Cert:\CurrentUser\TrustedPublisher | Out-Null
    Write-Host 'Development certificate trusted for this Windows user only.' -ForegroundColor Yellow
}

$validPaths = @($Paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
if ($validPaths.Count -eq 0) { throw 'No existing files were supplied for signing.' }
$report = @(
    'Discord Global Status Monitor - Windows Authenticode signing report',
    "Date: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')",
    "Signer subject: $($Cert.Subject)",
    "Signer thumbprint: $($Cert.Thumbprint)",
    "Development certificate created in this run: $CreatedDevelopmentCert",
    "SignTool: $($signTool.FullName)",
    ''
)

foreach ($Path in $validPaths) {
    Write-Host "Signing: $Path" -ForegroundColor Cyan
    $signed = $false
    $timestampServers = @('http://timestamp.digicert.com', 'http://timestamp.sectigo.com')
    foreach ($timestampServer in $timestampServers) {
        try {
            Invoke-SignTool -Arguments @('sign', '/sha1', $Cert.Thumbprint, '/s', 'My', '/fd', 'SHA256', '/tr', $timestampServer, '/td', 'SHA256', $Path) -Operation 'timestamped signing' | Out-Null
            $signed = $true
            break
        } catch {
            Write-Warning "Timestamp/sign attempt failed ($timestampServer): $_"
        }
        # A timestamp failure can occasionally leave a partial signature. Clean
        # that attempt before retrying or signing without a timestamp.
        $partial = Get-AuthenticodeSignature -FilePath $Path
        if ($partial.Status -ne 'NotSigned') {
            Invoke-SignTool -Arguments @('remove', '/s', $Path) -Operation 'partial-signature removal' | Out-Null
        }
        Write-Warning "Timestamp server failed ($timestampServer); trying another timestamp service."
    }
    if (-not $signed) {
        Write-Warning 'Timestamp services were unavailable. Applying an Authenticode signature without a timestamp.'
        Invoke-SignTool -Arguments @('sign', '/sha1', $Cert.Thumbprint, '/s', 'My', '/fd', 'SHA256', $Path) -Operation 'signing without timestamp' | Out-Null
    }

    Invoke-SignTool -Arguments @('verify', '/pa', '/all', '/v', $Path) -Operation 'signature verification' | Out-Null
    $auth = Get-AuthenticodeSignature -FilePath $Path
    if ($auth.Status -ne 'Valid') {
        throw "Windows Authenticode verification failed for $Path : $($auth.Status) $($auth.StatusMessage)"
    }
    $hash = Get-FileHash -LiteralPath $Path -Algorithm SHA256
    $report += ("SIGNED: {0}" -f (Split-Path -Leaf $Path))
    $report += ("SHA256: {0}" -f $hash.Hash.ToLowerInvariant())
    $report += ("Status: {0}" -f $auth.Status)
    $report += ''
    Write-Host "Signature verified: $Path" -ForegroundColor Green
}

$report | Set-Content -LiteralPath (Join-Path $SigningDir 'signing-report.txt') -Encoding UTF8
Write-Host "Signing report: $(Join-Path $SigningDir 'signing-report.txt')" -ForegroundColor Green
