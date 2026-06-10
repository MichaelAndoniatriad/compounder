# tradingagents/integrations/alpaca/executor.py
"""Paper-trade executor: mirrors PM proposals onto the Alpaca PAPER account.

In autonomous mode (TRADINGAGENTS_ACCOUNT_MODE=alpaca) the PM manages an Alpaca
PAPER account end-to-end. Every proposal the PM emits is executed here, building
a verifiable track record without touching any live account.

Safety invariants:
- PAPER ONLY. The client refuses any key that is not an Alpaca paper key
  (paper key IDs start with "PK"); ``paper=True`` is hard-coded.
- ``PYTEST_CURRENT_TEST`` guard: ``enabled()`` returns False during tests —
  tests must mock the client rather than hitting live APIs.
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


# ---------------------------------------------------------------------------
# R1 — Marketable-limit entry helpers
# ---------------------------------------------------------------------------


def _latest_quote(tk: str) -> Optional[Dict[str, Any]]:
    """Fetch the latest NBBO quote for tk via the Alpaca IEX free feed.

    Returns {"bid": float, "ask": float} or None on ANY failure (missing keys,
    no environment, pytest, network error, zero prices). Callers treat None as
    "quote unavailable" and fall back to market-order behaviour.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest

        key = (os.environ.get("ALPACA_API_KEY") or "").strip()
        sec = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
        if not key or not sec:
            return None
        data_client = StockHistoricalDataClient(key, sec)
        resp = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=tk))
        quote = resp.get(tk) if isinstance(resp, dict) else None
        if quote is None:
            return None
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        return {"bid": bid, "ask": ask}
    except Exception:
        logger.debug("_latest_quote failed for %s", tk, exc_info=True)
        return None

# Track tickers for which a "no plan, defaulting to core floors" warning has
# already been sent this process lifetime — avoids one Telegram per tick.
_warned_no_plan_tickers: set = set()


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
    (under-sizes on a larger paper book — the conservative direction).

    In autonomous mode (account_mode="alpaca") the PM sizes against the Alpaca
    snapshot directly, so proposal dollars ARE paper dollars: no scaling."""
    try:
        from tradingagents.portfolio_advisor.etoro_scan import account_mode

        if account_mode() == "alpaca":
            return 1.0
    except Exception:
        pass
    try:
        st = pa_state.load_state(cfg)
        etoro_total = float(st.get("last_total_value") or 0)
        if etoro_total > 0 and paper_equity > 0:
            return paper_equity / etoro_total
    except Exception:
        logger.debug("alpaca scale factor fallback", exc_info=True)
    return 1.0


def market_clock() -> Optional[Dict[str, Any]]:
    """US market clock from Alpaca — authoritative for holidays, half-days, DST.

    Returns {"is_open": bool, "next_open": str, "next_close": str} or None when
    Alpaca is unreachable / keys absent / under pytest. Callers must treat None
    as "unknown" and fall back to their own heuristic, not as "closed".
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return None
    if not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY")):
        return None
    try:
        c = _client().get_clock()
        return {
            "is_open": bool(c.is_open),
            "next_open": str(c.next_open),
            "next_close": str(c.next_close),
        }
    except Exception:
        logger.debug("market clock unavailable", exc_info=True)
        return None


def _vol_dampener(ticker: str) -> float:
    """60-day realized-vol divisor ∈ [0.5, 1.5]; 1.0 (neutral) on any failure.

    vol 0.30 annualized → 1.0 (neutral); 0.60+ → 1.5 (position halved);
    0.15 → 0.75 (position boosted). Divide a target size by this.
    """
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period="60d")["Close"]
        if len(hist) >= 20:
            returns = hist.pct_change().dropna()
            vol = float(returns.std() * (252 ** 0.5))  # annualized
            return max(0.5, min(1.5, vol / 0.30))
    except Exception:
        pass
    return 1.0


def _confidence_size_multiplier(cfg: Dict[str, Any], confidence: Optional[float], ticker: str) -> float:
    """§5.4 Compounder 2.0: confidence-weighted position sizing.

    Linear scale: conf 0.5 → min_pct of book; conf 0.9 → max_pct of book.
    Vol-dampened: divide by the 60-day realized-vol percentile (0.5–1.5 range)
    so high-vol names get smaller positions at the same confidence.

    Config keys:
        portfolio_advisor_alpaca_conf_min_pct  (default 0.02 = 2%)
        portfolio_advisor_alpaca_conf_max_pct  (default 0.06 = 6%)
    Both are fractions of paper equity. The executor's separate
    portfolio_advisor_alpaca_max_position_pct cap still applies after scaling.
    """
    min_pct = float(cfg.get("portfolio_advisor_alpaca_conf_min_pct", 0.02) or 0.02)
    max_pct = float(cfg.get("portfolio_advisor_alpaca_conf_max_pct", 0.06) or 0.06)

    if confidence is None:
        # No confidence supplied — use mid-range (neutral sizing)
        return (min_pct + max_pct) / 2 / max_pct

    conf = max(0.0, min(1.0, float(confidence)))
    # Linear interpolation between 0.5 and 0.9 confidence
    t = max(0.0, min(1.0, (conf - 0.5) / 0.4))
    target_pct = min_pct + t * (max_pct - min_pct)

    # Vol dampener: shrink for high-vol names
    target_pct = target_pct / _vol_dampener(ticker)

    # Return as a multiplier relative to the max_pct cap
    return min(1.0, target_pct / max_pct)


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


def execute_proposal(cfg: Dict[str, Any], proposal: Dict[str, Any]) -> Dict[str, str]:
    """Mirror one PM proposal onto the paper account.

    Returns a machine-readable result dict::

        {"status": "executed"|"skipped"|"error"|"disabled", "detail": <str>}

    Also Telegram-alerts and writes to the ledger on executed/error. Never raises.
    """
    if not enabled(cfg):
        return {"status": "disabled", "detail": "executor not enabled"}
    tk = (proposal.get("ticker") or "").strip().upper()
    act = (proposal.get("action") or "").strip().lower()
    if not tk or act not in ("buy", "add", "sell", "trim"):
        return {"status": "skipped", "detail": f"invalid ticker or action: {tk!r} / {act!r}"}
    try:
        client = _client()
        acct = client.get_account()
        equity = float(acct.equity)
        _ensure_baseline(cfg, equity)
        if act in ("buy", "add"):
            detail = _paper_buy(cfg, client, equity, tk, proposal)
        else:
            detail = _paper_reduce(cfg, client, tk, act, proposal, equity)
        # _paper_buy/_paper_reduce prefix "skipped" for non-executing outcomes.
        # "deferred" is a distinct outcome: the entry will be retried at open.
        if detail.startswith("skipped"):
            return {"status": "skipped", "detail": detail}
        if detail.startswith("deferred"):
            return {"status": "deferred", "detail": detail}
        return {"status": "executed", "detail": detail}
    except Exception as e:
        _log_row(cfg, {"ticker": tk, "action": act, "status": "error", "error": str(e)[:300]})
        _notify(cfg, f"{act.upper()} {tk} failed", f"Paper execution error: {e}")
        return {"status": "error", "detail": f"paper execution error: {e}"}


