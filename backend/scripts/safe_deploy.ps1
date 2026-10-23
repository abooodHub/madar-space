param(
    [int]$IdleTimeoutSeconds = 0,
    [int]$StartupTimeoutSeconds = 180,
    [string]$ApiUrl = 'http://127.0.0.1:8765',
    [string]$NoVncUrl = 'http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale',
    [switch]$AdoptMaintenanceGate,
    [switch]$InitializeAccountPortfolio
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repoRoot
$gateOwned = $false
$replacementStarted = $false
$script:LastCheckedExitCode = $null

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    $script:LastCheckedExitCode = $LASTEXITCODE
    if ($script:LastCheckedExitCode -ne 0) {
        throw "Command failed with exit code $($script:LastCheckedExitCode): $FilePath"
    }
}

function Get-ApiJson {
    param([Parameter(Mandatory = $true)][string]$Uri)
    Invoke-RestMethod -Uri $Uri -TimeoutSec 8
}

function Wait-ForMaintenanceBoot {
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Get-ApiJson "$ApiUrl/health"
            $status = Get-ApiJson "$ApiUrl/maintenance/status"
            $containerHealth = (& docker inspect --format '{{.State.Health.Status}}' xsa-app 2>$null).Trim()
            if (
                $LASTEXITCODE -eq 0 -and
                $containerHealth -eq 'healthy' -and
                $health.service_health.status -eq 'maintenance' -and
                $health.api_health.status -eq 'healthy' -and
                $health.monitor_running -eq $false -and
                $health.browser_connected -eq $false -and
                $health.database.healthy -eq $true -and
                $health.database.quick_check -eq 'ok' -and
                $health.database.foreign_key_violations -eq 0 -and
                $status.maintenance_mode -eq $true -and
                $status.safe_to_deploy -eq $true -and
                @($status.active_recordings).Count -eq 0 -and
                $status.recording_ffmpeg_count -eq 0 -and
                @($status.active_post_processing).Count -eq 0 -and
                @($status.unsafe_spaces).Count -eq 0
            ) {
                return
            }
        }
        catch {
            # The API is expected to be briefly unavailable while Compose replaces it.
        }
        Start-Sleep -Seconds 3
    }
    throw 'Maintenance boot did not pass API, monitor, browser, database, and zero-recorder checks.'
}

function Wait-ForMonitoringResume {
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Get-ApiJson "$ApiUrl/health"
            $live = Get-ApiJson "$ApiUrl/live"
            if (
                $health.api_health.status -eq 'healthy' -and
                $health.service_health.status -eq 'ok' -and
                $health.monitor_running -eq $true -and
                $health.browser_connected -eq $true -and
                $health.database.healthy -eq $true -and
                $health.database.quick_check -eq 'ok' -and
                $health.database.foreign_key_violations -eq 0 -and
                $health.maintenance_mode -eq $false -and
                $live.maintenance_mode -eq $false -and
                $live.recording_ffmpeg_count -le @($live.active).Count -and
                @($health.active_recordings).Count -eq @($live.active).Count
            ) {
                return [pscustomobject]@{ Health = $health; Live = $live }
            }
        }
        catch {
            # Keep checking until the configured startup deadline.
        }
        Start-Sleep -Seconds 3
    }
    throw 'Monitoring did not resume with healthy API/browser/database and consistent FFmpeg state.'
}

