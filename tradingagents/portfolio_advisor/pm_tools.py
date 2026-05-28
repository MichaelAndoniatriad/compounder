"""Tools the PM can call mid-reasoning to actually manage the book.

Bind these via ``llm.bind_tools(build_pm_tools(cfg, live_tickers))`` so the PM
can queue research, close stale action items, and look up any ticker's most
recent verdict on demand — without round-tripping through the structured
output schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from langchain_core.tools import tool

from tradingagents.portfolio_advisor import action_log as al
from tradingagents.portfolio_advisor import state as pa_state


def build_pm_tools(cfg: Dict[str, Any], live_tickers: set) -> List[Any]:
    """Return the tools bound for a single PM cycle, closed over cfg/live state."""

    @tool
    def queue_research(ticker: str, tier: str = "single_model", reason: str = "") -> str:
        """Queue a research job for any ticker (holding or watchlist candidate).

        Use this when you want fresh evidence before recommending a stance,
        when you want to validate a candidate, or when a holding's research
        is stale. Tier is "single_model" (cheap thesis check) or "full_graph"
        (full 12-agent deep dive, ~$0.5–1). Returns a confirmation + job id.
        """
        tk = (ticker or "").strip().upper()
        if not tk:
            return "error: empty ticker"
        if tier not in ("single_model", "full_graph"):
            tier = "single_model"
        now = datetime.now(timezone.utc)
        st = pa_state.load_state(cfg)
        # Light dedup: skip if a pending job with the same (ticker, tier) exists.
        for j in (st.get("jobs") or []):
            if (
                str(j.get("ticker") or "").strip().upper() == tk
                and str(j.get("execution_tier") or "") == tier
                and str(j.get("status") or "") == "pending"
            ):
                return f"already pending: {tk} {tier} (id={j.get('id')})"
        job = {
            "id": uuid.uuid4().hex[:20],
            "ticker": tk,
            "scheduled_at": (now + timedelta(minutes=1)).isoformat(),
            "kind": "deep_research",
            "reason": (reason or "PM tool-call request").strip()[:500],
            "status": "pending",
            "created_at": now.isoformat(),
            "execution_tier": tier,
            "job_type": "thesis_check",
            "source": "pm_tool_call",
        }
        st.setdefault("jobs", []).append(job)
        pa_state.save_state(cfg, st)
        return f"queued: {tk} {tier} (id={job['id']}, reason={reason!r})"

    @tool
    def mark_action_done(ticker: str) -> str:
        """Close any open SELL/TRIM action items in the action log for this ticker.

        Use this when the latest evidence overrides a prior sell call (e.g.,
        the human told you to hold, or a fresh full_graph flipped to Hold),
        so the morning/evening digest stops nagging about the stale action.
        Returns how many were closed.
        """
        tk = (ticker or "").strip().upper()
        if not tk:
            return "error: empty ticker"
        n = al.mark_done(cfg, tk)
        return (
            f"closed {n} open action(s) for {tk}"
            if n
            else f"no open actions to close for {tk}"
        )

    @tool
    def get_recent_research(ticker: str, days: int = 14) -> str:
        """Look up the most recent full_graph or single_model research verdict
        for any ticker (holdings or watchlist) within the last N days.

        Returns the verdict excerpt or "no recent research" if nothing found.
        """
        tk = (ticker or "").strip().upper()
        if not tk:
            return "error: empty ticker"
        try:
            from tradingagents.agents.utils.event_log import _iter_events
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days or 14))).isoformat()
            latest = None
            for row in _iter_events(cfg, max_lines=5000):
                ts = str(row.get("timestamp") or "")
                if ts < cutoff:
                    continue
                if str(row.get("ticker") or "").strip().upper() != tk:
                    continue
                et = str(row.get("event_type") or "")
                if et not in ("full_graph_decision", "single_model_analysis"):
                    continue
                if latest is None or ts > str(latest.get("timestamp", "")):
                    latest = row
            if latest is None:
                return f"no recent research for {tk} in last {int(days)}d"
            kd = latest.get("key_data") or {}
            excerpt = (kd.get("excerpt") or kd.get("decision") or "")
            excerpt = str(excerpt)[:400].replace("\n", " ").strip()
            return f"{tk} [{latest.get('event_type')} {str(latest.get('timestamp',''))[:10]}]: {excerpt}"
        except Exception as e:
            return f"error reading research log: {e}"

    return [queue_research, mark_action_done, get_recent_research]