def _paper_buy(
    cfg: Dict[str, Any],
    client,
    equity: float,
    tk: str,
    proposal: Dict[str, Any],
) -> str:
    from alpaca.trading.enums import OrderSide, TimeInForce

    usd = float(proposal.get("approx_usd") or 0)
    if usd <= 0:
        px = float(proposal.get("target_price") or 0)
        usd = px * float(proposal.get("shares") or 0)
    if usd <= 0:
        _log_row(cfg, {"ticker": tk, "action": "buy", "status": "skipped", "note": "no size on proposal"})
        return f"skipped {tk}: proposal has no dollar size"

    act = (proposal.get("action") or "").lower()

    # Re-entry cooldown: if this ticker was recently closed (stop-out, sell, watchdog_exit),
    # block a new buy for portfolio_advisor_reentry_cooldown_days (default 5) to prevent
    # the PM from immediately re-entering a position it just stopped out of.
    reentry_cooldown = int(cfg.get("portfolio_advisor_reentry_cooldown_days", 5) or 5)
    reentry_skip = _reentry_cooldown_skip_reason(cfg, tk, reentry_cooldown)
    if reentry_skip:
        _log_row(cfg, {"ticker": tk, "action": act, "status": "skipped", "note": reentry_skip})
        return f"skipped {tk}: {reentry_skip}"

    # A plain "buy" of a name the paper book already holds is the PM restating
    # itself — only an explicit "add" increases an existing position. A queued
    # not-yet-filled buy order counts as held: outside market hours DAY orders
    # sit ACCEPTED until the open, during which the position check sees nothing
    # (2026-06-10: three PM cycles each bought DKNG $2.5k pre-market this way).
    if act == "buy":
        try:
            client.get_open_position(tk)
            _log_row(cfg, {"ticker": tk, "action": "buy", "status": "skipped", "note": "already held"})
            return f"skipped {tk}: already held in paper book"
        except Exception:
            pass  # no position — check in-flight orders next
        if _open_buy_order_exists(client, tk):
            _log_row(cfg, {"ticker": tk, "action": "buy", "status": "skipped", "note": "buy order already queued"})
            return f"skipped {tk}: buy order already queued (unfilled)"

    # Restatement double-buy protection: skip if a same-ticker buy/add was
    # submitted within portfolio_advisor_add_cooldown_days (default 5). Applies
    # to plain "buy" too — the position check alone misses queued orders, and
    # independent PM cycles (ep_scan, batch_complete, morning_checkin) can each
    # restate the same entry within minutes.
    cooldown = int(cfg.get("portfolio_advisor_add_cooldown_days", 5) or 5)
    skip_reason = _cooldown_skip_reason(cfg, tk, cooldown)
    if skip_reason:
        _log_row(cfg, {"ticker": tk, "action": act, "status": "skipped", "note": skip_reason})
        return f"skipped {tk}: {skip_reason}"

    # Determine sleeve early so sizing branch can use it.
    sleeve = (proposal.get("sleeve") or "").lower() or "core"
    conf = proposal.get("confidence")

    scale = _scale_factor(cfg, equity)
    cap = float(cfg.get("portfolio_advisor_alpaca_max_position_pct", 0.10) or 0.10)

    # T2: R-based sizing for catalyst sleeve.
    # notional = equity × risk_pct / hard_stop_pct, so a full stop-out costs
    # exactly risk_pct of the book.  Confidence and high-conviction do NOT
    # affect catalyst size; the HC tier is core-only.
    # The regime multiplier and max-position cap still apply after.
    hc_granted = False
    hc_note = ""
    sizing_method: str

    if sleeve == "catalyst":
        risk_pct = float(cfg.get("portfolio_advisor_catalyst_risk_pct", 0.01) or 0.01)
        hard_stop_pct = float(cfg.get("portfolio_advisor_catalyst_hard_stop_pct", 0.08) or 0.08)
        r_notional = equity * risk_pct / hard_stop_pct if hard_stop_pct > 0 else 0.0
        notional = round(min(r_notional, equity * cap), 2)
        sizing_method = "r_based_1pct"
        conf_mult = 1.0  # not used in catalyst path but kept for ledger
        # HC denied for catalyst (enforce separately from _high_conviction_grant)
        if proposal.get("high_conviction"):
            hc_granted = False
            hc_note = "high-conviction tier is core-sleeve only (catalyst uses R-based sizing)"
    else:
        # §5.4: confidence-weighted sizing for core sleeve — multiplier ∈ [0.2, 1.0]
        sizing_method = "confidence"
        # High-conviction tier: a granted HC buy sizes against the bigger HC cap,
        # still vol-dampened (studied risk: size up on conviction, shrink on vol —
        # never both loosened). Denied requests fall through to normal sizing.
        if proposal.get("high_conviction"):
            hc_granted, hc_note = _high_conviction_grant(cfg, client, tk, conf, sleeve=sleeve)
        if hc_granted:
            hc_cap = float(cfg.get("portfolio_advisor_alpaca_high_conviction_pct", 0.15) or 0.15)
            conf_mult = min(1.0, 1.0 / _vol_dampener(tk))
            notional = round(min(usd * scale, equity * hc_cap * conf_mult), 2)
        else:
            conf_mult = _confidence_size_multiplier(cfg, conf, tk)
            notional = round(min(usd * scale, equity * cap * conf_mult), 2)

    if notional < 1.0:
        _log_row(cfg, {"ticker": tk, "action": "buy", "status": "skipped", "note": f"notional {notional} < $1"})
        return f"skipped {tk}: scaled notional under $1"

    # T1 — Regime overlay + circuit breaker.
    # Applies AFTER normal sizing (including HC path) so we downsize on top of the
    # already-sized notional.  Any failure in this block falls back to multiplier 1.0
    # so a data error never blocks a buy.  Sells/exits are never routed through here.
    _regime_mult = 1.0
    _breaker_level = "none"
    _regime_label = "unknown"
    _breaker_note = ""
    if cfg.get("portfolio_advisor_regime_enabled", True):
        try:
            from tradingagents.portfolio_advisor.regime import (
                compute_regime,
                drawdown_breaker,
                new_buy_multiplier,
            )

            _regime_data = compute_regime(cfg)
            _regime_label = _regime_data.get("regime", "caution")
            _regime_mult = new_buy_multiplier(_regime_label)

            _breaker = drawdown_breaker(cfg, equity)
            _breaker_level = _breaker.get("level", "none")
            _dd_pct = _breaker.get("drawdown_pct", 0.0)

            if _breaker_level == "halt":
                reason = (
                    f"circuit breaker: drawdown halt "
                    f"(−{_dd_pct*100:.1f}% from HWM)"
                )
                _log_row(cfg, {
                    "ticker": tk, "action": act, "status": "skipped",
                    "note": reason, "regime": _regime_label,
                    "breaker_level": _breaker_level, "drawdown_pct": _dd_pct,
                })
                return f"skipped {tk}: {reason}"

            if _breaker_level == "halve":
                _regime_mult *= 0.5
                _breaker_note = f"breaker=halve(-{_dd_pct*100:.1f}%)"

        except Exception:
            logger.warning(
                "_paper_buy: regime overlay failed for %s — proceeding at full size", tk,
                exc_info=True,
            )
            _regime_mult = 1.0
            _breaker_level = "none"

    if _regime_mult != 1.0:
        notional = round(notional * _regime_mult, 2)
        if notional < 1.0:
            _log_row(cfg, {"ticker": tk, "action": act, "status": "skipped",
                           "note": f"regime-scaled notional {notional} < $1",
                           "regime": _regime_label, "breaker_level": _breaker_level})
            return f"skipped {tk}: regime-scaled notional under $1"

    # sleeve was already determined above (for sizing); reuse it here.
    catalyst_date = (proposal.get("catalyst_date") or "").strip() or None

    # R1.3 — No overnight queuing for catalyst entries.
    # If the market is confirmed closed and this is a catalyst buy, defer it.
    # The deferred row (status="deferred_market_closed" in proposed_trades.jsonl)
    # will be retried by execute_deferred_entries() at the next watchdog tick
    # during market hours.  None means "unknown" (no keys, pytest) → allow.
    if sleeve == "catalyst":
        clock = market_clock()
        if clock is not None and not clock.get("is_open"):
            _log_row(cfg, {"ticker": tk, "action": act, "status": "deferred",
                           "note": "market closed — catalyst entry deferred for open-market execution"})
            return f"deferred {tk}: market closed — catalyst entry deferred for open-market execution"

    # R1.1 — Marketable-limit entry: fetch quote, check spread, build limit order.
    # Falls back to market order when quote is unavailable.
    max_spread_bps = int(cfg.get("portfolio_advisor_max_spread_bps", 100) or 100)
    limit_slip_bps = int(cfg.get("portfolio_advisor_limit_slip_bps", 10) or 10)

    quote = _latest_quote(tk)
    use_limit = False
    limit_price: Optional[float] = None
    intended_price: Optional[float] = None

    if quote is not None:
        bid = quote["bid"]
        ask = quote["ask"]
        spread_bps = round((ask - bid) / ask * 10000, 1)
        if spread_bps > max_spread_bps:
            _log_row(cfg, {"ticker": tk, "action": act, "status": "skipped",
                           "note": f"spread {spread_bps:.1f}bps > max {max_spread_bps}bps",
                           "bid": bid, "ask": ask, "spread_bps": spread_bps})
            return f"skipped {tk}: spread {spread_bps:.1f}bps exceeds limit {max_spread_bps}bps"
        # Marketable limit: ask × (1 + slip_bps/10000).
        # Sub-penny rule (SEC Rule 612): stocks ≥ $1 require $0.01-increment prices.
        # Use ceil to keep the buy limit marketable (rounding up keeps us ≥ ask).
        import math
        raw_limit = ask * (1 + limit_slip_bps / 10_000)
        limit_price = math.ceil(raw_limit * 100) / 100  # round UP to nearest cent
        intended_price = limit_price
        if sleeve == "catalyst":
            # Catalyst entries MUST be whole-share simple DAY limit orders.
            # Alpaca rejects bracket/OTO with fractional qty (422 "fractional orders
            # must be simple orders") and the bracket class itself requires take_profit
            # (400 "bracket orders require take_profit.limit_price").  Empirically
            # confirmed against live Alpaca paper API — review finding verified.
            # A standalone GTC stop is submitted after fill confirmation instead.
            qty = int(notional // limit_price)
            if qty < 1:
                _log_row(cfg, {"ticker": tk, "action": act, "status": "skipped",
                               "note": "position too small for whole-share catalyst entry",
                               "notional": notional, "limit_price": limit_price})
                return f"skipped {tk}: position too small for whole-share catalyst entry"
        else:
            # Core buys: 3-decimal fractional ok on simple limit orders
            qty = math.floor(notional / limit_price * 1000) / 1000
        if qty > 0:
            use_limit = True
        # If qty rounds to 0, fall back to market (notional path)

    if use_limit and limit_price is not None:
        from alpaca.trading.requests import LimitOrderRequest

        order = client.submit_order(
            LimitOrderRequest(
                symbol=tk,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
        )
    else:
        # Fallback: market order (notional-based, as before)
        from alpaca.trading.requests import MarketOrderRequest as _MOR

        order = client.submit_order(
            _MOR(symbol=tk, notional=notional, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        )
        intended_price = None  # market order — no specific intended price

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
            "confidence": conf,
            "conf_mult": round(conf_mult, 3),
            "sleeve": sleeve,
            "catalyst_date": catalyst_date,
            "high_conviction": hc_granted,
            "hc_note": hc_note or None,
            "proposal_ts": proposal.get("ts"),
            "intended_price": intended_price,
            "fill_check_pending": True,
            # T2: R-based sizing method label for ledger/audit.
            "sizing_method": sizing_method,
            # T1 — regime overlay fields
            "regime": _regime_label,
            "breaker_level": _breaker_level,
            # Catalyst entries need a standalone GTC stop submitted post-fill.
            # reconcile_fills detects this flag and submits the stop after confirming fill.
            "catalyst_stop_pending": sleeve == "catalyst" and use_limit,
        },
    )

    # R1.4 — Schedule a fill check ~2s post-submit (best-effort; watchdog
    # reconcile_fills back-fills any missed checks on the next tick).
    try:
        import threading

        def _deferred_fill_check():
            import time as _time
            _time.sleep(2)
            try:
                _check_order_fill(cfg, str(order.id), intended_price)
            except Exception:
                pass  # reconcile_fills will catch it next tick

        t = threading.Thread(target=_deferred_fill_check, daemon=True)
        t.start()
    except Exception:
        pass  # non-fatal: reconcile_fills is the backstop

    # Auto-create a PositionPlan so exit rules (trailing stop, time stop,
    # hard stops) actually run for autonomous buys. Wraps in try/except so a
    # plan-creation failure never breaks the buy path.
    try:
        _auto_create_position_plan(cfg, tk, proposal, sleeve, catalyst_date, high_conviction=hc_granted)
    except Exception:
        logger.debug("auto-create position plan failed for %s", tk, exc_info=True)

    conf_note = f", conf {conf:.2f}→{conf_mult:.0%} of cap" if conf is not None else ""
    sleeve_display = sleeve if sleeve != "?" else "?"
    hc_prefix = "HIGH-CONVICTION " if hc_granted else ""
    if abs(scale - 1.0) < 1e-9:
        msg = f"{hc_prefix}BUY {tk} ~${notional:,.0f} ({sleeve_display}) executed on Alpaca paper{conf_note}."
    else:
        msg = f"{hc_prefix}BUY {tk} ~${notional:,.0f} ({sleeve_display}) submitted to Alpaca paper (scaled from ~${usd:,.0f} eToro-size{conf_note})."
    if proposal.get("high_conviction") and not hc_granted:
        msg += f"\nHigh-conviction size-up DENIED: {hc_note}. Sized normally."
    _notify(cfg, f"BUY {tk}", msg + "\nFills at next market open if currently closed.")
    return msg


def _check_order_fill(cfg: Dict[str, Any], order_id: str, intended_price: Optional[float]) -> None:
    """Write a fill_check ledger row for a submitted order. Silent on any failure.

    Called ~2s post-submit from a daemon thread, and by reconcile_fills for any
    orders still flagged fill_check_pending in the ledger. Does NOT raise.
    """
    try:
        c = _client()
        order = c.get_order_by_id(order_id)
        status = str(getattr(order, "status", "")).lower()
        filled_avg = None
        slippage_bps = None
        try:
            raw = getattr(order, "filled_avg_price", None)
            if raw is not None and raw != "":
                filled_avg = float(raw)
        except (TypeError, ValueError):
            pass
        if filled_avg and intended_price and intended_price > 0:
            slippage_bps = round((filled_avg - intended_price) / intended_price * 10_000, 1)
        _log_row(
            cfg,
            {
                "action": "fill_check",
                "order_id": order_id,
                "status": status,
                "filled_avg_price": filled_avg,
                "intended_price": intended_price,
                "slippage_bps": slippage_bps,
            },
        )
    except Exception:
        logger.debug("_check_order_fill failed for order %s", order_id, exc_info=True)


_TERMINAL_FILL_STATUSES = frozenset({"filled", "canceled", "cancelled", "expired", "rejected"})
_PENDING_FILL_STATUSES = frozenset({"new", "accepted", "pending_new", "partially_filled",
                                     "accepted_for_bidding", "stopped", "suspended", "calculated"})


def _pending_order_ids_from_state(cfg: Dict[str, Any]) -> set:
    """Load the set of order_ids still pending reconciliation from alpaca_state.json."""
    try:
        p = _state_path(cfg)
        if not p.is_file():
            return set()
        raw = json.loads(p.read_text(encoding="utf-8"))
        return set(raw.get("pending_order_ids") or [])
    except Exception:
        return set()


def _save_pending_order_ids(cfg: Dict[str, Any], ids: set) -> None:
    """Persist the pending-order-ids set to alpaca_state.json."""
    try:
        p = _state_path(cfg)
        raw: Dict[str, Any] = {}
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        raw["pending_order_ids"] = sorted(ids)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        logger.debug("_save_pending_order_ids failed", exc_info=True)


def _submit_gtc_stop_for_catalyst(
    cfg: Dict[str, Any],
    client,
    tk: str,
    filled_qty: int,
    fill_price: float,
    hard_stop_pct: float,
) -> Optional[str]:
    """Submit a standalone GTC stop-market sell order anchored to actual fill price.

    Returns the stop order id on success, None on any failure.
    The stop price is fill_price × (1 − hard_stop_pct), rounded DOWN to nearest
    cent per SEC Rule 612.
    """
    import math as _math
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        raw_stop = fill_price * (1 - hard_stop_pct)
        stop_px = _math.floor(raw_stop * 100) / 100  # round DOWN for stop (more conservative)
        if stop_px <= 0:
            return None
        order = client.submit_order(
            StopOrderRequest(
                symbol=tk,
                qty=filled_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=stop_px,
            )
        )
        stop_oid = str(order.id)
        _log_row(cfg, {
            "ticker": tk,
            "action": "catalyst_stop_submitted",
            "status": "submitted",
            "order_id": stop_oid,
            "stop_price": stop_px,
            "fill_price": fill_price,
            "qty": filled_qty,
            "hard_stop_pct": hard_stop_pct,
        })
        _notify(cfg, f"GTC stop {tk}", (
            f"GTC stop-market sell {tk} {filled_qty}sh @ ${stop_px:.2f} "
            f"({hard_stop_pct*100:.0f}% below fill ${fill_price:.2f}) submitted."
        ))
        return stop_oid
    except Exception:
        logger.debug("_submit_gtc_stop_for_catalyst failed for %s", tk, exc_info=True)
        return None


def reconcile_fills(cfg: Dict[str, Any]) -> None:
    """Back-fill fill_check rows for submitted buy orders not yet reconciled.

    Robustness design (fixes review findings):
    - Only TERMINAL status rows (filled/canceled/expired/rejected) are added to
      reconciled_ids — partial/new/accepted rows stay pending so the real fill
      price is captured when the order eventually settles.
    - Lookback widened to 7 days to survive weekend gaps.
    - Orders that left the OPEN set but are not in the 7-day closed window get
      one final direct get_order_by_id check, then are marked terminal either
      way (no forever-pending).
    - Pending order_ids are persisted in alpaca_state.json so the full ledger
      does not need to be re-scanned every tick (cheap: maintain a small set).
    - After confirming a catalyst fill, submit a standalone GTC stop-market sell
      anchored to the actual fill price (not the limit price).

    Phantom-state unwind:
    - When a DAY limit order terminates unfilled (canceled/expired/rejected) AND
      the filled qty is 0, write a ledger row action="entry_expired", mark the
      originating proposal row status="cancelled" (note "entry expired unfilled"),
      and delete the auto-created PositionPlan if no Alpaca position exists for
      the ticker.

    Called from enforce_paper_exits top. Silent on any failure — never raises.
    """
    if not enabled(cfg):
        return
    try:
        from datetime import timedelta
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        p = _ledger_path(cfg)
        if not p.is_file():
            return

        # --- Pass 1: scan ledger to build pending / reconciled maps ---
        # pending_rows: order_id → full ledger row (for ticker, sleeve, proposal_ts, etc.)
        # reconciled_ids: order_ids that already have a TERMINAL fill_check row
        pending_rows: Dict[str, Dict[str, Any]] = {}
        reconciled_ids: set = set()

        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if row.get("action") == "fill_check":
                # Only TERMINAL fill_check rows count as reconciled.
                # Rows with non-terminal status (new/accepted/partially_filled) were
                # written by the 2s deferred check too early — they do NOT close the loop.
                fc_status = str(row.get("status") or "").lower()
                if fc_status in _TERMINAL_FILL_STATUSES:
                    reconciled_ids.add(str(row.get("order_id") or ""))
            elif row.get("fill_check_pending") and row.get("order_id"):
                oid = str(row["order_id"])
                if oid not in pending_rows:
                    pending_rows[oid] = row

        unreconciled = {oid: row for oid, row in pending_rows.items() if oid not in reconciled_ids}
        if not unreconciled:
            # Persist empty pending set to clear stale state
            _save_pending_order_ids(cfg, set())
            return

        # --- Pass 2: fetch last-7d closed orders ---
        c = _client()
        after_dt = datetime.now(timezone.utc) - timedelta(days=7)
        try:
            closed_orders = c.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=after_dt, limit=500)
            ) or []
        except Exception:
            logger.debug("reconcile_fills: get_orders failed", exc_info=True)
            closed_orders = []

        filled_map: Dict[str, Any] = {str(getattr(o, "id", "")): o for o in closed_orders
                                       if str(getattr(o, "id", ""))}

        hard_stop_pct = float(cfg.get("portfolio_advisor_catalyst_hard_stop_pct", 0.08) or 0.08)
        still_pending: set = set()

        for oid, ledger_row in unreconciled.items():
            order_obj = filled_map.get(oid)

            # Orders not in the 7d closed window: do one direct check then finalize.
            if order_obj is None:
                try:
                    order_obj = c.get_order_by_id(oid)
                except Exception:
                    # API error — keep pending and try next tick
                    still_pending.add(oid)
                    continue

            status = str(getattr(order_obj, "status", "")).lower()

            # Non-terminal: stay pending
            if status not in _TERMINAL_FILL_STATUSES:
                still_pending.add(oid)
                continue

            filled_avg = None
            filled_qty_raw = None
            try:
                raw = getattr(order_obj, "filled_avg_price", None)
                if raw is not None and raw != "":
                    filled_avg = float(raw)
            except (TypeError, ValueError):
                pass
            try:
                qr = getattr(order_obj, "filled_qty", None)
                if qr is not None and qr != "":
                    filled_qty_raw = float(qr)
            except (TypeError, ValueError):
                pass

            intended = None
            try:
                v = ledger_row.get("intended_price")
                if v is not None:
                    intended = float(v)
            except (TypeError, ValueError):
                pass

            slippage_bps = None
            if filled_avg and intended and intended > 0:
                slippage_bps = round((filled_avg - intended) / intended * 10_000, 1)

            _log_row(
                cfg,
                {
                    "action": "fill_check",
                    "order_id": oid,
                    "status": status,
                    "filled_avg_price": filled_avg,
                    "filled_qty": filled_qty_raw,
                    "intended_price": intended,
                    "slippage_bps": slippage_bps,
                    "source": "reconcile_fills",
                },
            )

            tk = str(ledger_row.get("ticker") or "").upper()
            sleeve = str(ledger_row.get("sleeve") or "").lower()
            proposal_ts = ledger_row.get("proposal_ts")

            # --- Post-fill GTC stop for catalyst entries ---
            if (
                status == "filled"
                and sleeve == "catalyst"
                and bool(ledger_row.get("catalyst_stop_pending"))
                and filled_avg
                and filled_avg > 0
                and filled_qty_raw
                and filled_qty_raw >= 1
            ):
                filled_qty_int = int(filled_qty_raw)
                stop_oid = _submit_gtc_stop_for_catalyst(
                    cfg, c, tk, filled_qty_int, filled_avg, hard_stop_pct
                )
                if stop_oid and tk:
                    # Record stop_order_id on the PositionPlan
                    try:
                        from tradingagents.portfolio_advisor.position_plans import (
                            load_position_plans, save_position_plans,
                        )
                        plans = load_position_plans(cfg)
                        plan = plans.get(tk)
                        if plan is not None:
                            plan.stop_order_id = stop_oid
                            plan.last_updated = _now_iso()
                            plans[tk] = plan
                            save_position_plans(cfg, plans)
                    except Exception:
                        logger.debug("reconcile_fills: could not save stop_order_id for %s", tk, exc_info=True)

            # --- Phantom-state unwind: entry expired unfilled ---
            unfilled = (
                status in ("canceled", "cancelled", "expired", "rejected")
                and (filled_qty_raw is None or filled_qty_raw == 0)
                and tk
            )
            if unfilled:
                # Write an entry_expired ledger row
                _log_row(cfg, {
                    "action": "entry_expired",
                    "ticker": tk,
                    "order_id": oid,
                    "status": status,
                    "sleeve": sleeve,
                    "proposal_ts": proposal_ts,
                    "note": "entry expired unfilled — unwinding phantom state",
                })
                _notify(cfg, f"Entry expired {tk}", (
                    f"[PAPER] {tk} DAY limit order {oid} expired/cancelled unfilled. "
                    "Phantom plan + proposal cancelled."
                ))

                # Cancel the originating proposal row
                if proposal_ts:
                    try:
                        from tradingagents.portfolio_advisor import proposals as _prop
                        rows = _prop.load_all(cfg)
                        now_iso = _now_iso()
                        for r in rows:
                            if r.get("ts") == proposal_ts and (r.get("ticker") or "").upper() == tk:
                                r["status"] = "cancelled"
                                r["status_set_at"] = now_iso
                                r["status_note"] = "entry expired unfilled"
                                break
                        _prop.save_all(cfg, rows)
                    except Exception:
                        logger.debug("reconcile_fills: proposal unwind failed for %s", tk, exc_info=True)

                # Delete the PositionPlan if no Alpaca position exists
                try:
                    c.get_open_position(tk)
                    # Position exists — don't delete the plan
                except Exception:
                    # No position — safe to delete the phantom plan
                    try:
                        from tradingagents.portfolio_advisor.position_plans import remove_position_plan
                        remove_position_plan(cfg, tk)
                        logger.info("reconcile_fills: removed phantom plan for %s (entry expired unfilled)", tk)
                    except Exception:
                        logger.debug("reconcile_fills: remove_position_plan failed for %s", tk, exc_info=True)

        _save_pending_order_ids(cfg, still_pending)
    except Exception:
        logger.debug("reconcile_fills failed", exc_info=True)


