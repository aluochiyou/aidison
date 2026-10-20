[CmdletBinding()]
param(
    [string]$EnvFile = ".env.production",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$composeFile = "compose.production.yaml"

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Missing $EnvFile. Copy .env.production.example and fill every required value."
}

$requiredKeys = @(
    "POSTGRES_PASSWORD",
    "AIDISON_ADMIN_USER",
    "AIDISON_ADMIN_PASSWORD_HASH",
    "CLOUDFLARE_TUNNEL_TOKEN",
    "DEEPSEEK_API_KEY"
)
$envText = Get-Content -Raw -LiteralPath $EnvFile
foreach ($key in $requiredKeys) {
    $pattern = "(?m)^$([regex]::Escape($key))=(?<value>[^\r\n]+)$"
    if ($envText -notmatch $pattern) {
        throw "Required production setting is missing or still a placeholder: $key"
    }
    $value = $Matches["value"].Trim().Trim("'", '"')
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match "replace") {
        throw "Required production setting is missing or still a placeholder: $key"
    }
}

$postgresMatch = [regex]::Match($envText, "(?m)^POSTGRES_PASSWORD=(?<value>[^\r\n]+)$")
$postgresPassword = $postgresMatch.Groups["value"].Value.Trim().Trim("'", '"')
if ($postgresPassword.Length -lt 32 -or $postgresPassword -notmatch '^[A-Za-z0-9_-]+$') {
    throw "POSTGRES_PASSWORD must contain at least 32 URL-safe random characters"
}

$hashMatch = [regex]::Match($envText, "(?m)^AIDISON_ADMIN_PASSWORD_HASH=(?<value>[^\r\n]+)$")
$passwordHash = $hashMatch.Groups["value"].Value.Trim().Trim("'", '"')
if ($passwordHash -notmatch '^\$2[aby]\$\d{2}\$.{53}$') {
    throw "AIDISON_ADMIN_PASSWORD_HASH must be a Caddy-compatible bcrypt hash"
}

Write-Host "Validating production Compose configuration"
& docker compose --env-file $EnvFile -f $composeFile config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "docker compose config validation failed"
}

if (-not $SkipBuild) {
    Write-Host "Building pinned Aidison images"
    & docker compose --env-file $EnvFile -f $composeFile build --pull
    if ($LASTEXITCODE -ne 0) {
        throw "production image build failed"
    }
}

Write-Host "Starting production stack"
& docker compose --env-file $EnvFile -f $composeFile up -d --remove-orphans
if ($LASTEXITCODE -ne 0) {
    throw "production stack startup failed"
}

$edgePort = 8080
if ($envText -match "(?m)^AIDISON_EDGE_PORT=(\d+)$") {
    $edgePort = [int]$Matches[1]
}

$deadline = (Get-Date).AddMinutes(4)
do {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$edgePort/edge-health" -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            Write-Host "Aidison production edge is healthy on loopback port $edgePort"
            & docker compose --env-file $EnvFile -f $composeFile ps
            exit 0
        }
    }
    catch {
        Start-Sleep -Seconds 3
    }
} while ((Get-Date) -lt $deadline)

& docker compose --env-file $EnvFile -f $composeFile ps
throw "production edge did not become healthy before the deadline"
