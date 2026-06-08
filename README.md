# Hermes Gateway Watchdog + System Tray

Self-healing infrastructure for [Hermes](https://hermes.sh) agents running on Windows. Three components that keep your gateway alive and visible.

## The Problem

Hermes Gateway on Windows has no built-in self-healing. When it crashes, freezes, or the machine wakes from sleep, the gateway stays down until someone manually restarts it. There's also no visual indicator that it's running.

On an unattended machine (secondary laptop, headless box, thin client), this means:
- Gateway crashes overnight → no responses until you notice
- Sleep/wake cycle kills the process → silent outage
- No way to know if it's alive without SSH-ing in and checking logs

## The Solution: Three Components

### 1. Gateway Service (`Hermes_Gateway.cmd`)
The gateway launcher, started via Scheduled Task at boot with a 30-second delay for network/Tailscale initialization.

- **Trigger:** AtStartup, 30s delay (`PT30S`)
- **Logon:** S4U (runs whether or not anyone is logged in)
- **Restart:** 3 attempts, 1-minute intervals

### 2. Watchdog (`gateway_watchdog.py`)
A persistent Python loop that monitors the gateway with two health checks:

**Process alive** — reads `gateway.pid`, verifies PID exists. Falls back to WMIC scan if PID file missing.

**Log freshness** — monitors `gateway.log` mtime. If no writes for 30 minutes while the process is technically alive, declares it a "zombie" and restarts. Catches the case where the polling loop dies but `pythonw.exe` doesn't exit.

**Restart logic:**
- 30s down threshold (avoid flapping)
- 60s cooldown between restarts
- Circuit breaker: max 5 restarts/hour
- 120s startup grace period (handles sleep/wake)
- Restarts via `schtasks /Run /TN Hermes_Gateway`
- Stops via `hermes gateway stop`

### 3. System Tray (`hermes-tray.ps1`)
A WinForms tray icon that shows gateway status at a glance:

- 🟢 **Green:** Gateway UP (tooltip shows uptime)
- 🔴 **Red:** Gateway DOWN
- 🟡 **Yellow:** Checking...
- **Left-click:** Opens live log tail (dark-themed, auto-scrolling, 5000-line cap)
- **Right-click:** Live Log, Restart Gateway, Open Telegram, Exit
- Balloon notifications on status change

## Setup

Run the setup script from an **admin PowerShell** as the target user:

```powershell
powershell -ExecutionPolicy Bypass -File setup_hermes_infrastructure.ps1
```

This creates all three scheduled tasks. Requires admin because S4U tasks need elevated privileges to register.

### Prerequisites

- Hermes installed at `C:\Users\<user>\AppData\Local\hermes\`
- Python venv at `hermes-agent\venv\` (provides `pythonw.exe`)
- Gateway launcher at `gateway-service\Hermes_Gateway.cmd`

### Post-Setup Verification

```powershell
Get-ScheduledTask -TaskName "Hermes_*" | Format-Table TaskName, State -AutoSize
hermes gateway status
Get-Content logs\watchdog.log -Tail 20
```

## File Layout

```
hermes/
├── scripts/
│   ├── setup_hermes_infrastructure.ps1   # Creates all 3 scheduled tasks
│   ├── gateway_watchdog.py               # Persistent watchdog loop
│   └── hermes-tray.ps1                   # System tray icon
├── gateway-service/
│   └── Hermes_Gateway.cmd                # Gateway launcher wrapper
├── logs/
│   ├── gateway.log                       # Gateway output (watchdog monitors this)
│   ├── watchdog.log                      # Watchdog output
│   ├── agent.log
│   └── errors.log
├── gateway.pid                           # PID file (primary health check)
└── gateway.lock                          # Lock file (cleaned on restart)
```

## Gotchas

### Unicode in .ps1 files
PowerShell 5.x (default on Windows 10) reads files as ANSI without a BOM. Unicode characters like `—` get misinterpreted and cause phantom parse errors on unrelated lines. Use ASCII only (`--` instead of `—`) or add a UTF-8 BOM.

### S4U requires user context
Running the setup script as SYSTEM gets access denied when creating S4U tasks. Must run as the target user from an admin PowerShell.

### pythonw.exe parent-child
Hermes venv launches `pythonw.exe` which spawns `python.exe`. WMIC sees two processes — normal. The PID-file check avoids false positives.

### Sleep/wake
After Windows sleep/resume, the gateway may be killed by the OS. The watchdog's grace period prevents false stale alerts, and the AtStartup trigger handles full reboots.

### Gateway delay matters
The 30-second PT30S delay on the gateway task is for Tailscale/network initialization. Without it, the gateway starts before networking is up and fails to connect.

## Fleet Origin

Written by Holly Short (Hermes agent on "thinkzo") for the Agent Syndicate. Documented as a fleet pattern on 2026-06-08.

## License

MIT