def _high_conviction_grant(
    cfg: Dict[str, Any], client, tk: str, confidence: Optional[float],
    sleeve: str = "core",
) -> tuple:
    """Decide whether a high-conviction size-up is allowed. Returns (granted, note).

    Guards (all must pass):
      1. Tier enabled in config.
      2. Sleeve must be 'core' — the HC tier is core-sleeve only.  Catalyst
         sleeve uses R-based sizing; the HC cap makes no sense with a hard stop.
      3. Stated confidence >= high_conviction_min_confidence (default 0.85).
      4. A concurrent HC slot is free: at most high_conviction_max_positions
         (default 2) open positions may carry the high_conviction plan flag.

    A denial NEVER blocks the trade — the buy proceeds at normal sizing and the
    denial reason is surfaced so the PM learns why. Cash only; this tier raises
    the per-position cap, it does not borrow.
    """
    # T2: HC tier is core-sleeve only. Catalyst positions size by R-math.
    if sleeve == "catalyst":
        return False, "high-conviction tier is core-sleeve only (catalyst uses R-based sizing)"
    if not bool(cfg.get("portfolio_advisor_alpaca_high_conviction_enabled", True)):
        return False, "high-conviction tier disabled in config"
    min_conf = float(cfg.get("portfolio_advisor_alpaca_high_conviction_min_confidence", 0.85) or 0.85)
    if confidence is None or float(confidence) < min_conf:
        stated = "unstated" if confidence is None else f"{float(confidence):.2f}"
        return False, f"confidence {stated} below high-conviction floor {min_conf:.2f}"
    max_hc = int(cfg.get("portfolio_advisor_alpaca_high_conviction_max_positions", 2) or 2)
    try:
        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        plans = load_position_plans(cfg)
        open_syms = {str(p.symbol).upper() for p in (client.get_all_positions() or [])}
        hc_open = sorted(
            t for t, pl in plans.items()
            if getattr(pl, "high_conviction", False) and t in open_syms and t != tk
        )
        if len(hc_open) >= max_hc:
            return False, f"high-conviction slots full ({', '.join(hc_open)}); max {max_hc} concurrent"
    except Exception:
        # Slot accounting failed — deny the size-up (the conservative direction),
        # but the buy itself still executes at normal size.
        logger.debug("high-conviction slot check failed for %s", tk, exc_info=True)
        return False, "slot check failed; sized normally"
    return True, f"high-conviction granted (confidence {float(confidence):.2f} >= floor {min_conf:.2f})"


