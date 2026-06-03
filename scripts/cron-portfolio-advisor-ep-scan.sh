#!/bin/bash
# Pre-market EP catalyst scan. Pulls recent news, surfaces ticker mentions
# with catalyst-keyword hits, applies Section 2 / 4.1 / 9 gates, hands the
# survivors to the PM for Section 3 classification. PM messages the human
# ONLY when a qualifying setup is found.
#
# Cron fires at BOTH EDT and EST UTC times for the pre-market window; the
# DST-safe gate below only proceeds at the real ET marker (08:30 ET).
#
#   30 12 * * 1-5   30 13 * * 1-5    # 08:30 ET pre-market
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="${PORTFOLIO_ADVISOR_EP_SCAN_CRON_LOG:-$HOME/.tradingagents/logs/portfolio-advisor-ep-scan.log}"
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

# DST-aware gate: only proceed at the 08:30 ET pre-market marker (+/-20 min).
GATE="$("$PY" - <<'PYEOF'
try:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    mins = now.hour * 60 + now.minute
    target = 8 * 60 + 30
    ok = now.weekday() < 5 and abs(mins - target) <= 20
    print("RUN" if ok else "SKIP")
except Exception:
    print("RUN")
PYEOF
)"
if [[ "$GATE" != "RUN" ]]; then
  echo "$(_ts) ep-scan: outside 08:30 ET window ($(TZ=America/New_York date '+%H:%M %Z')); skipping" >>"$LOG"
  exit 0
fi

LOCK="$HOME/.tradingagents/run/cron-portfolio-advisor-ep-scan.lock"
mkdir -p "$(dirname "$LOCK")"
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "$(_ts) cron-portfolio-advisor-ep-scan: another instance holds the lock; exiting" >>"$LOG"
  exit 0
fi

{
  echo "===== $(_ts) portfolio advisor ep-scan start ($(TZ=America/New_York date '+%H:%M %Z')) ====="
  set +e
  "$PY" -m cli.main advisor portfolio ep-scan
  ec=$?
  set -e
  echo "===== $(_ts) portfolio advisor ep-scan end (exit $ec) ====="
  exit "$ec"
} >>"$LOG" 2>&1
