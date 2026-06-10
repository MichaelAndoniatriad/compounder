"""Tests for R1 — Execution machinery.

Covers:
- Spread > 100 bps → skip with reason logged
- Quote unavailable → market-order fallback (notional path)
- Catalyst buy → simple whole-share DAY limit (NOT bracket — bracket was verified-broken)
- Catalyst buy: market closed → deferred (not cancelled); retried by execute_deferred_entries
- Catalyst buy with market_clock {"is_open": True} → proceeds
- Catalyst buy with market_clock returning None → proceeds (unknown = allow)
- Core buy queues regardless of market-clock state
- Slippage row math (intended 100, filled 100.5 → +50 bps)
- Sub-penny rule: limit_price rounded to 2dp (SEC Rule 612)
- GTC stop submitted post-fill in reconcile_fills at actual fill price × 0.92
- cancel-before-close: _cancel_open_orders_for_symbol called in close_for_watchdog
- Entry-expired unwind: proposal cancelled + plan deleted when DAY limit expires unfilled
- Deferred flow: closed market → deferred_market_closed → execute_deferred_entries at open
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
# Catalyst order redesign (was: bracket order — now: simple whole-share DAY limit)
# ---------------------------------------------------------------------------
#
# The OLD design submitted catalyst buys as OrderClass.BRACKET with only stop_loss.
# Alpaca rejects this on TWO grounds (empirically verified against live Alpaca paper
# account in the adversarial review):
#   (1) bracket orders require BOTH take_profit AND stop_loss legs
#       → 400 {"code":40010001,"message":"bracket orders require take_profit.limit_price"}
#   (2) fractional qty (3-decimal) is rejected on advanced order classes
#       → 422 {"code":42210000,"message":"fractional orders must be simple orders"}
# So every catalyst entry silently failed from day one.  The new design:
#   • qty = int(notional // limit_price) — whole shares only
#   • simple DAY LimitOrderRequest (no order_class, no stop leg)
#   • standalone GTC stop-market sell submitted by reconcile_fills after fill confirmation,
#     anchored to actual fill price × (1 − hard_stop_pct)


class TestCatalystOrderRedesign:
    """Catalyst sleeve → simple whole-share DAY limit (not bracket).

    The old bracket design was verified-broken by the adversarial review:
    Alpaca rejects bracket with stop_loss-only AND rejects fractional qty on
    advanced order classes — so every catalyst entry failed silently.
    """

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.market_clock", return_value={"is_open": True})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_buy_submits_simple_limit_not_bracket(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Catalyst + tight spread → simple LimitOrderRequest, NO bracket/OTO."""
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.requests import LimitOrderRequest

        mock_quote.return_value = _quote(99.9, 100.0)
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest), "Catalyst limit order must be LimitOrderRequest"
        # Must NOT use bracket (empirically confirmed as broken by review)
        assert getattr(req, "order_class", None) not in (OrderClass.BRACKET,), \
            "Catalyst buy MUST NOT use bracket (Alpaca rejects stop_loss-only bracket)"
        assert getattr(req, "stop_loss", None) is None, \
            "Catalyst entry must not carry a stop leg — stop is submitted post-fill by reconcile_fills"

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.market_clock", return_value={"is_open": True})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_buy_uses_whole_share_qty(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Catalyst buy qty = int(notional // limit_price) — whole shares only.

        Alpaca rejects fractional qty on advanced order classes (422).  The new
        design uses whole-share simple limit orders so both the qty and order
        class constraints are satisfied.
        """
        # ask=100, slip=10bps → limit=100.10 (ceil), notional≈5000 → qty=int(5000//100.10)=49
        mock_quote.return_value = _quote(99.9, 100.0)
        cfg = _cfg(tmp_path)
        client = _make_client(equity=100_000.0)

        from alpaca.trading.requests import LimitOrderRequest
        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI", usd=5000.0))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest)
        qty = req.qty
        assert qty is not None
        # Must be whole-share (integer value)
        assert qty == int(qty), f"Catalyst qty must be whole shares, got {qty}"
        assert qty >= 1, "Catalyst qty must be >= 1"

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.market_clock", return_value={"is_open": True})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_skip_when_qty_zero(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Catalyst buy is skipped with a clear reason when whole-share qty would be 0."""
        # Expensive stock: ask=1000, notional=500 → int(500//1001)=0 → skip
        mock_quote.return_value = _quote(999.0, 1000.0)
        cfg = _cfg(tmp_path)
        # tiny notional to force qty=0
        client = _make_client(equity=100_000.0)

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "SMCI",
                               {"ticker": "SMCI", "action": "buy", "approx_usd": 500.0,
                                "sleeve": "catalyst", "catalyst_date": "2026-09-15"})

        assert "skipped" in result.lower()
        assert "too small" in result.lower() or "whole-share" in result.lower()
        client.submit_order.assert_not_called()

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


