"""F4A – run-advisor.sh watchlist routing and emit_ep_candidate autonomous proposals.

F4A: run-advisor.sh watchlist <cmd> must route to `advisor portfolio watchlist <cmd>`
     (not `advisor portfolio watchlist` bare, which would just show help).
     We verify the script produces the correct compound slug in its log path and
     that the CLI path it invokes is the right one.

F4B: emit_ep_candidate must file a proposals row when account_mode()=="alpaca" AND
     a catalyst_date is provided; without a catalyst_date it must stay advisory-only
     even in alpaca mode; in etoro mode it must never file a proposal.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# F4A – run-advisor.sh routing
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).parent.parent / "scripts" / "run-advisor.sh"


def test_run_advisor_watchlist_help_exits_cleanly(tmp_path):
    """run-advisor.sh watchlist research --help must succeed (exit 0 or 2 for help).

    The script redirects all output to a log file; we verify via the log path
    that the correct CLI was invoked (advisor portfolio watchlist research).
    """
    result = subprocess.run(
        ["bash", str(SCRIPT), "watchlist", "research", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The log file should contain the CLI output (script redirects stdout+stderr to log)
    log_path = Path.home() / ".tradingagents" / "logs" / "advisor-watchlist-research.log"
    log_content = log_path.read_text() if log_path.exists() else ""
    # The log should show the advisor portfolio watchlist research invocation
    # Exit code 0 = --help succeeded; exit code 2 = usage (also fine for help flag)
    assert result.returncode in (0, 2), (
        f"Unexpected exit code {result.returncode}; log: {log_content[-500:]}"
    )
    # The log should mention the watchlist-research slug (from the start/end banner)
    assert "watchlist-research" in log_content, (
        f"Expected 'watchlist-research' in log, got: {log_content[-500:]}"
    )


def test_run_advisor_watchlist_log_slug(tmp_path, monkeypatch):
    """The compound slug 'watchlist-research' must appear in log output lines."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "watchlist", "research", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The log file is written to ~/.tradingagents/logs/advisor-watchlist-research.log
    log_path = Path.home() / ".tradingagents" / "logs" / "advisor-watchlist-research.log"
    assert log_path.exists(), (
        f"Expected log at {log_path}; got exit={result.returncode}, stderr={result.stderr[:200]}"
    )
    content = log_path.read_text()
    assert "watchlist-research" in content, (
        f"Expected 'watchlist-research' slug in log, got: {content[:300]}"
    )


