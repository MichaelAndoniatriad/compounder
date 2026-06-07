"""Shared integer limits for portfolio-advisor LLM prompts (chars, row counts)."""

from __future__ import annotations

from typing import Any, Dict


def cfg_int(cfg: Dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(cfg.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# --- Consensus factor context helpers (v4) ---

def consensus_pm_summary_line(positions: list) -> str:
    """One line, under 200 chars, for the PM prompt."""
    try:
        from tradingagents.dataflows.llm_consensus import load_llm_consensus_snapshot
        consensus = load_llm_consensus_snapshot()
    except Exception:
        return "[CONSENSUS] data unavailable."

    if consensus is None:
        return "[CONSENSUS] data unavailable."

    top_20 = {t["ticker"]: t["rank"] for t in consensus.get("top_20", [])}
    held_in = sorted(
        [(p.get("ticker", ""), top_20[p.get("ticker", "")])
         for p in positions if p.get("ticker", "") in top_20],
        key=lambda x: x[1],
    )[:3]

    total_val = sum(abs(float(p.get("invested_usd", 0) or p.get("initialAmountInDollars", 0))) for p in positions)
    consensus_val = sum(abs(float(p.get("invested_usd", 0) or p.get("initialAmountInDollars", 0)))
                        for p in positions if p.get("ticker", "") in top_20)
    weight = (consensus_val / total_val * 100) if total_val > 0 else 0

    top_str = ", ".join(f"{t}(rank {r})" for t, r in held_in) if held_in else "none"
    return f"[CONSENSUS] portfolio weight in top 20: {weight:.0f}%. Holdings in top 5: {top_str}."[:200]


def consensus_analyst_line(ticker: str) -> str:
    """One line, per ticker, for analyst prompts. Includes composite score."""
    try:
        from tradingagents.dataflows.llm_consensus import load_llm_consensus_snapshot
        consensus = load_llm_consensus_snapshot()
    except Exception:
        return ""

    if consensus is None:
        return ""

    try:
        from tradingagents.portfolio_advisor.consensus_score import compute_composite_consensus_score
        score = compute_composite_consensus_score(ticker)
    except Exception:
        score = {"composite": 0.0}

    top_20 = {t["ticker"]: t for t in consensus.get("top_20", [])}
    if ticker in top_20:
        info = top_20[ticker]
        return (
            f"[CONSENSUS] {ticker} ranked {info['rank']} in LLM consensus, "
            f"in top 20 for {info['days_in_top_20']} days. "
            f"Composite score: {score['composite']:+.2f}."
        )[:200]
    return f"[CONSENSUS] {ticker} not in public LLM consensus top 20. Composite score: {score['composite']:+.2f}."[:200]
