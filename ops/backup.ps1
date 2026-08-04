[CmdletBinding()]
param(
    [string]$OutputRoot = "backups",
    [string]$ComposeFile = "compose.yaml",
    [string]$ComposeProject = "aidison"
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

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)][string[]]$CommandArgs,
        [switch]$Quiet
    )
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& docker compose --file $script:ComposePath --project-name $script:ProjectName @CommandArgs 2>&1)
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

function Remove-StagingDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$StagingPath,
        [Parameter(Mandatory = $true)][string]$AllowedRoot
    )
    if (-not (Test-Path -LiteralPath $StagingPath)) {
        return
    }
    $resolvedStaging = [System.IO.Path]::GetFullPath($StagingPath)
    $resolvedRoot = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\') + '\'
    if (-not $resolvedStaging.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to remove staging path outside OutputRoot: $resolvedStaging"
    }
    if (-not ([System.IO.Path]::GetFileName($resolvedStaging).StartsWith(".aidison-backup-"))) {
        throw "refusing to remove an unexpected staging directory: $resolvedStaging"
    }
    Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
}

$script:ComposePath = Resolve-FullPath $ComposeFile
$script:ProjectName = $ComposeProject
if (-not (Test-Path -LiteralPath $script:ComposePath -PathType Leaf)) {
    throw "Compose file does not exist: $script:ComposePath"
}
if ($ComposeProject -notmatch '^[a-z0-9][a-z0-9_-]+$') {
    throw "ComposeProject must contain only lowercase letters, digits, underscore or hyphen"
}

$outputRootPath = Resolve-FullPath $OutputRoot
New-Item -ItemType Directory -Path $outputRootPath -Force | Out-Null
$timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$backupId = [Guid]::NewGuid().ToString("N")
$backupName = "aidison-backup-$timestamp-$($backupId.Substring(0, 8))"
$finalPath = Join-Path $outputRootPath $backupName
$stagingPath = Join-Path $outputRootPath ".$backupName.staging"
if ((Test-Path -LiteralPath $finalPath) -or (Test-Path -LiteralPath $stagingPath)) {
    throw "backup destination already exists"
}

$containerDump = "/tmp/aidison-backup-$backupId.dump"
$copyContainer = "aidison-backup-copy-$backupId"
$servicesToRestore = @()
$backupCompleted = $false

