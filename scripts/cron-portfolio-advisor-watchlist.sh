#!/bin/bash
# Weekly research funnel (Stage 1): discover candidates from live market news, gate them,
# and queue cheap single_model thesis screens. Names that clear the screen auto-promote to
# full_graph (capped per week); the PM then decides. News-driven, with memory-generator top-up.
#
# Schedule weekly (e.g. Monday 4am):
#   0 4 * * MON /ABS/PATH/trading-agents/scripts/cron-portfolio-advisor-watchlist.sh
#
# Env:
#   PORTFOLIO_ADVISOR_WATCHLIST_CRON_LOG=~/.tradingagents/logs/portfolio-advisor-watchlist.log
# Requires: ETORO_* keys (for live-ticker dedup), LLM keys.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="${PORTFOLIO_ADVISOR_WATCHLIST_CRON_LOG:-$HOME/.tradingagents/logs/portfolio-advisor-watchlist.log}"
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

LOCK="$HOME/.tradingagents/run/cron-portfolio-advisor-watchlist.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) cron-portfolio-advisor-watchlist: another instance is holding the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) portfolio advisor watchlist research start ====="
  set +e
  "$PY" -m cli.main advisor portfolio watchlist research
  ec=$?
  set -e
  echo "===== $(_ts) portfolio advisor watchlist research end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
