# Fleet SSH Setup: Windows OpenSSH Server

**A canonical guide for getting SSH working on a Windows machine, purpose-built for Hermes fleet operators.** Covers every known pitfall across multiple machine deployments.

**Prerequisites:** Windows 10 version 1809+ or Windows 11. Pro/Enterprise/Education editions have native OpenSSH support. Home edition users may need the winget fallback path (included in the script).

---

## How to Open PowerShell as Administrator

**Right-click the Start button > Windows PowerShell (Admin) / Terminal (Admin)**

Or press **Win+X** then **A**.

All commands in this guide assume you're running from an admin PowerShell window.

---

## The Big Gotcha (Read This First)

Windows OpenSSH has a trap: if you're an Administrator, **your `~\.ssh\authorized_keys` file is completely ignored.**

OpenSSH on Windows adds this to `sshd_config` by default:

```
Match Group administrators
       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
```

For any admin user, sshd reads `C:\ProgramData\ssh\administrators_authorized_keys` instead of `~\.ssh\authorized_keys`. And that file has **brutal permission requirements** — if *anyone* other than SYSTEM or Administrators can read it, sshd silently ignores it. No error in the logs. No warning. Just "Permission denied."

**The fix:** Your fleet operator's public key must be in `C:\ProgramData\ssh\administrators_authorized_keys` with strict ACLs. The script handles this automatically. If doing it manually, follow Step 5 carefully.

---

## Option A: The Automated Script (Recommended)

### Step 1: Get the script onto your machine

Have your fleet operator send you the script file, or download it from the fleet repo:

```powershell
# Replace URL with the actual fleet repo location
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/thegizmopro/hermes-scripts/main/fleet/setup-fleet-ssh.ps1" -OutFile "$env:TEMP\setup-fleet-ssh.ps1"
```

Or the fleet operator can paste it into a file on your machine.

### Step 2: Run it

```powershell
# With fleet operator keys (recommended):
& "$env:TEMP\setup-fleet-ssh.ps1" -FleetKey "ssh-ed25519 AAAA... holly@thinkzo" -FleetKey "ssh-ed25519 AAAA... mini@lappy"

# If running OVER SSH (skips the restart that would disconnect you):
& "$env:TEMP\setup-fleet-ssh.ps1" -FleetKey "ssh-ed25519 AAAA..." -SkipRestart
```

**What the script does (9 steps):**
1. Checks prerequisites (PowerShell 5.1+, winget availability)
2. Installs **both** OpenSSH Client and Server (DISM, then winget fallback, then manual instructions)
3. Starts and enables sshd, verifies port 22 listening
4. Checks and fixes Windows Firewall (Private profile only — safe for laptops on public WiFi)
5. Generates an ed25519 key if missing (skips if key exists — no overwrite prompt)
6. Adds keys to **both** `~/.ssh/authorized_keys` and `C:\ProgramData\ssh\administrators_authorized_keys`
7. Fixes ACLs on all SSH directories and files using SIDs (locale-independent)
7b. Hardens sshd_config (PasswordAuthentication no, PubkeyAuthentication yes)
8. Runs a local SSH test (handles first-connection host-key prompt)
9. Restarts sshd with poll-based verification, warns if running over SSH

### Step 3: Send your public key to fleet operators

The script prints your public key. Copy it and send it to your fleet operators.

---

## Option B: Manual Setup (Step by Step)

### 1. Install OpenSSH Client AND Server

```powershell
# Check what's installed
Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.*' | Format-Table Name, State

# Install both using the discovered names:
$caps = Get-WindowsCapability -Online | Where-Object { $_.Name -like 'OpenSSH.*' -and $_.State -ne 'Installed' }
if ($caps) {
    foreach ($cap in $caps) {
        Write-Host "Installing: $($cap.Name)"
        Add-WindowsCapability -Online -Name $cap.Name
    }
}

# If DISM fails (Home edition, blocked), try winget:
# (Only if winget is installed — check with: Get-Command winget)
winget install --id Microsoft.OpenSSH.Beta --accept-source-agreements

# Start and enable the service
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

# Verify both tools are available
Get-Command ssh
Get-Command ssh-keygen
```

