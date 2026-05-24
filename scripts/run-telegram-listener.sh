#!/usr/bin/env bash
# Run Telegram inbound listener for the portfolio advisor.
# Flock-locked so only one instance can run at a time — running a second
# listener against the same bot token causes Telegram getUpdates conflicts.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python3"
fi

LOCK="$HOME/.tradingagents/run/telegram-listener.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S%z') run-telegram-listener: another instance is holding the lock; exiting" >&2
  exit 1
fi

exec "$PY" -m cli.main advisor portfolio telegram-listen
