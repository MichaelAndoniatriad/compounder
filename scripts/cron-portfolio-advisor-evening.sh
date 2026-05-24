#!/bin/bash
# Send evening digest of open portfolio action items via Telegram/ntfy.
# Cron fires at both possible UTC times (21 + 22) to cover BST and GMT.
# The script gates on actual UK time so only one fires per day at 22:00 UK.
#
#   0 21,22 * * * /opt/tradingagents/scripts/cron-portfolio-advisor-evening.sh

set -euo pipefail

UK_HOUR=$(TZ='Europe/London' date +%H)
if [[ "$UK_HOUR" != "22" ]]; then
  exit 0
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="$HOME/.tradingagents/logs/portfolio-advisor-evening.log"
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

LOCK="$HOME/.tradingagents/run/cron-portfolio-advisor-evening.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) cron-portfolio-advisor-evening: another instance is holding the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) portfolio advisor evening-digest start ====="
  set +e
  "$PY" -m cli.main advisor portfolio evening-digest
  ec=$?
  set -e
  echo "===== $(_ts) portfolio advisor evening-digest end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
