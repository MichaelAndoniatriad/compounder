# Mac Watchdog

External reachability check for the Hetzner trading server. Runs on the Mac. Alerts via macOS notification (and optional Telegram) when the server has been unreachable for the alert threshold.

Complements `scripts/dead-mans-switch.sh`, which runs on the server and cannot self-report when the server itself is offline.

## Install

```bash
# 1. Make the script executable
chmod +x ~/workspace/trading-agents/scripts/mac-watchdog.sh

# 2. (Optional) Set Telegram credentials in your shell rc so they persist
# Add these to ~/.zshrc (or ~/.bash_profile):
#   export MAC_WATCHDOG_TELEGRAM_BOT_TOKEN="..."
#   export MAC_WATCHDOG_TELEGRAM_CHAT_ID="..."
# Without these, alerts go to macOS notification only.

# 3. Install the cron entry (runs every 5 minutes)
( crontab -l 2>/dev/null; echo "*/5 * * * * /Users/michaelandonia/workspace/trading-agents/scripts/mac-watchdog.sh" ) | crontab -

# 4. Confirm it is installed
crontab -l | grep mac-watchdog
```

## Behaviour

- Every 5 minutes, pings the server (default `116.203.153.58`).
- Maintains a state file at `/tmp/mac-watchdog.state.json`.
- After 6 consecutive failures (30 minutes), raises a macOS notification with sound. Sends a Telegram alert too if credentials are set.
- When server returns, raises a recovery notification.
- Logs every check to `~/Library/Logs/mac-watchdog.log`.

## Test

Force a failure to confirm alerting fires:

```bash
# Point at a non routable IP to trigger consecutive failures
MAC_WATCHDOG_HOST="192.0.2.1" MAC_WATCHDOG_ALERT_AFTER_FAILS=1 \
  ~/workspace/trading-agents/scripts/mac-watchdog.sh

# Check the state and log
cat /tmp/mac-watchdog.state.json
tail -5 ~/Library/Logs/mac-watchdog.log
```

You should see a macOS notification within seconds. If Telegram creds are set, you should also see a Telegram message.

## Uninstall

```bash
( crontab -l 2>/dev/null | grep -v mac-watchdog.sh || true ) | crontab -
rm -f /tmp/mac-watchdog.state.json
```

## Configuration

Override defaults via environment variables in the cron line or your shell rc:

| Variable | Default | Notes |
|----------|---------|-------|
| `MAC_WATCHDOG_HOST` | `116.203.153.58` | Server IP or hostname |
| `MAC_WATCHDOG_ALERT_AFTER_FAILS` | `6` | Failures before alerting (6 × 5min = 30min) |
| `MAC_WATCHDOG_TIMEOUT` | `5` | Ping timeout seconds |
| `MAC_WATCHDOG_TELEGRAM_BOT_TOKEN` | empty | Optional |
| `MAC_WATCHDOG_TELEGRAM_CHAT_ID` | empty | Required if bot token set |
| `MAC_WATCHDOG_LOG` | `~/Library/Logs/mac-watchdog.log` | Log path |

## Limits

- Single host watchdog. If you add more servers, copy the script.
- macOS sleeps: when the Mac is asleep, the watchdog does not run. That is acceptable for a personal setup but not for production reliability.
- Network failure on the Mac side will trigger false alerts. Acceptable trade off.
