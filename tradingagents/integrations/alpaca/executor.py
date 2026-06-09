# tradingagents/integrations/alpaca/executor.py
"""Paper-trade executor: mirrors PM proposals onto the Alpaca PAPER account.

This is the Compounder 2.0 measurement layer (docs/compounder_2_0_vision.md §6).
Every proposal the PM emits is executed for real on Alpaca's paper environment,
so the advisor's track record is verifiable by a third-party system instead of
internal bookkeeping. The real eToro account is untouched — the human still
executes there manually.

Safety invariants:
- PAPER ONLY. The client refuses any key that is not an Alpaca paper key
  (paper key IDs start with "PK"); ``paper=True`` is hard-coded.
- Sizing is scaled from eToro-book dollars to paper-book dollars by
  ``alpaca_equity / etoro_last_total_value`` and capped at
  ``portfolio_advisor_alpaca_max_position_pct`` of paper equity.
- Every public entry point swallows its own errors (alerted via Telegram,
  logged to the ledger) — a broken paper trade must never break a PM cycle
  or the watchdog.

Ledger: one JSONL row per execution attempt at
``~/.tradingagents/portfolio_advisor/alpaca_trades.jsonl``.
Baseline for the scoreboard lives in ``alpaca_state.json`` next to it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from tradingagents.portfolio_advisor import state as pa_state

logger = logging.getLogger(__name__)

_TRIM_DEFAULT_FRACTION = 0.5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_path(cfg: Dict[str, Any]) -> Path:
    return pa_state.advisor_dir(cfg) / "alpaca_trades.jsonl"


def _state_path(cfg: Dict[str, Any]) -> Path:
    return pa_state.advisor_dir(cfg) / "alpaca_state.json"


def enabled(cfg: Dict[str, Any]) -> bool:
    # Hard guard: never trade from a test run, even with keys in the environment.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    if not cfg.get("portfolio_advisor_alpaca_paper", True):
        return False
    return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))


def _client():
    """Paper-only TradingClient. Raises on missing/non-paper keys."""
    from alpaca.trading.client import TradingClient

    key = (os.environ.get("ALPACA_API_KEY") or "").strip()
    sec = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    if not key or not sec:
        raise ValueError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing from environment")
    if not key.startswith("PK"):
        # Live key IDs start with "AK" — refuse outright rather than trust paper=True alone.
        raise ValueError("ALPACA_API_KEY is not a paper key (must start with 'PK'); refusing")
    return TradingClient(key, sec, paper=True)


def _log_row(cfg: Dict[str, Any], row: Dict[str, Any]) -> None:
    try:
        p = _ledger_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now_iso(), **row}) + "\n")
    except Exception:
        logger.exception("alpaca ledger write failed")


def _notify(cfg: Dict[str, Any], subject: str, body: str) -> None:
    try:
        from tradingagents.portfolio_advisor import messaging

        messaging.send_advisor_message(cfg, f"[PAPER] {subject}", body, urgent=False)
    except Exception:
        logger.debug("alpaca notify failed", exc_info=True)


def _scale_factor(cfg: Dict[str, Any], paper_equity: float) -> float:
    """eToro-dollars → paper-dollars. 1.0 when the eToro total is unknown
    (under-sizes on a larger paper book — the conservative direction)."""
    try:
        st = pa_state.load_state(cfg)
        etoro_total = float(st.get("last_total_value") or 0)
        if etoro_total > 0 and paper_equity > 0:
            return paper_equity / etoro_total
    except Exception:
        logger.debug("alpaca scale factor fallback", exc_info=True)
    return 1.0


def _ensure_baseline(cfg: Dict[str, Any], equity: float) -> Dict[str, Any]:
    """Record the scoreboard baseline (start equity + SPY anchor) on first use."""
    p = _state_path(cfg)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    spy = None
    try:
        import yfinance as yf

        spy = float(yf.Ticker("SPY").history(period="5d")["Close"].iloc[-1])
    except Exception:
        logger.debug("SPY baseline fetch failed", exc_info=True)
    base = {"start_iso": _now_iso(), "start_equity": equity, "spy_start_price": spy}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(base, indent=2), encoding="utf-8")
    return base


def execute_proposal(cfg: Dict[str, Any], proposal: Dict[str, Any]) -> Optional[str]:
    """Mirror one PM proposal onto the paper account. Returns a one-line result
    (also Telegram'd and written to the ledger) or None when disabled/skipped.
    Never raises."""
    if not enabled(cfg):
        return None
    tk = (proposal.get("ticker") or "").strip().upper()
    act = (proposal.get("action") or "").strip().lower()
    if not tk or act not in ("buy", "add", "sell", "trim"):
        return None
    try:
        client = _client()
        acct = client.get_account()
        equity = float(acct.equity)
        _ensure_baseline(cfg, equity)
        if act in ("buy", "add"):
            return _paper_buy(cfg, client, equity, tk, proposal)
        return _paper_reduce(cfg, client, tk, act, proposal, equity)
    except Exception as e:
        _log_row(cfg, {"ticker": tk, "action": act, "status": "error", "error": str(e)[:300]})
        _notify(cfg, f"{act.upper()} {tk} failed", f"Paper execution error: {e}")
        return f"paper execution error: {e}"


def _paper_buy(
    cfg: Dict[str, Any],
    client,
    equity: float,
    tk: str,
    proposal: Dict[str, Any],
) -> str:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    usd = float(proposal.get("approx_usd") or 0)
    if usd <= 0:
        px = float(proposal.get("target_price") or 0)
        usd = px * float(proposal.get("shares") or 0)
    if usd <= 0:
        _log_row(cfg, {"ticker": tk, "action": "buy", "status": "skipped", "note": "no size on proposal"})
        return f"skipped {tk}: proposal has no dollar size"

    # A plain "buy" of a name the paper book already holds is the PM restating
    # itself — only an explicit "add" increases an existing position.
    if (proposal.get("action") or "").lower() == "buy":
        try:
            client.get_open_position(tk)
            _log_row(cfg, {"ticker": tk, "action": "buy", "status": "skipped", "note": "already held"})
            return f"skipped {tk}: already held in paper book"
        except Exception:
            pass  # no position — proceed

    scale = _scale_factor(cfg, equity)
    cap = float(cfg.get("portfolio_advisor_alpaca_max_position_pct", 0.10) or 0.10)
    notional = round(min(usd * scale, equity * cap), 2)
    if notional < 1.0:
        _log_row(cfg, {"ticker": tk, "action": "buy", "status": "skipped", "note": f"notional {notional} < $1"})
        return f"skipped {tk}: scaled notional under $1"

    order = client.submit_order(
        MarketOrderRequest(symbol=tk, notional=notional, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
    )
    sleeve = (proposal.get("sleeve") or "").lower() or "?"
    _log_row(
        cfg,
        {
            "ticker": tk,
            "action": proposal.get("action"),
            "status": "submitted",
            "order_id": str(order.id),
            "notional_usd": notional,
            "etoro_usd": usd,
            "scale": round(scale, 4),
            "sleeve": sleeve,
            "proposal_ts": proposal.get("ts"),
        },
    )
    msg = f"BUY {tk} ~${notional:,.0f} ({sleeve}) submitted to Alpaca paper (scaled from ~${usd:,.0f} eToro-size)."
    _notify(cfg, f"BUY {tk}", msg + "\nFills at next market open if currently closed.")
    return msg


def _paper_reduce(
    cfg: Dict[str, Any],
    client,
    tk: str,
    act: str,
    proposal: Dict[str, Any],
    equity: float,
) -> str:
    try:
        pos = client.get_open_position(tk)
    except Exception:
        _log_row(cfg, {"ticker": tk, "action": act, "status": "skipped", "note": "not held in paper book"})
        return f"skipped {tk} {act}: not held in paper book"

    fraction = 1.0
    if act == "trim":
        fraction = _TRIM_DEFAULT_FRACTION
        usd = float(proposal.get("approx_usd") or 0)
        mv = abs(float(pos.market_value or 0))
        if usd > 0 and mv > 0:
            fraction = min(1.0, max(0.05, usd * _scale_factor(cfg, equity) / mv))

    if fraction >= 0.999:
        order = client.close_position(tk)
        note = "closed 100%"
    else:
        from alpaca.trading.requests import ClosePositionRequest

        order = client.close_position(tk, close_options=ClosePositionRequest(percentage=str(round(fraction * 100, 1))))
        note = f"closed {fraction * 100:.0f}%"
    _log_row(
        cfg,
        {
            "ticker": tk,
            "action": act,
            "status": "submitted",
            "order_id": str(getattr(order, "id", "")),
            "fraction": round(fraction, 3),
            "proposal_ts": proposal.get("ts"),
        },
    )
    msg = f"{act.upper()} {tk}: {note} in Alpaca paper book."
    _notify(cfg, f"{act.upper()} {tk}", msg)
    return msg


def close_for_watchdog(cfg: Dict[str, Any], ticker: str, fraction: float, rule: str) -> Optional[str]:
    """Deterministic-rule exit (dd40 → 1.0, pre-earnings trim → 0.5). Never raises."""
    if not enabled(cfg):
        return None
    tk = (ticker or "").strip().upper()
    if not tk:
        return None
    try:
        client = _client()
        try:
            pos = client.get_open_position(tk)
        except Exception:
            _log_row(cfg, {"ticker": tk, "action": "watchdog_exit", "status": "skipped",
                           "note": "not held in paper book", "rule": rule})
            return None
        if fraction >= 0.999:
            client.close_position(tk)
            note = "closed 100%"
        else:
            from alpaca.trading.requests import ClosePositionRequest

            client.close_position(tk, close_options=ClosePositionRequest(percentage=str(round(fraction * 100, 1))))
            note = f"closed {fraction * 100:.0f}%"
        _log_row(cfg, {"ticker": tk, "action": "watchdog_exit", "status": "submitted",
                       "fraction": round(fraction, 3), "rule": rule,
                       "position_mv": float(pos.market_value or 0)})
        msg = f"{tk}: {note} on rule {rule}."
        _notify(cfg, f"watchdog exit {tk}", msg)
        return msg
    except Exception as e:
        _log_row(cfg, {"ticker": tk, "action": "watchdog_exit", "status": "error",
                       "rule": rule, "error": str(e)[:300]})
        return None


def build_scoreboard_block(cfg: Dict[str, Any]) -> str:
    """Compact paper-book-vs-SPY block for the weekly digest and PM prompt.
    Empty string when disabled or unreachable."""
    if not enabled(cfg):
        return ""
    try:
        client = _client()
        acct = client.get_account()
        equity = float(acct.equity)
        base = _ensure_baseline(cfg, equity)
        start_eq = float(base.get("start_equity") or 0)
        lines = ["--- Alpaca paper book (advisor track record) ---"]
        if start_eq > 0:
            ret = (equity / start_eq - 1) * 100
            line = f"Equity ${equity:,.0f} | {ret:+.2f}% since {str(base.get('start_iso',''))[:10]}"
            spy0 = base.get("spy_start_price")
            if spy0:
                try:
                    import yfinance as yf

                    spy_now = float(yf.Ticker("SPY").history(period="5d")["Close"].iloc[-1])
                    spy_ret = (spy_now / float(spy0) - 1) * 100
                    line += f" | SPY {spy_ret:+.2f}% | alpha {ret - spy_ret:+.2f}pts"
                except Exception:
                    pass
            lines.append(line)
        positions = client.get_all_positions()
        if positions:
            for p in positions:
                upl = float(p.unrealized_plpc or 0) * 100
                lines.append(f"  • {p.symbol}: ${abs(float(p.market_value or 0)):,.0f} ({upl:+.1f}%)")
        else:
            lines.append("  (no open paper positions)")
        return "\n".join(lines)
    except Exception:
        logger.debug("alpaca scoreboard unavailable", exc_info=True)
        return ""
