"""Structured market event memory — logs WHY the portfolio moved and builds pattern context.

Stores observations in a JSONL file so the PM can learn recurring patterns:
  - macro events (tariff news, Fed, geopolitical) that cause broad portfolio moves
  - sector rotations that affect specific holdings
  - earnings-driven waves that lift/sink the whole book
  - strategy implications: what to do next time this pattern appears
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CATEGORIES = {"macro", "sector", "fed", "geopolitical", "earnings", "earnings_sector", "other"}
MOVES = {"bull", "bear", "mixed", "flat"}
MAGNITUDES = {"strong", "moderate", "weak"}


def _market_events_path(cfg: Dict[str, Any]) -> Path:
    raw = cfg.get("market_events_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    mem = cfg.get("memory_log_path")
    if isinstance(mem, str) and mem.strip():
        return Path(mem).expanduser().parent / "market_events.jsonl"
    return Path("~/.tradingagents/memory/market_events.jsonl").expanduser()


def append_market_event(cfg: Dict[str, Any], event: Dict[str, Any]) -> str:
    """Write one market event entry to the JSONL store. Returns the event id."""
    p = _market_events_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": event.get("id") or uuid.uuid4().hex[:16],
        "date": event.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "category": (event.get("category") or "other").strip().lower(),
        "cause": (event.get("cause") or "").strip()[:600],
        "market_move": (event.get("market_move") or "mixed").strip().lower(),
        "magnitude": (event.get("magnitude") or "moderate").strip().lower(),
        "portfolio_impact": event.get("portfolio_impact") or {},
        "pattern_tags": event.get("pattern_tags") or [],
        "strategy_implication": (event.get("strategy_implication") or "").strip()[:400],
        "notes": (event.get("notes") or "").strip()[:400],
    }
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("market event logged: %s %s/%s — %s", entry["date"], entry["category"], entry["market_move"], entry["cause"][:80])
    return entry["id"]


def load_recent_market_events(cfg: Dict[str, Any], days: int = 180) -> List[Dict[str, Any]]:
    """Return market events from the last N days, newest first."""
    p = _market_events_path(cfg)
    if not p.is_file():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out: List[Dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("date") or "") >= cutoff:
                out.append(row)
    except OSError:
        return []
    out.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return out


def already_logged_today(cfg: Dict[str, Any], category: str, cause_prefix: str = "") -> bool:
    """Check if a similar event was already logged today to avoid duplicate entries."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    events = load_recent_market_events(cfg, days=2)
    for ev in events:
        if ev.get("date") != today:
            continue
        if ev.get("category", "").lower() != (category or "").strip().lower():
            continue
        if cause_prefix:
            existing_cause = (ev.get("cause") or "").lower()
            if existing_cause.startswith(cause_prefix[:60].lower()):
                return True
    return False


def build_market_memory_block(cfg: Dict[str, Any], days: int = 90) -> str:
    """Format recent market events as a compact PM prompt block.

    Shows the most recent events first, capped at 12 entries to stay within
    prompt budget. Each entry includes cause, portfolio impact, pattern tags,
    and strategy implication so the PM can recognize recurring conditions.
    """
    events = load_recent_market_events(cfg, days=days)
    if not events:
        return ""

    lines = [f"Market memory — recent events that moved the portfolio (last {days}d):"]
    for ev in events[:12]:
        date = ev.get("date", "?")
        cat = ev.get("category", "?")
        move = ev.get("market_move", "?")
        mag = ev.get("magnitude", "?")
        cause = (ev.get("cause") or "").strip()
        tags = ", ".join(ev.get("pattern_tags") or [])
        impact = ev.get("portfolio_impact") or {}
        imp_str = (
            ", ".join(f"{k}:{float(v):+.1f}%" for k, v in sorted(impact.items()))
            if impact else ""
        )
        impl = (ev.get("strategy_implication") or "").strip()

        header = f"{date} [{cat}/{move}/{mag}]: {cause}"
        if imp_str:
            header += f" | portfolio: {imp_str}"
        lines.append(f"  {header}")
        if tags:
            lines.append(f"    patterns: {tags}")
        if impl:
            lines.append(f"    strategy: {impl}")

    lines.append("")
    return "\n".join(lines)
