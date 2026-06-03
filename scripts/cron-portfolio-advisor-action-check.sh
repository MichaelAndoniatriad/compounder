#!/bin/bash
# Proactive PM action-check (3x/day). Runs one PM cycle and messages the human
# ONLY when there is an action to take. Decisions the human already made
# suppress settled calls.
#
#   35 13 * * 1-5   open
#   30 16 * * 1-5   midday
#   5  20 * * 1-5   close
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="${PORTFOLIO_ADVISOR_ACTION_CHECK_CRON_LOG:-$HOME/.tradingagents/logs/portfolio-advisor-action-check.log}"
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
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

_ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

LOCK="$HOME/.tradingagents/run/cron-portfolio-advisor-action-check.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) cron-portfolio-advisor-action-check: another instance holds the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) portfolio advisor action-check start ====="
  set +e
  "$PY" -m cli.main advisor portfolio action-check
  ec=$?
  set -e
  echo "===== $(_ts) portfolio advisor action-check end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