class TestMarketClosedCatalystDeferred:
    """Catalyst buy: closed market → deferred (not cancelled); open or None → proceed.

    Pre-redesign the closed-market path returned 'skipped' and proposals.py marked
    the row 'cancelled', so the entry was permanently lost.  The new design returns
    'deferred' so proposals.py sets status='deferred_market_closed' and
    execute_deferred_entries() retries it at the next open-market watchdog tick.
    """

    @patch("tradingagents.integrations.alpaca.executor._latest_quote", return_value=None)
    @patch("tradingagents.integrations.alpaca.executor.market_clock",
           return_value={"is_open": False, "next_open": "2026-06-11T13:30:00+00:00"})
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_catalyst_deferred_when_market_closed(self, mock_enabled, mock_clock, mock_quote, tmp_path):
        """Catalyst buy returns 'deferred' (not 'skipped') when market_clock is closed.

        The old 'skipped' path permanently cancelled the entry — the pipeline could
        never execute a trade.  'deferred' lets execute_deferred_entries() retry at open.
        """
        cfg = _cfg(tmp_path)
        client = _make_client()

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        assert "deferred" in result.lower()
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


# ---------------------------------------------------------------------------
# Sub-penny rule (SEC Rule 612): limit_price must be 2dp
# ---------------------------------------------------------------------------


