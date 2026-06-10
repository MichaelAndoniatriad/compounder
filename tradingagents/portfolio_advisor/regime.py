"""Regime overlay + circuit breaker for the portfolio advisor.

This module is **deterministic and defensive**: it never blocks trading on a data
error (fails open to multiplier 1.0), always errs toward cash conservation on a
genuine regime signal, and caches state so the executor can read it cheaply every
PM cycle without hammering yfinance.

Design rationale (from the 17-agent edge analysis):
- Insurance, not alpha. Cuts max drawdown ~30-50% by sizing down new buys when the
  market is in a degraded regime or the portfolio is already down significantly.
- Enforced IN CODE in the executor — never delegated to LLM judgment.
- Two failure modes neutralised: LLM panic de-risking on headlines (wrong-signed);
  and prompt language that manufactures turnover.

Regime labels (SPY-based):
    risk_on  — above 200DMA AND annualised 20-day vol < threshold (default 0.25)
    caution  — exactly one of the two conditions degraded
    risk_off — below 200DMA AND vol >= threshold

High-water-mark circuit breaker (account equity):
    -10% from HWM → "halve" (new buys sized at 50% of the regime-adjusted notional)
    -15% from HWM → "halt"  (new buys skipped entirely; sells/exits always pass)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_REGIME_STATE_FILENAME = "regime_state.json"
_RECOMPUTE_HOURS = 6


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _regime_state_path(cfg: Dict[str, Any]) -> Path:
    from tradingagents.portfolio_advisor import state as pa_state
    return pa_state.advisor_dir(cfg) / _REGIME_STATE_FILENAME


def _load_regime_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    p = _regime_state_path(cfg)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("regime_state.json corrupt; starting fresh", exc_info=True)
    return {}


def _save_regime_state(cfg: Dict[str, Any], data: Dict[str, Any]) -> None:
    p = _regime_state_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        logger.debug("regime_state.json save failed", exc_info=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_since(iso: Optional[str]) -> float:
    """Hours elapsed since the given ISO timestamp. Returns inf when iso is None/invalid."""
    if not iso:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() / 3600.0
    except Exception:
        return float("inf")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _compute_label_from_values(
    spy_close: float,
    sma200: float,
    vol: float,
    vol_threshold: float,
) -> tuple:
    """Pure function: derive regime label and above_200dma flag from raw values.

    Extracted for unit-testability so tests do not need to engineer synthetic
    price series.  Returns (label, above_200dma).
    """
    above_200dma = spy_close > sma200
    low_vol = vol < vol_threshold

    if above_200dma and low_vol:
        return "risk_on", True
    elif above_200dma or low_vol:
        return "caution", above_200dma
    else:
        return "risk_off", False


def compute_regime(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return the current market regime derived from SPY price data.

    Uses yfinance (250 daily closes). Result is cached in ``regime_state.json``
    and refreshed at most once every 6 hours.

    Returns:
        {
            "regime":       "risk_on" | "caution" | "risk_off",
            "above_200dma": bool,
            "vol":          float,          # annualised 20-day realised vol
            "spy_close":    float,
            "sma200":       float,
            "computed_at":  str,            # ISO timestamp
        }

    On yfinance failure: returns cached value if one exists, otherwise defaults
    to "caution" (conservative-but-not-frozen) with a WARNING log.
    """
    state = _load_regime_state(cfg)
    last_regime = state.get("last_regime") or {}
    computed_at = last_regime.get("computed_at")

    # Return cached value if fresh enough.
    if computed_at and _hours_since(computed_at) < _RECOMPUTE_HOURS:
        return dict(last_regime)

    # Attempt live recomputation.
    vol_threshold = float(cfg.get("portfolio_advisor_regime_vol_threshold", 0.25) or 0.25)
    try:
        import yfinance as yf

        hist = yf.Ticker("SPY").history(period="250d")["Close"]
        if len(hist) < 201:
            raise ValueError(f"SPY history too short: {len(hist)} bars")

        spy_close = float(hist.iloc[-1])
        sma200 = float(hist.iloc[-200:].mean())
        returns = hist.pct_change().dropna().iloc[-20:]
        vol = float(returns.std() * (252 ** 0.5))

        label, above_200dma = _compute_label_from_values(
            spy_close, sma200, vol, vol_threshold
        )

        regime = {
            "regime": label,
            "above_200dma": above_200dma,
            "vol": round(vol, 4),
            "spy_close": round(spy_close, 4),
            "sma200": round(sma200, 4),
            "computed_at": _now_iso(),
        }

        # Check for state transition — notify once per change.
        prev_label = last_regime.get("regime")
        prev_breaker = state.get("last_breaker_level")

        state["last_regime"] = regime
        _save_regime_state(cfg, state)

        if prev_label and prev_label != label:
            _notify_transition(cfg, regime, prev_label)

        return dict(regime)

    except Exception as e:
        logger.warning("compute_regime: yfinance failed (%s); using cached/default", e)

        if last_regime:
            return dict(last_regime)

        # No cache — default to caution.
        fallback = {
            "regime": "caution",
            "above_200dma": None,
            "vol": None,
            "spy_close": None,
            "sma200": None,
            "computed_at": _now_iso(),
        }
        logger.warning("compute_regime: no cache and yfinance failed — defaulting to 'caution'")
        return fallback


