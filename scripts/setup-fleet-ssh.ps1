# setup-fleet-ssh.ps1
# Canonical fleet SSH setup script for Windows OpenSSH Server.
# Run from an ADMIN PowerShell (right-click Start > Windows PowerShell (Admin)):
#   powershell -ExecutionPolicy Bypass -File setup-fleet-ssh.ps1 -FleetKey "ssh-ed25519 AAAA..."
#
# ASCII-only -- no Unicode characters (PowerShell 5.x reads .ps1 as ANSI without BOM).
# Tested on: Windows 10/11 Pro, Enterprise. Home edition may need winget fallback.
#
# Locale-independent: uses SIDs for ACLs (works on English, German, French, Japanese, etc.)

#Requires -Version 5.1
#Requires -RunAsAdministrator

param(
    # Fleet operator public keys to authorize. Pass multiple: -FleetKey "key1" -FleetKey "key2"
    # If you don't have fleet keys yet, run without this and send YOUR key to fleet operators first.
    [string[]]$FleetKey = @(),
    # Skip sshd restart (use when running this script OVER SSH to avoid disconnecting yourself)
    [switch]$SkipRestart
)

$ErrorActionPreference = "Stop"

# Locale-independent SIDs (critical: group names are localized on non-English Windows)
$SID_SYSTEM = "*S-1-5-18"
$SID_ADMINISTRATORS = "*S-1-5-32-544"

Write-Host "========================================"
Write-Host " Fleet SSH Setup"
Write-Host "========================================"
Write-Host ""

# ---------------------------------------------------------------------------
# 1. Check prerequisites
# ---------------------------------------------------------------------------
Write-Host "[1/9] Checking prerequisites..."

# Check PowerShell version
Write-Host "  PowerShell $($PSVersionTable.PSVersion): OK"

# Check if winget is available (for fallback install path)
$wingetAvailable = $false
if (Get-Command winget -ErrorAction SilentlyContinue) {
    $wingetAvailable = $true
    Write-Host "  winget available: OK (fallback path)"
} else {
    Write-Host "  winget not found (will use DISM or manual install only)"
}

# ---------------------------------------------------------------------------
# 2. Install OpenSSH Client AND Server if missing
# ---------------------------------------------------------------------------
Write-Host "[2/9] Installing OpenSSH (Client + Server)..."

$needClient = $false
$needServer = $false

try {
    $capabilities = Get-WindowsCapability -Online -ErrorAction Stop |
        Where-Object { $_.Name -like "OpenSSH.*" }

    $clientCap = $capabilities | Where-Object { $_.Name -like "OpenSSH.Client*" }
    $serverCap = $capabilities | Where-Object { $_.Name -like "OpenSSH.Server*" }

    if (-not $clientCap) {
        Write-Host "  OpenSSH Client not found in capabilities (may need winget)"
        $needClient = $true
    } elseif ($clientCap.State -ne "Installed") {
        Write-Host "  OpenSSH Client: Not installed"
        $needClient = $true
    } else {
        Write-Host "  OpenSSH Client: Already installed"
    }

    if (-not $serverCap) {
        Write-Host "  OpenSSH Server not found in capabilities (may need winget)"
        $needServer = $true
    } elseif ($serverCap.State -ne "Installed") {
        Write-Host "  OpenSSH Server: Not installed"
        $needServer = $true
    } else {
        Write-Host "  OpenSSH Server: Already installed"
    }
} catch {
    Write-Host "  WARNING: Could not query Windows capabilities (DISM issue)."
    Write-Host "  This may be a Home edition or Windows Update is blocked."
    $needClient = $true
    $needServer = $true
}

# Install via DISM first (if capabilities are known)
if ($needClient -and $clientCap -and $clientCap.Name) {
    Write-Host "  Installing Client via DISM..."
    try {
        Add-WindowsCapability -Online -Name $clientCap.Name -ErrorAction Stop | Out-Null
        Write-Host "  Client installed: OK"
        $needClient = $false
    } catch {
        Write-Host "  Client DISM install failed: $_"
    }
}

