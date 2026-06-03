"""Pydantic models for LLM-produced portfolio advisor schedules."""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


class AdvisorJobSpec(BaseModel):
    """One scheduled unit of work proposed by the advisor LLM."""

    ticker: str = Field(description="Uppercase equity symbol, e.g. NVDA or 7203.T")
    scheduled_at: str = Field(
        description="ISO-8601 datetime in UTC for when to run deep research, e.g. 2026-05-14T21:00:00+00:00",
    )
    action: Literal["deep_research", "watch_only", "skip"] = Field(
        description="deep_research = queue a full graph run; watch_only = narrative only (no graph); skip = ignore",
    )
    rationale: str = Field(
        default="",
        description="One or two sentences: why this timing and action.",
    )
    execution_tier: Literal["single_model", "full_graph"] = Field(
        default="single_model",
        description="single_model: one reasoning LLM pass; full_graph: TradingAgentsGraph.propagate",
    )
    job_type: Literal[
        "thesis_check",
        "weekly_summary",
        "post_earnings",
        "routine_monitoring",
        "catalyst_scan",
    ] = Field(
        default="routine_monitoring",
        description=(
            "Type of analysis this job should run. Determines prompt branching in "
            "single_model_analysis._build_prompt."
        ),
    )
    flags: List[str] = Field(
        default_factory=list,
        description="Planner or validator flags e.g. PRE_EARNINGS_TRIM_ACTIVE",
    )


class AdvisorPlanResult(BaseModel):
    """Structured plan returned by the portfolio advisor LLM."""

    executive_summary: str = Field(
        description="Plain-language portfolio memo for the investor (advisory only, no orders).",
    )
    jobs: List[AdvisorJobSpec] = Field(
        description="Up to 15 prioritized jobs; prefer catalysts within 21 days and larger risk names.",
    )
    immediate_actions: List[str] = Field(
        default_factory=list,
        description="Urgent notes for the human right now (e.g. consider exit, trim, or wait).",
    )


class AdvisorPMTickerStance(BaseModel):
    """Per-name stance from the advisor-level portfolio manager (advisory only)."""

    ticker: str = Field(description="Uppercase symbol as in the portfolio export.")
    stance: Literal["hold", "buy", "sell", "watch", "trim", "add", "unknown"] = Field(
        description="Advisory stance only; never implies an executed trade.",
    )
    rationale: str = Field(default="", description="One or two sentences.")
    evidence_refs: List[str] = Field(
        default_factory=list,
        description=(
            "Evidence IDs supporting this stance, e.g. event log IDs, research file IDs, "
            "pending job IDs, or context refs supplied in the PM prompt."
        ),
    )


class AdvisorPMAppendJob(BaseModel):
    """Optional extra pending advisor job the PM wants queued (live portfolio names only)."""

    ticker: str = Field(description="Uppercase symbol in the current eToro book.")
    execution_tier: Literal["single_model", "full_graph"] = Field(
        default="single_model",
        description="single_model for a light memo; full_graph for a TradingAgents graph run.",
    )
    job_type: Literal[
        "thesis_check",
        "weekly_summary",
        "post_earnings",
        "routine_monitoring",
        "catalyst_scan",
    ] = Field(default="thesis_check")
    rationale: str = Field(default="", description="Why this job should run.")
    source: Literal[
        "pm_missing_evidence",
        "pm_human_request",
        "pm_conflict",
        "pm_stale_evidence",
        "pm_followup",
    ] = Field(
        default="pm_followup",
        description="Why the PM is appending this job instead of relying on routine planner cadence.",
    )
    evidence_question: str = Field(
        default="",
        description="The specific question this job should answer, used for dedupe and audit logs.",
    )
    supersedes_job_id: str = Field(
        default="",
        description="Pending job ID this request replaces or accelerates, when applicable.",
    )


