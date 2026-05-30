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

    @tool
    def compare_candidates(ticker_a: str, ticker_b: str, days: int = 21) -> str:
        """Fetch the latest research verdict for two tickers side-by-side so you can
        decide which is the stronger move. Useful when both are watchlist candidates
        you might swap one of them in, or comparing a candidate against a holding.
        Returns both verdict excerpts in one block.
        """
        a = get_recent_research.invoke({"ticker": ticker_a, "days": days})
        b = get_recent_research.invoke({"ticker": ticker_b, "days": days})
        return f"A. {a}\n\nB. {b}"

    @tool
    def cancel_pending_job(ticker_or_job_id: str) -> str:
        """Cancel pending research jobs by ticker (cancels all pending for that name)
        or by exact job id. Returns the count cancelled.
        """
        key = (ticker_or_job_id or "").strip()
        if not key:
            return "error: empty key"
        st = pa_state.load_state(cfg)
        cancelled = 0
        for j in (st.get("jobs") or []):
            if str(j.get("status") or "") != "pending":
                continue
            if (str(j.get("id") or "") == key
                    or str(j.get("ticker") or "").strip().upper() == key.upper()):
                j["status"] = "cancelled"
                j["cancel_reason"] = "cancelled via PM tool"
                cancelled += 1
        if cancelled:
            pa_state.save_state(cfg, st)
        return f"cancelled {cancelled} pending job(s) for {key!r}"

    @tool
    def get_sleeve_mix() -> str:
        """Return the current core/catalyst/cash sleeve allocation vs the policy
        target (50/40/10). Use this whenever you're deciding whether to deploy
        cash or reshape exposure."""
        try:
            from tradingagents.portfolio_advisor.advisor_pm import _sleeve_allocation_block
            from tradingagents.portfolio_advisor import etoro_scan
            _payload, portfolio_text, _t, rows = etoro_scan.fetch_portfolio_rows()
            block = _sleeve_allocation_block(cfg, portfolio_text, rows)
            return block.strip() or "(sleeve mix unavailable)"
        except Exception as e:
            return f"error computing sleeve mix: {e}"

    @tool
    def adjust_position_plan(
        ticker: str,
        strategy: str = "",
        target_horizon: str = "",
        notes: str = "",
    ) -> str:
        """Edit the on-disk position plan for a holding. Pass empty strings for
        fields you don't want to change. ``strategy`` is 'core' or 'catalyst'
        (the sleeve). Stop-loss percentages aren't per-plan editable — they
        come from the sleeve defaults (core: -30%/-40%, catalyst: -8% etc.).
        """
        tk = (ticker or "").strip().upper()
        if not tk:
            return "error: empty ticker"
        try:
            from tradingagents.portfolio_advisor import position_plans as pp
            plans = pp.load_position_plans(cfg)
            plan = plans.get(tk)
            if plan is None:
                return f"no existing plan for {tk}; run classifier first"
            changes: List[str] = []
            strat_clean = (strategy or "").strip().lower()
            if strat_clean in ("core", "catalyst") and plan.strategy != strat_clean:
                plan.strategy = strat_clean
                changes.append(f"strategy→{strat_clean}")
            if target_horizon.strip() and plan.target_horizon != target_horizon.strip():
                plan.target_horizon = target_horizon.strip()
                changes.append(f"target_horizon→{target_horizon.strip()}")
            if notes.strip():
                plan.notes = notes.strip()
                changes.append("notes updated")
            if not changes:
                return f"{tk}: no changes (all fields empty)"
            pp.upsert_position_plan(cfg, plan)
            return f"{tk} plan updated: {', '.join(changes)}"
        except Exception as e:
            return f"error updating plan: {e}"

    @tool
    def propose_trade(
        ticker: str,
        action: str,
        shares: float = 0.0,
        approx_usd: float = 0.0,
        target_price: float = 0.0,
        sleeve: str = "",
        reason: str = "",
    ) -> str:
        """Log a PROPOSED trade for the human to review and execute manually on eToro.

        This is the dry-run execution path — it does NOT place a trade. It records
        the exact proposal (ticker, action buy/sell/trim, shares, dollar size,
        sleeve, and your reason) to ~/.tradingagents/portfolio_advisor/proposed_trades.jsonl
        so the human sees a precise actionable list. action ∈ {buy, sell, trim, add}.
        """
        tk = (ticker or "").strip().upper()
        act = (action or "").strip().lower()
        if not tk or act not in ("buy", "sell", "trim", "add"):
            return "error: need ticker + action in {buy, sell, trim, add}"
        try:
            import json
            from pathlib import Path
            p = pa_state.advisor_dir(cfg) / "proposed_trades.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ticker": tk,
                "action": act,
                "shares": float(shares or 0),
                "approx_usd": float(approx_usd or 0),
                "target_price": float(target_price or 0),
                "sleeve": (sleeve or "").strip().lower() or None,
                "reason": (reason or "").strip()[:500],
                "status": "proposed",
            }
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            qty = f"{shares:g} sh" if shares else (f"~${approx_usd:.0f}" if approx_usd else "size TBD")
            return f"PROPOSAL recorded: {act} {tk} {qty} (advisory only; human executes)"
        except Exception as e:
            return f"error recording proposal: {e}"

    @tool
    def log_market_event(
        category: str,
        cause: str,
        market_move: str = "mixed",
        magnitude: str = "moderate",
        portfolio_impact_json: str = "{}",
        pattern_tags_csv: str = "",
        strategy_implication: str = "",
        notes: str = "",
    ) -> str:
        """Log a structured market observation explaining WHY the portfolio moved.

        Call this when you observe a significant portfolio-wide or sector move
        and can explain the macro/market cause. This builds a pattern library
        that future PM cycles use to recognize similar conditions and adjust
        strategy — for example, when all tech stocks jump on tariff news, Fed
        signals, or earnings waves.

        category: macro | sector | fed | geopolitical | earnings | earnings_sector | other
        market_move: bull | bear | mixed | flat
        magnitude: strong (>3% avg move) | moderate (1-3%) | weak (<1%)
        portfolio_impact_json: JSON string like '{"NVDA": 7.2, "NOW": 5.1}' with % changes
        pattern_tags_csv: comma-separated tags e.g. "tariff_relief,tech_outperformance"
        strategy_implication: one sentence on what to do next time this pattern appears
        notes: any extra context worth remembering

        Do NOT call this for individual stock moves — only for broad portfolio or
        sector events where the cause is a macro/market driver you can name.
        """
        import json as _json
        from tradingagents.portfolio_advisor.market_memory import append_market_event, already_logged_today
        from datetime import datetime, timezone

        cause_clean = (cause or "").strip()
        cat_clean = (category or "other").strip().lower()

        if already_logged_today(cfg, cat_clean, cause_clean[:60]):
            return f"skipped: a similar {cat_clean} event was already logged today"

        try:
            impact = _json.loads(portfolio_impact_json) if (portfolio_impact_json or "").strip().startswith("{") else {}
        except Exception:
            impact = {}

        tags = [t.strip() for t in (pattern_tags_csv or "").split(",") if t.strip()]
        event_id = append_market_event(cfg, {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "category": cat_clean,
            "cause": cause_clean,
            "market_move": (market_move or "mixed").strip().lower(),
            "magnitude": (magnitude or "moderate").strip().lower(),
            "portfolio_impact": impact,
            "pattern_tags": tags,
            "strategy_implication": (strategy_implication or "").strip(),
            "notes": (notes or "").strip(),
        })
        return f"market event logged (id={event_id}): {cat_clean}/{market_move}/{magnitude} — {cause_clean[:80]}"

    return [
        queue_research,
        mark_action_done,
        get_recent_research,
        compare_candidates,
        cancel_pending_job,
        get_sleeve_mix,
        adjust_position_plan,
        propose_trade,
        log_market_event,
    ]
