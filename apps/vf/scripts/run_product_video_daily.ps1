param(
    [string]$DbPath,
    [string]$InboxRoot,
    [string]$LogPath,
    [string]$ProfilePath = $env:VF_CREATIVE_RUN_CONFIG,
    [string]$RunId,
    [ValidateSet("low", "middle", "high")]
    [string]$QcLevel = "high",
    [switch]$CheckOnly,
    [switch]$WorkerOnce
)

$ErrorActionPreference = "Stop"
$appRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..\..")).Path

if (-not $DbPath) {
    $runtimeDb = Join-Path $repoRoot "runtime\vf\hermes.db"
    $appDb = Join-Path $appRoot "data\hermes.db"
    $DbPath = if (Test-Path -LiteralPath (Split-Path -Parent $runtimeDb)) { $runtimeDb } else { $appDb }
}
if (-not $InboxRoot) {
    $runtimeInbox = Join-Path $repoRoot "runtime\exchange\inbox"
    $InboxRoot = if (Test-Path -LiteralPath $runtimeInbox) { $runtimeInbox } else { "D:\work\hermes-data\product-intelligence-data\inbox" }
}
if (-not $LogPath) {
    $LogPath = Join-Path (Split-Path -Parent $DbPath) "logs\product-video-daily.log"
}

$uv = if (Test-Path -LiteralPath "C:\Users\ninak\AppData\Local\hermes\bin\uv.exe" -PathType Leaf) {
    "C:\Users\ninak\AppData\Local\hermes\bin\uv.exe"
} elseif (Get-Command uv -ErrorAction SilentlyContinue) {
    (Get-Command uv).Source
} else {
    throw "uv executable not found on PATH or hermes directory."
}

$logDirectory = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$bangkokTimeZone = [TimeZoneInfo]::FindSystemTimeZoneById("SE Asia Standard Time")
$nowBangkok = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $bangkokTimeZone)
$today = $nowBangkok.ToString("yyyy-MM-dd")
$commandArgs = @("run", "video-factory", "--db-path", $DbPath)
if ($CheckOnly -and $WorkerOnce) {
    throw "Choose only one of -CheckOnly or -WorkerOnce"
}
if ($CheckOnly) {
    $commandArgs += @("daily-status", "--date", $today, "--minimum", "1")
} elseif ($WorkerOnce) {
    $commandArgs += @("--inbox-root", $InboxRoot, "watch", "--once", "--limit", "100", "--provider-mode", "configured", "--max-qc-retries", "3")
} else {
    $readyRoot = Join-Path $InboxRoot $today
    $ready = @()
    $piStartLocal = [datetime]::ParseExact("$today 06:00:00", "yyyy-MM-dd HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture)
    $piStart = [DateTimeOffset]::new($piStartLocal, [TimeSpan]::FromHours(7))
    $deadlineLocal = [datetime]::ParseExact("$today 09:00:00", "yyyy-MM-dd HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture)
    $deadline = [DateTimeOffset]::new($deadlineLocal, [TimeSpan]::FromHours(7))
    do {
        if (Test-Path -LiteralPath $readyRoot -PathType Container) {
            $ready = @(Get-ChildItem -LiteralPath $readyRoot -Directory -Filter "run_*" -ErrorAction SilentlyContinue |
                Where-Object {
                    $readyFile = Join-Path $_.FullName "READY"
                    $manifestFile = Join-Path $_.FullName "manifest.json"
                    if (-not (Test-Path -LiteralPath $readyFile -PathType Leaf) -or
                        -not (Test-Path -LiteralPath $manifestFile -PathType Leaf)) { return $false }
                    try {
                        $manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
                        $createdAt = [DateTimeOffset]::Parse([string]$manifest.created_at)
                        return ([string]$manifest.production_date -eq $today -and $createdAt -ge $piStart)
                    } catch { return $false }
                })
        }
        $nowBangkok = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $bangkokTimeZone)
        if ($ready.Count -gt 0 -or $nowBangkok -ge $deadline) { break }
        Start-Sleep -Seconds 60
    } while ($true)

    if ($ready.Count -eq 0) {
        "[$(Get-Date -Format o)] NO_READY_BY_09:00: no READY batch for $today; no backlog substitution was attempted." |
            Tee-Object -FilePath $LogPath -Append
        exit 0
    }
    if (-not $ProfilePath -or -not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) {
        "[$(Get-Date -Format o)] BLOCKED_PROFILE: set VF_CREATIVE_RUN_CONFIG to an approved creative run profile; pinned batch $($ready[0].Name) was not started." |
            Tee-Object -FilePath $LogPath -Append
        exit 2
    }
    $runId = if ($RunId) { $RunId } else { "vf-" + $today.Replace("-", "") }
    $commandArgs += @(
        "--inbox-root", $InboxRoot, "creative-batch", "--run-id", $runId,
        "--date", $today, "--all", "--profile", $ProfilePath,
        "--owner-user-id", "local-owner", "--max-ticks", "100", "--qc-level", $QcLevel
    )
}

Push-Location $repoRoot
try {
    "[$(Get-Date -Format o)] Video Factory $($commandArgs -join ' ')" |
        Tee-Object -FilePath $LogPath -Append
    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $uv @commandArgs 2>&1 |
            ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { $_ } } |
            Tee-Object -FilePath $LogPath -Append
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
} finally {
    Pop-Location
}
"[$(Get-Date -Format o)] Video Factory exited with code $exitCode" |
    Tee-Object -FilePath $LogPath -Append
exit $exitCode
