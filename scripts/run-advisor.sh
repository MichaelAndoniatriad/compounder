#!/usr/bin/env bash
# Generic local runner for portfolio-advisor CLI subcommands.
# Usage: run-advisor.sh <subcommand> [args...]
#   e.g. run-advisor.sh watchdog
#        run-advisor.sh run-due
#        run-advisor.sh core-scan
#
# Handles: venv selection, .env sourcing, PYTHONPATH, per-command flock
# (so overlapping launchd fires can't stack), and per-command logging.
# Market-gated commands (watchdog, ep-scan) self-skip outside US hours, so
# scheduling them generously is safe.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <advisor-subcommand> [args...]" >&2
  exit 2
fi

CMD="$1"; shift
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Python: prefer project venv
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3.12 &>/dev/null; then
  PY="$(command -v python3.12)"
else
  PY="python3"
fi

# Secrets / config
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

LOG="$HOME/.tradingagents/logs/advisor-${CMD}.log"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.tradingagents/run/advisor-${CMD}.lock"
mkdir -p "$(dirname "$LOCK")"
# flock is Linux-only; macOS lacks it. Under launchd, a job is already
# serialized with itself per-label, so the lock is redundant there. Use it
# when available (cron/Linux), otherwise rely on launchd's serialization.
if command -v flock >/dev/null 2>&1; then
  exec 200>"$LOCK"
  if ! flock -n 200; then
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') run-advisor ${CMD}: another instance holds the lock; exiting" >>"$LOG"
    exit 0
  fi
fi

{
  echo "===== $(date '+%Y-%m-%dT%H:%M:%S%z') advisor ${CMD} start ====="
  set +e
  "$PY" -m cli.main advisor portfolio "$CMD" "$@"
  ec=$?
  set -e
  if [[ $ec -eq 0 ]]; then
    mkdir -p "$HOME/.tradingagents/run/heartbeats"
    date -u "+%Y-%m-%dT%H:%M:%SZ" > "$HOME/.tradingagents/run/heartbeats/heartbeat-${CMD}.ts"
  fi
  echo "===== $(date '+%Y-%m-%dT%H:%M:%S%z') advisor ${CMD} end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
