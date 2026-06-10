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
from tradingagents.portfolio_advisor import pm_workspace as pmws


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
        so you stop carrying a stale action into your next check-in.
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
    def get_research_detail(ticker: str) -> str:
        """Read the FULL saved deep-research report for a ticker and return the actual
        reasoning — the Final decision plus the Market / Sentiment / News / Fundamentals
        sections — NOT just the short verdict excerpt that get_recent_research gives you.

        Call this whenever the human asks "why" about a holding or one of your
        recommendations, or any time you need the real argument behind a rating rather
        than the one-line label. Returns "no saved report" if a deep dive was never run.
        """
        tk = (ticker or "").strip().upper()
        if not tk:
            return "error: empty ticker"
        try:
            import re
            from pathlib import Path

            base = Path(str(cfg.get("results_dir", "."))) / "clerk_deep" / tk
            reports = sorted(base.glob("*.md")) if base.is_dir() else []
            if not reports:
                return (
                    f"no saved deep report for {tk} — only the short verdict is on file. "
                    "Use get_recent_research, or queue a full_graph dive if you need the full case."
                )
            latest = reports[-1]
            date = latest.stem.split("_")[0]
            text = latest.read_text(encoding="utf-8", errors="replace")

            # Section caps keep the whole thing phone-sized while preserving the reasoning.
            caps = {
                "final decision": 1400,
                "market": 700,
                "sentiment": 700,
                "news": 700,
                "fundamentals": 700,
                "research plan": 700,
                "trader plan": 700,
            }
            out = [f"{tk} deep report ({date}):"]
            for chunk in re.split(r"\n## ", text):
                head, _, body = chunk.strip().partition("\n")
                title = head.strip().lstrip("# ").strip()
                cap = caps.get(title.lower())
                if cap is None:  # title/date header block or unknown section
                    continue
                body = body.strip()
                if body:
                    out.append(f"\n## {title}\n{body[:cap]}")
            result = "\n".join(out).strip()
            return result[:5000] if len(out) > 1 else (
                f"{tk} report on file ({date}) but no readable sections — verdict only via get_recent_research."
            )
        except Exception as e:
            return f"error reading deep report: {e}"

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
        catalyst_date: str = "",
        confidence: float = 0.0,
    ) -> str:
        """Log a PROPOSED trade for the human to review and execute manually on eToro.

        This is the dry-run execution path — it does NOT place a trade. It records
        the exact proposal (ticker, action buy/sell/trim, shares, dollar size,
        sleeve, and your reason) to ~/.tradingagents/portfolio_advisor/proposed_trades.jsonl
        so the human sees a precise actionable list. action ∈ {buy, sell, trim, add}.

        For a CATALYST buy, pass catalyst_date (ISO YYYY-MM-DD, e.g. the earnings
        or event date) — it drives the concrete exit day on the ticket (exit
        time_stop_days after it). Omit it for core names. target_price = your
        intended entry; it lets the ticket print exact stop / +100% / -40% levels.

        confidence (0.5–1.0) is your honest conviction in this trade. It drives
        the PAPER book's position size (0.5 → smallest, 0.9+ → largest), so your
        stated conviction is measured against outcomes — miscalibration shows up
        as paper P&L. Be honest, not uniformly high: your calibration record is
        injected into future cycles.

        reason is REQUIRED — explain WHY (what changed, the catalyst, the rule that
        fired, the thesis read). A proposal with no reason is rejected. For a held
        name, the reason is also appended to that position's decision_history so the
        rationale behind every move stays attached to the position.
        """
        tk = (ticker or "").strip().upper()
        act = (action or "").strip().lower()
        if not tk or act not in ("buy", "sell", "trim", "add"):
            return "error: need ticker + action in {buy, sell, trim, add}"
        reason_clean = (reason or "").strip()
        if not reason_clean:
            return (
                "error: a reason is required — explain WHY before proposing this trade "
                "(what changed, the catalyst, the rule that fired, or the thesis read). "
                "Don't just propose buy/sell/trim with no rationale."
            )
        # Guard: a CATALYST buy must name a dated event. No catalyst_date means the
        # thesis is secular/compounder (e.g. "the AI buildout"), NOT a dated trade —
        # and the catalyst sleeve's -8% hard stop would knock it out on ordinary
        # volatility, often at the very price the thesis says to add. Force it to
        # core so it gets the -30/-40% floors and a multi-year hold instead.
        if (sleeve or "").strip().lower() == "catalyst" and act in ("buy", "add") \
                and not (catalyst_date or "").strip():
            return (
                "error: a catalyst buy requires catalyst_date (the dated event, ISO "
                "YYYY-MM-DD — earnings, a launch, a ruling). No dated event means this "
                "is a secular/compounder thesis, not a catalyst trade: re-propose with "
                "sleeve='core' so it gets the -30/-40% drawdown floors and a 3-5yr hold, "
                "NOT the -8% catalyst stop. Don't file a name in 'catalyst' just to fill "
                "the sleeve."
            )
        try:
            from tradingagents.portfolio_advisor import proposals as _proposals
            # add() supersedes any existing open proposal for the same ticker+side,
            # so a rule that keeps re-firing can't pile up duplicate pending rows.
            _proposals.add(
                cfg,
                ticker=tk,
                action=act,
                shares=shares,
                approx_usd=approx_usd,
                target_price=target_price,
                sleeve=sleeve,
                reason=reason_clean,
                catalyst_date=catalyst_date,
                confidence=confidence,
            )
            # Mirror the decision + WHY into the position plan's history so the
            # rationale travels with the holding (held names; new buys stay in
            # proposed_trades.jsonl until the position is opened and a plan exists).
            try:
                from tradingagents.portfolio_advisor import position_plans as _pp
                _pp.append_position_decision(
                    cfg, tk, act, reason_clean,
                    source="propose_trade",
                    price=(float(target_price) or None),
                )
            except Exception:
                pass
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
        result = f"market event logged (id={event_id}): {cat_clean}/{market_move}/{magnitude} — {cause_clean[:80]}"

        # Pre-event alert: check if this event matches any existing macro rule
        try:
            from tradingagents.portfolio_advisor.macro_learning import check_pre_event_alert
            alert = check_pre_event_alert(cfg, cat_clean, cause_clean)
            if alert:
                from tradingagents.portfolio_advisor import messaging
                messaging.send_advisor_message(
                    cfg, "PM",
                    f"MACRO ALERT — {alert}",
                    urgent=True,
                    log_as_recommendation=True,
                    rec_trigger="action_check",
                    rec_type="macro_alert",
                    rec_action=alert[:80],
                    rec_rationale=f"Pre-event alert matching macro rule. Event: [{cat_clean}] {cause_clean[:200]}",
                )
                result += " | pre-event alert sent"
        except Exception:
            pass

        return result

    @tool
    def log_decision_lesson(
        ticker: str,
        stance_was: str,
        outcome_quality: str,
        pnl_description: str,
        lesson: str,
        pattern_tags_csv: str = "",
        rule_name: str = "",
    ) -> str:
        """Log a lesson extracted from a past decision outcome.

        Call this when you see an outcome_recorded event and can articulate what
        your prior stance got right or wrong and why. This builds the PM's
        accumulated trading experience that persists across all future sessions.

        ticker: the position ticker (e.g. NVDA)
        stance_was: what the PM recommended (buy/hold/sell/trim/watch)
        outcome_quality: correct | incorrect | partial | unclear
        pnl_description: brief description e.g. "dropped 12% then recovered to +8%"
        lesson: one clear sentence — the transferable insight
        pattern_tags_csv: reusable labels e.g. "momentum_exhaustion,post_earnings_drift"
        rule_name: if this lesson confirms or violates an existing rule, name it here

        Example lesson: "Momentum exhaustion (MACD histogram -90%) after a 150%+
        rally reliably signals a 2-4 week pullback even when fundamentals are strong.
        Hold is correct; adding is a mistake."
        """
        from tradingagents.portfolio_advisor.rule_book import append_lesson
        tags = [t.strip() for t in (pattern_tags_csv or "").split(",") if t.strip()]
        lesson_id = append_lesson(cfg, {
            "ticker": (ticker or "").strip().upper(),
            "stance_was": (stance_was or "").strip().lower(),
            "outcome_quality": (outcome_quality or "").strip().lower(),
            "pnl_description": (pnl_description or "").strip(),
            "lesson": (lesson or "").strip(),
            "pattern_tags": tags,
            "rule_updated": rule_name.strip() or None,
        })
        return f"lesson logged (id={lesson_id}): {ticker} [{stance_was}→{outcome_quality}] — {(lesson or '')[:80]}"

    @tool
    def update_pm_rule(
        rule_name: str,
        action: str,
        pattern: str = "",
        rule_text: str = "",
        confidence: str = "",
        evidence_note: str = "",
    ) -> str:
        """Add or update a rule in the PM rule book (PM_RULES.md).

        This is how the PM evolves its own strategy based on accumulated experience.
        Rules are persistent — they survive across all future PM sessions and are
        injected into every cycle prompt so prior experience shapes current reasoning.

        action:
          add     — create a new rule (requires pattern + rule_text)
          confirm — a decision that followed this rule worked out (+1 confirmed)
          violate — a decision that followed this rule failed (-1, may downgrade confidence)
          revise  — update the pattern and/or rule text (provide new pattern/rule_text)
          retire  — mark the rule as retired (too many violations or superseded)

        confidence: emerging (default) | medium | high | weak | retired
        evidence_note: one sentence tying this update to a specific outcome or observation

        Example — add a new rule:
          rule_name="post-earnings-momentum-exhaustion"
          action="add"
          pattern="Stock up 100%+ in 4-6 weeks, MACD histogram collapsing, post-earnings drift lower despite beat"
          rule_text="Hold existing position. Do not add. Wait for 50-day SMA pullback (10-15%). Re-evaluate in 3 weeks."
          confidence="emerging"
          evidence_note="NVDA 2026-05-21: Hold at $223 after 173% rally was correct — stock pulled back to $195."

        Example — confirm after outcome:
          rule_name="post-earnings-momentum-exhaustion"
          action="confirm"
          evidence_note="NVDA pulled back 12% within 2 weeks of the Hold call, exactly as predicted."
        """
        from tradingagents.portfolio_advisor.rule_book import add_rule, update_rule
        action_clean = (action or "").strip().lower()
        name_clean = (rule_name or "").strip()
        if not name_clean:
            return "error: rule_name is required"
        if action_clean == "add":
            if not pattern.strip() or not rule_text.strip():
                return "error: add requires both pattern and rule_text"
            return add_rule(
                cfg,
                name=name_clean,
                pattern=pattern.strip(),
                rule_text=rule_text.strip(),
                confidence=(confidence or "emerging").strip(),
                evidence_note=evidence_note.strip(),
            )
        else:
            return update_rule(
                cfg,
                name=name_clean,
                action=action_clean,
                pattern=pattern.strip(),
                rule_text=rule_text.strip(),
                confidence=confidence.strip(),
                evidence_note=evidence_note.strip(),
            )

    @tool
    def record_decision(
        ticker: str,
        recommended: str,
        choice: str,
        reason: str = "",
        until: str = "",
    ) -> str:
        """Record what the human decided when you recommended an action.

        Call this whenever the human accepts, defers, or overrides an action
        you proposed (e.g. you said "sell half DDOG" and they said "holding
        through earnings"). It writes a durable decision so you stop re-nagging
        a settled call and remember WHY next cycle.

        - recommended: the action you advised (e.g. "sell half", "trim", "exit").
        - choice: what the human chose (e.g. "hold", "sell half", "defer").
        - reason: the human's stated reason (quote it briefly).
        - until: optional ISO date (YYYY-MM-DD) the decision holds until
          (e.g. through an earnings date). Empty = open-ended.
        """
        tk = (ticker or "").strip().upper()
        if not tk:
            return "error: empty ticker"
        row = pmws.record_decision(
            cfg,
            ticker=tk,
            recommended=recommended,
            choice=choice,
            reason=reason,
            until=(until.strip() or None),
        )
        return f"recorded decision for {tk}: human chose {row['choice']} (reason: {row['reason'] or 'none'})"

    @tool
    def clear_decision(ticker: str) -> str:
        """Clear the active standing decision for a ticker (re-arm alerts on it)."""
        tk = (ticker or "").strip().upper()
        if not tk:
            return "error: empty ticker"
        return f"cleared decision for {tk}" if pmws.clear_decision(cfg, tk) else f"no active decision for {tk}"

    @tool
    def update_position_memory(ticker: str, note: str) -> str:
        """Append a durable note to a position's memory file (memory/positions/<TICKER>.md).

        Use for thesis updates, what changed, what you learned — facts your next
        self should know about this holding. Keep each note tight.
        """
        tk = (ticker or "").strip().upper()
        if not tk or not (note or "").strip():
            return "error: need ticker and note"
        pmws.update_position_memory(cfg, tk, note)
        return f"updated position memory for {tk}"

    @tool
    def update_scoped_rule(ticker: str, rule_text: str) -> str:
        """Add a per-ticker rule to rules/<TICKER>.md.

        Use for a durable rule specific to one name (e.g. an entry/exit trigger
        the human set, or a learned constraint). Portfolio-wide rules belong in
        rules/_portfolio.md; do not put them here.
        """
        tk = (ticker or "").strip().upper()
        if not tk or not (rule_text or "").strip():
            return "error: need ticker and rule_text"
        pmws.update_scoped_rule(cfg, tk, rule_text)
        return f"added scoped rule for {tk}"

    @tool
    def emit_ep_candidate(
        ticker: str,
        tier: str,
        catalyst: str,
        entry_price: float,
        stop_price: float,
        risk_pct: float = 1.0,
        sector: str = "",
        gap_pct: float = 0.0,
        notes: str = "",
    ) -> str:
        """Push a structured Episodic Pivot recommendation to the human via Telegram.

        Call this when you have identified a qualifying EP setup per
        rules/strategies/episodic_pivot.md Section 5. It computes risk-based
        sizing (Section 6.1, default 1%% portfolio risk) and emits a Telegram
        message with the suggested entry, stop, and exact share/USD size.

        This is ADVISORY ONLY. The human decides whether to execute on eToro.
        Entry is recommended at the next session open, per the AI Advisory
        Edition (v2) workflow Section 9.3.

        - tier: "Tier 1" or "Tier 2" (per Section 4).
        - entry_price: recommended entry at next session open (today's close).
        - stop_price: hard stop (Section 7.1 — catalyst day low or -8% max).
        - risk_pct: percent of portfolio equity to risk (default 1.0).
        - sector: free-text sector, for the Section 6.2 1-per-sector check.
        - gap_pct: catalyst-day gap percentage, for journal.
        - notes: catalyst description, drift thesis, invalidation triggers.
        """
        from tradingagents.portfolio_advisor import etoro_scan, messaging
        tk = (ticker or "").strip().upper()
        if not tk:
            return "error: empty ticker"
        try:
            _payload, portfolio_text, _t, _rows = etoro_scan.fetch_portfolio_rows()
        except Exception as e:
            return f"error: portfolio fetch failed: {e}"
        # Best-effort equity estimate from the first-line totals.
        import re as _re
        eq = None
        m = _re.search(r"total_portfolio_value_usd=([0-9.]+)", portfolio_text)
        if m:
            try:
                eq = float(m.group(1))
            except ValueError:
                pass
        if eq is None:
            m2 = _re.search(r"available_balance=['\"]?([0-9.]+)", portfolio_text)
            eq = float(m2.group(1)) * 5.0 if m2 else 10000.0  # rough fallback
        usd_risk = round(eq * float(risk_pct) / 100.0, 2)
        risk_per_share = max(float(entry_price) - float(stop_price), 0.0001)
        shares = round(usd_risk / risk_per_share, 4)
        usd_position = round(shares * float(entry_price), 2)
        body = (
            f"EP RECOMMENDATION: {tk} [{tier}]\n"
            f"Catalyst: {catalyst}\n"
            f"Gap: {gap_pct:+.1f}%  |  Sector: {sector or 'unknown'}\n"
            f"Entry (next session open): ${float(entry_price):.2f}\n"
            f"Stop: ${float(stop_price):.2f}  (-{((float(entry_price)-float(stop_price))/float(entry_price)*100):.1f}%)\n"
            f"Risk: ${usd_risk:.0f}  ({float(risk_pct):.1f}% of ~${eq:.0f} equity)\n"
            f"Size: {shares:.2f} shares  /  ~${usd_position:.0f} position\n"
            f"Action: enter at next session open if gap held through close (Section 5.2).\n"
            f"Reply: 'entered {tk} <shares>sh @ <price>' to log; 'skipped' to discard.\n"
            + (f"Notes: {notes}\n" if notes else "")
        )
        messaging.send_advisor_message(
            cfg, "PM", body, urgent=True,
            log_as_recommendation=True,
            rec_trigger="ep_scan",
            rec_type="ep_entry",
            rec_action=f"buy {shares:.2f} sh {tk} @ ${entry_price:.2f}",
            rec_ticker=tk,
            rec_rationale=f"{catalyst} | gap={gap_pct:+.1f}% | sector={sector or 'unknown'}",
        )
        return f"emitted EP recommendation for {tk}: entry=${entry_price} stop=${stop_price} risk=${usd_risk} shares={shares}"

    @tool
    def log_ep_trade(
        ticker: str,
        tier: str,
        catalyst: str,
        entry_date: str,
        entry_price: float,
        stop_price: float,
        shares: float,
        usd_position: float,
        usd_risk: float,
        sector: str = "",
        notes: str = "",
    ) -> str:
        """Log a NEW open EP trade to memory/strategies/ep_trades.jsonl (Section 10 mandate).

        Call this when the human confirms execution of an EP candidate. After
        this, the trade appears in the "Open EP trades" prompt block every
        cycle, so you can apply Section 15 checkpoints as sessions progress.
        """
        row = pmws.log_ep_trade(
            cfg,
            ticker=ticker, tier=tier, catalyst=catalyst,
            entry_date=entry_date, entry_price=entry_price, stop_price=stop_price,
            shares=shares, usd_position=usd_position, usd_risk=usd_risk,
            sector=sector, notes=notes,
        )
        return f"logged EP trade {row['id']} for {row['ticker']}"

    @tool
    def update_ep_trade(
        ticker: str = "",
        trade_id: str = "",
        new_stop: float = 0.0,
        exit_date: str = "",
        exit_price: float = 0.0,
        exit_reason: str = "",
        pnl_usd: float = 0.0,
        r_multiple: float = 0.0,
        note: str = "",
    ) -> str:
        """Update an EP trade: raise the stop (per Section 6.2) or close it on exit.

        Match by trade_id when known, otherwise by latest open trade for ticker.
        Stop-raise: pass new_stop only. Close: pass exit_price + exit_reason
        (pnl/R are auto-computed from prices+risk if not supplied).
        """
        out = pmws.update_ep_trade(
            cfg,
            trade_id=trade_id, ticker=ticker,
            new_stop=(new_stop or None),
            exit_date=exit_date, exit_price=(exit_price or None),
            exit_reason=exit_reason,
            pnl_usd=(pnl_usd or None), r_multiple=(r_multiple or None),
            note=note,
        )
        if not out:
            return f"no matching open EP trade for {ticker or trade_id}"
        if out.get("status") == "closed":
            return f"closed EP trade {out['id']} {out['ticker']} pnl=${out.get('pnl_usd')} ({out.get('r_multiple')}R)"
        return f"updated EP trade {out['id']} {out['ticker']} stop=${out.get('stop_price')}"

    @tool
    def run_macro_review() -> str:
        """Extract durable portfolio rules from recent macro event patterns.

        Call this during your Saturday/weekend review to scan the last 90 days
        of market events for recurring patterns (tariff-driven selloffs, Fed
        relief rallies, sector rotations, etc.) and write learned rules to
        _portfolio.md so you remember them permanently.

        Rules extracted are SPECIFIC and ACTIONABLE: "when X happens, do Y."
        They persist across sessions and feed into every future PM prompt.
        """
        try:
            from tradingagents.portfolio_advisor.macro_learning import run_macro_learning_review
            result = run_macro_learning_review(cfg)
            return f"macro review: {result}"
        except Exception as e:
            return f"macro review failed: {e}"

    return [
        queue_research,
        mark_action_done,
        get_recent_research,
        get_research_detail,
        compare_candidates,
        cancel_pending_job,
        get_sleeve_mix,
        adjust_position_plan,
        propose_trade,
        log_market_event,
        log_decision_lesson,
        update_pm_rule,
        record_decision,
        clear_decision,
        update_position_memory,
        update_scoped_rule,
        emit_ep_candidate,
        log_ep_trade,
        update_ep_trade,
        run_macro_review,
    ]