if ($needServer -and $serverCap -and $serverCap.Name) {
    Write-Host "  Installing Server via DISM..."
    try {
        Add-WindowsCapability -Online -Name $serverCap.Name -ErrorAction Stop | Out-Null
        Write-Host "  Server installed: OK"
        $needServer = $false
    } catch {
        Write-Host "  Server DISM install failed: $_"
    }
}

# Winget fallback for anything still needed
if (($needClient -or $needServer) -and $wingetAvailable) {
    Write-Host "  Trying winget fallback..."
    try {
        winget install --id Microsoft.OpenSSH.Beta --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
        Write-Host "  winget install: OK"
        $needClient = $false
        $needServer = $false
    } catch {
        Write-Host "  winget fallback failed."
    }
}

# If anything is still missing, manual instructions
if ($needClient -or $needServer) {
    Write-Host ""
    Write-Host "  === MANUAL INSTALL REQUIRED ==="
    $missing = @()
    if ($needClient) { $missing += "OpenSSH Client" }
    if ($needServer) { $missing += "OpenSSH Server" }
    Write-Host "  Could not install: $($missing -join ', ')"
    Write-Host "  Settings > Apps > Optional Features > View features"
    Write-Host "  Search 'OpenSSH' and install both Client and Server."
    Write-Host "  Then re-run this script."
    Write-Host "  Or: https://github.com/PowerShell/Win32-OpenSSH/releases"
    Write-Host "  ==============================="
    throw "OpenSSH installation incomplete. Manual install required for: $($missing -join ', ')"
}

# Verify ssh-keygen and ssh are now available
$sshKeygen = Get-Command ssh-keygen -ErrorAction SilentlyContinue
$sshExe = Get-Command ssh -ErrorAction SilentlyContinue
if (-not $sshKeygen) {
    Write-Host "  ERROR: ssh-keygen not available after install. Try rebooting."
    throw "ssh-keygen not available"
}
if (-not $sshExe) {
    Write-Host "  ERROR: ssh not available after install. Try rebooting."
    throw "ssh not available"
}
Write-Host "  ssh-keygen + ssh verified: OK"

# ---------------------------------------------------------------------------
# 3. Ensure sshd is running and set to auto-start
# ---------------------------------------------------------------------------
Write-Host "[3/9] Configuring sshd service..."

$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if (-not $sshd) {
    Write-Host "  ERROR: sshd service not found after installation. Try rebooting and re-running."
    throw "sshd service not found"
}

Set-Service -Name sshd -StartupType Automatic
Write-Host "  Startup type: Automatic"

if ($sshd.Status -ne "Running") {
    Write-Host "  Starting sshd..."
    Start-Service sshd
    Start-Sleep -Seconds 3
} else {
    Write-Host "  Already running: OK"
}

# Verify it's listening (locale-independent, not deprecated)
$tcpListen = Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue
if ($tcpListen) {
    Write-Host "  Listening on port 22: OK"
} else {
    Write-Host "  WARNING: sshd is running but port 22 is not listening."
    Write-Host "  Check: Get-NetTCPConnection -LocalPort 22"
    Write-Host "  Check: Get-Content C:\\ProgramData\\ssh\\logs\\sshd.log -Tail 20"
}

# ---------------------------------------------------------------------------
# 4. Check / fix firewall rules
# ---------------------------------------------------------------------------
Write-Host "[4/9] Checking firewall rules..."

# Search for SSH firewall rules using netsh (most compatible across PS versions)
$fwCheck = netsh advfirewall firewall show rule name=all dir=in 2>&1 | Out-String
$hasPort22 = $fwCheck -match "LocalPort.*\b22\b"

if ($hasPort22) {
    Write-Host "  Port 22 already allowed in firewall: OK"
} else {
    Write-Host "  No port 22 rule found. Creating..."
    try {
        netsh advfirewall firewall add rule name="sshd" dir=in action=allow protocol=TCP localport=22 2>&1 | Out-Null
        Write-Host "  Firewall rule created: OK"
    } catch {
        Write-Host "  WARNING: Could not create firewall rule: $_"
        Write-Host "  SSH may be blocked by Windows Firewall."
    }
}

