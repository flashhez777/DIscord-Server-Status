[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Paths,
    [string]$PfxPath = $env:DGSM_PFX_PATH,
    [string]$CertThumbprint = $env:DGSM_CERT_THUMBPRINT,
    [string[]]$TimestampUrls = @('http://timestamp.digicert.com', 'http://timestamp.sectigo.com'),
    [string]$SdkBuildToolsVersion = '10.0.28000.2705'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$SigningDir = Join-Path $Root 'dist\windows\signing'
New-Item -ItemType Directory -Force -Path $SigningDir | Out-Null

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
    # Use ProcessStartInfo so native stderr is captured as text under both
    # Windows PowerShell 5.1 and PowerShell 7 (no NativeCommandError surprises).
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
    $result = Invoke-CapturedProcess -FilePath $script:SignTool.FullName -Arguments $Arguments -TimeoutSeconds 300
    if ($result.StdOut) { Write-Host $result.StdOut.TrimEnd() }
    if ($result.StdErr) { Write-Host $result.StdErr.TrimEnd() -ForegroundColor DarkYellow }
    if ($result.ExitCode -ne 0) {
        $detail = (@($result.StdErr, $result.StdOut) | Where-Object { $_ }) -join ' '
        throw "SignTool $Operation failed (exit $($result.ExitCode)). $detail"
    }
    return $result
}

function Test-MicrosoftAuthenticodeFile([string]$FilePath) {
    try {
        $signature = Get-AuthenticodeSignature -FilePath $FilePath
        return ($signature.Status -eq 'Valid' -and
            $signature.SignerCertificate -and
            $signature.SignerCertificate.Subject -match 'Microsoft Corporation')
    } catch {
        return $false
    }
}

function Find-InstalledSignTool {
    $roots = @()
    $pf86 = ${env:ProgramFiles(x86)}
    if ($pf86) { $roots += (Join-Path $pf86 'Windows Kits\10\bin') }
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles 'Windows Kits\10\bin') }
    $candidates = foreach ($rootPath in ($roots | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $rootPath -PathType Container) {
            Get-ChildItem -LiteralPath $rootPath -Filter 'signtool.exe' -File -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match '[\\/]x64[\\/]signtool\.exe$' }
        }
    }
    foreach ($candidate in ($candidates | Sort-Object FullName -Descending)) {
        if (Test-MicrosoftAuthenticodeFile $candidate.FullName) { return $candidate }
        Write-Warning "Ignoring SignTool without a valid Microsoft Authenticode signature: $($candidate.FullName)"
    }
    return $null
}

function Get-SignTool {
    $installed = Find-InstalledSignTool
    if ($installed) {
        Write-Host "Using installed Microsoft SignTool: $($installed.FullName)" -ForegroundColor Green
        return $installed
    }

    if ([string]::IsNullOrWhiteSpace($SdkBuildToolsVersion)) {
        throw 'SdkBuildToolsVersion cannot be empty.'
    }
    if ($PSVersionTable.PSVersion.Major -le 5) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }

    $script:SignToolTempRoot = Join-Path $env:TEMP ('DGSM-SignTool-' + [Guid]::NewGuid().ToString('N'))
    $extractDir = Join-Path $script:SignToolTempRoot 'extracted'
    $nupkgPath = Join-Path $script:SignToolTempRoot 'Microsoft.Windows.SDK.BuildTools.nupkg'
    $zipPath = Join-Path $script:SignToolTempRoot 'Microsoft.Windows.SDK.BuildTools.zip'
    New-Item -ItemType Directory -Force -Path $script:SignToolTempRoot, $extractDir | Out-Null

    $packageUri = "https://www.nuget.org/api/v2/package/Microsoft.Windows.SDK.BuildTools/$SdkBuildToolsVersion"
    Write-Host "SignTool is not installed. Downloading Microsoft's SDK BuildTools NuGet package $SdkBuildToolsVersion (no full SDK installation)..." -ForegroundColor Yellow
    $oldProgressPreference = $ProgressPreference
    try {
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $packageUri -OutFile $nupkgPath -UseBasicParsing -TimeoutSec 900 -ErrorAction Stop
    } finally {
        $ProgressPreference = $oldProgressPreference
    }
    if (-not (Test-Path -LiteralPath $nupkgPath -PathType Leaf)) {
        throw "NuGet did not download Microsoft's Windows SDK BuildTools package from $packageUri"
    }
    if ((Get-Item -LiteralPath $nupkgPath).Length -lt 1MB) {
        throw 'The downloaded Windows SDK BuildTools package is unexpectedly small; refusing to use it.'
    }

    # NuGet .nupkg is a ZIP container; copy to .zip for Windows PowerShell 5.1.
    Copy-Item -LiteralPath $nupkgPath -Destination $zipPath -Force
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
    $candidates = Get-ChildItem -LiteralPath $extractDir -Filter 'signtool.exe' -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '[\\/]x64[\\/]signtool\.exe$' } |
        Sort-Object {
            $sdkVersionDirectory = Split-Path (Split-Path $_.FullName -Parent) -Leaf
            try { [Version]$sdkVersionDirectory } catch { [Version]'0.0' }
        } -Descending

    foreach ($candidate in $candidates) {
        if (Test-MicrosoftAuthenticodeFile $candidate.FullName) {
            Write-Host "Using verified Microsoft SignTool from temporary NuGet extraction: $($candidate.FullName)" -ForegroundColor Green
            return $candidate
        }
        $signature = Get-AuthenticodeSignature -FilePath $candidate.FullName
        $subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
        Write-Warning "Rejected SignTool candidate: $($candidate.FullName) (status: $($signature.Status); signer: $subject)"
    }
    throw "Downloaded Microsoft SDK BuildTools $SdkBuildToolsVersion, but no x64 signtool.exe with a valid Microsoft Authenticode signature was found."
}

