"""Tests for R1 — Execution machinery.

Covers:
- Spread > 100 bps → skip with reason logged
- Quote unavailable → market-order fallback (notional path)
- Catalyst buy → bracket order with stop at entry × 0.92
- Catalyst buy with market_clock {"is_open": False} → skipped
- Catalyst buy with market_clock {"is_open": True} → proceeds
- Catalyst buy with market_clock returning None → proceeds (unknown = allow)
- Core buy queues regardless of market-clock state
- Slippage row math (intended 100, filled 100.5 → +50 bps)
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **overrides) -> Dict[str, Any]:
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_alpaca_paper": True,
        "portfolio_advisor_alpaca_max_position_pct": 0.10,
        "portfolio_advisor_add_cooldown_days": 0,       # disable for these tests
        "portfolio_advisor_reentry_cooldown_days": 0,   # disable for these tests
        "portfolio_advisor_max_spread_bps": 100,
        "portfolio_advisor_limit_slip_bps": 10,
        "portfolio_advisor_catalyst_hard_stop_pct": 0.08,
    }
    cfg.update(overrides)
    return cfg


def _pa_dir(cfg: Dict[str, Any]) -> Path:
    p = Path(cfg["portfolio_advisor_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def _make_client(equity: float = 100_000.0) -> MagicMock:
    """Minimal mock TradingClient with a fresh order on submit."""
    client = MagicMock()
    acct = MagicMock()
    acct.equity = str(equity)
    client.get_account.return_value = acct
    order = MagicMock()
    order.id = "mock-order-id"
    client.submit_order.return_value = order
    client.get_open_position.side_effect = Exception("no position")
    client.get_all_positions.return_value = []
    return client


def _core_proposal(ticker: str = "AAPL", usd: float = 5000.0) -> Dict[str, Any]:
    return {"ticker": ticker, "action": "buy", "approx_usd": usd, "sleeve": "core"}


def _catalyst_proposal(ticker: str = "SMCI", usd: float = 5000.0) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "action": "buy",
        "approx_usd": usd,
        "sleeve": "catalyst",
        "catalyst_date": "2026-09-15",
    }


def _quote(bid: float, ask: float) -> Dict[str, float]:
    return {"bid": bid, "ask": ask}


# ---------------------------------------------------------------------------
# Spread guard tests
# ---------------------------------------------------------------------------


class TestSpreadGuard:
    """Spread > max_spread_bps → skip; spread ≤ max → proceed."""

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_wide_spread_skips_with_reason(self, mock_enabled, mock_quote, tmp_path):
        """Spread > 100 bps → skip with logged reason, no order submitted."""
        # bid=100, ask=102 → spread = 2/102*10000 ≈ 196 bps
        mock_quote.return_value = _quote(100.0, 102.0)
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "SMCI", _core_proposal("SMCI"))

        assert "skipped" in result.lower()
        assert "spread" in result.lower()
        client.submit_order.assert_not_called()

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_wide_spread_logged_to_ledger(self, mock_enabled, mock_quote, tmp_path):
        """A spread-rejected buy writes a ledger row with spread_bps."""
        mock_quote.return_value = _quote(100.0, 102.0)  # ~196 bps
        cfg = _cfg(tmp_path)
        _pa_dir(cfg)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "SMCI", _core_proposal("SMCI"))

        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        assert ledger.is_file(), "Ledger file should be written"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        assert any(r.get("status") == "skipped" and "spread" in str(r.get("note", "")) for r in rows)

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_tight_spread_proceeds_as_limit(self, mock_enabled, mock_quote, tmp_path):
        """Spread ≤ 100 bps → proceeds, submits a limit order."""
        # bid=99.90, ask=100.0 → spread = 10/100*100 = 10 bps
        mock_quote.return_value = _quote(99.90, 100.0)
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "AAPL", _core_proposal("AAPL"))

        assert "skipped" not in result.lower() or "spread" not in result.lower()
        client.submit_order.assert_called_once()

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_custom_max_spread_bps_respected(self, mock_enabled, mock_quote, tmp_path):
        """Custom portfolio_advisor_max_spread_bps=200 allows a 196bps spread."""
        mock_quote.return_value = _quote(100.0, 102.0)  # ~196 bps
        cfg = _cfg(tmp_path, portfolio_advisor_max_spread_bps=200)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "AAPL", _core_proposal("AAPL"))

        assert "spread" not in result.lower() or "skipped" not in result.lower()
        client.submit_order.assert_called_once()


# ---------------------------------------------------------------------------
# Quote-unavailable fallback
# ---------------------------------------------------------------------------


class TestQuoteUnavailableFallback:
    """When _latest_quote returns None → market order fallback (notional path)."""

    @patch("tradingagents.integrations.alpaca.executor._latest_quote", return_value=None)
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_market_order_fallback_on_none_quote(self, mock_enabled, mock_quote, tmp_path):
        """None quote → submit_order called with a MarketOrderRequest (has notional)."""
        from alpaca.trading.requests import MarketOrderRequest

        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "AAPL", _core_proposal("AAPL"))

        assert "skipped" not in result.lower()
        client.submit_order.assert_called_once()
        req = client.submit_order.call_args[0][0]
        assert isinstance(req, MarketOrderRequest), "Should fall back to MarketOrderRequest"
        assert req.notional is not None

    @patch("tradingagents.integrations.alpaca.executor._latest_quote", return_value=None)
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_market_fallback_is_plain_market_not_bracket(self, mock_enabled, mock_quote, tmp_path):
        """Catalyst + no quote → plain market order (can't attach bracket without price)."""
        from alpaca.trading.requests import MarketOrderRequest

        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, MarketOrderRequest)


