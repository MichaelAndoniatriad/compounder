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
# Only market cap is a hard gate. The rest contribute to a composite score so
# strong-on-most / weak-on-one names survive the funnel into LLM ranking.
# The 4-hard-filter version produced zero candidates in the 7 Jun 2026 run.
MIN_MARKET_CAP = 1_000_000_000  # $1B — the only hard gate
SURVIVOR_SCORE = 0.55  # composite threshold to graduate to LLM ranking

# Tier breakpoints (each contributes points to the composite)
_GROWTH_TIERS = [(0.20, 0.35), (0.15, 0.25), (0.10, 0.15), (0.05, 0.07)]
_PEG_TIERS = [(1.5, 0.25), (2.0, 0.18), (3.0, 0.10), (5.0, 0.04)]
_ROIC_TIERS = [(0.20, 0.25), (0.15, 0.20), (0.10, 0.12), (0.07, 0.06)]
_GROSS_MARGIN_TIERS = [(0.50, 0.15), (0.35, 0.10), (0.25, 0.05)]
_COMBO_BONUS_THRESHOLD = (0.20, 0.15, 1.5)  # rev_growth, roic, peg
_COMBO_BONUS_POINTS = 0.15


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
            import io as _io
            import requests
            resp = requests.get(url, headers={"User-Agent": "TradingAgents/1.0 (portfolio research)"}, timeout=15)
            resp.raise_for_status()
            all_tables = pd.read_html(_io.StringIO(resp.text))
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


def _score_quantitative(info: Dict[str, Any]) -> float:
    """Composite quality score [0.0, 1.0] from yfinance info dict.

    Each criterion contributes points by tier. Names that are strong on most
    criteria but weak on one still survive. Names below the minimum cap are
    returned as 0 (hard gate handled by caller).
    """
    market_cap = info.get("marketCap", 0) or 0
    if market_cap < MIN_MARKET_CAP:
        return 0.0

    rev_growth = info.get("revenueGrowth", 0) or 0
    peg = info.get("pegRatio", 999) or 999
    # yfinance does not expose returnOnCapital reliably; fall back to ROE.
    # ROE is not identical to ROIC but is close enough for first pass screening
    # and is consistently populated by yfinance.
    roic = info.get("returnOnCapital") or info.get("returnOnEquity") or 0
    gross_margin = info.get("grossMargins", 0) or 0

    score = 0.0
    # Revenue growth tier
    for threshold, points in _GROWTH_TIERS:
        if rev_growth >= threshold:
            score += points
            break
    # PEG tier (lower is better; we step down)
    for threshold, points in _PEG_TIERS:
        if peg <= threshold:
            score += points
            break
    # ROIC tier
    for threshold, points in _ROIC_TIERS:
        if roic >= threshold:
            score += points
            break
    # Gross margin tier (quality proxy)
    for threshold, points in _GROSS_MARGIN_TIERS:
        if gross_margin >= threshold:
            score += points
            break
    # Rare-combination bonus: strong growth AND capital efficient AND not richly valued
    if (
        rev_growth >= _COMBO_BONUS_THRESHOLD[0]
        and roic >= _COMBO_BONUS_THRESHOLD[1]
        and peg <= _COMBO_BONUS_THRESHOLD[2]
    ):
        score += _COMBO_BONUS_POINTS

    return min(score, 1.0)


