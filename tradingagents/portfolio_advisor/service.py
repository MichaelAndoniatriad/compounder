"""Orchestration: eToro scan → LLM plan → persisted jobs → due runs."""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.agents.utils.event_log import append_event
from tradingagents.clerk.deep_runner import run_deep_research, save_deep_report
from tradingagents.dataflows.config import set_config
from tradingagents.portfolio_advisor import (
    catalysts,
    etoro_scan,
    messaging,
    outcome_sync,
    plan_validation,
    planner,
    state,
    weekly_check,
    weekly_significance,
    watchdog as watchdog_mod,
)
from tradingagents.portfolio_advisor.models import AdvisorPlanResult
from tradingagents.portfolio_advisor.single_model_analysis import run_single_model_analysis

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(dt: str) -> datetime:
    s = (dt or "").strip().replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _weekday_matches(cfg: Dict[str, Any]) -> bool:
    """Return True if today's weekday matches configured weekly scan day (0=Mon … 6=Sun)."""
    want = int(cfg.get("portfolio_advisor_weekly_weekday", 5))
    got = date.today().weekday()
    return got == want


def _catalyst_digest(catalyst_text: str) -> str:
    return hashlib.sha256((catalyst_text or "").encode("utf-8")).hexdigest()


def _replan_skip_llm(
    cfg: Dict[str, Any],
    mode: str,
    tickers: List[str],
    catalyst_text: str,
    st: Dict[str, Any],
) -> bool:
    """True when replan should skip the planner LLM (unchanged book + digest)."""
    if mode != "replan":
        return False
    if not bool(cfg.get("portfolio_advisor_skip_replan_llm_when_unchanged")):
        return False
    cur = sorted(str(t).strip().upper() for t in tickers if str(t).strip())
    prev = sorted(
        str(t).strip().upper() for t in (st.get("last_portfolio_tickers") or []) if str(t).strip()
    )
    if cur != prev:
        return False
    digest = _catalyst_digest(catalyst_text)
    return digest == (st.get("last_catalyst_digest") or "")


def _job_snapshot_for_diff(j: Dict[str, Any]) -> str:
    return (
        f"{str(j.get('ticker') or '').strip().upper()} @ {str(j.get('scheduled_at') or '')} "
        f"tier={str(j.get('execution_tier') or 'single_model')} type={str(j.get('job_type') or 'routine_monitoring')}"
    )


def _diff_pending_jobs_section(prev: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]) -> str:
    if not prev:
        return ""
    prev_lines = sorted({_job_snapshot_for_diff(j) for j in prev})
    new_lines = sorted({_job_snapshot_for_diff(j) for j in new_rows})
    if prev_lines == new_lines:
        return "Schedule diff: unchanged lines versus prior pending jobs.\n"
    ps, ns = set(prev_lines), set(new_lines)
    added, removed = sorted(ns - ps), sorted(ps - ns)
    parts = ["Schedule diff versus prior pending jobs:"]
    if added:
        parts.append("New or changed:")
        parts.extend(f"  {x}" for x in added[:50])
    if removed:
        parts.append("Dropped:")
        parts.extend(f"  {x}" for x in removed[:50])
    return "\n".join(parts) + "\n"


