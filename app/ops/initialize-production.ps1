[CmdletBinding()]
param(
    [string]$EnvFile = ".env.production",
    [string]$SourceEnvFile = ".env",
    [string]$AdminUser = "aidison",
    [ValidateSet("CloudflareAccess", "OriginBasicAuth")]
    [string]$AccessMode = "CloudflareAccess",
    [ValidateSet("Tailscale", "CloudflareTunnel")]
    [string]$PublishMode = "CloudflareTunnel",
    [switch]$Deploy,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$appRoot = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $appRoot ".env.production.example"

function Resolve-AppPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $appRoot $Path))
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

function Read-ConfirmedPassword {
    $firstSecure = Read-Host "Aidison 管理员密码（至少 16 个字符，不回显）" -AsSecureString
    $secondSecure = Read-Host "再次输入管理员密码" -AsSecureString
    $first = ConvertFrom-SecureValue $firstSecure
    $second = ConvertFrom-SecureValue $secondSecure
    try {
        if ($first.Length -lt 16) {
            throw "AIDISON admin password must contain at least 16 characters"
        }
        if ($first -cne $second) {
            throw "The two administrator passwords do not match"
        }
        return $first
    }
    finally {
        $second = $null
    }
}

function Read-SecretText {
    param([Parameter(Mandatory = $true)][string]$Prompt)

    $secureValue = Read-Host $Prompt -AsSecureString
    return ConvertFrom-SecureValue $secureValue
}

function Confirm-CloudflareAccessGuard {
    $confirmation = Read-Host "确认已为该 hostname 配置 Cloudflare Access：仅允许你的精确邮箱地址（输入 ACCESS_READY 继续）"
    if ($confirmation -cne "ACCESS_READY") {
        throw "Production initialization requires a single-owner Cloudflare Access Allow policy before the Tunnel token is accepted"
    }
}

function Read-EnvValues {
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -notmatch '^\s*(?<key>[A-Za-z_][A-Za-z0-9_]*)=(?<value>.*)$') {
            continue
        }
        $value = $Matches["value"].Trim()
        if ($value.Length -ge 2) {
            $singleQuoted = $value.StartsWith("'") -and $value.EndsWith("'")
            $doubleQuoted = $value.StartsWith('"') -and $value.EndsWith('"')
            if ($singleQuoted -or $doubleQuoted) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $values[$Matches["key"]] = $value
        }
    }
    return $values
}