# Check active firewall profiles
$profiles = netsh advfirewall show currentprofile 2>&1 | Out-String
if ($profiles -match "Private.*ON") {
    Write-Host "  Firewall Private profile: ON (Tailscale OK)"
} else {
    Write-Host "  NOTE: Firewall Private profile may be off. Check: netsh advfirewall show currentprofile"
}
}

# ---------------------------------------------------------------------------
# 5. SSH key management
# ---------------------------------------------------------------------------
Write-Host "[5/9] SSH keys..."

$sshDir = "$env:USERPROFILE\\.ssh"
if (-not (Test-Path $sshDir)) {
    New-Item -Path $sshDir -ItemType Directory -Force | Out-Null
    Write-Host "  Created .ssh directory"
}

# Restrict .ssh directory permissions (owner + SYSTEM only)
$aclErrors = $false
icacls $sshDir /inheritance:r 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { $aclErrors = $true }
icacls $sshDir /grant "${env:USERNAME}:(F)" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { $aclErrors = $true }
icacls $sshDir /grant "${SID_SYSTEM}:(F)" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { $aclErrors = $true }

if ($aclErrors) {
    Write-Host "  WARNING: Could not set .ssh directory ACLs. sshd may reject keys."
}

# Check for existing ed25519 key, generate if missing
$ed25519Key = "$sshDir\\id_ed25519"
$hadExistingKey = Test-Path $ed25519Key

if (-not $hadExistingKey) {
    Write-Host "  No ed25519 key found. Generating..."
    $hostname = hostname
    # NOTE: -N '""' passes an empty string as the passphrase.
    # In PowerShell, '""' is two double-quote chars; ssh-keygen interprets this as empty.
    $null = ssh-keygen -t ed25519 -f $ed25519Key -N '""' -C "$env:USERNAME@$hostname" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: ssh-keygen failed with exit code $LASTEXITCODE"
        throw "ssh-keygen failed"
    }
    Write-Host "  Key generated: $ed25519Key"
} else {
    Write-Host "  ed25519 key exists: OK (skipping generation to avoid overwrite prompt)"
}

# Read the public key
$pubKeyContent = Get-Content "$ed25519Key.pub" -Raw
Write-Host ""
Write-Host "  === YOUR PUBLIC KEY (send to fleet operators) ==="
Write-Host "  $($pubKeyContent.Trim())"
Write-Host "  =================================================="
Write-Host ""

# ---------------------------------------------------------------------------
# 6. Add authorized keys to both locations
# ---------------------------------------------------------------------------
Write-Host "[6/9] Adding authorized keys..."

$keysAddedUser = 0
$keysAddedAdmin = 0

# --- Location 1: User-level authorized_keys ---
$userAK = "$sshDir\\authorized_keys"
Write-Host "  Location 1: $userAK"

# Always use -Force to avoid race conditions
New-Item -Path $userAK -ItemType File -Force -ErrorAction SilentlyContinue | Out-Null

# Restrict permissions (owner + SYSTEM only, sshd rejects otherwise)
$userAclOk = $true
icacls $userAK /inheritance:r 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { $userAclOk = $false }
icacls $userAK /grant "${env:USERNAME}:(F)" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { $userAclOk = $false }
icacls $userAK /grant "${SID_SYSTEM}:(F)" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { $userAclOk = $false }

if (-not $userAclOk) {
    Write-Host "  WARNING: Could not set authorized_keys ACLs."
}

# --- Location 2: Admin-level administrators_authorized_keys (THE TRAP) ---
$adminAK = "C:\\ProgramData\\ssh\\administrators_authorized_keys"
Write-Host "  Location 2: $adminAK (admin override -- sshd reads THIS for admin users)"

New-Item -Path "C:\\ProgramData\\ssh" -ItemType Directory -Force -ErrorAction SilentlyContinue | Out-Null
New-Item -Path $adminAK -ItemType File -Force -ErrorAction SilentlyContinue | Out-Null

# --- Add keys ---
# Collect keys: always add the local machine's key + any fleet keys
$keysToAdd = @($pubKeyContent.Trim())
if ($FleetKey) {
    $keysToAdd += $FleetKey | ForEach-Object { $_.Trim() } | Where-Object { $_ }
}

