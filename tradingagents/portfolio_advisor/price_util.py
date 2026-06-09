"""Best effort last price from yfinance (no LLM)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

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


def _rsi_wilder(close: Any, period: int = 14) -> Optional[float]:
    """Wilder's RSI on a pandas close series. None if not enough data."""
    try:
        deltas = close.diff().dropna()
        if len(deltas) < period:
            return None
        gains = deltas.clip(lower=0.0)
        losses = (-deltas).clip(lower=0.0)
        avg_gain = gains.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
        avg_loss = losses.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
        if avg_loss == 0:
            return 100.0
        rs = float(avg_gain) / float(avg_loss)
        return float(100.0 - 100.0 / (1.0 + rs))
    except Exception:
        return None


def dip_signal_yfinance(ticker: str, *, ma_window: int = 50) -> Optional[Dict[str, Optional[float]]]:
    """One-shot technical dip read from a single yfinance history fetch.

    Returns ``{price, ma, below_ma_pct, off_high_pct, rsi}`` or None. ``below_ma_pct``
    is POSITIVE when price sits below the moving average (a dip), negative when above;
    ``off_high_pct`` is the percent below the trailing ~1yr high; ``rsi`` is Wilder(14).
    The classification of these into "buy zone" vs "falling knife" lives in dip_watch.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    try:
        import yfinance as yf

        need = max(int(ma_window), 20)
        hist = yf.Ticker(sym).history(period="1y")
        if hist is None or len(hist.index) < need:
            return None
        close = hist["Close"].dropna()
        if len(close) < need:
            return None
        price = float(close.iloc[-1])
        if price <= 0:
            return None
        ma = float(close.tail(int(ma_window)).mean())
        high = float(close.max())
        return {
            "price": price,
            "ma": ma,
            "below_ma_pct": ((ma - price) / ma * 100.0) if ma > 0 else 0.0,
            "off_high_pct": ((high - price) / high * 100.0) if high > 0 else 0.0,
            "rsi": _rsi_wilder(close, 14),
        }
    except Exception as e:
        logger.debug("yfinance dip signal failed for %s: %s", sym, e)
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
