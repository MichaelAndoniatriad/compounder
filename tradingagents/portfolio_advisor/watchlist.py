"""Candidate watchlist — flat file the user edits, scanned by the PM.

The watchlist is a JSON file at advisor_dir/watchlist.json.
Format:
  {
    "tickers": [
      {"ticker": "HIMS", "thesis": "men's health DTC, strong NRR, no moat analysis yet"},
      {"ticker": "DUOL", "thesis": "language learning SaaS, strong retention, waiting on valuation"},
      "CELH"
    ]
  }

Entries can be plain ticker strings (no thesis yet) or objects with a thesis field.
The scan_watchlist() function routes each entry through candidates.evaluate_candidate()
and queues research jobs for those that pass the gates.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tradingagents.portfolio_advisor import state

logger = logging.getLogger(__name__)


def _watchlist_path(cfg: Dict[str, Any]) -> Path:
    return state.advisor_dir(cfg) / "watchlist.json"


def load_watchlist(cfg: Dict[str, Any]) -> List[Any]:
    path = _watchlist_path(cfg)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        entries = raw.get("tickers") or []
        return entries if isinstance(entries, list) else []
    return []


def save_watchlist(cfg: Dict[str, Any], entries: List[Any]) -> None:
    path = _watchlist_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"tickers": entries}, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def add_to_watchlist(
    cfg: Dict[str, Any],
    ticker: str,
    thesis: str = "",
    *,
    strategy: str = "core",
    catalyst_date: str = "",
) -> bool:
    entries = load_watchlist(cfg)
    ticker = ticker.strip().upper()
    strategy = (strategy or "core").strip().lower()
    if strategy not in ("core", "catalyst"):
        strategy = "core"
    for e in entries:
        existing = (e if isinstance(e, str) else e.get("ticker", "")).strip().upper()
        if existing == ticker:
            return False
    entry: Dict[str, Any] = {"ticker": ticker, "strategy": strategy}
    if thesis:
        entry["thesis"] = thesis
    if catalyst_date:
        entry["catalyst_date"] = catalyst_date
    entries.append(entry)
    save_watchlist(cfg, entries)
    return True


def remove_from_watchlist(cfg: Dict[str, Any], ticker: str) -> bool:
    entries = load_watchlist(cfg)
    ticker = ticker.strip().upper()
    before = len(entries)
    entries = [
        e for e in entries
        if (e if isinstance(e, str) else e.get("ticker", "")).strip().upper() != ticker
    ]
    if len(entries) == before:
        return False
    save_watchlist(cfg, entries)
    return True


def scan_watchlist(
    cfg: Dict[str, Any],
    live_tickers: Optional[Any] = None,
    *,
    queue_jobs: bool = True,
) -> Dict[str, Any]:
    """Gate all watchlist entries, queue research for those that pass.

    Returns a summary dict with counts and per-ticker outcomes.
    """
    from tradingagents.portfolio_advisor.candidates import (
        evaluate_candidates_with_evidence,
        append_candidate_records,
        queue_candidate_research_jobs,
    )

    entries = load_watchlist(cfg)
    if not entries:
        return {"scanned": 0, "queued": 0, "rejected": [], "watch": [], "promoted": []}

    live = set()
    if live_tickers is not None:
        live = {str(t).strip().upper() for t in live_tickers if str(t).strip()}

    records = evaluate_candidates_with_evidence(
        cfg,
        entries,
        live_tickers=live,
        default_source="watchlist_scan",
    )

    append_candidate_records(cfg, records)

    queued = 0
    if queue_jobs:
        queued = queue_candidate_research_jobs(cfg, records)

    rejected = [r.ticker for r in records if r.status == "rejected"]
    watch = [r.ticker for r in records if r.status == "watch"]
    promoted = [r.ticker for r in records if r.status in ("promoted", "research_queued")]

    logger.info(
        "watchlist scan: %d entries, %d queued, %d rejected, %d watch, %d promoted/queued",
        len(records), queued, len(rejected), len(watch), len(promoted),
    )

    return {
        "scanned": len(records),
        "queued": queued,
        "rejected": rejected,
        "watch": watch,
        "promoted": promoted,
        "records": [r.to_dict() for r in records],
    }


def scan_and_queue_one_deep(
    cfg: Dict[str, Any],
    live_tickers: Optional[Any] = None,
    *,
    generate: bool = True,
    generate_n: int = 8,
) -> Dict[str, Any]:
    """Biweekly deep scan: discover ideas, gate the watchlist, pick the best, send it to full_graph.

    When generate=True, first asks the idea generator to discover fresh growth candidates and add
    them to the watchlist (Step 1 of the mandate). Then gates everything, picks one name (research-
    needed first, then priority), and queues a full_graph job. The next run-due tick runs the deep
    research; on completion the candidate pipeline runs the PM comparison to decide. Returns a summary.
    """
    from tradingagents.portfolio_advisor.candidates import (
        evaluate_candidates_with_evidence,
        append_candidate_records,
        queue_deep_candidate_job,
    )

    generated: Dict[str, Any] = {}
    if generate:
        try:
            from tradingagents.portfolio_advisor.idea_generator import discover_ideas_to_watchlist
            generated = discover_ideas_to_watchlist(cfg, live_tickers=live_tickers, n=generate_n)
        except Exception as e:
            logger.warning("idea generation failed; proceeding with existing watchlist: %s", e)

    entries = load_watchlist(cfg)
    if not entries:
        return {"picked": None, "reason": "watchlist empty (idea generation produced nothing)", "generated": generated}

    live = {str(t).strip().upper() for t in (live_tickers or []) if str(t).strip()}
    records = evaluate_candidates_with_evidence(
        cfg, entries, live_tickers=live, default_source="watchlist_biweekly"
    )
    append_candidate_records(cfg, records)

    eligible = [r for r in records if r.status != "rejected"]
    if not eligible:
        return {
            "picked": None,
            "reason": "all candidates rejected by gates",
            "rejected": [r.ticker for r in records],
            "generated": generated,
        }

    # Prefer candidates that still need research over already-promoted ones, then by priority.
    status_rank = {"research_queued": 0, "watch": 1, "candidate": 1, "promoted": 2}
    best = sorted(eligible, key=lambda r: (status_rank.get(r.status, 3), r.priority, r.ticker))[0]

    queued = queue_deep_candidate_job(
        cfg,
        best.ticker,
        reason=best.reason or f"Biweekly watchlist deep dive on {best.ticker}",
        strategy=best.strategy,
    )

    logger.info(
        "watchlist biweekly deep scan: picked %s [%s] (priority %s, status %s), queued=%s",
        best.ticker, best.strategy, best.priority, best.status, queued,
    )
    return {
        "picked": best.ticker,
        "strategy": best.strategy,
        "priority": best.priority,
        "status": best.status,
        "queued": queued,
        "eligible": [r.ticker for r in eligible],
        "rejected": [r.ticker for r in records if r.status == "rejected"],
        "generated": generated,
    }


def watchlist_summary_for_pm(cfg: Dict[str, Any]) -> str:
    """Short block showing the current watchlist for the PM prompt."""
    entries = load_watchlist(cfg)
    if not entries:
        return ""
    lines = [f"Candidate watchlist ({len(entries)} entries — not yet researched or gated):"]
    for e in entries[:20]:
        if isinstance(e, str):
            lines.append(f"  {e.strip().upper()} [core]: (no thesis written yet)")
        elif isinstance(e, dict):
            ticker = str(e.get("ticker") or "").strip().upper()
            thesis = str(e.get("thesis") or "").strip()
            strategy = str(e.get("strategy") or "core").strip().lower()
            cat = str(e.get("catalyst_date") or "").strip()
            cat_s = f" (catalyst: {cat})" if cat else ""
            lines.append(f"  {ticker} [{strategy}]{cat_s}: {thesis or '(no thesis written yet)'}")
    if len(entries) > 20:
        lines.append(f"  ... and {len(entries) - 20} more")
    return "\n".join(lines) + "\n"
