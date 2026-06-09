"""Mechanical pre-filter for core position discovery.

Layer 1: deterministic, no LLM. Runs before quantitative screen.
Filters out stocks that definitively cannot qualify. Fast, debuggable,
logs every rejection with reason.

Eliminates ~80% of universe before yfinance detail calls.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

# Hard disqualifiers — if ANY are true, the stock is out
MIN_PRICE = 5.0  # Below $5: manipulation risk, liquidity issues
MIN_MARKET_CAP = 500_000_000  # $500M (wider than quant screen's $1B)
MIN_AVG_VOLUME = 500_000  # 500K shares/day minimum liquidity
MAX_DEBT_EQUITY = 5.0  # Debt/equity above 5x: balance sheet risk
# EPS is no longer a hard gate — replaced by fragility gate (Phase 1a):
# unprofitable names survive if they show ≥20% revenue growth + ≥40% gross margins.


def mechanical_filter(tickers: List[str], reject_log_path: str = "") -> Tuple[List[str], Dict[str, List[str]]]:
    """Run fast disqualification pass. Returns (survivors, rejections).

    Uses yfinance fast_info where available (single API call per ticker).
    Falls back to full info for tickers where fast_info is unavailable.

    reject_log_path: if set, writes rejection CSV for audit.
    """
    import yfinance as yf

    survivors: List[str] = []
    rejections: Dict[str, List[str]] = {}  # reason -> list of tickers

    for ticker in tickers:
        reason = _check_one(ticker)
        if reason:
            rejections.setdefault(reason, []).append(ticker)
        else:
            survivors.append(ticker)

    logger.info(
        "mechanical filter: %d survivors from %d (%d rejected: %s)",
        len(survivors), len(tickers),
        len(tickers) - len(survivors),
        ", ".join(f"{r}:{len(ts)}" for r, ts in sorted(rejections.items()))
    )

    # Write audit log
    if reject_log_path:
        _write_reject_log(reject_log_path, rejections, len(tickers), len(survivors))

    return survivors, rejections


def _check_one(ticker: str) -> str:
    """Return rejection reason string, or empty string if passes."""
    import yfinance as yf

    try:
        t = yf.Ticker(ticker)

        # Try fast_info first (single call, few fields)
        try:
            fi = t.fast_info
            price = getattr(fi, "last_price", None) or getattr(fi, "regularMarketPrice", None) or 0
            market_cap = getattr(fi, "market_cap", None) or getattr(fi, "marketCap", None) or 0
        except Exception:
            price = 0
            market_cap = 0

        # Price check
        if price and price < MIN_PRICE:
            return "price_below_5"

        # Market cap check
        if market_cap and market_cap < MIN_MARKET_CAP:
            return "market_cap_below_500M"

        # For detailed checks, use full info
        info = t.info
        if not info:
            return "no_data"

        # Not a common stock
        qt = info.get("quoteType", "")
        if qt and qt not in ("EQUITY", None):
            return "not_equity"

        # Volume check
        avg_vol = info.get("averageVolume", 0) or info.get("avgVolume", 0) or 0
        if avg_vol and avg_vol < MIN_AVG_VOLUME:
            return "low_volume"

        # EPS check — fragility gate: unprofitable names survive only if they show
        # credible growth-investment characteristics (high growth + high margins).
        eps = info.get("trailingEps") or info.get("epsTrailingTwelveMonths")
        if eps is not None and eps <= 0:
            rev_growth   = info.get("revenueGrowth")      # yfinance fraction, may be None
            gross_margin = info.get("grossMargins")       # yfinance fraction, may be None
            # Only reject if we HAVE the data AND it fails the growth-investment bar.
            if rev_growth is not None and gross_margin is not None:
                if not (rev_growth >= 0.20 and gross_margin >= 0.40):
                    return "unprofitable_no_growth"
            # else: data missing → pass through to the AV quant screen (lenient)

        # Debt check
        de = info.get("debtToEquity", None)
        if de is not None and de > MAX_DEBT_EQUITY:
            return "high_debt_equity"

        return ""  # Passed all checks

    except Exception as e:
        logger.debug("mechanical filter error for %s: %s", ticker, e)
        return "fetch_error"


def _write_reject_log(path: str, rejections: Dict[str, List[str]],
                     total: int, survived: int) -> None:
    """Write rejection audit CSV."""
    try:
        with open(path, "w") as f:
            f.write("reason,count,tickers\n")
            for reason, tickers in sorted(rejections.items(), key=lambda x: -len(x[1])):
                f.write(f"{reason},{len(tickers)},{' '.join(tickers[:20])}\n")
            f.write(f"TOTAL,{total},\n")
            f.write(f"SURVIVED,{survived},\n")
    except OSError:
        pass
