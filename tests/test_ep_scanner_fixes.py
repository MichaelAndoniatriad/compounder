"""Tests for EP scanner fixes: Yahoo RSS enabled, catalyst date validation.

Scope:
1. default_config has yahoo_finance_rss enabled and the new max_days_out key.
2. validate_catalyst_date helper: past date, future date, too-far date, bad format, missing.
3. candidates.evaluate_candidate: catalyst-sleeve with invalid/past date fails
   the catalyst gate with reason 'catalyst_date_invalid'.
4. pm_tools.propose_trade: past/invalid date rejects with an informative error.
5. pm_tools.emit_ep_candidate: invalid date stays advisory-only; valid date in
   alpaca mode files a proposal; invalid date in alpaca mode does NOT file a
   proposal.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Dict[str, Any]:
    """Sandboxed config that never touches ~/.tradingagents."""
    return {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_catalyst_max_days_out": 90,
    }


def _future(days: int = 10) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _past(days: int = 5) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# 1. default_config
# ---------------------------------------------------------------------------


def test_default_config_has_yahoo_rss_enabled():
    from tradingagents.default_config import DEFAULT_CONFIG
    sources = DEFAULT_CONFIG.get("ep_scanner_news_sources", [])
    assert "yahoo_finance_rss" in sources, (
        "yahoo_finance_rss must be in default ep_scanner_news_sources"
    )
    assert "alpha_vantage" in sources, "alpha_vantage must still be present"


def test_default_config_has_catalyst_max_days_out():
    from tradingagents.default_config import DEFAULT_CONFIG
    val = DEFAULT_CONFIG.get("portfolio_advisor_catalyst_max_days_out")
    assert val is not None, "portfolio_advisor_catalyst_max_days_out must exist in DEFAULT_CONFIG"
    assert int(val) == 90


# ---------------------------------------------------------------------------
# 2. validate_catalyst_date helper
# ---------------------------------------------------------------------------


def test_validate_missing_date():
    from tradingagents.portfolio_advisor.candidates import validate_catalyst_date
    valid, msg = validate_catalyst_date("")
    assert not valid
    assert "missing" in msg.lower() or "catalyst_date" in msg.lower()


def test_validate_past_date():
    from tradingagents.portfolio_advisor.candidates import validate_catalyst_date
    valid, msg = validate_catalyst_date(_past(5))
    assert not valid
    assert "past" in msg.lower()


def test_validate_future_date_within_window():
    from tradingagents.portfolio_advisor.candidates import validate_catalyst_date
    valid, msg = validate_catalyst_date(_future(30))
    assert valid, f"Expected valid but got: {msg}"
    assert msg == ""


def test_validate_today_is_valid():
    from tradingagents.portfolio_advisor.candidates import validate_catalyst_date
    valid, msg = validate_catalyst_date(date.today().isoformat())
    assert valid, f"Today should be valid: {msg}"


def test_validate_too_far_out():
    from tradingagents.portfolio_advisor.candidates import validate_catalyst_date
    far = (date.today() + timedelta(days=200)).isoformat()
    valid, msg = validate_catalyst_date(far, cfg={"portfolio_advisor_catalyst_max_days_out": 90})
    assert not valid
    assert "more than 90 days" in msg or "90 days" in msg


def test_validate_bad_format():
    from tradingagents.portfolio_advisor.candidates import validate_catalyst_date
    valid, msg = validate_catalyst_date("March 15 2026")
    assert not valid
    assert "not a valid" in msg.lower() or "iso" in msg.lower() or "yyyy-mm-dd" in msg.lower()


def test_validate_custom_max_days():
    from tradingagents.portfolio_advisor.candidates import validate_catalyst_date
    near = (date.today() + timedelta(days=10)).isoformat()
    # With a window of 5 days, 10 days out should fail.
    valid, msg = validate_catalyst_date(near, cfg={"portfolio_advisor_catalyst_max_days_out": 5})
    assert not valid
    assert "5 days" in msg or "more than" in msg


# ---------------------------------------------------------------------------
# 3. candidates.evaluate_candidate: catalyst-sleeve date gate
# ---------------------------------------------------------------------------


def test_catalyst_candidate_past_date_fails(tmp_path):
    from tradingagents.portfolio_advisor.candidates import evaluate_candidate
    rec = evaluate_candidate(
        {
            "ticker": "NVDA",
            "strategy": "catalyst",
            "reason": "Earnings beat with guidance raise",
            "catalyst": "earnings",
            "catalyst_date": _past(3),
            "liquidity_ok": True,
            "policy_ok": True,
        },
        cfg=_cfg(tmp_path),
    )
    assert rec.status == "rejected"
    assert "catalyst_date_invalid" in rec.gate_failures
    assert rec.gates.get("catalyst") == "fail"


def test_catalyst_candidate_future_date_passes(tmp_path):
    from tradingagents.portfolio_advisor.candidates import evaluate_candidate
    rec = evaluate_candidate(
        {
            "ticker": "NVDA",
            "strategy": "catalyst",
            "reason": "Earnings beat with guidance raise",
            "catalyst": "Q2 earnings",
            "catalyst_date": _future(20),
            "liquidity_ok": True,
            "policy_ok": True,
            "priority": 2,
        },
        cfg=_cfg(tmp_path),
    )
    # Should not fail on catalyst_date_invalid
    assert "catalyst_date_invalid" not in rec.gate_failures
    assert rec.gates.get("catalyst") == "pass"


def test_catalyst_candidate_no_catalyst_date_still_fails_if_no_catalyst_text(tmp_path):
    """A catalyst-sleeve candidate with no catalyst text AND no date must fail missing_catalyst."""
    from tradingagents.portfolio_advisor.candidates import evaluate_candidate
    rec = evaluate_candidate(
        {
            "ticker": "MSFT",
            "strategy": "catalyst",
            "reason": "AI buildout theme",
            # No 'catalyst' or 'catalyst_date' field
            "liquidity_ok": True,
            "policy_ok": True,
        },
        cfg=_cfg(tmp_path),
    )
    assert rec.status == "rejected"
    assert "missing_catalyst" in rec.gate_failures


def test_catalyst_candidate_with_catalyst_text_and_no_date_passes_gate(tmp_path):
    """catalyst_text alone (no explicit date) still passes the gate — date is optional."""
    from tradingagents.portfolio_advisor.candidates import evaluate_candidate
    rec = evaluate_candidate(
        {
            "ticker": "AAPL",
            "strategy": "catalyst",
            "reason": "WWDC hardware refresh with AI integration",
            "catalyst": "product launch",
            # No catalyst_date — validation only fires when the field is present
            "liquidity_ok": True,
            "policy_ok": True,
            "priority": 2,
        },
        cfg=_cfg(tmp_path),
    )
    assert "catalyst_date_invalid" not in rec.gate_failures
    assert rec.gates.get("catalyst") == "pass"


# ---------------------------------------------------------------------------
# 4. pm_tools.propose_trade: catalyst_date validation
# ---------------------------------------------------------------------------


def _build_propose_tool(cfg, captured, monkeypatch):
    """Patch proposals.add and position_plans; return the propose_trade tool."""
    from tradingagents.portfolio_advisor import pm_tools, proposals, position_plans

    def fake_add(c, **kw):
        captured.append(kw)

    monkeypatch.setattr(proposals, "add", fake_add)
    monkeypatch.setattr(position_plans, "append_position_decision", lambda *a, **k: None)
    tools = pm_tools.build_pm_tools(cfg, live_tickers=set())
    return next(t for t in tools if getattr(t, "name", "") == "propose_trade")


def test_propose_trade_rejects_past_catalyst_date(tmp_path, monkeypatch):
    # propose_trade now accepts up to 5 days in the past (allow_recent_past_days=5).
    # Use 6 days ago to confirm rejection beyond the allowance window.
    captured: list = []
    tool = _build_propose_tool(_cfg(tmp_path), captured, monkeypatch)
    out = tool.invoke({
        "ticker": "DKNG",
        "action": "buy",
        "sleeve": "catalyst",
        "catalyst_date": _past(6),  # 6 days ago → outside 5-day allowance → rejected
        "approx_usd": 500,
        "reason": "Earnings beat last week",
    })
    assert "error" in out.lower()
    assert "past" in out.lower()
    assert captured == []  # nothing filed


def test_propose_trade_rejects_bad_date_format(tmp_path, monkeypatch):
    captured: list = []
    tool = _build_propose_tool(_cfg(tmp_path), captured, monkeypatch)
    out = tool.invoke({
        "ticker": "DKNG",
        "action": "buy",
        "sleeve": "catalyst",
        "catalyst_date": "Q2 2026",  # not ISO
        "approx_usd": 500,
        "reason": "Earnings beat",
    })
    assert "error" in out.lower()
    assert captured == []


def test_propose_trade_accepts_valid_future_date(tmp_path, monkeypatch):
    captured: list = []
    tool = _build_propose_tool(_cfg(tmp_path), captured, monkeypatch)
    out = tool.invoke({
        "ticker": "DKNG",
        "action": "buy",
        "sleeve": "catalyst",
        "catalyst_date": _future(15),
        "approx_usd": 500,
        "reason": "Earnings beat on strong sports-betting quarter",
    })
    assert "error" not in out.lower()
    assert captured and captured[0]["ticker"] == "DKNG"


def test_propose_trade_rejects_too_far_out(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg["portfolio_advisor_catalyst_max_days_out"] = 30
    captured: list = []
    tool = _build_propose_tool(cfg, captured, monkeypatch)
    out = tool.invoke({
        "ticker": "DKNG",
        "action": "buy",
        "sleeve": "catalyst",
        "catalyst_date": _future(60),  # 60 days out but limit is 30
        "approx_usd": 500,
        "reason": "Earnings far in future",
    })
    assert "error" in out.lower()
    assert captured == []


# ---------------------------------------------------------------------------
# 5. pm_tools.emit_ep_candidate: date validation gating proposals
# ---------------------------------------------------------------------------


def _mock_portfolio_text():
    return "total_portfolio_value_usd=100000.0 available_balance=20000"


def _emit_tool(cfg):
    from tradingagents.portfolio_advisor import pm_tools
    tools = pm_tools.build_pm_tools(cfg, live_tickers=set())
    return next(t for t in tools if getattr(t, "name", "") == "emit_ep_candidate")


def test_emit_ep_candidate_invalid_date_stays_advisory(tmp_path, monkeypatch):
    # emit_ep_candidate now accepts up to 5 days in the past (allow_recent_past_days=5).
    # Use 6 days ago to confirm rejection beyond the allowance window.
    from tradingagents.portfolio_advisor import etoro_scan, messaging, proposals

    filed: list = []
    monkeypatch.setattr(etoro_scan, "fetch_portfolio_rows", lambda: (
        {}, _mock_portfolio_text(), [], []
    ))
    monkeypatch.setattr(etoro_scan, "account_mode", lambda: "alpaca")
    monkeypatch.setattr(messaging, "send_advisor_message", lambda *a, **k: None)
    monkeypatch.setattr(proposals, "add", lambda c, **kw: filed.append(kw))

    tool = _emit_tool(_cfg(tmp_path))
    result = tool.invoke({
        "ticker": "AAPL",
        "tier": "Tier 1",
        "catalyst": "WWDC hardware event",
        "entry_price": 200.0,
        "stop_price": 185.0,
        "catalyst_date": _past(6),  # 6 days ago → outside 5-day allowance → rejected
    })
    assert "advisory" in result.lower() or "invalid" in result.lower()
    assert filed == []  # no proposal filed


def test_emit_ep_candidate_valid_date_alpaca_mode_files_proposal(tmp_path, monkeypatch):
    from tradingagents.portfolio_advisor import etoro_scan, messaging, proposals

    filed: list = []
    monkeypatch.setattr(etoro_scan, "fetch_portfolio_rows", lambda: (
        {}, _mock_portfolio_text(), [], []
    ))
    monkeypatch.setattr(etoro_scan, "account_mode", lambda: "alpaca")
    monkeypatch.setattr(messaging, "send_advisor_message", lambda *a, **k: None)
    monkeypatch.setattr(proposals, "add", lambda c, **kw: filed.append(kw))

    tool = _emit_tool(_cfg(tmp_path))
    result = tool.invoke({
        "ticker": "AAPL",
        "tier": "Tier 1",
        "catalyst": "Q3 earnings beat",
        "entry_price": 200.0,
        "stop_price": 184.0,
        "catalyst_date": _future(10),
    })
    assert "proposal filed" in result.lower()
    assert len(filed) == 1
    assert filed[0]["ticker"] == "AAPL"
    assert filed[0]["sleeve"] == "catalyst"


def test_emit_ep_candidate_no_date_stays_advisory_even_in_alpaca(tmp_path, monkeypatch):
    from tradingagents.portfolio_advisor import etoro_scan, messaging, proposals

    filed: list = []
    monkeypatch.setattr(etoro_scan, "fetch_portfolio_rows", lambda: (
        {}, _mock_portfolio_text(), [], []
    ))
    monkeypatch.setattr(etoro_scan, "account_mode", lambda: "alpaca")
    monkeypatch.setattr(messaging, "send_advisor_message", lambda *a, **k: None)
    monkeypatch.setattr(proposals, "add", lambda c, **kw: filed.append(kw))

    tool = _emit_tool(_cfg(tmp_path))
    result = tool.invoke({
        "ticker": "AAPL",
        "tier": "Tier 2",
        "catalyst": "Analyst upgrade",
        "entry_price": 200.0,
        "stop_price": 184.0,
        # no catalyst_date — advisory only
    })
    # Should be advisory (no date means no proposal)
    assert "proposal filed" not in result.lower()
    assert filed == []
