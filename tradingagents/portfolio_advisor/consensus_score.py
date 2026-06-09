"""Consensus as a tradable factor — scoring helpers for sizing modulation.

v4 reframe: consensus is a factor that modulates how much and when, not
whether. Scores are -1.0 to +1.0. Composite is the mean of three components.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def consensus_entry_score(ticker: str) -> float:
    """Return -1.0 to +1.0. Higher = better entry timing based on consensus age.

    - Fresh entry (< 14 days in top 20): +0.5
    - Mature (14-60 days): 0.0
    - Stale (60-180 days): -0.3
    - Very stale (> 180 days): -0.6
    - Not in consensus at all: +0.3 (negative universe edge)
    """
    consensus = _load()
    if consensus is None:
        return 0.0
    top_20 = {t["ticker"]: t for t in consensus.get("top_20", [])}
    if ticker not in top_20:
        return 0.3
    days = top_20[ticker].get("days_in_top_20", 0)
    if days < 14:
        return 0.5
    if days < 60:
        return 0.0
    if days < 180:
        return -0.3
    return -0.6


def consensus_divergence_score(ticker: str) -> float:
    """Return -1.0 to +1.0. Higher = DeepSeek dissenting from public consensus.

    - DeepSeek picked AND in public consensus: -0.4 (herding)
    - DeepSeek picked AND NOT in consensus: +0.4 (divergent alpha)
    - DeepSeek did not pick: 0.0 (no signal)
    """
    consensus = _load()
    if consensus is None:
        return 0.0
    deepseek = consensus.get("deepseek_alignment", {})
    deepseek_picks = set(deepseek.get("deepseek_last_recommended", []))
    if ticker not in deepseek_picks:
        return 0.0
    top_20 = {t["ticker"] for t in consensus.get("top_20", [])}
    if ticker in top_20:
        return -0.4
    return 0.4


def consensus_retail_flow_score(ticker: str) -> float:
    """Return -1.0 to +1.0. Higher = retail flow supports entry timing.

    - Retail flow > 35% of ADV: -0.5 (peak crowd)
    - 20-35%: -0.2
    - 10-20%: 0.0
    - < 10%: +0.2 (institutional dominated)
    """
    try:
        from tradingagents.dataflows.retail_flow_tracker import get_retail_flow_share
        flow = get_retail_flow_share(ticker)
    except Exception:
        flow = None
    if flow is None:
        return 0.0
    share = float(flow.get("share_30d", 0) or 0)
    if share > 0.35:
        return -0.5
    if share > 0.20:
        return -0.2
    if share > 0.10:
        return 0.0
    return 0.2


def compute_composite_consensus_score(ticker: str) -> dict:
    """Return composite score and components for transparency."""
    entry = consensus_entry_score(ticker)
    divergence = consensus_divergence_score(ticker)
    flow = consensus_retail_flow_score(ticker)
    composite = (entry + divergence + flow) / 3
    return {
        "composite": round(composite, 3),
        "entry": round(entry, 3),
        "divergence": round(divergence, 3),
        "flow": round(flow, 3),
    }


def _load() -> Optional[Dict[str, Any]]:
    try:
        from tradingagents.dataflows.llm_consensus import load_llm_consensus_snapshot
        return load_llm_consensus_snapshot()
    except Exception:
        return None
