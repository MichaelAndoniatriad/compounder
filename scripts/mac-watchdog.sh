#!/bin/bash
# Mac side server reachability watchdog.
#
# Why: scripts/dead-mans-switch.sh runs on the trading server and cannot alert
# when the server itself is offline. This script runs on the Mac, pings the
# server, and raises a macOS notification (and optional Telegram alert) when
# the server has been unreachable for the alert threshold.
#
# Install: see scripts/mac-watchdog.README.md
#
# State file: /tmp/mac-watchdog.state.json
#
# Configurable via environment variables (override defaults below):
#   MAC_WATCHDOG_HOST                server IP or hostname (required)
#   MAC_WATCHDOG_ALERT_AFTER_FAILS   consecutive failures before alerting (default 6 = 30min at 5min cron)
#   MAC_WATCHDOG_TIMEOUT             ping timeout in seconds (default 5)
#   MAC_WATCHDOG_TELEGRAM_BOT_TOKEN  optional; if set, sends Telegram alert too
#   MAC_WATCHDOG_TELEGRAM_CHAT_ID    optional; required if bot token set
#   MAC_WATCHDOG_LOG                 log path (default ~/Library/Logs/mac-watchdog.log)

set -uo pipefail

HOST="${MAC_WATCHDOG_HOST:-116.203.153.58}"
ALERT_AFTER_FAILS="${MAC_WATCHDOG_ALERT_AFTER_FAILS:-6}"
TIMEOUT="${MAC_WATCHDOG_TIMEOUT:-5}"
STATE_FILE="/tmp/mac-watchdog.state.json"
LOG_FILE="${MAC_WATCHDOG_LOG:-$HOME/Library/Logs/mac-watchdog.log}"
TELEGRAM_BOT_TOKEN="${MAC_WATCHDOG_TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${MAC_WATCHDOG_TELEGRAM_CHAT_ID:-}"

mkdir -p "$(dirname "$LOG_FILE")"

_ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

_log() {
  echo "$(_ts) $*" >> "$LOG_FILE"
}

_load_state() {
  if [[ -f "$STATE_FILE" ]]; then
    cat "$STATE_FILE"
  else
    echo '{"consecutive_failures":0,"down_since":null,"last_alert_sent":null,"status":"unknown"}'
  fi
}

_save_state() {
  printf '%s\n' "$1" > "$STATE_FILE"
}

_get() {
  # _get <json> <key>
  /usr/bin/python3 -c "import sys, json; d=json.loads(sys.argv[1]); v=d.get(sys.argv[2]); print('' if v is None else v)" "$1" "$2"
}

_set() {
  # _set <json> <key> <value>  (value treated as string or null)
  /usr/bin/python3 -c "
import sys, json
d = json.loads(sys.argv[1])
key = sys.argv[2]
val = sys.argv[3]
if val == 'NULL':
    d[key] = None
elif val.isdigit():
    d[key] = int(val)
else:
    d[key] = val
print(json.dumps(d))
" "$1" "$2" "$3"
}

_notify_macos() {
  local title="$1"
  local message="$2"
  /usr/bin/osascript -e "display notification \"${message//\"/\\\"}\" with title \"${title//\"/\\\"}\" sound name \"Sosumi\"" 2>/dev/null || true
}

_notify_telegram() {
  local message="$1"
  if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
    return 0
  fi
  /usr/bin/curl -s -m 10 \
    -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    > /dev/null 2>&1 || true
}

_alert() {
  local title="$1"
  local message="$2"
  _notify_macos "$title" "$message"
  _notify_telegram "$title: $message"
  _log "ALERT $title: $message"
}

# Single ping with timeout. Returns 0 if reachable, non-zero otherwise.
if /sbin/ping -c 1 -W "$((TIMEOUT * 1000))" "$HOST" > /dev/null 2>&1 \
   || /sbin/ping -c 1 -t "$TIMEOUT" "$HOST" > /dev/null 2>&1; then
  REACHABLE=1
else
  REACHABLE=0
fi

STATE=$(_load_state)
PREV_FAILS=$(_get "$STATE" consecutive_failures)
PREV_STATUS=$(_get "$STATE" status)
PREV_DOWN_SINCE=$(_get "$STATE" down_since)
PREV_ALERT_SENT=$(_get "$STATE" last_alert_sent)

if [[ "$REACHABLE" -eq 1 ]]; then
  if [[ "$PREV_STATUS" == "down_alerted" ]]; then
    # Recovery
    _alert "Compounder recovered" "Server $HOST reachable again after being down since $PREV_DOWN_SINCE."
    NEW_STATE=$(_set "$STATE" consecutive_failures 0)
    NEW_STATE=$(_set "$NEW_STATE" down_since NULL)
    NEW_STATE=$(_set "$NEW_STATE" last_alert_sent "$(_ts)")
    NEW_STATE=$(_set "$NEW_STATE" status up)
  else
    NEW_STATE=$(_set "$STATE" consecutive_failures 0)
    NEW_STATE=$(_set "$NEW_STATE" down_since NULL)
    NEW_STATE=$(_set "$NEW_STATE" status up)
  fi
  _log "up host=$HOST"
else
  NEW_FAILS=$((PREV_FAILS + 1))
  NEW_STATE=$(_set "$STATE" consecutive_failures "$NEW_FAILS")
  if [[ "$PREV_DOWN_SINCE" == "" || "$PREV_DOWN_SINCE" == "None" ]]; then
    NEW_STATE=$(_set "$NEW_STATE" down_since "$(_ts)")
  fi
  if [[ "$NEW_FAILS" -ge "$ALERT_AFTER_FAILS" && "$PREV_STATUS" != "down_alerted" ]]; then
    _alert "Compounder offline" "Server $HOST unreachable for $NEW_FAILS consecutive checks. Down since $(_get "$NEW_STATE" down_since)."
    NEW_STATE=$(_set "$NEW_STATE" last_alert_sent "$(_ts)")
    NEW_STATE=$(_set "$NEW_STATE" status down_alerted)
  else
    NEW_STATE=$(_set "$NEW_STATE" status down)
  fi
  _log "fail host=$HOST consecutive=$NEW_FAILS"
fi

_save_state "$NEW_STATE"
