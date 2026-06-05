#!/bin/bash
# Weekly macro learning review. Scans the last 90 days of market events for
# recurring patterns and extracts durable portfolio rules via LLM.
#
# Cron: Saturday 12:00 UTC (08:00 ET). Runs once a week.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="${PORTFOLIO_ADVISOR_MACRO_REVIEW_LOG:-$HOME/.tradingagents/logs/portfolio-advisor-macro-review.log}"
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

LOCK="$HOME/.tradingagents/run/cron-portfolio-advisor-macro-review.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) macro-review: another instance holds the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) portfolio advisor macro-review start ====="
  set +e
  "$PY" -m cli.main advisor portfolio macro-review
  ec=$?
  set -e
  echo "===== $(_ts) portfolio advisor macro-review end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
