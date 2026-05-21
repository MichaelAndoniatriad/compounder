"""Idea generation — the system discovers new candidates, the human doesn't curate a list.

This is Step 1 of the investor's growth framework ("Generate Ideas"). An LLM proposes
companies that fit the mandate (secular tailwinds, market leadership, >20% revenue growth,
durable moat), excluding names already held or already on the watchlist. Each idea is
sanity-checked against yfinance (drops hallucinated / delisted tickers) and added to the
watchlist, where the biweekly deep-scan gates them and sends the best to full_graph.

The LLM's knowledge has a cutoff, so it is a *first-pass idea source* only: every idea then
passes through the candidate gates (live liquidity) and full_graph deep research (live data),
which is where a stale or wrong idea gets filtered out.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from tradingagents.agents.utils.investor_policy import INVESTOR_POLICY_FULL
from tradingagents.llm_clients import create_llm_client
from tradingagents.portfolio_advisor import price_util, watchlist as watchlist_mod

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")


def _idea_llm(cfg: Dict[str, Any]):
    model = (
        cfg.get("portfolio_advisor_pm_model")
        or cfg.get("portfolio_advisor_reasoning_model")
        or "deepseek/deepseek-v4-pro"
    )
    model = str(model).strip()
    provider = (cfg.get("llm_provider") or "openrouter").lower()
    if "/" in model and provider != "openrouter":
        provider = "openrouter"
    base = cfg.get("corporate_openrouter_base_url") or cfg.get("backend_url")
    return create_llm_client(provider, model, base_url=base).get_llm()


def _extract_json_array(text: str) -> Optional[list]:
    s = str(text or "")
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", s, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start = s.find("[")
    end = s.rfind("]")
    if start != -1 and end > start:
        candidates.append(s[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, list):
                return obj
        except json.JSONDecodeError:
            continue
    return None


_PROMPT = """You are sourcing new growth-stock ideas for a personal portfolio. Step 1 of the mandate below.

{policy}

Already held or already on the watchlist (DO NOT propose these): {exclude}

Propose {n} HIGH-CONVICTION candidate companies that best fit the mandate above. For each:
- Pick a real, currently-listed US-traded equity (common ticker). No funds, no ETFs, no indices.
- Prefer market leaders in secular-tailwind sectors (AI, cloud, electrification, fintech, healthcare innovation, ageing populations).
- Each must plausibly pass the pre-buy checklist (durable moat, >20% revenue growth, large TAM).
- strategy is "core" for a long-term compounder, or "catalyst" only if there is a SPECIFIC dated near-term event
  (then include catalyst_date as YYYY-MM-DD and name the event in the thesis).

Return ONLY a JSON array, no prose, each element exactly:
{{"ticker": "TICK", "thesis": "one or two sentences on why it fits", "theme": "sector/tailwind",
"strategy": "core|catalyst", "catalyst_date": ""}}
"""


def generate_candidate_ideas(
    cfg: Dict[str, Any],
    *,
    live_tickers: Optional[Any] = None,
    n: int = 8,
    validate: bool = True,
) -> List[Dict[str, Any]]:
    """Ask the LLM for new growth candidates, validate tickers, return clean idea dicts."""
    live = {str(t).strip().upper() for t in (live_tickers or []) if str(t).strip()}
    existing = set()
    for e in watchlist_mod.load_watchlist(cfg):
        tk = (e if isinstance(e, str) else e.get("ticker", "")).strip().upper()
        if tk:
            existing.add(tk)
    exclude = sorted(live | existing)

    prompt = _PROMPT.format(
        policy=INVESTOR_POLICY_FULL[:2200],
        exclude=", ".join(exclude) if exclude else "(none)",
        n=int(n),
    )
    llm = _idea_llm(cfg)
    try:
        raw = llm.invoke([HumanMessage(content=prompt)])
    except Exception as e:
        logger.warning("idea generation LLM call failed: %s", e)
        return []
    content = getattr(raw, "content", str(raw))
    if isinstance(content, list):
        content = "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
        ) or str(content)
    arr = _extract_json_array(content)
    if not arr:
        logger.warning("idea generation: no JSON array parsed")
        return []

    seen = set(exclude)
    ideas: List[Dict[str, Any]] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        tk = str(item.get("ticker") or "").strip().upper()
        if not tk or not _TICKER_RE.match(tk) or tk in seen:
            continue
        strategy = str(item.get("strategy") or "core").strip().lower()
        if strategy not in ("core", "catalyst"):
            strategy = "core"
        if validate:
            try:
                if price_util.last_close_yfinance(tk) is None:
                    logger.info("idea generation: dropping %s (no price data — likely invalid)", tk)
                    continue
            except Exception:
                continue
        seen.add(tk)
        ideas.append(
            {
                "ticker": tk,
                "thesis": str(item.get("thesis") or "").strip()[:400],
                "theme": str(item.get("theme") or "").strip()[:120],
                "strategy": strategy,
                "catalyst_date": str(item.get("catalyst_date") or "").strip()[:10],
            }
        )
    return ideas


def discover_ideas_to_watchlist(
    cfg: Dict[str, Any],
    *,
    live_tickers: Optional[Any] = None,
    n: int = 8,
) -> Dict[str, Any]:
    """Generate ideas and add the valid, novel ones to the watchlist. Returns a summary."""
    ideas = generate_candidate_ideas(cfg, live_tickers=live_tickers, n=n)
    added: List[str] = []
    for idea in ideas:
        ok = watchlist_mod.add_to_watchlist(
            cfg,
            idea["ticker"],
            thesis=idea["thesis"],
            strategy=idea["strategy"],
            catalyst_date=idea.get("catalyst_date", ""),
        )
        if ok:
            added.append(idea["ticker"])
    logger.info("idea generation added %d new watchlist names: %s", len(added), ", ".join(added))
    return {"generated": [i["ticker"] for i in ideas], "added": added, "ideas": ideas}
