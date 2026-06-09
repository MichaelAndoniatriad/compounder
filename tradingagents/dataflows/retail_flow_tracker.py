"""Retail Flow Tracker — pulls Nasdaq Retail Activity data for consensus tickers.

Phase A of AI Consensus Guardrails. Daily cron. Pulls retail share of ADV
for current holdings, consensus top 50, and watchlist tickers. Caches at
~/.tradingagents/cache/retail_flow/YYYY-MM-DD.json with staleness gates.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "retail_flow"
_MAX_STALE_DAYS = 5

# Nasdaq Retail Activity Tracker endpoint
_NASDAQ_RETAIL_URL = (
    "https://www.nasdaqtrader.com/Trader.aspx?id=RetailActivityTracker"
)


def get_retail_flow_share(ticker: str) -> Optional[Dict[str, Any]]:
    """Return {share_30d, trend_7d, trend_30d, adv_dollar, stale_days} or None.

    Rejects data older than 5 days. Returns None if no data found or stale.
    """
    tk = ticker.strip().upper()
    today = date.today()

    # Try today's cache first, then recent days
    for days_back in range(_MAX_STALE_DAYS + 1):
        day = today - timedelta(days=days_back)
        cache_file = _CACHE_DIR / day.isoformat() / f"{tk}.json"
        if cache_file.is_file():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                data["stale_days"] = days_back
                return data
            except (json.JSONDecodeError, OSError):
                continue

    return None


def fetch_retail_flow_batch(tickers: List[str]) -> Dict[str, Any]:
    """Pull Nasdaq retail activity data for a batch of tickers.

    Parses the Nasdaq Retail Activity Tracker page. Falls back gracefully
    if the page structure changes or is unavailable.

    Returns {tickers: {ticker: data}, date, source, errors}.
    """
    today = date.today().isoformat()
    day_dir = _CACHE_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)

    results: Dict[str, Dict] = {}
    errors: List[str] = []

    try:
        resp = requests.get(_NASDAQ_RETAIL_URL, timeout=30,
                           headers={"User-Agent": "TradingAgents/1.0"})
        if resp.status_code != 200:
            errors.append(f"Nasdaq returned HTTP {resp.status_code}")
            return _fallback_or_cache(tickers, today, day_dir, errors)

        html = resp.text

        # Parse the page for ticker data rows
        # The Nasdaq page uses various table formats; try multiple patterns
        for ticker in tickers:
            data = _parse_nasdaq_row(html, ticker)
            if data:
                results[ticker] = data
                # Cache individual ticker result
                ticker_file = day_dir / f"{ticker}.json"
                ticker_file.write_text(json.dumps(data, ensure_ascii=False))
            else:
                errors.append(f"{ticker}: not found in Nasdaq data")

    except requests.RequestException as e:
        errors.append(f"Nasdaq fetch failed: {e}")
        return _fallback_or_cache(tickers, today, day_dir, errors)

    return {
        "date": today,
        "tickers_retrieved": len(results),
        "tickers_requested": len(tickers),
        "source": "nasdaq_retail_tracker",
        "results": results,
        "errors": errors,
    }


def _parse_nasdaq_row(html: str, ticker: str) -> Optional[Dict[str, Any]]:
    """Parse a single ticker's retail flow data from Nasdaq HTML.

    Nasdaq uses multiple table formats. Try known patterns.
    Returns None if ticker not found.
    """
    import re

    # Pattern 1: Look for ticker in table rows with retail metrics
    # Typical format: <tr> ... <td>$TICKER</td> ... <td>XX%</td> ...
    pattern = re.compile(
        r'<tr[^>]*>.*?(\$?' + re.escape(ticker) + r')'
        r'.*?<td[^>]*>(\d+\.?\d*%?)</td>'
        r'.*?<td[^>]*>(\d+\.?\d*%?)</td>',
        re.IGNORECASE | re.DOTALL,
    )

    match = pattern.search(html)
    if not match:
        return None

    try:
        share_30d = _parse_pct(match.group(2))
        trend_7d = _parse_pct(match.group(3))
    except (IndexError, ValueError):
        return None

    return {
        "ticker": ticker,
        "share_30d": share_30d,
        "trend_7d": trend_7d,
        "trend_30d": 0.0,  # Not parseable from single-row pattern
        "adv_dollar": 0,   # Not parseable from this pattern
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _parse_pct(text: str) -> float:
    """Parse a percentage string like '12.5%' to float 0.125."""
    text = text.strip().replace("%", "")
    val = float(text) / 100.0
    return round(val, 4)


def _fallback_or_cache(
    tickers: List[str], today: str, day_dir: Path, errors: List[str],
) -> Dict[str, Any]:
    """Use most recent cached data as fallback when Nasdaq is unavailable."""
    results: Dict[str, Dict] = {}
    for days_back in range(1, _MAX_STALE_DAYS + 1):
        day = (date.today() - timedelta(days=days_back)).isoformat()
        for ticker in tickers:
            if ticker in results:
                continue
            cache_file = _CACHE_DIR / day / f"{ticker}.json"
            if cache_file.is_file():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    data["stale_days"] = days_back
                    results[ticker] = data
                except (json.JSONDecodeError, OSError):
                    pass
        if len(results) == len(tickers):
            break

    return {
        "date": today,
        "tickers_retrieved": len(results),
        "tickers_requested": len(tickers),
        "source": "cache_fallback",
        "results": results,
        "errors": errors,
    }


def _get_watchlist_tickers() -> Set[str]:
    """Get watchlist tickers from state."""
    try:
        from tradingagents.portfolio_advisor.state import load_state
        from tradingagents.portfolio_advisor.watchlist import load_watchlist
        from tradingagents.default_config import DEFAULT_CONFIG
        cfg = DEFAULT_CONFIG.copy()
        watchlist = load_watchlist(cfg)
        return {t.strip().upper() for t in watchlist if t and str(t).strip()}
    except Exception:
        return set()


def _get_consensus_tickers() -> Set[str]:
    """Get consensus top 50 tickers from snapshot."""
    try:
        from tradingagents.dataflows.llm_consensus import load_llm_consensus_snapshot
        snapshot = load_llm_consensus_snapshot()
        if snapshot:
            return {t["ticker"] for t in snapshot.get("top_20", [])}
    except Exception:
        pass
    return set()


def build_retail_ticker_list() -> List[str]:
    """Build the list of tickers to track: holdings + consensus top 50 + watchlist."""
    tickers: Set[str] = set()

    try:
        from tradingagents.portfolio_advisor.etoro_scan import fetch_portfolio_rows
        _, _, live_tickers, _ = fetch_portfolio_rows()
        tickers.update(t.strip().upper() for t in live_tickers if t and str(t).strip())
    except Exception:
        pass

    tickers.update(_get_consensus_tickers())
    tickers.update(_get_watchlist_tickers())

    return sorted(tickers)[:100]  # Cap at 100 to control cost