function Get-DevelopmentCertificate {
    $subject = 'CN=Discord Global Status Monitor Local Development Signing'
    $certificate = Get-ChildItem Cert:\CurrentUser\My | Where-Object {
        $_.Subject -eq $subject -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date)
    } | Sort-Object NotAfter -Descending | Select-Object -First 1

    if (-not $certificate) {
        Write-Host 'Creating reusable user-scoped local code-signing certificate with .NET/CryptoAPI...' -ForegroundColor Yellow

        # Avoid New-SelfSignedCertificate here. On some hosted Windows images
        # that cmdlet can block inside the certificate provider indefinitely.
        # Generate the key with the .NET CryptoAPI RSA provider, create an
        # explicit code-signing certificate, then persist its private key in
        # the current user's certificate store via a short-lived in-memory PFX.
        $rsa = $null
        $selfSigned = $null
        $imported = $null
        $store = $null
        $pfxBytes = $null
        $temporaryPassword = ([Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N'))

        try {
            $rsa = New-Object -TypeName System.Security.Cryptography.RSACryptoServiceProvider -ArgumentList 3072
            $rsa.PersistKeyInCsp = $false
            if ($rsa.KeySize -ne 3072) {
                throw "The cryptographic provider returned an unexpected RSA key size: $($rsa.KeySize)."
            }

            $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
                $subject,
                $rsa,
                [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
            )

            $basicConstraints = [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new(
                $false, $false, 0, $true
            )
            $keyUsage = [System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
                [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature,
                $true
            )
            $ekuOids = New-Object System.Security.Cryptography.OidCollection
            [void]$ekuOids.Add([System.Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3', 'Code Signing'))
            $enhancedKeyUsage = [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new(
                $ekuOids, $false
            )
            $subjectKeyIdentifier = [System.Security.Cryptography.X509Certificates.X509SubjectKeyIdentifierExtension]::new(
                $request.PublicKey, $false
            )

            [void]$request.CertificateExtensions.Add($basicConstraints)
            [void]$request.CertificateExtensions.Add($keyUsage)
            [void]$request.CertificateExtensions.Add($enhancedKeyUsage)
            [void]$request.CertificateExtensions.Add($subjectKeyIdentifier)

            $notBefore = [DateTimeOffset]::UtcNow.AddMinutes(-5)
            $notAfter = [DateTimeOffset]::UtcNow.AddYears(3)
            $selfSigned = $request.CreateSelfSigned($notBefore, $notAfter)
            $pfxBytes = $selfSigned.Export(
                [System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx,
                $temporaryPassword
            )

            $keyStorageFlags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::UserKeySet -bor
                [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::PersistKeySet
            $imported = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
                $pfxBytes, $temporaryPassword, $keyStorageFlags
            )

            $store = [System.Security.Cryptography.X509Certificates.X509Store]::new(
                [System.Security.Cryptography.X509Certificates.StoreName]::My,
                [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
            )
            $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
            [void]$store.Add($imported)
            $store.Close()

            $createdThumbprint = $imported.Thumbprint
        } catch {
            throw "Could not create the local DGSM code-signing certificate with the .NET CryptoAPI provider: $($_.Exception.Message)"
        } finally {
            if ($store) {
                try { $store.Close() } catch { }
                $store.Dispose()
            }
            if ($imported) { $imported.Dispose() }
            if ($selfSigned) { $selfSigned.Dispose() }
            if ($rsa) { $rsa.Dispose() }
            if ($pfxBytes) { [Array]::Clear($pfxBytes, 0, $pfxBytes.Length) }
            $temporaryPassword = $null
        }

        $certificate = Get-ChildItem Cert:\CurrentUser\My | Where-Object {
            $_.Thumbprint -eq $createdThumbprint -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date)
        } | Select-Object -First 1
    }

    if (-not $certificate -or -not $certificate.HasPrivateKey) {
        throw 'Could not create or locate a usable local DGSM code-signing certificate.'
    }
    return $certificate
}

$script:SignTool = $null
$script:SignToolTempRoot = $null
$securePassword = $null
$bstr = [IntPtr]::Zero
$pfxPassword = $null
$signingMode = $null
$certificate = $null
$report = New-Object 'System.Collections.Generic.List[string]'

try {
    $validPaths = @($Paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | ForEach-Object { (Resolve-Path -LiteralPath $_).Path })
    if ($validPaths.Count -eq 0) { throw 'No existing files were supplied for signing.' }
    if ($validPaths.Count -ne $Paths.Count) { throw 'One or more requested signing artifacts do not exist.' }

    $script:SignTool = Get-SignTool

    if (-not [string]::IsNullOrWhiteSpace($PfxPath)) {
        if (-not (Test-Path -LiteralPath $PfxPath -PathType Leaf)) { throw "PFX certificate not found: $PfxPath" }
        if ([string]::IsNullOrWhiteSpace($env:DGSM_PFX_PASSWORD)) {
            $securePassword = Read-Host 'Enter the DGSM PFX password' -AsSecureString
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
            $pfxPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        } else {
            $pfxPassword = $env:DGSM_PFX_PASSWORD
        }
        $signingMode = 'External PFX'
        Write-Host 'Using the supplied publisher PFX certificate.' -ForegroundColor Yellow
    } elseif (-not [string]::IsNullOrWhiteSpace($CertThumbprint)) {
        $normalized = ($CertThumbprint -replace '\s', '').ToUpperInvariant()
        $certificate = Get-ChildItem Cert:\CurrentUser\My | Where-Object {
            $_.Thumbprint.ToUpperInvariant() -eq $normalized -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date)
        } | Select-Object -First 1
        if (-not $certificate) { throw "DGSM_CERT_THUMBPRINT $normalized was not found in CurrentUser\My with a valid private key." }
        $signingMode = 'Existing CurrentUser certificate store'
    } else {
        $certificate = Get-DevelopmentCertificate
        $signingMode = 'Local self-signed development certificate (CurrentUser trust only)'
    }

    $thumbprint = if ($certificate) { $certificate.Thumbprint } else { '' }
    $cerPath = Join-Path $SigningDir 'DGSM-Development-CodeSigning.cer'
    if ($certificate) {
        Export-Certificate -Cert $certificate -FilePath $cerPath -Force | Out-Null
        # Same local-test trust policy as the Destiny 2 Damage Tracker builder:
        # trust only in the current user's Root and TrustedPublisher stores.
        Import-Certificate -FilePath $cerPath -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
        Import-Certificate -FilePath $cerPath -CertStoreLocation Cert:\CurrentUser\TrustedPublisher | Out-Null
        Set-Content -LiteralPath (Join-Path $SigningDir 'LOCAL_SIGNING_CERT_THUMBPRINT.txt') -Value $certificate.Thumbprint -Encoding ASCII
        Write-Host "Signing certificate: $($certificate.Subject) [$($certificate.Thumbprint)]" -ForegroundColor Green
    } else {
        Remove-Item -LiteralPath $cerPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $SigningDir 'LOCAL_SIGNING_CERT_THUMBPRINT.txt') -Force -ErrorAction SilentlyContinue
    }

    $report.Add('Discord Global Status Monitor - Windows Authenticode signing report')
    $report.Add("Date: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')")
    $report.Add("Signing mode: $signingMode")
    $report.Add("SignTool: $($script:SignTool.FullName)")
    $report.Add("Certificate subject: $(if ($certificate) { $certificate.Subject } else { 'Read from supplied PFX signature' })")
    $report.Add("Certificate thumbprint: $(if ($certificate) { $certificate.Thumbprint } else { 'External PFX' })")
    $report.Add('')

    foreach ($path in $validPaths) {
        Write-Host "Signing: $path" -ForegroundColor Cyan
        $signed = $false
        $attemptErrors = New-Object 'System.Collections.Generic.List[string]'
        foreach ($timestampUrl in ($TimestampUrls | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
            $signArgs = @('sign', '/fd', 'SHA256', '/tr', $timestampUrl, '/td', 'SHA256')
            if ($PfxPath) {
                $signArgs += @('/f', $PfxPath, '/p', $pfxPassword)
            } else {
                $signArgs += @('/s', 'My', '/sha1', $certificate.Thumbprint)
            }
            $signArgs += $path
            try {
                Invoke-SignTool -Arguments $signArgs -Operation "timestamped signing ($timestampUrl)" | Out-Null
                $signed = $true
                $report.Add("Timestamp: $timestampUrl")
                break
            } catch {
                $attemptErrors.Add("$timestampUrl : $($_.Exception.Message)")
                Write-Warning "Timestamp/sign attempt failed for $path using ${timestampUrl}: $($_.Exception.Message)"
                $partial = Get-AuthenticodeSignature -FilePath $path
                if ($partial.Status -ne 'NotSigned') {
                    try { Invoke-SignTool -Arguments @('remove', '/s', $path) -Operation 'partial signature removal' | Out-Null }
                    catch { Write-Warning "Could not remove partial signature before retry: $_" }
                }
            }
        }

        if (-not $signed) {
            foreach ($attemptError in $attemptErrors) { $report.Add("Timestamp attempt failed: $attemptError") }
            Write-Warning "Timestamp services unavailable for $(Split-Path -Leaf $path); signing without a timestamp."
            $signArgs = @('sign', '/fd', 'SHA256')
            if ($PfxPath) {
                $signArgs += @('/f', $PfxPath, '/p', $pfxPassword)
            } else {
                $signArgs += @('/s', 'My', '/sha1', $certificate.Thumbprint)
            }
            $signArgs += $path
            Invoke-SignTool -Arguments $signArgs -Operation 'signing without timestamp' | Out-Null
            $report.Add('Timestamp: Not available; signed without timestamp')
        }

        Invoke-SignTool -Arguments @('verify', '/pa', '/all', '/v', $path) -Operation 'signature verification' | Out-Null
        $auth = Get-AuthenticodeSignature -FilePath $path
        if ($auth.Status -ne 'Valid') {
            throw "Windows Authenticode verification failed for '$path': $($auth.Status) $($auth.StatusMessage)"
        }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        $signerSubject = if ($auth.SignerCertificate) { $auth.SignerCertificate.Subject } else { 'Unknown' }
        $report.Add("SIGNED: $(Split-Path -Leaf $path)")
        $report.Add("SHA256: $hash")
        $report.Add("Status: $($auth.Status)")
        $report.Add("Signer: $signerSubject")
        $report.Add('')
        Write-Host "Authenticode signature verified: $path" -ForegroundColor Green
    }

    $report | Set-Content -LiteralPath (Join-Path $SigningDir 'signing-report.txt') -Encoding UTF8
    Write-Host "Signing report: $(Join-Path $SigningDir 'signing-report.txt')" -ForegroundColor Green
    if ($signingMode -like 'Local self-signed*') {
        Write-Warning 'The local self-signed certificate is trusted only for this Windows user. It does not create public publisher identity or SmartScreen reputation.'
    }
} finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $pfxPassword = $null
    $securePassword = $null
    if ($script:SignToolTempRoot -and (Test-Path -LiteralPath $script:SignToolTempRoot)) {
        try {
            Remove-Item -LiteralPath $script:SignToolTempRoot -Recurse -Force -ErrorAction Stop
            Write-Host 'Temporary Microsoft SDK BuildTools NuGet package and extracted SignTool files removed.' -ForegroundColor Green
        } catch {
            Write-Warning "Could not fully remove temporary SignTool files at '$($script:SignToolTempRoot)': $($_.Exception.Message)"
        }
    }
}
