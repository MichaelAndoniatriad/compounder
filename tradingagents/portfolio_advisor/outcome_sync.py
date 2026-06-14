"""Close memory outcomes and surface stale open decisions after eToro sync."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from tradingagents.agents.utils.event_log import append_event
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.integrations.etoro.clerk_bridge import _normalize_ticker
from tradingagents.portfolio_advisor import state as pa_state
from tradingagents.portfolio_advisor.plan_validation import (
    group_position_rows_by_ticker,
    weighted_avg_open_for_lots,
)

logger = logging.getLogger(__name__)


def _account_mode_safe() -> str:
    """Return the active account mode, falling back to 'alpaca' on any error."""
    try:
        from tradingagents.portfolio_advisor.etoro_scan import account_mode
        return account_mode()
    except Exception:
        return "alpaca"


def _parse_event_ts(row: Dict[str, Any]) -> Optional[datetime]:
    ts = row.get("timestamp")
    if not isinstance(ts, str):
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _yf_close_on_or_after(ticker: str, d0: date) -> Optional[float]:
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        hist = t.history(start=d0.isoformat(), auto_adjust=True)
        if hist is None or len(hist.index) == 0:
            return None
        return float(hist["Close"].iloc[0])
    except Exception as e:
        logger.debug("outcome_sync entry price fetch %s: %s", ticker, e)
        return None


def _yf_last_close(ticker: str) -> Optional[float]:
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period="7d")
        if hist is None or len(hist.index) == 0:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug("outcome_sync last close %s: %s", ticker, e)
        return None


def _alignment(rating: str, pnl_pct: float) -> str:
    r = (rating or "Hold").strip()
    if r == "Buy":
        if pnl_pct > 2.0:
            return "correct"
        if pnl_pct < -2.0:
            return "incorrect"
        return "partial"
    if r == "Sell":
        if pnl_pct < -2.0:
            return "correct"
        if pnl_pct > 2.0:
            return "incorrect"
        return "partial"
    return "partial"


def _recent_has(
    cfg: Dict[str, Any],
    *,
    ticker: str,
    event_type: str,
    within_days: int,
) -> bool:
    from tradingagents.agents.utils import event_log as el

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(within_days))
    for row in reversed(el._iter_events(cfg, max_lines=4000)):
        if str(row.get("event_type")) != event_type:
            continue
        if str(row.get("ticker", "")).strip().upper() != ticker.strip().upper():
            continue
        dt = _parse_event_ts(row)
        if dt and dt >= cutoff:
            return True
    return False


def _aggregate_units_by_ticker(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for r in rows or []:
        sym = _normalize_ticker(str(r.get("symbolFull") or ""))
        if not sym:
            continue
        try:
            v = float(r.get("units") or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        out[sym] = out.get(sym, 0.0) + v
    return out


def _sync_partial_unit_changes(
    cfg: Dict[str, Any],
    live: Set[str],
    rows: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Detect same ticker still open with lower total units than last snapshot.

    Returns the mutated state dict so the caller can save it — does not call
    ``pa_state.save_state`` internally to avoid overwriting concurrent mutations.
    Returns None when there is nothing to do (empty rows).
    """
    if not rows:
        return
    st = pa_state.load_state(cfg)
    prev_raw = st.get("last_book_units_by_ticker") or {}
    if not isinstance(prev_raw, dict):
        prev_raw = {}
    prev: Dict[str, float] = {}
    for k, v in prev_raw.items():
        sym = str(k).strip().upper()
        if not sym:
            continue
        try:
            prev[sym] = float(v)
        except (TypeError, ValueError):
            continue
    current = _aggregate_units_by_ticker(rows)
    by_lot = group_position_rows_by_ticker(rows)
    for sym in live:
        if sym not in prev:
            continue
        before = float(prev[sym])
        after = float(current.get(sym, 0.0))
        if before <= 0:
            continue
        if after >= before - 1e-9:
            continue
        lots = by_lot.get(sym, [])
        entry_px = weighted_avg_open_for_lots(lots)
        last_px = _yf_last_close(sym)
        price_delta_pct: Optional[float] = None
        if last_px is not None and entry_px > 0:
            price_delta_pct = (last_px - entry_px) / entry_px * 100.0
        append_event(
            cfg,
            {
                "ticker": sym,
                "event_type": "partial_close_outcome",
                "key_data": {
                    "ticker": sym,
                    "units_before": before,
                    "units_after": after,
                    "units_delta": after - before,
                    "weighted_avg_entry_proxy": entry_px if entry_px > 0 else None,
                    "last_close_proxy": last_px,
                    "price_delta_pct_vs_entry_proxy": price_delta_pct,
                    "pnl_source": "yfinance_proxy",
                    "note": (
                        "Units fell while the name stayed in the live book. "
                        "This is not eToro realized cash P and L. Use eToro history for exact fills."
                    ),
                },
                "outcome": None,
            },
        )
    st["last_book_units_by_ticker"] = {k: float(v) for k, v in current.items()}
    st["last_book_source"] = _account_mode_safe()
    return st


