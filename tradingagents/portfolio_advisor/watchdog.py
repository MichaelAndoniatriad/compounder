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
    "pre_earnings_trim_window": "Required action: sell half before the earnings print.",
    "dd30_review": "Required action: review window open. Decision point is next scheduled earnings.",
}


def _format_ticker_block(t: Dict[str, Any]) -> List[str]:
    """One per-ticker block: human-readable data line, action line, blank line."""
    gain = float(t.get("gain_pct") or 0.0)
    dd = float(t.get("drawdown_pct") or 0.0)
    entry = float(t.get("entry") or 0.0)
    price = float(t.get("price") or 0.0)
    direction = "up" if gain >= 0 else "down"
    magnitude = abs(gain) if gain >= 0 else dd
    data = (
        f"{t['ticker']}: {direction} {magnitude:.1f}% "
        f"(entry ~${entry:.2f}, now ~${price:.2f})."
    )
    codes = t.get("codes") or []
    primary_code = codes[0] if codes else ""
    action = ACTION_LINES.get(primary_code, "Review required.")
    return [data, f"  {action}", ""]


def in_us_equity_watch_window_utc() -> bool:
    """Weekdays roughly Mon to Fri, 13:30 to 20:00 UTC inclusive of end minute.

    Fallback heuristic only — no US-holiday or DST awareness. Prefer
    market_is_open(), which asks Alpaca's clock first and falls back here.
    """
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def market_is_open() -> bool:
    """True when US equities are tradable right now.

    Alpaca's clock API is authoritative (weekends, US bank holidays, half-days,
    DST). When it is unreachable (no keys, network down, pytest) fall back to
    the fixed UTC weekday window so the watchdog never goes blind.
    """
    try:
        from tradingagents.integrations.alpaca import executor as _alpaca_exec

        clock = _alpaca_exec.market_clock()
        if clock is not None:
            return bool(clock.get("is_open"))
    except Exception:
        logger.debug("alpaca market clock check failed", exc_info=True)
    return in_us_equity_watch_window_utc()


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
        units = sum(_to_float(l.get("units")) for l in lots)
        base = {
            "ticker": sym,
            "entry": entry,
            "price": px,
            "gain_pct": gain,
            "drawdown_pct": dd,
            "units": units,
            # Include earnings_date so the consumed-marker check can key on it.
            "earnings_date": str(ed) if ed is not None else "",
        }
        if dd >= 40.0:
            mandatory.append({**base, "codes": ["dd40_mandatory_exit"]})
            continue
        # The double_from_entry rule was removed: per-share gain%% does not
        # change when the human sells shares, so a position that stayed >100%%
        # after a half-sell would re-trigger forever. The user trims on their
        # own discretion now; the watchdog only handles drawdown + pre-earnings.
        trim_codes: List[str] = []
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

    # Drop exit proposals for names that left the book (position closed) so they
    # stop being nagged about — runs every tick during market hours.
    try:
        from tradingagents.portfolio_advisor import proposals as _proposals
        _proposals.reconcile_with_portfolio(cfg, live)
    except Exception:
        logger.debug("watchdog: proposal reconcile skipped", exc_info=True)

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


_HANDOFF_KIND = {
    "new": "NEW",
    "escalated": "ESCALATED",
    "deescalated": "de-escalated",
    "refresh": "ongoing",
}
_HANDOFF_BUCKET = {
    "dd40_mandatory_exit": "40% drawdown \u2014 mandatory-exit policy",
    "trim_half": "double or pre-earnings \u2014 sell-half policy",
    "dd30_review": "30% drawdown \u2014 review window",
}


def _format_watchdog_handoff(
    changes: List[Dict[str, Any]], cleared: List[str]
) -> str:
    """Plain-text summary of watchdog changes for the PM's extra_context."""
    lines = [
        "Watchdog price triggers changed (price-only monitor, no graph). "
        "You decide if and what reaches the user.",
        "",
    ]
    for ch in changes:
        t = ch["data"]
        lines.append(
            f"- {ch['ticker']}: {_HANDOFF_KIND.get(ch['kind'], ch['kind'])} "
            f"-> {_HANDOFF_BUCKET.get(ch['bucket'], ch['bucket'])}; "
            f"gain {float(t.get('gain_pct') or 0.0):+.1f}%, "
            f"drawdown {float(t.get('drawdown_pct') or 0.0):.1f}% "
            f"(entry ~${float(t.get('entry') or 0.0):.2f}, now ~${float(t.get('price') or 0.0):.2f})."
        )
    if cleared:
        lines.append(f"- Cleared (no longer triggering): {', '.join(sorted(cleared))}.")
    lines.append("")
    lines.append(
        "Use watchdog_updates to acknowledge, mute, clear, or annotate any of these "
        "once you have reasoned about them."
    )
    return "\n".join(lines)


