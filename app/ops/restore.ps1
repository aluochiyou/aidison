[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [string]$ComposeFile = "compose.yaml",
    [string]$TargetComposeProject = "aidison-restore-verify",
    [int]$PostgresPort = 55434
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Path))
}

function Resolve-BackupMember {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    if ([System.IO.Path]::IsPathRooted($RelativePath)) {
        throw "manifest path must be relative: $RelativePath"
    }
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $Root $RelativePath))
    if (-not $candidate.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "manifest path escaped BackupPath: $RelativePath"
    }
    return $candidate
}

function Invoke-TargetCompose {
    param(
        [Parameter(Mandatory = $true)][string[]]$CommandArgs,
        [switch]$Quiet
    )
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& docker compose --file $script:ComposePath --project-name $script:TargetProject @CommandArgs 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if (-not $Quiet) {
        $output | ForEach-Object { Write-Host $_ }
    }
    if ($exitCode -ne 0) {
        throw "docker compose failed with exit code ${exitCode}: $($CommandArgs -join ' ')"
    }
    return $output
}

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)][string[]]$CommandArgs,
        [switch]$Quiet
    )
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& docker @CommandArgs 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if (-not $Quiet) {
        $output | ForEach-Object { Write-Host $_ }
    }
    if ($exitCode -ne 0) {
        throw "docker failed with exit code ${exitCode}: $($CommandArgs -join ' ')"
    }
    return $output
}

function Test-DockerVolumeExists {
    param([Parameter(Mandatory = $true)][string]$Name)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & docker volume inspect $Name *> $null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

$script:ComposePath = Resolve-FullPath $ComposeFile
$script:TargetProject = $TargetComposeProject
$resolvedBackup = Resolve-FullPath $BackupPath
if (-not (Test-Path -LiteralPath $script:ComposePath -PathType Leaf)) {
    throw "Compose file does not exist: $script:ComposePath"
}
if (-not (Test-Path -LiteralPath $resolvedBackup -PathType Container)) {
    throw "BackupPath does not exist: $resolvedBackup"
}
if ($TargetComposeProject -notmatch '^[a-z0-9][a-z0-9_-]+$') {
    throw "TargetComposeProject must contain only lowercase letters, digits, underscore or hyphen"
}
if ($PostgresPort -lt 1024 -or $PostgresPort -gt 65535) {
    throw "PostgresPort must be between 1024 and 65535"
}

$manifestPath = Join-Path $resolvedBackup "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "manifest.json is missing"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.schema_version -ne 1) {
    throw "unsupported manifest schema_version: $($manifest.schema_version)"
}
if ([string]::IsNullOrWhiteSpace([string]$manifest.source.compose_project)) {
    throw "manifest source.compose_project is missing"
}
if ([string]$manifest.alembic_revision -notmatch '^[0-9a-f]{12}$') {
    throw "manifest Alembic revision is invalid"
}
if ($TargetComposeProject -eq [string]$manifest.source.compose_project) {
    throw "refusing to restore into the source Compose project"
}

$databasePath = Resolve-BackupMember -Root $resolvedBackup -RelativePath ([string]$manifest.database.path)
if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
    throw "database dump is missing: $databasePath"
}
$databaseFile = Get-Item -LiteralPath $databasePath
$databaseHash = (Get-FileHash -LiteralPath $databasePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($databaseHash -ne [string]$manifest.database.sha256) {
    throw "database dump SHA-256 does not match manifest"
}
if ([int64]$databaseFile.Length -ne [int64]$manifest.database.size_bytes) {
    throw "database dump size does not match manifest"
}

$artifactRoot = Resolve-BackupMember -Root $resolvedBackup -RelativePath ([string]$manifest.artifacts.root)
if (-not (Test-Path -LiteralPath $artifactRoot -PathType Container)) {
    throw "artifact backup directory is missing: $artifactRoot"
}
$verifiedArtifactBytes = [int64]0
$verifiedArtifactCount = 0
foreach ($entry in @($manifest.artifacts.files)) {
    $artifactPath = Resolve-BackupMember -Root $artifactRoot -RelativePath ([string]$entry.relative_path)
    if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf)) {
        throw "artifact file is missing: $($entry.relative_path)"
    }
    $file = Get-Item -LiteralPath $artifactPath
    if ([int64]$file.Length -ne [int64]$entry.size_bytes) {
        throw "artifact size does not match manifest: $($entry.relative_path)"
    }
    $hash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne [string]$entry.sha256) {
        throw "artifact SHA-256 does not match manifest: $($entry.relative_path)"
    }
    $verifiedArtifactCount += 1
    $verifiedArtifactBytes += [int64]$file.Length
}
if ($verifiedArtifactCount -ne [int]$manifest.artifacts.file_count) {
    throw "artifact file count does not match manifest"
}
if ($verifiedArtifactBytes -ne [int64]$manifest.artifacts.total_bytes) {
    throw "artifact total bytes do not match manifest"
}

