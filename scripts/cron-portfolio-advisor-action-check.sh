#!/bin/bash
# Proactive PM action-check. Runs one PM cycle and messages the human ONLY when
# there is an action to take. Decisions the human already made suppress settled calls.
#
# DST-safe scheduling: cron fires this at BOTH the EDT and EST UTC times for the
# three US-market session markers (open / midday / close). A timezone gate below
# only proceeds when the current America/New_York time is at a real marker, so
# exactly three runs go through per trading day regardless of daylight saving.
#
#   35 13 * * 1-5   35 14 * * 1-5    # open   (09:35 ET)
#   30 16 * * 1-5   30 17 * * 1-5    # midday (12:30 ET)
#   55 19 * * 1-5   55 20 * * 1-5    # close  (15:55 ET)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="${PORTFOLIO_ADVISOR_ACTION_CHECK_CRON_LOG:-$HOME/.tradingagents/logs/portfolio-advisor-action-check.log}"
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

# --- DST-aware gate: only proceed near a US-market session marker (ET) --------
# Markers (America/New_York): 09:35 open, 12:30 midday, 15:55 close. The +/-20min
# tolerance absorbs cron lag; wrong-season fires land ~60min off a marker and skip.
GATE="$("$PY" - <<'PYEOF'
try:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    markers = [(9, 35), (12, 30), (15, 55)]
    mins = now.hour * 60 + now.minute
    ok = now.weekday() < 5 and any(abs(mins - (h * 60 + m)) <= 20 for h, m in markers)
    print("RUN" if ok else "SKIP")
except Exception:
    # Fail open: if zoneinfo is unavailable, don't silently stop alerting.
    print("RUN")
PYEOF
)"
if [[ "$GATE" != "RUN" ]]; then
  echo "$(_ts) action-check: outside ET session markers ($(TZ=America/New_York date '+%H:%M %Z')); skipping" >>"$LOG"
  exit 0
fi

LOCK="$HOME/.tradingagents/run/cron-portfolio-advisor-action-check.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) cron-portfolio-advisor-action-check: another instance holds the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) portfolio advisor action-check start ($(TZ=America/New_York date '+%H:%M %Z')) ====="
  set +e
  "$PY" -m cli.main advisor portfolio action-check
  ec=$?
  set -e
  if [[ $ec -eq 0 ]]; then
    mkdir -p "$HOME/.tradingagents/run/heartbeats"
    date -u "+%Y-%m-%dT%H:%M:%SZ" > "$HOME/.tradingagents/run/heartbeats/heartbeat-action-check.ts"
  fi
  echo "===== $(_ts) portfolio advisor action-check end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