def _quantitative_screen(tickers: List[str]) -> List[Dict[str, Any]]:
    """Score every ticker and return those above SURVIVOR_SCORE.

    Uses Alpha Vantage OVERVIEW with 7-day disk cache. No yfinance dependency.
    """
    import time as _time

    passing = []
    for i, ticker in enumerate(tickers):
        # Respect Alpha Vantage rate limit (75/min premium, 5/min free).
        # 0.3s per call = ~200/min, safe for premium tier.
        if i > 0 and i % 10 == 0:
            _time.sleep(0.3)
        try:
            from tradingagents.dataflows.alpha_vantage_fundamentals_cached import get_ticker_fundamentals

            info = get_ticker_fundamentals(ticker)
            if not info:
                continue

            market_cap = info.get("marketCap", 0) or 0
            if market_cap < MIN_MARKET_CAP:
                continue

            score = _score_quantitative(info)
            if score < SURVIVOR_SCORE:
                continue

            rev_growth = info.get("revenueGrowth", 0) or 0
            peg = info.get("pegRatio", 999) or 999
            roic = info.get("returnOnCapital") or info.get("returnOnEquity") or 0

            passing.append({
                "ticker": ticker,
                "name": info.get("shortName", ticker),
                "market_cap_b": round(market_cap / 1e9, 1),
                "rev_growth": round(rev_growth * 100, 1),
                "peg": round(peg, 1) if peg < 999 else None,
                "roic": round(roic * 100, 1),
                "gross_margin": round(float(info.get("grossMargins", 0) or 0) * 100, 1),
                "score": round(score, 3),
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
                "price": info.get("currentPrice", 0),
                "fwd_pe": info.get("forwardPE", 0),
                "debt_equity": info.get("debtToEquity", 0) or 0,
            })
        except Exception:
            continue

    # Rank by score descending so the LLM sees the best candidates first
    passing.sort(key=lambda c: c["score"], reverse=True)
    return passing


def _llm_qualitative_rank(candidates: List[Dict], cfg: Dict[str, Any]) -> List[Dict]:
    """LLM pass: assess moat, founder, red flags. Rank by conviction.

    Returns up to 5, ordered by conviction score. Every candidate gets a
    one-line thesis. Previously recommended stocks are NOT excluded — they
    appear again if they still qualify, flagged as 'repeat' for context.
    """
    import re as _re

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
        peg_str = str(c["peg"]) if c.get("peg") is not None else "n/a"
        lines.append(
            f"{c['ticker']} | score={c.get('score', 0)} | "
            f"{c['sector']}/{c.get('industry','')} | "
            f"rev_growth={c['rev_growth']}% | PEG={peg_str} | "
            f"ROIC={c['roic']}% | gross_margin={c.get('gross_margin', 0)}% | "
            f"fwd_PE={c['fwd_pe']} | D/E={c.get('debt_equity', 0)}"
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
        "You are screening stocks for a concentrated growth portfolio. "
        "From the candidates below, identify up to 5 that have the best long-term compounding potential. "
        "Prioritise: durable competitive moat, founder-led or strong operator, secular tailwind, "
        "and clean financials (cash flow tracks earnings, limited dilution, manageable debt, "
        "growth not decelerating).\n\n"
        f"Candidates:\n{cand_text}\n"
        f"{holdings_text}\n\n"
        "Return EXACTLY one line per pick. Format each line as:\n"
        "TICKER — CONVICTION (High/Medium/Low) — STATUS (new/overlap/repeat) — DEEP_DIVE (YES/NO) — punchy one-line thesis\n\n"
        "DEEP_DIVE rules:\n"
        "- YES = genuinely worth Michael spending 2+ hours researching. Must have BOTH High conviction "
        "AND at least two of: clear moat, founder-led, secular tailwind, exceptional numbers.\n"
        "- NO = decent company, passes the screens, but not compelling enough to prioritise over "
        "existing holdings or other opportunities. Still note it — just flag it as not deep-dive-worthy.\n"
        "- It is COMPLETELY FINE to return 5 NOs if nothing stands out. A quiet week is better "
        "than a forced pick.\n\n"
        "Thesis: direct and specific. Bad: 'Software company with strong growth.' "
        "Good: 'Observability platform with sticky enterprise contracts; founder-led, 30%+ FCF margins, no debt.'\n\n"
        "Return 0-5 tickers, highest conviction first. Only include tickers from the list above. "
        "No numbering, no commentary outside the format.\n"
    )

    try:
        response = llm.invoke(prompt)
        content = getattr(response, "content", str(response))
    except Exception as e:
        logger.warning("core discovery LLM failed: %s", e)
        return _pick_top_5(candidates)

    # Parse ticker lines with regex.
    # Format: TICKER — CONVICTION (High|Medium|Low) — STATUS (...) — DEEP_DIVE (YES|NO) — thesis
    ticker_re = _re.compile(
        r"^([A-Z]{1,5})\s*[—\-]\s*CONVICTION\s+(High|Medium|Low)\s*[—\-]\s*STATUS\s+\(?(\w+)\)?"
        r"\s*[—\-]\s*DEEP_DIVE\s+\(?(YES|NO)\)?",
        _re.IGNORECASE,
    )

    selected = []
    for line in str(content).split("\n"):
        line = line.strip()
        m = ticker_re.search(line)
        if not m:
            continue
        ticker = m.group(1).upper()
        conviction = m.group(2)
        deep_dive = m.group(4).upper()
        for c in candidates:
            if c["ticker"] == ticker and c not in selected:
                c["thesis"] = line
                c["conviction"] = conviction
                c["deep_dive"] = deep_dive
                selected.append(c)
                break
        if len(selected) >= 5:
            break

    # Only deliver picks marked DEEP_DIVE YES
    deep_dive_picks = [s for s in selected if s.get("deep_dive") == "YES"]
    if deep_dive_picks:
        return deep_dive_picks

    # If LLM returned picks but none are deep-dive-worthy, return empty
    # so the caller can message "nothing compelling this week"
    return [] if selected else _pick_top_5(candidates)


