<#
.SYNOPSIS
  Remove the "CanvasDailyReport" scheduled task.
.NOTES
  ASCII-only: Windows PowerShell 5.1 parses .ps1 in the system codepage.
#>

$ErrorActionPreference = "Stop"
$TaskName = "CanvasDailyReport"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'."
} else {
    Write-Host "Task '$TaskName' not found; nothing to remove."
}
