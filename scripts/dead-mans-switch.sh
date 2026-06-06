#!/bin/bash
# Dead-man's-switch — pages if the system is actually dead, not if markets
# are quiet. Uses cron-completion heartbeats instead of business-output signals.
#
# Primary liveness signal: action-check heartbeat (runs 6x/day, writes
# heartbeat on every successful completion). Pages if >5h old.
# Secondary: PM process (telegram-listen) must be running.
#
# Cron: every 4 hours (runs regardless of market hours).
# Heartbeats are written by each cron script on successful completion.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEARTBEAT_DIR="$HOME/.tradingagents/run/heartbeats"
STALE_HOURS="${1:-5}"  # Default: page if action-check heartbeat >5h old
LOG="${PORTFOLIO_ADVISOR_HEARTBEAT_LOG:-$HOME/.tradingagents/logs/dead-mans-switch.log}"
mkdir -p "$(dirname "$LOG")"

if [[ -f "$ROOT/.env" ]]; then
  set -a; source "$ROOT/.env"; set +a
fi
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

_ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
_age_minutes() {
  local ts="$1"
  local ts_epoch
  ts_epoch=$(date -d "${ts:0:19}" +%s 2>/dev/null || echo 0)
  if [[ "$ts_epoch" -le 0 ]]; then echo 9999; return; fi
  echo $(( ($(date +%s) - ts_epoch) / 60 ))
}

LOCK="$HOME/.tradingagents/run/dead-mans-switch.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then exit 0; fi

# --- Read heartbeats ---
# Only the action-check heartbeat drives paging; it runs 6x/day regardless of
# market activity and is the true liveness signal.
AC_HB="$HEARTBEAT_DIR/heartbeat-action-check.ts"

read_hb() { cat "$1" 2>/dev/null || echo "MISSING"; }

AC_TS=$(read_hb "$AC_HB")

# --- Check PM process ---
PM_RUNNING=0
pgrep -f 'telegram-listen' > /dev/null 2>&1 && PM_RUNNING=1

# --- Evaluate ---
ALERTS=""

# action-check runs weekdays only, clustered in the US market session
# (~13:35-20:55 UTC). It is NOT a 24/7 heartbeat — outside the session no run
# is expected, so a stale heartbeat overnight or on weekends is normal, not a
# stall. Only enforce freshness during a weekday session window: 15:00-23:00
# UTC (after the open in both EST and EDT, through close + margin). Outside it,
# the PM-process check below is the liveness signal.
DOW=$(date -u +%u)          # 1=Mon .. 7=Sun
HOUR=$((10#$(date -u +%H)))  # 00-23 UTC; 10# forces base-10 (08/09 aren't octal)
ENFORCE_HB=0
if [[ "$DOW" -le 5 && "$HOUR" -ge 15 && "$HOUR" -lt 23 ]]; then
  ENFORCE_HB=1
fi

# Primary: action-check heartbeat — the main liveness signal (session hours).
# MISSING is a cold-start state (just deployed, first action-check hasn't run
# yet). Don't page on it — log a soft notice and wait for the first heartbeat.
AC_AGE=$(_age_minutes "$AC_TS")
if [[ "$AC_TS" == "MISSING" ]]; then
  echo "$(_ts) action-check heartbeat MISSING (cold start) — not paging" >>"$LOG"
elif [[ "$ENFORCE_HB" -eq 1 && "$AC_AGE" -gt $((STALE_HOURS * 60)) ]]; then
  ALERTS="${ALERTS}  action-check heartbeat: ${AC_AGE}min old (threshold: ${STALE_HOURS}h, in-session)\n"
fi

# Secondary: PM process
if [[ $PM_RUNNING -eq 0 ]]; then
  ALERTS="${ALERTS}  PM process: NOT RUNNING\n"
fi

# --- Page or log all-clear ---
if [[ -n "$ALERTS" ]]; then
  {
    echo "$(_ts) DEAD-MAN'S-SWITCH:"
    echo -e "$ALERTS"
  } | tee -a "$LOG"

  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PY="$ROOT/.venv/bin/python"
  elif command -v python3.12 &>/dev/null; then
    PY="$(command -v python3.12)"
  else
    PY="python3"
  fi

  "$PY" -c "
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio_advisor.messaging import send_telegram_message
cfg = DEFAULT_CONFIG.copy()
alert = '''$ALERTS'''
send_telegram_message(cfg, 'SYSTEM ALERT', f'Dead-mans-switch triggered:\n{alert}', urgent=True)
" 2>>"$LOG" || true
else
  if [[ "$ENFORCE_HB" -eq 1 ]]; then
    echo "$(_ts) all clear — action-check ${AC_AGE}min ago (in-session), PM running" >>"$LOG"
  else
    echo "$(_ts) all clear — out of session (heartbeat not enforced), PM running" >>"$LOG"
  fi
fi
