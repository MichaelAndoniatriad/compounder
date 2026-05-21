"""Best effort last price from yfinance (no LLM)."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def last_close_yfinance(ticker: str) -> Optional[float]:
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    try:
        import yfinance as yf

        hist = yf.Ticker(sym).history(period="5d")
        if hist is None or len(hist.index) == 0:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug("yfinance price failed for %s: %s", sym, e)
        return None


def next_earnings_date_yfinance(ticker: str) -> Optional[str]:
    """Best-effort next (or most recent upcoming) earnings date as ISO YYYY-MM-DD, else None."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    try:
        import yfinance as yf
        from datetime import date, datetime

        tk = yf.Ticker(sym)
        # Preferred: explicit earnings-dates table (has future rows).
        try:
            df = tk.get_earnings_dates(limit=12)
            if df is not None and len(df.index) > 0:
                today = datetime.now().date()
                future = [d for d in df.index if hasattr(d, "date") and d.date() >= today]
                if future:
                    return min(future).date().isoformat()
        except Exception:
            pass
        # Fallback: calendar dict.
        cal = getattr(tk, "calendar", None)
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, (list, tuple)) and ed:
                ed = ed[0]
            if isinstance(ed, date):
                return ed.isoformat()
            if isinstance(ed, str) and ed.strip():
                return ed.strip()[:10]
        return None
    except Exception as e:
        logger.debug("yfinance earnings date failed for %s: %s", sym, e)
        return None


def weekly_return_pct_yfinance(ticker: str, *, lookback_days: int = 7) -> Optional[float]:
    """Approximate calendar window return using last two closes in range."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    try:
        import yfinance as yf

        hist = yf.Ticker(sym).history(period=f"{int(lookback_days) + 3}d")
        if hist is None or len(hist.index) < 2:
            return None
        first = float(hist["Close"].iloc[0])
        last = float(hist["Close"].iloc[-1])
        if first <= 0:
            return None
        return (last - first) / first * 100.0
    except Exception as e:
        logger.debug("yfinance weekly return failed for %s: %s", sym, e)
        return None
