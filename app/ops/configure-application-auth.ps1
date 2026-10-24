[CmdletBinding()]
param(
    [string]$EnvFile = ".env.production",
    [string]$PublicOrigin = "https://aidison.xyz",
    [int]$SessionTtlSeconds = 28800,
    [switch]$RotateSessionSecret
)

$ErrorActionPreference = "Stop"
$appRoot = Split-Path -Parent $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile = Join-Path $appRoot $EnvFile
}
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Missing production environment file: $EnvFile"
}
if ($PublicOrigin -notmatch '^https://[^/]+$') {
    throw "PublicOrigin must be one exact HTTPS origin without a trailing slash"
}
if ($SessionTtlSeconds -lt 300 -or $SessionTtlSeconds -gt 604800) {
    throw "SessionTtlSeconds must be between 300 and 604800"
}

function New-UrlSafeSecret {
    $bytes = [byte[]]::new(48)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

$lines = [Collections.Generic.List[string]]::new()
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    $lines.Add($line)
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value,
        [switch]$OnlyWhenMissing
    )

    for ($index = 0; $index -lt $lines.Count; $index += 1) {
        if ($lines[$index] -match "^$([regex]::Escape($Key))=") {
            if (-not $OnlyWhenMissing) {
                $lines[$index] = "$Key=$Value"
            }
            return
        }
    }
    $lines.Add("$Key=$Value")
}

Set-EnvValue -Key "AIDISON_PUBLIC_ORIGIN" -Value $PublicOrigin
Set-EnvValue -Key "AIDISON_SESSION_TTL_SECONDS" -Value ([string]$SessionTtlSeconds)
if ($RotateSessionSecret) {
    Set-EnvValue -Key "AIDISON_SESSION_SECRET" -Value (New-UrlSafeSecret)
}
else {
    Set-EnvValue -Key "AIDISON_SESSION_SECRET" -Value (New-UrlSafeSecret) -OnlyWhenMissing
}

$temporaryPath = "$EnvFile.$([guid]::NewGuid()).tmp"
try {
    [IO.File]::WriteAllLines($temporaryPath, $lines, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $EnvFile -Force
}
finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

Write-Host "Application authentication settings updated in $EnvFile"
Write-Host "No secret value was printed. Recreate the API service to apply the change."