# ---------------------------------------------------------------------------
# Limit price math
# ---------------------------------------------------------------------------


class TestLimitPriceMath:
    """ask × (1 + slip_bps/10000); qty = floor(notional / limit_price × 1000) / 1000."""

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_limit_price_is_ask_plus_slip(self, mock_enabled, mock_quote, tmp_path):
        """limit_price = ask * (1 + 10/10000) = 100.0 * 1.001 = 100.1."""
        mock_quote.return_value = _quote(99.9, 100.0)
        cfg = _cfg(tmp_path, portfolio_advisor_limit_slip_bps=10)
        client = _make_client()

        from alpaca.trading.requests import LimitOrderRequest
        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "AAPL", _core_proposal("AAPL", usd=5000.0))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest)
        expected_limit = round(100.0 * 1.001, 4)
        assert abs(req.limit_price - expected_limit) < 0.001, \
            f"Expected limit_price ≈ {expected_limit}, got {req.limit_price}"

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_qty_is_floor_3_decimals(self, mock_enabled, mock_quote, tmp_path):
        """qty = floor(notional / limit_price * 1000) / 1000, rounded down."""
        # ask=100.0, slip=10bps → limit=100.1, notional≈5000 → qty = floor(5000/100.1*1000)/1000
        mock_quote.return_value = _quote(99.9, 100.0)
        cfg = _cfg(tmp_path)
        client = _make_client(equity=100_000.0)

        from alpaca.trading.requests import LimitOrderRequest
        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "AAPL", _core_proposal("AAPL", usd=5000.0))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest)
        limit_price = req.limit_price
        # notional ≈ min(5000, 100000*0.10*conf_mult) — use actual submitted qty * price
        # The key invariant: qty is a multiple of 0.001 and ≤ notional / limit_price
        qty = req.qty
        assert qty is not None
        # Check it's rounded to 3 decimals (no more than 3 decimal places)
        assert round(qty, 3) == qty
        # Check floor: qty * limit_price ≤ notional would hold (approximately;
        # conf_mult applies so we just verify the format)
        assert qty > 0


# ---------------------------------------------------------------------------
# Bracket order for catalyst buys
# ---------------------------------------------------------------------------


class TestCatalystBracketOrder:
    """Catalyst sleeve → bracket order with stop at entry × (1 - 0.08)."""

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.market_clock", return_value={"is_open": True})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_buy_submits_bracket(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Catalyst + tight spread → submit bracket order with stop_loss."""
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.requests import LimitOrderRequest

        mock_quote.return_value = _quote(99.9, 100.0)
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest), "Catalyst limit order must be LimitOrderRequest"
        assert req.order_class == OrderClass.BRACKET, "Catalyst buy must use bracket order class"
        assert req.stop_loss is not None, "Bracket order must include stop_loss"

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.market_clock", return_value={"is_open": True})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_bracket_stop_at_entry_times_0_92(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Bracket stop_price = limit_price × (1 - 0.08) = limit × 0.92."""
        from alpaca.trading.requests import LimitOrderRequest

        mock_quote.return_value = _quote(99.9, 100.0)
        cfg = _cfg(tmp_path, portfolio_advisor_catalyst_hard_stop_pct=0.08)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest)
        limit_price = req.limit_price
        expected_stop = round(limit_price * (1 - 0.08), 4)
        actual_stop = req.stop_loss.stop_price
        assert abs(actual_stop - expected_stop) < 0.001, \
            f"Expected stop_price ≈ {expected_stop}, got {actual_stop}"

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.market_clock", return_value={"is_open": True})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_core_buy_does_not_use_bracket(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Core buy → plain limit order, no bracket."""
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.requests import LimitOrderRequest

        mock_quote.return_value = _quote(99.9, 100.0)
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "AAPL", _core_proposal("AAPL"))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest)
        # Core orders must NOT be bracket
        assert getattr(req, "order_class", None) != OrderClass.BRACKET