# Read existing keys as lines (precise comparison, not regex)
$existingUserLines = @()
if (Test-Path $userAK) {
    $existingUserLines = @(Get-Content $userAK -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
}
$existingAdminLines = @()
if (Test-Path $adminAK) {
    $existingAdminLines = @(Get-Content $adminAK -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
}

foreach ($key in $keysToAdd) {
    if (-not $key) { continue }

    # User file
    if ($key -notin $existingUserLines) {
        Add-Content $userAK $key
        $existingUserLines += $key
        $keysAddedUser++
        Write-Host "    [user] Added: $($key.Substring(0, [Math]::Min(60, $key.Length)))..."
    }

    # Admin file
    if ($key -notin $existingAdminLines) {
        Add-Content $adminAK $key
        $existingAdminLines += $key
        $keysAddedAdmin++
        Write-Host "    [admin] Added: $($key.Substring(0, [Math]::Min(60, $key.Length)))..."
    }
}

if ($keysAddedUser -eq 0 -and $keysAddedAdmin -eq 0) {
    Write-Host "  All keys already present in both locations: OK"
} else {
    Write-Host "  Keys added -- user file: $keysAddedUser, admin file: $keysAddedAdmin"
}

if (-not $FleetKey) {
    Write-Host ""
    Write-Host "  === IMPORTANT ==="
    Write-Host "  No fleet keys provided (-FleetKey). Only the local machine key was added."
    Write-Host "  Fleet operators cannot SSH in until their keys are added."
    Write-Host "  Re-run with: -FleetKey 'ssh-ed25519 AAAA...'"
    Write-Host "  Or manually add to: $adminAK"
    Write-Host "  ================="
    Write-Host ""
}

# --- Fix admin file permissions (CRITICAL -- sshd silently skips if wrong) ---
Write-Host "  Setting ACLs on administrators_authorized_keys..."

$aclOk = $true

# Take ownership
takeown /f $adminAK /a 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: takeown failed."
    $aclOk = $false
}

# Remove all inheritance
icacls $adminAK /inheritance:r 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: icacls /inheritance:r failed."
    $aclOk = $false
}

# Grant SYSTEM full control (SID-based, locale-independent)
icacls $adminAK /grant "${SID_SYSTEM}:(F)" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: icacls grant SYSTEM failed."
    $aclOk = $false
}

# Grant Administrators full control (SID-based, locale-independent)
icacls $adminAK /grant "${SID_ADMINISTRATORS}:(F)" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: icacls grant Administrators failed."
    $aclOk = $false
}

# Verify ACLs
Write-Host "  Current ACLs on admin file:"
$aclOutput = icacls $adminAK 2>&1
Write-Host "    $($aclOutput -join '; ')"

if ($aclOk) {
    Write-Host "  ACLs set: OK (SYSTEM + Administrators only)"
} else {
    Write-Host "  WARNING: ACL setup may be incomplete. Verify manually:"
    Write-Host "    icacls $adminAK"
    Write-Host "  Should show only NT AUTHORITY\\\\SYSTEM and BUILTIN\\\\Administrators."
}

# ---------------------------------------------------------------------------
# 7. Verify sshd_config
# ---------------------------------------------------------------------------
Write-Host "[7/9] Checking sshd_config..."

$sshdConfig = "C:\\ProgramData\\ssh\\sshd_config"
if (Test-Path $sshdConfig) {
    $configContent = Get-Content $sshdConfig -Raw
    if ($configContent -match "Match Group administrators") {
        Write-Host "  Match Group administrators block found: OK (admin key path is active)"
    } else {
        Write-Host "  NOTE: No Match Group administrators block. Admin users will use ~/.ssh/authorized_keys."
        Write-Host "  If key auth fails for admin users, this may be why."
    }

    # Check PubkeyAuthentication (uncommented lines only)
    $pubkeyLines = $configContent -split "`n" | Where-Object { $_ -match '^\s*PubkeyAuthentication\s' -and $_ -notmatch '^\s*#' }
    if (-not $pubkeyLines -or ($pubkeyLines -match '\sno\b')) {
        Write-Host "  WARNING: PubkeyAuthentication may not be explicitly enabled."
        Write-Host "  Default is 'yes', but if key auth fails, check sshd_config."
    }
} else {
    Write-Host "  WARNING: sshd_config not found at $sshdConfig"
}