def auto_close_outcomes(
    cfg: Dict[str, Any],
    live: Set[str],
    *,
    rows: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Run after any successful eToro portfolio read.

    Partial unit detection loads and saves advisor state internally via
    ``_sync_partial_unit_changes`` (``load_state`` then ``save_state``), so the prior
    ``last_book_units_by_ticker`` snapshot is always read from disk inside this function.
    """
    if rows is not None:
        try:
            mutated_st = _sync_partial_unit_changes(cfg, live, rows)
            if mutated_st is not None:
                pa_state.save_state(cfg, mutated_st)
        except Exception as e:
            logger.warning("outcome_sync: partial unit sync failed: %s", e)

    mem = TradingMemoryLog(cfg)
    today = date.today()
    now = datetime.now(timezone.utc)

    for e in mem.get_pending_entries():
        t = str(e.get("ticker") or "").strip().upper()
        td = str(e.get("date") or "")[:10]
        if not t or len(td) < 10:
            continue
        if t in live:
            continue
        try:
            d0 = datetime.strptime(td, "%Y-%m-%d").date()
        except ValueError:
            continue
        entry_px = _yf_close_on_or_after(t, d0)
        exit_px = _yf_last_close(t)
        if entry_px is None or exit_px is None or entry_px <= 0:
            logger.info("outcome_sync skip %s: missing yfinance prices", t)
            continue
        pnl_pct = (exit_px - entry_px) / entry_px * 100.0
        raw_ret = pnl_pct / 100.0
        rating = parse_rating(str(e.get("decision") or ""))
        align = _alignment(rating, pnl_pct)
        holding_days = max(0, (today - d0).days)
        refl = (
            f"Auto closed: position no longer in eToro book. "
            f"Outcome alignment {align} versus rating {rating}. "
            f"Public market close proxies only. Not eToro realized P and L."
        )
        try:
            mem.update_with_outcome(
                ticker=t,
                trade_date=td,
                raw_return=float(raw_ret),
                alpha_return=0.0,
                holding_days=int(holding_days),
                reflection=refl,
            )
        except Exception as ex:
            logger.warning("outcome_sync update failed %s: %s", t, ex)
            continue
        append_event(
            cfg,
            {
                "ticker": t,
                "event_type": "outcome_recorded",
                "key_data": {
                    "ticker": t,
                    "decision_date": td,
                    "close_date": today.isoformat(),
                    "entry_price": entry_px,
                    "close_price": exit_px,
                    "pnl_pct": pnl_pct,
                    "decision_was": rating,
                    "outcome_alignment": align,
                    "pnl_source": "yfinance_proxy",
                    "source": "memory_pending_auto",
                },
                "outcome": align,
            },
        )

    from tradingagents.agents.utils import event_log as el

    pending_30_syms: Set[str] = set()
    for row in reversed(el._iter_events(cfg, max_lines=12000)):
        et = str(row.get("event_type") or "")
        if et not in ("full_graph_decision", "post_earnings_verdict"):
            continue
        if row.get("outcome") is not None:
            continue
        sym = str(row.get("ticker") or "").strip().upper()
        if not sym or sym == "*":
            continue
        if sym not in live:
            continue
        if sym in pending_30_syms:
            continue
        ev_dt = _parse_event_ts(row)
        if not ev_dt:
            continue
        if (now - ev_dt).days <= 30:
            continue
        if _recent_has(cfg, ticker=sym, event_type="pending_outcome_30d", within_days=14):
            continue
        kd = row.get("key_data") if isinstance(row.get("key_data"), dict) else {}
        append_event(
            cfg,
            {
                "ticker": sym,
                "event_type": "pending_outcome_30d",
                "key_data": {
                    "ticker": sym,
                    "original_event_type": et,
                    "original_timestamp": row.get("timestamp"),
                    "rating_or_excerpt": kd.get("rating") or (str(kd.get("excerpt") or "")[:200]),
                },
                "outcome": None,
            },
        )
        pending_30_syms.add(sym)
