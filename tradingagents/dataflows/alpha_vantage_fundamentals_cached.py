"""Per-ticker fundamentals via Alpha Vantage OVERVIEW with 7-day disk cache.

Provides get_ticker_fundamentals(ticker) returning a dict with the same
field names that _score_quantitative expects, so core_discovery needs no
changes to its scoring or data shape.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .alpha_vantage_fundamentals import get_fundamentals

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "fundamentals"
CACHE_TTL_DAYS = 7


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}.json"


def _read_cache(ticker: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(ticker)
    if not path.is_file():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fetched_str = entry.get("fetched_at", "")
    if not fetched_str:
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_str)
    except (ValueError, TypeError):
        return None
    age = datetime.now(timezone.utc) - fetched_at
    if age.days >= CACHE_TTL_DAYS:
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def _write_cache(ticker: str, data: Dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(ticker)
    tmp = path.with_suffix(".tmp")
    entry = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_ticker_fundamentals(ticker: str) -> Dict[str, Any]:
    """Return per-ticker fundamentals dict, cached for 7 days.

    Returns dict with yfinance-compatible field names:
      marketCap, revenueGrowth, pegRatio, returnOnEquity,
      grossMargins, name, sector, industry, forwardPE,
      debtToEquity, summary

    Returns empty dict on failure (caller handles).
    """
    ticker = ticker.strip().upper()
    if not ticker:
        return {}

    # Cache hit
    cached = _read_cache(ticker)
    if cached is not None:
        return cached

    # Fetch from Alpha Vantage
    try:
        raw = get_fundamentals(ticker)
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.debug("AV fetch failed for %s: %s", ticker, e)
        return {}

    if not isinstance(data, dict) or not data:
        return {}

    # Check for API error / rate limit
    if "Note" in data or "Information" in data:
        logger.debug("AV rate limit or note for %s: %s", ticker, data.get("Note") or data.get("Information"))
        return {}

    if "Symbol" not in data:
        return {}

    def _f(v: Any) -> float:
        """Cast AV string to float, returning 0 on failure."""
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    market_cap = _f(data.get("MarketCapitalization"))
    rev_growth = _f(data.get("QuarterlyRevenueGrowthYOY"))
    peg = _f(data.get("PEGRatio"))
    roe = _f(data.get("ReturnOnEquityTTM"))

    # Compute gross margin
    gross_profit = _f(data.get("GrossProfitTTM"))
    revenue = _f(data.get("RevenueTTM"))
    gross_margin = gross_profit / revenue if revenue > 0 else 0.0

    fwd_pe = _f(data.get("ForwardPE"))
    ev_to_revenue = _f(data.get("EVToRevenue"))
    price_to_sales = _f(data.get("PriceToSalesRatioTTM"))
    debt_equity_raw = data.get("DebtToEquityRatio")
    if debt_equity_raw is None or str(debt_equity_raw).strip().upper() in ("NONE", ""):
        debt_equity = 0.0
    else:
        debt_equity = _f(debt_equity_raw)

    name = str(data.get("Name") or ticker)
    sector = str(data.get("Sector") or "")
    industry = str(data.get("Industry") or "")
    summary = str(data.get("Description") or "")[:400]

    result = {
        "marketCap": market_cap,
        "revenueGrowth": rev_growth,
        "pegRatio": peg,
        "returnOnEquity": roe,
        "grossMargins": gross_margin,
        "shortName": name,  # yfinance alias
        "name": name,
        "sector": sector,
        "industry": industry,
        "forwardPE": fwd_pe,
        "evToRevenue": ev_to_revenue,
        "priceToSales": price_to_sales,
        "debtToEquity": debt_equity,
        "currentPrice": 0.0,  # AV OVERVIEW does not include price
        "summary": summary,
    }

    _write_cache(ticker, result)
    return result