def _cancel_open_orders_for_symbol(client, tk: str) -> int:
    """Cancel all open orders for a symbol before closing the position.

    Alpaca rejects ``close_position`` with 403 'insufficient qty available'
    when shares are reserved by an open child/sibling sell order (e.g. a
    GTC stop placed after fill confirmation).  Call this before every
    close_position call to avoid the deadlock.

    Returns the count of orders cancelled (0 on any failure, always silent).
    """
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[tk], limit=50)
        ) or []
        cancelled = 0
        for o in orders:
            oid = str(getattr(o, "id", "") or "")
            if not oid:
                continue
            try:
                client.cancel_order_by_id(oid)
                cancelled += 1
            except Exception:
                logger.debug("cancel_open_orders_for_symbol: cancel %s failed for %s", oid, tk, exc_info=True)
        return cancelled
    except Exception:
        logger.debug("cancel_open_orders_for_symbol failed for %s", tk, exc_info=True)
        return 0


def _open_buy_order_exists(client, tk: str) -> bool:
    """True if an unfilled buy order for tk is queued on the paper account."""
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[tk], limit=10)
        )
        return any(str(getattr(o, "side", "")).lower().endswith("buy") for o in orders or [])
    except Exception:
        logger.debug("open-order check failed for %s", tk, exc_info=True)
        return False  # fail open: the ledger cooldown still guards restatements


