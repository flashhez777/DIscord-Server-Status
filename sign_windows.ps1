param(
    [Parameter(Mandatory = $true)]
    [string[]]$Paths,
    [string]$CertThumbprint = $env:DGSM_CERT_THUMBPRINT,
    [switch]$TrustSelfSignedForCurrentUser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$SigningDir = Join-Path $Root "dist\windows\signing"
New-Item -ItemType Directory -Force -Path $SigningDir | Out-Null

$ProgramFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
if ([string]::IsNullOrWhiteSpace($ProgramFilesX86)) { $ProgramFilesX86 = $env:ProgramFiles }
$SdkRoot = Join-Path $ProgramFilesX86 "Windows Kits\10\bin"
$SignTool = Get-ChildItem -Path $SdkRoot -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $SignTool) {
    throw "signtool.exe not found. Install the Windows 10/11 SDK (Signing Tools for Desktop Apps)."
}

if ([string]::IsNullOrWhiteSpace($CertThumbprint)) {
    $Subject = "CN=Discord Global Status Monitor Local Development"
    $Cert = Get-ChildItem Cert:\CurrentUser\My |
        Where-Object { $_.Subject -eq $Subject -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1
    if (-not $Cert) {
        $Cert = New-SelfSignedCertificate `
            -Subject $Subject `
            -Type CodeSigningCert `
            -CertStoreLocation Cert:\CurrentUser\My `
            -KeyAlgorithm RSA `
            -KeyLength 3072 `
            -HashAlgorithm SHA256 `
            -NotAfter (Get-Date).AddYears(2)
    }
    $CertThumbprint = $Cert.Thumbprint
    $CerPath = Join-Path $SigningDir "DGSM-Development-CodeSigning.cer"
    Export-Certificate -Cert $Cert -FilePath $CerPath -Force | Out-Null
    Write-Host "Created/reused local development code-signing certificate: $CerPath"
    Write-Warning "This is self-signed and is not publicly trusted. For public distribution use a CA-issued code-signing certificate."
    if ($TrustSelfSignedForCurrentUser) {
        Import-Certificate -FilePath $CerPath -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
        Import-Certificate -FilePath $CerPath -CertStoreLocation Cert:\CurrentUser\TrustedPublisher | Out-Null
        Write-Host "Self-signed certificate trusted for this Windows user only." -ForegroundColor Yellow
    }
} else {
    $Cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Thumbprint -eq $CertThumbprint } | Select-Object -First 1
    if (-not $Cert -or -not $Cert.HasPrivateKey) {
        throw "Certificate thumbprint $CertThumbprint was not found in CurrentUser\My with a private key."
    }
}

foreach ($Path in $Paths) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-Warning "Skipping missing output: $Path"
        continue
    }
    Write-Host "Signing $Path"
    & $SignTool.FullName sign /sha1 $CertThumbprint /s My /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $Path
    if ($LASTEXITCODE -ne 0) { throw "SignTool failed for $Path (exit $LASTEXITCODE)" }
    Write-Host "Signed: $Path" -ForegroundColor Green
}
