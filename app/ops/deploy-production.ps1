[CmdletBinding()]
param(
    [string]$EnvFile = ".env.production",
    [ValidateSet("Tailscale", "CloudflareTunnel")]
    [string]$PublishMode = "CloudflareTunnel",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$appRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $appRoot "compose.production.yaml"
if (-not [IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile = Join-Path $appRoot $EnvFile
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Missing $EnvFile. Copy .env.production.example and fill every required value."
}

$requiredKeys = @(
    "POSTGRES_PASSWORD",
    "AIDISON_ADMIN_USER",
    "AIDISON_ADMIN_PASSWORD_HASH",
    "AIDISON_SESSION_SECRET",
    "AIDISON_PUBLIC_ORIGIN",
    "DEEPSEEK_API_KEY"
)
if ($PublishMode -eq "CloudflareTunnel") {
    $requiredKeys += "CLOUDFLARE_TUNNEL_TOKEN"
}
$envText = Get-Content -Raw -LiteralPath $EnvFile
foreach ($key in $requiredKeys) {
    # Do not require a trailing newline: the final dotenv entry is valid at EOF.
    $pattern = "(?m)^$([regex]::Escape($key))=(?<value>[^\r\n]+)"
    if ($envText -notmatch $pattern) {
        throw "Required production setting is missing or still a placeholder: $key"
    }
    $value = $Matches["value"].Trim().Trim("'", '"')
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match "replace") {
        throw "Required production setting is missing or still a placeholder: $key"
    }
}

$postgresMatch = [regex]::Match($envText, "(?m)^POSTGRES_PASSWORD=(?<value>[^\r\n]+)")
$postgresPassword = $postgresMatch.Groups["value"].Value.Trim().Trim("'", '"')
if ($postgresPassword.Length -lt 32 -or $postgresPassword -notmatch '^[A-Za-z0-9_-]+$') {
    throw "POSTGRES_PASSWORD must contain at least 32 URL-safe random characters"
}

$hashMatch = [regex]::Match($envText, "(?m)^AIDISON_ADMIN_PASSWORD_HASH=(?<value>[^\r\n]+)")
$passwordHash = $hashMatch.Groups["value"].Value.Trim().Trim("'", '"')
if ($passwordHash -notmatch '^\$2[aby]\$\d{2}\$.{53}$') {
    throw "AIDISON_ADMIN_PASSWORD_HASH must be a valid bcrypt hash"
}

$sessionSecretMatch = [regex]::Match($envText, "(?m)^AIDISON_SESSION_SECRET=(?<value>[^\r\n]+)")
$sessionSecret = $sessionSecretMatch.Groups["value"].Value.Trim().Trim("'", '"')
if ($sessionSecret.Length -lt 43 -or $sessionSecret -notmatch '^[A-Za-z0-9_-]+$') {
    throw "AIDISON_SESSION_SECRET must contain at least 43 URL-safe random characters"
}

$originMatch = [regex]::Match($envText, "(?m)^AIDISON_PUBLIC_ORIGIN=(?<value>[^\r\n]+)")
$publicOrigin = $originMatch.Groups["value"].Value.Trim().Trim("'", '"')
if ($publicOrigin -notmatch '^https://[^/]+$') {
    throw "AIDISON_PUBLIC_ORIGIN must be one exact HTTPS origin without a trailing slash"
}

$composeArguments = @("--env-file", $EnvFile, "-f", $composeFile)
if ($PublishMode -eq "CloudflareTunnel") {
    $composeArguments += @("--profile", "cloudflare")
}

Write-Host "Validating production Compose configuration for $PublishMode"
& docker compose @composeArguments config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "docker compose config validation failed"
}

if (-not $SkipBuild) {
    Write-Host "Building pinned Aidison images"
    & docker compose @composeArguments build --pull
    if ($LASTEXITCODE -ne 0) {
        throw "production image build failed"
    }
}

Write-Host "Starting $PublishMode production stack"
& docker compose @composeArguments up -d --remove-orphans
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
            & docker compose @composeArguments ps
            exit 0
        }
    }
    catch {
        Start-Sleep -Seconds 3
    }
} while ((Get-Date) -lt $deadline)

& docker compose @composeArguments ps
throw "production edge did not become healthy before the deadline"