try {
    $runningServices = @(
        Invoke-Compose -Quiet -CommandArgs @("ps", "--services", "--filter", "status=running") |
            ForEach-Object { "$($_)".Trim() } |
            Where-Object { $_ }
    )
    if ($runningServices -notcontains "postgres") {
        throw "postgres must be running before a backup can start"
    }
    $servicesToRestore = @($runningServices | Where-Object { $_ -in @("api", "worker") })

    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    if ($servicesToRestore.Count -gt 0) {
        Write-Host "Pausing write services: $($servicesToRestore -join ', ')"
        Invoke-Compose -CommandArgs (@("stop") + $servicesToRestore) | Out-Null
    }

    Write-Host "Running read-only PostgreSQL/Artifact integrity gate"
    Invoke-Compose -CommandArgs @(
        "run", "--rm", "--no-deps", "-T", "worker",
        "python", "-m", "aidison.operations.artifact_integrity"
    ) | Out-Null

    Invoke-Compose -CommandArgs @(
        "exec", "-T", "postgres", "pg_dump", "-U", "aidison", "-d", "aidison",
        "--format=custom", "--no-owner", "--no-privileges", "--file=$containerDump"
    ) | Out-Null
    $databasePath = Join-Path $stagingPath "database.dump"
    Invoke-Compose -CommandArgs @("cp", "postgres:$containerDump", $databasePath) | Out-Null

    $artifactsPath = Join-Path $stagingPath "artifacts"
    New-Item -ItemType Directory -Path $artifactsPath | Out-Null
    Invoke-Compose -CommandArgs @(
        "run", "--detach", "--name", $copyContainer, "--no-deps", "worker",
        "python", "-c", "import time; time.sleep(600)"
    ) | Out-Null
    Invoke-Docker -CommandArgs @("cp", "${copyContainer}:/data/artifacts/.", $artifactsPath) | Out-Null

    $revisionOutput = @(
        Invoke-Compose -Quiet -CommandArgs @(
            "exec", "-T", "postgres", "psql", "-U", "aidison", "-d", "aidison",
            "-Atqc", "SELECT version_num FROM alembic_version"
        )
    )
    $revisionLines = @(
        $revisionOutput |
            ForEach-Object { "$($_)".Trim() } |
            Where-Object { $_ }
    )
    $alembicRevision = $revisionLines[-1]
    if ($alembicRevision -notmatch '^[0-9a-f]{12}$') {
        throw "unexpected Alembic revision: $alembicRevision"
    }
    $workerImageOutput = @(
        Invoke-Compose -Quiet -CommandArgs @("images", "-q", "worker") |
            ForEach-Object { "$($_)".Trim() } |
            Where-Object { $_ }
    )
    $workerImage = if ($workerImageOutput.Count -gt 0) { $workerImageOutput[0] } else { $null }

    $artifactEntries = @()
    $artifactBytes = [int64]0
    $artifactPrefix = [System.IO.Path]::GetFullPath($artifactsPath).TrimEnd('\') + '\'
    foreach ($file in @(Get-ChildItem -LiteralPath $artifactsPath -File -Recurse | Sort-Object FullName)) {
        $relativePath = $file.FullName.Substring($artifactPrefix.Length).Replace('\', '/')
        $artifactEntries += [ordered]@{
            relative_path = $relativePath
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            size_bytes = [int64]$file.Length
        }
        $artifactBytes += [int64]$file.Length
    }

    $databaseFile = Get-Item -LiteralPath $databasePath
    $manifest = [ordered]@{
        schema_version = 1
        source = [ordered]@{
            compose_project = $ComposeProject
            compose_file = $script:ComposePath
            worker_image = $workerImage
        }
        created_at = [DateTime]::UtcNow.ToString("o")
        alembic_revision = $alembicRevision
        database = [ordered]@{
            path = "database.dump"
            sha256 = (Get-FileHash -LiteralPath $databasePath -Algorithm SHA256).Hash.ToLowerInvariant()
            size_bytes = [int64]$databaseFile.Length
        }
        artifacts = [ordered]@{
            root = "artifacts"
            file_count = $artifactEntries.Count
            total_bytes = $artifactBytes
            files = $artifactEntries
        }
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 8
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $stagingPath "manifest.json"),
        $manifestJson,
        $utf8WithoutBom
    )

    Move-Item -LiteralPath $stagingPath -Destination $finalPath
    $backupCompleted = $true
    Write-Host "Backup completed: $finalPath"
}
finally {
    try {
        Invoke-Docker -Quiet -CommandArgs @("rm", "-f", $copyContainer) | Out-Null
    }
    catch {
        Write-Warning "Could not remove the temporary Artifact copy container: $($_.Exception.Message)"
    }
    try {
        Invoke-Compose -Quiet -CommandArgs @(
            "exec", "-T", "postgres", "rm", "-f", $containerDump
        ) | Out-Null
    }
    catch {
        Write-Warning "Could not remove the temporary database dump: $($_.Exception.Message)"
    }

    if ($servicesToRestore.Count -gt 0) {
        try {
            Write-Host "Restoring original service state: $($servicesToRestore -join ', ')"
            Invoke-Compose -CommandArgs (@("start") + $servicesToRestore) | Out-Null
        }
        catch {
            Write-Warning "Could not restore all original services: $($_.Exception.Message)"
        }
    }

    if (-not $backupCompleted) {
        try {
            Remove-StagingDirectory -StagingPath $stagingPath -AllowedRoot $outputRootPath
        }
        catch {
            Write-Warning "Could not clean the staging directory: $($_.Exception.Message)"
        }
    }
}