$existingContainers = @(
    Invoke-TargetCompose -Quiet -CommandArgs @("ps", "-aq") |
        ForEach-Object { "$($_)".Trim() } |
        Where-Object { $_ }
)
if ($existingContainers.Count -gt 0) {
    throw "target Compose project already has containers; refusing to overwrite"
}
$postgresVolume = "${TargetComposeProject}_postgres-data"
$artifactVolume = "${TargetComposeProject}_artifacts"
if ((Test-DockerVolumeExists $postgresVolume) -or (Test-DockerVolumeExists $artifactVolume)) {
    throw "target Compose project already has named volumes; refusing to overwrite"
}

$previousPort = $env:AIDISON_POSTGRES_PORT
$env:AIDISON_POSTGRES_PORT = [string]$PostgresPort
$restoreId = [Guid]::NewGuid().ToString("N")
$containerDump = "/tmp/aidison-restore-$restoreId.dump"
$copyContainer = "$TargetComposeProject-artifact-restore-$restoreId"
try {
    Write-Host "Building the isolated verification worker image"
    Invoke-TargetCompose -CommandArgs @("build", "worker") | Out-Null
    Write-Host "Starting isolated PostgreSQL project $TargetComposeProject on port $PostgresPort"
    Invoke-TargetCompose -CommandArgs @("up", "-d", "postgres") | Out-Null

    $postgresContainer = @(
        Invoke-TargetCompose -Quiet -CommandArgs @("ps", "-q", "postgres") |
            ForEach-Object { "$($_)".Trim() } |
            Where-Object { $_ }
    )[0]
    $healthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
        $health = @(& docker inspect --format "{{.State.Health.Status}}" $postgresContainer 2>&1)
        if ($LASTEXITCODE -eq 0 -and "$($health[-1])".Trim() -eq "healthy") {
            $healthy = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) {
        throw "isolated PostgreSQL did not become healthy"
    }

    Invoke-TargetCompose -CommandArgs @("cp", $databasePath, "postgres:$containerDump") | Out-Null
    Invoke-TargetCompose -CommandArgs @(
        "exec", "-T", "postgres", "dropdb", "-U", "aidison", "--if-exists", "--force", "aidison"
    ) | Out-Null
    Invoke-TargetCompose -CommandArgs @(
        "exec", "-T", "postgres", "createdb", "-U", "aidison", "aidison"
    ) | Out-Null
    Invoke-TargetCompose -CommandArgs @(
        "exec", "-T", "postgres", "pg_restore", "-U", "aidison", "-d", "aidison",
        "--no-owner", "--no-privileges", $containerDump
    ) | Out-Null

    Invoke-TargetCompose -CommandArgs @(
        "run", "--detach", "--name", $copyContainer, "--no-deps", "worker",
        "python", "-c", "import time; time.sleep(600)"
    ) | Out-Null
    Invoke-Docker -CommandArgs @("cp", "$artifactRoot\.", "${copyContainer}:/data/artifacts") | Out-Null
    Invoke-Docker -CommandArgs @("rm", "-f", $copyContainer) | Out-Null
    Write-Host "Running restored PostgreSQL/Artifact integrity verification"
    Invoke-TargetCompose -CommandArgs @(
        "run", "--rm", "--no-deps", "-T", "worker",
        "python", "-m", "aidison.operations.artifact_integrity"
    ) | Out-Null

    $revisionOutput = @(
        Invoke-TargetCompose -Quiet -CommandArgs @(
            "exec", "-T", "postgres", "psql", "-U", "aidison", "-d", "aidison",
            "-Atqc", "SELECT version_num FROM alembic_version"
        ) |
            ForEach-Object { "$($_)".Trim() } |
            Where-Object { $_ }
    )
    $restoredRevision = $revisionOutput[-1]
    if ($restoredRevision -ne [string]$manifest.alembic_revision) {
        throw "restored Alembic revision does not match manifest"
    }

    Write-Host "Restore verification passed. Isolated project retained: $TargetComposeProject"
    Write-Host "Inspect with: docker compose -f `"$script:ComposePath`" -p $TargetComposeProject ps"
}
finally {
    try {
        Invoke-Docker -Quiet -CommandArgs @("rm", "-f", $copyContainer) | Out-Null
    }
    catch {
        Write-Warning "Could not remove the temporary Artifact restore container: $($_.Exception.Message)"
    }
    try {
        Invoke-TargetCompose -Quiet -CommandArgs @(
            "exec", "-T", "postgres", "rm", "-f", $containerDump
        ) | Out-Null
    }
    catch {
        Write-Warning "Could not remove the temporary restore dump: $($_.Exception.Message)"
    }
    $env:AIDISON_POSTGRES_PORT = $previousPort
}