function New-UrlSafeSecret {
    param([int]$ByteCount = 48)

    $bytes = [byte[]]::new($ByteCount)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Set-EnvTemplateValues {
    param(
        # Production dotenv templates intentionally contain blank separator lines.
        [Parameter(Mandatory = $true)][AllowEmptyString()][string[]]$TemplateLines,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    $seen = @{}
    $result = foreach ($line in $TemplateLines) {
        if ($line -match '^(?<key>[A-Za-z_][A-Za-z0-9_]*)=') {
            $key = $Matches["key"]
            if ($Values.ContainsKey($key)) {
                $seen[$key] = $true
                "$key=$($Values[$key])"
                continue
            }
        }
        $line
    }
    foreach ($key in $Values.Keys) {
        if (-not $seen.ContainsKey($key)) {
            $result += "$key=$($Values[$key])"
        }
    }
    return $result
}

$targetPath = Resolve-AppPath $EnvFile
$sourcePath = Resolve-AppPath $SourceEnvFile
if (-not (Test-Path -LiteralPath $examplePath -PathType Leaf)) {
    throw "Missing production template: $examplePath"
}
if (Test-Path -LiteralPath $targetPath) {
    throw "$targetPath already exists; initialization will not overwrite it"
}
if ($AdminUser -notmatch '^[A-Za-z0-9._-]{1,64}$') {
    throw "AdminUser must contain only letters, digits, dot, underscore, or hyphen"
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is unavailable"
}

$adminPassword = Read-ConfirmedPassword
try {
    $hashOutput = $adminPassword | & docker run --rm -i caddy:2.11.7-alpine `
        caddy hash-password --algorithm bcrypt --bcrypt-cost 14
    if ($LASTEXITCODE -ne 0) {
        throw "Caddy failed to hash the administrator password"
    }
    $adminHash = ($hashOutput | Select-Object -Last 1).Trim()
}
finally {
    $adminPassword = $null
}
if ($adminHash -notmatch '^\$2[aby]\$14\$.{53}$') {
    throw "Caddy returned an invalid bcrypt hash"
}

$tunnelToken = $null
if ($PublishMode -eq "CloudflareTunnel") {
    $tunnelToken = Read-SecretText "Cloudflare Tunnel Token（不回显）"
    if ([string]::IsNullOrWhiteSpace($tunnelToken)) {
        throw "Cloudflare Tunnel Token is required"
    }
    if ($AccessMode -eq "CloudflareAccess") {
        Confirm-CloudflareAccessGuard
    }
    else {
        Write-Warning "OriginBasicAuth mode is for a single trusted operator only. Caddy authentication remains required; do not expose API, PostgreSQL, or web ports."
    }
}
else {
    Write-Warning "Tailscale mode is private to the tailnet. It does not publish aidison.xyz and does not require Cloudflare credentials."
}

$sourceValues = Read-EnvValues $sourcePath
$providerKeys = @(
    "DEEPSEEK_API_KEY",
    "OPENCODE_API_KEY",
    "TAVILY_API_KEY",
    "FIRECRAWL_API_KEY",
    "AIDISON_WEB_SEARCH_PROVIDER",
    "FIRECRAWL_MAX_RESULTS",
    "FIRECRAWL_MAX_QUERIES",
    "AIDISON_GITHUB_API_TOKEN",
    "AIDISON_GITHUB_SOURCE_TARGETS",
    "TAOBAO_APP_KEY",
    "TAOBAO_APP_SECRET",
    "AIDISON_RUNTIME_LANGFUSE_PUBLIC_KEY",
    "AIDISON_RUNTIME_LANGFUSE_SECRET_KEY",
    "AIDISON_RUNTIME_LANGFUSE_BASE_URL"
)

$dotenvQuote = [string][char]39
$values = @{
    POSTGRES_PASSWORD = New-UrlSafeSecret
    AIDISON_ADMIN_USER = $AdminUser
    # Preserve bcrypt dollar signs in the dotenv file without relying on nested quotes.
    AIDISON_ADMIN_PASSWORD_HASH = ($dotenvQuote + $adminHash + $dotenvQuote)
}
if ($PublishMode -eq "CloudflareTunnel") {
    $values["CLOUDFLARE_TUNNEL_TOKEN"] = $tunnelToken
}
foreach ($key in $providerKeys) {
    if ($sourceValues.ContainsKey($key)) {
        $values[$key] = $sourceValues[$key]
    }
}

# Accept standard Langfuse names locally, but write only Aidison runtime names
# to the production file.
$langfuseAliases = @{
    "LANGFUSE_PUBLIC_KEY" = "AIDISON_RUNTIME_LANGFUSE_PUBLIC_KEY"
    "LANGFUSE_SECRET_KEY" = "AIDISON_RUNTIME_LANGFUSE_SECRET_KEY"
    "LANGFUSE_BASE_URL" = "AIDISON_RUNTIME_LANGFUSE_BASE_URL"
}
foreach ($sourceKey in $langfuseAliases.Keys) {
    $targetKey = $langfuseAliases[$sourceKey]
    if (-not $values.ContainsKey($targetKey) -and $sourceValues.ContainsKey($sourceKey)) {
        $values[$targetKey] = $sourceValues[$sourceKey]
    }
}
if (
    $values.ContainsKey("AIDISON_RUNTIME_LANGFUSE_PUBLIC_KEY") -and
    $values.ContainsKey("AIDISON_RUNTIME_LANGFUSE_SECRET_KEY")
) {
    $values["AIDISON_RUNTIME_LANGFUSE_ENABLED"] = "1"
}

# Older local installations used GITHUB_API_KEY.  The runtime now requires a
# token together with an explicit source allowlist, so only migrate the legacy
# token when the operator has already supplied that allowlist under its new
# name.  This avoids producing a production file that the worker will reject.
if (
    -not $values.ContainsKey("AIDISON_GITHUB_API_TOKEN") -and
    $values.ContainsKey("AIDISON_GITHUB_SOURCE_TARGETS") -and
    $sourceValues.ContainsKey("GITHUB_API_KEY")
) {
    $values["AIDISON_GITHUB_API_TOKEN"] = $sourceValues["GITHUB_API_KEY"]
    Write-Host "Using the existing local GitHub token for the configured source allowlist."
}

if (-not $values.ContainsKey("DEEPSEEK_API_KEY")) {
    $deepseekKey = Read-SecretText "DEEPSEEK_API_KEY（当前 .env 未配置时必填，不回显）"
    if ([string]::IsNullOrWhiteSpace($deepseekKey)) {
        throw "DEEPSEEK_API_KEY is required"
    }
    $values["DEEPSEEK_API_KEY"] = $deepseekKey
}

$templateLines = Get-Content -LiteralPath $examplePath
$renderedLines = Set-EnvTemplateValues -TemplateLines $templateLines -Values $values
$targetDirectory = Split-Path -Parent $targetPath
if (-not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
    throw "Target directory does not exist: $targetDirectory"
}
$temporaryPath = Join-Path $targetDirectory (".{0}.{1}.tmp" -f ([IO.Path]::GetFileName($targetPath)), [guid]::NewGuid())
try {
    [IO.File]::WriteAllLines($temporaryPath, $renderedLines, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $targetPath
}
finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
    $tunnelToken = $null
    $deepseekKey = $null
    $sourceValues.Clear()
    $values.Clear()
}

Write-Host "Created production environment file: $targetPath"
Write-Host "Secret values were not printed. The file is excluded by Git."

if ($Deploy) {
    Push-Location $appRoot
    try {
        & (Join-Path $PSScriptRoot "deploy-production.ps1") -EnvFile $targetPath -PublishMode $PublishMode -SkipBuild:$SkipBuild
        if ($LASTEXITCODE -ne 0) {
            throw "Production deployment failed"
        }
    }
    finally {
        Pop-Location
    }
}
