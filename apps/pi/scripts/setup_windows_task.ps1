# Setup script for Windows Task Scheduler with Automatic Catch-up and Retries
# Note: InteractiveToken and WakeToRun=False mean this task runs within an interactive user
# session and will not wake a sleeping machine. StartWhenAvailable ensures catch-up when unlocked.

$ErrorActionPreference = "Stop"
$TaskName = "ProductIntelligenceDailyScout"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Get-Item "$ScriptDir\..").FullName
$BatFile = "$ScriptDir\run_daily.bat"
$LogDir = "$ProjectDir\logs"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

Write-Host "=== CAU HINH WINDOWS TASK SCHEDULER: $TaskName ===" -ForegroundColor Cyan
Write-Host "Thu muc du an: $ProjectDir"
Write-Host "File thuc thi: $BatFile"
Write-Host "Thu muc logs: $LogDir"

# 1. Action: Execute run_daily.bat with dated logging under logs/
$CmdArgs = "/c `"`"$BatFile`" >> `"$LogDir\daily_task_run.log`" 2>&1`""
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $CmdArgs -WorkingDirectory $ProjectDir

# 2. Trigger: Daily at 07:00 AM
$Trigger = New-ScheduledTaskTrigger -Daily -At "07:00"

# 3. Settings: 3 retries at 15-minute intervals, StartWhenAvailable, MultipleInstances IgnoreNew
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15)

# 4. Register Task
try {
    # Unregister old task if exists
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Daily Product Scout: Tu dong quet san pham theo request.txt luc 07:00 co tinh nang chay bu va retry."

    # 5. Observable verification: re-read the registered task and verify configuration
    $Registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $RegSettings = $Registered.Settings
    $RegInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue

    if ($RegSettings.RestartCount -ne 3) {
        throw "Verification failed: RestartCount is $($RegSettings.RestartCount), expected 3"
    }
    if ($RegSettings.RestartInterval -ne "PT15M") {
        throw "Verification failed: RestartInterval is $($RegSettings.RestartInterval), expected PT15M"
    }
    if (-not $RegSettings.StartWhenAvailable) {
        throw "Verification failed: StartWhenAvailable is not enabled"
    }

    Write-Host "`n[THANH CONG] Da dang ky va xac thuc Task Scheduler thanh cong!" -ForegroundColor Green
    Write-Host "- Action: $($Registered.Actions[0].Execute) $($Registered.Actions[0].Arguments)"
    Write-Host "- WorkingDirectory: $($Registered.Actions[0].WorkingDirectory)"
    Write-Host "- Lich chay: Hang ngay vao luc 07:00 AM"
    Write-Host "- Next Run Time: $($RegInfo.NextRunTime)"
    Write-Host "- Che do chay bu: StartWhenAvailable = True"
    Write-Host "- Retry: $($RegSettings.RestartCount) lan (moi lan cach $($RegSettings.RestartInterval))"
} catch {
    Write-Host "`n[LOI] Dang ky Task that bai: $_" -ForegroundColor Red
    exit 1
}
