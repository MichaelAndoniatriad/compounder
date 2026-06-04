"""One-shot post-earnings thesis verdict (reasoning model, advisory only)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict

from langchain_core.messages import HumanMessage

from tradingagents.dataflows.config import set_config
from tradingagents.llm_clients import create_llm_client
from tradingagents.portfolio_advisor import etoro_scan, messaging, outcome_sync
from tradingagents.portfolio_advisor.prompt_limits import cfg_int

logger = logging.getLogger(__name__)


def _reasoning_client(cfg: Dict[str, Any]):
    model = (cfg.get("portfolio_advisor_reasoning_model") or "deepseek-reasoner").strip()
    provider = (cfg.get("llm_provider") or "openrouter").lower()
    if "/" in model and provider != "openrouter":
        provider = "openrouter"
    base = cfg.get("corporate_openrouter_base_url") or cfg.get("backend_url")
    return create_llm_client(provider, model, base_url=base).get_llm()


def run_post_earnings_verdict(cfg: Dict[str, Any], ticker: str) -> str:
    """Fetch a minimal context bundle and email a structured verdict for ``ticker``."""
    set_config(cfg)
    sym = ticker.strip().upper()
    if not sym:
        raise ValueError("ticker required")

    _, portfolio_text, live, rows = etoro_scan.fetch_portfolio_rows()
    live_set = etoro_scan.current_ticker_set(live)
    try:
        outcome_sync.auto_close_outcomes(cfg, live_set, rows=rows)
    except Exception:
        logger.debug("post-earnings outcome_sync skipped", exc_info=True)
    if sym not in live_set:
        raise ValueError(f"{sym} is not in the current eToro portfolio export.")

    catalyst_line = ""
    try:
        from tradingagents.portfolio_advisor import catalysts

        catalyst_line = catalysts.catalyst_block_for_tickers([sym], max_tickers=5)
    except Exception as e:
        logger.debug("catalyst snippet for post-earnings: %s", e)

    px_note = ""
    try:
        import yfinance as yf

        hist = yf.Ticker(sym).history(period="10d")
        if hist is not None and len(hist.index) > 0:
            last = float(hist["Close"].iloc[-1])
            px_note = f"Last close (best effort): {last:.4f}\n"
        else:
            px_note = "Last close: unavailable (empty history)\n"
    except Exception as e:
        px_note = f"Last close: unavailable ({e})\n"

    today = date.today().isoformat()
    pcap = cfg_int(cfg, "portfolio_advisor_post_verdict_portfolio_chars", 5500, 2000, 30000)
    prompt = f"""You are writing a post-earnings thesis verdict for one open position. Advisory only. No trade orders.

Ticker: {sym}
As-of date: {today}

Live portfolio excerpt (trimmed):
{portfolio_text[:pcap]}

{catalyst_line}

Market snapshot:
{px_note}

Deliver exactly these sections (plain text):

POST EARNINGS VERDICT {sym} {today}

VERDICT
State INTACT, WEAKENING, or BROKEN first. One line.

EARNINGS SUMMARY
3 to 4 sentences on what mattered in the most recent print versus expectations. If you lack numbers, say what is missing.

THESIS RISK
What would change your verdict before the next print?

REQUIRED ACTION
Explicit human action or none. If none, say none.

If any data above is missing, say so. Do not invent figures.
"""
    llm = _reasoning_client(cfg)
    msg = llm.invoke([HumanMessage(content=prompt)])
    content = getattr(msg, "content", str(msg))
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        content = "\n".join(parts) if parts else str(content)
    text = str(content).strip()
    subj = f"[TradingAgents] Post-earnings verdict — {sym} — {today}"
    # Post-earnings verdict is user-triggered; deliver regardless of window.
    messaging.send_advisor_message(cfg, subj, text, urgent=True)
    try:
        from tradingagents.agents.utils.event_log import append_event

        append_event(
            cfg,
            {
                "ticker": sym,
                "event_type": "post_earnings_verdict",
                "key_data": {"date": today, "excerpt": text[:800]},
                "outcome": None,
            },
        )
    except Exception:
        logger.debug("post-earnings event log skipped", exc_info=True)
    return text
