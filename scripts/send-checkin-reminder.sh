#!/usr/bin/env bash
# Daily Telegram check-in reminder for the compounder advisor.
# Sends a short, glanceable status so you can eyeball that the local-first
# advisor is alive: last heartbeat, pending recommendations, last EP scan,
# this month's core picks. Scheduled via launchd (com.compounder.checkin).
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

LOG="$HOME/.tradingagents/logs/advisor-checkin.log"
mkdir -p "$(dirname "$LOG")"

{
  echo "===== $(date '+%Y-%m-%dT%H:%M:%S%z') checkin start ====="
  "$PY" - <<'PYEOF'
import json
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio_advisor.messaging import send_telegram_message

cfg = DEFAULT_CONFIG.copy()
home = Path.home()
hb_dir = home / ".tradingagents" / "run" / "heartbeats"
now = datetime.now(timezone.utc)


def _age(ts_file: Path) -> str:
    try:
        ts = datetime.fromisoformat(ts_file.read_text().strip().replace("Z", "+00:00"))
        hrs = (now - ts).total_seconds() / 3600.0
        if hrs < 1:
            return f"{int(hrs * 60)}m ago"
        if hrs < 48:
            return f"{hrs:.0f}h ago"
        return f"{hrs / 24:.0f}d ago"
    except Exception:
        return "never"


# Pending recommendations awaiting outcome
pending = "?"
try:
    from tradingagents.portfolio_advisor import recommendation_log
    pending = str(len(recommendation_log.load_pending(cfg)))
except Exception as e:
    pending = f"err ({type(e).__name__})"

# This month's core picks
core_line = "no scan this month"
try:
    cache = sorted((home / ".tradingagents" / "cache").glob("core_discovery_picks_*.json"))
    if cache:
        picks = json.loads(cache[-1].read_text()).get("picks", [])
        deep = [p["ticker"] for p in picks if p.get("deep_dive") == "YES"]
        radar = [p["ticker"] for p in picks if p.get("deep_dive") != "YES"]
        core_line = f"deep-dive {', '.join(deep) or '—'}; radar {', '.join(radar) or '—'}"
except Exception:
    pass

lines = [
    f"• Heartbeat: {_age(hb_dir / 'heartbeat-heartbeat.ts')}",
    f"• Last EP scan: {_age(hb_dir / 'heartbeat-ep-scan.ts')}",
    f"• Pending recs: {pending}",
    f"• Core picks (this month): {core_line}",
    "",
    "Glance at the logs if anything looks stale:",
    "  tail ~/.tradingagents/logs/advisor-*.log",
]
body = "\n".join(lines)
ok = send_telegram_message(cfg, "⏰ Compounder check-in", body)
print("sent" if ok else "send FAILED (check token/chat_id)")
PYEOF
  echo "===== $(date '+%Y-%m-%dT%H:%M:%S%z') checkin end ====="
} >>"$LOG" 2>&1