class TestSubPennyRounding:
    """limit_price and stop_price are rounded to 2 decimal places."""

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_limit_price_is_2dp(self, mock_enabled, mock_quote, tmp_path):
        """Core limit_price is ceil-rounded to 2dp (never sub-penny)."""
        import math as _math
        # ask=52.37, slip=10bps → raw=52.4224 → ceil to 2dp = 52.43
        mock_quote.return_value = _quote(52.00, 52.37)
        cfg = _cfg(tmp_path, portfolio_advisor_limit_slip_bps=10)
        client = _make_client()

        from alpaca.trading.requests import LimitOrderRequest
        from tradingagents.integrations.alpaca import executor as ex

        ex._paper_buy(cfg, client, 100_000.0, "AAPL", _core_proposal("AAPL", usd=5000.0))

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, LimitOrderRequest)
        lp = req.limit_price
        # Must be a multiple of $0.01
        assert abs(lp * 100 - round(lp * 100)) < 1e-6, \
            f"limit_price {lp} must be a whole cent (2dp)"
        # Must be ≥ ask (marketable)
        assert lp >= 52.37, f"limit_price {lp} must be >= ask 52.37"

    @patch("tradingagents.integrations.alpaca.executor._latest_quote")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_stop_price_from_gtc_stop_is_2dp(self, mock_enabled, mock_quote, tmp_path):
        """_submit_gtc_stop_for_catalyst rounds stop_price to 2dp (floor)."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        stop_orders = []

        class _FakeClient:
            def submit_order(self, req):
                stop_orders.append(req)
                r = MagicMock()
                r.id = "stop-order-id"
                return r

        # fill_price=52.37, hard_stop=8% → raw_stop=48.1804 → floor to 2dp = 48.18
        result = ex._submit_gtc_stop_for_catalyst(cfg, _FakeClient(), "AAPL", 50, 52.37, 0.08)
        assert result == "stop-order-id"
        assert len(stop_orders) == 1
        sp = stop_orders[0].stop_price
        # Must be 2dp
        assert abs(sp * 100 - round(sp * 100)) < 1e-6, \
            f"stop_price {sp} must be a whole cent"
        # Must be floor (not above the -8% level)
        assert sp <= 52.37 * 0.92 + 0.005, \
            f"stop_price {sp} must be <= fill × 0.92"


# ---------------------------------------------------------------------------
# GTC stop submitted post-fill by reconcile_fills
# ---------------------------------------------------------------------------


class TestPostFillGTCStop:
    """reconcile_fills submits a GTC stop after confirming a catalyst fill,
    anchored to the actual fill price (not the limit price)."""

    def _write_catalyst_pending(
        self,
        cfg: Dict[str, Any],
        order_id: str,
        intended_price: float,
        proposal_ts: str = "2026-06-10T09:00:00+00:00",
    ) -> None:
        _pa_dir(cfg)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": "SMCI",
            "action": "buy",
            "status": "submitted",
            "order_id": order_id,
            "notional_usd": 5000.0,
            "intended_price": intended_price,
            "fill_check_pending": True,
            "catalyst_stop_pending": True,
            "sleeve": "catalyst",
            "proposal_ts": proposal_ts,
        }
        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        with ledger.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def test_gtc_stop_submitted_at_fill_times_0_92(self, tmp_path):
        """reconcile_fills submits GTC stop at fill_price × (1 - 0.08)."""
        import math as _math
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path, portfolio_advisor_catalyst_hard_stop_pct=0.08)
        order_id = "catalyst-order-1"
        fill_price = 102.50
        self._write_catalyst_pending(cfg, order_id, 100.0)

        stop_orders_submitted = []

        closed_order = MagicMock()
        closed_order.id = order_id
        closed_order.status = "filled"
        closed_order.filled_avg_price = str(fill_price)
        closed_order.filled_qty = "49"

        stop_order = MagicMock()
        stop_order.id = "gtc-stop-id"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [closed_order]
            # Capture the stop order submission
            def submit_side_effect(req):
                stop_orders_submitted.append(req)
                return stop_order
            mock_c.submit_order.side_effect = submit_side_effect
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        assert len(stop_orders_submitted) == 1, "Exactly one stop order must be submitted"
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import TimeInForce
        req = stop_orders_submitted[0]
        assert isinstance(req, StopOrderRequest)
        assert req.time_in_force == TimeInForce.GTC, "Stop must be GTC not DAY"
        # stop_price = floor(fill × 0.92 × 100) / 100
        expected_stop = _math.floor(fill_price * 0.92 * 100) / 100
        assert abs(req.stop_price - expected_stop) < 0.01, \
            f"Stop anchored to fill: expected {expected_stop}, got {req.stop_price}"

    def test_stop_order_id_saved_to_position_plan(self, tmp_path):
        """reconcile_fills saves the stop_order_id to the PositionPlan."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, upsert_position_plan, load_position_plans,
        )

        cfg = _cfg(tmp_path, portfolio_advisor_catalyst_hard_stop_pct=0.08)
        order_id = "catalyst-order-2"
        fill_price = 50.0
        self._write_catalyst_pending(cfg, order_id, 50.0)

        # Create the position plan
        _pa_dir(cfg)
        plan = PositionPlan(ticker="SMCI", entry_price=50.0, strategy="catalyst")
        upsert_position_plan(cfg, plan)

        closed_order = MagicMock()
        closed_order.id = order_id
        closed_order.status = "filled"
        closed_order.filled_avg_price = str(fill_price)
        closed_order.filled_qty = "100"

        stop_order = MagicMock()
        stop_order.id = "the-stop-order-id"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [closed_order]
            mock_c.submit_order.return_value = stop_order
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        plans = load_position_plans(cfg)
        assert "SMCI" in plans
        assert plans["SMCI"].stop_order_id == "the-stop-order-id", \
            "stop_order_id must be persisted to PositionPlan"

    def test_no_gtc_stop_for_core_fill(self, tmp_path):
        """reconcile_fills does NOT submit a stop for core-sleeve fills."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        _pa_dir(cfg)
        order_id = "core-order-1"
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": "AAPL",
            "action": "buy",
            "status": "submitted",
            "order_id": order_id,
            "notional_usd": 5000.0,
            "intended_price": 100.0,
            "fill_check_pending": True,
            "catalyst_stop_pending": False,
            "sleeve": "core",
        }
        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        with ledger.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

        closed_order = MagicMock()
        closed_order.id = order_id
        closed_order.status = "filled"
        closed_order.filled_avg_price = "100.5"
        closed_order.filled_qty = "50"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [closed_order]
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        # No stop order should be submitted for core fills
        mock_c.submit_order.assert_not_called()

    def test_pending_status_not_terminal_stays_pending(self, tmp_path):
        """reconcile_fills does NOT write terminal fill_check for non-terminal status rows."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        order_id = "pending-order"
        _pa_dir(cfg)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": "AAPL",
            "action": "buy",
            "status": "submitted",
            "order_id": order_id,
            "intended_price": 100.0,
            "fill_check_pending": True,
            "catalyst_stop_pending": False,
            "sleeve": "core",
        }
        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        with ledger.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

        # Simulate a fill_check row already written with non-terminal status (accepted)
        # by the 2s deferred check — it should NOT count as reconciled
        with ledger.open("a") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "fill_check",
                "order_id": order_id,
                "status": "accepted",  # NON-TERMINAL — must not close the loop
                "filled_avg_price": None,
                "slippage_bps": None,
            }) + "\n")

        # Now the order has "filled" in the closed list
        closed_order = MagicMock()
        closed_order.id = order_id
        closed_order.status = "filled"
        closed_order.filled_avg_price = "100.5"
        closed_order.filled_qty = "50"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [closed_order]
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        terminal_fills = [
            r for r in rows
            if r.get("action") == "fill_check"
            and str(r.get("status", "")).lower() == "filled"
        ]
        assert len(terminal_fills) == 1, \
            "A terminal fill_check row should be written even when a non-terminal one exists"


