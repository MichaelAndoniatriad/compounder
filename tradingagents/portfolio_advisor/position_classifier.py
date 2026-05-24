"""LLM classifier: decide each holding's sleeve, thesis-break metrics, and keep/sell.

The human should not have to hand-tag sleeves or hand-write thesis-break metrics.
This module reads what the research stack already knows about a position (markdown
memory, recent events, the latest full-graph decision) plus its price action versus
entry, applies the investor policy, and produces a structured classification:

  - strategy: core (long-term hold/growth) or catalyst (short-term event trade)
  - thesis_break_metrics: 2-3 specific, measurable conditions that would break the thesis
  - recommendation: hold or sell (advisory only)
  - rationale: one short paragraph

The classification is written back into the position plan (preserving the eToro entry
price and any catalyst metadata). A 'sell' recommendation is surfaced, never auto-executed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from tradingagents.agents.utils.event_log import append_event, format_recent_events_for_ticker
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.investor_policy import INVESTOR_POLICY_FULL, CATALYST_POLICY_FULL
from tradingagents.llm_clients import create_llm_client
from tradingagents.portfolio_advisor import price_util
from tradingagents.portfolio_advisor.position_plans import (
    PositionPlan,
    load_position_plans,
    upsert_position_plan,
)

logger = logging.getLogger(__name__)


class PositionClassification(BaseModel):
    """Structured sleeve + exit-rule decision for one holding."""

    ticker: str = Field(description="Uppercase symbol.")
    strategy: str = Field(
        description="'core' for a long-term hold/growth position, 'catalyst' for a short-term event-driven trade.",
    )
    recommendation: str = Field(
        default="hold",
        description="'hold' to keep the position, 'sell' if the policy says exit (thesis broken, drawdown floor, etc.).",
    )
    thesis_break_metrics: List[str] = Field(
        default_factory=list,
        description="2-3 specific, measurable conditions that would break the investment thesis.",
    )
    catalyst_description: str = Field(
        default="",
        description="If strategy is catalyst: the specific event being traded. Empty for core.",
    )
    rationale: str = Field(default="", description="One short paragraph explaining the classification.")


def _classifier_llm(cfg: Dict[str, Any]):
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


def _research_context(cfg: Dict[str, Any], ticker: str) -> str:
    parts: List[str] = []
    try:
        mem = TradingMemoryLog(cfg)
        md = mem.get_past_context(
            ticker,
            n_same=int(cfg.get("memory_context_max_same_ticker") or 5),
            n_cross=0,
            lookback_days=int(cfg.get("memory_context_lookback_days") or 90),
            compact=True,
        )
        if md:
            parts.append(f"Markdown memory (trimmed):\n{md[:4000]}")
    except Exception as e:
        logger.debug("classifier memory context failed for %s: %s", ticker, e)
    try:
        ev = format_recent_events_for_ticker(cfg, ticker, days=60, max_events=15, compact=True)
        if ev:
            parts.append(f"Recent events:\n{ev[:2500]}")
    except Exception as e:
        logger.debug("classifier event context failed for %s: %s", ticker, e)
    return "\n\n".join(parts) if parts else "(no prior research on file)"


_PROMPT = """You classify one portfolio holding into a strategy sleeve and write its exit rules. Advisory only.

The portfolio runs two sleeves:

CORE (long-term hold / growth, 3-5 year horizon):
{core_policy}

CATALYST (short-term, event-driven):
{catalyst_policy}

Holding under review: {ticker}
Entry price (cost basis): {entry}
Current price: {current}
Return from entry: {pct}

What the research stack knows about {ticker}:
{context}

Decide:
1. strategy: Is this a long-term compounder (core) or a position held for a specific near-term event (catalyst)?
   Default to core unless the evidence shows it is genuinely an event trade.
2. recommendation: 'hold' normally, or 'sell' if the policy clearly says exit (thesis broken, drawdown floor breached,
   structural deterioration). Do not recommend sell merely because the position is up or down.
