[CmdletBinding()]
param(
    [string]$EnvFile = ".env.production"
)

$ErrorActionPreference = "Stop"
$appRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $appRoot "compose.production.yaml"
if (-not [IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile = Join-Path $appRoot $EnvFile
}
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Missing $EnvFile."
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

$secureKey = Read-Host "Firecrawl API Key（不回显）" -AsSecureString
$apiKey = ConvertFrom-SecureValue $secureKey
try {
    if ($apiKey -notmatch '^fc-[A-Za-z0-9_-]{16,}$') {
        throw "Firecrawl API Key format is invalid"
    }

    $lines = @(Get-Content -LiteralPath $EnvFile)
    $keyUpdated = $false
    $providerUpdated = $false
    $rendered = @(
        foreach ($line in $lines) {
            if ($line -match '^FIRECRAWL_API_KEY=') {
                $keyUpdated = $true
                "FIRECRAWL_API_KEY=$apiKey"
            }
            elseif ($line -match '^AIDISON_WEB_SEARCH_PROVIDER=') {
                $providerUpdated = $true
                "AIDISON_WEB_SEARCH_PROVIDER=firecrawl"
            }
            else {
                $line
            }
        }
    )
    if (-not $keyUpdated) {
        $rendered += "FIRECRAWL_API_KEY=$apiKey"
    }
    if (-not $providerUpdated) {
        $rendered += "AIDISON_WEB_SEARCH_PROVIDER=firecrawl"
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

    & docker compose --env-file $EnvFile -f $composeFile up -d --no-deps --force-recreate worker
    if ($LASTEXITCODE -ne 0) {
        throw "Research worker restart failed; the key is stored but is not active"
    }
    & docker compose --env-file $EnvFile -f $composeFile exec -T worker python -c `
        "import os,sys;sys.exit(0 if os.getenv('FIRECRAWL_API_KEY') and os.getenv('AIDISON_WEB_SEARCH_PROVIDER') == 'firecrawl' else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Research worker did not activate Firecrawl search"
    }
    Write-Host "Firecrawl search configured and research worker restarted. No secret value was printed."
}
finally {
    $apiKey = $null
}