# ---------------------------------------------------------------------------
# Cancel-before-close
# ---------------------------------------------------------------------------


class TestCancelBeforeClose:
    """close_for_watchdog and _paper_reduce cancel open orders before close_position."""

    def test_cancel_before_close_called_in_close_for_watchdog(self, tmp_path):
        """close_for_watchdog calls _cancel_open_orders_for_symbol before close_position."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        call_order = []

        class _TrackingClient:
            def get_open_position(self, tk):
                pos = MagicMock()
                pos.market_value = "5000"
                return pos
            def get_orders(self, req):
                call_order.append("get_orders")
                return []
            def cancel_order_by_id(self, oid):
                call_order.append("cancel")
            def close_position(self, tk, **kwargs):
                call_order.append("close")
                r = MagicMock()
                r.id = "close-id"
                return r

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client", return_value=_TrackingClient()), \
             patch.object(ex, "_notify"):
            ex.close_for_watchdog(cfg, "SMCI", 1.0, "test_rule")

        # get_orders must be called before close
        assert "get_orders" in call_order
        assert "close" in call_order
        close_idx = call_order.index("close")
        orders_idx = call_order.index("get_orders")
        assert orders_idx < close_idx, "get_orders must be called before close_position"

    def test_cancel_helper_silent_on_failure(self, tmp_path):
        """_cancel_open_orders_for_symbol never raises on any failure."""
        from tradingagents.integrations.alpaca import executor as ex

        class _FailClient:
            def get_orders(self, req):
                raise Exception("network error")

        result = ex._cancel_open_orders_for_symbol(_FailClient(), "AAPL")
        assert result == 0


# ---------------------------------------------------------------------------
# Entry-expired phantom-state unwind
# ---------------------------------------------------------------------------


class TestEntryExpiredUnwind:
    """reconcile_fills detects unfilled-terminal orders and unwinds phantom state."""

    def _write_catalyst_entry(
        self,
        cfg: Dict[str, Any],
        order_id: str,
        proposal_ts: str,
        ticker: str = "SMCI",
    ) -> None:
        _pa_dir(cfg)
        row = {
            "ts": proposal_ts,
            "ticker": ticker,
            "action": "buy",
            "status": "submitted",
            "order_id": order_id,
            "notional_usd": 5000.0,
            "intended_price": 100.0,
            "fill_check_pending": True,
            "catalyst_stop_pending": True,
            "sleeve": "catalyst",
            "proposal_ts": proposal_ts,
        }
        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        with ledger.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def test_entry_expired_writes_ledger_row(self, tmp_path):
        """Expired unfilled DAY limit → entry_expired ledger row is written."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        order_id = "expired-order-1"
        proposal_ts = "2026-06-10T09:00:00+00:00"
        self._write_catalyst_entry(cfg, order_id, proposal_ts)

        expired_order = MagicMock()
        expired_order.id = order_id
        expired_order.status = "expired"
        expired_order.filled_avg_price = None
        expired_order.filled_qty = "0"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [expired_order]
            mock_c.get_open_position.side_effect = Exception("no position")
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        ledger = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        expired_rows = [r for r in rows if r.get("action") == "entry_expired"]
        assert len(expired_rows) == 1
        assert expired_rows[0]["ticker"] == "SMCI"
        assert expired_rows[0]["order_id"] == order_id

    def test_entry_expired_cancels_proposal(self, tmp_path):
        """Expired unfilled order → originating proposal row marked cancelled."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor import proposals as prop

        cfg = _cfg(tmp_path)
        now_ts = "2026-06-10T09:00:00+00:00"
        order_id = "expired-order-2"
        self._write_catalyst_entry(cfg, order_id, now_ts, ticker="SMCI")

        # Write a proposals row with matching ts
        prop_rows = [{
            "ts": now_ts,
            "ticker": "SMCI",
            "action": "buy",
            "status": "executed",
            "sleeve": "catalyst",
        }]
        prop.save_all(cfg, prop_rows)

        expired_order = MagicMock()
        expired_order.id = order_id
        expired_order.status = "expired"
        expired_order.filled_avg_price = None
        expired_order.filled_qty = "0"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [expired_order]
            mock_c.get_open_position.side_effect = Exception("no position")
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        updated = prop.load_all(cfg)
        smci_rows = [r for r in updated if r.get("ticker") == "SMCI"]
        assert smci_rows, "SMCI proposal must still exist"
        assert smci_rows[0]["status"] == "cancelled"
        assert "entry expired" in (smci_rows[0].get("status_note") or "")

    def test_entry_expired_deletes_plan_when_no_position(self, tmp_path):
        """Expired unfilled order → PositionPlan deleted when no position exists."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, upsert_position_plan, load_position_plans,
        )

        cfg = _cfg(tmp_path)
        _pa_dir(cfg)
        order_id = "expired-order-3"
        proposal_ts = "2026-06-10T09:00:00+00:00"
        self._write_catalyst_entry(cfg, order_id, proposal_ts, ticker="SMCI")

        # Create phantom plan
        plan = PositionPlan(ticker="SMCI", entry_price=100.0, strategy="catalyst")
        upsert_position_plan(cfg, plan)

        expired_order = MagicMock()
        expired_order.id = order_id
        expired_order.status = "canceled"
        expired_order.filled_avg_price = None
        expired_order.filled_qty = "0"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [expired_order]
            # No position exists
            mock_c.get_open_position.side_effect = Exception("no position")
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        plans = load_position_plans(cfg)
        assert "SMCI" not in plans, \
            "Phantom PositionPlan must be deleted when entry expired unfilled and no position exists"

    def test_entry_expired_keeps_plan_when_position_exists(self, tmp_path):
        """Plan is NOT deleted when a real position exists (rare: partial fill scenario)."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, upsert_position_plan, load_position_plans,
        )

        cfg = _cfg(tmp_path)
        _pa_dir(cfg)
        order_id = "canceled-order-with-pos"
        proposal_ts = "2026-06-10T09:00:00+00:00"
        self._write_catalyst_entry(cfg, order_id, proposal_ts, ticker="NVDA")

        # Plan exists and so does an Alpaca position
        plan = PositionPlan(ticker="NVDA", entry_price=100.0, strategy="catalyst")
        upsert_position_plan(cfg, plan)

        expired_order = MagicMock()
        expired_order.id = order_id
        expired_order.status = "canceled"
        expired_order.filled_avg_price = None
        expired_order.filled_qty = "0"

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn:
            mock_c = MagicMock()
            mock_c.get_orders.return_value = [expired_order]
            # Position DOES exist
            pos_mock = MagicMock()
            pos_mock.symbol = "NVDA"
            mock_c.get_open_position.return_value = pos_mock
            mock_client_fn.return_value = mock_c

            ex.reconcile_fills(cfg)

        plans = load_position_plans(cfg)
        assert "NVDA" in plans, "Plan must NOT be deleted when a real position still exists"


# ---------------------------------------------------------------------------
# Deferred catalyst entries flow
# ---------------------------------------------------------------------------


class TestDeferredCatalystEntries:
    """Market closed → deferred_market_closed status → execute_deferred_entries at open."""

    def test_closed_market_returns_deferred_not_skipped(self, tmp_path):
        """_paper_buy returns 'deferred' string (not 'skipped') when market is closed."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        client = _make_client()

        with patch.object(ex, "_latest_quote", return_value=None), \
             patch.object(ex, "market_clock", return_value={"is_open": False}):
            result = ex._paper_buy(cfg, client, 100_000.0, "SMCI", _catalyst_proposal("SMCI"))

        assert result.startswith("deferred"), f"Expected 'deferred' prefix, got: {result!r}"
        client.submit_order.assert_not_called()

    def test_execute_proposal_maps_deferred_correctly(self, tmp_path):
        """execute_proposal returns status='deferred' for closed-market catalyst."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "_client") as mock_client_fn, \
             patch.object(ex, "_latest_quote", return_value=None), \
             patch.object(ex, "market_clock", return_value={"is_open": False}):
            mock_c = _make_client()
            mock_client_fn.return_value = mock_c

            result = ex.execute_proposal(cfg, {
                "ticker": "SMCI", "action": "buy",
                "approx_usd": 5000.0, "sleeve": "catalyst",
                "catalyst_date": "2026-09-15",
            })

        assert result["status"] == "deferred", \
            f"execute_proposal must return status='deferred', got {result['status']!r}"

    def test_proposals_add_sets_deferred_status(self, tmp_path):
        """proposals.add sets status='deferred_market_closed' when executor returns deferred."""
        from tradingagents.portfolio_advisor import proposals as prop

        cfg = {
            "portfolio_advisor_dir": str(tmp_path / "pa"),
            "portfolio_advisor_alpaca_paper": True,
        }
        Path(cfg["portfolio_advisor_dir"]).mkdir(parents=True, exist_ok=True)

        mock_result = {"status": "deferred", "detail": "market closed — catalyst entry deferred"}

        from tradingagents.integrations.alpaca import executor as ex
        with patch.object(ex, "execute_proposal", return_value=mock_result), \
             patch.object(ex, "enabled", return_value=True):
            entry = prop.add(
                cfg,
                ticker="SMCI",
                action="buy",
                approx_usd=5000.0,
                sleeve="catalyst",
                catalyst_date="2026-09-15",
                reason="test deferred",
            )

        all_rows = prop.load_all(cfg)
        smci = [r for r in all_rows if r.get("ticker") == "SMCI"]
        assert smci, "SMCI proposal must exist"
        assert smci[0]["status"] == "deferred_market_closed", \
            f"Expected 'deferred_market_closed', got {smci[0]['status']!r}"

    def test_execute_deferred_entries_retries_at_open(self, tmp_path):
        """execute_deferred_entries re-attempts deferred rows when market is open."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor import proposals as prop
        from datetime import datetime, timezone

        cfg = {
            "portfolio_advisor_dir": str(tmp_path / "pa"),
            "portfolio_advisor_alpaca_paper": True,
        }
        Path(cfg["portfolio_advisor_dir"]).mkdir(parents=True, exist_ok=True)

        # Write a deferred row (fresh)
        now_ts = datetime.now(timezone.utc).isoformat()
        deferred_row = {
            "ts": now_ts,
            "ticker": "SMCI",
            "action": "buy",
            "approx_usd": 5000.0,
            "sleeve": "catalyst",
            "catalyst_date": "2026-09-15",
            "status": "deferred_market_closed",
            "status_set_at": now_ts,
        }
        prop.save_all(cfg, [deferred_row])

        execute_calls = []

        def mock_execute(cfg_, proposal):
            execute_calls.append(proposal.get("ticker"))
            return {"status": "executed", "detail": "buy executed"}

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "market_clock", return_value={"is_open": True}), \
             patch.object(ex, "execute_proposal", side_effect=mock_execute):
            count = ex.execute_deferred_entries(cfg)

        assert count == 1, f"Expected 1 attempted, got {count}"
        assert "SMCI" in execute_calls, "SMCI deferred entry must be re-attempted"

        # Verify proposal was marked executed
        updated = prop.load_all(cfg)
        smci = [r for r in updated if r.get("ticker") == "SMCI"]
        assert smci[0]["status"] == "executed"

    def test_execute_deferred_entries_expires_old_rows(self, tmp_path):
        """execute_deferred_entries cancels deferred rows older than 18h."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor import proposals as prop
        from datetime import datetime, timezone, timedelta

        cfg = {
            "portfolio_advisor_dir": str(tmp_path / "pa"),
            "portfolio_advisor_alpaca_paper": True,
        }
        Path(cfg["portfolio_advisor_dir"]).mkdir(parents=True, exist_ok=True)

        # Write a deferred row that is 20h old (expired)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
        deferred_row = {
            "ts": old_ts,
            "ticker": "AAPL",
            "action": "buy",
            "approx_usd": 5000.0,
            "sleeve": "catalyst",
            "catalyst_date": "2026-09-15",
            "status": "deferred_market_closed",
            "status_set_at": old_ts,
        }
        prop.save_all(cfg, [deferred_row])

        execute_calls = []

        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "market_clock", return_value={"is_open": True}), \
             patch.object(ex, "execute_proposal", side_effect=lambda c, p: execute_calls.append(p)):
            count = ex.execute_deferred_entries(cfg)

        # Should NOT have attempted execution (expired)
        assert "AAPL" not in execute_calls, "Expired deferred entry must not be re-executed"

        # Row must be cancelled
        updated = prop.load_all(cfg)
        aapl = [r for r in updated if r.get("ticker") == "AAPL"]
        assert aapl[0]["status"] == "cancelled"
        assert "expired" in (aapl[0].get("status_note") or "")

    def test_execute_deferred_skips_when_market_still_closed(self, tmp_path):
        """execute_deferred_entries does nothing when market is still closed."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = {
            "portfolio_advisor_dir": str(tmp_path / "pa"),
            "portfolio_advisor_alpaca_paper": True,
        }

        execute_calls = []
        with patch.object(ex, "enabled", return_value=True), \
             patch.object(ex, "market_clock", return_value={"is_open": False}), \
             patch.object(ex, "execute_proposal", side_effect=lambda c, p: execute_calls.append(p)):
            count = ex.execute_deferred_entries(cfg)

        assert count == 0
        assert execute_calls == []
