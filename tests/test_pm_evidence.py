from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradingagents.portfolio_advisor.evidence import collect_pm_evidence


def test_collect_pm_evidence_indexes_events_jobs_and_deep_report(tmp_path):
    # Dates are relative to today so the research stays inside the 30-day
    # freshness window as the real clock advances (a hardcoded date silently
    # ages past the staleness cutoff and flips NVDA to stale).
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    rdate = recent.strftime("%Y-%m-%d")
    jdate = (recent + timedelta(days=1)).strftime("%Y-%m-%d")

    event_log = tmp_path / "events.jsonl"
    results = tmp_path / "results"
    report_dir = results / "clerk_deep" / "NVDA"
    report_dir.mkdir(parents=True)
    (report_dir / f"{rdate}_clerk_triggered.md").write_text(
        "# Clerk-triggered deep research: NVDA\n"
        f"Date: {rdate}\n\n"
        "## Final decision\n\nHold; thesis intact.\n",
        encoding="utf-8",
    )
    event_log.write_text(
        (
            '{"timestamp":"' + rdate + 'T09:00:00+00:00","ticker":"NVDA",'
            '"event_type":"single_model_analysis","key_data":{"job_type":"thesis_check",'
            '"excerpt":"Hold; margin thesis intact."},"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    cfg = {
        "event_log_path": str(event_log),
        "results_dir": str(results),
        "portfolio_advisor_pm_evidence_stale_days": 30,
    }
    pending = [
        {
            "id": "job1",
            "ticker": "NVDA",
            "scheduled_at": f"{jdate}T09:00:00+00:00",
            "execution_tier": "single_model",
            "job_type": "thesis_check",
            "evidence_question": "Check margins",
        }
    ]

    ctx = collect_pm_evidence(cfg, ["NVDA"], pending_jobs=pending)
    ids = {e["id"] for e in ctx["evidence"]}

    assert "context:portfolio_snapshot:NVDA" in ids
    assert "job:job1" in ids
    assert f"event:single_model_analysis:NVDA:{rdate}" in ids
    assert any(i.startswith("file:clerk_deep:NVDA:") for i in ids)
    assert jdate in ctx["known_dates"]
    assert "NVDA" not in ctx["stale_tickers"]


def test_collect_pm_evidence_flags_missing_and_stale_research(tmp_path):
    event_log = tmp_path / "events.jsonl"
    event_log.write_text(
        (
            '{"timestamp":"2020-01-01T09:00:00+00:00","ticker":"OLD",'
            '"event_type":"single_model_analysis","key_data":{"excerpt":"Old hold."},"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    cfg = {
        "event_log_path": str(event_log),
        "results_dir": str(tmp_path / "missing"),
        "portfolio_advisor_pm_evidence_stale_days": 1,
    }

    ctx = collect_pm_evidence(cfg, ["OLD", "MISS"], pending_jobs=[])

    assert ctx["stale_tickers"]["OLD"]["reason"] == "stale_research"
    assert ctx["stale_tickers"]["MISS"]["reason"] == "missing_research"


def test_collect_pm_evidence_reads_structured_full_graph_decision(tmp_path):
    # Relative date so the decision stays fresh inside the 30-day window as the
    # real clock advances (a hardcoded date eventually trips the staleness gate).
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    rdate = recent.strftime("%Y-%m-%d")

    event_log = tmp_path / "events.jsonl"
    event_log.write_text(
        (
            '{"timestamp":"' + rdate + 'T09:00:00+00:00","ticker":"NVDA",'
            '"event_type":"full_graph_decision","key_data":{'
            '"decision_id":"dec1","trade_date":"' + rdate + '","rating":"Overweight",'
            '"confidence":"Medium","summary":"Add only if margins confirm.",'
            '"thesis":"AI demand intact.","thesis_break_metrics":["margin below 60%"]},'
            '"outcome":null}\n'
        ),
        encoding="utf-8",
    )
    cfg = {
        "event_log_path": str(event_log),
        "results_dir": str(tmp_path / "missing"),
        "portfolio_advisor_pm_evidence_stale_days": 30,
    }

    ctx = collect_pm_evidence(cfg, ["NVDA"], pending_jobs=[])
    row = next(e for e in ctx["evidence"] if e["id"] == f"event:full_graph_decision:NVDA:{rdate}")

    assert row["decision"] == "Overweight / Medium"
    assert row["summary"] == "Add only if margins confirm."
    assert ctx["latest_full_graph_decisions"]["NVDA"]["decision"] == "Overweight / Medium"
    assert "NVDA" not in ctx["stale_tickers"]
