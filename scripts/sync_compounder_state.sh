#!/bin/bash
# Sync the trading server's portfolio_advisor state to the Mac for offline dashboard generation.
#
# Pulls ~/.tradingagents/portfolio_advisor/ from the server to ~/local/compounder_state/.
# Designed for daily cron at 04:00 local. Idempotent. Read only on the server.
#
# Configurable via env vars:
#   COMPOUNDER_USER   default: root
#   COMPOUNDER_HOST   default: 116.203.153.58
#   COMPOUNDER_KEY    default: ~/.ssh/trading-server.key
#   COMPOUNDER_REMOTE default: ~/.tradingagents/portfolio_advisor/
#   COMPOUNDER_LOCAL  default: ~/local/compounder_state/

set -uo pipefail

USER_NAME="${COMPOUNDER_USER:-root}"
HOST="${COMPOUNDER_HOST:-116.203.153.58}"
KEY="${COMPOUNDER_KEY:-$HOME/.ssh/trading-server.key}"
REMOTE="${COMPOUNDER_REMOTE:-/root/.tradingagents/portfolio_advisor/}"
LOCAL="${COMPOUNDER_LOCAL:-$HOME/local/compounder_state/}"
LOG="${COMPOUNDER_SYNC_LOG:-$HOME/Library/Logs/compounder_sync.log}"

mkdir -p "$LOCAL"
mkdir -p "$(dirname "$LOG")"

_ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
echo "$(_ts) sync start" >> "$LOG"

# Use rsync with archive mode, compression, and a sensible timeout. Excludes any
# in flight tmp files. Read only on the server.
if rsync \
    -avz --timeout=30 \
    --exclude="*.tmp" --exclude="*.lock" --exclude="__pycache__/" \
    -e "ssh -i $KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new" \
    "${USER_NAME}@${HOST}:${REMOTE}" "$LOCAL" >> "$LOG" 2>&1; then
  echo "$(_ts) sync ok" >> "$LOG"
  exit 0
else
  RC=$?
  echo "$(_ts) sync failed rc=$RC" >> "$LOG"
  exit "$RC"
fi