3. thesis_break_metrics: 2-3 SPECIFIC, MEASURABLE conditions that would break the thesis for THIS company
   (e.g. "data center revenue growth decelerates below 30% YoY for two straight quarters", "gross margin falls
   below 70%", "net revenue retention drops under 110%"). No vague statements. These are what trigger an exit.
4. catalyst_description: only if strategy is catalyst — name the event. Empty for core.
5. rationale: one short paragraph.

Base everything on the evidence above and the policy. If research is thin, classify conservatively as core and
write thesis-break metrics from the company's known business model. Do not invent specific figures you cannot support;
prefer directional, checkable conditions.

Return ONLY a JSON object, no prose before or after, with exactly these keys:
{{"ticker": "{ticker}", "strategy": "core|catalyst", "recommendation": "hold|sell",
"thesis_break_metrics": ["...", "..."], "catalyst_description": "", "rationale": "..."}}
"""


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first balanced JSON object out of an LLM response (handles thinking-mode prose)."""
    s = str(text or "")
    # Prefer a fenced ```json block if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    # Fall back to the widest brace span.
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        candidates.append(s[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def classify_position(
    cfg: Dict[str, Any],
    ticker: str,
    plan: PositionPlan,
) -> Optional[PositionClassification]:
    """Run one structured classification pass for a single holding."""
    sym = ticker.strip().upper()
    px = price_util.last_close_yfinance(sym)
    pct = f"{(px/plan.entry_price - 1)*100:+.1f}%" if px and plan.entry_price else "unknown"
    prompt = _PROMPT.format(
        core_policy=INVESTOR_POLICY_FULL[:1500],
        catalyst_policy=CATALYST_POLICY_FULL[:1500],
        ticker=sym,
        entry=f"${plan.entry_price:.2f}",
        current=f"${px:.2f}" if px else "unknown",
        pct=pct,
        context=_research_context(cfg, sym),
    )
    llm = _classifier_llm(cfg)
    # Plain JSON-mode prompting (no forced tool_choice — DeepSeek V4 Pro rejects it in thinking mode).
    try:
        raw = llm.invoke([HumanMessage(content=prompt)])
    except Exception as e:
        logger.warning("classify_position LLM call failed for %s: %s", sym, e)
        return None
    content = getattr(raw, "content", str(raw))
    if isinstance(content, list):  # some providers return content blocks
        content = "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
        ) or str(content)
    obj = _extract_json_object(content)
    if not obj:
        logger.warning("classify_position: no JSON parsed for %s", sym)
        return None
    obj.setdefault("ticker", sym)
    try:
        return PositionClassification.model_validate(obj)
    except Exception as e:
        logger.warning("classify_position: invalid JSON shape for %s: %s", sym, e)
        return None


def _apply_classification(cfg: Dict[str, Any], plan: PositionPlan, c: PositionClassification) -> PositionPlan:
    strategy = (c.strategy or "core").strip().lower()
    if strategy not in ("core", "catalyst"):
        strategy = "core"
    plan.strategy = strategy
    if c.thesis_break_metrics:
        plan.thesis_break_metrics = [m.strip() for m in c.thesis_break_metrics if m.strip()][:3]
    if strategy == "catalyst" and c.catalyst_description:
        plan.catalyst_description = c.catalyst_description.strip()
    note = (plan.notes or "").split(" | classified")[0].strip()
    plan.notes = (note + f" | classified: {strategy}, rec={c.recommendation}").strip(" |")
    upsert_position_plan(cfg, plan)
    return plan


def classify_all_holdings(
    cfg: Dict[str, Any],
    *,
    only_missing: bool = False,
) -> Dict[str, Any]:
    """Classify every holding that has a position plan. Writes results back to the plans.

    only_missing: if True, skip plans that already have thesis_break_metrics set.
    Returns a summary with per-ticker classifications and any sell recommendations.
    """
    plans = load_position_plans(cfg)
    if not plans:
        return {"classified": [], "sell_flags": [], "skipped": [], "reason": "no position plans on file"}

    classified: List[Dict[str, Any]] = []
    sell_flags: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    for ticker, plan in sorted(plans.items()):
        if only_missing and plan.thesis_break_metrics:
            skipped.append(ticker)
            continue
        c = classify_position(cfg, ticker, plan)
        if c is None:
            failed.append(ticker)
            if (plan.strategy or "core") == "catalyst":
                logger.warning(
                    "classify_position failed for catalyst position %s — "
                    "exit rules may be wrong (core stops applied instead of -8%% hard stop). "
                    "Re-run: position-plan classify --ticker %s",
                    ticker, ticker,
                )
                try:
                    from tradingagents.portfolio_advisor import messaging
                    messaging.send_advisor_message(
                        cfg,
                        f"Advisor: classifier failed for catalyst {ticker}",
                        f"{ticker} is tagged catalyst but classification failed — "
                        f"the -8% hard stop rule will NOT fire until it is reclassified. "
                        f"Run: advisor portfolio position-plan classify",
                        urgent=True,
                    )
                except Exception:
                    pass
            continue
        _apply_classification(cfg, plan, c)
        rec = (c.recommendation or "hold").strip().lower()
        if rec == "sell":
            sell_flags.append(ticker)
        classified.append(
            {
                "ticker": ticker,
                "strategy": plan.strategy,
                "recommendation": rec,
                "thesis_break_metrics": plan.thesis_break_metrics,
                "rationale": (c.rationale or "")[:300],
            }
        )

    try:
        append_event(
            cfg,
            {
                "ticker": "*",
                "event_type": "position_classification",
                "key_data": {
                    "classified": [c["ticker"] for c in classified],
                    "sell_flags": sell_flags,
                    "n": len(classified),
                },
                "outcome": None,
            },
        )
    except Exception:
        pass

    return {"classified": classified, "sell_flags": sell_flags, "skipped": skipped, "failed": failed}