def _cooldown_skip_reason(cfg: Dict[str, Any], tk: str, cooldown_days: int) -> str:
    """Return a non-empty skip reason if a same-ticker buy/add was executed
    within cooldown_days in the alpaca ledger; empty string otherwise."""
    from datetime import timedelta

    p = _ledger_path(cfg)
    if not p.is_file():
        return ""
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if (
            row.get("status") == "submitted"
            and str(row.get("ticker") or "").upper() == tk
            and str(row.get("action") or "").lower() in ("buy", "add")
        ):
            try:
                ts = datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
                if ts >= cutoff:
                    return f"cooldown: buy/add within last {cooldown_days}d"
            except (TypeError, ValueError):
                pass
    return ""


def _reentry_cooldown_skip_reason(cfg: Dict[str, Any], tk: str, cooldown_days: int) -> str:
    """Return a non-empty skip reason if the same ticker was closed (watchdog_exit,
    sell, or trim submitted) within cooldown_days in the alpaca ledger.

    This is the buy-side re-entry guard: after a stop-out or manual exit the ticker
    should not be re-bought for cooldown_days to prevent wash-loop churn.
    Returns empty string when no recent close event is found.
    """
    from datetime import timedelta

    p = _ledger_path(cfg)
    if not p.is_file():
        return ""
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    _CLOSE_ACTIONS = ("sell", "trim", "watchdog_exit")
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if (
            row.get("status") == "submitted"
            and str(row.get("ticker") or "").upper() == tk
            and str(row.get("action") or "").lower() in _CLOSE_ACTIONS
        ):
            try:
                ts = datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
                days_ago = int((datetime.now(timezone.utc) - ts).days)
                if ts >= cutoff:
                    return f"re-entry cooldown: stopped out {days_ago}d ago"
            except (TypeError, ValueError):
                pass
    return ""


