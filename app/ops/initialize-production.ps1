[CmdletBinding()]
param(
    [string]$EnvFile = ".env.production",
    [string]$SourceEnvFile = ".env",
    [string]$AdminUser = "aidison",
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
        [Parameter(Mandatory = $true)][string[]]$TemplateLines,
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

$tunnelToken = Read-SecretText "Cloudflare Tunnel Token（不回显）"
if ([string]::IsNullOrWhiteSpace($tunnelToken)) {
    throw "Cloudflare Tunnel Token is required"
}

$sourceValues = Read-EnvValues $sourcePath
$providerKeys = @(
    "DEEPSEEK_API_KEY",
    "OPENCODE_API_KEY",
    "TAVILY_API_KEY",
    "FIRECRAWL_API_KEY",
    "AIDISON_GITHUB_API_TOKEN",
    "AIDISON_GITHUB_SOURCE_TARGETS",
    "TAOBAO_APP_KEY",
    "TAOBAO_APP_SECRET",
    "AIDISON_RUNTIME_LANGFUSE_PUBLIC_KEY",
    "AIDISON_RUNTIME_LANGFUSE_SECRET_KEY"
)

$values = @{
    POSTGRES_PASSWORD = New-UrlSafeSecret
    AIDISON_ADMIN_USER = $AdminUser
    AIDISON_ADMIN_PASSWORD_HASH = "'$adminHash'"
    CLOUDFLARE_TUNNEL_TOKEN = $tunnelToken
}
foreach ($key in $providerKeys) {
    if ($sourceValues.ContainsKey($key)) {
        $values[$key] = $sourceValues[$key]
    }
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
        & (Join-Path $PSScriptRoot "deploy-production.ps1") -EnvFile $targetPath -SkipBuild:$SkipBuild
        if ($LASTEXITCODE -ne 0) {
            throw "Production deployment failed"
        }
    }
    finally {
        Pop-Location
    }
}
