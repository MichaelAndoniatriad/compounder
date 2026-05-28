#!/bin/bash
# Daily system-alive heartbeat: sends a Telegram ping with positions tracked,
# pending jobs, and last alert age. Silence from this script = system is down.
#
# Schedule once daily (e.g. 8am UK = 7am UTC in winter, 8am UTC in summer):
#   0 7,8 * * * /opt/tradingagents/scripts/cron-portfolio-advisor-heartbeat.sh
# The UK-hour gate ensures only one fires per day regardless of DST.

set -euo pipefail

UK_HOUR=$(TZ='Europe/London' date +%H)
if [[ "$UK_HOUR" != "08" ]]; then
  exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="$HOME/.tradingagents/logs/portfolio-advisor-heartbeat.log"
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

LOCK="$HOME/.tradingagents/run/cron-portfolio-advisor-heartbeat.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) cron-portfolio-advisor-heartbeat: another instance is holding the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) portfolio advisor heartbeat start ====="
  set +e
  "$PY" -m cli.main advisor portfolio heartbeat
  ec=$?
  # Off-box dead-man's-switch: ping an external healthcheck URL so an
  # INDEPENDENT service alerts the human if the whole box goes dark.
  # Set TRADINGAGENTS_HEALTHCHECK_URL in .env (e.g. a healthchecks.io URL).
  if [[ -n "${TRADINGAGENTS_HEALTHCHECK_URL:-}" ]]; then
    if [[ "$ec" -eq 0 ]]; then
      curl -fsS -m 10 --retry 2 "$TRADINGAGENTS_HEALTHCHECK_URL" -o /dev/null \
        && echo "$(_ts) healthcheck ping OK" \
        || echo "$(_ts) healthcheck ping FAILED"
    else
      curl -fsS -m 10 --retry 2 "${TRADINGAGENTS_HEALTHCHECK_URL%/}/fail" -o /dev/null \
        && echo "$(_ts) healthcheck fail-ping OK" \
        || echo "$(_ts) healthcheck fail-ping FAILED"
    fi
  fi
  set -e
  echo "===== $(_ts) portfolio advisor heartbeat end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
