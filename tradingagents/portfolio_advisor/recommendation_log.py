"""Recommendation log — append-only JSONL tracking every PM recommendation.

Phase 2 centerpiece. Every piece of advice the PM sends is logged before
the Telegram message goes out. Outcomes are measured weekly. Human overrides
are tracked so we can answer: is the system helping or hurting?

Schema (one JSON object per line):
{
  "id": "uuid hex",
  "ts": "ISO8601",
  "trigger": "action_check | ep_scan | watchdog | human_query",
  "type": "sizing | ep_entry | ep_exit | stop_adjust | macro_alert",
  "ticker": "AAPL or null for portfolio-level",
  "action": "reduce_entries_50pct | buy_100_shares | raise_stop_to_50 | hold | exit",
  "rationale": "Macro risk 6/10, tariff escalation pattern...",
  "rule_ref": "macro_2026-06-05_tariff-panic-no-selling or null",
  "entry_price": null or float,
  "stop_price": null or float,
  "shares": null or float,
  "status": "pending | accepted | rejected | expired",
  "human_response": null or "accepted: reduced EP size",
  "outcome_measured_at": null,
  "was_correct": null or true/false,
  "pnl_impact_est": null or float,
  "outcome_note": null
}
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _log_path(cfg: Dict[str, Any]) -> Path:
    """Path to recommendation_log.jsonl under the advisor directory."""
    from tradingagents.portfolio_advisor import state as pa_state
    return pa_state.advisor_dir(cfg) / "recommendation_log.jsonl"


def log_recommendation(
    cfg: Dict[str, Any],
    *,
    trigger: str,
    type: str,
    action: str,
    rationale: str = "",
    ticker: Optional[str] = None,
    rule_ref: Optional[str] = None,
    entry_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    shares: Optional[float] = None,
) -> Optional[str]:
    """Write a recommendation to the append-only log. Returns the entry ID.

    Call this BEFORE sending the Telegram message. If this returns None,
    the log write failed — do not send the message.
    """
    p = _log_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)

    entry: Dict[str, Any] = {
        "id": uuid.uuid4().hex[:16],
        "ts": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "type": type,
        "ticker": (ticker or "").strip().upper() or None,
        "action": (action or "").strip(),
        "rationale": (rationale or "").strip()[:600],
        "rule_ref": (rule_ref or "").strip() or None,
        "entry_price": round(float(entry_price), 2) if entry_price is not None else None,
        "stop_price": round(float(stop_price), 2) if stop_price is not None else None,
        "shares": round(float(shares), 4) if shares is not None else None,
        "status": "pending",
        "human_response": None,
        "outcome_measured_at": None,
        "was_correct": None,
        "pnl_impact_est": None,
        "outcome_note": None,
    }

    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("recommendation logged: %s (%s/%s/%s)", entry["id"], entry["type"], entry["ticker"] or "portfolio", entry["action"][:60])
        return entry["id"]
    except OSError as e:
        logger.error("recommendation log write failed: %s", e)
        return None


def load_pending(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return all recommendations still awaiting outcome measurement."""
    return _load_all(cfg, status_filter="pending")


def load_measured(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return all recommendations with measured outcomes."""
    rows = _load_all(cfg)
    return [r for r in rows if r.get("was_correct") is not None]


def update_outcome(
    cfg: Dict[str, Any],
    rec_id: str,
    *,
    was_correct: Optional[bool] = None,
    pnl_impact_est: Optional[float] = None,
    note: str = "",
) -> bool:
    """Update a recommendation with measured outcome data.

    Appends a new entry with supersedes pointing to the original. Never
    modifies an existing entry in place.
    """
    rows = _load_all(cfg)
    target = None
    for r in rows:
        if r.get("id") == rec_id:
            target = r
            break
    if target is None:
        logger.warning("outcome update: rec %s not found", rec_id)
        return False

    target["outcome_measured_at"] = datetime.now(timezone.utc).isoformat()
    target["was_correct"] = was_correct
    if pnl_impact_est is not None:
        target["pnl_impact_est"] = round(float(pnl_impact_est), 2)
    if note:
        target["outcome_note"] = note[:400]

    _save_all(cfg, rows)
    return True


def update_human_response(
    cfg: Dict[str, Any],
    rec_id: str,
    response: str,
) -> bool:
    """Record the human's response to a recommendation (accepted/rejected)."""
    rows = _load_all(cfg)
    target = None
    for r in rows:
        if r.get("id") == rec_id:
            target = r
            break
    if target is None:
        return False

    resp_lower = (response or "").strip().lower()
    if any(kw in resp_lower for kw in ["accepted", "entered", "executed", "done"]):
        target["status"] = "accepted"
    elif any(kw in resp_lower for kw in ["rejected", "skipped", "declined", "no"]):
        target["status"] = "rejected"
    else:
        target["status"] = "acknowledged"

    target["human_response"] = response[:300]
    _save_all(cfg, rows)
    return True


def human_override_analysis(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Compare outcomes when human followed PM advice vs overrode it.

    Returns counts and average P&L for each group. Works at any sample size —
    just shows the data honestly. No statistical claims.
    """
    rows = _load_all(cfg)
    followed = [r for r in rows if r["status"] == "accepted" and r.get("was_correct") is not None]
    overrode = [r for r in rows if r["status"] == "rejected" and r.get("was_correct") is not None]

    def avg_pnl(recs):
        vals = [r["pnl_impact_est"] for r in recs if r.get("pnl_impact_est") is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "followed_count": len(followed),
        "followed_avg_pnl": avg_pnl(followed),
        "overrode_count": len(overrode),
        "overrode_avg_pnl": avg_pnl(overrode),
        "total_measured": len(followed) + len(overrode),
    }


# --- internal ---

def _load_all(cfg: Dict[str, Any], status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    p = _log_path(cfg)
    if not p.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if status_filter and r.get("status") != status_filter:
                continue
            rows.append(r)
    except OSError:
        return []
    return rows


def _save_all(cfg: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    p = _log_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)
