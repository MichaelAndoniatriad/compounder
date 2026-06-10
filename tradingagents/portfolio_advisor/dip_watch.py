"""Quality-on-a-dip watch — price-only scan, no LLM here.

Watches the CORE (quality) names already on the watchlist and, when one pulls back
into a shallow BUY ZONE — a dip, not a falling knife — wakes the PM to judge the
fundamentals/estimates and, if the thesis is intact, propose a staged entry.

Research basis (deep-research, 2026-06-09): the exploitable, anti-herd version of
"buy the dip" is a QUALITY filter + a SHALLOW dislocation. Stocks far below trend
keep falling (momentum, the falling-knife signature) rather than reverting, so the
buy zone is a modest pullback off the high — and the value-trap check (estimate
revisions / guidance / margins) is left to the PM's judgment before any ticket.

Mirrors watchdog.py: a price-only sweep that latches per-ticker state and only
wakes the PM when something *changes* (a name newly enters the buy zone), so a name
sitting in the zone doesn't re-ping every scan. The PM owns what reaches the user.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from tradingagents.portfolio_advisor import price_util, state as pa_state, watchlist as watchlist_mod
from tradingagents.portfolio_advisor.watchdog import market_is_open

logger = logging.getLogger(__name__)

_LATCH_KEY = "dip_watch_triggers"


def _f(cfg: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def classify_dip(signal: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[str, str]:
    """Return (verdict, reason); verdict in {buy_zone, falling_knife, neutral}.

    Falling-knife guards run FIRST — a deep dislocation is momentum-down, not a
    reversion setup, and must never be surfaced as a buy. The buy zone is a shallow
    dip below trend that is meaningfully off its high but not crashing.
    """
    below = float(signal.get("below_ma_pct") or 0.0)
    off_high = float(signal.get("off_high_pct") or 0.0)
    rsi = signal.get("rsi")

    ma_window = int(_f(cfg, "portfolio_advisor_dip_watch_ma_window", 50))
    knife_below = _f(cfg, "portfolio_advisor_dip_watch_falling_knife_below_ma_pct", 25.0)
    off_high_max = _f(cfg, "portfolio_advisor_dip_watch_off_high_max_pct", 35.0)
    rsi_floor = _f(cfg, "portfolio_advisor_dip_watch_rsi_floor", 25.0)
    dip_min = _f(cfg, "portfolio_advisor_dip_watch_min_below_ma_pct", 0.0)
    dip_max = _f(cfg, "portfolio_advisor_dip_watch_max_below_ma_pct", 12.0)
    off_high_min = _f(cfg, "portfolio_advisor_dip_watch_off_high_min_pct", 8.0)

    # --- falling-knife guards (reject) ---
    if below >= knife_below:
        return "falling_knife", f"{below:.0f}% below the {ma_window}-day MA — too deep, momentum-down"
    if off_high >= off_high_max:
        return "falling_knife", f"{off_high:.0f}% off the high — collapsed, not a pullback"
    if rsi is not None and float(rsi) < rsi_floor:
        return "falling_knife", f"RSI {float(rsi):.0f} — capitulation, let it stabilise"

    # --- buy zone: a shallow dip below trend that's off its high but not crashing ---
    if dip_min <= below <= dip_max and off_high >= off_high_min:
        extra = f", RSI {float(rsi):.0f}" if rsi is not None else ""
        return "buy_zone", f"{below:.0f}% below the {ma_window}-day MA, {off_high:.0f}% off the high{extra}"

    return "neutral", "above trend / not dipped enough"


def _core_watchlist_names(cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    """CORE (non-catalyst) watchlist entries — the quality names we watch for dips."""
    out: List[Dict[str, str]] = []
    seen: set = set()
    for e in watchlist_mod.load_watchlist(cfg):
        if isinstance(e, str):
            tk, strat, thesis = e.strip().upper(), "core", ""
        elif isinstance(e, dict):
            tk = str(e.get("ticker") or "").strip().upper()
            strat = str(e.get("strategy") or "core").strip().lower()
            thesis = str(e.get("thesis") or "")
        else:
            continue
        if not tk or strat == "catalyst" or tk in seen:
            continue
        seen.add(tk)
        out.append({"ticker": tk, "thesis": thesis})
    return out


def _held_tickers(cfg: Dict[str, Any]) -> set:
    """Best-effort live book — we don't surface a dip-buy for a name already held."""
    try:
        from tradingagents.portfolio_advisor import etoro_scan

        _payload, _text, tickers, _rows = etoro_scan.fetch_portfolio_rows()
        return etoro_scan.current_ticker_set(tickers)
    except Exception:
        logger.debug("dip-watch: held-book fetch skipped", exc_info=True)
        return set()


def _latch(state: Dict[str, Any]) -> Dict[str, Any]:
    d = state.get(_LATCH_KEY)
    if not isinstance(d, dict):
        d = {}
        state[_LATCH_KEY] = d
    return d


