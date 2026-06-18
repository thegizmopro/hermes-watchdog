# ===========================================================================
#  Fix ExecutionTimeLimit on existing Hermes scheduled tasks
#
#  PROBLEM: Windows Task Scheduler defaults "Stop task if it runs longer than"
#  to 72 hours (PT72H). This silently kills daemons like the watchdog and
#  gateway after 3 days, and nothing restarts them until the next reboot.
#
#  FIX: Sets ExecutionTimeLimit to PT0S (no limit) on all Hermes_* tasks.
#  Uses COM interface (Set-ScheduledTask cmdlet silently ignores this change).
#
#  Run as Administrator. No parameters needed.
#  Safe to re-run — idempotent.
# ===========================================================================

$tasksToFix = @("Hermes_Gateway", "Hermes_Watchdog", "Hermes_Tray", "Hermes_MemorySync")

$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder("\")

foreach ($taskName in $tasksToFix) {
    try {
        $task = $folder.GetTask($taskName)
    } catch {
        Write-Output "[SKIP] Task '$taskName' not found"
        continue
    }

    $def = $task.Definition
    $current = $def.Settings.ExecutionTimeLimit
    Write-Output "[CHECK] $taskName : ExecutionTimeLimit = $current"

    if ($current -eq "PT0S") {
        Write-Output "  Already PT0S, no change needed"
        continue
    }

    $def.Settings.ExecutionTimeLimit = "PT0S"
    $user = $def.Principal.UserId
    $folder.RegisterTaskDefinition($taskName, $def, 6, $user, $null, $def.Principal.LogonType)

    # Verify
    $task2 = $folder.GetTask($taskName)
    $after = $task2.Definition.Settings.ExecutionTimeLimit
    Write-Output "  [FIXED] $current -> $after"
}

Write-Output ""
Write-Output "Done. All Hermes daemon tasks now have no execution time limit."