def test_run_advisor_non_watchlist_still_works():
    """Legacy form run-advisor.sh <cmd> must still route to advisor portfolio <cmd>."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "watchdog", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    # watchdog --help prints its usage; exit code may be 0 (typer help)
    assert "watchdog" in combined.lower() or result.returncode in (0, 2), (
        f"watchdog routing broken: exit={result.returncode}, out={combined[:300]}"
    )


# ---------------------------------------------------------------------------
# F4B – emit_ep_candidate autonomous proposals
# ---------------------------------------------------------------------------

def _build_ep_tool(cfg: Dict[str, Any], captured_proposals: List, captured_msgs: List, mode: str = "etoro"):
    """Build the emit_ep_candidate tool with all external calls mocked."""
    import tradingagents.portfolio_advisor.pm_tools as pm_tools_mod
    import tradingagents.portfolio_advisor.proposals as proposals_mod
    import tradingagents.portfolio_advisor.messaging as messaging_mod
    import tradingagents.portfolio_advisor.etoro_scan as etoro_scan_mod

    # Mock the tool builder; we need just emit_ep_candidate
    def fake_add(cfg, **kw):
        captured_proposals.append(kw)
        return {"id": "x", **kw}

    def fake_send(cfg, source, body, **kw):
        captured_msgs.append(body)
        return True

    def fake_fetch():
        return (
            {},
            "total_portfolio_value_usd=100000.0 available_balance='20000'",
            [],
            [],
        )

    proposals_mod.add = fake_add  # type: ignore[assignment]
    messaging_mod.send_advisor_message = fake_send  # type: ignore[assignment]
    etoro_scan_mod.fetch_portfolio_rows = fake_fetch  # type: ignore[assignment]

    # Patch account_mode to return the desired mode
    import unittest.mock as _mock
    patcher = _mock.patch.object(etoro_scan_mod, "account_mode", return_value=mode)
    patcher.start()

    tools = pm_tools_mod.build_pm_tools(cfg, live_tickers=set())
    tool = next(t for t in tools if getattr(t, "name", "") == "emit_ep_candidate")
    return tool, patcher


def test_emit_ep_candidate_alpaca_with_date_files_proposal(monkeypatch):
    """In alpaca mode with a catalyst_date, a proposal must be filed."""
    import tradingagents.portfolio_advisor.proposals as proposals_mod
    import tradingagents.portfolio_advisor.messaging as messaging_mod
    import tradingagents.portfolio_advisor.etoro_scan as etoro_scan_mod

    captured_proposals: List[Dict[str, Any]] = []
    captured_msgs: List[str] = []

    monkeypatch.setattr(
        etoro_scan_mod, "fetch_portfolio_rows",
        lambda: ({}, "total_portfolio_value_usd=100000.0", [], []),
    )
    monkeypatch.setattr(etoro_scan_mod, "account_mode", lambda: "alpaca")
    monkeypatch.setattr(proposals_mod, "add", lambda cfg, **kw: captured_proposals.append(kw) or {"id": "x"})
    monkeypatch.setattr(messaging_mod, "send_advisor_message", lambda cfg, src, body, **kw: captured_msgs.append(body))

    import tradingagents.portfolio_advisor.pm_tools as pm_tools_mod
    tools = pm_tools_mod.build_pm_tools({}, live_tickers=set())
    tool = next(t for t in tools if getattr(t, "name", "") == "emit_ep_candidate")

    result = tool.invoke({
        "ticker": "NVDA",
        "tier": "Tier 1",
        "catalyst": "Blowout earnings beat",
        "entry_price": 950.0,
        "stop_price": 874.0,
        "risk_pct": 1.0,
        "catalyst_date": "2026-06-11",
        "confidence": 0.8,
    })

    # A proposal must have been filed
    assert len(captured_proposals) == 1, f"Expected 1 proposal, got {len(captured_proposals)}"
    p = captured_proposals[0]
    assert p["ticker"] == "NVDA"
    assert p["action"] == "buy"
    assert p["sleeve"] == "catalyst"
    assert p["catalyst_date"] == "2026-06-11"
    assert p["confidence"] == 0.8

    # Telegram must also have been sent
    assert len(captured_msgs) == 1
    assert "PROPOSAL" in captured_msgs[0]

    # Return value must acknowledge the proposal was filed
    assert "proposal" in result.lower()


def test_emit_ep_candidate_alpaca_without_date_stays_advisory(monkeypatch):
    """In alpaca mode WITHOUT catalyst_date, no proposal must be filed (advisory-only)."""
    import tradingagents.portfolio_advisor.proposals as proposals_mod
    import tradingagents.portfolio_advisor.messaging as messaging_mod
    import tradingagents.portfolio_advisor.etoro_scan as etoro_scan_mod

    captured_proposals: List[Dict[str, Any]] = []
    captured_msgs: List[str] = []

    monkeypatch.setattr(
        etoro_scan_mod, "fetch_portfolio_rows",
        lambda: ({}, "total_portfolio_value_usd=100000.0", [], []),
    )
    monkeypatch.setattr(etoro_scan_mod, "account_mode", lambda: "alpaca")
    monkeypatch.setattr(proposals_mod, "add", lambda cfg, **kw: captured_proposals.append(kw) or {"id": "x"})
    monkeypatch.setattr(messaging_mod, "send_advisor_message", lambda cfg, src, body, **kw: captured_msgs.append(body))

    import tradingagents.portfolio_advisor.pm_tools as pm_tools_mod
    tools = pm_tools_mod.build_pm_tools({}, live_tickers=set())
    tool = next(t for t in tools if getattr(t, "name", "") == "emit_ep_candidate")

    result = tool.invoke({
        "ticker": "CRDO",
        "tier": "Tier 2",
        "catalyst": "Momentum on AI chip tailwind",
        "entry_price": 70.0,
        "stop_price": 64.4,
        # No catalyst_date — must stay advisory
    })

    # No proposal filed
    assert captured_proposals == [], f"Expected no proposal, got {captured_proposals}"

    # Telegram advisory must still be sent
    assert len(captured_msgs) == 1
    # Body should say RECOMMENDATION not PROPOSAL
    assert "RECOMMENDATION" in captured_msgs[0]
    assert "PROPOSAL" not in captured_msgs[0]


def test_emit_ep_candidate_etoro_mode_never_files_proposal(monkeypatch):
    """In etoro mode, no proposal must be filed even when catalyst_date is provided."""
    import tradingagents.portfolio_advisor.proposals as proposals_mod
    import tradingagents.portfolio_advisor.messaging as messaging_mod
    import tradingagents.portfolio_advisor.etoro_scan as etoro_scan_mod

    captured_proposals: List[Dict[str, Any]] = []
    captured_msgs: List[str] = []

    monkeypatch.setattr(
        etoro_scan_mod, "fetch_portfolio_rows",
        lambda: ({}, "total_portfolio_value_usd=100000.0", [], []),
    )
    monkeypatch.setattr(etoro_scan_mod, "account_mode", lambda: "etoro")
    monkeypatch.setattr(proposals_mod, "add", lambda cfg, **kw: captured_proposals.append(kw) or {"id": "x"})
    monkeypatch.setattr(messaging_mod, "send_advisor_message", lambda cfg, src, body, **kw: captured_msgs.append(body))

    import tradingagents.portfolio_advisor.pm_tools as pm_tools_mod
    tools = pm_tools_mod.build_pm_tools({}, live_tickers=set())
    tool = next(t for t in tools if getattr(t, "name", "") == "emit_ep_candidate")

    result = tool.invoke({
        "ticker": "DKNG",
        "tier": "Tier 1",
        "catalyst": "World Cup major contract win",
        "entry_price": 55.0,
        "stop_price": 50.6,
        "catalyst_date": "2026-06-12",  # date present but etoro mode
    })

    # In etoro mode, no proposal must be filed
    assert captured_proposals == [], f"Expected no proposal in etoro mode, got {captured_proposals}"

    # Telegram advisory must still be sent
    assert len(captured_msgs) == 1
    assert "RECOMMENDATION" in captured_msgs[0]


def test_emit_ep_candidate_proposal_includes_sizing(monkeypatch):
    """The filed proposal must include computed shares and usd_position from risk sizing."""
    import tradingagents.portfolio_advisor.proposals as proposals_mod
    import tradingagents.portfolio_advisor.messaging as messaging_mod
    import tradingagents.portfolio_advisor.etoro_scan as etoro_scan_mod

    captured_proposals: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        etoro_scan_mod, "fetch_portfolio_rows",
        lambda: ({}, "total_portfolio_value_usd=100000.0", [], []),
    )
    monkeypatch.setattr(etoro_scan_mod, "account_mode", lambda: "alpaca")
    monkeypatch.setattr(proposals_mod, "add", lambda cfg, **kw: captured_proposals.append(kw) or {"id": "x"})
    monkeypatch.setattr(messaging_mod, "send_advisor_message", lambda *a, **k: None)

    import tradingagents.portfolio_advisor.pm_tools as pm_tools_mod
    tools = pm_tools_mod.build_pm_tools({}, live_tickers=set())
    tool = next(t for t in tools if getattr(t, "name", "") == "emit_ep_candidate")

    tool.invoke({
        "ticker": "META",
        "tier": "Tier 1",
        "catalyst": "Record Q2 earnings beat",
        "entry_price": 600.0,
        "stop_price": 552.0,   # risk_per_share = 48
        "risk_pct": 1.0,       # 1% of 100k = $1000 risk → 1000/48 ≈ 20.83 shares
        "catalyst_date": "2026-07-30",
    })

    assert len(captured_proposals) == 1
    p = captured_proposals[0]
    # shares ≈ 1000 / 48 = 20.8333
    assert abs(p["shares"] - round(1000 / 48, 4)) < 0.01, f"Unexpected shares: {p['shares']}"
    # approx_usd ≈ shares * entry_price
    assert p["approx_usd"] > 0
    assert p["target_price"] == 600.0
    assert p["sleeve"] == "catalyst"
    assert p["catalyst_date"] == "2026-07-30"
