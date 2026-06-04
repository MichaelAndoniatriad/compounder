"""News-driven Episodic Pivot candidate discovery.

The EP strategy (v2, AI Advisory Edition) Section 9.1 specifies a pre-market
scan for catalyst-driven gappers. This module is the *pre-filter*: it pulls
recent news, surfaces tickers with catalyst-relevant headlines, computes the
Section 3 (universe) / Section 5.1 (gap) / Section 10 (disqualifiers) gates,
and returns a structured candidate list.

The LLM (PM cycle with the EP doc loaded) does Section 4 Tier 1/2/Disqualified
classification on the survivors — this module deliberately stays deterministic
so the gates are debuggable.

Entry recommendations are issued post-close per Section 9.3, not intraday.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Section 3 catalyst-keyword hints. The LLM does final classification; these
# only decide which news items are worth fetching ticker-level data for.
_TIER1_HINTS = re.compile(
    r"\b(fda approval|fda approves|phase 3|positive (top-?line|topline)|"
    r"raises (full[- ]year )?guidance|guidance raise|guidance hike|"
    r"beats (eps|revenue|earnings)|tops estimates|exceeds estimates|"
    r"contract (win|award)|landmark deal|strategic partnership|anchor partner|"
    r"activist (stake|campaign)|13d filed|tender offer|to acquire|acquisition of|"
    r"announces acquisition|buyout offer|deal to buy|all[- ]cash deal)\b",
    re.IGNORECASE,
)
_TIER2_HINTS = re.compile(
    r"\b(beats earnings|earnings beat|tops estimates|"
    r"raised to (overweight|buy)|upgraded to (overweight|buy)|"
    r"price target (raised|hiked|increased)|insider buying|insiders bought|"
    r"sector tailwind|cluster buy)\b",
    re.IGNORECASE,
)
_DISQ_HINTS = re.compile(
    r"\b(stock split|share split|buyback|dividend (raise|hike|increase)|"
    r"crypto pivot|blockchain pivot|reverse split|going concern|"
    r"sec investigation|department of justice|fraud|class action|"
    r"short squeeze|meme rally|social[- ]media frenzy)\b",
    re.IGNORECASE,
)

# Section 2 minimum price.
_MIN_PRICE = 5.0
# Section 4.1 gap threshold.
_MIN_GAP_PCT = 10.0
# Section 9: extended run check window + threshold.
_EXTENDED_LOOKBACK_DAYS = 10
_EXTENDED_THRESHOLD_PCT = 50.0


def _classify_hint(text: str) -> Optional[str]:
    """Return 'tier1' | 'tier2' | 'disq' | None for a headline+summary blob."""
    if _DISQ_HINTS.search(text):
        return "disq"
    if _TIER1_HINTS.search(text):
        return "tier1"
    if _TIER2_HINTS.search(text):
        return "tier2"
    return None


def _parse_av_time(ts: str) -> Optional[datetime]:
    """Alpha Vantage uses YYYYMMDDTHHMMSS."""
    try:
        return datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _pull_news(today: date, look_back_days: int = 2, limit: int = 200) -> List[Dict[str, Any]]:
    """Pull recent global news from Alpha Vantage. Returns parsed feed list."""
    try:
        from tradingagents.dataflows.alpha_vantage_news import get_global_news
        raw = get_global_news(today.isoformat(), look_back_days=look_back_days, limit=limit)
        d = json.loads(raw) if isinstance(raw, str) else raw
        feed = d.get("feed", []) if isinstance(d, dict) else []
        return list(feed)
    except Exception as e:
        logger.warning("Alpha Vantage news fetch failed: %s", e)
        return []


def _bucket_by_ticker(feed: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Surface one record per ticker: best hint tier + most-relevant headline."""
    out: Dict[str, Dict[str, Any]] = {}
    tier_rank = {"tier1": 3, "tier2": 2, None: 1, "disq": 0}
    for item in feed:
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "")
        blob = f"{title} {summary}"
        hint = _classify_hint(blob)
        if hint == "disq" or hint is None:
            continue
        tickers = item.get("ticker_sentiment") or []
        for ts in tickers:
            tk = str(ts.get("ticker") or "").strip().upper()
            if not tk or "." in tk or ":" in tk:  # skip foreign listings
                continue
            try:
                rel = float(ts.get("relevance_score") or 0.0)
            except (ValueError, TypeError):
                rel = 0.0
            if rel < 0.3:
                continue
            prev = out.get(tk)
            if (
                prev is None
                or tier_rank[hint] > tier_rank.get(prev.get("hint"), 0)
                or (tier_rank[hint] == tier_rank.get(prev.get("hint"), 0) and rel > prev["relevance"])
            ):
                out[tk] = {
                    "ticker": tk,
                    "hint": hint,
                    "relevance": rel,
                    "ticker_sentiment": str(ts.get("ticker_sentiment_label") or ""),
                    "title": title[:300],
                    "summary": summary[:600],
                    "source": str(item.get("source") or ""),
                    "url": str(item.get("url") or ""),
                    "time_published": str(item.get("time_published") or ""),
                }
    return out


