"""Monthly-style review over JSONL event log (optional reasoning LLM)."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict

from langchain_core.messages import HumanMessage

from tradingagents.agents.utils.event_log import load_events_for_review
from tradingagents.dataflows.config import set_config
from tradingagents.llm_clients import create_llm_client
from tradingagents.portfolio_advisor import messaging
from tradingagents.portfolio_advisor.prompt_limits import cfg_int

logger = logging.getLogger(__name__)


_OUTPUT_RULES = """Rules for this output (apply without exception):
- No dashes of any kind. No em dashes, en dashes, hyphens as separators or list bullets.
- No filler words (just, really, basically, simply, notably).
- No AI patterns (leverage, seamlessly, robust, comprehensive, actionable, transformative, it is worth noting, furthermore).
- Verdicts before reasoning. State the conclusion first.
- If a data point is missing, say so explicitly. Do not estimate or invent figures."""


def run_memory_review(cfg: Dict[str, Any], *, lookback_days: int = 120) -> str:
    """Summarize recent events; email result. Uses reasoning model when configured."""
    set_config(cfg)
    rows = load_events_for_review(cfg, days=int(lookback_days))
    if not rows:
        body = f"No event log rows in the last {lookback_days} days. Path uses event_log_path or memory sibling."
        messaging.send_advisor_message(cfg, "[TradingAgents] Memory review (empty)", body)
        return body

    # Compact histogram without LLM
    counts: Dict[str, int] = {}
    for r in rows:
        et = str(r.get("event_type") or "?")
        counts[et] = counts.get(et, 0) + 1
    hist = "\n".join(f"- {k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    n = cfg_int(cfg, "portfolio_advisor_memory_review_sample_events", 36, 10, 80)
    jcap = cfg_int(cfg, "portfolio_advisor_memory_review_json_chars", 11000, 2000, 120000)
    sample = json.dumps(rows[-n:], separators=(",", ":"), ensure_ascii=False)[:jcap]

    model = (cfg.get("portfolio_advisor_reasoning_model") or "deepseek-reasoner").strip()
    provider = "openrouter" if "/" in model else (cfg.get("llm_provider") or "openrouter").lower()
    base = cfg.get("corporate_openrouter_base_url") or cfg.get("backend_url")
    today = date.today().isoformat()
    prompt = f"""You are reviewing a machine event log for a trading research stack. Advisory only. No trade orders.
Lookback: last {lookback_days} days.

Event counts by type:
{hist}

Last {n} raw events (compact JSON):
{sample}

Today (UTC): {today}

Deliver exactly these sections (plain text, no decorative separators):

MEMORY REVIEW {today}

CADENCE CHECK
Up to four bullets. Which event types dominate? Is the frequency healthy for an active portfolio?
Flag any type that is missing entirely but should be present (e.g. no outcome_recorded events means the feedback loop is not closing).
If the log is too sparse to infer patterns, say so explicitly and stop here.

TICKER FLAGS
Up to four bullets. Any ticker with repeated failures, missing follow-ups, or no full_graph_decision in the window?
Format each bullet: TICKER, observation, suggested action.
If no flags: none.

WORKFLOW GAP
One concrete suggestion to tighten the human workflow. Not model hype. Specific and actionable.
If nothing stands out: none.

DATA GAPS
Any event types present but unreadable, malformed, or missing expected fields?
If none: none.

{_OUTPUT_RULES}
"""
    try:
        llm = create_llm_client(provider, model, base_url=base).get_llm()
        msg = llm.invoke([HumanMessage(content=prompt)])
        narrative = getattr(msg, "content", str(msg))
        if isinstance(narrative, list):
            bits = []
            for block in narrative:
                if isinstance(block, dict) and block.get("type") == "text":
                    bits.append(block.get("text", ""))
            narrative = "\n".join(bits) if bits else str(narrative)
        narrative = str(narrative).strip()
    except Exception as e:
        logger.warning("memory review LLM failed: %s", e)
        narrative = f"(LLM narrative skipped: {e})"

    body = f"--- Event histogram ---\n{hist}\n\n--- Review ---\n{narrative}"
    messaging.send_advisor_message(
        cfg,
        f"[TradingAgents] Memory review — {len(rows)} events / {lookback_days}d",
        body[:50000],
    )
    return body
