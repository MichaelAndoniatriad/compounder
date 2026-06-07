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
                # Apply consensus guardrail priority hierarchy before rendering
                plan = _apply_priority_hierarchy(plan)
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


def _apply_priority_hierarchy(plan: Any) -> Any:
    """Apply consensus guardrail priority hierarchy.

    If system is in defensive mode, reject new entries into consensus top 20.
    Lower-priority rules apply to the residual as normal.
    """
    try:
        from tradingagents.portfolio_advisor.state import load_system_mode
        mode = load_system_mode()
    except Exception:
        return plan

    if mode != "consensus_defensive":
        return plan

    try:
        from tradingagents.dataflows.llm_consensus import load_llm_consensus_snapshot
        consensus = load_llm_consensus_snapshot()
    except Exception:
        return plan

    if consensus is None:
        return plan

    top_20 = {t["ticker"] for t in consensus.get("top_20", [])}
    ticker = getattr(plan, "ticker", "") or getattr(plan, "recommendation", "")

    # If the plan recommends a BUY/OVERWEIGHT on a consensus name, reject it
    if ticker and str(ticker).strip().upper() in top_20:
        rating = str(getattr(plan, "rating", "") or getattr(plan, "recommendation", "") or "")
        if rating.upper() in ("BUY", "OVERWEIGHT"):
            try:
                plan.rating = "Hold"
                plan.recommendation = "Hold"
                plan.rejection_reason = (
                    f"Defensive mode active: new consensus entries blocked. "
                    f"{ticker} is in LLM consensus top 20."
                )
            except AttributeError:
                pass

    return plan