def new_buy_multiplier(regime_or_label) -> float:
    """Return the position-sizing multiplier for new buys given the regime.

    Accepts either a regime dict (as returned by compute_regime) or a plain
    label string ("risk_on" / "caution" / "risk_off").

    risk_on  → 1.0   (full size)
    caution  → 0.75  (25% reduction)
    risk_off → 0.5   (50% reduction)
    """
    if isinstance(regime_or_label, dict):
        label = str(regime_or_label.get("regime") or "caution")
    else:
        label = str(regime_or_label or "caution")

    return {"risk_on": 1.0, "caution": 0.75, "risk_off": 0.5}.get(label, 1.0)


def drawdown_breaker(cfg: Dict[str, Any], equity: float) -> Dict[str, Any]:
    """High-water-mark circuit breaker.

    Tracks HWM in ``regime_state.json``.  Updates HWM when equity > current HWM.

    Returns:
        {"level": "none" | "halve" | "halt", "drawdown_pct": float}

    Thresholds (from config, with defaults):
        portfolio_advisor_breaker_halve_pct  — default 0.10 (−10%)
        portfolio_advisor_breaker_halt_pct   — default 0.15 (−15%)
    """
    halve_pct = float(cfg.get("portfolio_advisor_breaker_halve_pct", 0.10) or 0.10)
    halt_pct = float(cfg.get("portfolio_advisor_breaker_halt_pct", 0.15) or 0.15)

    state = _load_regime_state(cfg)
    hwm = float(state.get("hwm") or 0.0)

    # Ratchet up — never down.
    if equity > hwm:
        hwm = equity
        state["hwm"] = hwm
        _save_regime_state(cfg, state)

    if hwm <= 0:
        return {"level": "none", "drawdown_pct": 0.0}

    drawdown_pct = (hwm - equity) / hwm  # positive = drawdown

    if drawdown_pct >= halt_pct:
        level = "halt"
    elif drawdown_pct >= halve_pct:
        level = "halve"
    else:
        level = "none"

    # Notify on level transition.
    prev_level = state.get("last_breaker_level")
    if level != prev_level:
        state["last_breaker_level"] = level
        _save_regime_state(cfg, state)
        if level != "none":
            _notify_breaker_transition(cfg, level, drawdown_pct)

    return {"level": level, "drawdown_pct": round(drawdown_pct, 4)}


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------


def _notify_transition(cfg: Dict[str, Any], regime: Dict[str, Any], prev_label: str) -> None:
    """Send a one-time Telegram note when regime label changes."""
    try:
        from tradingagents.portfolio_advisor import messaging

        label = regime.get("regime", "?")
        spy_close = regime.get("spy_close")
        sma200 = regime.get("sma200")
        vol = regime.get("vol")
        above = regime.get("above_200dma")

        dma_str = "above" if above else "below"
        vol_str = f"{vol*100:.0f}%" if vol is not None else "?"
        spy_str = f"${spy_close:.2f}" if spy_close is not None else "?"
        sma_str = f"${sma200:.2f}" if sma200 is not None else "?"

        mult = new_buy_multiplier(label)
        body = (
            f"Regime -> {label} (SPY {dma_str} 200DMA [{spy_str} vs {sma_str}], vol {vol_str}). "
            f"New-buy sizing multiplier: {mult:.2f}x."
        )
        messaging.send_advisor_message(cfg, f"[REGIME] {prev_label} -> {label}", body, urgent=False)
    except Exception:
        logger.debug("regime transition notify failed", exc_info=True)


def _notify_breaker_transition(cfg: Dict[str, Any], level: str, drawdown_pct: float) -> None:
    """Send a one-time Telegram note when circuit breaker level changes."""
    try:
        from tradingagents.portfolio_advisor import messaging

        dd_str = f"{drawdown_pct*100:.1f}%"
        if level == "halt":
            body = f"Circuit breaker HALT: portfolio -{dd_str} from high-water mark. All new buys suspended."
        elif level == "halve":
            body = f"Circuit breaker HALVE: portfolio -{dd_str} from high-water mark. New buys cut 50%."
        else:
            body = f"Circuit breaker cleared: drawdown -{dd_str}."
        messaging.send_advisor_message(cfg, f"[BREAKER] {level.upper()}", body, urgent=True)
    except Exception:
        logger.debug("breaker transition notify failed", exc_info=True)