try {
    Write-Host 'Arming the maintenance gate and waiting for two independent idle checks. Active recordings will be allowed to finish.'
    $beforeArm = Get-ApiJson "$ApiUrl/maintenance/status"
    if ($beforeArm.maintenance_mode -eq $true -and -not $AdoptMaintenanceGate) {
        throw 'A maintenance gate is already active; inspect it before starting another deployment.'
    }
    if ($beforeArm.maintenance_mode -eq $true -and $AdoptMaintenanceGate) {
        $gateOwned = $true
        if ($beforeArm.safe_to_deploy -ne $true) { throw 'Existing maintenance gate is not idle; refusing deployment.' }
        Start-Sleep -Seconds 3
        $secondIdle = Get-ApiJson "$ApiUrl/maintenance/status"
        if ($secondIdle.safe_to_deploy -ne $true) { throw 'Existing gate failed its second idle check.' }
    }
    else { try {
        Invoke-CheckedCommand -FilePath 'docker' -Arguments @(
            'compose', 'exec', '-T', 'app', 'python', '/app/backend/scripts/maintenance_gate.py',
            '--api', 'http://127.0.0.1:8765', 'arm', '--timeout-seconds', "$IdleTimeoutSeconds"
        )
        $gateOwned = $true
    }
    catch {
        # Exit 3 is the gate CLI's explicit timeout-after-arm result.
        if ($script:LastCheckedExitCode -eq 3) { $gateOwned = $true }
        throw
    }
    }

    $preflight = Get-ApiJson "$ApiUrl/maintenance/status"
    if ($preflight.safe_to_deploy -ne $true -or $preflight.maintenance_mode -ne $true) {
        throw 'Production maintenance preflight is not safe; no backup, build, or restart was attempted.'
    }

    Write-Host 'Creating and validating a SQLite online backup.'
    Invoke-CheckedCommand -FilePath 'docker' -Arguments @(
        'compose', 'exec', '-T', 'app', 'python', '/app/backend/scripts/backup_database.py',
        '--source', '/state/xsa.sqlite3', '--backup-dir', '/data/_maintenance/backups'
    )

    # Preserve the exact previous image before the mutable development tag is
    # replaced by the build. No container is stopped by tagging its image.
    $deploymentStamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $previousImage = (& docker inspect --format '{{.Image}}' xsa-app).Trim()
    if ($LASTEXITCODE -ne 0 -or $previousImage -notmatch '^sha256:[a-f0-9]{64}$') {
        throw 'Could not identify the current image; refusing to build without a rollback image.'
    }
    $rollbackTag = "xsa-app:rollback-$deploymentStamp"
    Invoke-CheckedCommand -FilePath 'docker' -Arguments @('image', 'tag', $previousImage, $rollbackTag)
    $recordDirectory = Join-Path $repoRoot 'artifacts\deployment-records'
    New-Item -ItemType Directory -Force -Path $recordDirectory | Out-Null
    [pscustomobject]@{
        created_at_utc = [DateTime]::UtcNow.ToString('o')
        previous_image = $previousImage
        rollback_tag = $rollbackTag
        database_backup_directory = '/data/_maintenance/backups'
        maintenance_gate_armed = $true
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $recordDirectory "$deploymentStamp.json") -Encoding utf8

    Write-Host 'Building the application image.'
    Invoke-CheckedCommand -FilePath 'docker' -Arguments @('compose', 'build', 'app')

    # Recheck after the build, immediately before replacing the only app
    # container. The gate should prevent new work, but deployment still
    # requires a fresh independent zero-recorder result.
    $finalPreflight = Get-ApiJson "$ApiUrl/maintenance/status"
    if ($finalPreflight.safe_to_deploy -ne $true -or $finalPreflight.maintenance_mode -ne $true) {
        throw 'Final pre-restart check is not safe; the running app was not replaced and maintenance remains armed.'
    }

    Write-Host 'Replacing the application while the persistent maintenance gate remains armed.'
    $replacementStarted = $true
    Invoke-CheckedCommand -FilePath 'docker' -Arguments @('compose', 'up', '-d', '--no-deps', 'app')
    Wait-ForMaintenanceBoot

    $dashboard = Invoke-WebRequest -Uri "$ApiUrl/" -TimeoutSec 10 -UseBasicParsing
    if ($dashboard.StatusCode -ne 200) { throw 'Dashboard did not return HTTP 200 during maintenance boot.' }
    foreach ($section in @('live', 'manual', 'library', 'accounts', 'processing', 'analytics', 'health', 'settings')) {
        $page = Invoke-WebRequest -Uri "$ApiUrl/workspace/$section" -TimeoutSec 10 -UseBasicParsing
        if ($page.StatusCode -ne 200 -or $page.Content -notmatch 'XSpace Analyzer') {
            throw "Workspace route failed its maintenance boot check: $section"
        }
    }
    $configuration = Get-ApiJson "$ApiUrl/workspace/config"
    if ($null -eq $configuration.max_capture_recoveries) { throw 'New runtime configuration endpoint is unavailable.' }
    $jobPage = Get-ApiJson "$ApiUrl/processing/jobs?limit=1"
    if ($null -eq $jobPage.PSObject.Properties['items']) { throw 'Processing job endpoint is unavailable.' }
    $containerHealth = (& docker inspect --format '{{.State.Health.Status}}' xsa-app).Trim()
    if ($LASTEXITCODE -ne 0 -or $containerHealth -ne 'healthy') {
        throw 'The replacement xsa-app container is not healthy.'
    }

    if ($InitializeAccountPortfolio) {
        Write-Host 'Selecting ten historically active accounts while maintenance remains armed.'
        Invoke-CheckedCommand -FilePath 'docker' -Arguments @(
            'compose', 'exec', '-T', 'app', 'python', '/app/backend/scripts/bootstrap_accounts.py',
            '--apply', '--expected-count', '59'
        )
        $portfolio = Get-ApiJson "$ApiUrl/accounts"
        if (@($portfolio).Count -ne 59 -or @($portfolio | Where-Object { $_.active }).Count -ne 10) {
            throw 'Portfolio verification failed: 59 preserved accounts and ten active were expected.'
        }
    }

    Write-Host 'Maintenance boot passed. Releasing the gate so automatic monitoring can resume.'
    Invoke-CheckedCommand -FilePath 'docker' -Arguments @(
        'compose', 'exec', '-T', 'app', 'python', '/app/backend/scripts/maintenance_gate.py',
        '--api', 'http://127.0.0.1:8765', 'release'
    )
    $gateOwned = $false
    $verified = Wait-ForMonitoringResume
    $health = $verified.Health
    $live = $verified.Live

    $dashboard = Invoke-WebRequest -Uri "$ApiUrl/" -TimeoutSec 10 -UseBasicParsing
    if ($dashboard.StatusCode -ne 200) { throw 'Dashboard did not return HTTP 200 after release.' }
    $noVnc = Invoke-WebRequest -Uri $NoVncUrl -TimeoutSec 10 -UseBasicParsing
    if ($noVnc.StatusCode -ne 200) { throw 'noVNC did not return HTTP 200 after monitoring resumed.' }

    [pscustomobject]@{
        result = 'DEPLOYMENT_VERIFIED'
        api = $health.status
        monitor_running = $health.monitor_running
        browser_connected = $health.browser_connected
        database_quick_check = $health.database.quick_check
        foreign_key_violations = $health.database.foreign_key_violations
        maintenance_mode = $health.maintenance_mode
        active_recordings = @($live.active).Count
        recording_ffmpeg_count = $live.recording_ffmpeg_count
        dashboard_http = $dashboard.StatusCode
        novnc_http = $noVnc.StatusCode
    } | Format-List
}
catch {
    if ($gateOwned -and -not $replacementStarted) {
        try {
            Write-Warning 'Deployment stopped before app replacement; releasing the gate to resume the existing monitor.'
            Invoke-CheckedCommand -FilePath 'docker' -Arguments @(
                'compose', 'exec', '-T', 'app', 'python', '/app/backend/scripts/maintenance_gate.py',
                '--api', 'http://127.0.0.1:8765', 'release'
            )
            $gateOwned = $false
            $null = Wait-ForMonitoringResume
            Write-Warning 'The existing app resumed monitoring after the pre-restart failure.'
        }
        catch {
            Write-Warning 'Could not verify monitoring recovery; maintenance may still need operator attention.'
        }
    }
    throw
}
finally {
    Pop-Location
}
