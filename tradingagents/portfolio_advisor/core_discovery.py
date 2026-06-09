"""Core position discovery — monthly screen for long-term growth candidates.

Runs first Saturday of each month. Pulls live index constituents, screens
quantitatively via Alpha Vantage, runs LLM qualitative pass on survivors.
No hardcoded universes. No artificial dedup. Merit-only: if a stock
qualifies, it qualifies."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
# EV/Sales-to-Growth tiers for loss-makers (lower = cheaper for the growth).
# Used as fallback when PEG is garbage (peg >= 999 or peg <= 0).
_EVS_GROWTH_TIERS = [(0.40, 0.25), (0.70, 0.18), (1.20, 0.10), (2.00, 0.04)]
_ROIC_TIERS = [(0.20, 0.25), (0.15, 0.20), (0.10, 0.12), (0.07, 0.06)]
_GROSS_MARGIN_TIERS = [(0.50, 0.15), (0.35, 0.10), (0.25, 0.05)]
_COMBO_BONUS_THRESHOLD = (0.20, 0.15, 1.5)  # rev_growth, roic, peg
_COMBO_BONUS_POINTS = 0.15


def _build_universe() -> List[str]:
    """Pull live index constituents from Wikipedia. No hardcoded lists.

    Scans all tables on each page for a Symbol/Ticker column rather than
    hardcoding table indices, which break when Wikipedia editors reorder tables.

    UNIVERSE = S&P 500 + NASDAQ 100. Down-cap widening is DELIBERATELY DEFERRED:
    a separate decision after de-biasing is proven (see core_discovery_v2_plan.md section 9).
    Widening the universe raises risk and must be assessed independently.
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
    """Composite quality score [0.0, 1.0] from fundamentals dict.

    Each criterion contributes points by tier. Names that are strong on most
    criteria but weak on one still survive. Names below the minimum cap are
    returned as 0 (hard gate handled by caller).

    VALUATION POLICY (per core_discovery_v2_plan.md section 6):
    Valuation enters only as GROWTH-ADJUSTED (PEG or EV/Sales / growth for
    loss-makers), as one weighted input among moat/growth/ROIC/margins.
    No deep-value / "buy cheap" bias: that excludes the best compounders
    and walks into value traps. Entry timing lives in the PM layer. DO NOT
    add any "undervalued" or "cheap" filter here.
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
    # PEG tier (lower is better; we step down). When PEG is garbage,
    # fall back to EV/Sales÷growth for loss-makers.
    if 0 < peg < 999:
        for threshold, points in _PEG_TIERS:
            if peg <= threshold:
                score += points
                break
    else:
        ev_sales = info.get("evToRevenue") or info.get("priceToSales") or 0
        growth_pct = (rev_growth or 0) * 100      # rev_growth is a fraction
        evs_to_growth = ev_sales / growth_pct if (ev_sales > 0 and growth_pct > 0) else None
        if evs_to_growth is not None:
            for threshold, points in _EVS_GROWTH_TIERS:
                if evs_to_growth <= threshold:
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
                "summary": (info.get("longBusinessSummary") or "")[:400],
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
            f"{c['ticker']} ({c.get('name','')}) — "
            f"{c['sector']}/{c.get('industry','')} | "
            f"rev_growth={c['rev_growth']}% | PEG={peg_str} | "
            f"ROIC={c['roic']}% | gross_margin={c.get('gross_margin', 0)}% | "
            f"fwd_PE={c['fwd_pe']} | D/E={c.get('debt_equity', 0)} | "
            f"score={c.get('score', 0)}"
        )
        summary = c.get("summary", "")
        if summary:
            lines.append(f"  {summary}")
        lines.append("")
    cand_text = "\n".join(lines)

    # Build current holdings + existing research context
    holdings_text = ""
    researched_text = ""
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

    # Check which candidates already have deep research
    try:
        from pathlib import Path as _Path
        research_dir = _Path.home() / ".tradingagents" / "portfolio_advisor" / "clerk_deep"
        researched = set()
        if research_dir.is_dir():
            for d in research_dir.iterdir():
                if d.is_dir():
                    researched.add(d.name.upper())
        if researched:
            overlap = [t for t in [c["ticker"] for c in candidates] if t in researched]
            if overlap:
                researched_text = (
                    "\n\nThese tickers ALREADY have deep research on file. "
                    "Do NOT recommend them for deep dive. Note them if they qualify, "
                    "but mark DEEP_DIVE NO with reason 'existing research': "
                    + ", ".join(sorted(overlap))
                )
    except Exception:
        pass

    # Build anti-herd context: load consensus top_20 if available
    consensus_top = _load_consensus_top_list()
    consensus_text = ""
    if consensus_top:
        consensus_text = (
            "\n\nCONSENSUS HERD LIST (names the AI/financial-media herd is currently pushing):\n"
            + ", ".join(consensus_top[:20])
            + "\nDo NOT default to these names. Treat a consensus presence as a mild negative "
            "unless the setup is genuinely exceptional."
        )

    prompt = (
        "You are screening stocks for a concentrated growth portfolio. "
        "From the candidates below, identify up to 5 that have the best long-term compounding potential. "
        "Prioritise: durable competitive moat, founder-led or strong operator, secular tailwind, "
        "and clean financials (cash flow tracks earnings, limited dilution, manageable debt, "
        "growth not decelerating).\\n\\n"
        "IMPORTANT: Do not default to the obvious mega-caps that every AI screen surfaces. "
        "Prefer under-followed, differentiated businesses that clear the same quality bar. "
        "If a candidate is in the consensus/herd list above, treat that as a mild negative "
        "unless its setup is exceptional.\\n\\n"
        f"Candidates:\\n{cand_text}\\n"
        f"{consensus_text}\\n"
        f"{holdings_text}\\n"
        f"{researched_text}\\n"
        "Return EXACTLY one line per pick. Format each line as:\\n"
        "TICKER — CONVICTION (High/Medium/Low) — STATUS (new/overlap/repeat) — DEEP_DIVE (YES/NO) — HERD (CONSENSUS/DIFFERENTIATED) — punchy one-line thesis\\n\\n"
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

    # Parse ticker lines.
    # LLM format: TICKER — CONVICTION — STATUS — DEEP_DIVE — HERD — thesis
    ticker_re = _re.compile(
        r"^([A-Z]{1,5})\s*[—\-]\s*(High|Medium|Low)\s*[—\-]\s*(new|overlap|repeat|\w+)"
        r"\s*[—\-]\s*(YES|NO)\s*[—\-]\s*(CONSENSUS|DIFFERENTIATED|\w+)",
        _re.IGNORECASE,
    )

    selected = []
    for line in str(content).split("\n"):
        line = line.strip()
        m = ticker_re.search(line)
        if not m:
            # Fall back to old format without HERD tag for backward compatibility
            fallback_re = _re.compile(
                r"^([A-Z]{1,5})\s*[—\-]\s*(High|Medium|Low)\s*[—\-]\s*(new|overlap|repeat|\w+)"
                r"\s*[—\-]\s*(YES|NO)",
                _re.IGNORECASE,
            )
            m = fallback_re.search(line)
            herd_tag = "UNKNOWN"
        else:
            herd_tag = m.group(5).upper()
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
                c["herd"] = herd_tag
                selected.append(c)
                break
        if len(selected) >= 5:
            break

    # Return all selected picks — caller splits deep-dive vs on-the-radar.
    # If LLM returned no picks at all, fall back to top-5 by score.
    if selected:
        return selected
    return _pick_top_5(candidates)


def _load_monthly_cache(month_key: str) -> Optional[List[Dict]]:
    """Return cached picks if they exist for this month and quant results match."""
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    cache_path = _Path.home() / ".tradingagents" / "cache" / f"core_discovery_picks_{_dt.now(timezone.utc).strftime('%Y-%m')}.json"
    if not cache_path.is_file():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("month_key") == month_key:
            logger.info("core discovery: using cached picks from earlier this month (%d picks)", len(cached.get("picks", [])))
            return cached["picks"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def _save_monthly_cache(month_key: str, picks: List[Dict]) -> None:
    """Cache picks for the rest of the month so repeated runs return same results."""
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    cache_path = _Path.home() / ".tradingagents" / "cache" / f"core_discovery_picks_{_dt.now(timezone.utc).strftime('%Y-%m')}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    slim = [{
        "ticker": p["ticker"], "name": p.get("name", ""), "sector": p.get("sector", ""),
        "rev_growth": p.get("rev_growth"), "roic": p.get("roic"), "peg": p.get("peg"),
        "score": p.get("score"), "conviction": p.get("conviction"), "deep_dive": p.get("deep_dive"),
        "herd": p.get("herd", "UNKNOWN"), "thesis": p.get("thesis", ""), "gross_margin": p.get("gross_margin"),
        "fwd_pe": p.get("fwd_pe"), "debt_equity": p.get("debt_equity"),
    } for p in picks]
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"month_key": month_key, "picks": slim}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_path)
    logger.info("core discovery: cached %d picks for the month", len(picks))


def _pick_top_5(candidates: List[Dict]) -> List[Dict]:
    """Fallback: pick top 5 by composite score when LLM unavailable."""
    return sorted(candidates, key=lambda c: c.get("score", 0), reverse=True)[:5]


def _load_consensus_top_list() -> List[str]:
    """Load the current top 20 consensus tickers from the snapshot, if it exists.

    Returns empty list if no snapshot, no data, or any error — fail-safe.
    """
    from pathlib import Path as _Path
    try:
        snapshot_path = _Path.home() / ".tradingagents" / "cache" / "llm_consensus" / "snapshot.json"
        if snapshot_path.is_file():
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            return data.get("top_20", [])
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return []


def _apply_consensus_tilt(candidates: List[Dict], cfg: Dict[str, Any]) -> List[Dict]:
    """Apply discovery consensus tilt to re-rank survivors by crowding.

    Uses only consensus_entry_score + consensus_divergence_score (averaged).
    Retail flow is reserved for the PM sizing layer.
    Gated behind CONSENSUS_FACTOR_LIVE — returns candidates unchanged if off
    or if snapshot is empty (fail-safe: never errors, never blocks a pick).
    """
    if not cfg.get("CONSENSUS_FACTOR_LIVE", False):
        return candidates

    tilt_weight = float(cfg.get("consensus_tilt_weight", 0.10))
    if tilt_weight <= 0:
        return candidates

    try:
        from tradingagents.portfolio_advisor.consensus_score import (
            consensus_entry_score,
            consensus_divergence_score,
        )

        for c in candidates:
            ticker = c["ticker"]
            entry = consensus_entry_score(ticker)
            divergence = consensus_divergence_score(ticker)
            tilt = tilt_weight * (entry + divergence) / 2.0
            # Store tilt for audit; adjust effective score for ranking
            c["consensus_tilt"] = round(tilt, 3)
            c["effective_score"] = round(c.get("score", 0) + tilt, 3)

        # Re-sort by effective score descending
        candidates.sort(key=lambda c: c.get("effective_score", c.get("score", 0)), reverse=True)
        logger.info(
            "core discovery: applied consensus tilt to %d candidates (weight=%.2f)",
            len(candidates), tilt_weight,
        )
    except Exception as e:
        logger.warning("core discovery: consensus tilt failed, returning unscored: %s", e)

    return candidates


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
        return "Core discovery: no tickers passed quantitative filters this month."
    logger.info("core discovery: %d passed quantitative screen (%.1fs)", len(quant_pass), t3 - t2)

    # Phase 2b: Apply consensus tilt (post-gate, ranking only).
    # Re-orders survivors by crowding; never changes who passes the gate.
    # Gated behind CONSENSUS_FACTOR_LIVE — no-ops to neutral until snapshot is populated.
    quant_pass = _apply_consensus_tilt(quant_pass, cfg)

    # Phase 3: LLM qualitative rank (monthly cache makes it deterministic within a month)
    # TODO earnings-season refresh: run an extra discovery pass during earnings season.
    month_key = f"{today}_{len(quant_pass)}"
    picks = _load_monthly_cache(month_key)
    if picks is None:
        picks = _llm_qualitative_rank(quant_pass, cfg)
        if picks:
            _save_monthly_cache(month_key, picks)
    t4 = time.monotonic()

    # Phase 4: Split into deep-dive and on-the-radar tiers
    deep_dive_picks = [p for p in picks if p.get("deep_dive") == "YES"]
    radar_picks = [p for p in picks if p.get("deep_dive") != "YES"]

    # Phase 4a: Build Telegram message for deep-dive picks only
    if deep_dive_picks:
        top_pick = deep_dive_picks[0]
        lines = [
            f"Screened {len(universe) + total_rejected} names this month — {len(quant_pass)} made it past the numbers, "
            f"and these {len(deep_dive_picks)} came out on top after qualitative review.",
            "",
        ]

        for i, pick in enumerate(deep_dive_picks, 1):
            thesis_line = pick.get("thesis", "")
            parts = thesis_line.split("—") if "—" in thesis_line else [thesis_line]
            conviction = parts[1].strip() if len(parts) >= 2 else ""
            status = parts[2].strip() if len(parts) >= 3 else ""
            thesis = "—".join(parts[3:]).strip() if len(parts) >= 4 else thesis_line

            label = ""
            herd_label = f" [{pick.get('herd', '')}]" if pick.get("herd") and pick.get("herd") != "UNKNOWN" else ""
            if "overlap" in status.lower():
                label = " (already held)"
            elif "repeat" in status.lower():
                label = " (repeat, thesis intact)"

            lines.append(f"{i}. {pick['ticker']}{label}{herd_label} — {thesis}")
            lines.append(
                f"   {pick['sector']} | rev_growth {pick['rev_growth']}% | "
                f"ROIC {pick['roic']}% | PEG {pick['peg']} | score {pick.get('score', 0):.2f}"
            )
            lines.append("")

        top_ticker = top_pick["ticker"]
        radar_note = ""
        if radar_picks:
            radar_names = ", ".join(p["ticker"] for p in radar_picks[:5])
            radar_note = f" {len(radar_picks)} more on the radar: {radar_names}."
        lines.append(
            f"Top pick is {top_ticker}. Adding all {len(deep_dive_picks)} to the PM watchlist "
            f"for the next sleeve allocation.{radar_note} Research queued on the top two."
        )

        body = "\n".join(lines)

        try:
            from tradingagents.portfolio_advisor.messaging import send_advisor_message
            send_advisor_message(
                cfg, "Core Discovery", body, urgent=True,
                log_as_recommendation=True,
                rec_trigger="core_discovery",
                rec_type="core_candidate",
                rec_action=f"monthly_screen_{len(deep_dive_picks)}_deep_dive",
                rec_rationale=(
                    f"Screened {len(universe)} tickers. {len(quant_pass)} passed quant. "
                    f"{len(deep_dive_picks)} deep-dive, {len(radar_picks)} on-radar. "
                    f"Herd tags: {', '.join(p['ticker'] + ':' + p.get('herd', 'UNKNOWN') for p in deep_dive_picks)}"
                ),
            )
        except Exception as e:
            logger.warning("core discovery send failed: %s", e)
            return f"Core discovery complete but send failed: {e}"
    else:
        # No deep-dive picks — send quiet summary if there are radar picks
        body = (
            f"Screened {len(universe) + total_rejected} names this month — {len(quant_pass)} passed the numbers, "
            f"but nothing cleared the deep-dive bar after qualitative review."
        )
        if radar_picks:
            radar_names = ", ".join(p["ticker"] for p in radar_picks[:5])
            body += f" {len(radar_picks)} on the radar (watchlist only): {radar_names}."

        try:
            from tradingagents.portfolio_advisor.messaging import send_advisor_message
            send_advisor_message(
                cfg, "Core Discovery", body, urgent=False,
                log_as_recommendation=True,
                rec_trigger="core_discovery",
                rec_type="core_candidate",
                rec_action="monthly_screen_no_deep_dive",
                rec_rationale=(
                    f"Screened {len(universe)} tickers. {len(quant_pass)} passed quant. "
                    f"0 deep-dive, {len(radar_picks)} on-radar."
                ),
            )
        except Exception as e:
            logger.warning("core discovery send failed: %s", e)
            return f"Core discovery complete but send failed: {e}"

    # Phase 5: Push picks into PM watchlist (both tiers)
    try:
        from tradingagents.portfolio_advisor.watchlist import load_watchlist, save_watchlist

        wl = load_watchlist(cfg)
        added_deep = 0
        added_radar = 0
        updated = 0

        for pick in deep_dive_picks:
            ticker = pick["ticker"]
            raw = pick.get("thesis", "")
            parts = raw.split("—") if "—" in raw else [raw]
            thesis_text = parts[-1].strip() if len(parts) >= 5 else raw

            found = None
            for e in wl:
                et = (e if isinstance(e, str) else e.get("ticker", "")).strip().upper()
                if et == ticker:
                    found = e
                    break

            if found and isinstance(found, dict):
                if found.get("thesis", ""):
                    continue
                found["thesis"] = thesis_text
                found["source"] = "core_discovery"
                found["added"] = today
                updated += 1
            elif not found:
                wl.append({
                    "ticker": ticker, "thesis": thesis_text,
                    "strategy": "core", "source": "core_discovery",
                    "added": today,
                })
                added_deep += 1

        # On-the-radar picks: source="core_discovery_watch", low priority
        for pick in radar_picks:
            ticker = pick["ticker"]
            raw = pick.get("thesis", "")
            parts = raw.split("—") if "—" in raw else [raw]
            thesis_text = parts[-1].strip() if len(parts) >= 5 else raw

            found = None
            for e in wl:
                et = (e if isinstance(e, str) else e.get("ticker", "")).strip().upper()
                if et == ticker:
                    found = e
                    break

            if found and isinstance(found, dict):
                continue  # already exists, don't downgrade source
            elif not found:
                wl.append({
                    "ticker": ticker, "thesis": thesis_text,
                    "strategy": "core", "source": "core_discovery_watch",
                    "added": today,
                })
                added_radar += 1

        if added_deep or added_radar or updated:
            save_watchlist(cfg, wl)
            logger.info(
                "core discovery: deep-dive +%d, radar +%d, updated %d in PM watchlist",
                added_deep, added_radar, updated,
            )
    except Exception as e:
        logger.warning("core discovery: watchlist update failed: %s", e)

    logger.info("core discovery: complete — %d deep-dive, %d on-radar (%.1fs total)",
                len(deep_dive_picks), len(radar_picks), time.monotonic() - t0)
    return f"Core discovery: {len(deep_dive_picks)} deep-dive, {len(radar_picks)} on-radar"
