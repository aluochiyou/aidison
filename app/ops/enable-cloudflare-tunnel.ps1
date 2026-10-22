[CmdletBinding()]
param(
    [string]$EnvFile = ".env.production",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$appRoot = Split-Path -Parent $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile = Join-Path $appRoot $EnvFile
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Missing $EnvFile. Deploy the private production stack first."
}

function ConvertFrom-SecureValue {
    param([Parameter(Mandatory = $true)][securestring]$Value)

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

$secureToken = Read-Host "Cloudflare Tunnel Token（不回显）" -AsSecureString
$tunnelToken = ConvertFrom-SecureValue $secureToken
try {
    if ([string]::IsNullOrWhiteSpace($tunnelToken)) {
        throw "Cloudflare Tunnel Token is required"
    }

    $lines = Get-Content -LiteralPath $EnvFile
    $updated = $false
    $rendered = foreach ($line in $lines) {
        if ($line -match '^CLOUDFLARE_TUNNEL_TOKEN=') {
            $updated = $true
            "CLOUDFLARE_TUNNEL_TOKEN=$tunnelToken"
        }
        else {
            $line
        }
    }
    if (-not $updated) {
        $rendered += "CLOUDFLARE_TUNNEL_TOKEN=$tunnelToken"
    }

    $directory = Split-Path -Parent $EnvFile
    $temporaryPath = Join-Path $directory (".{0}.{1}.tmp" -f ([IO.Path]::GetFileName($EnvFile)), [guid]::NewGuid())
    try {
        [IO.File]::WriteAllLines($temporaryPath, $rendered, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporaryPath -Destination $EnvFile -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }

    & (Join-Path $PSScriptRoot "deploy-production.ps1") `
        -EnvFile $EnvFile `
        -PublishMode CloudflareTunnel `
        -SkipBuild:$SkipBuild
    if ($LASTEXITCODE -ne 0) {
        throw "Cloudflare Tunnel deployment failed"
    }
}
finally {
    $tunnelToken = $null
}
