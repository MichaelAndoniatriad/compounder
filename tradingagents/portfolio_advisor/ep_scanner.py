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
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Section 3 catalyst-keyword hints. The LLM does final classification; these
# only decide which news items are worth fetching ticker-level data for.
_TIER1_HINTS = re.compile(
    # Strict catalyst phrasing (the original wire-service vocabulary)
    r"\b(fda approval|fda approves|fda grants|fda clears|"
    r"phase 3|phase 2/3|positive (top-?line|topline)|trial (success|met endpoint)|"
    r"raises (full[- ]year )?guidance|guidance raise|guidance hike|guidance lift|"
    r"raises (outlook|forecast)|upbeat (outlook|forecast)|"
    r"beats (eps|revenue|earnings)|tops estimates|exceeds estimates|"
    r"record (revenue|quarter|earnings)|blowout (quarter|results)|"
    r"contract (win|award)|wins (major|big|landmark) (contract|deal)|"
    r"secures (major|big)? ?contract|secures (deal|partnership)|"
    r"landmark deal|strategic partnership|anchor partner|"
    r"activist (stake|campaign)|13d filed|tender offer|to acquire|acquisition of|"
    r"announces acquisition|buyout offer|deal to buy|all[- ]cash deal|"
    # Looser phrasings that wire services and aggregators actually use.
    # Patterns tightened to avoid common false positives like
    # "reports profit warning" (would have matched a bare "reports profit").
    r"reports strong quarter(ly)?|delivers? strong quarter(ly)?|"
    r"surges? on (results|earnings|beat)|jumps? on (earnings|guidance|beat)|"
    r"reports (strong|record|stronger[- ]than[- ]expected) (results|revenue|earnings|quarter)|"
    r"approval (granted|received)|regulatory approval|"
    r"reports record profit|profit (surges?|jumps?)|swings? to profit)\b",
    re.IGNORECASE,
)
_TIER2_HINTS = re.compile(
    r"\b(beats earnings|earnings beat|tops estimates|"
    r"raised to (overweight|buy|outperform)|upgraded to (overweight|buy|outperform)|"
    r"price target (raised|hiked|increased|lifted)|target (raised|hiked|lifted)|"
    r"insider buying|insiders bought|"
    r"sector tailwind|cluster buy|"
    # Broader Tier 2 phrasings
    r"better[- ]than[- ]expected (results|revenue|earnings)|"
    r"narrow(er|s) loss|reduces? loss|"
    r"raises (price target|estimate)|estimates (raised|hiked)|"
    r"reports (q[1-4]|first|second|third|fourth) quarter|"
    r"announces? (record|strong) (results|growth)|"
    r"orders surge|demand (jumps?|surges?|accelerates?))\b",
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


def _pull_news_alpha_vantage(today: date, look_back_days: int, limit: int) -> List[Dict[str, Any]]:
    """Pull from Alpha Vantage. Returns parsed feed list. Empty on failure."""
    try:
        from tradingagents.dataflows.alpha_vantage_news import get_global_news
        raw = get_global_news(today.isoformat(), look_back_days=look_back_days, limit=limit)
        d = json.loads(raw) if isinstance(raw, str) else raw
        feed = d.get("feed", []) if isinstance(d, dict) else []
        return list(feed)
    except Exception as e:
        logger.warning("Alpha Vantage news fetch failed: %s", e)
        return []


def _pull_news_yahoo_rss(today: date, look_back_days: int, limit: int) -> List[Dict[str, Any]]:
    """Pull from Yahoo Finance RSS. Returns parsed feed list. Empty on failure."""
    try:
        from tradingagents.dataflows.yahoo_news_rss import get_market_news
        return get_market_news(today, look_back_days=look_back_days, limit=limit)
    except Exception as e:
        logger.warning("Yahoo Finance RSS fetch failed: %s", e)
        return []


def _pull_news_edgar(today: date, look_back_days: int, limit: int) -> List[Dict[str, Any]]:
    """Pull recent 8-K filings from SEC EDGAR. Returns a feed-compatible list.

    Each 8-K item is normalised to the same dict shape as Alpha Vantage / Yahoo
    RSS feed items so it flows through the existing _bucket_by_ticker / hint-regex
    pipeline unchanged. Title is "8-K item <item>: <filing title>" so the
    Tier1/Tier2 hint regexes can match on their normal vocabulary if the filing
    title includes catalyst keywords.

    Empty list on any failure (EDGAR is optional enrichment, not a hard dep).
    """
    try:
        from tradingagents.dataflows.edgar import fetch_recent_8k_items
        hours = look_back_days * 24
        filings = fetch_recent_8k_items(hours=hours)
        out: List[Dict[str, Any]] = []
        for f in filings[:limit]:
            ticker = f.get("ticker") or ""
            if not ticker:
                continue
            title = f.get("title") or ""
            item = f.get("item") or ""
            headline = f"8-K item {item}: {title}"
            out.append({
                "title": headline,
                "summary": "",
                "source": "edgar_8k",
                "url": f.get("url") or "",
                "time_published": f.get("filed_at") or "",
                # Build a synthetic ticker_sentiment list so _bucket_by_ticker
                # can pick it up; relevance_score 1.0 (direct filing = relevant).
                "ticker_sentiment": [
                    {
                        "ticker": ticker,
                        "relevance_score": "1.0",
                        "ticker_sentiment_label": "Neutral",
                    }
                ],
            })
        return out
    except Exception as e:
        logger.warning("EDGAR 8-K fetch failed: %s", e)
        return []


# ep_scanner_news_sources registry. Default list in default_config.py uses
# ["alpha_vantage", "yahoo_finance_rss"]. "edgar" is intentionally NOT in
# the default list — enable by adding "edgar" to ep_scanner_news_sources in
# your config once the EDGAR module has been validated in production.
_NEWS_SOURCE_REGISTRY = {
    "alpha_vantage": _pull_news_alpha_vantage,
    "yahoo_finance_rss": _pull_news_yahoo_rss,
    "edgar": _pull_news_edgar,
}


def _pull_news(
    today: date,
    look_back_days: int = 2,
    limit: int = 200,
    sources: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Pull news from one or more configured sources and dedupe.

    Sources are resolved via the _NEWS_SOURCE_REGISTRY. Unknown source names
    are skipped with a warning. Items are deduped by URL; if URLs are missing,
    falls back to (source, title) dedupe. Default is Alpha Vantage only,
    preserving previous behaviour for any caller that does not pass `sources`.
    """
    if sources is None:
        sources = ["alpha_vantage"]

    combined: List[Dict[str, Any]] = []
    for src in sources:
        fetcher = _NEWS_SOURCE_REGISTRY.get(src)
        if fetcher is None:
            logger.warning("Unknown EP scanner news source: %s", src)
            continue
        feed = fetcher(today, look_back_days, limit)
        combined.extend(feed)

    # Dedupe by URL (preferred) or by (source, title) fallback.
    seen_urls: set = set()
    seen_keys: set = set()
    deduped: List[Dict[str, Any]] = []
    for item in combined:
        url = (item.get("url") or "").strip()
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            deduped.append(item)
            continue
        key = (item.get("source", ""), item.get("title", ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(item)

    return deduped[:limit]


def _bucket_by_ticker(feed: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Surface one record per ticker: best hint tier + most-relevant headline.

    Returns (bucketed, hint_summary) where hint_summary counts news items by
    classification: disq, no_hint, foreign_ticker_skip, low_relevance_skip, bucketed.
    """
    out: Dict[str, Dict[str, Any]] = {}
    tier_rank = {"tier1": 3, "tier2": 2, None: 1, "disq": 0}
    hint_summary: Dict[str, int] = {"disq": 0, "no_hint": 0, "foreign": 0, "low_rel": 0, "bucketed_events": 0}
    for item in feed:
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "")
        blob = f"{title} {summary}"
        hint = _classify_hint(blob)
        if hint == "disq":
            hint_summary["disq"] += 1
            continue
        if hint is None:
            hint_summary["no_hint"] += 1
            continue
        tickers = item.get("ticker_sentiment") or []
        for ts in tickers:
            tk = str(ts.get("ticker") or "").strip().upper()
            if not tk or "." in tk or ":" in tk:  # skip foreign listings
                hint_summary["foreign"] += 1
                continue
            try:
                rel = float(ts.get("relevance_score") or 0.0)
            except (ValueError, TypeError):
                rel = 0.0
            if rel < 0.3:
                hint_summary["low_rel"] += 1
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
                hint_summary["bucketed_events"] += 1
    return out, hint_summary


def _yf_quote(ticker: str) -> Optional[Dict[str, Any]]:
    """Pull current and prior-close price + volume data via yfinance. None on failure.

    Returns a dict with price fields plus:
      - volume_today: today's session volume (shares)
      - avg_volume_20d: 20-day average daily volume (shares)
      - close_price_for_dollar_vol: close price used to compute dollar volume
    These are consumed by the RVOL gate.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="1mo", interval="1d", auto_adjust=False, prepost=False)
        if hist.empty or len(hist) < 2:
            return None
        last = hist.iloc[-1]
        prev = hist.iloc[-2]
        # Volume: use last 20 sessions for average (up to 20 available)
        vol_series = hist["Volume"].dropna()
        vol_today = float(last["Volume"]) if "Volume" in hist.columns else 0.0
        avg_vol_20d = float(vol_series.tail(20).mean()) if len(vol_series) >= 2 else 0.0
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
            "volume_today": vol_today,
            "avg_volume_20d": avg_vol_20d,
        }
    except Exception as e:
        logger.debug("yf_quote(%s) failed: %s", ticker, e)
        return None


def _alpaca_live_price(ticker: str) -> Optional[float]:
    """Fetch a live price for pre-market use via Alpaca IEX free feed.

    Tries latest trade first (last sale price); falls back to mid of latest
    quote bid/ask. Returns None on any failure (no keys, pytest, network).
    This is intentionally thin — callers treat None as "no live data".
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest, StockLatestQuoteRequest

        key = (os.environ.get("ALPACA_API_KEY") or "").strip()
        sec = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
        if not key or not sec:
            return None
        client = StockHistoricalDataClient(key, sec)
        # Try latest trade first
        try:
            resp = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
            trade = resp.get(ticker) if isinstance(resp, dict) else None
            if trade is not None:
                price = float(getattr(trade, "price", 0) or 0)
                if price > 0:
                    return price
        except Exception:
            pass
        # Fall back to mid of latest quote
        try:
            resp = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=ticker))
            quote = resp.get(ticker) if isinstance(resp, dict) else None
            if quote is not None:
                bid = float(getattr(quote, "bid_price", 0) or 0)
                ask = float(getattr(quote, "ask_price", 0) or 0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0
        except Exception:
            pass
        return None
    except Exception:
        logger.debug("_alpaca_live_price(%s) failed", ticker, exc_info=True)
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


def _is_pre_market_window() -> bool:
    """Return True when the US market is closed AND we are before next open (pre-market window).

    Uses the Alpaca market clock. Returns False on any error or when under
    pytest (so regular-hours test paths are unaffected).
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    try:
        from tradingagents.integrations.alpaca.executor import market_clock
        clk = market_clock()
        if clk is None:
            return False
        if clk.get("is_open"):
            return False
        # Market is closed. We are "pre-market" if current UTC time is before next_open.
        next_open_str = clk.get("next_open") or ""
        if not next_open_str:
            return False
        # next_open is an ISO string; parse it.
        try:
            if next_open_str.endswith("Z"):
                next_open_str = next_open_str[:-1] + "+00:00"
            next_open_dt = datetime.fromisoformat(next_open_str)
            if next_open_dt.tzinfo is None:
                next_open_dt = next_open_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return False
        now_utc = datetime.now(timezone.utc)
        return now_utc < next_open_dt
    except Exception:
        return False


# RVOL gate thresholds (Section 4.2 / roadmap R4).
_RVOL_MIN_MULTIPLE = 2.0     # day volume must be >= 2x 20d average
_RVOL_MIN_DOLLAR_VOL = 5_000_000.0  # day dollar volume must be >= $5M


def scan_for_ep_candidates(
    cfg: Dict[str, Any],
    *,
    look_back_days: int = 2,
    news_limit: int = 200,
    max_candidates: int = 25,
    post_close: bool = False,
    _market_clock_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the full pre-filter pipeline. Returns a dict suitable for PM context.

    When ``post_close`` is True, an additional gate requires that the
    close held above the 10% gap level (Section 5.2) — used by the
    post-close recommendation scan at 16:15 ET.

    Pre-market honesty: when called during the pre-market window (market closed,
    before next open), prices come from the Alpaca IEX latest trade/quote rather
    than yfinance daily bars (which would show yesterday's stale close). If no
    live Alpaca price is available for a ticker during pre-market, that ticker
    is skipped. If Alpaca is entirely unreachable the scan is aborted early with
    a single log line so the caller does not act on stale data.

    ``_market_clock_override`` is a test seam: pass a dict matching the
    market_clock() return shape to inject a synthetic clock state.

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
    # --- Pre-market honesty check ---
    # Determine whether we are in the pre-market window (closed, before next open).
    in_pre_market = False
    if _market_clock_override is not None:
        clk = _market_clock_override
        if not clk.get("is_open", True):
            next_open_str = clk.get("next_open") or ""
            if next_open_str:
                try:
                    if next_open_str.endswith("Z"):
                        next_open_str = next_open_str[:-1] + "+00:00"
                    next_open_dt = datetime.fromisoformat(next_open_str)
                    if next_open_dt.tzinfo is None:
                        next_open_dt = next_open_dt.replace(tzinfo=timezone.utc)
                    in_pre_market = datetime.now(timezone.utc) < next_open_dt
                except (ValueError, TypeError):
                    pass
    else:
        in_pre_market = _is_pre_market_window()

    # Pre-market scan abort: if we are in pre-market but Alpaca keys are absent
    # (meaning _alpaca_live_price will always return None), skip the whole scan
    # rather than silently scanning on stale yfinance daily closes.
    if in_pre_market and "PYTEST_CURRENT_TEST" not in os.environ:
        has_alpaca_keys = bool(
            (os.environ.get("ALPACA_API_KEY") or "").strip()
            and (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
        )
        if not has_alpaca_keys:
            logger.info(
                "pre-market scan skipped: no live quote source (ALPACA_API_KEY / ALPACA_SECRET_KEY absent)"
            )
            return {
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "scan_mode": "pre_market",
                "skipped_reason": "pre-market scan skipped: no live quote source",
                "news_items": 0,
                "ticker_hits": 0,
                "hint_summary": {},
                "market": {"spy_pct": None, "vix": None, "blocked": False, "reason": ""},
                "candidates": [],
                "skipped": [],
                "gate_log": [],
            }

    today = date.today()
    news_sources = cfg.get("ep_scanner_news_sources") or ["alpha_vantage"]
    if isinstance(news_sources, str):
        news_sources = [news_sources]
    feed = _pull_news(today, look_back_days=look_back_days, limit=news_limit, sources=news_sources)
    hits, hint_summary = _bucket_by_ticker(feed)
    market = _market_disqualifiers()

    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    # Per-candidate gate audit trail for diagnostic use.
    gate_log: List[Dict[str, Any]] = []
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
        gate = {"t": tk, "hint": h["hint"], "title": h["title"][:120],
                "relevance": h["relevance"]}
        # Gate 1: already holding
        if tk in holdings:
            gate["failed_at"] = "holding"
            gate_log.append(gate)
            skipped.append({"ticker": tk, "reason": "already a current holding"})
            continue
        # Gate 2: price data
        q = _yf_quote(tk)
        if not q:
            gate["failed_at"] = "no_price_data"
            gate_log.append(gate)
            skipped.append({"ticker": tk, "reason": "no price data"})
            continue

        # Pre-market honesty: replace yfinance close (stale yesterday's close) with
        # Alpaca live price when the market is closed and we are before next open.
        # If the live price is unavailable, skip this ticker rather than scanning
        # on stale data that would produce misleading gap readings.
        if in_pre_market:
            live_px = _alpaca_live_price(tk)
            if live_px is None:
                gate["failed_at"] = "pre_market_no_live_price"
                gate_log.append(gate)
                skipped.append({
                    "ticker": tk,
                    "reason": "pre-market: no live Alpaca price available; skipping stale yfinance close",
                })
                continue
            # Use live price as the "close" and "open" approximation for gap computation.
            q = dict(q)
            q["close"] = live_px
            q["open"] = live_px
            gate["pre_market_live_px"] = round(live_px, 2)

        gate["close"] = round(q["close"], 2)
        gate["open"] = round(q["open"], 2)
        gate["prev_close"] = round(q["prev_close"], 2)
        # Gate 3: min price
        if q["close"] < _MIN_PRICE:
            gate["failed_at"] = "min_price"
            gate["threshold"] = _MIN_PRICE
            gate_log.append(gate)
            skipped.append({"ticker": tk, "reason": f"price ${q['close']:.2f} < ${_MIN_PRICE}"})
            continue
        gap_pct = (q["open"] / q["prev_close"] - 1.0) * 100.0 if q["prev_close"] else 0.0
        # Catalyst-day gap OR a recent strong move that may have started yesterday.
        recent_move_pct = (q["close"] / q["prev_close"] - 1.0) * 100.0 if q["prev_close"] else 0.0
        effective_gap = max(gap_pct, recent_move_pct)
        gate["gap_pct"] = round(gap_pct, 2)
        gate["move_pct"] = round(recent_move_pct, 2)
        # Gate 4: gap threshold
        if effective_gap < _MIN_GAP_PCT:
            gate["failed_at"] = "gap"
            gate["threshold"] = _MIN_GAP_PCT
            gate["effective_gap"] = round(effective_gap, 2)
            gate_log.append(gate)
            skipped.append({
                "ticker": tk,
                "reason": f"gap {gap_pct:.1f}% / move {recent_move_pct:.1f}% < {_MIN_GAP_PCT}% (Section 4.1)",
            })
            continue
        # Gate 4.5: RVOL gate (Section 4.2 / EP SOP volume condition).
        # Day volume must be >= 2x 20d average AND day dollar volume >= $5M.
        # Confirms institutional participation, not retail noise.
        # Evaluated here (same location as gap gate) with full pass/fail logging.
        vol_today = float(q.get("volume_today") or 0.0)
        avg_vol_20d = float(q.get("avg_volume_20d") or 0.0)
        rvol = (vol_today / avg_vol_20d) if avg_vol_20d > 0 else 0.0
        # Dollar volume = today's volume × close price.
        dollar_vol = vol_today * q["close"]
        gate["rvol"] = round(rvol, 2)
        gate["vol_today"] = int(vol_today)
        gate["avg_vol_20d"] = int(avg_vol_20d)
        gate["dollar_vol"] = round(dollar_vol, 0)
        rvol_pass = (rvol >= _RVOL_MIN_MULTIPLE) and (dollar_vol >= _RVOL_MIN_DOLLAR_VOL)
        if not rvol_pass:
            reason_parts = []
            if rvol < _RVOL_MIN_MULTIPLE:
                reason_parts.append(
                    f"RVOL {rvol:.1f}x < {_RVOL_MIN_MULTIPLE:.0f}x 20d avg "
                    f"(vol {int(vol_today):,} vs avg {int(avg_vol_20d):,})"
                )
            if dollar_vol < _RVOL_MIN_DOLLAR_VOL:
                reason_parts.append(
                    f"dollar vol ${dollar_vol/1e6:.1f}M < ${_RVOL_MIN_DOLLAR_VOL/1e6:.0f}M"
                )
            gate["failed_at"] = "rvol"
            gate["rvol_pass"] = False
            gate_log.append(gate)
            skipped.append({
                "ticker": tk,
                "reason": "RVOL gate fail: " + "; ".join(reason_parts) + " (Section 4.2)",
            })
            continue
        gate["rvol_pass"] = True
        # Gate 5: extended-run check (Section 10).
        if q.get("close_10d_ago"):
            run10 = (q["close"] / q["close_10d_ago"] - 1.0) * 100.0
            gate["run10d_pct"] = round(run10, 2)
            if run10 > _EXTENDED_THRESHOLD_PCT:
                gate["failed_at"] = "extended_run"
                gate["threshold"] = _EXTENDED_THRESHOLD_PCT
                gate_log.append(gate)
                skipped.append({
                    "ticker": tk,
                    "reason": f"up {run10:.0f}% in 10 sessions > {_EXTENDED_THRESHOLD_PCT}% (Section 10 extended)",
                })
                continue
        # Gate 6: post-close hold (Section 5.2).
        if post_close and recent_move_pct < _MIN_GAP_PCT:
            gate["failed_at"] = "post_close_hold"
            gate["threshold"] = _MIN_GAP_PCT
            gate_log.append(gate)
            skipped.append({
                "ticker": tk,
                "reason": f"close {recent_move_pct:.1f}% < {_MIN_GAP_PCT}% gap — gap did not hold through close (Section 5.2)",
            })
            continue
        # Passed all gates.
        gate["passed"] = True
        gate_log.append(gate)
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
            "rvol": round(rvol, 2),
            "dollar_vol_m": round(dollar_vol / 1e6, 2),
        })
        if len(candidates) >= max_candidates:
            break

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scan_mode": "post_close" if post_close else "pre_market",
        "news_items": len(feed),
        "ticker_hits": len(hits),
        "hint_summary": hint_summary,
        "market": market,
        "candidates": candidates,
        "skipped": skipped,
        "gate_log": gate_log,
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
        "entry_price, stop_price, catalyst_date=<ISO YYYY-MM-DD of the catalyst event>)`. "
        "catalyst_date is REQUIRED for autonomous execution — it must be the concrete event date "
        "(e.g. earnings date, FDA decision date, contract announcement date). "
        "Without catalyst_date the trade will be advisory-only even in autonomous mode. "
        "Entry recommendation is for the next session open, not intraday. "
        "Disqualified or weak setups: explain WHY in your summary. Do not "
        "emit a candidate that fails any Section 5 condition or any Section 10 disqualifier."
    )
    return "\n".join(lines)
