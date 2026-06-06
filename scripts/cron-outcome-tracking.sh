#!/bin/bash
# Weekly outcome tracking. Measures every pending recommendation against
# actual market outcomes and writes results to the recommendation log.
#
# Cron: Sunday 14:00 UTC. Runs once a week.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="${PORTFOLIO_ADVISOR_OUTCOME_LOG:-$HOME/.tradingagents/logs/portfolio-advisor-outcome.log}"
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

LOCK="$HOME/.tradingagents/run/cron-outcome-tracking.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) outcome-tracking: another instance holds the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) outcome tracking start ====="
  set +e
  "$PY" -m cli.main advisor portfolio measure-outcomes
  ec=$?
  set -e
  if [[ $ec -eq 0 ]]; then
    mkdir -p "$HOME/.tradingagents/run/heartbeats"
    date -u "+%Y-%m-%dT%H:%M:%SZ" > "$HOME/.tradingagents/run/heartbeats/heartbeat-outcome-tracking.ts"
  fi
  echo "===== $(_ts) outcome tracking end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
