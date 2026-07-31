"""
Hermes Gateway Watchdog for Windows (v2.6)
Runs persistently, monitors gateway, restarts if down or frozen.
Designed to be started via Scheduled Task (AtStartup trigger).

v2.6 changes (2026-07-31):
  - NEW: Check 3 — platform adapter zombie detection
    Reads gateway_state.json and checks if platforms.telegram.state is healthy.
    If the adapter is in "retrying" state with an error_code AND hasn't updated
    its timestamp recently, the gateway is a zombie (process alive, adapter dead).
    This catches the case where the gateway logs "Fatal adapter error... Restarting"
    but never actually exits — the process hangs as a SYSTEM-level zombie that
    can't be killed from user sessions. The watchdog runs as SYSTEM (S4U task)
    so it CAN kill the zombie PID.
  - False-positive safe: only triggers when adapter state is bad AND stale.
    An actively-retrying adapter has a fresh updated_at and is left alone.

v2.5 changes (2026-07-01):
  - CRITICAL: Added self-healing heartbeat to watchdog.lock (last_heartbeat updated every loop)
  - CRITICAL: Added top-level exception safety — main loop catches all exceptions and continues
  - CRITICAL: Added sleep/wake detection — large time gap between iterations triggers immediate re-check
  - These fix the Modern Standby kill: StopOnIdleEnd=true + idle event = silent watchdog death
  - Script changes complement the task XML fix (StopOnIdleEnd=false, dual BootTrigger+CalendarTrigger)

v2.4 changes (2026-06-30):
  - Merged best of Holly v2.3 + Mini v2.3 into unified fleet version
  - Added single-instance guard via PID lock file (watchdog.lock) [Holly]
    On startup, checks if another watchdog instance is alive; if so, exits silently.
    Fixes zombie stacking: Task Scheduler launching new instances while old ones
    were still alive but wedged, causing multiple zombie watchdog pairs.
  - Removed log freshness backstop entirely (was 4h threshold) [Mini]
    Cron tick lock heartbeat is now the sole zombie detection mechanism.
    The 4h log backstop caused false restarts during long idle periods.

v2.2 changes (2026-06-08):
  - Replaced log staleness with cron tick lock heartbeat as primary zombie check
  - cron/.tick.lock is touched every 60s by the gateway's event loop ticker
  - This eliminates false positives when the gateway is healthy but idle (no messages)
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
  3. Platform adapter health — is telegram state "connected" in gateway_state.json?
     (Catches "wedged zombie": process alive, event loop alive, but adapter dead
     after fatal network error. Gateway logs "restarting" but never exits.)

Single-instance guard:
  Uses watchdog.lock to ensure only one watchdog process runs at a time.
  If Task Scheduler launches a new instance while one is alive, the new one exits.
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

RESTART_COOLDOWN = 60      # minimum seconds between restarts
MAX_RESTARTS_PER_HOUR = 5  # circuit breaker
STARTUP_GRACE = 120        # seconds after restart before heartbeat checks (handles startup + sleep/wake)

# Platform adapter zombie detection (Check 3)
# When the gateway hits a fatal Telegram error, it logs "restarting" but may not
# actually exit. The adapter state in gateway_state.json will show "retrying" with
# an error_code. We only treat it as a zombie if the state has been stale for this
# long — an actively-retrying adapter updates its timestamp frequently.
ADAPTER_STALE_THRESHOLD = 180  # 3 minutes — if no state update for this long while in error, it's wedged

HERMES_HOME = r"C:\Users\kenzo\AppData\Local\hermes"
HERMES_EXE = r"C:\Users\kenzo\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe"
GATEWAY_PID_FILE = os.path.join(HERMES_HOME, "gateway.pid")
GATEWAY_STATE_FILE = os.path.join(HERMES_HOME, "gateway_state.json")
CRON_TICK_LOCK = os.path.join(HERMES_HOME, "cron", ".tick.lock")
WATCHDOG_LOG = os.path.join(HERMES_HOME, "logs", "watchdog.log")
WATCHDOG_LOCK = os.path.join(HERMES_HOME, "watchdog.lock")
GATEWAY_TASK = "Hermes_Gateway"

# State files that can go stale and cause issues
STATE_FILES = ["gateway.pid", "gateway.lock"]

# For fallback WMIC scan only
GATEWAY_MARKERS = ["gateway run"]
GATEWAY_EXCLUDES = ["gateway_watchdog", "wmic"]

down_since = None
tick_stale_since = None
adapter_zombie_since = None
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


# ---- Single-instance guard ----

def _read_watchdog_lock():
    """Read PID from watchdog.lock. Returns (pid, start_time) or (None, None)."""
    try:
        with open(WATCHDOG_LOCK, "r") as f:
            data = json.load(f)
            return data.get("pid"), data.get("start_time")
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None


def _read_watchdog_lock_full():
    """Read all fields from watchdog.lock. Returns (pid, start_time, last_heartbeat) or (None, None, None)."""
    try:
        with open(WATCHDOG_LOCK, "r") as f:
            data = json.load(f)
            return data.get("pid"), data.get("start_time"), data.get("last_heartbeat")
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None, None


def _write_watchdog_lock():
    """Write our PID and start time to watchdog.lock."""
    try:
        with open(WATCHDOG_LOCK, "w") as f:
            json.dump({"pid": os.getpid(), "start_time": time.time(),
                       "last_heartbeat": time.time()}, f)
    except Exception as e:
        log(f"Warning: could not write watchdog.lock: {e}")


def _touch_watchdog_lock():
    """Update the heartbeat timestamp in watchdog.lock without changing PID."""
    try:
        with open(WATCHDOG_LOCK, "r") as f:
            data = json.load(f)
        data["last_heartbeat"] = time.time()
        with open(WATCHDOG_LOCK, "w") as f:
            json.dump(data, f)
    except Exception:
        # Lock file might be gone or corrupted — rewrite it
        _write_watchdog_lock()


def _clear_watchdog_lock():
    """Remove watchdog.lock on exit."""
    try:
        if os.path.exists(WATCHDOG_LOCK):
            os.remove(WATCHDOG_LOCK)
    except Exception:
        pass


def acquire_instance_lock():
    """Ensure only one watchdog instance is running.
    Returns True if we should proceed, False if another instance is alive."""
    existing_pid, existing_start = _read_watchdog_lock()
    if existing_pid is not None:
        if is_pid_alive(existing_pid):
            # Another watchdog is alive — exit silently
            sys.exit(0)
        else:
            # Stale lock from a dead process — clean it up
            # Check heartbeat to see how long it's been dead
            _, _, last_hb = _read_watchdog_lock_full()
            if last_hb:
                dead_for = time.time() - last_hb
                log(f"Found stale watchdog.lock (PID {existing_pid} dead, "
                    f"heartbeat stopped {dead_for:.0f}s ago), clearing")
            else:
                log(f"Found stale watchdog.lock (PID {existing_pid} dead), clearing")
            _clear_watchdog_lock()
    _write_watchdog_lock()
    return True


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


# ---- Platform adapter health check (Check 3) ----

def check_adapter_health():
    """Check if the Telegram adapter in gateway_state.json is healthy.
    
    Returns (healthy: bool, reason: str).
    
    A gateway can be a "wedged zombie": process alive, cron ticker alive,
    but the Telegram adapter died after a fatal network error and the
    gateway never actually exited. It logs "restarting" but hangs.
    
    Detection: telegram.state != "connected" AND the state hasn't been
    updated recently (stale). An actively-retrying adapter updates its
    timestamp frequently — only a truly wedged one goes silent.
    
    False-positive safety:
    - State "connected" → always healthy
    - State "retrying" with fresh updated_at → healthy (it's trying)
    - State "retrying" with stale updated_at → zombie
    - Any error_code with stale updated_at → zombie
    """
    try:
        with open(GATEWAY_STATE_FILE, "r") as f:
            data = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        # Can't read state file — skip this check, other checks handle it
        return True, "state file unreadable"
    
    platforms = data.get("platforms", {})
    telegram = platforms.get("telegram", {})
    if not telegram:
        return True, "no telegram platform configured"
    
    tg_state = telegram.get("state", "")
    tg_error = telegram.get("error_code")
    tg_updated = telegram.get("updated_at", "")
    
    # Healthy state — no need to check further
    if tg_state == "connected" and not tg_error:
        return True, "connected"
    
    # Adapter is not connected. Check if the state is stale.
    # Parse ISO 8601 timestamp and check age.
    if not tg_updated:
        return True, "no updated_at (adapter may be initializing)"
    
    try:
        from datetime import datetime as dt
        # Handle timezone-aware ISO timestamps
        ts = tg_updated.replace("Z", "+00:00")
        updated_dt = dt.fromisoformat(ts)
        now_dt = dt.now(updated_dt.tzinfo) if updated_dt.tzinfo else dt.now()
        age = (now_dt - updated_dt).total_seconds()
    except Exception:
        # Can't parse timestamp — don't act on it
        return True, "unreadable timestamp"
    
    if age < ADAPTER_STALE_THRESHOLD:
        return True, f"adapter in {tg_state}, but fresh ({age:.0f}s ago)"
    
    # Adapter is in a bad state AND stale — this is a wedged zombie
    return False, (f"adapter zombie: telegram state={tg_state} "
                   f"error={tg_error} stale={age:.0f}s")


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
    global last_restart, restart_times, down_since, tick_stale_since, adapter_zombie_since
    log(f"RESTARTING gateway: {reason}")
    if is_gateway_running():
        kill_gateway()
    start_gateway()
    last_restart = time.time()
    restart_times.append(time.time())
    down_since = None
    tick_stale_since = None
    adapter_zombie_since = None


def main():
    global down_since, tick_stale_since, adapter_zombie_since, last_restart, restart_times

    # Single-instance guard: exit if another watchdog is already alive
    acquire_instance_lock()

    log(f"Watchdog v2.6 started (PID {os.getpid()}) - checking every {CHECK_INTERVAL}s "
        f"(tick heartbeat: {TICK_STALE_THRESHOLD}s, adapter stale: {ADAPTER_STALE_THRESHOLD}s)")

    try:
        while True:
            try:
                loop_start = time.time()

                # --- Heartbeat: touch our own lock so we're trackable ---
                _touch_watchdog_lock()

                # --- Sleep/wake detection ---
                # If more than 2x CHECK_INTERVAL passed since last loop iteration,
                # the machine likely slept/woke. Log it and reset grace timers.
                if hasattr(main, '_last_loop_time'):
                    elapsed = loop_start - main._last_loop_time
                    if elapsed > CHECK_INTERVAL * 3:
                        log(f"Recovered from sleep/suspend ({elapsed:.0f}s gap) - "
                            f"rechecking gateway immediately")
                        # Reset the grace period to give gateway time to wake up too
                        last_restart = loop_start
                main._last_loop_time = loop_start

                running = is_gateway_running()
                # Grace period: skip heartbeat checks for STARTUP_GRACE after any restart
                in_grace = (time.time() - last_restart) < STARTUP_GRACE if last_restart else \
                           (time.time() - watchdog_start_time) < STARTUP_GRACE

                # --- Check 1: Process alive ---
                if not running:
                    tick_stale_since = None  # irrelevant if process is dead
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

                # --- Check 3: Platform adapter health (catches wedged zombies) ---
                # Skip during grace period
                if not in_grace:
                    adapter_healthy, adapter_reason = check_adapter_health()
                    if not adapter_healthy:
                        if adapter_zombie_since is None:
                            adapter_zombie_since = time.time()
                            log(f"Gateway adapter unhealthy: {adapter_reason}")
                        else:
                            zombie_duration = time.time() - adapter_zombie_since
                            if zombie_duration >= 60 and can_restart():
                                do_restart(f"adapter zombie: {adapter_reason} "
                                           f"(detected {zombie_duration:.0f}s ago)")
                                time.sleep(CHECK_INTERVAL)
                                continue
                    else:
                        if adapter_zombie_since is not None:
                            log(f"Gateway adapter recovered: {adapter_reason}")
                        adapter_zombie_since = None

                time.sleep(CHECK_INTERVAL)

            except KeyboardInterrupt:
                log("Watchdog stopped by user")
                break
            except Exception as e:
                # CRITICAL: never die from an unexpected error — log and continue
                log(f"Unexpected error in main loop: {type(e).__name__}: {e}")
                time.sleep(CHECK_INTERVAL)
    finally:
        _clear_watchdog_lock()


if __name__ == "__main__":
    main()
