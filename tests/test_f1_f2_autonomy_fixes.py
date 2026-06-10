"""Tests for F1 and F2 autonomy gap fixes (compounder_2_1_autonomy_fixes.md).

F1 — Auto-create PositionPlan on autonomous buy:
  - catalyst buy creates position_plans.json entry with strategy=catalyst + date
  - core buy creates plan with strategy=core
  - catalyst_date written to alpaca ledger row
  - plan-first sleeve resolution in enforce_paper_exits + one-time warning

F2 — Proposal lifecycle: mark executed/cancelled, cooldown gate:
  (a) buy → executed status after successful paper execution
  (b) re-proposal after exit executes again (executed/cancelled rows release gate)
  (c) add 5+ days after buy executes (tranche allowed); add 1 day after is skipped
  (d) sell of unheld name → cancelled
  (e) disabled mode (no keys / test env) leaves status as proposed
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
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
        "portfolio_advisor_alpaca_paper": True,
        "portfolio_advisor_alpaca_max_position_pct": 0.10,
        "portfolio_advisor_add_cooldown_days": 5,
        "portfolio_advisor_catalyst_max_hold_days": 30,
        "portfolio_advisor_catalyst_time_stop_days": 3,
        "portfolio_advisor_catalyst_hard_stop_pct": 0.08,
    }


def _make_alpaca_client(equity: float = 100_000.0, position_market_value: float = 0.0):
    """Mock TradingClient that simulates a paper book."""
    client = MagicMock()
    acct = MagicMock()
    acct.equity = str(equity)
    client.get_account.return_value = acct

    order = MagicMock()
    order.id = "mock-order-id-001"
    client.submit_order.return_value = order

    if position_market_value > 0:
        pos = MagicMock()
        pos.market_value = str(position_market_value)
        pos.unrealized_plpc = "0.05"
        client.get_open_position.return_value = pos
    else:
        client.get_open_position.side_effect = Exception("no position")

    client.get_all_positions.return_value = []
    return client


def _write_ledger_row(pa_dir: Path, ticker: str, action: str, days_ago: int = 0) -> None:
    """Directly write a submitted buy row to alpaca_trades.jsonl."""
    pa_dir.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    row = {
        "ts": ts,
        "ticker": ticker.upper(),
        "action": action,
        "status": "submitted",
        "sleeve": "core",
        "order_id": "old-order",
    }
    ledger = pa_dir / "alpaca_trades.jsonl"
    with ledger.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


# ===========================================================================
# F1 Tests — Auto-create PositionPlan
# ===========================================================================


class TestF1AutoCreatePositionPlan:
    """F1: _paper_buy auto-creates a PositionPlan with correct fields."""

    def _run_buy(self, cfg, proposal, client):
        """Run _paper_buy with executor enabled (no PYTEST env guard needed
        because we call the internal function directly, not execute_proposal)."""
        from tradingagents.integrations.alpaca import executor as ex

        return ex._paper_buy(cfg, client, 100_000.0, proposal["ticker"].upper(), proposal)

    def test_catalyst_buy_creates_plan_with_strategy_and_date(self, tmp_path):
        cfg = _cfg(tmp_path)
        client = _make_alpaca_client()
        proposal = {
            "ticker": "NVDA",
            "action": "buy",
            "approx_usd": 5000.0,
            "target_price": 130.0,
            "sleeve": "catalyst",
            "catalyst_date": "2026-06-15",
            "confidence": None,
        }

        result = self._run_buy(cfg, proposal, client)

        assert "skipped" not in result.lower(), f"Expected buy to execute, got: {result}"

        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        plans = load_position_plans(cfg)
        assert "NVDA" in plans, "PositionPlan must be created for autonomous buy"
        plan = plans["NVDA"]
        assert plan.strategy == "catalyst"
        assert plan.catalyst_date == "2026-06-15"
        assert plan.entry_price == pytest.approx(130.0)
        assert "auto-created on autonomous buy" in plan.notes

    def test_core_buy_creates_plan_with_core_strategy(self, tmp_path):
        cfg = _cfg(tmp_path)
        client = _make_alpaca_client()
        proposal = {
            "ticker": "MSFT",
            "action": "buy",
            "approx_usd": 4000.0,
            "target_price": 400.0,
            "sleeve": "core",
            "catalyst_date": None,
            "confidence": None,
        }

        result = self._run_buy(cfg, proposal, client)
        assert "skipped" not in result.lower()

        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        plans = load_position_plans(cfg)
        assert "MSFT" in plans
        assert plans["MSFT"].strategy == "core"
        assert plans["MSFT"].catalyst_date == ""

    def test_existing_plan_not_overwritten(self, tmp_path):
        """An existing plan must not be clobbered by a new autonomous buy."""
        cfg = _cfg(tmp_path)
        from tradingagents.portfolio_advisor.position_plans import PositionPlan, upsert_position_plan

        existing_plan = PositionPlan(
            ticker="AAPL",
            entry_price=150.0,
            strategy="core",
            notes="manually set",
        )
        upsert_position_plan(cfg, existing_plan)

        client = _make_alpaca_client()
        proposal = {
            "ticker": "AAPL",
            "action": "buy",
            "approx_usd": 3000.0,
            "target_price": 200.0,  # different entry price
            "sleeve": "catalyst",
            "catalyst_date": "2026-07-01",
            "confidence": None,
        }
        self._run_buy(cfg, proposal, client)

        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        plans = load_position_plans(cfg)
        # Strategy and entry_price must not change
        assert plans["AAPL"].strategy == "core"
        assert plans["AAPL"].entry_price == pytest.approx(150.0)
        assert plans["AAPL"].notes == "manually set"

    def test_existing_plan_gets_catalyst_date_if_missing(self, tmp_path):
        """Existing plan with no catalyst_date should get it from the new buy."""
        cfg = _cfg(tmp_path)
        from tradingagents.portfolio_advisor.position_plans import PositionPlan, upsert_position_plan

        existing_plan = PositionPlan(
            ticker="CRWD",
            entry_price=200.0,
            strategy="catalyst",
            catalyst_date="",
        )
        upsert_position_plan(cfg, existing_plan)

        client = _make_alpaca_client()
        proposal = {
            "ticker": "CRWD",
            "action": "add",
            "approx_usd": 2000.0,
            "target_price": 200.0,
            "sleeve": "catalyst",
            "catalyst_date": "2026-06-20",
            "confidence": None,
        }
        self._run_buy(cfg, proposal, client)

        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        plans = load_position_plans(cfg)
        # catalyst_date should be updated from the buy proposal
        assert plans["CRWD"].catalyst_date == "2026-06-20"

    def test_catalyst_date_in_ledger_row(self, tmp_path):
        """catalyst_date must appear in the alpaca_trades.jsonl buy row."""
        cfg = _cfg(tmp_path)
        client = _make_alpaca_client()
        proposal = {
            "ticker": "AMD",
            "action": "buy",
            "approx_usd": 3000.0,
            "target_price": 150.0,
            "sleeve": "catalyst",
            "catalyst_date": "2026-06-25",
            "confidence": None,
        }
        self._run_buy(cfg, proposal, client)

        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        assert ledger.is_file()
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        buy_rows = [r for r in rows if r.get("ticker") == "AMD" and r.get("status") == "submitted"]
        assert len(buy_rows) == 1
        assert buy_rows[0].get("catalyst_date") == "2026-06-25"


class TestF1EnforcePaperExitsPlanFirst:
    """F1.3: enforce_paper_exits prefers plan sleeve over ledger row."""

    def _mock_positions(self, entries):
        """entries: list of (ticker, unrealized_plpc) tuples."""
        positions = []
        for tk, plpc in entries:
            pos = MagicMock()
            pos.symbol = tk
            pos.unrealized_plpc = str(plpc)
            pos.market_value = "5000"
            positions.append(pos)
        return positions

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_uses_plan_sleeve_over_ledger(self, mock_enabled, mock_client_fn, tmp_path):
        """Position with plan strategy=catalyst should use catalyst hard stop,
        even if the ledger row says sleeve=core."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import PositionPlan, upsert_position_plan

        # Write a plan saying catalyst
        upsert_position_plan(cfg, PositionPlan(
            ticker="ZETA",
            entry_price=50.0,
            strategy="catalyst",
            entry_date="2026-06-01",
        ))

        # Write a ledger row saying core (conflicting)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "ticker": "ZETA",
            "action": "buy",
            "status": "submitted",
            "sleeve": "core",  # intentionally wrong
        }
        (pa_dir / "alpaca_trades.jsonl").write_text(json.dumps(row) + "\n")

        # Build the position mock — close_for_watchdog also calls get_open_position
        zeta_pos = MagicMock()
        zeta_pos.market_value = "5000"
        zeta_pos.unrealized_plpc = "-0.09"

        client = MagicMock()
        client.get_all_positions.return_value = self._mock_positions([("ZETA", -0.09)])
        # get_open_position is called by close_for_watchdog — must succeed
        client.get_open_position.return_value = zeta_pos
        client.close_position.return_value = MagicMock(id="close-order")
        mock_client_fn.return_value = client

        # -9% should trigger catalyst hard stop (>= 8%), NOT core stop (needs -40%)
        closed = ex.enforce_paper_exits(cfg)
        assert closed == 1, "Catalyst hard stop should fire at -9%, not core stop"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    @patch("tradingagents.integrations.alpaca.executor._notify")
    def test_no_plan_no_ledger_sends_warning(
        self, mock_notify, mock_enabled, mock_client_fn, tmp_path
    ):
        """Position with neither plan nor ledger row should get a one-time warning."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        # Clear the module-level warned set before the test
        ex._warned_no_plan_tickers.discard("UNKNW")

        client = MagicMock()
        # Position at -10% but still above -40% (core threshold)
        client.get_all_positions.return_value = self._mock_positions([("UNKNW", -0.10)])
        client.close_position.return_value = MagicMock(id="close-order")
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        # -10% does not reach -40% core floor, so nothing closed
        assert closed == 0
        # One-time warning must have been sent
        mock_notify.assert_called_once()
        assert "UNKNW" in mock_notify.call_args[0][1]  # ticker in subject

        # Second call must NOT send another warning
        mock_notify.reset_mock()
        ex.enforce_paper_exits(cfg)
        mock_notify.assert_not_called()


# ===========================================================================
# F2 Tests — Proposal lifecycle
# ===========================================================================


class TestF2ProposalLifecycle:
    """F2: proposals.add() marks status based on executor result."""

    def _add_proposal(self, cfg, ticker="NVDA", action="buy", usd=5000.0, sleeve="core",
                      catalyst_date="", executor_result=None):
        """Call proposals.add() with a mocked executor."""
        from tradingagents.portfolio_advisor import proposals as pr

        result = executor_result or {"status": "executed", "detail": "BUY NVDA ~$5000 (core) executed."}

        with patch(
            "tradingagents.integrations.alpaca.executor.execute_proposal",
            return_value=result,
        ):
            return pr.add(
                cfg,
                ticker=ticker,
                action=action,
                approx_usd=usd,
                sleeve=sleeve,
                catalyst_date=catalyst_date,
                reason="test proposal",
            )

    # (a) buy → executed status
    def test_buy_marks_executed_on_success(self, tmp_path):
        cfg = _cfg(tmp_path)
        entry = self._add_proposal(cfg, ticker="NVDA", action="buy",
                                   executor_result={"status": "executed", "detail": "BUY NVDA ok"})

        from tradingagents.portfolio_advisor import proposals as pr

        rows = pr.load_all(cfg)
        nvda_rows = [r for r in rows if r.get("ticker") == "NVDA"]
        assert len(nvda_rows) == 1
        assert nvda_rows[0]["status"] == "executed"
        assert "BUY NVDA ok" in nvda_rows[0].get("status_note", "")

    # (a) executor error → cancelled so future re-entry is not blocked
    def test_buy_marks_cancelled_on_error(self, tmp_path):
        cfg = _cfg(tmp_path)
        self._add_proposal(cfg, ticker="TSLA", action="buy",
                           executor_result={"status": "error", "detail": "paper execution error: timeout"})

        from tradingagents.portfolio_advisor import proposals as pr

        rows = pr.load_all(cfg)
        tsla_rows = [r for r in rows if r.get("ticker") == "TSLA"]
        assert tsla_rows[0]["status"] == "cancelled"

    # (b) re-proposal after exit: executed row releases the dedup gate
    def test_re_proposal_after_executed_row_executes_again(self, tmp_path):
        """An executed row must NOT block a new buy on the same ticker+side."""
        cfg = _cfg(tmp_path)

        # First buy — executed
        self._add_proposal(cfg, ticker="CRM", action="buy",
                           executor_result={"status": "executed", "detail": "ok"})

        # Verify first is executed
        from tradingagents.portfolio_advisor import proposals as pr
        rows = pr.load_all(cfg)
        assert any(r.get("status") == "executed" for r in rows if r.get("ticker") == "CRM")

        # Second buy — should execute again (gate released by executed status)
        exec_mock = MagicMock(return_value={"status": "executed", "detail": "ok 2"})
        with patch("tradingagents.integrations.alpaca.executor.execute_proposal", exec_mock):
            pr.add(cfg, ticker="CRM", action="buy", approx_usd=5000.0, reason="re-entry")

        exec_mock.assert_called_once()  # executor was called for the new proposal

        rows = pr.load_all(cfg)
        crm_executed = [r for r in rows if r.get("ticker") == "CRM" and r.get("status") == "executed"]
        assert len(crm_executed) == 2, "Both buy proposals should be executed"

    # (c) add cooldown: 5+ days after buy succeeds; 1 day after is skipped
    def test_add_after_cooldown_window_executes(self, tmp_path):
        """An 'add' proposal > cooldown_days after last buy should go through."""
        cfg = _cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        # Write a buy row that is 6 days old (outside 5-day cooldown)
        _write_ledger_row(pa_dir, "SHOP", "buy", days_ago=6)

        exec_mock = MagicMock(return_value={"status": "executed", "detail": "ADD SHOP ok"})
        with patch("tradingagents.integrations.alpaca.executor.execute_proposal", exec_mock):
            from tradingagents.portfolio_advisor import proposals as pr

            pr.add(cfg, ticker="SHOP", action="add", approx_usd=3000.0, reason="tranche 2")

        exec_mock.assert_called_once()

    def test_add_within_cooldown_window_is_skipped(self, tmp_path):
        """An 'add' proposal within cooldown_days should be skipped (not executed)."""
        cfg = _cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        # Write a buy row that is only 1 day old (within 5-day cooldown)
        _write_ledger_row(pa_dir, "SHOP", "buy", days_ago=1)

        exec_mock = MagicMock(return_value={"status": "skipped", "detail": "cooldown: buy/add within last 5d"})
        with patch("tradingagents.integrations.alpaca.executor.execute_proposal", exec_mock):
            from tradingagents.portfolio_advisor import proposals as pr

            pr.add(cfg, ticker="SHOP", action="add", approx_usd=3000.0, reason="too soon")

        # Status should be cancelled (skipped by executor)
        rows = pr.load_all(cfg)
        shop_rows = [r for r in rows if r.get("ticker") == "SHOP" and r.get("action") == "add"]
        assert shop_rows[0]["status"] == "cancelled"

    # (d) sell of unheld name → cancelled
    def test_sell_unheld_name_cancelled(self, tmp_path):
        cfg = _cfg(tmp_path)
        result = {"status": "skipped", "detail": "skipped NOPE sell: not held in paper book"}
        self._add_proposal(cfg, ticker="NOPE", action="sell", executor_result=result)

        from tradingagents.portfolio_advisor import proposals as pr

        rows = pr.load_all(cfg)
        nope_rows = [r for r in rows if r.get("ticker") == "NOPE"]
        assert nope_rows[0]["status"] == "cancelled"

    # (e) disabled mode: executor returns disabled → status stays proposed
    def test_disabled_executor_leaves_proposed(self, tmp_path):
        cfg = _cfg(tmp_path)
        result = {"status": "disabled", "detail": "executor not enabled"}
        self._add_proposal(cfg, ticker="META", action="buy", executor_result=result)

        from tradingagents.portfolio_advisor import proposals as pr

        rows = pr.load_all(cfg)
        meta_rows = [r for r in rows if r.get("ticker") == "META"]
        assert meta_rows[0]["status"] == "proposed"


class TestF2Cooldown:
    """F2: add-cooldown logic in _paper_buy (unit test the helper directly)."""

    def test_cooldown_skip_within_window(self, tmp_path):
        cfg = _cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        _write_ledger_row(pa_dir, "NVDA", "buy", days_ago=2)

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason, "Should return skip reason within cooldown window"
        assert "cooldown" in reason

    def test_cooldown_allows_after_window(self, tmp_path):
        cfg = _cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        _write_ledger_row(pa_dir, "NVDA", "buy", days_ago=7)

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason == "", "Should allow after cooldown window"

    def test_cooldown_no_ledger(self, tmp_path):
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason == "", "No ledger → no cooldown"

    def test_cooldown_checks_only_same_ticker(self, tmp_path):
        cfg = _cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        _write_ledger_row(pa_dir, "AAPL", "buy", days_ago=1)  # different ticker

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason == "", "Cooldown for different ticker should not block"


class TestF2ExecuteProposalResult:
    """F2: execute_proposal returns machine-readable dict."""

    def test_disabled_returns_dict(self, tmp_path):
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        # In test environment, enabled() returns False (PYTEST_CURRENT_TEST guard)
        result = ex.execute_proposal(cfg, {"ticker": "NVDA", "action": "buy", "approx_usd": 5000})
        assert isinstance(result, dict)
        assert result["status"] == "disabled"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_executed_returns_dict_with_executed_status(self, mock_enabled, mock_client_fn, tmp_path):
        cfg = _cfg(tmp_path)
        client = _make_alpaca_client(equity=100_000.0)
        mock_client_fn.return_value = client

        from tradingagents.integrations.alpaca import executor as ex

        result = ex.execute_proposal(cfg, {
            "ticker": "TSLA",
            "action": "buy",
            "approx_usd": 5000.0,
            "target_price": 200.0,
            "sleeve": "core",
        })
        assert isinstance(result, dict)
        assert result["status"] == "executed"
        assert "detail" in result

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_skipped_returns_skipped_status(self, mock_enabled, mock_client_fn, tmp_path):
        """Already-held name with action=buy returns skipped status."""
        cfg = _cfg(tmp_path)
        client = _make_alpaca_client(equity=100_000.0, position_market_value=5000.0)
        mock_client_fn.return_value = client

        from tradingagents.integrations.alpaca import executor as ex

        result = ex.execute_proposal(cfg, {
            "ticker": "NVDA",
            "action": "buy",  # not "add" — should skip for already-held
            "approx_usd": 5000.0,
            "target_price": 130.0,
            "sleeve": "core",
        })
        assert isinstance(result, dict)
        assert result["status"] == "skipped"
        assert "already held" in result["detail"].lower()


class TestF2AutoCloseStalePath:
    """F2.5: auto_close_stale is reachable and works correctly."""

    def test_auto_close_stale_cancels_old_proposals(self, tmp_path):
        from tradingagents.portfolio_advisor import proposals as pr

        cfg = _cfg(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        row = {
            "ts": old_ts,
            "ticker": "AAPL",
            "action": "buy",
            "status": "proposed",
            "reason": "old idea",
        }
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        (pa_dir / "proposed_trades.jsonl").write_text(json.dumps(row) + "\n")

        n = pr.auto_close_stale(cfg, max_age_days=14)
        assert n == 1

        rows = pr.load_all(cfg)
        assert rows[0]["status"] == "cancelled"
        assert "stale" in rows[0].get("status_note", "")

    def test_auto_close_stale_ignores_recent(self, tmp_path):
        from tradingagents.portfolio_advisor import proposals as pr

        cfg = _cfg(tmp_path)
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        row = {"ts": recent_ts, "ticker": "AAPL", "action": "buy", "status": "proposed"}
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        (pa_dir / "proposed_trades.jsonl").write_text(json.dumps(row) + "\n")

        n = pr.auto_close_stale(cfg, max_age_days=14)
        assert n == 0
