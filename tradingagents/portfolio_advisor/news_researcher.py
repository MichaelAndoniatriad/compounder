"""Weekly news-driven candidate discovery — Stage 1 of the research funnel.

A researcher reads live market news (get_global_news), applies the discovery rules below,
and surfaces candidate companies — both long-term/core ideas and dated catalyst trades —
grounded in what is actually happening rather than the model's training memory.

The funnel:
  Stage 1 (here): news + rules -> candidates -> watchlist
  Stage 2 (candidates.py): hard gates + single_model thesis screen  (initial analysis)
  Stage 3 (full_graph): the best screened names get the 12-agent deep run, capped per week
                        -> PM decides, tagged core or catalyst

News is the primary source. If a week's news yields fewer than the target number of
candidates that fit the rules, the memory-based idea_generator tops up the rest so the
funnel never goes idle (hybrid). Already-owned names are dropped by the portfolio-fit gate.

DISCOVERY_RULES / SCREEN_RULES are drafted from the investor policy and meant to be edited.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.llm_clients import create_llm_client
from tradingagents.portfolio_advisor import price_util, watchlist as watchlist_mod

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")

# --- Editable discovery rules (drafted from the investor policy) ---
DISCOVERY_RULES = """Pursue a company surfaced in the news ONLY if it clearly fits ONE of these:

A) LONG-TERM (core): a market leader or clear #2 in a secular-tailwind sector (AI, cloud/software,
   semiconductors, electrification/energy, fintech, healthcare innovation, ageing demographics). The
   news should show accelerating growth, TAM expansion, a new product/market, or a strengthening moat.
   Market cap > $1B. Not a turnaround, deep-cyclical, or commodity bet.

B) CATALYST (event): a SPECIFIC, DATED, tradeable upcoming event within ~6 weeks — earnings setup,
   M&A, FDA/PDUFA decision, major contract or customer win, guidance raise, index inclusion,
   regulatory/legal ruling, or product launch. Must be liquid enough to enter and exit.

EXCLUDE: pre-revenue or binary-only biotech, sub-$1B microcaps, OTC/pink sheets, ETFs/funds,
leveraged/inverse products, meme or short-squeeze moves with no fundamental basis, and pure rumor
with no named source or date."""

# --- Editable screen / promotion rules (used by the funnel + documented for the screen stage) ---
SCREEN_RULES = """Promote a screened candidate to full_graph deep research ONLY if:
- Hard gates pass (liquidity, portfolio-fit, catalyst-present for catalyst names).
- CORE: the single_model thesis check finds the growth/moat case directionally intact, with no
  immediate red flag from the disqualifier scan (decelerating growth, dilution, margin collapse).
- CATALYST: the catalyst is confirmed real, dated, inside the tradeable window, and not already played out.
- The weekly full_graph budget is not exhausted; otherwise hold the candidate for next week, ranked by priority."""


def _researcher_llm(cfg: Dict[str, Any]):
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


def _fetch_global_news(cfg: Dict[str, Any]) -> str:
    set_config(cfg)
    today = date.today().isoformat()
    look_back = int(cfg.get("global_news_lookback_days") or 7)
    limit = int(cfg.get("news_researcher_article_limit") or 40)
    try:
        return str(route_to_vendor("get_global_news", today, look_back, limit) or "")
    except Exception as e:
        logger.warning("news researcher: get_global_news failed: %s", e)
        return ""


_PROMPT = """You are a buy-side research analyst doing a weekly idea scan from market news.

Discovery rules (apply strictly):
{rules}

Already held or already on the watchlist (do not propose these): {exclude}

Recent market news (last {days} days):
{news}

From this news, surface up to {n} candidate companies that fit the discovery rules. Favour quality over
quantity — return fewer if the news is thin. For each candidate:
- Use a real, currently-listed US-traded common-stock ticker.
- strategy "core" for a long-term compounder, or "catalyst" only if there is a specific dated event (then set
  catalyst_date YYYY-MM-DD and name the event).
- news_basis: the specific item in the news above that prompted this idea.