# ---------------------------------------------------------------------------
# 8. Test local SSH
# ---------------------------------------------------------------------------
Write-Host "[8/9] Testing local SSH..."

# First connection to localhost triggers a host-key prompt. Use accept-new to handle it.
$localTestCmd = "ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=`"$sshDir\\known_hosts`" $env:USERNAME@localhost `"hostname; whoami`""
Write-Host "  Attempting: $localTestCmd"
try {
    $localTest = Invoke-Expression $localTestCmd 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  Local SSH test: PASSED"
        Write-Host "  Output: $localTest"
    } else {
        Write-Host "  Local SSH test: FAILED (exit code $LASTEXITCODE)"
        Write-Host "  Output: $localTest"
        Write-Host "  This may be expected if password auth is required. Key auth is the goal."
    }
} catch {
    Write-Host "  Local SSH test: ERROR - $_"
}

# ---------------------------------------------------------------------------
# 9. Restart sshd and verify
# ---------------------------------------------------------------------------
Write-Host "[9/9] Verifying setup..."

if ($SkipRestart) {
    Write-Host "  Skipping sshd restart (-SkipRestart was specified)"
    Write-Host "  NOTE: sshd must be restarted for key changes to take effect."
    Write-Host "  Run: Restart-Service sshd -Force"
} elseif ($env:SSH_CONNECTION -or $env:SSH_TTY) {
    Write-Host "  === WARNING ==="
    Write-Host "  Detected active SSH session. Restarting sshd will disconnect you."
    Write-Host "  Skipping restart. After this script completes, restart manually:"
    Write-Host "    Restart-Service sshd -Force"
    Write-Host "  Or re-run with -SkipRestart to suppress this warning."
    Write-Host "  ==============="
} else {
    Write-Host "  Restarting sshd..."
    Restart-Service sshd -Force

    # Poll for service to come back up
    $maxWait = 30
    $elapsed = 0
    while ($elapsed -lt $maxWait) {
        Start-Sleep -Seconds 2
        $elapsed += 2
        $currentStatus = (Get-Service sshd -ErrorAction SilentlyContinue).Status
        if ($currentStatus -eq "Running") {
            Write-Host "  sshd restarted and running (took ${elapsed}s): OK"
            break
        }
    }

    if ((Get-Service sshd -ErrorAction SilentlyContinue).Status -ne "Running") {
        Write-Host "  ERROR: sshd failed to restart within ${maxWait}s."
        Write-Host "  Check logs: Get-Content C:\\ProgramData\\ssh\\logs\\sshd.log -Tail 30"
        throw "sshd restart failed"
    }
}

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================"
Write-Host " SETUP COMPLETE"
Write-Host "========================================"
Write-Host ""
Write-Host "Machine:   $(hostname)"
Write-Host "User:      $env:USERNAME"

# Find routable IPs (locale-independent)
$routableIPs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*" -and
        $_.PrefixOrigin -ne "WellKnown"
    } |
    Select-Object -ExpandProperty IPAddress

if ($routableIPs) {
    Write-Host "IP(s):     $($routableIPs -join ', ')"
}
Write-Host "SSH Port:  22"
Write-Host "sshd:      $((Get-Service sshd -ErrorAction SilentlyContinue).Status)"
Write-Host ""
Write-Host "=== FLEET OPERATORS CAN NOW CONNECT ==="
Write-Host "(From a Linux/Mac fleet machine, not this Windows machine):"
Write-Host "  ssh -o StrictHostKeyChecking=no $env:USERNAME@<tailscale-ip> 'hostname'"
Write-Host ""
Write-Host "=== YOUR PUBLIC KEY ==="
Write-Host "  $($pubKeyContent.Trim())"
Write-Host "========================"
Write-Host ""
Write-Host "Fleet keys provided: $(if ($FleetKey) { $FleetKey.Count } else { 0 })"
if (-not $FleetKey) {
    Write-Host "Re-run with fleet operator keys:"
    Write-Host "  -FleetKey 'ssh-ed25519 AAAA...' -FleetKey 'ssh-ed25519 AAAA...'"
}
Write-Host ""
