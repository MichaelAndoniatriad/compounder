"""Tests for T3 — Re-underwrite flow + hard book-loss cap + counterfactual ledger.

Covers:
- trigger sets plan fields + queues re-underwrite job (idempotent across ticks)
- deadline expiry closes position (rule paper_core_reunderwrite_expired)
- reconfirmed verdict prevents close + cooldown suppresses re-trigger
- broken verdict closes immediately (rule paper_core_thesis_broken)
- book-loss cap closes regardless of re-underwrite state (5% equity math)
  - 3% position at -40% → -1.2% book → below cap → re-underwrite (not closed)
  - 14% position at -40% → -5.6% book → above cap → closes immediately
- counterfactual rows written by close_for_watchdog
- score_counterfactuals scores aged rows once (mocked yfinance), skips young rows
- PM prompt surfaces pending re-underwrites via build_trigger_block
- record_reunderwrite_verdict PM tool: reconfirmed clears fields, broken triggers close
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **overrides) -> Dict[str, Any]:
    """Minimal config — no live APIs, sandbox dir."""
    cfg: Dict[str, Any] = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_alpaca_paper": True,
        "portfolio_advisor_alpaca_max_position_pct": 0.10,
        "portfolio_advisor_add_cooldown_days": 0,
        "portfolio_advisor_reentry_cooldown_days": 0,
        "portfolio_advisor_max_spread_bps": 100,
        "portfolio_advisor_limit_slip_bps": 10,
        "portfolio_advisor_catalyst_hard_stop_pct": 0.08,
        "portfolio_advisor_catalyst_risk_pct": 0.01,
        "portfolio_advisor_pm_veto_minutes": 45,
        "portfolio_advisor_regime_enabled": False,
        "portfolio_advisor_alpaca_high_conviction_enabled": True,
        "portfolio_advisor_alpaca_high_conviction_pct": 0.15,
        "portfolio_advisor_alpaca_high_conviction_min_confidence": 0.85,
        "portfolio_advisor_alpaca_high_conviction_max_positions": 2,
        "portfolio_advisor_reunderwrite_days": 5,
        "portfolio_advisor_reunderwrite_cooldown_days": 30,
        "portfolio_advisor_max_position_book_loss_pct": 0.05,
    }
    cfg.update(overrides)
    return cfg


def _make_position(
    symbol: str = "NVDA",
    plpc: float = -0.45,
    unrealized_pl: float = -1200.0,
    market_value: float = 3000.0,
    qty: float = 10.0,
) -> MagicMock:
    pos = MagicMock()
    pos.symbol = symbol
    pos.unrealized_plpc = str(plpc)
    pos.unrealized_pl = str(unrealized_pl)
    pos.market_value = str(market_value)
    pos.qty = str(qty)
    pos.current_price = None
    pos.avg_entry_price = str(abs(market_value) / qty) if qty else None
    return pos


def _make_client(
    positions=None,
    equity: float = 100_000.0,
    open_position=None,
) -> MagicMock:
    client = MagicMock()
    acct = MagicMock()
    acct.equity = str(equity)
    client.get_account.return_value = acct
    client.get_all_positions.return_value = positions or []
    order = MagicMock()
    order.id = "mock-order-id"
    client.submit_order.return_value = order
    if open_position is not None:
        client.get_open_position.return_value = open_position
    else:
        client.get_open_position.side_effect = Exception("no position")
    return client


def _make_core_plan(
    ticker: str = "NVDA",
    entry_price: float = 100.0,
    **kwargs,
) -> "PositionPlan":
    from tradingagents.portfolio_advisor.position_plans import PositionPlan
    return PositionPlan(
        ticker=ticker,
        entry_price=entry_price,
        strategy="core",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Trigger sets fields + queues job (idempotent)
# ---------------------------------------------------------------------------


def test_reunderwrite_trigger_sets_fields_and_queues_job(tmp_path):
    """First tick at -45%: sets plan fields and queues a full_graph job."""
    from tradingagents.portfolio_advisor.position_plans import (
        load_position_plans, upsert_position_plan,
    )
    from tradingagents.portfolio_advisor import state as pa_state

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    # Set up a core plan with entry_price $100; position is now at $55 (-45%).
    plan = _make_core_plan("NVDA", entry_price=100.0)
    upsert_position_plan(cfg, plan)

    pos = _make_position("NVDA", plpc=-0.45, unrealized_pl=-1200.0)
    client = _make_client(positions=[pos], equity=100_000.0)
    # close_position should NOT be called on first trigger tick.
    client.close_position.return_value = MagicMock(id="mock-close")
    client.get_open_position.side_effect = lambda tk: pos if tk == "NVDA" else (_ for _ in ()).throw(Exception("no"))

    with (
        patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True),
        patch("tradingagents.integrations.alpaca.executor._client", return_value=client),
        patch("tradingagents.integrations.alpaca.executor.reconcile_fills"),
        patch("tradingagents.integrations.alpaca.executor.execute_deferred_entries"),
        patch("tradingagents.integrations.alpaca.executor.execute_unvetoed_candidates"),
        patch("tradingagents.integrations.alpaca.executor._latest_buys_from_ledger", return_value={}),
    ):
        from tradingagents.integrations.alpaca.executor import enforce_paper_exits
        result = enforce_paper_exits(cfg)

    # No position should have been closed on the first trigger.
    assert result == 0
    client.close_position.assert_not_called()

    # Plan must now have trigger fields set.
    plans = load_position_plans(cfg)
    nvda = plans.get("NVDA")
    assert nvda is not None
    assert nvda.reunderwrite_triggered_at != ""
    assert nvda.reunderwrite_deadline != ""
    assert nvda.reunderwrite_verdict == ""

    # Deadline must be ~5 days from now.
    dl = datetime.fromisoformat(nvda.reunderwrite_deadline.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert 4 <= (dl - now).days <= 6

    # A full_graph job must have been queued.
    st = pa_state.load_state(cfg)
    jobs = [j for j in (st.get("jobs") or []) if j.get("source") == "reunderwrite_dd40"]
    assert len(jobs) == 1, f"Expected 1 reunderwrite_dd40 job, got {len(jobs)}"
    assert jobs[0]["status"] == "pending"
    assert jobs[0]["ticker"] == "NVDA"
    assert jobs[0]["execution_tier"] == "full_graph"


def test_reunderwrite_trigger_idempotent(tmp_path):
    """Second tick at -45% does NOT queue a second job or change the deadline."""
    from tradingagents.portfolio_advisor.position_plans import (
        load_position_plans, upsert_position_plan,
    )
    from tradingagents.portfolio_advisor import state as pa_state

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    plan = _make_core_plan("NVDA", entry_price=100.0)
    upsert_position_plan(cfg, plan)

    pos = _make_position("NVDA", plpc=-0.45, unrealized_pl=-1200.0)
    client = _make_client(positions=[pos], equity=100_000.0)
    client.get_open_position.side_effect = lambda tk: pos if tk == "NVDA" else (_ for _ in ()).throw(Exception("no"))

    def _run_tick():
        with (
            patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True),
            patch("tradingagents.integrations.alpaca.executor._client", return_value=client),
            patch("tradingagents.integrations.alpaca.executor.reconcile_fills"),
            patch("tradingagents.integrations.alpaca.executor.execute_deferred_entries"),
            patch("tradingagents.integrations.alpaca.executor.execute_unvetoed_candidates"),
            patch("tradingagents.integrations.alpaca.executor._latest_buys_from_ledger", return_value={}),
        ):
            from tradingagents.integrations.alpaca.executor import enforce_paper_exits
            return enforce_paper_exits(cfg)

    _run_tick()
    plans_after_1 = load_position_plans(cfg)
    deadline_1 = plans_after_1["NVDA"].reunderwrite_deadline

    _run_tick()
    plans_after_2 = load_position_plans(cfg)
    deadline_2 = plans_after_2["NVDA"].reunderwrite_deadline

    assert deadline_1 == deadline_2, "Deadline changed on second tick — not idempotent"

    st = pa_state.load_state(cfg)
    jobs = [j for j in (st.get("jobs") or []) if j.get("source") == "reunderwrite_dd40"]
    assert len(jobs) == 1, f"Expected exactly 1 job, got {len(jobs)}"


# ---------------------------------------------------------------------------
# 2. Deadline expiry closes position
# ---------------------------------------------------------------------------


def test_deadline_expiry_closes_position(tmp_path):
    """Tick after deadline with no reconfirmed verdict → close (reunderwrite_expired)."""
    from tradingagents.portfolio_advisor.position_plans import upsert_position_plan

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    expired_deadline = (now - timedelta(days=1)).isoformat()

    plan = _make_core_plan(
        "NVDA",
        entry_price=100.0,
        reunderwrite_triggered_at=(now - timedelta(days=6)).isoformat(),
        reunderwrite_deadline=expired_deadline,
        reunderwrite_verdict="",
    )
    upsert_position_plan(cfg, plan)

    pos = _make_position("NVDA", plpc=-0.45, unrealized_pl=-1200.0)
    client = _make_client(positions=[pos], equity=100_000.0, open_position=pos)

    with (
        patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True),
        patch("tradingagents.integrations.alpaca.executor._client", return_value=client),
        patch("tradingagents.integrations.alpaca.executor.reconcile_fills"),
        patch("tradingagents.integrations.alpaca.executor.execute_deferred_entries"),
        patch("tradingagents.integrations.alpaca.executor.execute_unvetoed_candidates"),
        patch("tradingagents.integrations.alpaca.executor._latest_buys_from_ledger", return_value={}),
    ):
        from tradingagents.integrations.alpaca.executor import enforce_paper_exits
        result = enforce_paper_exits(cfg)

    assert result == 1
    client.close_position.assert_called_once_with("NVDA")

    # Check ledger for the rule.
    ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
    rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    watchdog_rows = [r for r in rows if r.get("action") == "watchdog_exit"]
    assert any("reunderwrite_expired" in r.get("rule", "") for r in watchdog_rows)


# ---------------------------------------------------------------------------
# 3. Reconfirmed verdict: no close + cooldown suppresses re-trigger
# ---------------------------------------------------------------------------


def test_reconfirmed_verdict_prevents_close_and_sets_cooldown(tmp_path):
    """After reconfirmed verdict, plan is cleared and position stays open even if ≤-40%."""
    from tradingagents.portfolio_advisor.position_plans import (
        load_position_plans, upsert_position_plan,
    )

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    plan = _make_core_plan(
        "NVDA",
        entry_price=100.0,
        reunderwrite_triggered_at=(now - timedelta(days=2)).isoformat(),
        reunderwrite_deadline=(now + timedelta(days=3)).isoformat(),
        reunderwrite_verdict="",
    )
    upsert_position_plan(cfg, plan)

    # Simulate PM calling record_reunderwrite_verdict with "reconfirmed".
    from tradingagents.portfolio_advisor.pm_tools import build_pm_tools
    tools = {t.name: t for t in build_pm_tools(cfg, {"NVDA"})}
    verdict_tool = tools.get("record_reunderwrite_verdict")
    assert verdict_tool is not None

    result_msg = verdict_tool.invoke({
        "ticker": "NVDA",
        "verdict": "reconfirmed",
        "reason": "AI infrastructure thesis intact; -42% drawdown is temporary sentiment.",
    })
    assert "RECONFIRMED" in result_msg.upper(), f"Unexpected: {result_msg}"

    # Verify plan cleared.
    plans = load_position_plans(cfg)
    nvda = plans["NVDA"]
    assert nvda.reunderwrite_triggered_at == ""
    assert nvda.reunderwrite_deadline == ""
    assert nvda.reunderwrite_verdict == ""
    assert nvda.reunderwrite_last_cleared_at != ""

    # Now run enforce_paper_exits — position at -45% but in cooldown → should NOT close.
    pos = _make_position("NVDA", plpc=-0.45, unrealized_pl=-1200.0)
    client = _make_client(positions=[pos], equity=100_000.0, open_position=pos)

    with (
        patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True),
        patch("tradingagents.integrations.alpaca.executor._client", return_value=client),
        patch("tradingagents.integrations.alpaca.executor.reconcile_fills"),
        patch("tradingagents.integrations.alpaca.executor.execute_deferred_entries"),
        patch("tradingagents.integrations.alpaca.executor.execute_unvetoed_candidates"),
        patch("tradingagents.integrations.alpaca.executor._latest_buys_from_ledger", return_value={}),
    ):
        from tradingagents.integrations.alpaca.executor import enforce_paper_exits
        closed = enforce_paper_exits(cfg)

    # Position should stay open (in cooldown, no new trigger).
    assert closed == 0
    client.close_position.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Broken verdict closes immediately
# ---------------------------------------------------------------------------


def test_broken_verdict_closes_immediately(tmp_path):
    """When verdict='broken', position closes on next executor tick."""
    from tradingagents.portfolio_advisor.position_plans import (
        load_position_plans, upsert_position_plan,
    )

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    plan = _make_core_plan(
        "NVDA",
        entry_price=100.0,
        reunderwrite_triggered_at=(now - timedelta(days=2)).isoformat(),
        reunderwrite_deadline=(now + timedelta(days=3)).isoformat(),
        reunderwrite_verdict="",
    )
    upsert_position_plan(cfg, plan)

    # PM calls broken verdict.
    from tradingagents.portfolio_advisor.pm_tools import build_pm_tools
    tools = {t.name: t for t in build_pm_tools(cfg, {"NVDA"})}
    verdict_tool = tools["record_reunderwrite_verdict"]

    msg = verdict_tool.invoke({
        "ticker": "NVDA",
        "verdict": "broken",
        "reason": "Secular decline; revenue guidance cut 40%, losing market share.",
    })
    assert "THESIS BROKEN" in msg.upper(), f"Unexpected: {msg}"

    # Verify plan has broken verdict.
    plans = load_position_plans(cfg)
    assert plans["NVDA"].reunderwrite_verdict == "broken"

    # Executor tick should close immediately.
    pos = _make_position("NVDA", plpc=-0.45, unrealized_pl=-1200.0)
    client = _make_client(positions=[pos], equity=100_000.0, open_position=pos)

    with (
        patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True),
        patch("tradingagents.integrations.alpaca.executor._client", return_value=client),
        patch("tradingagents.integrations.alpaca.executor.reconcile_fills"),
        patch("tradingagents.integrations.alpaca.executor.execute_deferred_entries"),
        patch("tradingagents.integrations.alpaca.executor.execute_unvetoed_candidates"),
        patch("tradingagents.integrations.alpaca.executor._latest_buys_from_ledger", return_value={}),
    ):
        from tradingagents.integrations.alpaca.executor import enforce_paper_exits
        closed = enforce_paper_exits(cfg)

    assert closed == 1
    client.close_position.assert_called_once_with("NVDA")

    ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
    rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    assert any("thesis_broken" in r.get("rule", "") for r in rows if r.get("action") == "watchdog_exit")


# ---------------------------------------------------------------------------
# 5. Book-loss cap math — 3% vs 14% position examples
# ---------------------------------------------------------------------------


def test_book_loss_cap_small_position_no_close(tmp_path):
    """3% position at -40%: unrealized loss = -1.2% of equity → below 5% cap → re-underwrite, not close."""
    from tradingagents.portfolio_advisor.position_plans import (
        load_position_plans, upsert_position_plan,
    )

    cfg = _cfg(tmp_path, portfolio_advisor_max_position_book_loss_pct=0.05)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    # Equity = $100k. 3% position = $3k MV. -40% → unrealized_pl = -$2k. Book loss = 2% < 5%.
    plan = _make_core_plan("AAPL", entry_price=100.0)
    upsert_position_plan(cfg, plan)

    pos = _make_position(
        "AAPL",
        plpc=-0.40,
        unrealized_pl=-2000.0,   # -2% of $100k equity → below cap
        market_value=3000.0,
        qty=30.0,
    )
    client = _make_client(positions=[pos], equity=100_000.0, open_position=pos)

    with (
        patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True),
        patch("tradingagents.integrations.alpaca.executor._client", return_value=client),
        patch("tradingagents.integrations.alpaca.executor.reconcile_fills"),
        patch("tradingagents.integrations.alpaca.executor.execute_deferred_entries"),
        patch("tradingagents.integrations.alpaca.executor.execute_unvetoed_candidates"),
        patch("tradingagents.integrations.alpaca.executor._latest_buys_from_ledger", return_value={}),
    ):
        from tradingagents.integrations.alpaca.executor import enforce_paper_exits
        closed = enforce_paper_exits(cfg)

    # Should NOT close; re-underwrite triggered.
    assert closed == 0
    client.close_position.assert_not_called()

    plans = load_position_plans(cfg)
    assert plans["AAPL"].reunderwrite_triggered_at != ""


def test_book_loss_cap_large_position_closes_immediately(tmp_path):
    """14% position at -40%: unrealized loss = -5.6% of equity → above 5% cap → close now."""
    from tradingagents.portfolio_advisor.position_plans import (
        load_position_plans, upsert_position_plan,
    )

    cfg = _cfg(tmp_path, portfolio_advisor_max_position_book_loss_pct=0.05)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    # Equity = $100k. 14% position = $14k MV. -40% → unrealized_pl = -$5600. Book loss = 5.6% > 5%.
    plan = _make_core_plan("MSFT", entry_price=100.0)
    upsert_position_plan(cfg, plan)

    pos = _make_position(
        "MSFT",
        plpc=-0.40,
        unrealized_pl=-5600.0,   # -5.6% of $100k equity → above 5% cap
        market_value=14000.0,
        qty=140.0,
    )
    client = _make_client(positions=[pos], equity=100_000.0, open_position=pos)

    with (
        patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True),
        patch("tradingagents.integrations.alpaca.executor._client", return_value=client),
        patch("tradingagents.integrations.alpaca.executor.reconcile_fills"),
        patch("tradingagents.integrations.alpaca.executor.execute_deferred_entries"),
        patch("tradingagents.integrations.alpaca.executor.execute_unvetoed_candidates"),
        patch("tradingagents.integrations.alpaca.executor._latest_buys_from_ledger", return_value={}),
    ):
        from tradingagents.integrations.alpaca.executor import enforce_paper_exits
        closed = enforce_paper_exits(cfg)

    assert closed == 1
    client.close_position.assert_called_once_with("MSFT")

    ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
    rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    assert any("book_loss_cap" in r.get("rule", "") for r in rows if r.get("action") == "watchdog_exit")


def test_book_loss_cap_overrides_reunderwrite(tmp_path):
    """Book-loss cap fires even when a re-underwrite is already pending."""
    from tradingagents.portfolio_advisor.position_plans import upsert_position_plan

    cfg = _cfg(tmp_path, portfolio_advisor_max_position_book_loss_pct=0.05)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    # Plan has a pending re-underwrite.
    plan = _make_core_plan(
        "GOOG",
        entry_price=100.0,
        reunderwrite_triggered_at=(now - timedelta(days=2)).isoformat(),
        reunderwrite_deadline=(now + timedelta(days=3)).isoformat(),
        reunderwrite_verdict="",
    )
    upsert_position_plan(cfg, plan)

    # Position loss exceeds book-loss cap.
    pos = _make_position(
        "GOOG",
        plpc=-0.55,
        unrealized_pl=-6000.0,   # -6% > 5% cap
        market_value=14000.0,
        qty=140.0,
    )
    client = _make_client(positions=[pos], equity=100_000.0, open_position=pos)

    with (
        patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True),
        patch("tradingagents.integrations.alpaca.executor._client", return_value=client),
        patch("tradingagents.integrations.alpaca.executor.reconcile_fills"),
        patch("tradingagents.integrations.alpaca.executor.execute_deferred_entries"),
        patch("tradingagents.integrations.alpaca.executor.execute_unvetoed_candidates"),
        patch("tradingagents.integrations.alpaca.executor._latest_buys_from_ledger", return_value={}),
    ):
        from tradingagents.integrations.alpaca.executor import enforce_paper_exits
        closed = enforce_paper_exits(cfg)

    assert closed == 1
    client.close_position.assert_called_once_with("GOOG")


# ---------------------------------------------------------------------------
# 6. Counterfactual rows written on rule closes
# ---------------------------------------------------------------------------


def test_counterfactual_row_written_on_close(tmp_path):
    """close_for_watchdog appends a row to counterfactual_ledger.jsonl."""
    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    pos = _make_position("NVDA", plpc=-0.45, market_value=3000.0, qty=10.0)
    client = _make_client(open_position=pos)

    from tradingagents.integrations.alpaca import executor as _exec
    with (
        patch.object(_exec, "enabled", return_value=True),
        patch.object(_exec, "_client", return_value=client),
    ):
        result = _exec.close_for_watchdog(cfg, "NVDA", 1.0, "paper_core_reunderwrite_expired")

    assert result is not None

    ledger = Path(cfg["portfolio_advisor_dir"]) / "counterfactual_ledger.jsonl"
    assert ledger.is_file()
    rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "NVDA"
    assert r["rule"] == "paper_core_reunderwrite_expired"
    assert r["exit_price"] is not None


def test_counterfactual_row_written_for_book_loss_cap(tmp_path):
    """close_for_watchdog appends a row for paper_book_loss_cap rule."""
    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    pos = _make_position("MSFT", plpc=-0.45, market_value=14000.0, qty=140.0)
    client = _make_client(open_position=pos)

    from tradingagents.integrations.alpaca import executor as _exec
    with (
        patch.object(_exec, "enabled", return_value=True),
        patch.object(_exec, "_client", return_value=client),
    ):
        _exec.close_for_watchdog(cfg, "MSFT", 1.0, "paper_book_loss_cap")

    ledger = Path(cfg["portfolio_advisor_dir"]) / "counterfactual_ledger.jsonl"
    rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    assert any(r["rule"] == "paper_book_loss_cap" for r in rows)


# ---------------------------------------------------------------------------
# 7. score_counterfactuals: scores aged rows, skips young rows
# ---------------------------------------------------------------------------


def _mock_price_fetcher(ticker, start_date, end_date):
    """Return fake prices: exit+10% for all tickers, flat for SPY."""
    prices = {
        "NVDA": (100.0, 110.0),
        "SPY":  (450.0, 450.0),
    }
    return prices.get(ticker, (100.0, 105.0))


def test_score_counterfactuals_scores_aged_rows(tmp_path):
    """Rows ≥30d old get a 30d score; rows <30d are skipped."""
    from tradingagents.portfolio_advisor.outcome_tracker import score_counterfactuals
    from tradingagents.portfolio_advisor import state as pa_state

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    ledger = pa_dir / "counterfactual_ledger.jsonl"
    now = datetime.now(timezone.utc)

    # Two rows: one 40d old (should score), one 5d old (too young).
    old_ts = (now - timedelta(days=40)).isoformat()
    young_ts = (now - timedelta(days=5)).isoformat()

    rows = [
        {"ts": old_ts, "ticker": "NVDA", "rule": "paper_core_reunderwrite_expired", "exit_price": 55.0},
        {"ts": young_ts, "ticker": "NVDA", "rule": "paper_core_reunderwrite_expired", "exit_price": 55.0},
    ]
    with ledger.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    summary = score_counterfactuals(cfg, price_fetcher=_mock_price_fetcher)

    assert summary["ledger_rows"] == 2
    assert summary["scored_30d"] == 1
    assert summary["scored_180d"] == 0
    assert summary["skipped_too_young"] >= 1

    scores_path = pa_dir / "counterfactual_scores.jsonl"
    assert scores_path.is_file()
    scored_rows = [json.loads(l) for l in scores_path.read_text().splitlines() if l.strip()]
    assert len(scored_rows) == 1
    sr = scored_rows[0]
    assert sr["ticker"] == "NVDA"
    assert sr["horizon_days"] == 30
    assert sr["fwd_return"] == pytest.approx(0.1, abs=0.001)  # 110/100 - 1 = +10%


def test_score_counterfactuals_idempotent(tmp_path):
    """Running score_counterfactuals twice does not double-score rows."""
    from tradingagents.portfolio_advisor.outcome_tracker import score_counterfactuals

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    ledger = pa_dir / "counterfactual_ledger.jsonl"
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=35)).isoformat()

    with ledger.open("w") as f:
        f.write(json.dumps({"ts": old_ts, "ticker": "NVDA", "rule": "paper_book_loss_cap", "exit_price": 55.0}) + "\n")

    score_counterfactuals(cfg, price_fetcher=_mock_price_fetcher)
    score_counterfactuals(cfg, price_fetcher=_mock_price_fetcher)

    scores_path = pa_dir / "counterfactual_scores.jsonl"
    rows = [json.loads(l) for l in scores_path.read_text().splitlines() if l.strip()]
    # Only 1 row despite 2 runs (30d only since not 180d old).
    assert len(rows) == 1


def test_score_counterfactuals_scores_180d(tmp_path):
    """Rows ≥180d old get both 30d and 180d scores."""
    from tradingagents.portfolio_advisor.outcome_tracker import score_counterfactuals

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    ledger = pa_dir / "counterfactual_ledger.jsonl"
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=190)).isoformat()

    with ledger.open("w") as f:
        f.write(json.dumps({"ts": old_ts, "ticker": "NVDA", "rule": "paper_core_reunderwrite_expired", "exit_price": 55.0}) + "\n")

    summary = score_counterfactuals(cfg, price_fetcher=_mock_price_fetcher)

    assert summary["scored_30d"] == 1
    assert summary["scored_180d"] == 1

    scores_path = pa_dir / "counterfactual_scores.jsonl"
    rows = [json.loads(l) for l in scores_path.read_text().splitlines() if l.strip()]
    horizons = {r["horizon_days"] for r in rows}
    assert horizons == {30, 180}


# ---------------------------------------------------------------------------
# 8. PM prompt surfaces pending re-underwrites
# ---------------------------------------------------------------------------


def test_build_trigger_block_surfaces_pending_reunderwrites(tmp_path):
    """build_trigger_block includes PENDING RE-UNDERWRITES section when one is active."""
    from tradingagents.portfolio_advisor.position_plans import (
        build_trigger_block, upsert_position_plan,
    )

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    plan = _make_core_plan(
        "NVDA",
        entry_price=100.0,
        reunderwrite_triggered_at=(now - timedelta(days=1)).isoformat(),
        reunderwrite_deadline=(now + timedelta(days=4)).isoformat(),
        reunderwrite_verdict="",
    )
    upsert_position_plan(cfg, plan)

    # Patch price fetch so build_trigger_block doesn't call yfinance.
    with patch(
        "tradingagents.portfolio_advisor.position_plans._fetch_prices",
        return_value={"NVDA": 60.0},
    ):
        block = build_trigger_block(cfg, {"NVDA"})

    assert "PENDING RE-UNDERWRITES" in block
    assert "NVDA" in block
    assert "record_reunderwrite_verdict" in block


def test_build_trigger_block_no_section_when_no_pending(tmp_path):
    """build_trigger_block has no PENDING RE-UNDERWRITES section when none are active."""
    from tradingagents.portfolio_advisor.position_plans import (
        build_trigger_block, upsert_position_plan,
    )

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    plan = _make_core_plan("NVDA", entry_price=100.0)
    upsert_position_plan(cfg, plan)

    with patch(
        "tradingagents.portfolio_advisor.position_plans._fetch_prices",
        return_value={"NVDA": 110.0},
    ):
        block = build_trigger_block(cfg, {"NVDA"})

    assert "PENDING RE-UNDERWRITES" not in block


# ---------------------------------------------------------------------------
# 9. record_reunderwrite_verdict: guards and error cases
# ---------------------------------------------------------------------------


def test_record_reunderwrite_verdict_no_pending_is_noop(tmp_path):
    """Calling tool when no re-underwrite is pending returns an error."""
    from tradingagents.portfolio_advisor.position_plans import upsert_position_plan
    from tradingagents.portfolio_advisor.pm_tools import build_pm_tools

    cfg = _cfg(tmp_path)
    pa_dir = Path(cfg["portfolio_advisor_dir"])
    pa_dir.mkdir(parents=True, exist_ok=True)

    plan = _make_core_plan("NVDA", entry_price=100.0)
    upsert_position_plan(cfg, plan)

    tools = {t.name: t for t in build_pm_tools(cfg, {"NVDA"})}
    result = tools["record_reunderwrite_verdict"].invoke({
        "ticker": "NVDA",
        "verdict": "reconfirmed",
        "reason": "thesis intact",
    })

    assert "no pending re-underwrite" in result.lower()


def test_record_reunderwrite_verdict_invalid_verdict(tmp_path):
    """Calling tool with invalid verdict string returns an error."""
    from tradingagents.portfolio_advisor.pm_tools import build_pm_tools

    cfg = _cfg(tmp_path)
    Path(cfg["portfolio_advisor_dir"]).mkdir(parents=True, exist_ok=True)

    tools = {t.name: t for t in build_pm_tools(cfg, set())}
    result = tools["record_reunderwrite_verdict"].invoke({
        "ticker": "NVDA",
        "verdict": "maybe",
        "reason": "not sure",
    })
    assert "error" in result.lower()


def test_record_reunderwrite_verdict_requires_reason(tmp_path):
    """Calling tool with empty reason returns an error."""
    from tradingagents.portfolio_advisor.pm_tools import build_pm_tools

    cfg = _cfg(tmp_path)
    Path(cfg["portfolio_advisor_dir"]).mkdir(parents=True, exist_ok=True)

    tools = {t.name: t for t in build_pm_tools(cfg, set())}
    result = tools["record_reunderwrite_verdict"].invoke({
        "ticker": "NVDA",
        "verdict": "reconfirmed",
        "reason": "",
    })
    assert "error" in result.lower()
