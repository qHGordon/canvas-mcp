<#
.SYNOPSIS
  Register the "CanvasDailyReport" scheduled task (daily 07:45).
.DESCRIPTION
  Runs scripts/run_daily_report.cmd via uv.exe (absolute path auto-detected and
  passed in as UV_EXE). Behaviours so it "just runs whenever the PC is on":
    - StartWhenAvailable: if the PC was off/asleep at the scheduled time, run as
      soon as it is next available (catch-up), so a missed day still gets sent.
    - WakeToRun: wake the machine from sleep to run.
    - AllowStartIfOnBatteries / DontStopIfGoingOnBatteries: run on battery too.
    - ExecutionTimeLimit 15 min; MultipleInstances IgnoreNew (never overlap).
  Logon type is Interactive by default so LM Studio (a user-session app) is
  reachable for announcement summaries. The report still sends without the LLM
  (template summaries), so to run even when NOT logged in, pass -LogonType S4U
  (summaries fall back to the first 150 chars).
.NOTES
  No elevation needed (RunLevel Limited, current user). ASCII-only on purpose:
  Windows PowerShell 5.1 parses .ps1 in the system codepage.
#>

param(
    [ValidateSet("Interactive", "S4U")]
    [string]$LogonType = "Interactive",
    [string]$At = "7:45am"
)

$ErrorActionPreference = "Stop"
$TaskName = "CanvasDailyReport"
$RepoDir  = Split-Path -Parent $PSScriptRoot
$Cmd      = Join-Path $RepoDir "scripts\run_daily_report.cmd"

if (-not (Test-Path $Cmd)) { throw "run_daily_report.cmd not found at $Cmd" }

# Locate uv.exe absolutely (scheduled tasks often have an incomplete PATH).
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv.exe not found (tried PATH and $uv). Install uv first." }
Write-Host "uv.exe: $uv"

# Action: cmd.exe sets UV_EXE for this session, then runs the wrapper.
$arg = '/c set "UV_EXE=' + $uv + '" & "' + $Cmd + '"'
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arg -WorkingDirectory $RepoDir

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

if ($LogonType -eq "S4U") {
    # Runs whether or not the user is logged on (no stored password). LM Studio
    # won't be reachable, so announcement summaries fall back to template text.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
} else {
    # Runs only while the user is logged on, so LM Studio is reachable.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host ""
Write-Host "Registered task '$TaskName' (LogonType=$LogonType, daily at $At)."
Write-Host "  Next run : $($info.NextRunTime)"
Write-Host "  Action   : cmd /c ... $Cmd"
Write-Host ""
Write-Host "Test now (sends a real email): Start-ScheduledTask -TaskName $TaskName"
Write-Host "Dry test (no send)          : & '$Cmd' --dry-run"
Write-Host "Results                     : $RepoDir\data\reports\ (report_<date>.html / errors.log)"
Write-Host "Uninstall                   : scripts\uninstall_schedule.ps1"
