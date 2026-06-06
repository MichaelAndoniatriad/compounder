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
STALE_SEC=$((STALE_HOURS * 3600))

# Primary: action-check heartbeat — the main liveness signal.
# MISSING is a cold-start state (just deployed, first action-check hasn't run
# yet). Don't page on it — log a soft notice and wait for the first heartbeat.
# The PM-process check below still covers a genuinely-down system at startup.
AC_AGE=$(_age_minutes "$AC_TS")
if [[ "$AC_TS" == "MISSING" ]]; then
  echo "$(_ts) action-check heartbeat MISSING (cold start) — not paging" >>"$LOG"
elif [[ "$AC_AGE" -gt $((STALE_HOURS * 60)) ]]; then
  ALERTS="${ALERTS}  action-check heartbeat: ${AC_AGE}min old (threshold: ${STALE_HOURS}h)\n"
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
  echo "$(_ts) all clear — action-check ${AC_AGE}min ago, PM running" >>"$LOG"
fi