def _yf_quote(ticker: str) -> Optional[Dict[str, Any]]:
    """Pull current and prior-close price via yfinance. None on failure."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="1mo", interval="1d", auto_adjust=False, prepost=False)
        if hist.empty or len(hist) < 2:
            return None
        last = hist.iloc[-1]
        prev = hist.iloc[-2]
        return {
            "open": float(last["Open"]),
            "close": float(last["Close"]),
            "prev_close": float(prev["Close"]),
            "high_1mo": float(hist["High"].max()),
            "close_10d_ago": float(hist["Close"].iloc[-min(11, len(hist))]),
            "above_50dma": (
                float(last["Close"]) >= float(hist["Close"].tail(50).mean())
                if len(hist) >= 50 else None
            ),
        }
    except Exception as e:
        logger.debug("yf_quote(%s) failed: %s", ticker, e)
        return None


def _market_disqualifiers() -> Dict[str, Any]:
    """Section 9 market-wide checks: SPY day change, VIX level."""
    out: Dict[str, Any] = {"spy_pct": None, "vix": None, "blocked": False, "reason": ""}
    try:
        import yfinance as yf
        spy = yf.Ticker("SPY").history(period="5d", interval="1d", auto_adjust=False)
        if len(spy) >= 2:
            out["spy_pct"] = (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[-2]) - 1) * 100.0
    except Exception as e:
        logger.debug("SPY fetch failed: %s", e)
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX").history(period="5d", interval="1d", auto_adjust=False)
        if len(vix) >= 1:
            out["vix"] = float(vix["Close"].iloc[-1])
    except Exception as e:
        logger.debug("VIX fetch failed: %s", e)
    if out["spy_pct"] is not None and out["spy_pct"] < -2.0:
        out["blocked"] = True
        out["reason"] = f"SPY down {out['spy_pct']:.1f}% (Section 9)"
    elif out["vix"] is not None and out["vix"] > 35.0:
        out["blocked"] = True
        out["reason"] = f"VIX at {out['vix']:.1f} > 35 (Section 9)"
    return out


def scan_for_ep_candidates(
    cfg: Dict[str, Any],
    *,
    look_back_days: int = 2,
    news_limit: int = 200,
    max_candidates: int = 25,
    post_close: bool = False,
) -> Dict[str, Any]:
    """Run the full pre-filter pipeline. Returns a dict suitable for PM context.

    When ``post_close`` is True, an additional gate requires that the
    close held above the 10% gap level (Section 5.2) — used by the
    post-close recommendation scan at 16:15 ET.

    Output:
        {
          "scanned_at": iso,
          "news_items": int,
          "ticker_hits": int,
          "market": {"spy_pct": ..., "vix": ..., "blocked": bool, "reason": str},
          "candidates": [
            {ticker, hint, gap_pct, prior_close, open, last_price, news_title, ...}, ...
          ],
          "skipped": [{ticker, reason}, ...],
        }
    """
    today = date.today()
    feed = _pull_news(today, look_back_days=look_back_days, limit=news_limit)
    hits = _bucket_by_ticker(feed)
    market = _market_disqualifiers()

    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    holdings: set = set()
    try:
        from tradingagents.portfolio_advisor import etoro_scan
        _p, _t, tickers, _r = etoro_scan.fetch_portfolio_rows()
        holdings = etoro_scan.current_ticker_set(tickers)
    except Exception:
        pass

    # Sort by hint tier first, then relevance descending.
    tier_rank = {"tier1": 0, "tier2": 1}
    ranked = sorted(
        hits.values(),
        key=lambda h: (tier_rank.get(h["hint"], 9), -h["relevance"]),
    )

    for h in ranked[: max_candidates * 3]:  # over-pull for filter rejections
        tk = h["ticker"]
        if tk in holdings:
            skipped.append({"ticker": tk, "reason": "already a current holding"})
            continue
        q = _yf_quote(tk)
        if not q:
            skipped.append({"ticker": tk, "reason": "no price data"})
            continue
        if q["close"] < _MIN_PRICE:
            skipped.append({"ticker": tk, "reason": f"price ${q['close']:.2f} < ${_MIN_PRICE}"})
            continue
        gap_pct = (q["open"] / q["prev_close"] - 1.0) * 100.0 if q["prev_close"] else 0.0
        # Catalyst-day gap OR a recent strong move that may have started yesterday.
        recent_move_pct = (q["close"] / q["prev_close"] - 1.0) * 100.0 if q["prev_close"] else 0.0
        effective_gap = max(gap_pct, recent_move_pct)
        if effective_gap < _MIN_GAP_PCT:
            skipped.append({
                "ticker": tk,
                "reason": f"gap {gap_pct:.1f}% / move {recent_move_pct:.1f}% < {_MIN_GAP_PCT}% (Section 4.1)",
            })
            continue
        # Section 10 extended-run check.
        if q.get("close_10d_ago"):
            run10 = (q["close"] / q["close_10d_ago"] - 1.0) * 100.0
            if run10 > _EXTENDED_THRESHOLD_PCT:
                skipped.append({
                    "ticker": tk,
                    "reason": f"up {run10:.0f}% in 10 sessions > {_EXTENDED_THRESHOLD_PCT}% (Section 10 extended)",
                })
                continue
        # Post-close gate: close must hold above the 10% gap level (Section 5.2).
        if post_close and recent_move_pct < _MIN_GAP_PCT:
            skipped.append({
                "ticker": tk,
                "reason": f"close {recent_move_pct:.1f}% < {_MIN_GAP_PCT}% gap — gap did not hold through close (Section 5.2)",
            })
            continue
        candidates.append({
            "ticker": tk,
            "hint": h["hint"],
            "news_title": h["title"],
            "news_summary": h["summary"],
            "news_source": h["source"],
            "news_sentiment": h["ticker_sentiment"],
            "news_relevance": round(h["relevance"], 2),
            "prior_close": round(q["prev_close"], 2),
            "open": round(q["open"], 2),
            "last_price": round(q["close"], 2),
            "gap_pct": round(gap_pct, 2),
            "today_move_pct": round(recent_move_pct, 2),
            "above_50dma": q.get("above_50dma"),
        })
        if len(candidates) >= max_candidates:
            break

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scan_mode": "post_close" if post_close else "pre_market",
        "news_items": len(feed),
        "ticker_hits": len(hits),
        "market": market,
        "candidates": candidates,
        "skipped": skipped,
    }


def format_scan_for_pm(scan: Dict[str, Any]) -> str:
    """Render the scan result as plain text for the PM's extra_context block."""
    lines: List[str] = []
    m = scan.get("market") or {}
    mode = scan.get("scan_mode", "pre_market")
    mode_label = "POST-CLOSE (gap-hold verified per Section 5.2)" if mode == "post_close" else "PRE-MARKET (classify only, do NOT emit recommendations)"
    lines.append(
        f"EP catalyst scan [{mode_label}] ({scan.get('scanned_at','')[:16]}): {scan.get('news_items',0)} news items, "
        f"{scan.get('ticker_hits',0)} ticker hits."
    )
    spy = m.get("spy_pct"); vix = m.get("vix")
    spy_s = f"{spy:+.2f}%" if spy is not None else "?"
    vix_s = f"{vix:.1f}" if vix is not None else "?"
    lines.append(f"Market: SPY today {spy_s}, VIX {vix_s}.")
    if m.get("blocked"):
        lines.append(f"** Section 10 MARKET BLOCK: {m.get('reason')} — skip all entries.**")
    cands = scan.get("candidates") or []
    if not cands:
        lines.append("No candidates passed the pre-filter (Section 3 + 5.1 + 10 extended-run check).")
    else:
        lines.append("")
        lines.append(f"Filtered candidates (you must classify each per Section 4 Tier 1/2/Disqualified):")
        for c in cands:
            dma = "above" if c.get("above_50dma") is True else ("below" if c.get("above_50dma") is False else "?")
            lines.append(
                f"- {c['ticker']} [hint={c['hint']}] gap {c['gap_pct']:+.1f}% / "
                f"close {c['today_move_pct']:+.1f}% (open ${c['open']:.2f}, close ${c['last_price']:.2f}, "
                f"prior ${c['prior_close']:.2f}); 50DMA: {dma}; news sentiment: {c['news_sentiment']}"
            )
            lines.append(f"    NEWS [{c['news_source']}]: {c['news_title']}")
            if c.get("news_summary"):
                lines.append(f"      summary: {c['news_summary'][:300]}")
    skipped = scan.get("skipped") or []
    if skipped:
        lines.append("")
        lines.append("Skipped (and why):")
        for s in skipped[:15]:
            lines.append(f"  - {s['ticker']}: {s['reason']}")
    lines.append("")
    lines.append(
        "For each candidate: apply Section 4 (Tier 1/2/Disqualified) using the news + sentiment + your "
        "own knowledge of what the catalyst means. For each that you classify as Tier 1 or Tier 2 "
        "AND that meets Section 5 (gap >= 10%% held through close, trend context, no upcoming earnings <10 "
        "sessions out — check via your tools), CALL `emit_ep_candidate(ticker, tier, catalyst, "
        "entry_price, stop_price)`. Entry recommendation is for the next session open, not intraday. "
        "Disqualified or weak setups: explain WHY in your summary. Do not "
        "emit a candidate that fails any Section 5 condition or any Section 10 disqualifier."
    )
    return "\n".join(lines)
