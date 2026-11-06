<#
.SYNOPSIS
    Unified PI/VF Product Automation Platform operational entrypoint.

.DESCRIPTION
    Runs, monitors, and recovers the automated video production pipeline
    spanning Product Intelligence (PI) and Video Factory (VF).

.EXAMPLE
    .\Run.ps1 doctor
    .\Run.ps1 run --limit 1 --scenes 4 --provider-mode fake
    .\Run.ps1 find-mp4
    .\Run.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Locate Python environment (prefer VF venv, fallback to PI venv, fallback to system python)
$PythonExe = Join-Path $ScriptDir "apps\vf\.venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = Join-Path $ScriptDir "apps\pi\.venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

# Set UTF-8 encoding for console and Python subprocesses
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

# Set PYTHONPATH to include repo root so automation module is importable
$env:PYTHONPATH = "$ScriptDir;$env:PYTHONPATH"

# Set canonical isolated runtime paths
$env:HERMES_VIDEO_FACTORY_WORKSPACE = Join-Path $ScriptDir "runtime\vf\workspace"
$env:HERMES_EXCHANGE_INBOX = Join-Path $ScriptDir "runtime\exchange\inbox"
$env:VIDEO_FACTORY_HOME = Join-Path $ScriptDir "runtime\vf"
$env:HERMES_HOME = Join-Path $ScriptDir "runtime\vf"

if (-not $ScriptArgs -or $ScriptArgs.Count -eq 0) {
    & $PythonExe -m automation.cli --help
    exit $LASTEXITCODE
}

& $PythonExe -m automation.cli @ScriptArgs
exit $LASTEXITCODE
