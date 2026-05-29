"""LLM portfolio planner: positions + catalysts → structured schedule."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from tradingagents.agents.utils.structured import bind_structured
from tradingagents.llm_clients import create_llm_client
from tradingagents.portfolio_advisor import catalysts
from tradingagents.portfolio_advisor.models import AdvisorPlanResult
from tradingagents.portfolio_advisor.prompt_limits import cfg_int

logger = logging.getLogger(__name__)


def _provider_kwargs(cfg: Dict[str, Any]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    prov = (cfg.get("llm_provider") or "openai").lower()
    if prov == "google" and cfg.get("google_thinking_level"):
        kwargs["thinking_level"] = cfg["google_thinking_level"]
    if prov == "openai" and cfg.get("openai_reasoning_effort"):
        kwargs["reasoning_effort"] = cfg["openai_reasoning_effort"]
    if prov == "anthropic" and cfg.get("anthropic_effort"):
        kwargs["effort"] = cfg["anthropic_effort"]
    return kwargs


def build_advisor_plan(
    cfg: Dict[str, Any],
    *,
    portfolio_text: str,
    catalyst_text: str,
    mode: str,
    tickers: list[str],
) -> AdvisorPlanResult:
    """Run planner model with structured output. ``mode`` is ``init`` or ``replan``."""
    provider = (cfg.get("llm_provider") or "openai").lower()
    raw_model = cfg.get("portfolio_advisor_planner_model")
    if isinstance(raw_model, str) and raw_model.strip():
        model = raw_model.strip()
    else:
        model = (cfg.get("quick_think_llm") or "gpt-5.4-mini").strip()
    client = create_llm_client(
        provider=provider,
        model=model,
        base_url=cfg.get("backend_url"),
        **_provider_kwargs(cfg),
    )
    llm = client.get_llm()
    structured = bind_structured(llm, AdvisorPlanResult, "PortfolioAdvisor")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_jobs = int(cfg.get("portfolio_advisor_max_jobs_per_plan") or 15)
    ramp_days = int(cfg.get("portfolio_advisor_earnings_ramp_days") or 7)
    ramp_block = catalysts.earnings_ramp_block_for_tickers(tickers, ramp_days)
    pcap = cfg_int(cfg, "portfolio_advisor_planner_portfolio_chars", 10000, 4000, 50000)
    ccap = cfg_int(cfg, "portfolio_advisor_planner_catalyst_chars", 7000, 2000, 50000)
    prompt = f"""You are an autonomous portfolio advisor (not a broker). {mode.upper()} scheduling scan.

Now (UTC): {now}

Rules:
- Output advisory planning only — never claim you executed trades.
- Schedule at most {max_jobs} deep_research jobs total; stagger times across the next 14 days in UTC.
- Prioritize names with nearer catalysts, larger notional risk, or stale research needs.
- Names with earnings inside the ramp window below deserve tighter scheduling or watch_only with clear rationale.
- Use action watch_only when a full graph run is not worth cost this week (still explain).
- Use action skip to omit noise.
- scheduled_at must be ISO-8601 with timezone offset (prefer Z or +00:00).
- For every deep_research job set execution_tier to single_model when the task is a light thesis check, a weekly style summary recap, post earnings review with a likely clear verdict, or routine monitoring with stable metrics. The safe default when unsure is single_model because it is cheaper than the full graph. Use full_graph only when the book shows a new name, when thesis break levels are undefined or in dispute, when post print ambiguity is high, when drawdown risk already signals stress, or when any validator would need the full multi agent stack.
- Set job_type on each deep_research job to exactly one of Literal["thesis_check", "weekly_summary", "post_earnings", "routine_monitoring", "catalyst_scan"]. Match the value to the narrative in rationale; the schema rejects any other string. Use catalyst_scan for watchlist candidates when the catalyst sleeve is empty — it asks specifically what near-term event makes the name worth a short-term entry rather than evaluating long-term thesis.
- Leave flags as an empty list unless you add short freeform tags you want surfaced in metadata.

Portfolio snapshot:
{portfolio_text[:pcap]}

Earnings proximity (ramp window):
{ramp_block}

Catalyst hints (best-effort; verify in research):
{catalyst_text[:ccap]}
"""
    if structured is not None:
        try:
            return structured.invoke(prompt)
        except Exception as e:
            logger.warning("Portfolio advisor structured plan failed: %s", e)
    # Free-text fallback: minimal plan
    raw = llm.invoke(prompt)
    content = getattr(raw, "content", str(raw))
    return AdvisorPlanResult(
        executive_summary=str(content)[:4000],
        jobs=[],
        immediate_actions=["Planner returned unstructured output; run again or check model."],
    )
