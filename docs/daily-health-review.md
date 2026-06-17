# Hermes Daily Health Review -- Cron Job Reference

A cron job prompt for daily automated health checks. Designed to run as a Hermes scheduled task every morning.

## The Problem

Health checks that filter `tasklist` for `hermes.exe` always return empty -- the gateway process is `python.exe`, not `hermes.exe`. This causes false-negative CRITICAL alerts.

## Corrected Cron Prompt

Copy this into `hermes cron create --prompt "..."` or edit `cron/jobs.json` directly:

```
Hermes Daily Health Review. Check the last 24 hours of logs for problems.

Steps:
1. Read the last 24h of errors.log at ~/AppData/Local/hermes/logs/errors.log (use grep to filter to last 24h by timestamp)
2. Read the last 24h of gateway.log at ~/AppData/Local/hermes/logs/gateway.log
3. Read the watchdog log at ~/AppData/Local/hermes/logs/watchdog.log
4. Group errors by type: ZAI/API errors, gateway crashes, tool failures, memory issues, other
5. For each group, determine if it's:
   - TRANSIENT (ZAI 500s, rate limits, network blips) -- note count but no action needed
   - ACTIONABLE (missing config, broken file, repeated pattern) -- describe the fix clearly
   - CRITICAL (gateway down, watchdog not running) -- flag prominently
6. Check if watchdog is running: `schtasks //Query //TN "Hermes_Watchdog" //FO LIST //V 2>&1` -- look for Status = Running and Last Result = 0. Also check watchdog.log staleness (if no entries in >4h, the watchdog may have died silently).
7. Check if gateway is up: `schtasks //Query //TN "Hermes_Gateway" //FO LIST //V 2>&1` -- look for Status = Running. Also verify gateway.log has recent timestamps (within last few minutes). NOTE: the gateway process is python.exe (launched via pythonw.exe), NOT hermes.exe -- do NOT use `tasklist /FI "IMAGENAME eq hermes.exe"` as it will always show nothing (false negative).
8. Check disk space: df -h /c | tail -1
9. Check cron output dir for any failed jobs: ls -lt ~/AppData/Local/hermes/cron/output/ | head -10

Report format:
- If everything is clean: "All clear -- no issues in last 24h." (keep it brief)
- If issues found: group by severity, give counts for transient stuff, give fixes for actionable stuff
- Do NOT report ZAI 500/429 errors individually -- just give a count like "ZAI transient errors: 47 (500s: 32, 429s: 15)"
- Do NOT suggest fixes for ZAI instability -- it's their problem
- DO flag anything that looks like a config issue, missing dependency, or new recurring pattern

If no issues worth reporting, respond with just "No actionable issues in last 24h. [one-line summary of transient noise]"
```

## Schedule

Recommended: daily at 10:30 AM in the user's timezone (morning review).

```bash
# Create via Hermes CLI
hermes cron create --name "Hermes Daily Health Review" --schedule "30 10 * * *" --prompt "..." --deliver origin
```

## What Changed (v2)

Previous versions used Steps 6/7 like:
```
tasklist /FI "IMAGENAME eq hermes.exe" /NH
```

This was a false negative because the gateway runs as `python.exe`. The corrected version uses:
- `schtasks /Query` for both gateway and watchdog task status
- `watchdog.log` freshness as a secondary indicator
- `gateway.log` timestamps for gateway liveness

## Fleet Deployment

This cron prompt is deployed on:
- **Holly** (thinkzo): schedule `30 10 * * *`
- **Mini** (lappy): schedule `15 10 * * *`

Both use `deliver: origin` so the report goes to the user's Telegram DM.
