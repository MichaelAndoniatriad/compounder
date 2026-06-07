"""Research And Execution: combined Research Manager + Trader in one LLM call.

Replaces the two-node sequence (Research Manager → Trader) with a single
agent that reads the analyst debate (if any) and the four analyst reports,
then outputs both an investment plan and an immediate trade proposal.

The two render helpers produce the same markdown shape as the original
Research Manager and Trader agents so that downstream state consumers
(Portfolio Manager, _log_state, memory log) work unchanged.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.agents.schemas import (
    ResearchExecutionPlan,
    render_research_execution_plan_part,
    render_research_execution_trade_part,
)
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_investor_policy_full_instruction,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import bind_structured

logger = logging.getLogger(__name__)


def create_research_and_execution_agent(llm):
    structured_llm = bind_structured(llm, ResearchExecutionPlan, "Research And Execution")

    def research_and_execution_node(state) -> dict:
        company_name = state["company_of_interest"]
        instrument_context = build_instrument_context(company_name)
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")

        market_report = state.get("market_report", "")
        sentiment_report = state.get("sentiment_report", "")
        news_report = state.get("news_report", "")
        fundamentals_report = state.get("fundamentals_report", "")

        # Static: role + rating scale + policy — cached across all ticker runs.
        static_system = (
            "You are the Research Manager and Trader combined into a single decision agent.\n\n"
            "Your job is twofold:\n\n"
            "**Part 1 — Investment Plan:** Critically evaluate the analyst debate (if any) "
            "together with the analyst reports to produce a clear, actionable investment plan. "
            "Weigh bull and bear arguments where available; otherwise rely on the analyst "
            "reports alone.\n\n"
            "**Rating Scale** (use exactly one):\n"
            "- **Buy**: Strong conviction in the bull thesis; recommend entering or growing the position\n"
            "- **Overweight**: Constructive view; recommend gradually increasing exposure\n"
            "- **Hold**: Balanced view; maintain current position\n"
            "- **Underweight**: Cautious view; recommend trimming\n"
            "- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding\n\n"
            "Commit to a clear stance whenever the evidence warrants one; reserve Hold "
            "for situations where the evidence on both sides is genuinely balanced.\n\n"
            "**Part 2 — Trade Proposal:** Immediately translate the investment plan into a "
            "concrete transaction direction (Buy, Hold, or Sell). Anchor the reasoning in the "
            "analyst reports and the plan you just produced. Include entry price, stop-loss, "
            "and position sizing where the data supports specific levels."
            + get_investor_policy_full_instruction()
            + get_language_instruction()
        )

        debate_section = (
            f"\n\n**Debate History:**\n{history}"
            if history.strip()
            else "\n\n*(No bull/bear debate — base the decision on analyst reports only.)*"
        )

        # Dynamic: instrument context, reports, and debate history change per call.
        dynamic_user = (
            f"{instrument_context}\n\n"
            f"---\n\n"
            f"**Analyst Reports:**\n"
            f"- Market: {market_report}\n"
            f"- Sentiment: {sentiment_report}\n"
            f"- News: {news_report}\n"
            f"- Fundamentals: {fundamentals_report}"
            f"{debate_section}"
        )

        messages = [
            SystemMessage(content=[
                {"type": "text", "text": static_system, "cache_control": {"type": "ephemeral"}},
            ]),
            HumanMessage(content=dynamic_user),
        ]

        if structured_llm is not None:
            try:
                plan = structured_llm.invoke(messages)
                # Apply consensus factor: tag and modulate sizing (v4: advisory, not a gate)
                plan = _apply_consensus_factor(plan)
                investment_plan = render_research_execution_plan_part(plan)
                trader_plan = render_research_execution_trade_part(plan)
            except Exception as exc:
                logger.warning(
                    "Research And Execution: structured output failed (%s); retrying as free text",
                    exc,
                )
                response = llm.invoke(messages)
                investment_plan = trader_plan = response.content
        else:
            response = llm.invoke(messages)
            investment_plan = trader_plan = response.content

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
            "trader_investment_plan": trader_plan,
        }

    return research_and_execution_node


def _apply_consensus_factor(plan: Any) -> Any:
    """Apply consensus factor: tag and modulate sizing (v4).

    Tags every proposal with consensus score regardless of feature flag.
    Modulates sizing only when CONSENSUS_FACTOR_LIVE is true.
    Does NOT reject any proposal based on consensus.
    """
    from tradingagents.portfolio_advisor.consensus_score import compute_composite_consensus_score

    ticker = getattr(plan, "ticker", "") or getattr(plan, "recommendation", "")
    if not ticker:
        return plan

    try:
        score = compute_composite_consensus_score(str(ticker).strip().upper())
    except Exception:
        score = {"composite": 0.0, "entry": 0.0, "divergence": 0.0, "flow": 0.0}

    # Tag always
    try:
        plan.consensus_score = score
    except AttributeError:
        pass

    # Modulate sizing only when feature flag is true
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        if not DEFAULT_CONFIG.get("CONSENSUS_FACTOR_LIVE", False):
            return plan
    except Exception:
        return plan

    # Composite +1.0 = +30% sizing. Composite -1.0 = -30% sizing.
    multiplier = 1.0 + (score["composite"] * 0.3)
    try:
        current_sizing = float(getattr(plan, "sizing_usd", 0) or 0)
        plan.sizing_usd = current_sizing * multiplier
    except (AttributeError, TypeError, ValueError):
        pass

    return plan
