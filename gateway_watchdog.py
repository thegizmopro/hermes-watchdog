"""
Hermes Gateway Watchdog for Windows (v2.2)
Runs persistently, monitors gateway, restarts if down or frozen.
Designed to be started via Scheduled Task (AtStartup trigger).

v2.2 changes (2026-06-08):
  - Replaced log staleness with cron tick lock heartbeat as primary zombie check
  - cron/.tick.lock is touched every 60s by the gateway's event loop ticker
  - This eliminates false positives when the gateway is healthy but idle (no messages)
  - Old log staleness kept as a secondary backstop at very high threshold (4h)
  - Fixed post-restart grace period (applies after ANY restart, not just watchdog start)

v2.1 changes (2026-06-07):
  - Primary health check uses gateway.pid file, not WMIC scan
  - WMIC scan only used as fallback (fixes false dual-process detection)
  - pythonw.exe + python.exe parent-child = ONE gateway (uv venv behavior)
  - kill_gateway() uses `hermes gateway stop` (handles PID/state/scheduled task)
  - start_gateway() uses `schtasks /Run` (single process chain, no duplicates)
  - Explicit HERMES_HOME env var in all subprocess calls

Three-tier health check:
  1. PID file check — is the process from gateway.pid alive?
  2. Cron tick lock heartbeat — has cron/.tick.lock been touched recently?
     (Catches "zombie" state: process alive but event loop dead.)
  3. Log freshness backstop — has gateway.log been written to in 4 hours?
     (Last-resort catch for pathological edge cases.)
"""

import subprocess
import time
import os
import sys
import json
from datetime import datetime

CHECK_INTERVAL = 10        # seconds between checks
DOWN_THRESHOLD = 30        # seconds of process missing before restarting

# Primary zombie check: cron tick lock heartbeat
# The gateway's cron ticker touches cron/.tick.lock every 60s.
# If it hasn't been touched in 5 minutes (5 missed ticks), the event loop is dead.
TICK_STALE_THRESHOLD = 300  # 5 minutes

# Secondary backstop: log freshness (only fires after 4 hours of silence)
# This catches pathological cases where the ticker somehow runs but nothing else works.
LOG_STALE_THRESHOLD = 14400  # 4 hours

RESTART_COOLDOWN = 60      # minimum seconds between restarts
MAX_RESTARTS_PER_HOUR = 5  # circuit breaker
STARTUP_GRACE = 120        # seconds after restart before heartbeat checks (handles startup + sleep/wake)

HERMES_HOME = r"C:\Users\kenzo\AppData\Local\hermes"
HERMES_EXE = r"C:\Users\kenzo\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe"
GATEWAY_PID_FILE = os.path.join(HERMES_HOME, "gateway.pid")
GATEWAY_LOG = os.path.join(HERMES_HOME, "logs", "gateway.log")
CRON_TICK_LOCK = os.path.join(HERMES_HOME, "cron", ".tick.lock")
WATCHDOG_LOG = os.path.join(HERMES_HOME, "logs", "watchdog.log")
GATEWAY_TASK = "Hermes_Gateway"

# State files that can go stale and cause issues
STATE_FILES = ["gateway.pid", "gateway.lock"]

# For fallback WMIC scan only
GATEWAY_MARKERS = ["gateway run"]
GATEWAY_EXCLUDES = ["gateway_watchdog", "wmic"]

