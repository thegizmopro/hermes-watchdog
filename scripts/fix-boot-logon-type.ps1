# fix-boot-logon-type.ps1 — Convert Hermes_Gateway + Hermes_Watchdog from S4U to
# Password logon (fixes flaky S4U boot failures: 0x80070520, gateway dead until
# user logs in). Preserves ALL existing task settings (BootTrigger, battery-agnostic,
# PT0S time limit) by modifying the registered task object instead of recreating it.
#
# Run from an ELEVATED PowerShell. Prompts for the Windows password ONCE.
# Works for any local user — pass username as param 1, defaults to kenzo.
#
# Do NOT convert Hermes_Tray: it needs InteractiveToken (tray icon requires a
# desktop session).
#
# Proven: lappy Aug 20 2026 — gateway up 51s after boot, no login required.

param([string]$UserName = "kenzo")

$ErrorActionPreference = 'Stop'
$tasks = @('Hermes_Gateway', 'Hermes_Watchdog')

$cred = Get-Credential -UserName "$env:COMPUTERNAME\$UserName" -Message 'Enter the Windows password (stored so tasks can log on at boot, before anyone signs in)'
if (-not $cred) { Write-Host 'Cancelled. Nothing changed.'; exit 1 }

foreach ($name in $tasks) {
    try {
        $task = Get-ScheduledTask -TaskName $name
        $task.Principal.LogonType = 'Password'
        Set-ScheduledTask -InputObject $task -User $cred.UserName -Password $cred.GetNetworkCredential().Password | Out-Null
    }
    catch {
        Write-Host "Direct set failed for ${name}: $($_.Exception.Message)"
        Write-Host "Trying stop-modify-restart fallback..."
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        $task = Get-ScheduledTask -TaskName $name
        $task.Principal.LogonType = 'Password'
        Set-ScheduledTask -InputObject $task -User $cred.UserName -Password $cred.GetNetworkCredential().Password | Out-Null
        Start-ScheduledTask -TaskName $name
    }

    $check = Get-ScheduledTask -TaskName $name
    Write-Host ("{0}: LogonType={1}  UserId={2}  State={3}" -f $name, $check.Principal.LogonType, $check.Principal.UserId, $check.State)
}

Write-Host ''
Write-Host 'If both lines above say LogonType=Password, run the reboot test:'
Write-Host '  1. Reboot. Do NOT log in. Wait 5 minutes.'
Write-Host '  2. Message the agent from a phone.'
Write-Host '  3. If it answers, the fix is proven.'