Return ONLY a JSON array, no prose, each element exactly:
{{"ticker": "TICK", "thesis": "why it fits the rules", "theme": "sector/tailwind",
"strategy": "core|catalyst", "catalyst_date": "", "news_basis": "what in the news triggered this"}}
If the news surfaces nothing that fits the rules, return [].
"""


def discover_from_news(
    cfg: Dict[str, Any],
    *,
    live_tickers: Optional[Any] = None,
    n: int = 8,
    validate: bool = True,
) -> List[Dict[str, Any]]:
    """Read live market news and return candidate ideas that fit the discovery rules."""
    news = _fetch_global_news(cfg)
    if not news.strip():
        logger.info("news researcher: no news returned; skipping news discovery")
        return []

    live = {str(t).strip().upper() for t in (live_tickers or []) if str(t).strip()}
    existing = set()
    for e in watchlist_mod.load_watchlist(cfg):
        tk = (e if isinstance(e, str) else e.get("ticker", "")).strip().upper()
        if tk:
            existing.add(tk)
    exclude = sorted(live | existing)

    prompt = _PROMPT.format(
        rules=DISCOVERY_RULES,
        exclude=", ".join(exclude) if exclude else "(none)",
        days=int(cfg.get("global_news_lookback_days") or 7),
        news=news[: int(cfg.get("news_researcher_news_chars") or 12000)],
        n=int(n),
    )
    llm = _researcher_llm(cfg)
    try:
        raw = llm.invoke([HumanMessage(content=prompt)])
    except Exception as e:
        logger.warning("news researcher LLM call failed: %s", e)
        return []
    content = getattr(raw, "content", str(raw))
    if isinstance(content, list):
        content = "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
        ) or str(content)
    arr = _extract_json_array(content)
    if not arr:
        logger.info("news researcher: no JSON array parsed (news may not fit the rules)")
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
                    logger.info("news researcher: dropping %s (no price data)", tk)
                    continue
            except Exception:
                continue
        seen.add(tk)
        thesis = str(item.get("thesis") or "").strip()
        basis = str(item.get("news_basis") or "").strip()
        if basis:
            thesis = f"{thesis} [news: {basis}]"[:500]
        ideas.append(
            {
                "ticker": tk,
                "thesis": thesis[:500],
                "theme": str(item.get("theme") or "").strip()[:120],
                "strategy": strategy,
                "catalyst_date": str(item.get("catalyst_date") or "").strip()[:10],
                "source": "news_researcher",
            }
        )
    return ideas


def run_weekly_discovery(
    cfg: Dict[str, Any],
    *,
    live_tickers: Optional[Any] = None,
) -> Dict[str, Any]:
    """Stage 1 of the weekly funnel: discover candidates, gate them, queue single_model screens.

    News is the primary source; if it yields fewer than the target, the memory-based idea generator
    tops up the rest (hybrid). Gate-passing candidates get a cheap single_model thesis screen queued;
    the existing candidate pipeline auto-promotes the winners to full_graph (subject to the weekly cap).
    Returns a summary dict.
    """
    from tradingagents.portfolio_advisor.candidates import (
        evaluate_candidates_with_evidence,
        append_candidate_records,
        queue_candidate_research_jobs,
        weekly_full_graph_budget_left,
    )

    target = int(cfg.get("news_researcher_target_candidates") or 8)
    live = {str(t).strip().upper() for t in (live_tickers or []) if str(t).strip()}

    news_ideas = discover_from_news(cfg, live_tickers=live, n=target)
    ideas = list(news_ideas)

    # Hybrid top-up: if news was thin, fill the rest from the memory-based generator.
    topped_up: List[str] = []
    if len(ideas) < target:
        try:
            from tradingagents.portfolio_advisor.idea_generator import generate_candidate_ideas
            have = {i["ticker"] for i in ideas}
            extra = generate_candidate_ideas(cfg, live_tickers=live, n=target - len(ideas))
            for e in extra:
                if e["ticker"] not in have:
                    e["source"] = "idea_generator_topup"
                    ideas.append(e)
                    topped_up.append(e["ticker"])
                    have.add(e["ticker"])
        except Exception as e:
            logger.warning("hybrid top-up failed: %s", e)

    if not ideas:
        return {"discovered": [], "added": [], "screened": 0, "reason": "no candidates from news or generator"}

    # Persist to the watchlist.
    added: List[str] = []
    for idea in ideas:
        if watchlist_mod.add_to_watchlist(
            cfg, idea["ticker"], thesis=idea["thesis"],
            strategy=idea["strategy"], catalyst_date=idea.get("catalyst_date", ""),
        ):
            added.append(idea["ticker"])

    # Gate + queue cheap single_model screens (the "initial analysis" stage).
    candidate_items = [
        {
            "ticker": i["ticker"],
            "thesis": i["thesis"],
            "strategy": i["strategy"],
            "catalyst": i.get("catalyst_date", ""),
            "source": i.get("source", "news_researcher"),
        }
        for i in ideas
    ]
    records = evaluate_candidates_with_evidence(
        cfg, candidate_items, live_tickers=live, default_source="weekly_discovery"
    )
    append_candidate_records(cfg, records)
    screened = queue_candidate_research_jobs(cfg, records)

    return {
        "discovered": [i["ticker"] for i in news_ideas],
        "topped_up": topped_up,
        "added": added,
        "screened": screened,
        "rejected": [r.ticker for r in records if r.status == "rejected"],
        "full_graph_budget_left": weekly_full_graph_budget_left(cfg),
    }
