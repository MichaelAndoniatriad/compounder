from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data,
    get_stock_summary,
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_fundamentals_summary,
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)


def _learned_rules_excerpt_for_prompt(max_chars: int = 6000) -> str:
    from tradingagents.dataflows.config import get_config
    from tradingagents.agents.utils.learned_rules_log import read_learned_rules_excerpt

    return read_learned_rules_excerpt(get_config(), max_chars=max_chars).strip()


def _active_strategy() -> str:
    """Strategy sleeve for the current run, set on config by the deep runner ('core' default)."""
    from tradingagents.dataflows.config import get_config

    try:
        return str((get_config() or {}).get("active_strategy") or "core").strip().lower()
    except Exception:
        return "core"


def get_investor_policy_full_instruction() -> str:
    """Full exit policy + framework for decision agents, selected by the active strategy sleeve."""
    from tradingagents.agents.utils.investor_policy import policy_for_strategy

    base = (
        "\n\n---\n\n## Mandated portfolio policy (follow strictly)\n\n"
        + policy_for_strategy(_active_strategy())
    )
    learned = _learned_rules_excerpt_for_prompt(max_chars=6000)
    if learned:
        base += (
            "\n\n---\n\n## Learned rules from past outcomes "
            "(append-only log; apply when consistent with policy above)\n\n"
            + learned
        )
    return base


def get_investor_policy_analyst_supplement() -> str:
    """Short mandate context for market/news/sentiment analysts, selected by active strategy sleeve."""
    from tradingagents.agents.utils.investor_policy import analyst_supplement_for_strategy

    tail = _learned_rules_excerpt_for_prompt(max_chars=900)
    extra = ""
    if tail:
        extra = (
            "\n\n**Desk habits learned from past runs (trimmed):** "
            + tail[:900]
            + ("…" if len(tail) > 900 else "")
        )
    return "\n\n---\n" + analyst_supplement_for_strategy(_active_strategy()) + extra


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Applied to every agent whose output reaches the saved report —
    analysts, researchers, debaters, research manager, trader, and
    portfolio manager — so a non-English run produces a fully localized
    report rather than a mix of languages.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`)."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