> **Note:** Both Client AND Server are needed. Client gives you `ssh.exe` and `ssh-keygen.exe`. Server gives you `sshd`. The manual install via Settings > Apps > Optional Features also works if the above fails.

### 2. Verify Firewall

OpenSSH usually creates the rule automatically. Check:

```powershell
# Look for any port-22 inbound rule
Get-NetFirewallRule -Direction Inbound |
    Where-Object { $_.Enabled -eq $True } |
    Get-NetFirewallPortFilter |
    Where-Object { $_.LocalPort -eq 22 }
```

If missing, create it (Private profile only — never open SSH on Public profile):

```powershell
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' `
    -Enabled True -Direction Inbound -Protocol TCP -Action Allow `
    -LocalPort 22 -Profile Private
```

### 3. Generate an SSH Key (if you don't have one)

```powershell
$ed25519Key = "$env:USERPROFILE\.ssh\id_ed25519"
if (-not (Test-Path $ed25519Key)) {
    # 6 double-quotes = empty passphrase (safest pattern across PS5/7)
    ssh-keygen -t ed25519 -f $ed25519Key -N """""" -C "$env:USERNAME@$(hostname)"
} else {
    Write-Host "Key already exists — skipping to avoid overwrite"
}
```

> **Important:** Check if a key exists before running ssh-keygen. If you run it on an existing key, it prompts "Overwrite (y/n)?" and hangs waiting for input.

### 4. Send Your Public Key to Fleet Operators

```powershell
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
```

Copy the entire line starting with `ssh-ed25519` and send it to whoever needs SSH access.

### 5. Add Fleet Keys (the trap-aware way)

For admin users, you MUST add keys to `C:\ProgramData\ssh\administrators_authorized_keys`. The `~\.ssh\authorized_keys` file is ignored for admins by default.

```powershell
$key = "ssh-ed25519 AAAA... operator@their-machine"
$adminAK = "C:\ProgramData\ssh\administrators_authorized_keys"

# Create the file if it doesn't exist
if (-not (Test-Path $adminAK)) {
    New-Item $adminAK -ItemType File -Force | Out-Null
}

# Add the key (skip if already present)
$existing = if (Test-Path $adminAK) { @(Get-Content $adminAK | Where-Object { $_.Trim() }) } else { @() }
if ($key -notin $existing) {
    Add-Content $adminAK $key
}

# Do the same for the user-level file (good practice, covers non-admin scenarios)
$userAK = "$env:USERPROFILE\.ssh\authorized_keys"
if (-not (Test-Path $userAK)) { New-Item $userAK -ItemType File -Force | Out-Null }
$existingUser = if (Test-Path $userAK) { @(Get-Content $userAK | Where-Object { $_.Trim() }) } else { @() }
if ($key -notin $existingUser) { Add-Content $userAK $key }
```

### 6. Fix Permissions on the Admin File (CRITICAL)

This is the step everyone gets wrong. The file must have ONLY SYSTEM + Administrators — no inherited permissions, no Authenticated Users, no Users group. If anyone else can read it, sshd silently ignores the file.

All commands use SIDs so this works on non-English Windows too.

```powershell
$adminAK = "C:\ProgramData\ssh\administrators_authorized_keys"

# Take ownership first (in case you don't own the file)
takeown /f $adminAK /a

# Remove all inherited permissions
icacls $adminAK /inheritance:r

# Grant SYSTEM full control (SID: S-1-5-18 — locale-independent)
icacls $adminAK /grant "*S-1-5-18:(F)"

# Grant Administrators full control (SID: S-1-5-32-544 — locale-independent)
icacls $adminAK /grant "*S-1-5-32-544:(F)"

# Verify — should show only these two entries
icacls $adminAK
```

**Expected output:**
```
C:\ProgramData\ssh\administrators_authorized_keys
    NT AUTHORITY\SYSTEM:(F)
    BUILTIN\Administrators:(F)
```