def _jobs_from_plan(plan: AdvisorPlanResult, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    now = _utc_now()
    max_jobs = int(cfg.get("portfolio_advisor_max_jobs_per_plan") or 15)
    out: List[Dict[str, Any]] = []
    for spec in plan.jobs:
        if len(out) >= max_jobs:
            break
        if spec.action != "deep_research":
            continue
        try:
            when = _parse_iso(spec.scheduled_at)
        except ValueError:
            logger.warning("skip job bad scheduled_at for %s: %r", spec.ticker, spec.scheduled_at)
            continue
        if when < now:
            logger.info("skip past-dated job %s %s", spec.ticker, spec.scheduled_at)
            continue
        out.append(
            {
                "id": uuid.uuid4().hex[:20],
                "ticker": spec.ticker.strip().upper(),
                "scheduled_at": spec.scheduled_at.strip(),
                "kind": "deep_research",
                "reason": (spec.rationale or "").strip(),
                "status": "pending",
                "created_at": now.isoformat(),
                "execution_tier": spec.execution_tier,
                "job_type": (spec.job_type or "routine_monitoring").strip(),
                "source": "planner",
                "evidence_question": (spec.rationale or "").strip()[:300],
                "supersedes_job_id": "",
                "flags": list(spec.flags or []),
            }
        )
    return out


def run_planning_session(cfg: Dict[str, Any], *, mode: str) -> Tuple[AdvisorPlanResult, Dict[str, Any]]:
    """Full eToro scan + LLM plan + persist jobs. ``mode`` is ``init`` or ``replan``."""
    set_config(cfg)
    _payload, portfolio_text, tickers, rows = etoro_scan.fetch_portfolio_rows()
    if not tickers:
        raise RuntimeError("No tickers in eToro portfolio export.")
    live = etoro_scan.current_ticker_set(tickers)
    try:
        outcome_sync.auto_close_outcomes(cfg, live, rows=rows)
    except Exception:
        logger.debug("outcome_sync after planning fetch skipped", exc_info=True)
    cat = catalysts.catalyst_block_for_tickers(tickers)
    st = state.load_state(cfg)
    if _replan_skip_llm(cfg, mode, tickers, cat, st):
        body = (
            "Portfolio advisor (replan) skipped the planner LLM: live ticker set and catalyst digest "
            "match the last successful plan. Pending jobs were left unchanged."
        )
        skip_plan = AdvisorPlanResult(
            executive_summary=body,
            jobs=[],
            immediate_actions=[],
        )
        st["last_replan_skip_iso"] = _utc_now().isoformat()
        state.save_state(cfg, st)
        append_event(
            cfg,
            {
                "ticker": "*",
                "event_type": "advisor_replan_skipped",
                "key_data": {"reason": "portfolio_and_catalyst_unchanged"},
                "outcome": None,
            },
        )
        return skip_plan, st

    plan = planner.build_advisor_plan(
        cfg,
        portfolio_text=portfolio_text,
        catalyst_text=cat,
        mode=mode,
        tickers=tickers,
    )
    if not plan.jobs:
        # Could be a structured-output parse failure (free-text fallback returned
        # jobs=[]) or a legitimately empty plan. Either way the queue is now empty
        # and nothing will run — surface it instead of failing silently.
        try:
            messaging.send_advisor_message(
                cfg,
                f"Advisor: {mode} produced no jobs",
                (
                    f"Planner returned 0 jobs for {len(tickers)} tickers. "
                    "Pending queue will be empty. Check planner LLM and rerun."
                ),
                urgent=True,
            )
        except Exception:
            logger.exception("failed to send empty-plan alert")
    thesis_raw = cfg.get("portfolio_advisor_thesis_metrics") or {}
    thesis_metrics = thesis_raw if isinstance(thesis_raw, dict) else {}
    plan, overrides = plan_validation.validate_advisor_plan(
        cfg, plan, rows, thesis_metrics=thesis_metrics
    )
    if overrides:
        append_event(
            cfg,
            {
                "ticker": "*",
                "event_type": "plan_validation_override",
                "key_data": {"overrides": overrides},
                "outcome": None,
            },
        )
    prev_pending = state.list_pending_jobs(st)
    cancelled = state.cancel_all_pending(st, reason="replaced by new advisor plan")
    new_rows = _jobs_from_plan(plan, cfg)
    state.append_jobs(st, new_rows)
    st["last_portfolio_tickers"] = sorted(tickers)
    st["first_scan_complete"] = True
    ts = _utc_now().isoformat()
    if mode == "init":
        st["last_init_iso"] = ts
    if mode == "replan":
        st["last_replan_iso"] = ts
    st["last_catalyst_digest"] = _catalyst_digest(cat)
    st["last_portfolio_text_hash"] = hashlib.sha256(portfolio_text.encode("utf-8")).hexdigest()
    state.save_state(cfg, st)

    last_date = max((j.get("scheduled_at", "")[:10] for j in new_rows), default="?") if new_rows else "?"
    action_lines = [f"- {a}" for a in (plan.immediate_actions or [])]
    body_parts = [f"Replanned: {len(new_rows)} jobs queued through {last_date}."]
    if action_lines:
        body_parts.append("Action required:")
        body_parts.extend(action_lines)
    subj = f"Replanned — {len(new_rows)} jobs"
    messaging.send_advisor_message(cfg, subj, "\n".join(body_parts))
    append_event(
        cfg,
        {
            "ticker": "*",
            "event_type": "advisor_plan",
            "key_data": {"mode": mode, "jobs_queued": len(new_rows), "cancelled_pending": cancelled},
            "outcome": None,
        },
    )
    return plan, st


def run_init(cfg: Dict[str, Any], *, force: bool = False) -> None:
    if force:
        state.save_state(cfg, state.default_state())
    else:
        st = state.load_state(cfg)
        if st.get("first_scan_complete"):
            raise RuntimeError(
                "Advisor already initialized. Use `advisor portfolio weekly` for the light check, "
                "`advisor portfolio replan` to rebuild the LLM schedule, or `advisor portfolio init --force`."
            )
    run_planning_session(cfg, mode="init")
    if bool(cfg.get("portfolio_advisor_bootstrap_on_init")):
        from tradingagents.portfolio_advisor.bootstrap import run_full_portfolio_bootstrap

        delay = float(cfg.get("portfolio_advisor_bootstrap_delay_seconds") or 45.0)
        maxp = cfg.get("portfolio_advisor_bootstrap_max_positions")
        maxp_i = int(maxp) if maxp is not None else None
        run_full_portfolio_bootstrap(
            cfg, delay_seconds=delay, max_positions=maxp_i
        )


def run_weekly(cfg: Dict[str, Any], *, ignore_weekday: bool = False) -> str:
    """Lightweight weekly portfolio check (no full replan). Returns status token."""
    if not ignore_weekday and not _weekday_matches(cfg):
        logger.info(
            "Portfolio advisor weekly skipped (today weekday %s != configured %s)",
            date.today().weekday(),
            cfg.get("portfolio_advisor_weekly_weekday", 5),
        )
        return "skipped_weekday"
    set_config(cfg)
    digest, attention, live = weekly_check.run_weekly_quick_check(cfg)
    always = bool(cfg.get("portfolio_advisor_weekly_always_email", False))
    worth = weekly_significance.weekly_email_worth_sending(
        cfg, digest, live, attention_flag=attention
    )
    if always or worth:
        subj = "[TradingAgents] Weekly portfolio check"
        if attention:
            subj += " — review suggested"
        messaging.send_advisor_message(cfg, subj, digest)
    else:
        logger.info("Weekly portfolio check ran clean; email suppressed (no significance gate hit).")
    return "checked"


def run_replan(cfg: Dict[str, Any], *, ignore_weekday: bool = False) -> str:
    """Full LLM reschedule (same engine as init, keeps existing state except pending jobs replaced)."""
    if not ignore_weekday and not _weekday_matches(cfg):
        logger.info(
            "Portfolio advisor replan skipped (weekday gate; use --force to override)",
        )
        return "skipped_weekday"
    run_planning_session(cfg, mode="replan")
    return "replanned"


_state_lock = threading.Lock()


def _save_job_outcome(cfg: Dict[str, Any], jid: str, status: str, error: Optional[str] = None) -> None:
    """Thread-safe update of a single job's status in state.json."""
    with _state_lock:
        st = state.load_state(cfg)
        for j in st.get("jobs", []):
            if j.get("id") == jid and j.get("status") in ("pending", "in_progress"):
                j["status"] = status
                j["completed_at"] = _utc_now().isoformat()
                if error:
                    j["error"] = error
                break
        state.save_state(cfg, st)


def _run_job(j: Dict[str, Any], cfg: Dict[str, Any], live: set, trade_date: str) -> Dict[str, Any]:
    """Execute one job. Returns result dict. Sends its own ntfy message when done."""
    tid = str(j.get("ticker") or "").strip().upper()
    jid = str(j.get("id") or "")

    if not tid or not jid:
        return {"ticker": tid, "status": "skipped", "verdict": ""}

    try:
        from tradingagents.portfolio_advisor.candidates import is_candidate_job

        candidate_job = is_candidate_job(j)
    except Exception:
        candidate_job = False

    if tid not in live and not candidate_job:
        _save_job_outcome(cfg, jid, "cancelled")
        return {"ticker": tid, "status": "cancelled", "verdict": ""}

    analysts = cfg.get("portfolio_advisor_deep_analysts") or ["news", "fundamentals", "market"]
    if not isinstance(analysts, list):
        analysts = ["news", "fundamentals", "market"]
    tier = str(j.get("execution_tier") or "single_model").strip().lower()
    job_type = str(j.get("job_type") or "routine_monitoring").strip()

    # Per-ticker rolling cap on full_graph: prevents retry storms (NVDA had 11
    # runs in 14d under the old behavior). If exceeded, downgrade to single_model
    # so the work still happens cheaply rather than re-running an expensive pipe.
    if tier == "full_graph":
        try:
            from datetime import timedelta
            from tradingagents.agents.utils.event_log import _iter_events
            cap = int(cfg.get("portfolio_advisor_full_graph_per_ticker_14d_cap") or 2)
            cutoff = (_utc_now() - timedelta(days=14)).isoformat()
            recent = 0
            for row in _iter_events(cfg, max_lines=5000):
                if str(row.get("timestamp") or "") < cutoff:
                    continue
                if str(row.get("event_type") or "") != "full_graph_decision":
                    continue
                if str(row.get("ticker") or "").strip().upper() == tid:
                    recent += 1
            if recent >= cap:
                logger.info(
                    "downgrading %s full_graph -> single_model (already %d full_graph runs in 14d, cap=%d)",
                    tid, recent, cap,
                )
                tier = "single_model"
                j["execution_tier"] = "single_model"
                j["downgraded_reason"] = f"full_graph 14d cap hit ({recent}/{cap})"
        except Exception as e:
            logger.debug("full_graph cap check failed for %s: %s", tid, e)

    try:
        if tier == "full_graph":
            try:
                from tradingagents.portfolio_advisor.position_plans import strategy_for_ticker
                strategy = strategy_for_ticker(cfg, tid, job=j)
            except Exception:
                strategy = "core"
            final_state, _signal = run_deep_research(tid, trade_date, analysts, cfg, strategy=strategy)
            rd = Path(str(cfg.get("results_dir", ".")))
            save_deep_report(results_dir=rd, ticker=tid, trade_date=trade_date, final_state=final_state)
            dec = str(final_state.get("final_trade_decision") or "")
            try:
                from tradingagents.portfolio_advisor.action_log import ingest_from_analysis
                ingest_from_analysis(cfg, tid, dec, source="full_graph")
            except Exception as e:
                logger.warning("ingest_from_analysis failed for %s: %s", tid, e)
            try:
                from tradingagents.portfolio_advisor.candidates import handle_candidate_full_graph_result

                handle_candidate_full_graph_result(cfg, j, dec, live_tickers=live)
            except Exception as e:
                logger.warning("candidate full_graph transition failed for %s: %s", tid, e)
            # Refresh this name's sleeve + thesis-break metrics from the fresh research.
            if bool(cfg.get("portfolio_advisor_auto_classify_after_full_graph", True)):
                try:
                    from tradingagents.portfolio_advisor.position_plans import load_position_plans
                    from tradingagents.portfolio_advisor.position_classifier import (
                        classify_position, _apply_classification,
                    )
                    plans = load_position_plans(cfg)
                    if tid in plans:
                        c = classify_position(cfg, tid, plans[tid])
                        if c is not None:
                            _apply_classification(cfg, plans[tid], c)
                except Exception as e:
                    logger.warning("auto-classify after full_graph failed for %s: %s", tid, e)
            _save_job_outcome(cfg, jid, "completed")
            verdict = messaging.ntfy_verdict(dec, tid)
            return {"ticker": tid, "status": "completed", "verdict": verdict}
        else:
            analysis_text = run_single_model_analysis(cfg, tid, job_type)
            try:
                from tradingagents.portfolio_advisor.action_log import ingest_from_analysis
                ingest_from_analysis(cfg, tid, analysis_text or "", source=f"single_model_{job_type}")
            except Exception as e:
                logger.warning("ingest_from_analysis failed for %s: %s", tid, e)
            try:
                from tradingagents.portfolio_advisor.candidates import handle_candidate_light_research_result

                handle_candidate_light_research_result(cfg, j, analysis_text or "")
            except Exception as e:
                logger.warning("candidate light research transition failed for %s: %s", tid, e)
            _save_job_outcome(cfg, jid, "completed")
            verdict = messaging.ntfy_verdict(analysis_text or "", tid)
            return {"ticker": tid, "status": "completed", "verdict": verdict}
    except Exception as e:
        logger.exception("job failed for %s: %s", tid, e)
        _save_job_outcome(cfg, jid, "failed", str(e))
        return {"ticker": tid, "status": "failed", "verdict": ""}


def _all_hold(results: List[Dict[str, Any]]) -> bool:
    """True iff every completed verdict text starts with HOLD (cheap heuristic)."""
    if not results:
        return False
    for r in results:
        v = str(r.get("verdict") or "").strip().upper()
        if not v.startswith("HOLD"):
            return False
    return True


def _post_batch_pm_brief(cfg: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    """After all jobs finish, run one PM cycle and send a consolidated brief."""
    if bool(cfg.get("portfolio_advisor_pm_skip_if_all_hold", True)) and _all_hold(results):
        logger.info("post-batch PM brief skipped: all %d verdicts were HOLD", len(results))
        return
    try:
        from tradingagents.portfolio_advisor.advisor_pm import run_pm_cycle
        verdicts = "\n".join(f"{r['ticker']}: {r['verdict']}" for r in results if r.get("verdict"))
        context = (
            f"Research batch just completed. Individual results:\n{verdicts}\n\n"
            "Your job now:\n"
            "1. Call get_sleeve_mix() to confirm the current catalyst allocation.\n"
            "2. Rank the researched candidates against each other using compare_candidates "
            "— not each in isolation, but relative to what the portfolio needs right now.\n"
            "3. If catalyst sleeve is empty and cash > $500: pick the BEST available "
            "candidate (even if Hold-rated long-term) and call propose_trade with a "
            "~$300-500 starter size. A Hold long-term rating is fine for a catalyst entry "
            "if there is a near-term event setup. If no candidate has a clear catalyst, "
            "queue catalyst_scan job_type on the top 2 names to get catalyst-specific "
            "research.\n"
            "4. Set push_note with your recommendation or next step. The human wants to "
            "hear what you decided, not just what the research said."
        )
        result = run_pm_cycle(cfg, trigger="batch_complete", extra_context=context)
        # Send push_note as urgent (bypasses quiet hours) when the PM has something to say.
        push = (result.push_note or "").strip()
        has_action = (
            push
            or any(s.stance in ("sell", "trim", "buy", "add") for s in (result.stances or []))
            or bool(result.candidate_comparisons)
            or bool(result.append_jobs)
        )
        if has_action:
            body = push or (result.executive_summary or "").strip()
            if body:
                messaging.send_advisor_message(cfg, "PM", body, urgent=True)
        elif result.stances:
            # Quiet update — goes through normal send-window gate
            lines = []
            for s in result.stances:
                rat = (s.rationale or "").split(".")[0].strip()[:60]
                lines.append(f"{s.ticker} {s.stance.upper()}" + (f" — {rat}" if rat else ""))
            messaging.send_advisor_message(cfg, "PM Brief", "\n".join(lines))
    except Exception as e:
        logger.warning("post-batch PM brief failed: %s", e)


def _check_daily_budget(cfg: Dict[str, Any]) -> bool:
    """Return False (and throttle-alert) when today's LLM spend is over cap."""
    from tradingagents.llm_clients.budget_guard import is_over_daily_cap

    over, spent, cap = is_over_daily_cap(cfg)
    if not over:
        return True
    logger.warning("daily budget cap hit: $%.2f >= $%.2f — skipping run_due batch", spent, cap)
    throttle_h = int(cfg.get("portfolio_advisor_daily_cost_alert_throttle_hours") or 6)
    try:
        with _state_lock:
            st = state.load_state(cfg)
            last_iso = str(st.get("last_budget_cap_alert_iso") or "")
            last_dt = _parse_iso(last_iso) if last_iso else None
            should_alert = (
                last_dt is None
                or (_utc_now() - last_dt).total_seconds() >= throttle_h * 3600
            )
            if should_alert:
                st["last_budget_cap_alert_iso"] = _utc_now().isoformat()
                state.save_state(cfg, st)
        if should_alert:
            messaging.send_advisor_message(
                cfg,
                "Advisor: daily LLM budget cap hit",
                f"Today's logged spend ${spent:.2f} >= cap ${cap:.2f}. "
                "Non-urgent job batches will skip until midnight UTC. Watchdog + chat replies still run.",
                urgent=True,
            )
    except Exception:
        logger.exception("budget cap alert failed")
    return False


def run_due_jobs(cfg: Dict[str, Any]) -> int:
    """Execute all due jobs in parallel, then send a PM brief when done."""
    set_config(cfg)
    if not _check_daily_budget(cfg):
        return 0
    max_run = int(cfg.get("portfolio_advisor_run_due_max") or 8)
    try:
        _p, _t, live_tickers, rows = etoro_scan.fetch_portfolio_rows()
        live = etoro_scan.current_ticker_set(live_tickers)
        outcome_sync.auto_close_outcomes(cfg, live, rows=rows)
    except Exception as e:
        logger.error("run_due: cannot fetch portfolio: %s", e)
        # Push a one-shot alert so a silent fetch failure does not look like a
        # quiet system. Throttle with state so we do not spam every 15 minutes.
        try:
            with _state_lock:
                st_err = state.load_state(cfg)
                last_iso = st_err.get("last_etoro_fetch_alert_iso") or ""
                last_dt = _parse_iso(last_iso) if last_iso else None
                throttle_h = int(cfg.get("portfolio_advisor_silent_alert_throttle_hours") or 6)
                should_alert = (
                    last_dt is None
                    or (_utc_now() - last_dt).total_seconds() >= throttle_h * 3600
                )
                if should_alert:
                    st_err["last_etoro_fetch_alert_iso"] = _utc_now().isoformat()
                    state.save_state(cfg, st_err)
            if should_alert:
                messaging.send_advisor_message(
                    cfg,
                    "Advisor: portfolio fetch failed",
                    f"run_due could not fetch eToro portfolio: {e}. No jobs will run until this clears.",
                    urgent=True,
                )
        except Exception:
            logger.exception("failed to send eToro fetch alert")
        return 0

    # Hold the lock across select + claim so concurrent cron ticks cannot pick the same job.
    with _state_lock:
        st = state.load_state(cfg)
        now = _utc_now()
        # Reset stalled in_progress jobs (crashed run_due, OOM, etc.) before claiming new ones.
        ttl_minutes = int(cfg.get("portfolio_advisor_in_progress_ttl_minutes") or 30)
        reset_ids = state.reset_stalled_in_progress_jobs(st, ttl_seconds=ttl_minutes * 60)
        if reset_ids:
            logger.warning("run_due: reset %d stalled in_progress job(s): %s", len(reset_ids), reset_ids)
            state.save_state(cfg, st)
        due: List[Dict[str, Any]] = []
        for j in state.list_pending_jobs(st):
            try:
                if _parse_iso(str(j.get("scheduled_at") or "")) <= now:
                    due.append(j)
            except ValueError:
                continue
        due.sort(key=lambda x: str(x.get("scheduled_at") or ""))
        due = due[:max_run]

        if not due:
            return 0

        # Deduplicate: one job per ticker (take the oldest due)
        seen_tickers: set = set()
        deduped: List[Dict[str, Any]] = []
        for j in due:
            tid = str(j.get("ticker") or "").strip().upper()
            if tid and tid not in seen_tickers:
                seen_tickers.add(tid)
                deduped.append(j)
        due = deduped[:max_run]

        state.claim_jobs_for_run(st, [j.get("id") for j in due])
        state.save_state(cfg, st)

    trade_date = date.today().isoformat()
    job_results: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=len(due)) as pool:
        futures = {pool.submit(_run_job, j, cfg, live, trade_date): j for j in due}
        for future in as_completed(futures):
            try:
                job_results.append(future.result())
            except Exception as e:
                logger.exception("job worker raised: %s", e)

    completed = [r for r in job_results if r["status"] == "completed" and r.get("verdict")]
    if completed:
        _post_batch_pm_brief(cfg, completed)

    return len([r for r in job_results if r["status"] in ("completed", "cancelled")])


def run_bootstrap(
    cfg: Dict[str, Any],
    *,
    delay_seconds: float = 45.0,
    max_positions: int | None = None,
    trade_date: str | None = None,
    resume: bool = False,
) -> dict:
    """Explicit full-graph pass for all (or capped) live eToro holdings."""
    from tradingagents.portfolio_advisor.bootstrap import run_full_portfolio_bootstrap

    return run_full_portfolio_bootstrap(
        cfg,
        trade_date=trade_date,
        delay_seconds=delay_seconds,
        max_positions=max_positions,
        resume=resume,
    )


def run_memory_review(cfg: Dict[str, Any], *, lookback_days: int = 120) -> str:
    from tradingagents.portfolio_advisor.memory_review import run_memory_review as _mr

    return _mr(cfg, lookback_days=lookback_days)


def run_watchdog(cfg: Dict[str, Any], *, ignore_market_hours: bool = False) -> int:
    """Price only exit rule sweep (separate from ``run_due_jobs``)."""
    set_config(cfg)
    return int(watchdog_mod.run_watchdog(cfg, ignore_market_hours=ignore_market_hours))


def run_dip_watch(cfg: Dict[str, Any], *, ignore_market_hours: bool = False) -> int:
    """Quality-on-a-dip scan: watch core watchlist names, hand fresh dips to the PM."""
    set_config(cfg)
    from tradingagents.portfolio_advisor import dip_watch as dip_watch_mod

    return int(dip_watch_mod.run_dip_watch(cfg, ignore_market_hours=ignore_market_hours))


def run_post_earnings(cfg: Dict[str, Any], ticker: str) -> str:
    """Email a one-shot post-earnings verdict using the configured reasoning model."""
    from tradingagents.portfolio_advisor.post_verdict import run_post_earnings_verdict

    return run_post_earnings_verdict(cfg, ticker)


def _digest_preview(digest: Any) -> str:
    s = str(digest or "")
    if not s:
        return "(none)"
    if len(s) <= 16:
        return s
    return f"{s[:16]}…"


def _bootstrap_summary_one_line(summary: Any) -> str:
    if not isinstance(summary, dict) or not summary:
        return "(none)"
    td = str(summary.get("trade_date") or "").strip() or "?"
    tickers = summary.get("tickers") or []
    n = len(tickers) if isinstance(tickers, list) else 0
    ok = summary.get("ok")
    err = int(summary.get("errors") or 0)
    bits: List[str] = [f"as-of {td}", f"{n} name(s)"]
    if ok is not None:
        bits.append(f"{int(ok)} OK")
    if err:
        bits.append(f"{int(err)} failed")
    ratings = summary.get("ratings") or {}
    if isinstance(ratings, dict) and ratings:
        rbits = [f"{k}:{v}" for k, v in sorted(ratings.items(), key=lambda kv: str(kv[0]))]
        bits.append("ratings " + ",".join(rbits))
    return " | ".join(bits)


def status_text(cfg: Dict[str, Any]) -> str:
    st = state.load_state(cfg)
    lines = [
        f"State file: {state.state_path(cfg)}",
        f"first_scan_complete: {st.get('first_scan_complete')}",
        f"last_init_iso: {st.get('last_init_iso')}",
        f"last_replan_iso: {st.get('last_replan_iso')}",
        f"last_replan_skip_iso: {st.get('last_replan_skip_iso')}",
        f"last_catalyst_digest: {_digest_preview(st.get('last_catalyst_digest'))}",
        f"last_bootstrap_iso: {st.get('last_bootstrap_iso')}",
        f"last_bootstrap_summary: {_bootstrap_summary_one_line(st.get('last_bootstrap_summary'))}",
        f"last_pm_cycle_iso: {st.get('last_pm_cycle_iso')}",
        f"last_pm_executive_prefix: {(st.get('last_pm_executive_prefix') or '(none)')[:200]}",
        f"last_weekly_check_iso: {st.get('last_weekly_check_iso')}",
        f"last_weekly_scan_iso: {st.get('last_weekly_scan_iso')}",
        f"last_portfolio_tickers: {', '.join(st.get('last_portfolio_tickers') or [])}",
        "",
        "Pending jobs:",
    ]
    pend = [j for j in (st.get("jobs") or []) if j.get("status") == "pending"]
    if not pend:
        lines.append("  (none)")
    else:
        for j in sorted(pend, key=lambda x: str(x.get("scheduled_at") or "")):
            lines.append(
                f"  - {j.get('ticker')} @ {j.get('scheduled_at')} id={j.get('id')} {j.get('reason', '')[:80]}"
            )
    return "\n".join(lines)