def _auto_create_position_plan(
    cfg: Dict[str, Any],
    tk: str,
    proposal: Dict[str, Any],
    sleeve: str,
    catalyst_date: Optional[str],
    high_conviction: bool = False,
) -> None:
    """Create or upsert a PositionPlan for an autonomous buy.

    Entry price: proposal target_price if > 0, else last close from yfinance.
    Existing plans are NOT overwritten — an existing plan means the user or a
    prior autonomous buy already set one up. high_conviction marks the plan as
    occupying an HC slot (counted by _high_conviction_grant while open).
    """
    from tradingagents.portfolio_advisor.position_plans import (
        PositionPlan,
        load_position_plans,
        upsert_position_plan,
    )

    existing = load_position_plans(cfg)
    if tk in existing:
        # Plan already exists — do not clobber it. Only update catalyst_date if
        # the existing plan has none and the new proposal supplies one, and
        # latch the HC flag if this buy was granted the high-conviction tier.
        plan = existing[tk]
        dirty = False
        if catalyst_date and not plan.catalyst_date:
            plan.catalyst_date = catalyst_date
            dirty = True
        if high_conviction and not plan.high_conviction:
            plan.high_conviction = True
            dirty = True
        if dirty:
            upsert_position_plan(cfg, plan)
        return

    entry_price = float(proposal.get("target_price") or 0)
    if entry_price <= 0:
        try:
            import yfinance as yf

            hist = yf.Ticker(tk).history(period="5d")["Close"]
            if len(hist) >= 1:
                entry_price = float(hist.iloc[-1])
        except Exception:
            logger.debug("yfinance price fetch failed for %s in auto-create plan", tk)

    if entry_price <= 0:
        logger.debug("auto-create plan: no entry price for %s — skipping", tk)
        return

    proposal_source = str(proposal.get("source") or "").strip()
    # PEAD plans need a longer time-stop window (drift takes time).
    # Use cfg portfolio_advisor_pead_hold_days (default 20) as the override.
    _tso: Optional[int] = None
    if proposal_source == "pead_scanner":
        try:
            _tso = int(cfg.get("portfolio_advisor_pead_hold_days", 20) or 20)
        except (TypeError, ValueError):
            _tso = 20

    note = f"auto-created on autonomous buy {_now_iso()}"
    plan = PositionPlan(
        ticker=tk,
        entry_price=entry_price,
        strategy=sleeve if sleeve in ("core", "catalyst") else "core",
        entry_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        catalyst_date=catalyst_date or "",
        notes=note,
        high_conviction=bool(high_conviction),
        time_stop_days_override=_tso,
        source=proposal_source,
    )
    upsert_position_plan(cfg, plan)


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
    mv = abs(float(pos.market_value or 0))
    if act == "trim":
        fraction = _TRIM_DEFAULT_FRACTION
        usd = float(proposal.get("approx_usd") or 0)
        if usd > 0 and mv > 0:
            fraction = min(1.0, max(0.05, usd * _scale_factor(cfg, equity) / mv))
    elif act == "sell":
        # A sell with an explicit approx_usd strictly less than 95% of the
        # current market value is a fractional close, not a full liquidation.
        usd = float(proposal.get("approx_usd") or 0)
        if usd > 0 and mv > 0 and usd < 0.95 * mv:
            fraction = min(1.0, max(0.05, usd * _scale_factor(cfg, equity) / mv))

    # Cancel any open orders (e.g. GTC stop) before closing the position.
    # Alpaca rejects close_position with 403 when shares are reserved by an
    # open order — cancel first to avoid the conflict.
    _cancel_open_orders_for_symbol(client, tk)
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
        # Cancel any open orders (e.g. GTC stop) before closing the position.
        # Alpaca rejects close_position with 403 when shares are reserved by an
        # open order — cancel first to avoid the conflict.
        _cancel_open_orders_for_symbol(client, tk)
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


