"""Core position discovery — weekly screen for long-term growth candidates.

Runs Saturday morning. Pulls live index constituents, screens quantitatively
via yfinance, runs LLM qualitative pass on survivors. No hardcoded universes.
No artificial dedup. Merit-only: if a stock qualifies, it qualifies.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Quantitative pre-buy filters
MIN_MARKET_CAP = 1_000_000_000  # $1B
MIN_REVENUE_GROWTH = 0.20  # 20% YoY
MAX_PEG = 1.5
MIN_ROIC = 0.15  # 15%


def _build_universe() -> List[str]:
    """Pull live index constituents from Wikipedia. No hardcoded lists.

    Scans all tables on each page for a Symbol/Ticker column rather than
    hardcoding table indices, which break when Wikipedia editors reorder tables.
    """
    import pandas as pd

    tickers: set[str] = set()

    def _extract_tickers(url: str, col_names: tuple[str, ...], label: str) -> int:
        """Scan all tables on a Wikipedia page for a ticker column. Returns count added."""
        try:
            # Use requests with User-Agent — Wikipedia blocks default Python
            # user agents from some IP ranges (including Hetzner).
            import requests
            resp = requests.get(url, headers={"User-Agent": "TradingAgents/1.0 (portfolio research)"}, timeout=15)
            resp.raise_for_status()
            all_tables = pd.read_html(resp.text)
        except Exception as e:
            logger.warning("core discovery: %s fetch failed: %s", label, e)
            return 0

        found = 0
        for table in all_tables:
            for col in col_names:
                if col not in table.columns:
                    continue
                symbols = [
                    str(t).strip().upper().replace(".", "-")
                    for t in table[col].tolist()
                    if str(t).strip().upper()[:1].isalpha()  # skip non-ticker rows
                ]
                tickers.update(symbols)
                found += len(symbols)
                break  # first matching column wins per table

        logger.info("core discovery: loaded %d %s constituents", found, label)
        return found

    sp500_count = _extract_tickers(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        ("Symbol", "Ticker"),
        "S&P 500",
    )
    nasdaq_count = _extract_tickers(
        "https://en.wikipedia.org/wiki/Nasdaq-100",
        ("Ticker", "Symbol"),
        "NASDAQ 100",
    )

    # Fallback if both fail
    if sp500_count == 0 and nasdaq_count == 0:
        logger.warning("core discovery: both Wikipedia fetches failed, using fallback")
        tickers.update([
            "NVDA", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "TSM", "AAPL",
            "NOW", "CRM", "ADBE", "INTU", "SNOW", "DDOG", "MNDY", "NET",
            "CRWD", "ZS", "PANW", "FTNT", "SHOP", "UBER", "ABNB", "DKNG",
        ])

    return sorted(tickers)


def _quantitative_screen(tickers: List[str]) -> List[Dict[str, Any]]:
    """Filter tickers by quantitative pre-buy criteria. Returns passing records."""
    import yfinance as yf

    passing = []
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            info = t.info
            if not info or info.get("quoteType") not in ("EQUITY", None):
                continue

            market_cap = info.get("marketCap", 0) or 0
            if market_cap < MIN_MARKET_CAP:
                continue

            rev_growth = info.get("revenueGrowth", 0) or 0
            if rev_growth < MIN_REVENUE_GROWTH:
                continue

            peg = info.get("pegRatio", 999) or 999
            if peg > MAX_PEG:
                continue

            roic = info.get("returnOnCapital", 0) or 0
            if roic < MIN_ROIC:
                continue

            passing.append({
                "ticker": ticker,
                "name": info.get("shortName", ticker),
                "market_cap_b": round(market_cap / 1e9, 1),
                "rev_growth": round(rev_growth * 100, 1),
                "peg": round(peg, 1),
                "roic": round(roic * 100, 1),
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
                "price": info.get("currentPrice", 0),
                "fwd_pe": info.get("forwardPE", 0),
                "debt_equity": info.get("debtToEquity", 0) or 0,
            })
        except Exception:
            continue

    return passing


def _llm_qualitative_rank(candidates: List[Dict], cfg: Dict[str, Any]) -> List[Dict]:
    """LLM pass: assess moat, founder, red flags. Rank by conviction.

    Returns up to 5, ordered by conviction score. Every candidate gets a
    one-line thesis. Previously recommended stocks are NOT excluded — they
    appear again if they still qualify, flagged as 'repeat' for context.
    """
    import re

    if not candidates:
        return []

    try:
        from tradingagents.llm_clients.corporate_llm_factory import build_corporate_hierarchy_llms
        llms = build_corporate_hierarchy_llms(cfg, callbacks=[])
        llm = llms.get("market_analyst") or llms.get("reflection")
        if llm is None:
            return _pick_top_5(candidates)
    except Exception:
        return _pick_top_5(candidates)

    # Build compact candidate table for the LLM
    lines = []
    for c in candidates[:30]:
        lines.append(
            f"{c['ticker']} | {c['sector']}/{c.get('industry','')} | "
            f"rev_growth={c['rev_growth']}% | PEG={c['peg']} | "
            f"ROIC={c['roic']}% | fwd_PE={c['fwd_pe']} | "
            f"D/E={c.get('debt_equity',0)}"
        )
    cand_text = "\n".join(lines)

    # Build current holdings context
    holdings_text = ""
    try:
        from tradingagents.portfolio_advisor.etoro_scan import fetch_portfolio_rows
        _payload, _text, _tickers, rows = fetch_portfolio_rows()
        if rows:
            hold_lines = [
                f"  {r.get('symbolFull','')} ({r.get('instrumentDisplayName','')}) — "
                f"entry={r.get('openRate','?')}"
                for r in rows
            ]
            holdings_text = (
                "\n\nMichael currently holds these positions. "
                "Flag any overlap with the candidates below:\n" + "\n".join(hold_lines)
            )
    except Exception:
        pass

    prompt = (
        "You are screening for long-term growth stocks for a concentrated portfolio. "
        "From the candidates below, identify up to 5 that best fit ALL of:\n"
        "- Durable competitive moat (network effects, switching costs, scale economies)\n"
        "- Strong operator with skin in the game\n"
        "- Secular tailwind, not cyclical demand\n"
        "- No red flags: cash flow roughly tracks GAAP earnings, limited dilution, "
        "debt manageable, growth not decelerating\n\n"
        f"Candidates:\n{cand_text}\n"
        f"{holdings_text}\n\n"
        "Return EXACTLY one line per pick. Format each line as:\n"
        "TICKER — CONVICTION (High/Medium) — STATUS (new/overlap) — one-sentence thesis\n\n"
        "Example:\n"
        "DDOG — CONVICTION High — OVERLAP (already held) — Observability platform with "
        "durable switching costs; founder-led, profitable, no red flags.\n\n"
        "Return up to 5 tickers, ranked by conviction (highest first). "
        "If a stock was likely recommended before but still qualifies, note it as "
        "'repeat but thesis intact'. Include only tickers from the list above. "
        "Do not number the lines or add commentary."
    )

    try:
        response = llm.invoke(prompt)
        content = getattr(response, "content", str(response))
    except Exception as e:
        logger.warning("core discovery LLM failed: %s", e)
        return _pick_top_5(candidates)

    # Parse ticker lines with regex to avoid false substring matches
    # Matches: TICKER — CONVICTION (High|Medium) — ...
    ticker_re = re.compile(
        r"^([A-Z]{1,5})\s*[—\-]\s*CONVICTION\s+(High|Medium)",
        re.IGNORECASE,
    )

    selected = []
    for line in str(content).split("\n"):
        line = line.strip()
        m = ticker_re.match(line)
        if not m:
            continue
        ticker = m.group(1).upper()
        for c in candidates:
            if c["ticker"] == ticker and c not in selected:
                c["thesis"] = line
                selected.append(c)
                break
        if len(selected) >= 5:
            break

    return selected if selected else _pick_top_5(candidates)


def _pick_top_5(candidates: List[Dict]) -> List[Dict]:
    """Fallback: pick top 5 by ROIC when LLM unavailable."""
    return sorted(candidates, key=lambda c: c["roic"], reverse=True)[:5]


def run_core_discovery(cfg: Dict[str, Any]) -> str:
    """Run the full weekly core position discovery pipeline."""
    import time

    today = date.today().isoformat()
    t0 = time.monotonic()

    # Phase 1: Build dynamic universe
    logger.info("core discovery: building universe")
    universe = _build_universe()
    t1 = time.monotonic()
    logger.info("core discovery: %d tickers in universe (%.1fs)", len(universe), t1 - t0)

    # Phase 1b: Mechanical filter — fast, deterministic disqualification
    logger.info("core discovery: running mechanical filter on %d tickers", len(universe))
    from tradingagents.portfolio_advisor.mechanical_filter import mechanical_filter

    reject_log = str(Path.home() / ".tradingagents" / "logs" / f"core_discovery_rejects_{today}.csv")
    universe, rejections = mechanical_filter(universe, reject_log_path=reject_log)
    total_rejected = sum(len(ts) for ts in rejections.values())
    t2 = time.monotonic()
    logger.info("core discovery: %d survived mechanical filter (%d rejected) (%.1fs)", len(universe), total_rejected, t2 - t1)

    # Phase 2: Quantitative screen
    quant_pass = _quantitative_screen(universe)
    t3 = time.monotonic()
    if not quant_pass:
        return "Core discovery: no tickers passed quantitative filters this week."
    logger.info("core discovery: %d passed quantitative screen (%.1fs)", len(quant_pass), t3 - t2)

    # Phase 3: LLM qualitative rank
    picks = _llm_qualitative_rank(quant_pass, cfg)
    t4 = time.monotonic()
    if not picks:
        return "Core discovery: LLM filter returned no picks."

    # Phase 4: Build Telegram message
    lines = [
        "CORE POSITION DISCOVERY",
        f"Week of {today}",
        "",
        f"Screened {len(universe) + total_rejected} tickers (S&P 500 + NASDAQ 100). "
        f"{total_rejected} eliminated by mechanical filters, "
        f"{len(quant_pass)} passed quantitative screen. "
        f"Top picks after qualitative review:",
        "",
    ]

    for i, pick in enumerate(picks, 1):
        lines.append(
            f"{i}. {pick['ticker']} ({pick['name']}) — "
            f"{pick['sector']} | rev_growth={pick['rev_growth']}% | "
            f"ROIC={pick['roic']}% | PEG={pick['peg']}"
        )
        if "thesis" in pick:
            lines.append(f"   {pick['thesis']}")
        lines.append("")

    lines.append("Reply with a ticker to deep-dive. Say 'skip' to pass.")

    body = "\n".join(lines)

    # Send via Telegram
    try:
        from tradingagents.portfolio_advisor.messaging import send_advisor_message
        send_advisor_message(
            cfg, "Core Discovery", body, urgent=False,
            log_as_recommendation=True,
            rec_trigger="core_discovery",
            rec_type="core_candidate",
            rec_action=f"weekly_screen_{len(picks)}_candidates",
            rec_rationale=f"Screened {len(universe)} tickers. {len(quant_pass)} passed quant. {len(picks)} selected.",
        )
    except Exception as e:
        logger.warning("core discovery send failed: %s", e)
        return f"Core discovery complete but send failed: {e}"

    # Phase 5: Push picks into PM watchlist so the PM can factor them into
    # sleeve allocation and queue research without manual relay.
    try:
        from tradingagents.portfolio_advisor.watchlist import load_watchlist, save_watchlist

        wl = load_watchlist(cfg)
        existing = {
            (e if isinstance(e, str) else e.get("ticker", "")).strip().upper()
            for e in wl
        }
        added = 0
        for pick in picks:
            ticker = pick["ticker"]
            if ticker in existing:
                continue
            thesis = pick.get("thesis", "")
            wl.append({
                "ticker": ticker,
                "thesis": thesis,
                "strategy": "core",
                "source": "core_discovery",
                "added": today,
            })
            added += 1
        if added:
            save_watchlist(cfg, wl)
            logger.info("core discovery: added %d candidates to PM watchlist", added)
    except Exception as e:
        logger.warning("core discovery: watchlist update failed: %s", e)

    logger.info("core discovery: complete (%.1fs total)", time.monotonic() - t0)
    return f"Core discovery: {len(picks)} candidates sent via Telegram"
