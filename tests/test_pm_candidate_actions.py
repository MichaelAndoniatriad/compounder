"""Portfolio-level candidate decisions: high-conviction replace/add -> proposed trades.

Covers _apply_candidate_comparisons (the handler that turns the PM's candidate
comparisons into proposed_trades.jsonl rows + a Telegram nudge). The PM cycle and
eToro fetch are not exercised here — we call the handler directly with a built result.
"""
from typing import Any, Dict, List

import pytest

from tradingagents.portfolio_advisor import advisor_pm
from tradingagents.portfolio_advisor.models import (
    AdvisorPMCandidateComparison,
    AdvisorPMCycleResult,
)


@pytest.fixture
def captured(monkeypatch):
    """Capture proposals.add() and messaging.send_advisor_message() calls."""
    proposals: List[Dict[str, Any]] = []
    messages: List[str] = []

    import tradingagents.portfolio_advisor.proposals as proposals_mod
    import tradingagents.portfolio_advisor.messaging as messaging_mod

    def fake_add(cfg, **kw):
        proposals.append(kw)
        return {"ok": True, **kw}

    def fake_send(cfg, source, body, **kw):
        messages.append(body)
        return True

    monkeypatch.setattr(proposals_mod, "add", fake_add)
    monkeypatch.setattr(messaging_mod, "send_advisor_message", fake_send)
    return {"proposals": proposals, "messages": messages}


def _result(*comparisons) -> AdvisorPMCycleResult:
    return AdvisorPMCycleResult(
        executive_summary="t",
        candidate_comparisons=list(comparisons),
    )


def test_high_conviction_replace_records_sell_and_buy(captured):
    res = _result(
        AdvisorPMCandidateComparison(
            candidate_ticker="veev",
            replace_or_add="replace",
            replace_ticker="ADBE",
            proposed_size_usd=500,
            target_sleeve="core",
            conviction="high",
            rationale="Better growth-adjusted value than ADBE.",
        )
    )
    applied = advisor_pm._apply_candidate_comparisons({}, res, live_tickers={"ADBE", "NVDA"})

    sides = {(p["ticker"], p["action"]) for p in captured["proposals"]}
    assert ("ADBE", "sell") in sides
    assert ("VEEV", "buy") in sides
    buy = next(p for p in captured["proposals"] if p["action"] == "buy")
    assert buy["approx_usd"] == 500 and buy["sleeve"] == "core"
    # The sell ticket carries the replace linkage; proposals.add() emits the
    # order tickets now, so the handler itself no longer sends a message.
    sell = next(p for p in captured["proposals"] if p["action"] == "sell")
    assert "Replace with VEEV" in sell["reason"]
    assert applied[0]["decision"] == "replace" and applied[0]["replace_ticker"] == "ADBE"


def test_replace_with_non_live_holding_downgrades_to_add(captured):
    res = _result(
        AdvisorPMCandidateComparison(
            candidate_ticker="INCY",
            replace_or_add="replace",
            replace_ticker="ZZZZ",  # not in the book
            proposed_size_usd=400,
            target_sleeve="core",
            conviction="high",
        )
    )
    applied = advisor_pm._apply_candidate_comparisons({}, res, live_tickers={"ADBE"})

    actions = [(p["ticker"], p["action"]) for p in captured["proposals"]]
    assert actions == [("INCY", "buy")]  # no sell for a non-live holding
    assert applied[0]["decision"] == "add"


def test_zero_size_falls_back_to_starter(captured):
    res = _result(
        AdvisorPMCandidateComparison(
            candidate_ticker="MPWR", replace_or_add="add",
            proposed_size_usd=0, conviction="high",
        )
    )
    advisor_pm._apply_candidate_comparisons(
        {"portfolio_advisor_pm_candidate_starter_usd": 350}, res, live_tickers=set()
    )
    buy = captured["proposals"][0]
    assert buy["approx_usd"] == 350


def test_low_conviction_and_watch_reject_are_advisory_only(captured):
    res = _result(
        AdvisorPMCandidateComparison(candidate_ticker="A", replace_or_add="add", conviction="medium"),
        AdvisorPMCandidateComparison(candidate_ticker="B", replace_or_add="watch", conviction="high"),
        AdvisorPMCandidateComparison(candidate_ticker="C", replace_or_add="reject", conviction="high"),
    )
    applied = advisor_pm._apply_candidate_comparisons({}, res, live_tickers=set())
    assert captured["proposals"] == []
    assert captured["messages"] == []
    assert applied == []


def test_kill_switch_disables_recording(captured):
    res = _result(
        AdvisorPMCandidateComparison(
            candidate_ticker="VEEV", replace_or_add="add", conviction="high", proposed_size_usd=500,
        )
    )
    applied = advisor_pm._apply_candidate_comparisons(
        {"portfolio_advisor_pm_apply_candidate_actions": False}, res, live_tickers=set()
    )
    assert applied == [] and captured["proposals"] == []