class AdvisorPMCandidateComparison(BaseModel):
    """PM comparison of a promoted candidate against current holdings."""

    candidate_ticker: str = Field(description="Uppercase candidate symbol. This may be outside the live portfolio.")
    better_than_current_holding: Literal["yes", "no", "unknown"] = Field(
        default="unknown",
        description="Whether the candidate appears better than at least one current holding on available evidence.",
    )
    replace_or_add: Literal["replace", "add", "watch", "reject", "unknown"] = Field(
        default="unknown",
        description="Portfolio-level next step for the candidate. Advisory only; no trade execution.",
    )
    compared_against: List[str] = Field(
        default_factory=list,
        description="Live portfolio tickers the candidate was compared against.",
    )
    rationale: str = Field(default="", description="Short comparison rationale grounded in evidence.")
    evidence_refs: List[str] = Field(
        default_factory=list,
        description="Evidence refs supporting this candidate comparison.",
    )


class WatchdogUpdate(BaseModel):
    """PM-issued update to a price-watchdog latch for one ticker."""

    ticker: str = Field(description="Ticker whose watchdog latch to update.")
    action: Literal["acknowledge", "mute", "clear", "note"] = Field(
        description=(
            "acknowledge = seen/handled, keep latched but silent; mute = suppress until it escalates; "
            "clear = drop the latch entirely; note = just attach a note without changing status."
        ),
    )
    note: str = Field(default="", description="Short reason/context for this update. Max 300 chars.")


class AdvisorPMCycleResult(BaseModel):
    """One PM council pass: big picture, stances, forward work, durable memory note."""

    executive_summary: str = Field(
        description="Short portfolio-wide memo: risk posture, themes, what changed.",
    )
    stances: List[AdvisorPMTickerStance] = Field(
        default_factory=list,
        description="Stance for each material name; omit pure cash or noise.",
    )
    forward_tasks: List[str] = Field(
        default_factory=list,
        description="Concrete next tasks (research, replan, verify thesis, schedule job, etc.).",
    )
    memory_note: str = Field(
        default="",
        description="What the PM wants remembered for the next cycle (one tight paragraph).",
    )
    request_replan: bool = Field(
        default=False,
        description=(
            "If true, run a full advisor replan after this cycle (cancels existing pending jobs, "
            "runs planner LLM, queues a fresh schedule). Use when the book or priorities shifted materially."
        ),
    )
    replan_rationale: str = Field(
        default="",
        description="One short sentence logged when request_replan is true.",
    )
    replan_reason_code: Literal[
        "portfolio_changed",
        "schedule_conflict",
        "coverage_gap",
        "human_requested",
        "other",
    ] = Field(
        default="other",
        description="Structured reason for request_replan; used for PM governance logs.",
    )
    append_jobs: List[AdvisorPMAppendJob] = Field(
        default_factory=list,
        description=(
            "Up to five extra pending jobs appended after any replan. Tickers must still be in the "
            "live export; unknown symbols are skipped."
        ),
    )
    evidence_refs: List[str] = Field(
        default_factory=list,
        description="Portfolio-wide evidence refs used for the executive summary and forward tasks.",
    )
    candidate_comparisons: List[AdvisorPMCandidateComparison] = Field(
        default_factory=list,
        description=(
            "Use only for promoted candidates supplied in extra_context. Candidate tickers are not live "
            "stances unless they also appear in the live portfolio snapshot."
        ),
    )
    push_note: str = Field(
        default="",
        description=(
            "A short observation worth pushing to the human right now — deadline approaching, "
            "unexpected data point, stance change, catalyst in next 48h. Max 280 chars. "
            "Leave empty if nothing urgent or new. Do not repeat what was already said this cycle."
        ),
    )
    watchdog_updates: List[WatchdogUpdate] = Field(
        default_factory=list,
        description=(
            "Updates to active price-watchdog latches shown in context. Use to acknowledge, mute, "
            "clear, or annotate a watchdog trigger once you have reasoned about it. Tickers not in "
            "the live snapshot are ignored."
        ),
    )
