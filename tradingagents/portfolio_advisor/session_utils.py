"""Shared session-timing utilities for the portfolio advisor scanners.

Provides ``session_elapsed_fraction``: the fraction of the 09:30–16:00 ET
regular trading session that has elapsed at ``now``.

Semantics
---------
* **During the regular session** (09:30–16:00 ET on a trading day):
  fraction = elapsed / 6.5 h, clamped to [0.05, 1.0].

* **Outside the session** (before open, after close, weekend, holiday, or
  clock unavailable):
  fraction = 1.0 — because the daily bar being compared at those times is a
  COMPLETE session (yesterday's bar before the open; today's bar after the
  close).  The 0.05 floor must ONLY ever apply intraday to avoid inflating
  RVOL 20x outside market hours.

Parameters
----------
market_clock_override : dict | None
    Optional Alpaca-style clock dict (keys ``is_open``, ``next_close``).
    When provided it is used directly instead of calling ``market_clock()``.
    Pass ``{"is_open": True, "next_close": <iso>}`` for an in-session test.
    Pass ``{"is_open": True}`` (no next_close) to exercise the ET fallback.
now : datetime | None
    Inject the current UTC datetime for deterministic testing.  Defaults to
    ``datetime.now(timezone.utc)``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_SESSION_HOURS = 6.5           # 09:30–16:00 ET
_SESSION_OPEN_HOUR_ET = 9
_SESSION_OPEN_MIN_ET = 30
_SESSION_CLOSE_HOUR_ET = 16
_SESSION_CLOSE_MIN_ET = 0


def session_elapsed_fraction(
    market_clock_override: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> float:
    """Return the fraction of the regular session elapsed, or 1.0 outside it.

    See module docstring for full semantics.
    """
    now_utc: datetime = now if now is not None else datetime.now(timezone.utc)

    # --- Branch 1: use Alpaca market_clock --------------------------------
    try:
        if market_clock_override is not None:
            clk: Optional[Dict[str, Any]] = market_clock_override
        else:
            from tradingagents.integrations.alpaca.executor import market_clock
            clk = market_clock()

        if clk is not None and clk.get("is_open"):
            next_close_str = clk.get("next_close") or ""
            if next_close_str:
                if next_close_str.endswith("Z"):
                    next_close_str = next_close_str[:-1] + "+00:00"
                next_close = datetime.fromisoformat(next_close_str)
                if next_close.tzinfo is None:
                    next_close = next_close.replace(tzinfo=timezone.utc)
                total_session_s = _SESSION_HOURS * 3600.0
                remaining_s = (next_close - now_utc).total_seconds()
                elapsed_s = total_session_s - remaining_s
                # Negative elapsed_s means we're somehow before the open despite
                # is_open=True (clock edge case).  Return 1.0 (full-day scale).
                if elapsed_s < 0:
                    return 1.0
                return max(0.05, min(1.0, elapsed_s / total_session_s))

            # is_open=True but no next_close key: fall through to ET fallback
        elif clk is not None and not clk.get("is_open"):
            # Market is definitively closed → complete session → 1.0
            return 1.0
    except Exception:
        pass  # fall through to ET fallback

    # --- Branch 2: naive ET-clock fallback --------------------------------
    # Use zoneinfo when available (stdlib 3.9+); otherwise approximate UTC-4.
    try:
        from zoneinfo import ZoneInfo  # type: ignore[import]
        now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et_offset = timedelta(hours=-4)  # EDT approximation
        now_et = now_utc + et_offset

    # Weekend → complete session → 1.0
    if now_et.weekday() > 4:
        return 1.0

    open_today = now_et.replace(
        hour=_SESSION_OPEN_HOUR_ET,
        minute=_SESSION_OPEN_MIN_ET,
        second=0,
        microsecond=0,
    )
    close_today = now_et.replace(
        hour=_SESSION_CLOSE_HOUR_ET,
        minute=_SESSION_CLOSE_MIN_ET,
        second=0,
        microsecond=0,
    )

    if now_et < open_today:
        # Before the open (pre-market / overnight) → comparing against yesterday's
        # complete bar → full-day scale.
        return 1.0

    if now_et >= close_today:
        # After the close → today's bar is complete.
        return 1.0

    # Inside the session window.
    elapsed_s = (now_et - open_today).total_seconds()
    return max(0.05, min(1.0, elapsed_s / (_SESSION_HOURS * 3600.0)))