down_since = None
tick_stale_since = None
log_stale_since = None
last_restart = 0
restart_times = []
watchdog_start_time = time.time()


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass  # pythonw.exe has no stdout
    try:
        os.makedirs(os.path.dirname(WATCHDOG_LOG), exist_ok=True)
        with open(WATCHDOG_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _hermes_env():
    """Return a copy of os.environ with HERMES_HOME explicitly set."""
    env = os.environ.copy()
    env["HERMES_HOME"] = HERMES_HOME
    return env


# ---- PID-based health check (primary) ----

def get_gateway_pid_from_file():
    """Read the gateway PID from the state file."""
    try:
        with open(GATEWAY_PID_FILE, "r") as f:
            data = json.load(f)
            return data.get("pid")
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def is_pid_alive(pid):
    """Check if a Windows process exists by PID."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        return str(pid) in result.stdout and "No tasks" not in result.stdout
    except Exception:
        return False


# ---- WMIC-based fallback (for kill operations) ----

def _find_gateway_pids_wmic():
    """Return list of PIDs for processes that look like the gateway.
    Used for force-kill operations, not for health checking."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "(name='python.exe' or name='pythonw.exe')",
             "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000
        )
    except Exception as e:
        log(f"WMIC error: {e}")
        return []

    found = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "ProcessId" in line:
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        cmdline, pid_str = parts
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        cmdline_lower = cmdline.lower()
        if any(m in cmdline_lower for m in GATEWAY_MARKERS) and \
           not any(x in cmdline_lower for x in GATEWAY_EXCLUDES):
            found.append(pid)
    return found


def is_gateway_running():
    """Check if gateway is alive using PID file (primary).
    Falls back to WMIC scan if PID file is missing."""
    # Primary: check PID file
    pid = get_gateway_pid_from_file()
    if pid is not None:
        return is_pid_alive(pid)
    
    # Fallback: scan for gateway processes (no PID file yet)
    pids = _find_gateway_pids_wmic()
    return len(pids) > 0


def get_file_age(path):
    """Return age of a file in seconds, or None if file missing."""
    try:
        mtime = os.path.getmtime(path)
        return time.time() - mtime
    except OSError:
        return None


def kill_gateway():
    """Stop the gateway cleanly using `hermes gateway stop`.
    This handles: process termination, PID file cleanup, scheduled task stop.
    Falls back to taskkill for any stragglers."""
    # Step 1: Official stop command (stops scheduled task + drains process + cleans state)
    try:
        result = subprocess.run(
            [HERMES_EXE, "gateway", "stop"],
            capture_output=True, text=True, timeout=30,
            env=_hermes_env(),
            creationflags=0x08000000
        )
        output = (result.stdout + result.stderr).strip()
        if output:
            log(f"hermes gateway stop: {output}")
    except subprocess.TimeoutExpired:
        log("hermes gateway stop timed out, using force kill")
    except Exception as e:
        log(f"hermes gateway stop error: {e}")

    # Step 2: Wait, then force-kill any surviving processes
    time.sleep(3)
    pids = _find_gateway_pids_wmic()
    if pids:
        log(f"Stragglers alive after stop, force-killing: {pids}")
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                    creationflags=0x08000000
                )
                log(f"Force-killed PID {pid}")
            except Exception as e:
                log(f"Error force-killing PID {pid}: {e}")
        time.sleep(2)

    # Step 3: Clean up stale state files
    for state_file in STATE_FILES:
        path = os.path.join(HERMES_HOME, state_file)
        try:
            if os.path.exists(path):
                os.remove(path)
                log(f"Removed stale {state_file}")
        except Exception as e:
            log(f"Could not remove {state_file}: {e}")


def start_gateway():
    """Start the gateway via the scheduled task.
    Uses schtasks /Run to trigger Hermes_Gateway, which creates a single
    pythonw.exe -> python.exe process chain (no duplicates)."""
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", GATEWAY_TASK],
            capture_output=True, text=True, timeout=15,
            env=_hermes_env(),
            creationflags=0x08000000
        )
        output = (result.stdout + result.stderr).strip()
        if output:
            log(f"Started {GATEWAY_TASK} task: {output}")
        else:
            log(f"Started {GATEWAY_TASK} task")
    except Exception as e:
        log(f"Failed to start gateway: {e}")


def circuit_breaker_tripped():
    """Return True if we've restarted too many times in the last hour."""
    global restart_times
    now = time.time()
    restart_times = [t for t in restart_times if now - t < 3600]
    return len(restart_times) >= MAX_RESTARTS_PER_HOUR


