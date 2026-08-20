# Incident: Gateway dead after reboot until login (S4U boot failure)

**Date:** 2026-08-19/20 · **Machine:** lappy (fixed + verified) · **Fleet:** thinzo had the same latent bug

## Symptom

Windows rebooted (overnight update). Gateway + watchdog did NOT come up. The
agent only came online ~13 minutes after boot — exactly when the user logged
in interactively.

## Root cause

`Hermes_Gateway` and `Hermes_Watchdog` were registered with **BootTrigger +
LogonType S4U** ("run as user without storing password"). At boot, Task
Scheduler's S4U logon attempt was denied:

```
Last Result: -2147020576  (0x80070520 ERROR_LOGON_TYPE_NOT_GRANTED)
```

The S4U pathway is simply flaky at boot time — especially after Windows
Update reboots. When the user logged in, `StartWhenAvailable` retried the
task against the interactive session and it succeeded. So the task config
looked perfect (trigger, battery flags, time limit all correct) while
failing every unattended boot.

## Diagnostics (rule out policy causes first)

```powershell
# 1. Credential storage policy — must be 0/absent
reg query HKLM\SYSTEM\CurrentControlSet\Control\Lsa /v DisableDomainCreds

# 2. Batch logon right — UTF-16 file, decode before grep
secedit /export /cfg C:\temp\secpol.cfg
# then: iconv -f UTF-16LE -t UTF-8 secpol.cfg | grep SeBatchLogonRight
# Admins (S-1-5-32-544) present = OK; also check no SeDenyBatchLogonRight
```

On the affected machine both were CLEAN — the right was granted, storage was
allowed. The S4U boot failure happened anyway. Conclusion: don't rely on S4U
for BootTrigger tasks that must survive unattended reboots.

## The schtasks trap

`schtasks /change /TN <task> /RU <user>` (interactive password prompt)
**reports SUCCESS but does NOT change LogonType.** The task stays S4U and
the stored password is ignored. Also: pressing Enter at the prompt stores an
EMPTY password, producing only a WARNING.

**Never trust schtasks SUCCESS output — verify the XML:**

```powershell
(Get-ScheduledTask -TaskName Hermes_Gateway).Principal.LogonType   # must say Password
```

## Fix

`scripts/fix-boot-logon-type.ps1` — elevated PowerShell, prompts for the
password ONCE, mutates the registered task in place (preserves BootTrigger,
battery-agnostic settings, PT0S time limit):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\fix-boot-logon-type.ps1
```

Leave `Hermes_Tray` as InteractiveToken — a tray icon requires a desktop
session by design.

## Verification — reboot test, nothing less

1. Reboot. Do NOT log in. Wait 5 minutes.
2. Message the agent from a phone.
3. Confirm boot path via timestamps:

```bash
systeminfo | grep "System Boot Time"          # boot time
wmic process where "name='pythonw.exe'" get ProcessId,CreationDate,CommandLine
# gateway CreationDate should be < 2 min after boot, well before any login
```

**Proven on lappy:** boot 11:09:45 → watchdog 11:10:00 (+15s) → gateway
11:10:36 (+51s). No login.

## Trade-offs of Password logon

- Stored password must be re-armed if the Windows password changes (local
  accounts with password-never-expires: negligible).
- Full logon session with network credentials (S4U tokens have none) — fine
  for the gateway, which needs network access anyway.

## Prevention

Any future task registration for boot-time start must use
`-LogonType Password` (with stored credential), not S4U. Do not "simplify"
back to S4U — that's the bug.
