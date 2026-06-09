#!/usr/bin/env bash
# Stand up (or tear down) the portfolio advisor as local macOS launchd agents.
# Local-first: no server. Run from the repo root.
#
#   scripts/setup-local-advisor.sh            # install + load all agents
#   scripts/setup-local-advisor.sh uninstall  # unload + remove all agents
#
# Times are Mac LOCAL time. UK↔US-Eastern offset is a constant 5h (both observe
# DST together), so local times map cleanly to ET. Market-tied jobs (watchdog,
# ep-scan) self-skip outside US equity hours, so generous scheduling is safe.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/.tradingagents/logs"
RUN="$ROOT/scripts/run-advisor.sh"
mkdir -p "$LA" "$LOGDIR"

LABELS=(
  com.compounder.watchdog
  com.compounder.run-due
  com.compounder.ep-scan
  com.compounder.ep-scan-postclose
  com.compounder.morning-digest
  com.compounder.evening-digest
  com.compounder.weekly
  com.compounder.core-scan
  com.compounder.consensus-scrape
  com.compounder.measure-outcomes
  com.compounder.heartbeat
  com.compounder.checkin
  com.compounder.telegram-listener
)

uninstall() {
  for L in "${LABELS[@]}"; do
    P="$LA/$L.plist"
    launchctl unload "$P" 2>/dev/null || true
    rm -f "$P"
    echo "removed $L"
  done
  echo "All compounder agents unloaded and removed."
}

# write_plist <label> <schedule-xml> <program-arg-string>
write_plist() {
  local label="$1" sched="$2" prog="$3"
  cat > "$LA/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>$prog</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
$sched
  <key>StandardOutPath</key><string>$LOGDIR/launchd-$label.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/launchd-$label.err.log</string>
</dict>
</plist>
PLIST
  launchctl unload "$LA/$label.plist" 2>/dev/null || true
  launchctl load "$LA/$label.plist"
  echo "loaded $label"
}

interval() { printf '  <key>StartInterval</key><integer>%s</integer>' "$1"; }
cal() { # cal <Hour> <Minute> [extraKey extraVal]
  local h="$1" m="$2" ek="${3:-}" ev="${4:-}"
  printf '  <key>StartCalendarInterval</key>\n  <dict>\n'
  [[ -n "$ek" ]] && printf '    <key>%s</key><integer>%s</integer>\n' "$ek" "$ev"
  printf '    <key>Hour</key><integer>%s</integer>\n    <key>Minute</key><integer>%s</integer>\n  </dict>' "$h" "$m"
}

install() {
  # --- interval jobs (self-gate where market-tied) ---
  write_plist com.compounder.watchdog "$(interval 300)" "exec $RUN watchdog"
  write_plist com.compounder.run-due  "$(interval 900)" "exec $RUN run-due"

  # --- catalyst scans (13:30 local = 08:30 ET pre-market; 21:15 = 16:15 ET post-close) ---
  write_plist com.compounder.ep-scan "$(cal 13 30)" "exec $RUN ep-scan"
  write_plist com.compounder.ep-scan-postclose "$(cal 21 15)" "exec $RUN ep-scan --post-close"

  # --- daily / weekly PM rhythm ---
  write_plist com.compounder.morning-digest "$(cal 13 45)" "exec $RUN morning-digest"
  write_plist com.compounder.evening-digest "$(cal 21 30)" "exec $RUN evening-digest"
  write_plist com.compounder.weekly "$(cal 14 0 Weekday 6)" "exec $RUN weekly"
  write_plist com.compounder.heartbeat "$(cal 8 0)" "exec $RUN heartbeat"
  write_plist com.compounder.measure-outcomes "$(cal 11 0 Weekday 0)" "exec $RUN measure-outcomes"

  # --- daily Telegram check-in reminder (glanceable advisor status) ---
  write_plist com.compounder.checkin "$(cal 9 0)" "exec $ROOT/scripts/send-checkin-reminder.sh"

  # --- monthly core discovery (1st of month, 15:00 local) ---
  write_plist com.compounder.core-scan "$(cal 15 0 Day 1)" "exec $RUN core-scan"

  # --- daily consensus snapshot scrape (feeds the anti-herd tilt) ---
  write_plist com.compounder.consensus-scrape "$(cal 6 30)" \
    "cd $ROOT && set -a && source .env && set +a && export PYTHONPATH=$ROOT && .venv/bin/python -c 'from tradingagents.dataflows.llm_consensus import run_daily_scrape; print(run_daily_scrape())' >> $LOGDIR/advisor-consensus-scrape.log 2>&1"

  # --- inbound Telegram listener (sole listener; keepalive) ---
  cat > "$LA/com.compounder.telegram-listener.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.compounder.telegram-listener</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>exec $ROOT/scripts/run-telegram-listener.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/launchd-telegram-listener.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/launchd-telegram-listener.err.log</string>
</dict>
</plist>
PLIST
  launchctl unload "$LA/com.compounder.telegram-listener.plist" 2>/dev/null || true
  launchctl load "$LA/com.compounder.telegram-listener.plist"
  echo "loaded com.compounder.telegram-listener"

  echo
  echo "All compounder agents installed and loaded."
}

case "${1:-install}" in
  uninstall|teardown|remove) uninstall ;;
  *) install ;;
esac
