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

$secureKey = Read-Host "DashScope API Key（不回显）" -AsSecureString
$apiKey = ConvertFrom-SecureValue $secureKey
try {
    if ($apiKey -notmatch '^sk-[A-Za-z0-9_-]{16,}$') {
        throw "DashScope API Key format is invalid"
    }

    $lines = @(Get-Content -LiteralPath $EnvFile)
    $updated = $false
    $rendered = @(
        foreach ($line in $lines) {
            if ($line -match '^DASHSCOPE_API_KEY=') {
                $updated = $true
                "DASHSCOPE_API_KEY=$apiKey"
            }
            else {
                $line
            }
        }
    )
    if (-not $updated) {
        $rendered += "DASHSCOPE_API_KEY=$apiKey"
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

    & docker compose --env-file $EnvFile -f $composeFile up -d --no-deps --force-recreate module-visual-worker
    if ($LASTEXITCODE -ne 0) {
        throw "Module visual worker restart failed; the key is stored but is not active"
    }
    & docker compose --env-file $EnvFile -f $composeFile exec -T module-visual-worker `
        python -c "import os,sys;sys.exit(0 if os.getenv('DASHSCOPE_API_KEY') else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Module visual worker started without DASHSCOPE_API_KEY"
    }
    Write-Host "Module visual provider configured and worker restarted. No secret value was printed."
}
finally {
    $apiKey = $null
}
