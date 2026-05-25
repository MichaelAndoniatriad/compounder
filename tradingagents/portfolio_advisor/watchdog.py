"""Price only watchdog during market hours. No LLM and no LangGraph."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List

from tradingagents.agents.utils.event_log import append_event
from tradingagents.portfolio_advisor import etoro_scan, messaging, outcome_sync, price_util, state as pa_state
from tradingagents.portfolio_advisor.plan_validation import (
    _gain_dd_pct,
    group_position_rows_by_ticker,
    representative_is_long_for_lots,
    weighted_avg_open_for_lots,
)
from tradingagents.integrations.etoro.portfolio import (
    position_invested_usd,
    position_unrealized_pnl,
)

logger = logging.getLogger(__name__)


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _current_price_from_lots(lots: list) -> float | None:
    """Derive live price from eToro row fields (unitsBaseValueDollars / units)."""
    for r in lots:
        try:
            ubv = r.get("unitsBaseValueDollars")
            units = r.get("units")
            if ubv is not None and units:
                return float(ubv) / float(units)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        try:
            open_rate = r.get("openRate")
            pnl = r.get("unrealizedPnL")
            units = r.get("units")
            if open_rate is not None and pnl is not None and units:
                return float(open_rate) + float(pnl) / float(units)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return None


ACTION_LINES = {
    "dd40_mandatory_exit": "Required action: full exit. No exceptions. No deliberating.",
    "double_from_entry": "Required action: sell half. Lock in recovered capital. Let remainder run.",
    "pre_earnings_trim_window": "Required action: sell half before the earnings print.",
    "dd30_review": "Required action: review window open. Decision point is next scheduled earnings.",
}


def _format_ticker_block(t: Dict[str, Any]) -> List[str]:
    """One per-ticker block: data line, primary action line, trailing blank line."""
    data = (
        f"{t['ticker']}: codes {t['codes']} | weighted_avg_entry {t['entry']:.4f} "
        f"| last ~{t['price']:.4f} | gain {t['gain_pct']:.1f}% | drawdown {t['drawdown_pct']:.1f}%"
    )
    codes = t.get("codes") or []
    primary_code = codes[0] if codes else ""
    action = ACTION_LINES.get(primary_code, "Review required.")
    return [data, f"  {action}", ""]


def in_us_equity_watch_window_utc() -> bool:
    """Weekdays roughly Mon to Fri, 13:30 to 20:00 UTC inclusive of end minute."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def _split_watchdog_triggers(
    rows: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (dd40_mandatory_exit, sell_half_trim, dd30_review_only).

    One line per ticker. Lots are merged using size weighted average open for gain or drawdown math.
    Buckets are mutually exclusive per ticker: dd40 wins; else trim half rules; else dd30 review.
    """
    mandatory: List[Dict[str, Any]] = []
    trim: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    by_sym = group_position_rows_by_ticker(rows)
    for sym, lots in by_sym.items():
        entry = weighted_avg_open_for_lots(lots)
        is_long = representative_is_long_for_lots(lots)
        # Gain/drawdown from eToro's own reported P&L — split-immune and reliable.
        # unitsBaseValueDollars/units is unreliable (often equals cost basis),
        # which produced false 0% gains and bogus mandatory-exit drawdowns.
        pnl = sum(_to_float(position_unrealized_pnl(l)) for l in lots)
        inv = sum(_to_float(position_invested_usd(l)) for l in lots)
        if inv > 0:
            gain = pnl / inv * 100.0
            dd = max(0.0, -gain)
            px = entry * (1.0 + gain / 100.0) if is_long else entry * (1.0 - gain / 100.0)
        elif entry > 0:
            # eToro P&L unavailable — fall back to the price-based estimate.
            px = _current_price_from_lots(lots) or price_util.last_close_yfinance(sym)
            if px is None:
                continue
            gain, dd = _gain_dd_pct(entry, px, is_long)
        else:
            continue
        ed_str = price_util.next_earnings_date_yfinance(sym)
        try:
            from datetime import datetime as _dt
            ed = _dt.strptime(ed_str, "%Y-%m-%d").date() if ed_str else None
        except (ValueError, TypeError):
            ed = None
        pre = (
            ed is not None
            and gain >= 15.0
            and 0 <= (ed - date.today()).days <= 14
        )
        base = {
            "ticker": sym,
            "entry": entry,
            "price": px,
            "gain_pct": gain,
            "drawdown_pct": dd,
        }
        if dd >= 40.0:
            mandatory.append({**base, "codes": ["dd40_mandatory_exit"]})
            continue
        trim_codes: List[str] = []
        if gain >= 100.0:
            trim_codes.append("double_from_entry")
        if pre:
            trim_codes.append("pre_earnings_trim_window")
        if trim_codes:
            trim.append({**base, "codes": trim_codes})
            continue
        if dd >= 30.0:
            review.append({**base, "codes": ["dd30_review"]})
    return mandatory, trim, review


def _watchdog_check_position_changes(
    cfg: Dict[str, Any],
    live_list: Any,
    rows: List[Dict[str, Any]],
) -> None:
    """Detect ticker additions/removals and ping the PM when the book changes."""
    live: set[str] = etoro_scan.current_ticker_set(live_list)

    try:
        outcome_sync.auto_close_outcomes(cfg, live, rows=rows)
    except Exception:
        logger.debug("watchdog: outcome_sync skipped", exc_info=True)

    st = pa_state.load_state(cfg)
    prev: set[str] = {str(t).upper().strip() for t in (st.get("last_portfolio_tickers") or []) if t}
    added = sorted(live - prev)
    removed = sorted(prev - live)

    if not added and not removed:
        return

    for j in list(st.get("jobs") or []):
        if j.get("status") != "pending":
            continue
        tid = str(j.get("ticker") or "").strip().upper()
        if tid and tid not in live:
            jid = str(j.get("id") or "")
            if jid:
                pa_state.cancel_job(st, jid, reason="watchdog: not in portfolio")

    st["last_portfolio_tickers"] = sorted(live)
    pa_state.save_state(cfg, st)

    logger.info("watchdog: position change detected — added %s removed %s", added, removed)
    try:
        from tradingagents.portfolio_advisor.advisor_pm import optional_pm_cycle_on_portfolio_change
        optional_pm_cycle_on_portfolio_change(
            cfg,
            trigger="watchdog_position_change",
            old_portfolio_text_hash=None,
            new_portfolio_text_hash="",
            tickers_added=added,
            tickers_removed=removed,
        )
    except Exception:
        logger.exception("watchdog: PM cycle on position change failed")


def run_watchdog(cfg: Dict[str, Any], *, ignore_market_hours: bool = False) -> int:
    """Return count of outbound watchdog notifications (0 to 3 if all buckets fire)."""
    if not ignore_market_hours and not in_us_equity_watch_window_utc():
        logger.info("watchdog skipped (outside US equity watch window UTC)")
        return 0

    rows: List[Dict[str, Any]] = []
    try:
        _payload, _text, _tickers, rows = etoro_scan.fetch_portfolio_rows()
    except Exception as e:
        logger.error("watchdog: eToro fetch failed: %s", e)
        try:
            st_err = pa_state.load_state(cfg)
            last_iso = st_err.get("last_watchdog_fetch_alert_iso") or ""
            from datetime import datetime as _dt2, timezone as _tz
            last_dt = _dt2.fromisoformat(last_iso) if last_iso else None
            now_utc = _dt2.now(_tz.utc)
            throttle_h = int(cfg.get("portfolio_advisor_silent_alert_throttle_hours") or 6)
            if last_dt is None or (now_utc - last_dt).total_seconds() >= throttle_h * 3600:
                st_err["last_watchdog_fetch_alert_iso"] = now_utc.isoformat()
                pa_state.save_state(cfg, st_err)
                messaging.send_advisor_message(
                    cfg,
                    "Advisor: watchdog portfolio fetch failed",
                    f"Watchdog could not fetch eToro portfolio: {e}. Price triggers are paused until this clears.",
                    urgent=True,
                )
        except Exception:
            logger.debug("watchdog: failed to send fetch-failure alert", exc_info=True)
        return 0

    _watchdog_check_position_changes(cfg, _tickers, rows)

    mandatory, trim, review = _split_watchdog_triggers(rows)
    sent = 0
    if mandatory:
        lines = [
            "Watchdog CRITICAL (price only, no graph).",
            "Policy: full exit within your written window. This is not a sell half trim.",
            "",
        ]
        for t in mandatory:
            lines.extend(_format_ticker_block(t))
        messaging.send_advisor_message(
            cfg,
            "[TradingAgents] Watchdog CRITICAL dd40_mandatory_exit",
            "\n".join(lines),
            urgent=True,
        )
        append_event(
            cfg,
            {
                "ticker": "*",
                "event_type": "watchdog_critical_alert",
                "key_data": {"triggers": mandatory},
                "outcome": None,
            },
        )
        sent += 1
    if trim:
        lines = [
            "Watchdog HIGH sell half policy (price only, no graph).",
            "Policy: trim or scale per your rules (often sell half), not a mandatory full exit unless you also have dd40 in a separate notice.",
            "",
        ]
        for t in trim:
            lines.extend(_format_ticker_block(t))
        messaging.send_advisor_message(
            cfg,
            "[TradingAgents] Watchdog HIGH sell_half double_or_pre_earnings",
            "\n".join(lines),
            urgent=True,
        )
        append_event(
            cfg,
            {
                "ticker": "*",
                "event_type": "watchdog_trim_alert",
                "key_data": {"triggers": trim},
                "outcome": None,
            },
        )
        sent += 1
    if review:
        lines = [
            "Watchdog HIGH dd30 review (price only, no graph).",
            "Policy: review window, not a mandatory full exit by itself.",
            "",
        ]
        for t in review:
            lines.extend(_format_ticker_block(t))
        messaging.send_advisor_message(
            cfg,
            "[TradingAgents] Watchdog HIGH dd30_review",
            "\n".join(lines),
            urgent=True,
        )
        append_event(
            cfg,
            {
                "ticker": "*",
                "event_type": "watchdog_high_alert",
                "key_data": {"triggers": review},
                "outcome": None,
            },
        )
        sent += 1
    return sent
