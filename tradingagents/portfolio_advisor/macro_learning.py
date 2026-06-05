"""Macro learning review — extracts durable portfolio rules from market event memory.

Runs weekly (Saturday) or on-demand. Scans recent market events for recurring
patterns, sends them to the LLM for rule extraction, and appends learned rules
to _portfolio.md so the PM internalises macro patterns permanently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tradingagents.portfolio_advisor.market_memory import (
    build_market_memory_block,
    load_recent_market_events,
)
from tradingagents.portfolio_advisor.pm_workspace import (
    portfolio_rules_path,
    update_position_memory,
)

logger = logging.getLogger(__name__)

_MACRO_LEARNING_PROMPT = """You are a macro trading strategist reviewing recent market events
that impacted this portfolio. Your job: extract DUrable, reusable rules from
recurring patterns. Rules must be SPECIFIC and ACTIONABLE — not vague advice.

A good rule:
- Names the trigger condition precisely (e.g. "SPY down 2%+ pre-market on tariff news")
- Prescribes a concrete action (e.g. "halve all new EP entry sizes until VIX < 25")
- Has a clear expiry or review condition (e.g. "review after 30 days")
- References what happened last time (e.g. "Jun 3 tariff rout: NVDA -11% intraday, fully recovered in 2 sessions")

A bad rule:
- "Be careful during volatility" (too vague)
- "Consider reducing risk" (no action specified)
- "Tariffs are bad for tech" (descriptive, not prescriptive)

Rules should be written in the style of the existing _portfolio.md document.
Prepend "## Macro-learned rules" section if it doesn't exist, then add each
rule as a bullet under that heading with the date it was learned.

Below are the market events. Extract 0-3 rules maximum. If no clear pattern
exists, say "NO_RULES" and explain why."""


def run_macro_learning_review(cfg: Dict[str, Any]) -> str:
    """Scan recent market events and extract durable portfolio rules via LLM.

    Returns a status string describing what was learned (or why nothing was).
    """
    events = load_recent_market_events(cfg, days=90)
    if len(events) < 3:
        return "macro learning: skipped — fewer than 3 market events in 90 days"

    memory_block = build_market_memory_block(cfg, days=90)
    if not memory_block.strip():
        return "macro learning: skipped — no market memory block generated"

    # Build the LLM call
    llm_result = _call_llm_for_rules(cfg, memory_block)
    if not llm_result or "NO_RULES" in llm_result:
        return f"macro learning: no new rules extracted — {llm_result or 'LLM returned empty'}"

    # Append rules to _portfolio.md
    rules_written = _append_rules(cfg, llm_result)
    if rules_written:
        return f"macro learning: {rules_written} new macro rule(s) written to _portfolio.md"
    return "macro learning: rules extracted but write failed"


def _call_llm_for_rules(cfg: Dict[str, Any], memory_block: str) -> Optional[str]:
    """Send market events to the LLM and return extracted rules text."""
    try:
        from tradingagents.llm_clients.corporate_llm_factory import build_corporate_hierarchy_llms

        # Use the quick-thinking LLM (cheap) — pattern extraction doesn't need deep reasoning
        llms = build_corporate_hierarchy_llms(cfg, callbacks=[])
        llm = llms.get("reflection") or llms.get("market_analyst")
        if llm is None:
            logger.warning("macro learning: no LLM available from corporate hierarchy")
            return None

        prompt = (
            f"{_MACRO_LEARNING_PROMPT}\n\n"
            f"=== MARKET EVENTS (LAST 90 DAYS) ===\n\n"
            f"{memory_block}\n\n"
            f"Extract 0-3 durable portfolio rules from these events:\n"
        )
        response = llm.invoke(prompt)
        content = getattr(response, "content", str(response))
        return str(content).strip()
    except Exception as e:
        logger.warning("macro learning: LLM call failed: %s", e)
        return None


def _append_rules(cfg: Dict[str, Any], rules_text: str) -> int:
    """Append extracted rules to _portfolio.md. Returns count of rules added."""
    if not rules_text or len(rules_text) < 10:
        return 0

    path = portfolio_rules_path(cfg)
    existing = ""
    if path.is_file():
        existing = path.read_text(encoding="utf-8")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build the new section
    if "## Macro-learned rules" not in existing:
        header = (
            "\n\n## Macro-learned rules\n\n"
            "Rules extracted from recurring macro patterns in portfolio history. "
            "These are DUrable: they persist across sessions and override "
            "general heuristics when the trigger condition matches.\n\n"
        )
        separator = ""
    else:
        header = ""
        separator = "\n"

    # Clean the LLM output — only keep bullet lines and brief context
    lines = rules_text.split("\n")
    cleaned: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("NO_RULES"):
            return 0
        # Keep only bullet-like lines and short non-bullet context
        if stripped.startswith("-") or stripped.startswith("*"):
            cleaned.append(stripped)
        elif len(stripped) < 120 and any(
            kw in stripped.lower()
            for kw in ["rule", "when", "if", "reduce", "increase", "hold", "exit", "size"]
        ):
            cleaned.append(stripped)

    if not cleaned:
        return 0

    rule_block = "\n".join(f"  {line}" if not line.startswith("-") else line for line in cleaned)
    new_section = f"{header}{separator}### {today}\n{rule_block}\n"

    path.write_text(existing + new_section, encoding="utf-8")
    return len(cleaned)
