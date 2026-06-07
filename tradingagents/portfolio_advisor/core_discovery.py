"""Core position discovery — weekly screen for long-term growth candidates.

Runs Saturday morning. Screens for stocks meeting the pre-buy framework,
runs a quick LLM pass on survivors, and sends Telegram recommendations.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Pre-buy quantitative filters
MIN_MARKET_CAP = 1_000_000_000  # $1B
MIN_REVENUE_GROWTH = 0.20  # 20% YoY
MAX_PEG = 1.5
MIN_ROIC = 0.15  # 15%

# Seed universe: combine common growth screens. Expanded weekly.
_BASE_UNIVERSE = [
    "NVDA", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "TSM", "AAPL", "PLTR", "AMD",
    "NOW", "CRM", "ADBE", "INTU", "SNOW", "DDOG", "MNDY", "NET", "CRWD", "ZS",
    "PANW", "FTNT", "SQ", "PYPL", "COIN", "SHOP", "UBER", "ABNB", "RBLX", "DKNG",
    "MELI", "NU", "SE", "DASH", "ARM", "MRVL", "ANET", "TEAM", "DELL", "SMCI",
    "VRTX", "REGN", "ISRG", "GILD", "LLY", "NVO", "COST", "CMG", "LULU", "DHI",
    "CAT", "GE", "HON", "LMT", "RTX", "UNP", "WM", "RSG", "ECL", "SHW",
    "MA", "V", "SPGI", "MCO", "AXP", "GS", "MS", "BLK", "BX", "KKR",
]


def _quantitative_screen(tickers: List[str]) -> List[Dict[str, Any]]:
    """Filter tickers by quantitative pre-buy criteria. Returns passing records."""
    import yfinance as yf

    passing = []
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            info = t.info
            if not info:
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
                "price": info.get("currentPrice", 0),
                "fwd_pe": info.get("forwardPE", 0),
            })
        except Exception:
            logger.debug("quant screen failed for %s", ticker, exc_info=True)
            continue

    return passing


def _llm_qualitative_filter(candidates: List[Dict], cfg: Dict[str, Any]) -> List[Dict]:
    """Run a quick LLM pass to assess moat, founder, and red flags.

    Returns the top 5 candidates with a one-line thesis each.
    """
    if not candidates:
        return []

    try:
        from tradingagents.llm_clients.corporate_llm_factory import build_corporate_hierarchy_llms
        llms = build_corporate_hierarchy_llms(cfg, callbacks=[])
        llm = llms.get("market_analyst") or llms.get("reflection")
        if llm is None:
            return candidates[:5]
    except Exception:
        return candidates[:5]

    cand_text = "\n".join(
        f"- {c['ticker']} ({c['name']}): rev_growth={c['rev_growth']}%, "
        f"PEG={c['peg']}, ROIC={c['roic']}%, sector={c['sector']}, "
        f"fwd_PE={c['fwd_pe']}"
        for c in candidates[:20]
    )

    prompt = (
        "You are screening for long-term growth stocks. From the candidates below, "
        "select the TOP 5 that best fit this pre-buy framework:\n"
        "- Durable competitive moat (network effects, switching costs, scale)\n"
        "- Founder-led or strong operator-led\n"
        "- No red flags (declining growth, cash flow vs GAAP earnings gap, dilution)\n"
        "- Secular tailwind (AI, cloud, fintech, electrification, ageing)\n\n"
        f"Candidates:\n{cand_text}\n\n"
        "Return EXACTLY 5 tickers, one per line, with a one-sentence thesis for each. "
        "Format: TICKER — one sentence thesis. Only return tickers from the list above."
    )

    try:
        response = llm.invoke(prompt)
        content = getattr(response, "content", str(response))
    except Exception as e:
        logger.warning("core discovery LLM call failed: %s", e)
        return candidates[:5]

    # Parse ticker lines from response
    selected = []
    for line in str(content).split("\n"):
        line = line.strip()
        for c in candidates:
            if c["ticker"] in line and c not in selected:
                c["thesis"] = line
                selected.append(c)
                break
        if len(selected) >= 5:
            break

    return selected[:5]


def run_core_discovery(cfg: Dict[str, Any]) -> str:
    """Run the full weekly core position discovery pipeline.

    Returns a formatted Telegram message with the top candidates.
    """
    today = date.today().isoformat()

    # Phase 1: Quantitative screen
    logger.info("core discovery: screening %d tickers", len(_BASE_UNIVERSE))
    quant_pass = _quantitative_screen(_BASE_UNIVERSE)
    if not quant_pass:
        return "Core discovery: no tickers passed quantitative filters this week."

    logger.info("core discovery: %d passed quantitative screen", len(quant_pass))

    # Phase 2: LLM qualitative filter
    picks = _llm_qualitative_filter(quant_pass, cfg)
    if not picks:
        return "Core discovery: LLM filter returned no picks."

    # Phase 3: Build Telegram message
    lines = [
        "CORE POSITION DISCOVERY",
        f"Week of {today}",
        "",
        f"Screened {len(_BASE_UNIVERSE)} tickers. {len(quant_pass)} passed quantitative filters. "
        f"Top 5 after qualitative review:",
        "",
    ]

    for i, pick in enumerate(picks, 1):
        lines.append(
            f"{i}. {pick['ticker']} ({pick['name']}) — "
            f"rev_growth={pick['rev_growth']}%, ROIC={pick['roic']}%, "
            f"PEG={pick['peg']}, fwd_PE={pick['fwd_pe']}"
        )
        if "thesis" in pick:
            lines.append(f"   {pick['thesis']}")
        lines.append("")

    lines.append("Reply with the ticker to deep-dive, or 'skip' to pass.")

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
            rec_rationale=f"Quantitative screen: {len(quant_pass)} passed from {len(_BASE_UNIVERSE)}",
        )
    except Exception as e:
        logger.warning("core discovery send failed: %s", e)
        return f"Core discovery complete but send failed: {e}"

    return f"Core discovery: {len(picks)} candidates sent via Telegram"


def _expand_universe() -> None:
    """Placeholder: expand the base universe from external sources weekly."""
    pass
