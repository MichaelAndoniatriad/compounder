"""Daily LLM spend cap: read cost.jsonl, sum today's cost_usd, compare to cap.

Used by the portfolio advisor service to skip non-urgent job batches when the
configured ``portfolio_advisor_daily_cost_cap_usd`` is exceeded. Watchdog and
chat replies bypass this check (they call LLMs directly, not via run_due_jobs).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _cost_log_path() -> Path:
    override = os.environ.get("TRADINGAGENTS_COST_LOG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tradingagents" / "logs" / "cost.jsonl"


def todays_spend_usd(path: Optional[Path] = None, *, today: Optional[date] = None) -> float:
    """Sum ``cost_usd`` for records dated today (UTC) in the cost log."""
    log = path or _cost_log_path()
    if not log.exists():
        return 0.0
    day = (today or datetime.utcnow().date()).isoformat()
    total = 0.0
    try:
        with log.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = str(rec.get("ts") or "")
                if not ts.startswith(day):
                    continue
                cost = rec.get("cost_usd")
                if cost is None:
                    continue
                try:
                    total += float(cost)
                except (TypeError, ValueError):
                    continue
    except OSError:
        logger.debug("cost log read failed", exc_info=True)
        return 0.0
    return total


def is_over_daily_cap(cfg: Dict[str, Any]) -> tuple[bool, float, float]:
    """Return (over_cap, today_usd, cap_usd). cap<=0 disables the check."""
    raw = cfg.get("portfolio_advisor_daily_cost_cap_usd")
    try:
        cap = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        cap = 0.0
    if cap <= 0:
        return False, 0.0, 0.0
    today_usd = todays_spend_usd()
    return today_usd >= cap, today_usd, cap
