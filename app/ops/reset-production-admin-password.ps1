[CmdletBinding()]
param(
    [string]$EnvFile = ".env.production"
)

$ErrorActionPreference = "Stop"
$appRoot = Split-Path -Parent $PSScriptRoot
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

$firstSecure = Read-Host "新管理员密码（至少 16 个字符，不回显）" -AsSecureString
$secondSecure = Read-Host "再次输入新管理员密码" -AsSecureString
$password = ConvertFrom-SecureValue $firstSecure
$confirmation = ConvertFrom-SecureValue $secondSecure
try {
    if ($password.Length -lt 16) {
        throw "Administrator password must contain at least 16 characters"
    }
    if ($password -cne $confirmation) {
        throw "The two administrator passwords do not match"
    }

    $hashOutput = $password | & docker run --rm -i caddy:2.11.7-alpine `
        caddy hash-password --algorithm bcrypt --bcrypt-cost 14
    if ($LASTEXITCODE -ne 0) {
        throw "Caddy failed to hash the administrator password"
    }
    $hash = ($hashOutput | Select-Object -Last 1).Trim()
    if ($hash -notmatch '^\$2[aby]\$14\$.{53}$') {
        throw "Caddy returned an invalid bcrypt hash"
    }

    $updated = $false
    $lines = Get-Content -LiteralPath $EnvFile
    $rendered = foreach ($line in $lines) {
        if ($line -match '^AIDISON_ADMIN_PASSWORD_HASH=') {
            $updated = $true
            "AIDISON_ADMIN_PASSWORD_HASH='$hash'"
        }
        else {
            $line
        }
    }
    if (-not $updated) {
        throw "AIDISON_ADMIN_PASSWORD_HASH is missing from $EnvFile"
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

    $composeFile = Join-Path $appRoot "compose.production.yaml"
    & docker compose --env-file $EnvFile -f $composeFile up -d --no-deps --force-recreate gateway
    if ($LASTEXITCODE -ne 0) {
        throw "Gateway restart failed; the new password is stored but not active until gateway starts successfully"
    }

    $adminUserLine = $lines | Where-Object { $_ -match '^AIDISON_ADMIN_USER=' } | Select-Object -First 1
    if (-not $adminUserLine) {
        throw "AIDISON_ADMIN_USER is missing from $EnvFile"
    }
    $adminUser = ($adminUserLine -replace '^AIDISON_ADMIN_USER=', '').Trim().Trim("'", '"')
    if ([string]::IsNullOrWhiteSpace($adminUser)) {
        throw "AIDISON_ADMIN_USER is empty in $EnvFile"
    }

    $client = [Net.Http.HttpClient]::new()
    try {
        $credential = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${adminUser}:$password"))
        $client.DefaultRequestHeaders.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new("Basic", $credential)
        $verified = $false
        foreach ($attempt in 1..20) {
            try {
                $response = $client.GetAsync("http://127.0.0.1:8080/").GetAwaiter().GetResult()
                if ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 400) {
                    $verified = $true
                    break
                }
            }
            catch {
                # The gateway may not have completed its health check yet.
            }
            Start-Sleep -Milliseconds 500
        }
        if (-not $verified) {
            throw "Gateway restarted, but local Basic Auth verification did not succeed. Check gateway logs before using the public URL."
        }
    }
    finally {
        if ($null -ne $client) {
            $client.Dispose()
        }
    }

    Write-Host "Administrator password updated and verified locally. No secret values were printed."
}
finally {
    $password = $null
    $confirmation = $null
    $hash = $null
}