def _send_direct_watchdog_alerts(
    cfg: Dict[str, Any],
    mandatory: List[Dict[str, Any]],
    trim: List[Dict[str, Any]],
    review: List[Dict[str, Any]],
) -> int:
    """Legacy path: DM the user directly per non-empty bucket. Off by default."""
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
            "Watchdog: mandatory exit (40% drawdown hit)",
            "\n".join(lines),
            urgent=True,
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
            "Watchdog: sell half (double or pre-earnings trim)",
            "\n".join(lines),
            urgent=True,
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
            "Watchdog: drawdown review (30% threshold)",
            "\n".join(lines),
            urgent=True,
        )
        sent += 1
    return sent


def run_watchdog(cfg: Dict[str, Any], *, ignore_market_hours: bool = False, suppress_pm_handoff: bool = False) -> int:
    """Return count of outbound watchdog notifications (0 to 3 if all buckets fire)."""
    if not ignore_market_hours and not market_is_open():
        logger.info("watchdog skipped (market closed: weekend, US holiday, or outside hours)")
        return 0

    # Paper-book hard floor — runs EVERY tick, before any eToro-dependent early
    # return: positions held only on Alpaca (advice the human didn't take on
    # eToro) are invisible to the eToro trigger logic below, so their -8%/-40%
    # stops are enforced directly from Alpaca's own P&L here.
    try:
        from tradingagents.integrations.alpaca import executor as _alpaca_exec

        _alpaca_exec.enforce_paper_exits(cfg)
    except Exception:
        logger.debug("paper exit enforcement skipped", exc_info=True)

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

    # The watchdog is an input to the PM, not a direct user channel. Instead of
    # DMing the user every 5 minutes, latch each trigger in state and wake the PM
    # only when something *changes* (a ticker enters a bucket, escalates,
    # de-escalates, or clears). The PM then owns what (if anything) reaches the
    # user, through its quiet-hours-aware push_note.
    from datetime import datetime as _dt, timezone as _tz

    now_iso = _dt.now(_tz.utc).isoformat()
    bucketed: List[tuple] = (
        [("dd40_mandatory_exit", t) for t in mandatory]
        + [("trim_half", t) for t in trim]
        + [("dd30_review", t) for t in review]
    )

    st = pa_state.load_state(cfg)
    changes: List[Dict[str, Any]] = []
    active: List[str] = []
    for bucket, t in bucketed:
        sym = str(t.get("ticker") or "").strip().upper()
        if not sym:
            continue
        active.append(sym)
        res = pa_state.upsert_watchdog_trigger(
            st,
            sym,
            bucket=bucket,
            codes=list(t.get("codes") or []),
            gain_pct=float(t.get("gain_pct") or 0.0),
            drawdown_pct=float(t.get("drawdown_pct") or 0.0),
            now_iso=now_iso,
            entry=float(t.get("entry") or 0.0),
            price=float(t.get("price") or 0.0),
            units=float(t.get("units") or 0.0),
        )
        if res.get("changed"):
            changes.append(
                {
                    "ticker": sym,
                    "bucket": bucket,
                    "kind": res.get("kind"),
                    "prev_bucket": res.get("prev_bucket"),
                    "data": t,
                }
            )
    cleared = pa_state.clear_watchdog_triggers_not_in(st, active, now_iso)

    # Mark this tick's genuine changes as needing a PM hand-off.
    wt_now = pa_state.get_watchdog_triggers(st)
    for ch in changes:
        row = wt_now.get(ch["ticker"])
        if isinstance(row, dict):
            row["handoff_pending"] = True

    # The hand-off set is every latch still awaiting delivery — this tick's
    # changes PLUS any earlier trigger whose hand-off failed (e.g. the PM was
    # out of credits). That way the PM still learns about it once it recovers.
    # One-shot trims: a sell-half-on-double is consumed once executed. Detect
    # via a units drop vs the size when it first latched; the remaining runner
    # staying >100% is expected and must not re-alert. Re-arm only if the
    # position grows back (a genuine new entry). Also honor an explicit human
    # decision that the action is handled.
    from tradingagents.portfolio_advisor import pm_workspace as _pmws
    _settled_choices = {"executed", "done", "sold", "trimmed", "hold", "holding", "skip"}
    suppressed: set = set()
    for _sym, _row in wt_now.items():
        if not isinstance(_row, dict):
            continue
        _codes = _row.get("codes") or []
        _uat = _row.get("units_at_trigger")
        _cur = _row.get("units")
        if _row.get("bucket") == "trim_half" and "double_from_entry" in _codes and _uat and _cur is not None:
            if float(_cur) <= float(_uat) * 0.6:
                _row["double_trim_consumed"] = True
            elif float(_cur) > float(_uat) * 1.1:
                _row["double_trim_consumed"] = False
                _row["units_at_trigger"] = float(_cur)
        if _row.get("double_trim_consumed"):
            suppressed.add(_sym)
        try:
            _dec = _pmws.active_decision(cfg, _sym)
        except Exception:
            _dec = None
        if _dec and str(_dec.get("choice", "")).strip().lower() in _settled_choices:
            suppressed.add(_sym)
    if suppressed:
        changes = [c for c in changes if c["ticker"] not in suppressed]
        for _s in suppressed:
            _r = wt_now.get(_s)
            if isinstance(_r, dict):
                _r["handoff_pending"] = False

    pending = [tk for tk, row in wt_now.items()
               if isinstance(row, dict) and row.get("handoff_pending")]

    if not pending and not cleared:
        pa_state.save_state(cfg, st)
        logger.info("watchdog: nothing changed or pending (active=%d)", len(active))
        return 0

    # Audit log: one event per genuine change this tick. The PM reads these.
    for ch in changes:
        d = ch["data"]
        append_event(
            cfg,
            {
                "ticker": ch["ticker"],
                "event_type": "watchdog_trigger_change",
                "key_data": {
                    "bucket": ch["bucket"],
                    "kind": ch["kind"],
                    "prev_bucket": ch["prev_bucket"],
                    "gain_pct": d.get("gain_pct"),
                    "drawdown_pct": d.get("drawdown_pct"),
                },
                "outcome": None,
            },
        )

    # Legacy: direct-to-user alerts. Off by default — the PM is the user channel now.
    if bool(cfg.get("portfolio_advisor_watchdog_direct_alerts", False)):
        _send_direct_watchdog_alerts(cfg, mandatory, trim, review)

    # Mirror deterministic exits onto the Alpaca PAPER book immediately — rule
    # exits are the risk limit and must not wait for the next PM cycle. The
    # executor is idempotent (skips names the paper book no longer holds).
    #
    # Pre-earnings trims are one-shot: after a trim fires for (ticker, earnings_date)
    # it is marked consumed in state so subsequent ticks don't re-trim.
    try:
        from tradingagents.integrations.alpaca import executor as _alpaca

        for t in mandatory:
            _alpaca.close_for_watchdog(cfg, t.get("ticker", ""), 1.0, "dd40_mandatory_exit")

        # Consumed-marker set for pre-earnings trims, keyed by "TICKER:earnings_date".
        consumed_trims: set = set()
        try:
            consumed_trims = set(st.get("pre_earnings_trim_consumed") or [])
        except (TypeError, AttributeError):
            consumed_trims = set()

        for t in trim:
            ticker_str = str(t.get("ticker") or "").strip().upper()
            codes = list(t.get("codes") or [])
            rule = ",".join(codes) or "trim"
            if "pre_earnings_trim_window" in codes:
                earnings_date_str = str(t.get("earnings_date") or "").strip()
                consumed_key = f"{ticker_str}:{earnings_date_str}"
                if consumed_key in consumed_trims:
                    logger.debug(
                        "watchdog: pre_earnings_trim_window for %s / %s already consumed — skipping",
                        ticker_str, earnings_date_str,
                    )
                    continue  # one-shot: do not re-fire
                result = _alpaca.close_for_watchdog(cfg, ticker_str, 0.5, rule)
                if result is not None:
                    # Mark consumed so this (ticker, earnings_date) pair never fires again.
                    consumed_trims.add(consumed_key)
                    st["pre_earnings_trim_consumed"] = sorted(consumed_trims)
            else:
                _alpaca.close_for_watchdog(cfg, ticker_str, 0.5, rule)
    except Exception:
        logger.debug("alpaca watchdog mirror skipped", exc_info=True)

    # Build the hand-off payload from the full pending set, reconstructing each
    # ticker's data from its latch so retries read the same as fresh changes.
    kind_by_ticker = {c["ticker"]: c["kind"] for c in changes}
    handoff_changes: List[Dict[str, Any]] = []
    for tk in sorted(pending):
        row = wt_now[tk]
        handoff_changes.append({
            "ticker": tk,
            "bucket": row.get("bucket"),
            "kind": kind_by_ticker.get(tk, "pending"),
            "prev_bucket": None,
            "data": {
                "ticker": tk,
                "gain_pct": row.get("gain_pct"),
                "drawdown_pct": row.get("drawdown_pct"),
                "entry": row.get("entry", 0.0),
                "price": row.get("price", 0.0),
                "codes": row.get("codes"),
            },
        })

    # Hand off to the PM. Best-effort: a PM failure must not crash the watchdog,
    # and must leave the triggers pending so the next tick retries them.
    handed_off = False
    if handoff_changes and not suppress_pm_handoff and bool(cfg.get("portfolio_advisor_pm_enabled", True)) and bool(
        cfg.get("portfolio_advisor_watchdog_pm_handoff", True)
    ):
        extra = _format_watchdog_handoff(handoff_changes, cleared)
        try:
            from tradingagents.portfolio_advisor.advisor_pm import run_pm_cycle

            run_pm_cycle(cfg, trigger="watchdog_price_trigger", extra_context=extra)
            handed_off = True
        except Exception:
            logger.exception("watchdog: PM hand-off cycle failed (will retry next tick)")

    if handed_off:
        for tk in pending:
            row = wt_now.get(tk)
            if isinstance(row, dict):
                row["handoff_pending"] = False
                row["last_pm_handoff_iso"] = now_iso

    pa_state.save_state(cfg, st)
    logger.info(
        "watchdog: %d changed, %d pending, %d cleared, pm_handoff=%s",
        len(changes), len(pending), len(cleared), handed_off,
    )
    return len(changes)
