#!/bin/bash
# Dead-man's-switch: alert if no market events or recommendations have been
# logged in the last N hours. Protects against silent stalls like the
# learned-rules pipeline outage on May 16.
#
# Cron: every 4 hours. Sends urgent Telegram alert if system appears dead.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOURS="${1:-8}"  # Default: 8-hour window
LOG="${PORTFOLIO_ADVISOR_WATCHDOG_LOG:-$HOME/.tradingagents/logs/dead-mans-switch.log}"
mkdir -p "$(dirname "$LOG")"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3.12 &>/dev/null; then
  PY="$(command -v python3.12)"
else
  PY="python3"
fi

if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

_ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

LOCK="$HOME/.tradingagents/run/dead-mans-switch.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  exit 0
fi

# Check last market event timestamp
LAST_EVENT=$("$PY" -c "
import json, sys
from pathlib import Path
p = Path.home() / '.tradingagents' / 'memory' / 'market_events.jsonl'
if not p.is_file():
    print('NEVER')
    sys.exit(0)
lines = [l for l in p.read_text().splitlines() if l.strip()]
if not lines:
    print('NEVER')
    sys.exit(0)
last = json.loads(lines[-1])
print(last.get('logged_at', last.get('date', 'UNKNOWN')))
" 2>/dev/null)

# Check last recommendation timestamp
LAST_REC=$("$PY" -c "
import json, sys
from pathlib import Path
p = Path.home() / '.tradingagents' / 'portfolio_advisor' / 'recommendation_log.jsonl'
if not p.is_file():
    print('NEVER')
    sys.exit(0)
lines = [l for l in p.read_text().splitlines() if l.strip()]
if not lines:
    print('NEVER')
    sys.exit(0)
last = json.loads(lines[-1])
print(last.get('ts', 'UNKNOWN'))
" 2>/dev/null)

# Check if PM process is running
PM_RUNNING=0
if pgrep -f 'telegram-listen' > /dev/null 2>&1; then
  PM_RUNNING=1
fi

# Evaluate staleness
ALERT=""
NOW_EPOCH=$(date +%s)
HOURS_SEC=$((HOURS * 3600))

check_staleness() {
  local ts="$1"
  local name="$2"
  if [[ "$ts" == "NEVER" ]]; then
    ALERT="${ALERT}${name}: never logged\n"
    return
  fi
  # Parse ISO timestamp, compare to now
  local ts_epoch
  ts_epoch=$(date -d "${ts:0:19}" +%s 2>/dev/null || echo 0)
  if [[ "$ts_epoch" -gt 0 ]]; then
    local age=$((NOW_EPOCH - ts_epoch))
    if [[ $age -gt $HOURS_SEC ]]; then
      local age_h=$((age / 3600))
      ALERT="${ALERT}${name}: last logged ${age_h}h ago\n"
    fi
  fi
}

check_staleness "$LAST_EVENT" "Market events"
check_staleness "$LAST_REC" "Recommendations"

if [[ $PM_RUNNING -eq 0 ]]; then
  ALERT="${ALERT}PM process: NOT RUNNING\n"
fi

if [[ -n "$ALERT" ]]; then
  {
    echo "$(_ts) DEAD-MAN'S-SWITCH ALERT:"
    echo -e "$ALERT"
  } | tee -a "$LOG"

  # Send urgent Telegram alert
  "$PY" -c "
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio_advisor.messaging import send_telegram_message
cfg = DEFAULT_CONFIG.copy()
alert_text = '''$ALERT'''
send_telegram_message(cfg, 'DEAD-MANS-SWITCH', f'System may be stalled (>${HOURS}h):\n{alert_text}')
" 2>>"$LOG" || true
else
  echo "$(_ts) dead-man's-switch: all clear (events: ${LAST_EVENT:0:19}, recs: ${LAST_REC:0:19}, PM: $PM_RUNNING)" >>"$LOG"
fi