If you see `Authenticated Users`, `Users`, or any inherited entry (`OI`, `CI`, `IO` flags), the fix didn't take. Repeat the `icacls /inheritance:r` step.

### 7. Set permissions on ~\.ssh directory and authorized_keys

```powershell
$sshDir = "$env:USERPROFILE\.ssh"
if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory $sshDir -Force | Out-Null }

# Restrict directory
icacls $sshDir /inheritance:r
icacls $sshDir /grant "$($env:USERNAME):(F)"
icacls $sshDir /grant "*S-1-5-18:(F)"

# Restrict user authorized_keys
$userAK = "$sshDir\authorized_keys"
if (-not (Test-Path $userAK)) { New-Item $userAK -ItemType File -Force | Out-Null }
icacls $userAK /inheritance:r
icacls $userAK /grant "$($env:USERNAME):(F)"
icacls $userAK /grant "*S-1-5-18:(F)"
```

> **Note:** If `$sshDir` doesn't exist yet (no key generated), create it first. The `if` guard above handles this.

### 7b. Harden sshd_config (key-only auth)

A key-only fleet should not have password auth sitting open. Append to the bottom of `C:\ProgramData\ssh\sshd_config`:

```powershell
$sshdConfig = "C:\ProgramData\ssh\sshd_config"
Add-Content $sshdConfig ""
Add-Content $sshdConfig "PasswordAuthentication no"
Add-Content $sshdConfig "PubkeyAuthentication yes"
```

> **Why:** Windows OpenSSH defaults to allowing password auth. If someone guesses or brute-forces a password, they're in. Key-only auth eliminates that attack surface entirely. The script does this automatically.

### 8. Restart sshd

```powershell
Restart-Service sshd -Force

# Wait a moment, then verify
Start-Sleep -Seconds 3
Get-Service sshd | Select Name, Status, StartType
Get-NetTCPConnection -LocalPort 22 -State Listen
```

### 9. Test Locally First

```powershell
# First connection triggers a host-key prompt. accept-new handles this:
ssh -o StrictHostKeyChecking=accept-new $env:USERNAME@localhost "hostname; whoami"
```

> **If you see a prompt asking about host authenticity:** This is normal for the first connection ever. Type `yes` and press Enter. After this first time, it won't ask again. The `accept-new` flag above automates this.

If key auth works, you're done. If not, enable debug logging before troubleshooting with a fleet operator.

### 10. Test from a Fleet Machine

From the fleet operator's Linux/Mac machine (NOT the Windows machine being set up):

```bash
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes \
    <user>@<tailscale-ip> "hostname; whoami"
```

---

## For Fleet Operators: SSH-ing from Hermes

When Hermes runs as a Windows service, it runs as SYSTEM — not your user. This breaks SSH in several ways.

**The command that works:**

```bash
ssh -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -i /c/Users/<user>/.ssh/id_ed25519 \
    -o IdentitiesOnly=yes \
    <user>@<tailscale-ip> "<powershell-command>"
```

**Why each flag:**
- `StrictHostKeyChecking=no` — accept host keys without prompting (SYSTEM has no known_hosts)
- `UserKnownHostsFile=/dev/null` — don't try to write known_hosts (doesn't exist for SYSTEM)
- `-i /c/Users/<user>/.ssh/<key>` — explicit key path (SYSTEM has none)
- `IdentitiesOnly=yes` — don't try other keys from nonexistent keyrings
- `ConnectTimeout=10` — fail fast if unreachable

> **Security note:** `StrictHostKeyChecking=no` disables MITM protection for host keys. Safe within Tailscale's encrypted overlay network. Do NOT use these settings on the open internet.

**Remote shell is PowerShell — use `;` not `&&`:**

```bash
# RIGHT — semicolons work in PowerShell
ssh kenzo@100.113.84.24 "hostname; Get-Date; Get-Process python"

# WRONG — && is bash syntax, breaks in PowerShell
ssh kenzo@100.113.84.24 "hostname && Get-Date"  # ParserError
```

---

## Fleet SSH Config Pattern

On each operator machine, `~/.ssh/config`:

```
Host thinkpad13
    HostName 100.113.84.24
    User john
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ConnectTimeout 10
```

