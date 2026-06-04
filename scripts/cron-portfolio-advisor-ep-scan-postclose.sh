#!/bin/bash
# Post-close EP catalyst scan. Runs at 16:15 ET — after the market close —
# so the gap-hold gate (Section 5.2) can be verified before the AI issues
# a recommendation. Pre-filter + PM classification + Telegram push.
#
# Cron fires at BOTH EDT and EST UTC times for the post-close window; the
# DST-safe gate below only proceeds at the real ET marker (16:15 ET).
#
#   15 20 * * 1-5   15 21 * * 1-5    # 16:15 ET post-close
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="${PORTFOLIO_ADVISOR_EP_SCAN_CRON_LOG:-$HOME/.tradingagents/logs/portfolio-advisor-ep-scan-postclose.log}"
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

# DST-aware gate: only proceed at the 16:15 ET post-close marker (+/-20 min).
GATE="$("$PY" - <<'PYEOF'
try:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    mins = now.hour * 60 + now.minute
    target = 16 * 60 + 15
    ok = now.weekday() < 5 and abs(mins - target) <= 20
    print("RUN" if ok else "SKIP")
except Exception:
    print("RUN")
PYEOF
)"
if [[ "$GATE" != "RUN" ]]; then
  echo "$(_ts) ep-scan-postclose: outside 16:15 ET window ($(TZ=America/New_York date '+%H:%M %Z')); skipping" >>"$LOG"
  exit 0
fi

LOCK="$HOME/.tradingagents/run/cron-portfolio-advisor-ep-scan-postclose.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) cron-portfolio-advisor-ep-scan-postclose: another instance holds the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) portfolio advisor ep-scan POST-CLOSE start ($(TZ=America/New_York date '+%H:%M %Z')) ====="
  set +e
  "$PY" -m cli.main advisor portfolio ep-scan --post-close
  ec=$?
  set -e
  echo "===== $(_ts) portfolio advisor ep-scan POST-CLOSE end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