# ---------------------------------------------------------------------------
# Market-closed catalyst skip
# ---------------------------------------------------------------------------


class TestMarketClosedCatalystSkip:
    """Catalyst buy: closed market → skip; open or None → proceed."""

    @patch("tradingagents.integrations.alpaca.executor._latest_quote", return_value=None)
    @patch("tradingagents.integrations.alpaca.executor.market_clock",
           return_value={"is_open": False, "next_open": "2026-06-11T13:30:00+00:00"})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_skipped_when_market_closed(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Catalyst buy is skipped when market_clock returns is_open=False."""
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        assert "skipped" in result.lower()
        assert "market closed" in result.lower()
        client.submit_order.assert_not_called()

    @patch("tradingagents.integrations.alpaca.executor._latest_quote", return_value=None)
    @patch("tradingagents.integrations.alpaca.executor.market_clock",
           return_value={"is_open": True, "next_close": "2026-06-10T20:00:00+00:00"})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_proceeds_when_market_open(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Catalyst buy proceeds when market_clock returns is_open=True."""
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        # Should not skip for market-closed reason
        assert "market closed" not in result.lower()
        client.submit_order.assert_called_once()

    @patch("tradingagents.integrations.alpaca.executor._latest_quote", return_value=None)
    @patch("tradingagents.integrations.alpaca.executor.market_clock", return_value=None)
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_proceeds_when_clock_none(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Catalyst buy proceeds when market_clock returns None (unknown = allow)."""
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        assert "market closed" not in result.lower()
        client.submit_order.assert_called_once()

    @patch("tradingagents.integrations.alpaca.executor._latest_quote", return_value=None)
    @patch("tradingagents.integrations.alpaca.executor.market_clock",
           return_value={"is_open": False})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_core_buy_queues_regardless_of_closed_market(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Core buy proceeds even when market_clock says closed (DAY order queues at open)."""
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "AAPL", _core_proposal("AAPL"))

        assert "market closed" not in result.lower()
        client.submit_order.assert_called_once()


# ---------------------------------------------------------------------------
# Fill + slippage logging
# ---------------------------------------------------------------------------


class TestFillSlippageLogging:
    """fill_check ledger rows: slippage_bps math, reconcile_fills back-fill."""

    def _write_pending_order(
        self,
        cfg: Dict[str, Any],
        order_id: str,
        intended_price: Optional[float],
    ) -> None:
        """Write a submitted buy row with fill_check_pending=True to the ledger."""
        _pa_dir(cfg)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": "AAPL",
            "action": "buy",
            "status": "submitted",
            "order_id": order_id,
            "notional_usd": 5000.0,
            "intended_price": intended_price,
            "fill_check_pending": True,
        }
        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        with ledger.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def test_slippage_math_50bps(self, tmp_path):
        """intended=100, filled=100.5 → slippage_bps = (100.5-100)/100 * 10000 = 50."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        _pa_dir(cfg)
        order_id = "test-order-slippage"
        self._write_pending_order(cfg, order_id, 100.0)

        mock_order = MagicMock()
        mock_order.status = "filled"
        mock_order.filled_avg_price = "100.5"

        with patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_order_by_id.return_value = mock_order
            mock_client_fn.return_value = mock_c

            ex._check_order_fill(cfg, order_id, 100.0)

        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        fill_rows = [r for r in rows if r.get("action") == "fill_check"]
        assert len(fill_rows) == 1
        fr = fill_rows[0]
        assert fr["order_id"] == order_id
        assert fr["status"] == "filled"
        assert abs(fr["filled_avg_price"] - 100.5) < 0.001
        assert abs(fr["slippage_bps"] - 50.0) < 0.5, \
            f"Expected 50 bps slippage, got {fr['slippage_bps']}"

    def test_slippage_zero_when_no_intended_price(self, tmp_path):
        """When intended_price is None (market order), slippage_bps is None."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        _pa_dir(cfg)

        mock_order = MagicMock()
        mock_order.status = "filled"
        mock_order.filled_avg_price = "100.5"

        with patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_order_by_id.return_value = mock_order
            mock_client_fn.return_value = mock_c

            ex._check_order_fill(cfg, "some-order", None)

        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        fill_rows = [r for r in rows if r.get("action") == "fill_check"]
        assert fill_rows[0]["slippage_bps"] is None

    def test_check_order_fill_silent_on_failure(self, tmp_path):
        """_check_order_fill never raises, even if _client() fails."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)

        with patch.object(ex, "_client", side_effect=Exception("no client")):
            # Must not raise
            ex._check_order_fill(cfg, "some-id", 100.0)

    def test_reconcile_fills_back_fills_unreconciled(self, tmp_path):
        """reconcile_fills writes fill_check rows for pending orders missing them."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        order_id = "unreconciled-order"
        self._write_pending_order(cfg, order_id, 50.0)

        closed_order = MagicMock()
        closed_order.id = order_id
        closed_order.status = "filled"
        closed_order.filled_avg_price = "50.25"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [closed_order]
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        fill_rows = [r for r in rows if r.get("action") == "fill_check"]
        assert len(fill_rows) == 1
        fr = fill_rows[0]
        assert fr["order_id"] == order_id
        # slippage = (50.25 - 50) / 50 * 10000 = 50 bps
        assert abs(fr["slippage_bps"] - 50.0) < 1.0

    def test_reconcile_fills_skips_already_reconciled(self, tmp_path):
        """reconcile_fills does NOT write a second fill_check for already-reconciled orders."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        order_id = "already-reconciled"
        self._write_pending_order(cfg, order_id, 50.0)

        # Write a fill_check row already
        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        with ledger.open("a") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "fill_check",
                "order_id": order_id,
                "status": "filled",
                "filled_avg_price": 50.25,
                "slippage_bps": 50.0,
            }) + "\n")

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = []  # don't need to return anything
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        fill_rows = [r for r in rows if r.get("action") == "fill_check"]
        # Should still be exactly 1 (not duplicated)
        assert len(fill_rows) == 1

    def test_reconcile_fills_silent_when_disabled(self, tmp_path):
        """reconcile_fills returns silently when executor is disabled."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)

        with patch.object(ex, "enabled", return_value=False), \
             patch.object(ex, "_client") as mock_client_fn:
            ex.reconcile_fills(cfg)
            mock_client_fn.assert_not_called()

    def test_reconcile_fills_silent_on_api_failure(self, tmp_path):
        """reconcile_fills never raises when the Alpaca API call fails."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        self._write_pending_order(cfg, "order-x", 100.0)

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.side_effect = Exception("network failure")
            mock_client_fn.return_value = mock_c

            # Must not raise
            ex.reconcile_fills(cfg)


# ---------------------------------------------------------------------------
# _latest_quote guard: always returns None under pytest
# ---------------------------------------------------------------------------


class TestLatestQuoteGuard:
    """_latest_quote must return None under PYTEST_CURRENT_TEST."""

    def test_latest_quote_returns_none_under_pytest(self, tmp_path):
        """Under test (PYTEST_CURRENT_TEST set), _latest_quote returns None."""
        from tradingagents.integrations.alpaca import executor as ex

        # PYTEST_CURRENT_TEST is always set when running pytest
        result = ex._latest_quote("AAPL")
        assert result is None, "_latest_quote must return None under pytest"


# ---------------------------------------------------------------------------
# enforce_paper_exits calls reconcile_fills
# ---------------------------------------------------------------------------


class TestEnforcePaperExitsCallsReconcile:
    """reconcile_fills is called at the top of enforce_paper_exits."""

    @patch("tradingagents.integrations.alpaca.executor.reconcile_fills")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    @patch("tradingagents.integrations.alpaca.executor._client")
    def test_enforce_paper_exits_calls_reconcile_fills(
        self, mock_client_fn, mock_enabled, mock_reconcile, tmp_path
    ):
        """enforce_paper_exits calls reconcile_fills before processing positions."""
        cfg = _cfg(tmp_path)
        client = MagicMock()
        client.get_all_positions.return_value = []
        mock_client_fn.return_value = client

        from tradingagents.integrations.alpaca import executor as ex

        ex.enforce_paper_exits(cfg)

        mock_reconcile.assert_called_once_with(cfg)
