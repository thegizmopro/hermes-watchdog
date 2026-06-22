# ===========================================================================
#  Hotfix: Add PT5M repetition interval to Hermes_Watchdog trigger
#
#  PROBLEM: The watchdog BootTrigger fires once at boot. If the pythonw
#  process dies silently (OS kill, clean exit, crash), the task never
#  re-fires — the watchdog stays dead until the next reboot.
#
#  FIX: Add a 5-minute repetition interval to the BootTrigger. Task Scheduler
#  attempts to launch every 5 min; if already running, IgnoreNew skips it.
#  If dead, it relaunches.
#
#  Usage: powershell -ExecutionPolicy Bypass -File fix-watchdog-repetition.ps1
# ===========================================================================

$taskName = "Hermes_Watchdog"

Write-Output "=== Fixing $taskName trigger ==="

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Error "Task '$taskName' not found. Run setup_hermes_infrastructure.ps1 instead."
    exit 1
}

# Build new AtStartup trigger with PT5M repetition
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.StartBoundary = ""
$trigger.Repetition = (New-CimInstance -CimClass (Get-CimClass MSFT_TaskRepetitionPattern Root/Microsoft/Windows/TaskScheduler) -Property @{ Interval='PT5M'; Duration='' } -ClientOnly)

# Preserve existing settings and principal
Set-ScheduledTask -TaskName $taskName -Trigger $trigger | Out-Null

Write-Output "Trigger updated: BootTrigger + PT5M repetition interval"

# Start the task now if not already running
$state = (Get-ScheduledTask -TaskName $taskName).State
if ($state -ne 'Running') {
    Write-Output "Watchdog not running — starting now..."
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $taskName).State
}

Write-Output "Done. Task state: $state"
Write-Output ""
Write-Output "Verify with: Export-ScheduledTask -TaskName 'Hermes_Watchdog' | Select-String 'Repetition'"