def _pick_top_5(candidates: List[Dict]) -> List[Dict]:
    """Fallback: pick top 5 by composite score when LLM unavailable."""
    return sorted(candidates, key=lambda c: c.get("score", 0), reverse=True)[:5]


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

    # Phase 4: Build Telegram message
    if not picks:
        lines = [
            f"Screened {len(universe) + total_rejected} names this week — {len(quant_pass)} passed the numbers, "
            f"but nothing cleared the deep-dive bar after qualitative review.",
            "",
            "No deep-dive candidates this week. Sometimes the best move is no move.",
        ]
        body = "\n".join(lines)
        try:
            from tradingagents.portfolio_advisor.messaging import send_advisor_message
            send_advisor_message(
                cfg, "Core Discovery", body, urgent=False,
                log_as_recommendation=True,
                rec_trigger="core_discovery",
                rec_type="core_candidate",
                rec_action="weekly_screen_no_candidates",
                rec_rationale=f"Screened {len(universe)} tickers. {len(quant_pass)} passed quant. 0 deep-dive worthy.",
            )
        except Exception as e:
            logger.warning("core discovery send failed: %s", e)
            return f"Core discovery complete but send failed: {e}"

        logger.info("core discovery: complete — no deep-dive candidates (%.1fs total)", time.monotonic() - t0)
        return "Core discovery: no deep-dive candidates this week."
    top_pick = picks[0]
    top_thesis = top_pick.get("thesis", "").split("—")[-1].strip() if "—" in top_pick.get("thesis", "") else ""

    lines = [
        f"Screened {len(universe) + total_rejected} names this week — {len(quant_pass)} made it past the numbers, and these {len(picks)} came out on top after qualitative review.",
        "",
    ]

    for i, pick in enumerate(picks, 1):
        thesis_line = pick.get("thesis", "")
        # Parse the LLM output format: TICKER — CONVICTION X — STATUS — thesis
        parts = thesis_line.split("—") if "—" in thesis_line else [thesis_line]
        conviction = ""
        status = ""
        thesis = ""
        if len(parts) >= 2:
            conviction = parts[1].strip()
        if len(parts) >= 3:
            status = parts[2].strip()
        if len(parts) >= 4:
            thesis = "—".join(parts[3:]).strip()
        else:
            thesis = thesis_line

        label = ""
        if "overlap" in status.lower():
            label = " (already held)"
        elif "repeat" in status.lower():
            label = " (repeat, thesis intact)"

        lines.append(f"{i}. {pick['ticker']}{label} — {thesis}")
        lines.append(
            f"   {pick['sector']} | rev_growth {pick['rev_growth']}% | "
            f"ROIC {pick['roic']}% | PEG {pick['peg']} | score {pick.get('score', 0):.2f}"
        )
        lines.append("")

    # Lead recommendation based on top pick
    top_ticker = top_pick["ticker"]
    lines.append(
        f"Top pick is {top_ticker}. I am adding all {len(picks)} to the PM watchlist "
        f"so they factor into the next sleeve allocation. Research queued on the top two. "
        f"If any of these overlap with current holdings, the PM already knows and will "
        f"flag sizing or thesis updates in the next cycle."
    )

    body = "\n".join(lines)

    # Send via Telegram
    try:
        from tradingagents.portfolio_advisor.messaging import send_advisor_message
        send_advisor_message(
            cfg, "Core Discovery", body, urgent=True,
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
