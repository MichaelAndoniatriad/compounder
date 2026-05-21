# tradingagents/clerk/deep_runner.py

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from tradingagents.dataflows.config import set_config
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)

# Same canonical order as the interactive CLI
ANALYST_ORDER = ["market", "social", "news", "fundamentals"]


def clerk_deep_ticker_dir(results_dir: Path, ticker: str) -> Path:
    return Path(results_dir).expanduser() / "clerk_deep" / ticker.strip().upper()


def clerk_report_path_for_trade_date(results_dir: Path, ticker: str, trade_date: str) -> Path:
    return clerk_deep_ticker_dir(results_dir, ticker) / f"{trade_date.strip()}_clerk_triggered.md"


def has_clerk_report_for_trade_date(results_dir: Path, ticker: str, trade_date: str) -> bool:
    return clerk_report_path_for_trade_date(results_dir, ticker, trade_date).is_file()


def load_latest_prior_clerk_report_text(
    *,
    results_dir: Path,
    ticker: str,
    max_chars: int = 16_000,
) -> str:
    """Most recently modified ``*_clerk_triggered.md`` under ``clerk_deep/<TICKER>/``."""
    sym = ticker.strip().upper()
    if not sym:
        return ""
    base = Path(results_dir).expanduser() / "clerk_deep" / sym
    if not base.is_dir():
        return ""
    files = [p for p in base.glob("*_clerk_triggered.md") if p.is_file()]
    if not files:
        return ""
    latest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    if len(text) > max_chars:
        cut = max(0, max_chars - 100)
        text = text[:cut].rstrip() + "\n\n[... prior clerk snapshot truncated ...]"
    return text


def normalize_analysts(requested: List[str]) -> List[str]:
    s = {a.lower() for a in requested}
    return [a for a in ANALYST_ORDER if a in s]


def run_deep_research(
    ticker: str,
    trade_date: str,
    analysts: List[str],
    config: Dict[str, Any],
    strategy: str = "core",
) -> Tuple[dict, Any]:
    """Run the full LangGraph pipeline for one ticker (expensive).

    ``strategy`` ('core' or 'catalyst') selects which investor policy the agents apply.
    """
    cfg = config.copy()
    cfg["output_language"] = cfg.get("output_language", "English")
    cfg["active_strategy"] = (strategy or "core").strip().lower()
    set_config(cfg)

    selected = normalize_analysts(analysts)
    if not selected:
        selected = ["news", "fundamentals"]

    graph = TradingAgentsGraph(
        selected_analysts=selected,
        debug=False,
        config=cfg,
    )
    logger.info("Clerk: starting deep research for %s on %s", ticker, trade_date)
    final_state, decision = graph.propagate(ticker, trade_date)
    from tradingagents.portfolio_advisor.advisor_pm import run_pm_after_full_graph_if_enabled

    run_pm_after_full_graph_if_enabled(
        cfg, ticker=ticker, trade_date=trade_date, final_state=final_state
    )
    return final_state, decision


def save_deep_report(
    *,
    results_dir: Path,
    ticker: str,
    trade_date: str,
    final_state: dict,
) -> Path:
    """Write a compact markdown summary under results_dir/clerk_deep/."""
    base = Path(results_dir) / "clerk_deep" / ticker.upper()
    base.mkdir(parents=True, exist_ok=True)
    out = base / f"{trade_date}_clerk_triggered.md"

    parts = [
        f"# Clerk-triggered deep research: {ticker}",
        f"Date: {trade_date}",
        f"Generated (UTC): {datetime.utcnow().isoformat()}Z",
        "",
    ]
    for key, title in [
        ("market_report", "Market"),
        ("sentiment_report", "Sentiment"),
        ("news_report", "News"),
        ("fundamentals_report", "Fundamentals"),
    ]:
        val = final_state.get(key)
        if val:
            parts.append(f"## {title}\n\n{val}\n")

    if final_state.get("investment_plan"):
        parts.append(f"## Research plan\n\n{final_state['investment_plan']}\n")
    if final_state.get("trader_investment_plan"):
        parts.append(f"## Trader plan\n\n{final_state['trader_investment_plan']}\n")
    if final_state.get("final_trade_decision"):
        parts.append(f"## Final decision\n\n{final_state['final_trade_decision']}\n")

    out.write_text("\n".join(parts), encoding="utf-8")
    return out