def _latest_buys_from_ledger(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """ticker → most recent submitted buy/add row from alpaca_trades.jsonl."""
    out: Dict[str, Dict[str, Any]] = {}
    p = _ledger_path(cfg)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if row.get("status") == "submitted" and str(row.get("action") or "").lower() in ("buy", "add"):
            tk = str(row.get("ticker") or "").upper()
            if tk:
                out[tk] = row
    return out


def _sync_plan_entry_to_fill(
    plan,
    pos,
    now_iso: str,
) -> bool:
    """Sync a position plan's entry_price to the Alpaca actual fill if they diverge.

    Idempotent: only updates once (checks for 'entry synced to fill' in plan.notes).
    Returns True if the plan was modified (caller must persist it).

    The plan's entry_price may differ from the Alpaca avg_entry_price when the PM
    proposed at a target price but the order filled at a gapped open. The divergence
    causes phantom TRIGGERED stops in the PM prompt (plan shows stop at wrong level).
    Fix: update plan.entry_price to the actual fill once, append an audit note.
    """
    # Idempotency guard: only sync once.
    if "entry synced to fill" in (plan.notes or ""):
        return False

    fill_px: Optional[float] = None
    try:
        raw = getattr(pos, "avg_entry_price", None)
        # Only accept numeric types or string representations; reject MagicMock etc.
        if isinstance(raw, (int, float, str)) and raw not in ("", None):
            fill_px = float(raw) or None
    except (TypeError, ValueError):
        pass
    if fill_px is None or fill_px <= 0:
        return False

    entry = plan.entry_price
    if entry <= 0:
        return False

    divergence = abs(fill_px - entry) / entry
    if divergence <= 0.005:
        return False

    # Divergence exceeds 0.5% — update to fill price.
    old_entry = entry
    plan.entry_price = round(fill_px, 6)
    note = f"entry synced to fill {fill_px:.4f} on {now_iso[:10]} (was {old_entry:.4f}, diverged {divergence*100:.2f}%)"
    plan.notes = ((plan.notes or "").rstrip() + "\n" + note).strip()
    plan.last_updated = now_iso
    return True


def execute_deferred_entries(cfg: Dict[str, Any]) -> int:
    """Re-attempt catalyst entries that were deferred because the market was closed.

    Scans proposed_trades.jsonl for rows with status=="deferred_market_closed"
    that are younger than 18h and re-runs execute_proposal on each.  All entry
    guards (cooldown, open orders, spread, market clock) re-check live — if the
    market is still closed the entry will be deferred again.

    Rows older than 18h are expired (marked cancelled with note "deferred entry
    expired") — too stale to enter.

    Called from enforce_paper_exits top (which runs every watchdog tick during
    market hours) so no additional scheduling is needed.

    Never raises. Returns number of entries re-attempted (not necessarily filled).
    """
    if not enabled(cfg):
        return 0
    # Only run when market is open; if clock is unknown (None) proceed anyway.
    clock = market_clock()
    if clock is not None and not clock.get("is_open"):
        return 0
    try:
        from datetime import timedelta
        from tradingagents.portfolio_advisor import proposals as _prop

        rows = _prop.load_all(cfg)
        now = datetime.now(timezone.utc)
        cutoff_18h = now - timedelta(hours=18)
        attempted = 0
        dirty = False

        for r in rows:
            if r.get("status") != "deferred_market_closed":
                continue
            ts_str = str(r.get("status_set_at") or r.get("ts") or "")
            try:
                deferred_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                deferred_at = now  # unknown age — treat as fresh

            tk = (r.get("ticker") or "").upper()

            if deferred_at < cutoff_18h:
                # Entry too stale — expire it
                r["status"] = "cancelled"
                r["status_set_at"] = now.isoformat()
                r["status_note"] = "deferred entry expired"
                _log_row(cfg, {
                    "action": "entry_expired",
                    "ticker": tk,
                    "note": "deferred catalyst entry expired (>18h)",
                })
                dirty = True
                logger.info("execute_deferred_entries: expired deferred entry for %s", tk)
                continue

            # Re-run execute_proposal with the original proposal payload
            proposal = dict(r)
            result = execute_proposal(cfg, proposal)
            ex_status = result.get("status", "error") if isinstance(result, dict) else "error"
            ex_detail = result.get("detail", "") if isinstance(result, dict) else ""

            now_iso = now.isoformat()
            if ex_status == "executed":
                r["status"] = "executed"
                r["status_set_at"] = now_iso
                r["status_note"] = f"deferred entry executed: {ex_detail}"[:300]
            elif ex_status == "deferred":
                # Still closed — keep deferred, update status_set_at so 18h clock refreshes
                r["status_set_at"] = now_iso
            elif ex_status in ("skipped", "error"):
                r["status"] = "cancelled"
                r["status_set_at"] = now_iso
                r["status_note"] = f"deferred entry {ex_status}: {ex_detail}"[:300]
            # disabled → leave as deferred
            dirty = True
            attempted += 1

        if dirty:
            _prop.save_all(cfg, rows)
        return attempted
    except Exception:
        logger.debug("execute_deferred_entries failed", exc_info=True)
        return 0


def execute_unvetoed_candidates(cfg: Dict[str, Any]) -> int:
    """Execute scanner-sourced catalyst proposals whose PM veto window has expired.

    Scanner-sourced catalyst proposals (source in {"ep_scanner", "pead_scanner"})
    are filed with a ``pm_veto_window_until`` timestamp.  They sit as
    status="proposed" during the veto window so the PM can call
    ``veto_candidate(ticker, reason)`` to block execution.  This function:

    1. Finds all proposed rows with a ``pm_veto_window_until`` in the past.
    2. Re-checks all existing guards (cooldown, spread, market clock).
    3. Executes them via execute_proposal on the Alpaca paper book.
    4. Marks the row executed/cancelled in the proposals ledger.

    Called from enforce_paper_exits top (alongside execute_deferred_entries)
    so no additional scheduling is needed.

    Never raises. Returns number of proposals attempted.
    """
    if not enabled(cfg):
        return 0
    # Only run when market is open; if clock is unknown (None) proceed anyway.
    clock = market_clock()
    if clock is not None and not clock.get("is_open"):
        return 0
    try:
        from tradingagents.portfolio_advisor import proposals as _prop

        rows = _prop.load_all(cfg)
        now = datetime.now(timezone.utc)
        attempted = 0
        dirty = False

        for r in rows:
            if r.get("status") != "proposed":
                continue
            veto_until_str = r.get("pm_veto_window_until")
            if not veto_until_str:
                continue  # not a scanner-sourced veto-gated proposal
            try:
                veto_until = datetime.fromisoformat(
                    str(veto_until_str).replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                continue  # malformed — skip

            if now < veto_until:
                continue  # still within veto window — do not execute yet

            tk = (r.get("ticker") or "").upper()
            # Re-run execute_proposal with the original proposal payload.
            # All guards (cooldown, spread, market clock, etc.) re-check live.
            proposal = dict(r)
            result = execute_proposal(cfg, proposal)
            ex_status = result.get("status", "error") if isinstance(result, dict) else "error"
            ex_detail = result.get("detail", "") if isinstance(result, dict) else ""

            now_iso = now.isoformat()
            if ex_status == "executed":
                r["status"] = "executed"
                r["status_set_at"] = now_iso
                r["status_note"] = f"veto window expired, executed: {ex_detail}"[:300]
            elif ex_status == "deferred":
                r["status"] = "deferred_market_closed"
                r["status_set_at"] = now_iso
                r["status_note"] = ex_detail[:300]
            elif ex_status in ("skipped", "error"):
                r["status"] = "cancelled"
                r["status_set_at"] = now_iso
                r["status_note"] = f"veto window expired, {ex_status}: {ex_detail}"[:300]
            # disabled → leave as proposed
            dirty = True
            attempted += 1
            logger.info(
                "execute_unvetoed_candidates: %s → %s (%s)",
                tk, r["status"], ex_detail[:80],
            )

        if dirty:
            _prop.save_all(cfg, rows)
        return attempted
    except Exception:
        logger.debug("execute_unvetoed_candidates failed", exc_info=True)
        return 0


def enforce_paper_exits(cfg: Dict[str, Any]) -> int:
    """Deterministic exits for paper positions every watchdog tick.

    At the top of each tick, auto-created plans whose entry_price diverges from
    the Alpaca avg_entry_price by >0.5% are synced once to the actual fill price
    (see _sync_plan_entry_to_fill). This prevents phantom TRIGGERED stops when
    the PM proposed at target_price but the fill gapped.

    Sleeve rules applied each tick:
      catalyst: close at ≤ -8% unrealized (hard stop); trailing stop once peak
                >= entry*(1+trail_arm_pct) (default +5%) and price falls
                trail_dist_pct (default 8%) from peak; time stop 3d after
                catalyst_date when the expected move did not happen; max-hold
                fallback when no catalyst_date is set.
      core:     close at ≤ -40% unrealized (hard stop); close 50% once when
                price >= entry*2.0 (trim-half rule), keyed by plan
                double_trim_done_at so it only fires once.

    Sleeve resolution order (plan-first):
      1. position_plans.json (set at buy time by _auto_create_position_plan)
      2. alpaca_trades.jsonl ledger buy row sleeve field
      3. If NEITHER source knows the position → one-time Telegram warning and
         default to core floors.

    Price for trailing-stop peak ratcheting comes from the Alpaca position
    (market_value / qty) — no yfinance call per tick.

    Idempotent; never raises. Returns number of close actions taken.
    """
    if not enabled(cfg):
        return 0
    # R1.4 — Back-fill any submitted orders not yet reconciled.
    reconcile_fills(cfg)
    # Re-attempt any catalyst entries that were deferred at market-close time.
    execute_deferred_entries(cfg)
    # T2 — Execute scanner-sourced catalyst proposals whose PM veto window expired.
    execute_unvetoed_candidates(cfg)
    try:
        client = _client()
        positions = client.get_all_positions()
        if not positions:
            return 0
        buys = _latest_buys_from_ledger(cfg)
        try:
            from tradingagents.portfolio_advisor.position_plans import (
                load_position_plans,
                save_position_plans,
                catalyst_rules_from_cfg,
                eval_catalyst_exit,
                update_catalyst_peak,
            )
            plans = load_position_plans(cfg)
        except Exception:
            plans = {}
        catalyst_rules = None
        try:
            catalyst_rules = catalyst_rules_from_cfg(cfg)
        except Exception:
            pass
        max_hold = int(cfg.get("portfolio_advisor_catalyst_max_hold_days", 30) or 30)
        now = datetime.now(timezone.utc)
        closed = 0
        plans_dirty = False  # track whether we need to save plans

        for pos in positions:
            tk = str(pos.symbol).upper()
            try:
                plpc = float(pos.unrealized_plpc or 0)
            except (TypeError, ValueError):
                continue

            # Derive current price from Alpaca position — no yfinance needed per tick.
            current_price: Optional[float] = None
            try:
                mv = float(pos.market_value or 0)
                qty = float(pos.qty or 0)
                if qty != 0:
                    current_price = abs(mv) / abs(qty)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            # Fallback: check for current_price attribute directly
            if current_price is None:
                try:
                    current_price = float(pos.current_price or 0) or None
                except (TypeError, ValueError, AttributeError):
                    pass

            # Plan-first sleeve resolution.
            plan = plans.get(tk)
            buy = buys.get(tk, {})

            # Fix 2: Sync auto-created plan entry_price to actual Alpaca fill once.
            # A gapped open can diverge plan.entry_price from avg_entry_price by >0.5%,
            # causing phantom TRIGGERED stops in the PM prompt. Sync is idempotent.
            if plan is not None:
                try:
                    now_iso = now.isoformat()
                    if _sync_plan_entry_to_fill(plan, pos, now_iso):
                        plans[tk] = plan
                        plans_dirty = True
                        logger.debug("enforce_paper_exits: synced entry_price for %s to fill", tk)
                except Exception:
                    logger.debug("enforce_paper_exits: fill sync failed for %s", tk, exc_info=True)

            if plan is not None:
                # Trust the plan — it was set at buy time and captures the intent.
                sleeve = plan.strategy
                catalyst_date = plan.catalyst_date or ""
            elif buy:
                sleeve = str(buy.get("sleeve") or "core").lower()
                catalyst_date = str(buy.get("catalyst_date") or "").strip()
            else:
                # Neither source knows this position — warn once and default safe.
                if tk not in _warned_no_plan_tickers:
                    _warned_no_plan_tickers.add(tk)
                    _notify(
                        cfg,
                        f"Unknown sleeve for {tk}",
                        f"[PAPER] {tk} is open in the paper book but has no position plan "
                        f"and no ledger buy row. Defaulting to core hard-stop floors. "
                        f"Set a plan via: advisor portfolio position-plan set {tk} --entry PRICE",
                    )
                    logger.warning(
                        "enforce_paper_exits: %s has no plan or ledger row — defaulting to core floors", tk
                    )
                sleeve = "core"
                catalyst_date = ""

            rule: Optional[str] = None
            trim_fraction = 1.0  # default: full close

            if sleeve == "catalyst":
                hard_stop_pct = float(cfg.get("portfolio_advisor_catalyst_hard_stop_pct", 0.08) or 0.08)
                if plpc <= -hard_stop_pct:
                    rule = f"paper_catalyst_hard_stop_{-hard_stop_pct*100:.0f}pct"
                else:
                    # Pre-binary-event flat (Option B): go flat N days before the catalyst event.
                    # Catalyst trades capture the pre-event run-up; going flat before the print
                    # avoids the binary outcome (FDA approval/rejection, earnings surprise, M&A vote).
                    # One-shot per plan: pre_event_flat_done_at consumption flag prevents log spam.
                    if catalyst_date and plan is not None and not plan.pre_event_flat_done_at:
                        pre_event_days = int(
                            cfg.get("portfolio_advisor_catalyst_pre_event_days_before", 1) or 1
                        )
                        try:
                            cat_dt = datetime.fromisoformat(catalyst_date[:10]).replace(tzinfo=timezone.utc)
                            days_until = (cat_dt - now).days
                            # Fire within the window: 1 day before through 1 day after (timezone buffer).
                            if -1 <= days_until <= pre_event_days:
                                rule = "paper_catalyst_pre_event_flat"
                        except (TypeError, ValueError):
                            pass

                if not rule:
                    # F6: Ratchet peak from Alpaca price and evaluate trailing/time stops.
                    if plan is not None and current_price is not None and catalyst_rules is not None:
                        # update_catalyst_peak saves to disk — do it in-memory to batch saves.
                        entry = plan.entry_price
                        old_peak = plan.peak_price
                        new_peak = current_price if (old_peak is None or current_price > old_peak) else old_peak
                        if new_peak != old_peak:
                            plan.peak_price = new_peak
                            plan.last_updated = datetime.now(timezone.utc).isoformat()
                            plans[tk] = plan
                            plans_dirty = True
                        # Evaluate trailing stop and time stop via shared helper.
                        rule = eval_catalyst_exit(plan, current_price, catalyst_rules)
                    else:
                        # No plan — fall back to legacy time-stop logic from ledger.
                        # Anchor: measure from max(catalyst_date, buy_ts) so that a
                        # past catalyst_date (e.g. PEAD report date = yesterday) does
                        # NOT fire the time stop on the very first tick after entry.
                        time_stop_days = int(cfg.get("portfolio_advisor_catalyst_time_stop_days", 3) or 3)
                        if catalyst_date:
                            try:
                                from datetime import timedelta as _td
                                cat_dt = datetime.fromisoformat(catalyst_date[:10])
                                cat_dt = cat_dt.replace(tzinfo=timezone.utc)
                                # Anchor to the buy timestamp when available so the
                                # clock doesn't start before the trade was entered.
                                try:
                                    buy_dt = datetime.fromisoformat(
                                        str(buy.get("ts") or "").replace("Z", "+00:00")
                                    ).replace(tzinfo=timezone.utc)
                                    anchor_dt = max(cat_dt, buy_dt)
                                except (TypeError, ValueError):
                                    anchor_dt = cat_dt
                                if (now - anchor_dt).days >= time_stop_days:
                                    rule = f"paper_catalyst_time_stop_{time_stop_days}d_post_catalyst"
                            except (TypeError, ValueError):
                                pass
                        if not rule:
                            try:
                                opened = datetime.fromisoformat(str(buy.get("ts") or "").replace("Z", "+00:00"))
                                if (now - opened).days >= max_hold:
                                    rule = f"paper_catalyst_max_hold_{max_hold}d"
                            except (TypeError, ValueError):
                                pass

            elif sleeve == "core":
                if plpc <= -0.40:
                    rule = "paper_core_hard_stop_-40pct"
                elif (
                    plan is not None
                    and current_price is not None
                    and plan.entry_price > 0
                    and current_price >= plan.entry_price * 2.0
                    and not plan.double_trim_done_at  # one-shot: not yet consumed
                ):
                    # F5.3: +100% trim-half — close 50% once.
                    rule = "paper_core_double_trim"
                    trim_fraction = 0.5

            if rule:
                result = close_for_watchdog(cfg, tk, trim_fraction, rule)
                if result is not None:
                    closed += 1
                    if rule == "paper_core_double_trim" and plan is not None:
                        # Persist the consumption flag so this rule never re-fires.
                        plan.double_trim_done_at = datetime.now(timezone.utc).isoformat()
                        plan.last_updated = plan.double_trim_done_at
                        plans[tk] = plan
                        plans_dirty = True
                    elif rule == "paper_catalyst_pre_event_flat" and plan is not None:
                        # Persist the consumption flag so re-open-order retries don't re-spam.
                        plan.pre_event_flat_done_at = datetime.now(timezone.utc).isoformat()
                        plan.last_updated = plan.pre_event_flat_done_at
                        plans[tk] = plan
                        plans_dirty = True

        # Batch-save position plans if peaks or consumption flags changed.
        if plans_dirty:
            try:
                save_position_plans(cfg, plans)
            except Exception:
                logger.debug("enforce_paper_exits: failed to save plans", exc_info=True)

        return closed
    except Exception:
        logger.debug("enforce_paper_exits skipped", exc_info=True)
        return 0


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
        # Market clock (Alpaca-authoritative: weekends, US holidays, half-days).
        clock = market_clock()
        if clock is not None:
            if clock.get("is_open"):
                lines.append(f"Market: OPEN (closes {str(clock.get('next_close'))[:16]})")
            else:
                lines.append(
                    f"Market: CLOSED (weekend/holiday/after-hours) — next open {str(clock.get('next_open'))[:16]}. "
                    "Orders placed now queue as DAY orders and fill at the next open; "
                    "position prices below are frozen at the last close."
                )
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
