"""Mechanical pre-filter for core position discovery.

Layer 1: deterministic, no LLM. Runs before quantitative screen.
Filters out stocks that definitively cannot qualify. Fast, debuggable,
logs every rejection with reason.

Hard gates:
  price >= $5, market cap >= $500M, debt/equity <= 5x.
  20d average dollar volume (shares × price) >= portfolio_advisor_min_dollar_adv
  (default $2M) — this replaces the old flat share-count volume gate for the
  R5 small-cap cohort.  The old 500K-share gate is kept as a secondary floor
  for names where ADV cannot be computed (price unavailable).
  EPS is a fragility gate (Phase 1a), not a hard disqualifier.

Edge zone: names with ADV in [min_dollar_adv, edge_zone_max_adv] are tagged
  edge_zone=True in the metadata returned alongside survivors.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Hard disqualifiers — if ANY are true, the stock is out
MIN_PRICE = 5.0  # Below $5: manipulation risk, liquidity issues
MIN_MARKET_CAP = 500_000_000  # $500M (wider than quant screen's $1B)
MIN_AVG_VOLUME = 500_000  # Fallback share-count floor when ADV unavailable
MAX_DEBT_EQUITY = 5.0  # Debt/equity above 5x: balance sheet risk
# EPS is no longer a hard gate — replaced by fragility gate (Phase 1a):
# unprofitable names survive if they show ≥20% revenue growth + ≥40% gross margins.

# Default dollar-ADV gates (overridden by cfg keys from default_config.py)
_DEFAULT_MIN_DOLLAR_ADV = 2_000_000    # $2M: tradability floor
_DEFAULT_EDGE_ZONE_MAX_ADV = 50_000_000  # $50M: top of edge zone


def mechanical_filter(
    tickers: List[str],
    reject_log_path: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], Dict[str, List[str]]]:
    """Run fast disqualification pass. Returns (survivors, rejections).

    Uses yfinance fast_info where available (single API call per ticker).
    Falls back to full info for tickers where fast_info is unavailable.

    reject_log_path: if set, writes rejection CSV for audit.
    cfg: config dict; reads portfolio_advisor_min_dollar_adv and
         portfolio_advisor_edge_zone_max_adv.
    """
    _cfg = cfg or {}
    min_dollar_adv = float(_cfg.get("portfolio_advisor_min_dollar_adv", _DEFAULT_MIN_DOLLAR_ADV) or _DEFAULT_MIN_DOLLAR_ADV)
    edge_zone_max_adv = float(_cfg.get("portfolio_advisor_edge_zone_max_adv", _DEFAULT_EDGE_ZONE_MAX_ADV) or _DEFAULT_EDGE_ZONE_MAX_ADV)

    survivors: List[str] = []
    rejections: Dict[str, List[str]] = {}  # reason -> list of tickers
    edge_zone_tickers: set[str] = set()

    for ticker in tickers:
        reason, adv = _check_one(ticker, min_dollar_adv=min_dollar_adv)
        if reason:
            rejections.setdefault(reason, []).append(ticker)
        else:
            survivors.append(ticker)
            # Tag edge-zone names: ADV in [min_dollar_adv, edge_zone_max_adv]
            if adv is not None and min_dollar_adv <= adv <= edge_zone_max_adv:
                edge_zone_tickers.add(ticker)

    logger.info(
        "mechanical filter: %d survivors from %d (%d edge-zone, %d rejected: %s)",
        len(survivors), len(tickers),
        len(edge_zone_tickers),
        len(tickers) - len(survivors),
        ", ".join(f"{r}:{len(ts)}" for r, ts in sorted(rejections.items()))
    )

    # Expose edge-zone set in rejections dict under a meta-key so callers can
    # read it without changing the (survivors, rejections) return signature.
    # Convention: _edge_zone key holds the list (not a rejection).
    if edge_zone_tickers:
        rejections["_edge_zone"] = sorted(edge_zone_tickers)

    # Write audit log
    if reject_log_path:
        _write_reject_log(reject_log_path, rejections, len(tickers), len(survivors))

    return survivors, rejections


def _check_one(ticker: str, *, min_dollar_adv: float = _DEFAULT_MIN_DOLLAR_ADV) -> Tuple[str, Optional[float]]:
    """Return (rejection_reason, dollar_adv).

    rejection_reason is empty string if the ticker passes all gates.
    dollar_adv is the computed 20d average dollar volume (or None when unavailable).
    """
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
            return "price_below_5", None

        # Market cap check
        if market_cap and market_cap < MIN_MARKET_CAP:
            return "market_cap_below_500M", None

        # For detailed checks, use full info
        info = t.info
        if not info:
            return "no_data", None

        # Not a common stock
        qt = info.get("quoteType", "")
        if qt and qt not in ("EQUITY", None):
            return "not_equity", None

        # --- Dollar-ADV gate (R5): 20d average dollar volume ≥ min_dollar_adv ---
        # Compute ADV = avg_volume × price (use regularMarketPrice if available).
        avg_vol = (
            info.get("averageVolume", 0)
            or info.get("averageVolume10days", 0)
            or info.get("avgVolume", 0)
            or 0
        )
        current_price = (
            float(price)
            or float(info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose") or 0)
        )
        dollar_adv: Optional[float] = None
        if avg_vol and current_price:
            dollar_adv = float(avg_vol) * float(current_price)
        elif avg_vol:
            # Price unavailable — fall back to legacy share-count floor
            if avg_vol < MIN_AVG_VOLUME:
                return "low_volume", None
        # If both unavailable, pass through (be lenient — quant screen will reject)

        if dollar_adv is not None and dollar_adv < min_dollar_adv:
            return "low_dollar_adv", dollar_adv

        # EPS check — fragility gate: unprofitable names survive only if they show
        # credible growth-investment characteristics (high growth + high margins).
        eps = info.get("trailingEps") or info.get("epsTrailingTwelveMonths")
        if eps is not None and eps <= 0:
            rev_growth   = info.get("revenueGrowth")      # yfinance fraction, may be None
            gross_margin = info.get("grossMargins")       # yfinance fraction, may be None
            # Only reject if we HAVE the data AND it fails the growth-investment bar.
            if rev_growth is not None and gross_margin is not None:
                if not (rev_growth >= 0.20 and gross_margin >= 0.40):
                    return "unprofitable_no_growth", dollar_adv
            # else: data missing → pass through to the AV quant screen (lenient)

        # Debt check
        de = info.get("debtToEquity", None)
        if de is not None and de > MAX_DEBT_EQUITY:
            return "high_debt_equity", dollar_adv

        return "", dollar_adv  # Passed all checks

    except Exception as e:
        logger.debug("mechanical filter error for %s: %s", ticker, e)
        return "fetch_error", None


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