def can_restart():
    """Check cooldown and circuit breaker."""
    if circuit_breaker_tripped():
        log(f"Circuit breaker: {MAX_RESTARTS_PER_HOUR} restarts/hour exceeded - waiting")
        return False
    if time.time() - last_restart < RESTART_COOLDOWN:
        remaining = RESTART_COOLDOWN - (time.time() - last_restart)
        log(f"In cooldown ({remaining:.0f}s remaining)")
        return False
    return True


def do_restart(reason):
    """Kill (if needed) and restart the gateway."""
    global last_restart, restart_times, down_since, tick_stale_since, log_stale_since
    log(f"RESTARTING gateway: {reason}")
    if is_gateway_running():
        kill_gateway()
    start_gateway()
    last_restart = time.time()
    restart_times.append(time.time())
    down_since = None
    tick_stale_since = None
    log_stale_since = None


def main():
    global down_since, tick_stale_since, log_stale_since, last_restart, restart_times

    log(f"Watchdog v2.2 started (PID {os.getpid()}) - checking every {CHECK_INTERVAL}s "
        f"(tick heartbeat: {TICK_STALE_THRESHOLD}s, log backstop: {LOG_STALE_THRESHOLD}s)")

    while True:
        try:
            running = is_gateway_running()
            # Grace period: skip heartbeat checks for STARTUP_GRACE after any restart
            in_grace = (time.time() - last_restart) < STARTUP_GRACE if last_restart else \
                       (time.time() - watchdog_start_time) < STARTUP_GRACE

            # --- Check 1: Process alive ---
            if not running:
                tick_stale_since = None  # irrelevant if process is dead
                log_stale_since = None
                if down_since is None:
                    down_since = time.time()
                    log("Gateway process DOWN")
                else:
                    down_duration = time.time() - down_since
                    if down_duration >= DOWN_THRESHOLD and can_restart():
                        do_restart(f"process down {down_duration:.0f}s")
                time.sleep(CHECK_INTERVAL)
                continue
            else:
                # Process is alive
                if down_since is not None:
                    log("Gateway process recovered")
                down_since = None

            # --- Check 2: Cron tick lock heartbeat (primary zombie check) ---
            # Skip during grace period (fresh restart or watchdog just started)
            if not in_grace:
                tick_age = get_file_age(CRON_TICK_LOCK)
                if tick_age is not None and tick_age > TICK_STALE_THRESHOLD:
                    if tick_stale_since is None:
                        tick_stale_since = time.time()
                        log(f"Gateway tick lock stale ({tick_age:.0f}s since last touch) - "
                            f"event loop may be dead")
                    else:
                        stale_duration = time.time() - tick_stale_since
                        if stale_duration >= 60 and can_restart():
                            do_restart(f"tick lock stale for {tick_age:.0f}s "
                                       f"(detected {stale_duration:.0f}s ago)")
                            time.sleep(CHECK_INTERVAL)
                            continue
                else:
                    if tick_stale_since is not None:
                        log("Gateway tick lock recovered - event loop alive")
                    tick_stale_since = None

            # --- Check 3: Log freshness backstop (4h, catches pathological cases) ---
            # Only fires if the tick heartbeat is ALSO stale — a quiet log with a live
            # heartbeat just means nobody's talking to the gateway (normal idle).
            if not in_grace and tick_stale_since is not None:
                log_age = get_file_age(GATEWAY_LOG)
                if log_age is not None and log_age > LOG_STALE_THRESHOLD:
                    if log_stale_since is None:
                        log_stale_since = time.time()
                        log(f"Gateway log stale ({log_age:.0f}s since last write) AND "
                            f"tick stale - backstop trigger")
                    else:
                        stale_duration = time.time() - log_stale_since
                        if stale_duration >= 60 and can_restart():
                            do_restart(f"log stale for {log_age:.0f}s AND tick stale "
                                       f"(detected {stale_duration:.0f}s ago) [backstop]")
                            time.sleep(CHECK_INTERVAL)
                            continue
                else:
                    if log_stale_since is not None:
                        log("Gateway log active again - recovered")
                    log_stale_since = None

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log("Watchdog stopped by user")
            break
        except Exception as e:
            log(f"Unexpected error: {e}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
