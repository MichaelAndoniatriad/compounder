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
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="60d")["Close"]
        if len(hist) >= 20:
            returns = hist.pct_change().dropna()
            vol = float(returns.std() * (252 ** 0.5))  # annualized
            # Map vol to a dampener: vol 0.30 → 1.0 (neutral), 0.60+ → 1.5 (halved), 0.15 → 0.75 (boosted)
            dampener = max(0.5, min(1.5, vol / 0.30))
            target_pct = target_pct / dampener
    except Exception:
        pass  # skip dampener on any failure

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
        if detail.startswith("skipped"):
            return {"status": "skipped", "detail": detail}
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
    from alpaca.trading.requests import MarketOrderRequest

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

    scale = _scale_factor(cfg, equity)
    cap = float(cfg.get("portfolio_advisor_alpaca_max_position_pct", 0.10) or 0.10)
    # §5.4: confidence-weighted sizing — multiplier ∈ [0.2, 1.0] of the cap
    conf = proposal.get("confidence")
    conf_mult = _confidence_size_multiplier(cfg, conf, tk)
    notional = round(min(usd * scale, equity * cap * conf_mult), 2)
    if notional < 1.0:
        _log_row(cfg, {"ticker": tk, "action": "buy", "status": "skipped", "note": f"notional {notional} < $1"})
        return f"skipped {tk}: scaled notional under $1"

    order = client.submit_order(
        MarketOrderRequest(symbol=tk, notional=notional, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
    )
    sleeve = (proposal.get("sleeve") or "").lower() or "core"
    catalyst_date = (proposal.get("catalyst_date") or "").strip() or None
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
            "proposal_ts": proposal.get("ts"),
        },
    )

    # Auto-create a PositionPlan so exit rules (trailing stop, time stop,
    # hard stops) actually run for autonomous buys. Wraps in try/except so a
    # plan-creation failure never breaks the buy path.
    try:
        _auto_create_position_plan(cfg, tk, proposal, sleeve, catalyst_date)
    except Exception:
        logger.debug("auto-create position plan failed for %s", tk, exc_info=True)

    conf_note = f", conf {conf:.2f}→{conf_mult:.0%} of cap" if conf is not None else ""
    sleeve_display = sleeve if sleeve != "?" else "?"
    if abs(scale - 1.0) < 1e-9:
        msg = f"BUY {tk} ~${notional:,.0f} ({sleeve_display}) executed on Alpaca paper{conf_note}."
    else:
        msg = f"BUY {tk} ~${notional:,.0f} ({sleeve_display}) submitted to Alpaca paper (scaled from ~${usd:,.0f} eToro-size{conf_note})."
    _notify(cfg, f"BUY {tk}", msg + "\nFills at next market open if currently closed.")
    return msg


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
) -> None:
    """Create or upsert a PositionPlan for an autonomous buy.

    Entry price: proposal target_price if > 0, else last close from yfinance.
    Existing plans are NOT overwritten — an existing plan means the user or a
    prior autonomous buy already set one up.
    """
    from tradingagents.portfolio_advisor.position_plans import (
        PositionPlan,
        load_position_plans,
        upsert_position_plan,
    )

    existing = load_position_plans(cfg)
    if tk in existing:
        # Plan already exists — do not clobber it. Only update catalyst_date if
        # the existing plan has none and the new proposal supplies one.
        plan = existing[tk]
        if catalyst_date and not plan.catalyst_date:
            plan.catalyst_date = catalyst_date
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

    note = f"auto-created on autonomous buy {_now_iso()}"
    plan = PositionPlan(
        ticker=tk,
        entry_price=entry_price,
        strategy=sleeve if sleeve in ("core", "catalyst") else "core",
        entry_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        catalyst_date=catalyst_date or "",
        notes=note,
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
                        time_stop_days = int(cfg.get("portfolio_advisor_catalyst_time_stop_days", 3) or 3)
                        if catalyst_date:
                            try:
                                from datetime import timedelta
                                cat_dt = datetime.fromisoformat(catalyst_date[:10])
                                cat_dt = cat_dt.replace(tzinfo=timezone.utc)
                                if (now - cat_dt).days >= time_stop_days:
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
