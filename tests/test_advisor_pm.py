"""Tests for advisor-level PM council (no live LLM)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.portfolio_advisor.advisor_pm import (
    _append_pm_memory_md,
    _format_close_instruction,
    _notify_action_stances,
    _pm_model,
    apply_pm_cycle_followups,
    load_recent_pm_cycles,
    optional_pm_cycle_on_portfolio_change,
    pm_log_path,
    pm_memory_path,
    run_pm_after_full_graph_if_enabled,
    run_pm_cycle,
)
from tradingagents.portfolio_advisor.models import (
    AdvisorPMAppendJob,
    AdvisorPMCandidateComparison,
    AdvisorPMCycleResult,
    AdvisorPMTickerStance,
)


def test_run_pm_cycle_writes_logs(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
    fake = AdvisorPMCycleResult(
        executive_summary="Test memo",
        stances=[AdvisorPMTickerStance(ticker="NVDA", stance="hold", rationale="ok")],
        forward_tasks=["Replan next week"],
        memory_note="Watch semis",
    )
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["NVDA"], [])
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                with patch("tradingagents.portfolio_advisor.advisor_pm.append_event"):
                    out = run_pm_cycle(cfg, trigger="test_trigger")
    assert out.executive_summary == "Test memo"
    assert pm_log_path(cfg).is_file()
    rows = load_recent_pm_cycles(cfg, limit=3)
    assert len(rows) == 1
    assert rows[0]["trigger"] == "test_trigger"
    assert rows[0]["result"]["stances"][0]["ticker"] == "NVDA"


def test_pm_default_model_is_gpt_55():
    assert _pm_model({}) == "openai/gpt-5.5"


def test_format_close_instruction_summarizes_sell_in_plain_english():
    rows = [
        {
            "symbolFull": "TEAM",
            "positionId": 101,
            "openRate": 167.49,
            "units": 1.5,
            "unitsBaseValueDollars": 130.0,
            "unrealizedPnL": -42.5,
        },
        {
            "symbolFull": "TEAM",
            "positionId": 102,
            "openRate": 154.93,
            "units": 2.0,
            "unitsBaseValueDollars": 177.0,
            "unrealizedPnL": -21.0,
        },
    ]

    out = _format_close_instruction("TEAM", "sell", "Exit the TEAM position.", rows)

    # Conversational format (commit 09e03ad): no position IDs / capital-base / per-lot
    # P&L jargon — just plain English. A bare sell with no specific lots named closes
    # the whole position, and the units aggregate across both lots (1.5 + 2.0 = 3.5).
    assert "close the whole position" in out
    assert "3.5 shares" in out
    assert "$244" in out


def test_notify_action_stances_suppresses_unchanged_repeats(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "message_log_path": str(tmp_path / "messages.jsonl")}
    res = AdvisorPMCycleResult(
        executive_summary="x",
        stances=[AdvisorPMTickerStance(ticker="TEAM", stance="sell", rationale="Exit TEAM.")],
    )
    rows = [
        {
            "symbolFull": "TEAM",
            "positionId": 1,
            "openRate": 100,
            "units": 2,
            "unitsBaseValueDollars": 180,
            "unrealizedPnL": -20,
        }
    ]

    with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message") as send:
        first = _notify_action_stances(cfg, res, rows)
        second = _notify_action_stances(cfg, res, rows)

    assert first is True
    assert second is False
    assert send.call_count == 1
    body = send.call_args[0][2]
    # Conversational close instruction (caller prepends "TEAM: ").
    assert "TEAM" in body
    assert "close the whole position" in body


def test_run_pm_cycle_combines_action_alert_and_push_note(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "event_log_path": str(tmp_path / "events.jsonl")}
    event_log = tmp_path / "events.jsonl"
    event_log.write_text(
        (
            '{"timestamp":"2026-05-15T09:00:00+00:00","ticker":"TEAM",'
            '"event_type":"full_graph_decision","key_data":{"rating":"Sell",'
            '"summary":"Sell TEAM."},"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    fake = AdvisorPMCycleResult(
        executive_summary="memo",
        stances=[
            AdvisorPMTickerStance(
                ticker="TEAM",
                stance="sell",
                rationale="Sell based on latest full graph.",
                evidence_refs=["event:full_graph_decision:TEAM:2026-05-15"],
            )
        ],
        push_note="Do not send this as a separate PM message.",
    )
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm
    rows = [{"symbolFull": "TEAM", "positionId": 1, "openRate": 100, "units": 2, "unitsBaseValueDollars": 180}]

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["TEAM"], rows)
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message") as send:
                    run_pm_cycle(cfg, trigger="test_trigger")

    assert send.call_count == 1
    assert send.call_args[0][1] == "PM"
    # Body should be the PM's own push_note (no rigid header, no "PM note:" prefix).
    assert send.call_args[0][2] == "Do not send this as a separate PM message."
    assert send.call_args.kwargs.get("urgent") is True


def test_run_pm_cycle_prompt_includes_latest_research(tmp_path):
    event_log = tmp_path / "events.jsonl"
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "event_log_path": str(event_log),
    }
    event_log.write_text(
        (
            '{"timestamp":"2026-05-15T09:00:00+00:00","ticker":"NVDA",'
            '"event_type":"single_model_analysis","key_data":{"job_type":"thesis_check",'
            '"excerpt":"Hold; margin thesis intact after latest check."},"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    fake = AdvisorPMCycleResult(executive_summary="Test memo")
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["NVDA"], [])
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                with patch("tradingagents.portfolio_advisor.advisor_pm.append_event"):
                    run_pm_cycle(cfg, trigger="test_trigger")

    prompt = m_llm.bind_tools.return_value.invoke.call_args[0][0][0].content
    assert "Latest completed research decisions/results:" in prompt
    assert "Hold; margin thesis intact" in prompt
    assert "Do not create facts" in prompt
    assert "Retrieved evidence refs and known dates" in prompt


def test_ntfy_question_prompt_includes_full_context_and_anti_echo_guard(tmp_path):
    """Chat-mode (ntfy_question) gets the SAME full context as scheduled cycles.

    Commit 6ae7e93 deliberately stopped stripping PM memory / prior context for
    ntfy_question — the lightweight prompt made the model defer with empty replies.
    The prompt now carries the prior PM memory AND the standing instruction telling
    the model not to parrot it; we assert both, plus the latest evidence + question.
    """
    event_log = tmp_path / "events.jsonl"
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "event_log_path": str(event_log),
        "portfolio_advisor_pm_prior_cycles": 2,
    }
    event_log.write_text(
        (
            '{"timestamp":"2026-05-15T09:00:00+00:00","ticker":"TEAM",'
            '"event_type":"single_model_analysis","key_data":{"job_type":"thesis_check",'
            '"excerpt":"Latest TEAM evidence."},"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    prior = AdvisorPMCycleResult(
        executive_summary="PRIOR PM EXECUTIVE SUMMARY",
        memory_note="PRIOR PM MEMORY NOTE",
    )
    _append_pm_memory_md(cfg, trigger="old_cycle", result=prior)
    _write_pm_memory_update = __import__(
        "tradingagents.portfolio_advisor.advisor_pm",
        fromlist=["_write_pm_memory_update"],
    )._write_pm_memory_update
    _write_pm_memory_update(cfg, "PRIOR STRUCTURED MEMORY", "old_cycle")
    fake = AdvisorPMCycleResult(executive_summary="answer")
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["TEAM"], [])
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                run_pm_cycle(cfg, trigger="ntfy_question", extra_context="what should I do?")

    prompt = m_llm.bind_tools.return_value.invoke.call_args[0][0][0].content
    assert "Latest TEAM evidence." in prompt
    assert "what should I do?" in prompt
    # Full PM context is fed to chat mode (commit 6ae7e93) ...
    assert "PRIOR PM EXECUTIVE SUMMARY" in prompt
    assert "PRIOR STRUCTURED MEMORY" in prompt
    # ... but the standing instruction tells the model not to parrot it.
    assert "Do not echo prior PM prose" in prompt


def test_run_pm_cycle_downgrades_unsupported_action_stance(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "event_log_path": str(tmp_path / "events.jsonl")}
    fake = AdvisorPMCycleResult(
        executive_summary="Sell NVDA because the thesis is broken.",
        stances=[AdvisorPMTickerStance(ticker="NVDA", stance="sell", rationale="Thesis broken.")],
    )
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["NVDA"], [])
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message"):
                    out = run_pm_cycle(cfg, trigger="test_trigger")

    assert out.stances[0].stance == "unknown"
    assert "insufficient" in out.stances[0].rationale.lower()
    rows = load_recent_pm_cycles(cfg, limit=1)
    assert rows[0]["validation_overrides"]


def test_run_pm_cycle_keeps_action_stance_with_research_evidence(tmp_path):
    # Recent date so the evidence stays fresh (a hardcoded date eventually trips
    # the staleness gate and the stance gets downgraded for the wrong reason).
    rdate = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    event_log = tmp_path / "events.jsonl"
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "event_log_path": str(event_log),
    }
    event_log.write_text(
        (
            '{"timestamp":"' + rdate + 'T09:00:00+00:00","ticker":"NVDA",'
            '"event_type":"single_model_analysis","key_data":{"job_type":"thesis_check",'
            '"excerpt":"Trim; thesis weakened after latest check."},"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    fake = AdvisorPMCycleResult(
        executive_summary="Trim NVDA based on latest research.",
        stances=[
            AdvisorPMTickerStance(
                ticker="NVDA",
                stance="trim",
                rationale="Latest thesis check weakened.",
                evidence_refs=[f"event:single_model_analysis:NVDA:{rdate}"],
            )
        ],
    )
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["NVDA"], [])
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message"):
                    out = run_pm_cycle(cfg, trigger="test_trigger")

    assert out.stances[0].stance == "trim"
    assert out.stances[0].evidence_refs == [f"event:single_model_analysis:NVDA:{rdate}"]


def test_run_pm_cycle_downgrades_conflict_with_latest_full_graph(tmp_path):
    # Recent date so the conflict path (not the staleness path) is the one tested.
    rdate = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    event_log = tmp_path / "events.jsonl"
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "event_log_path": str(event_log),
    }
    event_log.write_text(
        (
            '{"timestamp":"' + rdate + 'T09:00:00+00:00","ticker":"NVDA",'
            '"event_type":"full_graph_decision","key_data":{'
            '"decision_id":"dec1","trade_date":"' + rdate + '","rating":"Hold",'
            '"confidence":"Medium","summary":"Hold; evidence balanced.",'
            '"thesis":"Balanced thesis."},"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    fake = AdvisorPMCycleResult(
        executive_summary="Sell NVDA.",
        stances=[
            AdvisorPMTickerStance(
                ticker="NVDA",
                stance="sell",
                rationale="Looks broken.",
                evidence_refs=[f"event:full_graph_decision:NVDA:{rdate}"],
            )
        ],
    )
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["NVDA"], [])
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message"):
                    out = run_pm_cycle(cfg, trigger="test_trigger")

    assert out.stances[0].stance == "unknown"
    assert out.append_jobs[0].source == "pm_conflict"
    rows = load_recent_pm_cycles(cfg, limit=1)
    assert any(o["action"] == "downgraded_full_graph_conflict" for o in rows[0]["validation_overrides"])


def test_run_pm_cycle_allows_full_graph_disagreement_with_newer_evidence(tmp_path):
    # Recent date so both events stay fresh; this test is about newer single_model
    # evidence overriding an older full_graph, not about staleness.
    rdate = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    event_log = tmp_path / "events.jsonl"
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "event_log_path": str(event_log),
    }
    event_log.write_text(
        (
            '{"timestamp":"' + rdate + 'T09:00:00+00:00","ticker":"NVDA",'
            '"event_type":"full_graph_decision","key_data":{'
            '"decision_id":"dec1","trade_date":"' + rdate + '","rating":"Hold",'
            '"confidence":"Medium","summary":"Hold; evidence balanced.",'
            '"thesis":"Balanced thesis."},"outcome":null}\n'
            '{"timestamp":"' + rdate + 'T12:00:00+00:00","ticker":"NVDA",'
            '"event_type":"single_model_analysis","key_data":{"job_type":"thesis_check",'
            '"excerpt":"Trim; new margin warning changed the thesis."},"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    fake = AdvisorPMCycleResult(
        executive_summary="Trim NVDA.",
        stances=[
            AdvisorPMTickerStance(
                ticker="NVDA",
                stance="trim",
                rationale="New margin warning.",
                evidence_refs=[f"event:single_model_analysis:NVDA:{rdate}"],
            )
        ],
    )
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["NVDA"], [])
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message"):
                    out = run_pm_cycle(cfg, trigger="test_trigger")

    assert out.stances[0].stance == "trim"
    assert not out.append_jobs


def test_run_pm_cycle_uses_candidate_comparisons_for_non_held_candidate(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "event_log_path": str(tmp_path / "events.jsonl")}
    fake = AdvisorPMCycleResult(
        executive_summary="ASML is interesting but compare it rather than treating it as held.",
        stances=[AdvisorPMTickerStance(ticker="ASML", stance="buy", rationale="Candidate looks strong.")],
        candidate_comparisons=[
            AdvisorPMCandidateComparison(
                candidate_ticker="ASML",
                better_than_current_holding="unknown",
                replace_or_add="watch",
                compared_against=["NVDA", "ASML"],
                rationale="Needs comparison against current semis.",
                evidence_refs=["candidate:ASML"],
            )
        ],
    )
    m_llm = MagicMock()
    m_client = MagicMock()
    m_client.get_llm.return_value = m_llm

    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "portfolio text here", ["NVDA"], [])
        with patch("tradingagents.portfolio_advisor.advisor_pm.create_llm_client", return_value=m_client):
            with patch("tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text", return_value=fake):
                out = run_pm_cycle(
                    cfg,
                    trigger="candidate_comparison",
                    extra_context="Candidate comparison request:\n- ASML",
                )

    assert out.stances[0].stance == "unknown"
    assert "not in the live portfolio" in out.stances[0].rationale
    assert out.candidate_comparisons[0].candidate_ticker == "ASML"
    assert out.candidate_comparisons[0].compared_against == ["NVDA"]
    rows = load_recent_pm_cycles(cfg, limit=1)
    assert any(o["action"] == "downgraded_non_live_ticker" for o in rows[0]["validation_overrides"])


def test_append_pm_memory_writes_candidate_comparisons(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "portfolio_advisor_pm_unified_memory": False}
    res = AdvisorPMCycleResult(
        executive_summary="memo",
        candidate_comparisons=[
            AdvisorPMCandidateComparison(
                candidate_ticker="ASML",
                better_than_current_holding="yes",
                replace_or_add="add",
                compared_against=["NVDA"],
                rationale="Diversifies semis.",
            )
        ],
    )

    _append_pm_memory_md(cfg, trigger="candidate_comparison", result=res)

    text = pm_memory_path(cfg).read_text(encoding="utf-8")
    assert "Candidate comparisons" in text
    assert "ASML" in text
    assert "better_than_current_holding=yes" in text


def test_optional_pm_after_bootstrap_calls_run_pm_cycle(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "portfolio_advisor_pm_cycle_after_bootstrap": True}
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        from tradingagents.portfolio_advisor.bootstrap import _optional_pm_cycle_after_bootstrap

        _optional_pm_cycle_after_bootstrap(cfg)
    m.assert_called_once_with(cfg, trigger="after_bootstrap")


def test_optional_pm_after_bootstrap_skipped_when_disabled(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "portfolio_advisor_pm_cycle_after_bootstrap": False}
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        from tradingagents.portfolio_advisor.bootstrap import _optional_pm_cycle_after_bootstrap

        _optional_pm_cycle_after_bootstrap(cfg)
    m.assert_not_called()


def test_optional_pm_after_bootstrap_skipped_when_pm_globally_disabled(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_enabled": False,
        "portfolio_advisor_pm_cycle_after_bootstrap": True,
    }
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        from tradingagents.portfolio_advisor.bootstrap import _optional_pm_cycle_after_bootstrap

        _optional_pm_cycle_after_bootstrap(cfg)
    m.assert_not_called()


def test_run_pm_after_full_graph_calls_run_pm_cycle(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        run_pm_after_full_graph_if_enabled(
            cfg,
            ticker="nvda",
            trade_date="2026-05-01",
            final_state={"final_trade_decision": "Hold — test"},
        )
    m.assert_called_once()
    assert m.call_args[1]["trigger"] == "after_langgraph"
    assert "NVDA" in m.call_args[1]["extra_context"]


def test_run_pm_after_full_graph_skipped_when_disabled(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_after_each_langgraph": False,
    }
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        run_pm_after_full_graph_if_enabled(
            cfg, ticker="AAPL", trade_date="2026-05-01", final_state={"final_trade_decision": "x"}
        )
    m.assert_not_called()


def test_run_pm_after_full_graph_skipped_when_pm_globally_disabled(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_enabled": False,
        "portfolio_advisor_pm_after_each_langgraph": True,
    }
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        run_pm_after_full_graph_if_enabled(
            cfg, ticker="AAPL", trade_date="2026-05-01", final_state={"final_trade_decision": "x"}
        )
    m.assert_not_called()


def test_apply_pm_followups_replan(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_apply_actions": True,
    }
    res = AdvisorPMCycleResult(
        executive_summary="x",
        request_replan=True,
        replan_rationale="book changed",
    )
    with patch("tradingagents.portfolio_advisor.service.run_replan", return_value="replanned") as m:
        out = apply_pm_cycle_followups(cfg, res)
    m.assert_called_once()
    assert out["replan_outcome"] == "replanned"


def test_apply_pm_followups_append_jobs(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_apply_actions": True,
    }
    res = AdvisorPMCycleResult(
        executive_summary="x",
        append_jobs=[
            AdvisorPMAppendJob(ticker="NVDA", execution_tier="single_model", job_type="thesis_check", rationale="pm"),
        ],
    )
    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "t", ["NVDA"], [])
        out = apply_pm_cycle_followups(cfg, res)
    assert out["jobs_appended"] == 1
    from tradingagents.portfolio_advisor import state as pa_state

    st = pa_state.load_state(cfg)
    pend = [j for j in st.get("jobs") or [] if j.get("status") == "pending"]
    assert len(pend) == 1
    assert pend[0]["ticker"] == "NVDA"
    assert pend[0]["flags"] == ["PM_APPEND"]
    assert pend[0]["source"] == "pm_followup"
    assert pend[0]["evidence_question"] == "pm"


def test_apply_pm_followups_dedupes_matching_pending_job(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_apply_actions": True,
    }
    from tradingagents.portfolio_advisor import state as pa_state

    st = pa_state.default_state()
    st["jobs"] = [
        {
            "id": "existing1",
            "ticker": "NVDA",
            "scheduled_at": "2026-05-16T09:00:00+00:00",
            "kind": "deep_research",
            "reason": "Check margins",
            "status": "pending",
            "execution_tier": "single_model",
            "job_type": "thesis_check",
            "source": "planner",
            "evidence_question": "Check margins",
        }
    ]
    pa_state.save_state(cfg, st)
    res = AdvisorPMCycleResult(
        executive_summary="x",
        append_jobs=[
            AdvisorPMAppendJob(
                ticker="NVDA",
                execution_tier="single_model",
                job_type="thesis_check",
                rationale="Check margins",
                evidence_question="Check margins",
                source="pm_missing_evidence",
            ),
        ],
    )
    with patch("tradingagents.portfolio_advisor.advisor_pm.etoro_scan.fetch_portfolio_rows") as fetch:
        fetch.return_value = ({}, "t", ["NVDA"], [])
        out = apply_pm_cycle_followups(cfg, res)

    assert out["jobs_appended"] == 0
    assert out["jobs_deduped"][0]["existing_job_id"] == "existing1"
    st2 = pa_state.load_state(cfg)
    pend = [j for j in st2.get("jobs") or [] if j.get("status") == "pending"]
    assert len(pend) == 1


def test_apply_pm_followups_disabled_skips_side_effects(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "portfolio_advisor_pm_apply_actions": False}
    res = AdvisorPMCycleResult(executive_summary="x", request_replan=True, append_jobs=[AdvisorPMAppendJob(ticker="NVDA")])
    with patch("tradingagents.portfolio_advisor.service.run_replan") as m:
        out = apply_pm_cycle_followups(cfg, res)
    m.assert_not_called()
    assert out["apply_enabled"] is False


def test_optional_pm_on_portfolio_change_skipped_when_pm_globally_disabled(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_enabled": False,
        "portfolio_advisor_pm_cycle_on_portfolio_change": True,
    }
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        optional_pm_cycle_on_portfolio_change(
            cfg,
            trigger="t",
            old_portfolio_text_hash="a",
            new_portfolio_text_hash="b",
            tickers_added=["X"],
        )
    m.assert_not_called()


def test_optional_pm_on_portfolio_change_skipped_when_disabled(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_cycle_on_portfolio_change": False,
    }
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        optional_pm_cycle_on_portfolio_change(
            cfg,
            trigger="t",
            old_portfolio_text_hash="a",
            new_portfolio_text_hash="b",
            tickers_added=["X"],
        )
    m.assert_not_called()


def test_optional_pm_on_portfolio_change_noop_when_no_signal(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        optional_pm_cycle_on_portfolio_change(
            cfg,
            trigger="t",
            old_portfolio_text_hash=None,
            new_portfolio_text_hash="same",
            tickers_added=[],
            tickers_removed=[],
        )
    m.assert_not_called()


def test_optional_pm_on_portfolio_change_runs_on_ticker_delta(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        optional_pm_cycle_on_portfolio_change(
            cfg,
            trigger="weekly_portfolio_change",
            old_portfolio_text_hash=None,
            new_portfolio_text_hash="unchanged",
            tickers_added=["nvda"],
            tickers_removed=[],
        )
    m.assert_called_once()
    assert m.call_args[1]["trigger"] == "weekly_portfolio_change"
    assert "NVDA" in (m.call_args[1].get("extra_context") or "")


def test_pm_memory_path_unified_uses_memory_log_path(tmp_path):
    mem = tmp_path / "trading_memory.md"
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_unified_memory": True,
        "memory_log_path": str(mem),
    }
    assert pm_memory_path(cfg) == mem


def test_pm_memory_path_legacy_uses_advisor_dir(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "portfolio_advisor_pm_unified_memory": False}
    assert pm_memory_path(cfg) == tmp_path / "pa" / "pm_memory.md"


def test_append_pm_unified_writes_sentinel_and_entry_separator(tmp_path):
    mem = tmp_path / "trading_memory.md"
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_pm_unified_memory": True,
        "memory_log_path": str(mem),
    }
    res = AdvisorPMCycleResult(executive_summary="memo", stances=[])
    _append_pm_memory_md(cfg, trigger="unit", result=res)
    text = mem.read_text(encoding="utf-8")
    assert "<!-- TRADINGAGENTS_PM_ADVISOR_LOG -->" in text
    assert "PM cycle" in text
    assert TradingMemoryLog._SEPARATOR in text


def test_append_pm_legacy_file_has_no_trading_separator(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "portfolio_advisor_pm_unified_memory": False}
    res = AdvisorPMCycleResult(executive_summary="memo", stances=[])
    _append_pm_memory_md(cfg, trigger="unit", result=res)
    p = pm_memory_path(cfg)
    text = p.read_text(encoding="utf-8")
    assert "<!-- TRADINGAGENTS_PM_ADVISOR_LOG -->" in text
    assert TradingMemoryLog._SEPARATOR not in text


def test_prior_pm_context_empty_when_cycles_zero(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa"), "portfolio_advisor_pm_prior_cycles": 0}
    from tradingagents.portfolio_advisor.advisor_pm import _prior_pm_context

    assert _prior_pm_context(cfg) == ""


def test_optional_pm_on_portfolio_change_runs_on_hash_delta(tmp_path):
    cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
    with patch("tradingagents.portfolio_advisor.advisor_pm.run_pm_cycle") as m:
        optional_pm_cycle_on_portfolio_change(
            cfg,
            trigger="portfolio_book_changed",
            old_portfolio_text_hash="aa" * 32,
            new_portfolio_text_hash="bb" * 32,
            tickers_added=[],
            tickers_removed=[],
        )
    m.assert_called_once()
    assert m.call_args[1]["trigger"] == "portfolio_book_changed"
    assert "fingerprint" in (m.call_args[1].get("extra_context") or "").lower()