def _format_dip_handoff(rows: List[Dict[str, Any]]) -> str:
    """Plain-text summary of fresh dips for the PM's extra_context (directive)."""
    lines = [
        "Quality watchlist names just pulled back into a BUY ZONE (price-only dip "
        "screen — a shallow dip that already passed the falling-knife guard). "
        "You decide if and what reaches the user.",
        "",
    ]
    for r in rows:
        rsi = r.get("rsi")
        line = (
            f"- {r['ticker']}: now ~${float(r.get('price') or 0):.2f}, "
            f"{float(r.get('below_ma_pct') or 0):.0f}% below its MA, "
            f"{float(r.get('off_high_pct') or 0):.0f}% off its recent high"
            + (f", RSI {float(rsi):.0f}" if rsi is not None else "")
        )
        if r.get("thesis"):
            line += f". Thesis on file: {str(r['thesis'])[:160]}"
        lines.append(line)
    lines += [
        "",
        "Before proposing, confirm this is a SENTIMENT dip on intact quality, NOT a "
        "value trap: check estimate-revision direction (falling forward EPS = trap), "
        "any recent guidance cut, and the margin trajectory. If it's a real dip on an "
        "intact thesis, propose a STAGED CORE entry via propose_trade (sleeve='core', "
        "target_price=the current price, ~1/3 now). If estimates are falling or "
        "guidance was cut, skip it and say why — do not buy the knife.",
    ]
    return "\n".join(lines)


def run_dip_watch(
    cfg: Dict[str, Any],
    *,
    ignore_market_hours: bool = False,
    suppress_pm_handoff: bool = False,
) -> int:
    """Scan core watchlist names for fresh dips; hand new ones to the PM.

    Returns the number of NEW dips handed off this scan (0 if none / disabled /
    outside market hours). Idempotent within a dip: a name already latched in the
    zone is not re-handed until it leaves and re-enters.
    """
    if not bool(cfg.get("portfolio_advisor_dip_watch_enabled", True)):
        logger.info("dip-watch disabled")
        return 0
    if not ignore_market_hours and not market_is_open():
        logger.info("dip-watch skipped (market closed: weekend, US holiday, or outside hours)")
        return 0

    candidates = _core_watchlist_names(cfg)
    if not candidates:
        logger.info("dip-watch: no core watchlist names to scan")
        return 0

    held = _held_tickers(cfg)
    ma_window = int(_f(cfg, "portfolio_advisor_dip_watch_ma_window", 50))
    now_iso = datetime.now(timezone.utc).isoformat()

    st = pa_state.load_state(cfg)
    latch = _latch(st)
    newly: List[Dict[str, Any]] = []
    in_zone_now: List[str] = []

    for c in candidates:
        tk = c["ticker"]
        if tk in held:
            latch.pop(tk, None)  # we own it now; stop watching for an entry
            continue
        sig = price_util.dip_signal_yfinance(
            tk,
            ma_window=ma_window,
            high_window_days=int(_f(cfg, "portfolio_advisor_dip_watch_high_window_days", 30)),
        )
        if not sig:
            continue
        verdict, reason = classify_dip(sig, cfg)
        prev = latch.get(tk) if isinstance(latch.get(tk), dict) else None

        if verdict == "buy_zone":
            in_zone_now.append(tk)
            was_in_zone = bool(prev and prev.get("in_zone"))
            rsi_v = sig.get("rsi")
            row = {
                "ticker": tk,
                "in_zone": True,
                "first_seen": (prev or {}).get("first_seen") or now_iso,
                "last_seen": now_iso,
                "last_fired_iso": (prev or {}).get("last_fired_iso"),
                "price": round(float(sig["price"]), 4),
                "below_ma_pct": round(float(sig["below_ma_pct"]), 2),
                "off_high_pct": round(float(sig["off_high_pct"]), 2),
                "rsi": (round(float(rsi_v), 1) if rsi_v is not None else None),
                "reason": reason,
                "thesis": c.get("thesis", ""),
                # carry a still-pending handoff forward (e.g. PM was down last scan)
                "handoff_pending": bool(prev and prev.get("handoff_pending")),
            }
            if not was_in_zone:
                row["handoff_pending"] = True   # fresh entry into the zone
                row["last_fired_iso"] = now_iso
            latch[tk] = row
            if row["handoff_pending"]:
                newly.append(row)
        else:
            # recovered above trend, or fell into knife territory → re-arm for next dip
            latch.pop(tk, None)

    if not newly:
        pa_state.save_state(cfg, st)
        logger.info("dip-watch: %d in zone, no new handoffs", len(in_zone_now))
        return 0

    handed_off = False
    if not suppress_pm_handoff and bool(cfg.get("portfolio_advisor_pm_enabled", True)):
        extra = _format_dip_handoff(newly)
        try:
            from tradingagents.portfolio_advisor.advisor_pm import run_pm_cycle

            run_pm_cycle(cfg, trigger="dip_watch_triggered", extra_context=extra)
            handed_off = True
        except Exception:
            logger.exception("dip-watch: PM hand-off failed (will retry next scan)")

    if handed_off:
        for row in newly:
            r = latch.get(row["ticker"])
            if isinstance(r, dict):
                r["handoff_pending"] = False
                r["last_pm_handoff_iso"] = now_iso

    pa_state.save_state(cfg, st)
    logger.info("dip-watch: %d new dip(s), pm_handoff=%s", len(newly), handed_off)
    return len(newly)
