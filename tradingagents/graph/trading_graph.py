# TradingAgents/graph/trading_graph.py

import logging
import os
from pathlib import Path
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import yfinance as yf

logger = logging.getLogger(__name__)

from langgraph.prebuilt import ToolNode

from tradingagents.llm_clients import create_llm_client
from tradingagents.llm_clients.corporate_llm_factory import build_corporate_hierarchy_llms

from tradingagents.agents import *
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from tradingagents.dataflows.config import set_config

# Import the new abstract tool methods from agent_utils
from tradingagents.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_news,
    get_insider_transactions,
    get_global_news
)

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .setup import GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals"],
        debug=False,
        config: Dict[str, Any] = None,
        callbacks: Optional[List] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            callbacks: Optional list of callback handlers (e.g., for tracking LLM/tool stats)
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.callbacks = callbacks or []

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        llm_kwargs = self._get_provider_kwargs()
        if self.callbacks:
            llm_kwargs["callbacks"] = self.callbacks

        if self.config.get("corporate_hierarchy_enabled"):
            self.llm_by_role = build_corporate_hierarchy_llms(self.config, self.callbacks)
            self.quick_thinking_llm = self.llm_by_role["reflection"]
            self.deep_thinking_llm = self.llm_by_role["research_manager"]
        elif (
            self.config.get("analyst_llm")
            and self.config.get("debate_llm")
            and self.config.get("execution_llm")
        ):
            # Tiered routing: three model tiers, all via the configured provider.
            # Build one client per tier then fan out to per-role LLM instances.
            analyst_client = create_llm_client(
                provider=self.config["llm_provider"],
                model=self.config["analyst_llm"],
                base_url=self.config.get("backend_url"),
                **llm_kwargs,
            )
            debate_client = create_llm_client(
                provider=self.config["llm_provider"],
                model=self.config["debate_llm"],
                base_url=self.config.get("backend_url"),
                **llm_kwargs,
            )
            execution_client = create_llm_client(
                provider=self.config["llm_provider"],
                model=self.config["execution_llm"],
                base_url=self.config.get("backend_url"),
                **llm_kwargs,
            )
            analyst_llm = analyst_client.get_llm()
            debate_llm = debate_client.get_llm()
            execution_llm = execution_client.get_llm()

            _ANALYST_ROLES = ("market", "social", "news", "fundamentals")
            _DEBATE_ROLES = (
                "bull", "bear", "trader", "research_manager", "research_execution",
                "risk_aggressive", "risk_neutral", "risk_conservative",
            )
            self.llm_by_role = {}
            for role in _ANALYST_ROLES:
                self.llm_by_role[role] = analyst_llm
            for role in _DEBATE_ROLES:
                self.llm_by_role[role] = debate_llm
            self.llm_by_role["portfolio_manager"] = execution_llm
            # reflection helper uses the debate-tier model (cheap but capable)
            self.llm_by_role["reflection"] = debate_llm

            # Keep quick/deep references consistent for helpers outside GraphSetup
            # (Reflector, SignalProcessor, learned-rules).
            self.quick_thinking_llm = analyst_llm
            self.deep_thinking_llm = execution_llm
        else:
            self.llm_by_role = None
            deep_client = create_llm_client(
                provider=self.config["llm_provider"],
                model=self.config["deep_think_llm"],
                base_url=self.config.get("backend_url"),
                **llm_kwargs,
            )
            quick_client = create_llm_client(
                provider=self.config["llm_provider"],
                model=self.config["quick_think_llm"],
                base_url=self.config.get("backend_url"),
                **llm_kwargs,
            )
            self.deep_thinking_llm = deep_client.get_llm()
            self.quick_thinking_llm = quick_client.get_llm()

        self.memory_log = TradingMemoryLog(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
            llm_by_role=self.llm_by_role,
        )

        self.propagator = Propagator(
            max_recur_limit=self.config.get("max_recur_limit", 100),
        )
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Set up the graph: keep the workflow for recompilation with a checkpointer.
        self.workflow = self.graph_setup.setup_graph(
            selected_analysts,
            debate_enabled=self.config.get("debate_enabled", True),
            risk_debate_enabled=self.config.get("risk_debate_enabled", True),
        )
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    def _get_provider_kwargs(self) -> Dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        return kwargs

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        return {
            "market": ToolNode(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                ]
            ),
            "social": ToolNode(
                [
                    # News tools for social media analysis
                    get_news,
                ]
            ),
            "news": ToolNode(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    # Fundamental analysis tools
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                ]
            ),
        }

    def _resolve_benchmark(self, ticker: str) -> str:
        """Pick the benchmark ticker for alpha calculation against ``ticker``.

        ``config["benchmark_ticker"]`` overrides everything when set; otherwise
        the suffix map matches the ticker's exchange suffix (e.g. ``.T`` for
        Tokyo). US-listed tickers without a dotted suffix fall through to the
        empty-suffix entry (SPY by default). Unrecognised suffixes (including
        US tickers with dots like ``BRK.B``) also fall back to the empty-suffix
        entry, which is the right default because the alpha calculation works
        in USD.
        """
        explicit = self.config.get("benchmark_ticker")
        if explicit:
            return explicit
        benchmark_map = self.config.get("benchmark_map", {})
        ticker_upper = ticker.upper()
        for suffix, benchmark in benchmark_map.items():
            if suffix and ticker_upper.endswith(suffix.upper()):
                return benchmark
        return benchmark_map.get("", "SPY")

    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int = 5,
        benchmark: str = "SPY",
    ) -> Tuple[Optional[float], Optional[float], Optional[int]]:
        """Fetch raw and alpha return for ticker over holding_days from trade_date.

        ``benchmark`` is the index used as the alpha baseline (resolved by the
        caller via ``_resolve_benchmark``). Returns ``(raw_return, alpha_return,
        actual_holding_days)`` or ``(None, None, None)`` if price data is
        unavailable (too recent, delisted, or network error).
        """
        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            end = start + timedelta(days=holding_days + 7)  # buffer for weekends/holidays
            end_str = end.strftime("%Y-%m-%d")

            stock = yf.Ticker(ticker).history(start=trade_date, end=end_str)
            bench = yf.Ticker(benchmark).history(start=trade_date, end=end_str)

            if len(stock) < 2 or len(bench) < 2:
                return None, None, None

            actual_days = min(holding_days, len(stock) - 1, len(bench) - 1)
            raw = float(
                (stock["Close"].iloc[actual_days] - stock["Close"].iloc[0])
                / stock["Close"].iloc[0]
            )
            bench_ret = float(
                (bench["Close"].iloc[actual_days] - bench["Close"].iloc[0])
                / bench["Close"].iloc[0]
            )
            alpha = raw - bench_ret
            return raw, alpha, actual_days
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s vs %s (will retry next run): %s",
                ticker, trade_date, benchmark, e,
            )
            return None, None, None

    def _resolve_pending_entries(self, ticker: str) -> None:
        """Resolve pending log entries for ticker at the start of a new run.

        Fetches returns for each same-ticker pending entry, generates reflections,
        then writes all updates in a single atomic batch write to avoid redundant I/O.
        Skips entries whose price data is not yet available (too recent or delisted).

        Trade-off: only same-ticker entries are resolved per run.  Entries for
        other tickers accumulate until that ticker is run again.
        """
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return

        reflection_enabled = bool(self.config.get("reflection_per_run_enabled", True))
        benchmark = self._resolve_benchmark(ticker)
        updates = []
        for entry in pending:
            raw, alpha, days = self._fetch_returns(
                ticker, entry["date"], benchmark=benchmark,
            )
            if raw is None:
                continue  # price not available yet — try again next run
            if reflection_enabled:
                reflection = self.reflector.reflect_on_final_decision(
                    final_decision=entry.get("decision", ""),
                    raw_return=raw,
                    alpha_return=alpha,
                    benchmark_name=benchmark,
                )
            else:
                reflection = ""  # numeric outcome still recorded; LLM reflection deferred
            updates.append({
                "ticker": ticker,
                "trade_date": entry["date"],
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": days,
                "reflection": reflection,
            })

        if updates:
            self.memory_log.batch_update_with_outcomes(updates)
            if self.config.get("learned_rules_enabled", True):
                from tradingagents.agents.utils.learned_rules_log import (
                    maybe_extend_learned_rules_from_outcome,
                )

                for u in updates:
                    try:
                        maybe_extend_learned_rules_from_outcome(
                            self.config,
                            self.quick_thinking_llm,
                            u,
                            benchmark,
                        )
                    except Exception:
                        logger.debug(
                            "learned-rules extension failed for %s %s",
                            u.get("ticker"),
                            u.get("trade_date"),
                            exc_info=True,
                        )

    def propagate(self, company_name, trade_date):
        """Run the trading agents graph for a company on a specific date.

        When ``checkpoint_enabled`` is set in config, the graph is recompiled
        with a per-ticker SqliteSaver so a crashed run can resume from the last
        successful node on a subsequent invocation with the same ticker+date.
        """
        self.ticker = company_name

        # Resolve any pending memory-log entries for this ticker before the pipeline runs.
        self._resolve_pending_entries(company_name)

        # Recompile with a checkpointer if the user opted in.
        if self.config.get("checkpoint_enabled"):
            self._checkpointer_ctx = get_checkpointer(
                self.config["data_cache_dir"], company_name
            )
            saver = self._checkpointer_ctx.__enter__()
            self.graph = self.workflow.compile(checkpointer=saver)

            step = checkpoint_step(
                self.config["data_cache_dir"], company_name, str(trade_date)
            )
            if step is not None:
                logger.info(
                    "Resuming from step %d for %s on %s", step, company_name, trade_date
                )
            else:
                logger.info("Starting fresh for %s on %s", company_name, trade_date)

        try:
            return self._run_graph(company_name, trade_date)
        finally:
            if self._checkpointer_ctx is not None:
                self._checkpointer_ctx.__exit__(None, None, None)
                self._checkpointer_ctx = None
                self.graph = self.workflow.compile()

    def _run_graph(self, company_name, trade_date):
        """Execute the graph and write the resulting state to disk and memory log."""
        # Initialize state — inject markdown memory + JSONL event tail for PM.
        lb = self.config.get("memory_context_lookback_days")
        try:
            lb_i = int(lb) if lb is not None else None
        except (TypeError, ValueError):
            lb_i = None
        mx_s = int(self.config.get("memory_context_max_same_ticker", 8))
        mx_c = int(self.config.get("memory_context_max_cross_ticker", 3))
        past_md = self.memory_log.get_past_context(
            company_name, mx_s, mx_c, lookback_days=lb_i
        )
        from tradingagents.agents.utils.event_log import format_recent_events_for_ticker

        try:
            ev_days = int(self.config.get("memory_event_log_prompt_days", 30))
        except (TypeError, ValueError):
            ev_days = 30
        past_ev = ""
        try:
            past_ev = format_recent_events_for_ticker(
                self.config, company_name, days=ev_days, max_events=25
            )
        except Exception:
            logger.debug("event log prompt tail skipped", exc_info=True)
        prior_block = ""
        if self.config.get("portfolio_advisor_inject_prior_clerk_report", True):
            try:
                from tradingagents.clerk.deep_runner import load_latest_prior_clerk_report_text

                try:
                    mx = int(self.config.get("portfolio_advisor_prior_clerk_report_max_chars") or 16_000)
                except (TypeError, ValueError):
                    mx = 16_000
                rd = Path(str(self.config.get("results_dir", "."))).expanduser()
                blob = load_latest_prior_clerk_report_text(
                    results_dir=rd, ticker=str(company_name), max_chars=mx
                )
                if blob.strip():
                    prior_block = (
                        "## Prior automated deep-research snapshot (latest on disk)\n"
                        "Continue from this narrative where it helps; validate everything with fresh data/tools.\n\n"
                        + blob.strip()
                    )
            except Exception:
                logger.debug("prior clerk snapshot inject skipped", exc_info=True)
        past_context = "\n\n".join(
            x for x in (past_md, past_ev, prior_block) if (x or "").strip()
        )
        init_agent_state = self.propagator.create_initial_state(
            company_name, trade_date, past_context=past_context
        )
        args = self.propagator.get_graph_args()

        # Inject thread_id so same ticker+date resumes, different date starts fresh.
        if self.config.get("checkpoint_enabled"):
            tid = thread_id(company_name, str(trade_date))
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        progress_cb = self.config.get("progress_callback")
        if self.debug:
            trace = []
            for chunk in self.graph.stream(init_agent_state, **args):
                if len(chunk.get("messages", [])) > 0:
                    chunk["messages"][-1].pretty_print()
                trace.append(chunk)
            # Streamed chunks are per-node deltas. Merge them so the returned
            # state matches what graph.invoke() yields in the non-debug path.
            final_state = {}
            for chunk in trace:
                final_state.update(chunk)
        elif progress_cb is not None and callable(progress_cb):
            final_state: Dict[str, Any] = {}
            for chunk in self.graph.stream(init_agent_state, **args):
                final_state.update(chunk)
                try:
                    progress_cb(dict(final_state), chunk)
                except Exception:
                    logger.debug("progress_callback raised", exc_info=True)
        else:
            final_state = self.graph.invoke(init_agent_state, **args)

        # Store current state for reflection.
        self.curr_state = final_state

        # Log state to disk.
        self._log_state(trade_date, final_state)

        # Store decision for deferred reflection on the next same-ticker run.
        self.memory_log.store_decision(
            ticker=company_name,
            trade_date=trade_date,
            final_trade_decision=final_state["final_trade_decision"],
        )

        decision_text = str(final_state.get("final_trade_decision") or "")
        try:
            from tradingagents.agents.utils.event_log import append_event
            from tradingagents.portfolio_advisor.ticker_decision import extract_ticker_decision

            ticker_decision = extract_ticker_decision(
                ticker=company_name,
                trade_date=str(trade_date),
                final_trade_decision=decision_text,
            )

            append_event(
                self.config,
                {
                    "ticker": company_name,
                    "event_type": "full_graph_decision",
                    "key_data": ticker_decision,
                    "outcome": None,
                },
            )
        except Exception:
            logger.debug("event log append skipped", exc_info=True)

        notify_url = (self.config.get("analysis_webhook_url") or "").strip()
        suppress_hold = bool(self.config.get("analysis_notify_suppress_hold"))

        if notify_url:
            try:
                from tradingagents.advisor.notify import send_webhook

                if suppress_hold and rating == "Hold":
                    logger.info(
                        "analysis_webhook skipped (Hold rating; analysis_notify_suppress_hold)"
                    )
                else:
                    headline = (
                        f"Advisory plan — {company_name} — {trade_date} — {rating}\n"
                        f"(not a trade; for your review)"
                    )
                    body = f"{headline}\n\n{decision_text[:12000]}"
                    send_webhook(notify_url, body)
            except Exception as e:
                logger.warning("analysis_webhook_url POST failed: %s", e)

        try:
            from tradingagents.advisor.email_notify import (
                analysis_smtp_ready,
                send_analysis_advisory_email,
            )

            if analysis_smtp_ready(self.config):
                if suppress_hold and rating == "Hold":
                    logger.info(
                        "analysis advisory email skipped (Hold rating; analysis_notify_suppress_hold)"
                    )
                else:
                    ok = send_analysis_advisory_email(
                        self.config,
                        ticker=company_name,
                        trade_date=str(trade_date),
                        decision_text=decision_text,
                        rating=rating,
                    )
                    if not ok:
                        logger.warning(
                            "analysis advisory email not sent for %s %s",
                            company_name,
                            trade_date,
                        )
        except Exception as e:
            logger.warning("analysis advisory email path failed: %s", e)

        # Clear checkpoint on successful completion to avoid stale state.
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date)
            )

        return final_state, self.process_signal(final_state["final_trade_decision"])

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "aggressive_history": final_state["risk_debate_state"]["aggressive_history"],
                "conservative_history": final_state["risk_debate_state"]["conservative_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        # Save to file. Reject ticker values that would escape the
        # results directory when joined as a path component.
        safe_ticker = safe_ticker_component(self.ticker)
        directory = Path(self.config["results_dir"]) / safe_ticker / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4)

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
