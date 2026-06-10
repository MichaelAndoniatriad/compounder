# TradingAgents/graph/setup.py

from typing import Any, Dict, Optional
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import *
from tradingagents.agents.utils.agent_states import AgentState

from .conditional_logic import ConditionalLogic


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: Dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
        *,
        llm_by_role: Optional[Dict[str, Any]] = None,
    ):
        """Initialize with required components.

        When ``llm_by_role`` is set (corporate hierarchy mode), each graph role
        uses its own provider/model; ``quick_thinking_llm`` / ``deep_thinking_llm``
        are still kept for backward compatibility (e.g. reflection helpers).
        """
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.llm_by_role = llm_by_role or {}
        self._corporate = bool(self.llm_by_role)
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic

    def _llm(self, role: str) -> Any:
        """Resolve the LLM for a graph role (corporate) or legacy quick/deep pools."""
        if self._corporate:
            return self.llm_by_role[role]
        if role in ("research_execution", "portfolio_manager"):
            return self.deep_thinking_llm
        return self.quick_thinking_llm

    def setup_graph(  # noqa: PLR0912
        self,
        selected_analysts=["market", "social", "news", "fundamentals"],
        *,
        debate_enabled: bool = True,
        risk_debate_enabled: bool = True,
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts: List of analyst types to include. Options are:
                - "market": Market analyst
                - "social": Social media analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
            debate_enabled: When True (default), the bull/bear researcher debate
                is included before Research And Execution. When False, analysts
                feed directly into Research And Execution — saves 2+ LLM calls
                but removes the adversarial signal.
            risk_debate_enabled: When True (default), the aggressive/conservative
                risk debate runs after Research And Execution. When False, R&E
                feeds directly into the Portfolio Manager — saves 2 LLM calls.
                Disable both flags to run the minimum viable graph (analysts →
                R&E → PM) when scoreboard data shows debates add no alpha.
        """
        if len(selected_analysts) == 0:
            raise ValueError("Trading Agents Graph Setup Error: no analysts selected!")

        # Create analyst nodes
        analyst_nodes = {}
        delete_nodes = {}
        tool_nodes = {}

        if "market" in selected_analysts:
            analyst_nodes["market"] = create_market_analyst(
                self._llm("market")
            )
            delete_nodes["market"] = create_msg_delete()
            tool_nodes["market"] = self.tool_nodes["market"]

        if "social" in selected_analysts:
            # "social" selector key preserved for back-compat with existing
            # user configs; the underlying agent has been renamed to
            # sentiment_analyst (the old name advertised social-media data
            # the agent never had access to — see issue #557).
            analyst_nodes["social"] = create_sentiment_analyst(
                self._llm("social")
            )
            delete_nodes["social"] = create_msg_delete()
            tool_nodes["social"] = self.tool_nodes["social"]

        if "news" in selected_analysts:
            analyst_nodes["news"] = create_news_analyst(
                self._llm("news")
            )
            delete_nodes["news"] = create_msg_delete()
            tool_nodes["news"] = self.tool_nodes["news"]

        if "fundamentals" in selected_analysts:
            analyst_nodes["fundamentals"] = create_fundamentals_analyst(
                self._llm("fundamentals")
            )
            delete_nodes["fundamentals"] = create_msg_delete()
            tool_nodes["fundamentals"] = self.tool_nodes["fundamentals"]

        # Research And Execution: combined Research Manager + Trader (single LLM call).
        research_and_execution_node = create_research_and_execution_agent(
            self._llm("research_execution")
        )

        # Risk analysis nodes (neutral debator removed — aggressive ↔ conservative only).
        aggressive_analyst = create_aggressive_debator(self._llm("risk_aggressive"))
        conservative_analyst = create_conservative_debator(self._llm("risk_conservative"))
        portfolio_manager_node = create_portfolio_manager(self._llm("portfolio_manager"))

        # Create workflow
        workflow = StateGraph(AgentState)

        # Add analyst nodes to the graph
        for analyst_type, node in analyst_nodes.items():
            workflow.add_node(f"{analyst_type.capitalize()} Analyst", node)
            workflow.add_node(
                f"Msg Clear {analyst_type.capitalize()}", delete_nodes[analyst_type]
            )
            workflow.add_node(f"tools_{analyst_type}", tool_nodes[analyst_type])

        # Add decision nodes
        workflow.add_node("Research And Execution", research_and_execution_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # Define edges
        first_analyst = selected_analysts[0]
        workflow.add_edge(START, f"{first_analyst.capitalize()} Analyst")

        # Connect analysts in sequence; last analyst leads to debate or straight to R&E.
        for i, analyst_type in enumerate(selected_analysts):
            current_analyst = f"{analyst_type.capitalize()} Analyst"
            current_tools = f"tools_{analyst_type}"
            current_clear = f"Msg Clear {analyst_type.capitalize()}"

            workflow.add_conditional_edges(
                current_analyst,
                getattr(self.conditional_logic, f"should_continue_{analyst_type}"),
                [current_tools, current_clear],
            )
            workflow.add_edge(current_tools, current_analyst)

            if i < len(selected_analysts) - 1:
                next_analyst = f"{selected_analysts[i+1].capitalize()} Analyst"
                workflow.add_edge(current_clear, next_analyst)
            else:
                # Last analyst: go to debate or directly to Research And Execution.
                if debate_enabled:
                    workflow.add_edge(current_clear, "Bull Researcher")
                else:
                    workflow.add_edge(current_clear, "Research And Execution")

        # Bull/Bear debate (only wired when debate_enabled=True).
        if debate_enabled:
            bull_researcher_node = create_bull_researcher(self._llm("bull"))
            bear_researcher_node = create_bear_researcher(self._llm("bear"))
            workflow.add_node("Bull Researcher", bull_researcher_node)
            workflow.add_node("Bear Researcher", bear_researcher_node)

            workflow.add_conditional_edges(
                "Bull Researcher",
                self.conditional_logic.should_continue_debate,
                {
                    "Bear Researcher": "Bear Researcher",
                    "Research And Execution": "Research And Execution",
                },
            )
            workflow.add_conditional_edges(
                "Bear Researcher",
                self.conditional_logic.should_continue_debate,
                {
                    "Bull Researcher": "Bull Researcher",
                    "Research And Execution": "Research And Execution",
                },
            )

        # Research And Execution → risk debate (optional) → Portfolio Manager
        if risk_debate_enabled:
            workflow.add_edge("Research And Execution", "Aggressive Analyst")
            workflow.add_conditional_edges(
                "Aggressive Analyst",
                self.conditional_logic.should_continue_risk_analysis,
                {
                    "Conservative Analyst": "Conservative Analyst",
                    "Portfolio Manager": "Portfolio Manager",
                },
            )
            workflow.add_conditional_edges(
                "Conservative Analyst",
                self.conditional_logic.should_continue_risk_analysis,
                {
                    "Aggressive Analyst": "Aggressive Analyst",
                    "Portfolio Manager": "Portfolio Manager",
                },
            )
        else:
            workflow.add_edge("Research And Execution", "Portfolio Manager")

        workflow.add_edge("Portfolio Manager", END)

        return workflow
