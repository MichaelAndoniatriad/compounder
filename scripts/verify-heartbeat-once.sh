#!/bin/bash
# One-shot verification: confirm the action-check heartbeat populated after the
# first weekday session run, and that the dead-man's-switch in-session
# enforcement path reads correctly. Sends a Telegram summary, then removes its
# own crontab line so it runs exactly once.
#
# Scheduled for Monday ~15:05 UTC (after the morning action-check, inside the
# 15:00-23:00 UTC enforcement window). Self-deleting.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEARTBEAT_DIR="$HOME/.tradingagents/run/heartbeats"
AC_HB="$HEARTBEAT_DIR/heartbeat-action-check.ts"
LOG="$HOME/.tradingagents/logs/dead-mans-switch.log"

if [[ -f "$ROOT/.env" ]]; then
  set -a; source "$ROOT/.env"; set +a
fi
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3.12 &>/dev/null; then
  PY="$(command -v python3.12)"
else
  PY="python3"
fi

_age_minutes() {
  local ts="$1" e
  e=$(date -d "${ts:0:19}" +%s 2>/dev/null || echo 0)
  if [[ "$e" -le 0 ]]; then echo 9999; return; fi
  echo $(( ($(date +%s) - e) / 60 ))
}

# 1. Heartbeat present and fresh?
if [[ -f "$AC_HB" ]]; then
  HB_TS=$(cat "$AC_HB")
  HB_AGE=$(_age_minutes "$HB_TS")
  HB_STATUS="present, ${HB_AGE}min old (${HB_TS})"
else
  HB_TS="MISSING"
  HB_AGE=9999
  HB_STATUS="STILL MISSING — first action-check did not write a heartbeat"
fi

# 2. Run the switch once and grab its verdict line.
bash "$ROOT/scripts/dead-mans-switch.sh" 8 >/dev/null 2>&1 || true
SWITCH_LINE=$(tail -1 "$LOG" 2>/dev/null || echo "(no log line)")

# 3. Overall verdict.
if [[ "$HB_TS" != "MISSING" && "$HB_AGE" -lt 480 ]]; then
  VERDICT="PASS — heartbeat live, in-session enforcement active"
else
  VERDICT="CHECK NEEDED — heartbeat absent or stale during session"
fi

MSG="Heartbeat verification (Monday session):
- $VERDICT
- Heartbeat: $HB_STATUS
- Switch said: $SWITCH_LINE"

echo "$MSG"

"$PY" -c "
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio_advisor.messaging import send_telegram_message
cfg = DEFAULT_CONFIG.copy()
send_telegram_message(cfg, 'HEARTBEAT VERIFY', '''$MSG''')
" 2>>"$LOG" || true

# 4. Self-remove from crontab so this runs exactly once.
( crontab -l 2>/dev/null | grep -v 'verify-heartbeat-once.sh' || true ) | crontab - || true