Then SSH is just: `ssh thinkpad13 "hostname"`

---

## Troubleshooting

### Quick local diagnostic

```powershell
# Is sshd running?
Get-Service sshd | Select Name, Status, StartType

# Is it listening?
Get-NetTCPConnection -LocalPort 22 -State Listen

# Is the firewall open?
Get-NetFirewallRule -Direction Inbound |
    Get-NetFirewallPortFilter |
    Where-Object { $_.LocalPort -eq 22 } |
    ForEach-Object { Get-NetFirewallRule -InstanceID $_.InstanceID } |
    Format-Table Name, Enabled, DisplayName

# What does sshd_config say?
Get-Content C:\ProgramData\ssh\sshd_config
```

### Symptom -> Cause Lookup

| Symptom | Likely Cause | Diagnostic |
|---------|-------------|------------|
| `Connection timed out` | Firewall blocking OR machine unreachable | Check firewall rule + `Get-NetTCPConnection -LocalPort 22` |
| `Connection refused` | sshd service not running OR wrong port | `Get-Service sshd` |
| `Permission denied (publickey)` — admin user | Key missing from `administrators_authorized_keys` | Check `C:\ProgramData\ssh\administrators_authorized_keys` |
| `Permission denied (publickey)` — key IS in admin file | ACLs wrong on admin file | `icacls C:\ProgramData\ssh\administrators_authorized_keys` |
| `Permission denied (publickey)` — non-admin user | `~/.ssh/authorized_keys` missing or wrong ACLs | Check file exists + `icacls ~/.ssh/authorized_keys` |
| `Permission denied (all methods)` | Key format issue, sshd misconfiguration | Enable debug logging (see below) |
| SSH hangs silently from Hermes | Host key verification prompt (SYSTEM context) | Use `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` |
| `ssh: The term 'ssh' is not recognized` | OpenSSH Client not installed | Install via Settings > Optional Features > OpenSSH Client |
| Remote commands error with `&&` | Remote shell is PowerShell | Use `;` not `&&` |
| Script reports "Manual install required" | Home edition or DISM blocked | Install via Settings > Apps > Optional Features > OpenSSH |
| `icacls` fails with access denied | Don't own the file | Run `takeown /f <path> /a` first |

### Debug mode (for stubborn permission problems)

```powershell
# 1. Enable debug logging
# Open C:\ProgramData\ssh\sshd_config as Administrator:
#   Start Notepad as Administrator > File > Open > C:\ProgramData\ssh\sshd_config
# Add or change:
#   LogLevel DEBUG3
# Save and exit.

# 2. Restart sshd
Restart-Service sshd -Force

# 3. Try connecting, then check the logs
Get-ChildItem C:\ProgramData\ssh\logs\* -ErrorAction SilentlyContinue | Get-Content -Tail 50

# 4. AFTER fixing the issue, set log level back to INFO and restart
#    (DEBUG3 generates massive log files — don't leave it on)
```

---

## Reference: Tailscale IP Discovery

To find the Tailscale IP of the machine:

```powershell
# If Tailscale CLI is installed:
tailscale ip -4

# Otherwise, check all IPs:
ipconfig | Select-String "IPv4"
```

---

## What the Script Checks That Most Guides Miss

- **Both Client and Server** — not just Server (you need `ssh.exe` to test)
- **Locale-independent ACLs** — uses SIDs (`*S-1-5-18`, `*S-1-5-32-544`) not English group names
- **Home edition fallback** — tries winget if DISM fails
- **SSH session detection** — warns if you're running over SSH and need -SkipRestart
- **.ssh directory permissions** — not just the admin file
- **Every icacls exit code checked** — no silently swallowed ACL failures
- **Poll-based restart verification** — waits up to 30s for sshd to come back
- **Precise key deduplication** — compares by exact line, not regex (won't false-match substrings)
- **Both firewall search paths** — finds existing rules before creating new ones
- **Local SSH test** — handles first-connection host-key prompt automatically
- **Winget pre-check** — verifies winget exists before trying the fallback path
