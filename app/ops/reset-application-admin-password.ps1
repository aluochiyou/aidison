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
    if ([Text.Encoding]::UTF8.GetByteCount($password) -gt 72) {
        throw "Administrator password must not exceed bcrypt's 72-byte UTF-8 limit"
    }
    if ($password -cne $confirmation) {
        throw "The two administrator passwords do not match"
    }

    $hashInputPath = Join-Path (Split-Path -Parent $EnvFile) (".aidison-admin-password.{0}.tmp" -f [guid]::NewGuid())
    try {
        [IO.File]::WriteAllText($hashInputPath, "$password`n", [Text.UTF8Encoding]::new($false))
        $hashOutput = & docker run --rm `
            -v "${hashInputPath}:/run/secrets/aidison-admin-password:ro" `
            --entrypoint sh caddy:2.11.7-alpine `
            -c 'caddy hash-password --algorithm bcrypt --bcrypt-cost 14 < /run/secrets/aidison-admin-password'
        if ($LASTEXITCODE -ne 0) {
            throw "Caddy failed to hash the administrator password"
        }
    }
    finally {
        if (Test-Path -LiteralPath $hashInputPath) {
            Remove-Item -LiteralPath $hashInputPath -Force
        }
    }
    $hash = $hashOutput.Trim()
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

    $temporaryPath = "$EnvFile.$([guid]::NewGuid()).tmp"
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
    & docker compose --env-file $EnvFile -f $composeFile up -d --no-deps --force-recreate api
    if ($LASTEXITCODE -ne 0) {
        throw "API restart failed; the new password is stored but not active"
    }

    $adminUserLine = $rendered | Where-Object { $_ -match '^AIDISON_ADMIN_USER=' } | Select-Object -First 1
    $originLine = $rendered | Where-Object { $_ -match '^AIDISON_PUBLIC_ORIGIN=' } | Select-Object -First 1
    if (-not $adminUserLine -or -not $originLine) {
        throw "Administrator user or public origin is missing from $EnvFile"
    }
    $adminUser = ($adminUserLine -replace '^AIDISON_ADMIN_USER=', '').Trim().Trim("'", '"')
    $publicOrigin = ($originLine -replace '^AIDISON_PUBLIC_ORIGIN=', '').Trim().Trim("'", '"')
    $body = @{ username = $adminUser; password = $password } | ConvertTo-Json -Compress

    $verified = $false
    foreach ($attempt in 1..30) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing `
                -Uri "http://127.0.0.1:8080/api/auth/login" `
                -Method Post `
                -ContentType "application/json" `
                -Headers @{ Origin = $publicOrigin } `
                -Body $body `
                -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                $verified = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $verified) {
        throw "API restarted, but application login verification did not succeed"
    }

    Write-Host "Administrator password updated. Existing application sessions are invalidated."
    Write-Host "No secret values were printed."
}
finally {
    $password = $null
    $confirmation = $null
    $hash = $null
    $body = $null
}
