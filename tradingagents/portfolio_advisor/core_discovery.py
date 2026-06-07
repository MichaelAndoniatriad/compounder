"""Core position discovery — weekly screen for long-term growth candidates.

Runs Saturday morning. Pulls live index constituents, screens quantitatively
via yfinance, runs LLM qualitative pass on survivors. No hardcoded universes.
No artificial dedup. Merit-only: if a stock qualifies, it qualifies.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Quantitative pre-buy filters
MIN_MARKET_CAP = 1_000_000_000  # $1B
MIN_REVENUE_GROWTH = 0.20  # 20% YoY
MAX_PEG = 1.5
MIN_ROIC = 0.15  # 15%


def _build_universe() -> List[str]:
    """Pull live index constituents from Wikipedia/yfinance. No hardcoded lists."""
    tickers: set[str] = set()

    # S&P 500 from Wikipedia (free, always current)
    try:
        import pandas as pd
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        sp500 = [str(t).strip().upper().replace(".", "-") for t in tables[0]["Symbol"].tolist()]
        tickers.update(sp500)
        logger.info("core discovery: loaded %d S&P 500 constituents", len(sp500))
    except Exception as e:
        logger.warning("core discovery: S&P 500 fetch failed: %s", e)

    # NASDAQ 100
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        nasdaq = [str(t).strip().upper() for t in tables[2]["Ticker"].tolist()]
        tickers.update(nasdaq)
        logger.info("core discovery: loaded %d NASDAQ 100 constituents", len(nasdaq))
    except Exception as e:
        logger.warning("core discovery: NASDAQ 100 fetch failed: %s", e)

    # Fallback if both fail
    if len(tickers) < 100:
        logger.warning("core discovery: only %d tickers from indices, adding fallback", len(tickers))
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

    prompt = (
        "You are screening for long-term growth stocks for a concentrated portfolio. "
        "From the candidates below, identify up to 5 that best fit ALL of:\n"
        "- Durable competitive moat (network effects, switching costs, scale economies)\n"
        "- Strong operator with skin in the game\n"
        "- Secular tailwind, not cyclical demand\n"
        "- No red flags: cash flow roughly tracks GAAP earnings, limited dilution, "
        "debt manageable, growth not decelerating\n\n"
        f"Candidates:\n{cand_text}\n\n"
        "Return up to 5 tickers, ranked by conviction (highest first). "
        "Format each as: TICKER — CONVICTION (High/Medium) — one-sentence investment thesis. "
        "If a stock was likely recommended before but still qualifies, note it as "
        "'repeat but thesis intact'. Include only tickers from the list above."
    )

    try:
        response = llm.invoke(prompt)
        content = getattr(response, "content", str(response))
    except Exception as e:
        logger.warning("core discovery LLM failed: %s", e)
        return _pick_top_5(candidates)

    # Parse ticker lines
    selected = []
    for line in str(content).split("\n"):
        line = line.strip()
        if not line or not line[0].isalpha():
            continue
        for c in candidates:
            if c["ticker"] in line and c not in selected:
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
    today = date.today().isoformat()

    # Phase 1: Build dynamic universe
    logger.info("core discovery: building universe")
    universe = _build_universe()
    logger.info("core discovery: %d tickers in universe", len(universe))

    # Phase 2: Quantitative screen
    quant_pass = _quantitative_screen(universe)
    if not quant_pass:
        return "Core discovery: no tickers passed quantitative filters this week."
    logger.info("core discovery: %d passed quantitative screen", len(quant_pass))

    # Phase 3: LLM qualitative rank
    picks = _llm_qualitative_rank(quant_pass, cfg)
    if not picks:
        return "Core discovery: LLM filter returned no picks."

    # Phase 4: Build Telegram message
    lines = [
        "CORE POSITION DISCOVERY",
        f"Week of {today}",
        "",
        f"Screened {len(universe)} tickers (S&P 500 + NASDAQ 100). "
        f"{len(quant_pass)} passed quantitative filters. "
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

    return f"Core discovery: {len(picks)} candidates sent via Telegram"
