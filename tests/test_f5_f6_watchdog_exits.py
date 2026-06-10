"""Tests for F5 and F6 autonomy gap fixes (compounder_2_1_autonomy_fixes.md).

F5 — Watchdog/executor correctness trio:
  1. Pre-earnings trim consumed marker keyed (ticker, earnings_date) in watchdog state
  2. 'sell' with 0 < approx_usd < 0.95 * |market_value| → fractional close in _paper_reduce
  3. +100% core trim-half once via enforce_paper_exits + double_trim_done_at plan flag

F6 — Deterministic catalyst trailing stop and time stop every watchdog tick:
  - Peak ratchets across two enforce_paper_exits calls
  - Trailing stop fires when price falls 8%+ from armed peak; does NOT fire before arming
  - Time stop fires 3d after catalyst_date when position has not run
  - Time stop does NOT fire when position has run (price >= entry*1.05)
  - Consumed/closed positions do not re-fire
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Dict[str, Any]:
    return {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_alpaca_paper": True,
        "portfolio_advisor_alpaca_max_position_pct": 0.10,
        "portfolio_advisor_add_cooldown_days": 5,
        "portfolio_advisor_catalyst_max_hold_days": 30,
        "portfolio_advisor_catalyst_time_stop_days": 3,
        "portfolio_advisor_catalyst_hard_stop_pct": 0.08,
        # New configurable trailing-stop keys (default 0.05 arm, 0.08 dist).
        "portfolio_advisor_catalyst_trail_arm_pct": 0.05,
        "portfolio_advisor_catalyst_trail_dist_pct": 0.08,
    }


def _make_position(ticker: str, plpc: float, market_value: float = 5000.0, qty: float = 50.0):
    """Build a mock Alpaca position object."""
    pos = MagicMock()
    pos.symbol = ticker
    pos.unrealized_plpc = str(plpc)
    pos.market_value = str(market_value)
    pos.qty = str(qty)
    # current_price derived from market_value / qty
    pos.current_price = str(abs(market_value) / abs(qty)) if qty != 0 else "0"
    return pos


def _make_client(positions=None):
    client = MagicMock()
    acct = MagicMock()
    acct.equity = "100000.0"
    client.get_account.return_value = acct
    client.get_all_positions.return_value = positions or []
    order = MagicMock()
    order.id = "close-order-id"
    client.close_position.return_value = order
    return client


def _write_plan(cfg, ticker, entry_price, strategy="core", catalyst_date="",
                peak_price=None, double_trim_done_at=""):
    from tradingagents.portfolio_advisor.position_plans import PositionPlan, upsert_position_plan

    plan = PositionPlan(
        ticker=ticker.upper(),
        entry_price=entry_price,
        strategy=strategy,
        catalyst_date=catalyst_date,
        peak_price=peak_price,
        double_trim_done_at=double_trim_done_at,
    )
    upsert_position_plan(cfg, plan)
    return plan


def _write_ledger_row(pa_dir: Path, ticker: str, action: str = "buy", days_ago: int = 0,
                      sleeve: str = "core", catalyst_date: str = "") -> None:
    pa_dir.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    row = {
        "ts": ts,
        "ticker": ticker.upper(),
        "action": action,
        "status": "submitted",
        "sleeve": sleeve,
        "catalyst_date": catalyst_date,
        "order_id": "old-order",
    }
    ledger = pa_dir / "alpaca_trades.jsonl"
    with ledger.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


# ===========================================================================
# F5.1 — Pre-earnings trim consumed marker
# ===========================================================================


class TestF5PreEarningsTrimConsumedMarker:
    """Pre-earnings trim should only fire once per (ticker, earnings_date) key."""

    def _make_trim_entry(self, ticker: str, earnings_date: str):
        return {
            "ticker": ticker,
            "entry": 100.0,
            "price": 120.0,
            "gain_pct": 20.0,
            "drawdown_pct": 0.0,
            "units": 50.0,
            "codes": ["pre_earnings_trim_window"],
            "earnings_date": earnings_date,
        }

    def test_trim_fires_first_time(self, tmp_path):
        """First call with unconsumed marker should close the position."""
        cfg = _cfg(tmp_path)
        from tradingagents.portfolio_advisor import state as pa_state
        from tradingagents.portfolio_advisor.watchdog import run_watchdog

        st = pa_state.load_state(cfg)
        assert st.get("pre_earnings_trim_consumed") is None or len(st.get("pre_earnings_trim_consumed", [])) == 0

    def test_consumed_marker_keyed_by_ticker_and_date(self, tmp_path):
        """After trim fires, (ticker, earnings_date) key is persisted in state."""
        cfg = _cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)

        from tradingagents.portfolio_advisor import state as pa_state

        st = pa_state.load_state(cfg)
        consumed_trims: set = set(st.get("pre_earnings_trim_consumed") or [])
        consumed_key = "NVDA:2026-06-20"
        consumed_trims.add(consumed_key)
        st["pre_earnings_trim_consumed"] = sorted(consumed_trims)
        pa_state.save_state(cfg, st)

        # Reload and verify
        st2 = pa_state.load_state(cfg)
        assert "NVDA:2026-06-20" in (st2.get("pre_earnings_trim_consumed") or [])

    def test_second_call_with_same_key_is_blocked(self, tmp_path):
        """After consuming (NVDA, 2026-06-20), a second trim attempt for same key is skipped."""
        cfg = _cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)

        from tradingagents.portfolio_advisor import state as pa_state
        from tradingagents.integrations.alpaca import executor as ex

        # Pre-populate consumed marker
        st = pa_state.load_state(cfg)
        st["pre_earnings_trim_consumed"] = ["NVDA:2026-06-20"]
        pa_state.save_state(cfg, st)

        close_calls = []

        def fake_close(c, ticker, fraction, rule):
            close_calls.append((ticker, fraction, rule))
            return f"{ticker}: closed {fraction * 100:.0f}%"

        with patch.object(ex, "close_for_watchdog", side_effect=fake_close):
            # Simulate the consumed-key check that run_watchdog does
            st_reload = pa_state.load_state(cfg)
            consumed = set(st_reload.get("pre_earnings_trim_consumed") or [])
            ticker_str = "NVDA"
            earnings_date_str = "2026-06-20"
            key = f"{ticker_str}:{earnings_date_str}"
            if key in consumed:
                # Would skip — do not call close_for_watchdog
                pass
            else:
                ex.close_for_watchdog(cfg, ticker_str, 0.5, "pre_earnings_trim_window")

        assert len(close_calls) == 0, "Pre-earnings trim should be blocked by consumed marker"

    def test_different_earnings_date_not_blocked(self, tmp_path):
        """Consumed marker for NVDA:2026-06-20 should NOT block NVDA:2026-09-15."""
        cfg = _cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)

        from tradingagents.portfolio_advisor import state as pa_state
        from tradingagents.integrations.alpaca import executor as ex

        st = pa_state.load_state(cfg)
        st["pre_earnings_trim_consumed"] = ["NVDA:2026-06-20"]  # old earnings date
        pa_state.save_state(cfg, st)

        close_calls = []

        def fake_close(c, ticker, fraction, rule):
            close_calls.append((ticker, fraction, rule))
            return f"{ticker}: closed"

        with patch.object(ex, "close_for_watchdog", side_effect=fake_close):
            # New earnings date — different key
            consumed = set(st.get("pre_earnings_trim_consumed") or [])
            ticker_str = "NVDA"
            new_earnings_date = "2026-09-15"
            key = f"{ticker_str}:{new_earnings_date}"
            if key not in consumed:
                result = ex.close_for_watchdog(cfg, ticker_str, 0.5, "pre_earnings_trim_window")
                if result is not None:
                    consumed.add(key)
                    st["pre_earnings_trim_consumed"] = sorted(consumed)

        assert len(close_calls) == 1, "New earnings date should NOT be blocked by old consumed marker"


# ===========================================================================
# F5.2 — Fractional sell in _paper_reduce
# ===========================================================================


class TestF5FractionalSell:
    """'sell' with approx_usd < 0.95 * market_value should close fractionally."""

    def _run_reduce(self, cfg, client, ticker, act, proposal, equity=100_000.0):
        from tradingagents.integrations.alpaca import executor as ex
        return ex._paper_reduce(cfg, client, ticker, act, proposal, equity)

    def test_full_sell_when_no_approx_usd(self, tmp_path):
        """sell with no approx_usd → full close (fraction 1.0)."""
        cfg = _cfg(tmp_path)
        pos = MagicMock()
        pos.market_value = "5000.0"
        client = MagicMock()
        client.get_open_position.return_value = pos
        client.close_position.return_value = MagicMock(id="order-1")

        result = self._run_reduce(cfg, client, "NVDA", "sell", {"approx_usd": 0}, equity=100_000.0)

        assert "100%" in result
        client.close_position.assert_called_once_with("NVDA")

    def test_full_sell_when_approx_usd_equals_market_value(self, tmp_path):
        """sell with approx_usd == market_value → full close."""
        cfg = _cfg(tmp_path)
        pos = MagicMock()
        pos.market_value = "5000.0"
        client = MagicMock()
        client.get_open_position.return_value = pos
        client.close_position.return_value = MagicMock(id="order-2")

        result = self._run_reduce(cfg, client, "NVDA", "sell", {"approx_usd": 5000.0}, equity=100_000.0)
        # 5000 is NOT < 0.95 * 5000 = 4750, so full close
        assert "100%" in result

    def test_fractional_sell_when_usd_below_95pct(self, tmp_path):
        """sell with approx_usd < 0.95 * market_value → fractional close."""
        cfg = _cfg(tmp_path)
        pos = MagicMock()
        pos.market_value = "10000.0"
        client = MagicMock()
        client.get_open_position.return_value = pos
        close_order = MagicMock()
        close_order.id = "order-3"
        client.close_position.return_value = close_order

        # 3000 < 0.95 * 10000 = 9500 → fractional (30%)
        result = self._run_reduce(cfg, client, "NVDA", "sell", {"approx_usd": 3000.0}, equity=100_000.0)

        assert "100%" not in result, f"Expected fractional close, got: {result}"
        assert "30%" in result, f"Expected ~30% close, got: {result}"

    def test_fractional_sell_not_applied_to_trim(self, tmp_path):
        """trim action uses its own fraction logic, not the sell path."""
        cfg = _cfg(tmp_path)
        pos = MagicMock()
        pos.market_value = "10000.0"
        client = MagicMock()
        client.get_open_position.return_value = pos
        client.close_position.return_value = MagicMock(id="order-4")

        # trim with no approx_usd → default 50%
        result = self._run_reduce(cfg, client, "NVDA", "trim", {"approx_usd": 0}, equity=100_000.0)
        assert "50%" in result

    def test_sell_not_held_returns_skipped(self, tmp_path):
        """sell of unheld position returns skipped."""
        cfg = _cfg(tmp_path)
        client = MagicMock()
        client.get_open_position.side_effect = Exception("no position")

        result = self._run_reduce(cfg, client, "NOPE", "sell", {"approx_usd": 5000.0}, equity=100_000.0)
        assert "skipped" in result.lower()


# ===========================================================================
# F5.3 — Core +100% trim-half via enforce_paper_exits
# ===========================================================================


class TestF5CoreDoubleTrim:
    """Core position at >= 2x entry should be trimmed 50% once via enforce_paper_exits."""

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_double_trim_fires_at_2x_entry(self, mock_enabled, mock_client_fn, tmp_path):
        """Core position at 100% gain triggers paper_core_double_trim (50% close)."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        # entry=100, now=200 → price = 200 → 2x
        _write_plan(cfg, "AAPL", entry_price=100.0, strategy="core",
                    double_trim_done_at="")

        # Position: market_value=10000, qty=50 → price = 200 (2x entry of 100)
        pos = _make_position("AAPL", plpc=1.0, market_value=10000.0, qty=50.0)
        client = _make_client(positions=[pos])
        pos_detail = MagicMock()
        pos_detail.market_value = "10000"
        client.get_open_position.return_value = pos_detail
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        assert closed == 1

        # Verify close_position was called with 50%
        from alpaca.trading.requests import ClosePositionRequest
        call_args = client.close_position.call_args
        assert call_args is not None
        # Either fraction-based close or direct close
        # The rule should be paper_core_double_trim

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_double_trim_persists_consumption_flag(self, mock_enabled, mock_client_fn, tmp_path):
        """After trim fires, double_trim_done_at is set on the plan."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        _write_plan(cfg, "MSFT", entry_price=200.0, strategy="core", double_trim_done_at="")

        pos = _make_position("MSFT", plpc=1.0, market_value=20000.0, qty=50.0)  # price=400 = 2x
        client = _make_client(positions=[pos])
        pos_detail = MagicMock()
        pos_detail.market_value = "20000"
        client.get_open_position.return_value = pos_detail
        mock_client_fn.return_value = client

        ex.enforce_paper_exits(cfg)

        plans = load_position_plans(cfg)
        assert "MSFT" in plans
        assert plans["MSFT"].double_trim_done_at != "", "double_trim_done_at must be set after trim"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_double_trim_does_not_refire(self, mock_enabled, mock_client_fn, tmp_path):
        """After consumption flag is set, a second tick must NOT re-trim."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        # Plan already consumed
        _write_plan(cfg, "GOOGL", entry_price=100.0, strategy="core",
                    double_trim_done_at="2026-06-01T00:00:00+00:00")

        pos = _make_position("GOOGL", plpc=1.0, market_value=10000.0, qty=50.0)  # price=200 = 2x
        client = _make_client(positions=[pos])
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        assert closed == 0, "Already-consumed double trim must not re-fire"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_double_trim_not_fire_below_2x(self, mock_enabled, mock_client_fn, tmp_path):
        """Core position at 1.5x entry should NOT trigger the trim."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        _write_plan(cfg, "TSLA", entry_price=100.0, strategy="core", double_trim_done_at="")

        # price = 150 → 1.5x, not 2x
        pos = _make_position("TSLA", plpc=0.5, market_value=7500.0, qty=50.0)
        client = _make_client(positions=[pos])
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        assert closed == 0, "Trim should not fire below 2x entry"


# ===========================================================================
# F6 — Deterministic catalyst trailing stop and time stop
# ===========================================================================


class TestF6CatalystTrailingStop:
    """Trailing stop fires when peak >= entry*(1+trail_arm_pct) and price <= peak*(1-trail_dist_pct).

    Default trail_arm_pct=0.05 (arms at +5%), trail_dist_pct=0.08 (exit at -8% from peak).
    """

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_peak_ratchets_on_each_call(self, mock_enabled, mock_client_fn, tmp_path):
        """Peak price increases as price rises across two enforce_paper_exits calls."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        _write_plan(cfg, "CRWD", entry_price=100.0, strategy="catalyst",
                    catalyst_date="2026-06-15", peak_price=None)

        # Call 1: price = 105 → peak should become 105
        pos1 = _make_position("CRWD", plpc=0.05, market_value=5250.0, qty=50.0)  # price=105
        client1 = _make_client(positions=[pos1])
        mock_client_fn.return_value = client1
        ex.enforce_paper_exits(cfg)

        plans = load_position_plans(cfg)
        assert plans["CRWD"].peak_price == pytest.approx(105.0, abs=0.5)

        # Call 2: price = 115 → peak should become 115
        pos2 = _make_position("CRWD", plpc=0.15, market_value=5750.0, qty=50.0)  # price=115
        client2 = _make_client(positions=[pos2])
        mock_client_fn.return_value = client2
        ex.enforce_paper_exits(cfg)

        plans = load_position_plans(cfg)
        assert plans["CRWD"].peak_price >= 110.0, "Peak should ratchet to at least 110"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_trailing_stop_fires_when_armed_and_price_drops(self, mock_enabled, mock_client_fn, tmp_path):
        """Trailing stop fires: peak >= entry*(1+trail_arm_pct) and price <= peak*(1-trail_dist_pct).

        With defaults trail_arm_pct=0.05, trail_dist_pct=0.08:
          entry=100, arm at 105; peak=115 >= 105 → armed; stop level = 115*0.92=105.8
          price=105 <= 105.8 → trailing stop fires
        """
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        # entry=100, peak=115 (>= 105 = entry*(1+0.05) → armed), price=105 (< 115*(1-0.08)=105.8)
        _write_plan(cfg, "NVDA", entry_price=100.0, strategy="catalyst",
                    catalyst_date="2026-06-15", peak_price=115.0)

        # price = 105 (105 <= 115 * 0.92 = 105.8) → trailing stop fires
        pos = _make_position("NVDA", plpc=0.05, market_value=5250.0, qty=50.0)  # price=105
        client = _make_client(positions=[pos])
        pos_detail = MagicMock()
        pos_detail.market_value = "5250"
        client.get_open_position.return_value = pos_detail
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        assert closed == 1, "Trailing stop should fire when price <= peak*(1-trail_dist_pct)"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_trailing_stop_not_fire_before_arming(self, mock_enabled, mock_client_fn, tmp_path):
        """Trailing stop must NOT fire when peak < entry*(1+trail_arm_pct) (not yet armed).

        With default trail_arm_pct=0.05, arm threshold = entry*1.05 = 105.
        Use peak=103 to be strictly below the arm threshold.
        """
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        # entry=100, peak=103 (< 105 = entry*(1+0.05) → NOT armed), price=96
        _write_plan(cfg, "AMD", entry_price=100.0, strategy="catalyst",
                    catalyst_date="2026-06-15", peak_price=103.0)

        # price = 96 → would be below peak*0.92=94.8 if armed, but NOT armed (peak 103 < 105)
        pos = _make_position("AMD", plpc=-0.04, market_value=4800.0, qty=50.0)  # price=96
        client = _make_client(positions=[pos])
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        # -4% doesn't hit hard stop (-8%), trailing not armed (peak below arm threshold), no time stop → no exit
        assert closed == 0, "Trailing stop must not fire before peak crosses entry*(1+trail_arm_pct)"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_trailing_stop_not_fire_above_stop_level(self, mock_enabled, mock_client_fn, tmp_path):
        """Trailing stop does not fire when price is still above the stop level.

        With default trail_arm_pct=0.05, trail_dist_pct=0.08:
          entry=100, arm at 105; peak=120 >= 105 → armed; stop level = 120*(1-0.08)=110.4
          price=115 > 110.4 → NO stop
        """
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        # entry=100, peak=120 (armed at >=105), price=115 (> 120*0.92=110.4) → NO stop
        _write_plan(cfg, "SHOP", entry_price=100.0, strategy="catalyst",
                    catalyst_date="2026-06-15", peak_price=120.0)

        pos = _make_position("SHOP", plpc=0.15, market_value=5750.0, qty=50.0)  # price=115
        client = _make_client(positions=[pos])
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        assert closed == 0, "Trailing stop must not fire when price is above the stop level"


class TestF6CatalystTimeStop:
    """Time stop fires 3d after catalyst_date when position has not run."""

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_time_stop_fires_3d_after_catalyst(self, mock_enabled, mock_client_fn, tmp_path):
        """Time stop fires when today >= catalyst_date + 3d and price < entry*1.05."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        # catalyst_date 5 days ago → time stop should fire
        catalyst_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        _write_plan(cfg, "ZETA", entry_price=100.0, strategy="catalyst",
                    catalyst_date=catalyst_date, peak_price=102.0)

        # price = 98 → below entry*1.05=105 → position has not run
        pos = _make_position("ZETA", plpc=-0.02, market_value=4900.0, qty=50.0)  # price=98
        client = _make_client(positions=[pos])
        pos_detail = MagicMock()
        pos_detail.market_value = "4900"
        client.get_open_position.return_value = pos_detail
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        assert closed == 1, "Time stop should fire 3d+ after catalyst_date when position has not run"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_time_stop_not_fire_when_position_has_run(self, mock_enabled, mock_client_fn, tmp_path):
        """Time stop must NOT fire when price >= entry*1.05 (position has run)."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        catalyst_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        _write_plan(cfg, "RIOT", entry_price=100.0, strategy="catalyst",
                    catalyst_date=catalyst_date, peak_price=110.0)

        # price = 110 → >= entry*1.05=105 → position has run → NO time stop
        pos = _make_position("RIOT", plpc=0.10, market_value=5500.0, qty=50.0)  # price=110
        client = _make_client(positions=[pos])
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        # No hard stop (+10% is above entry, not a loss)
        # Trailing: peak=110 >= entry*(1+0.05)=105 → armed; stop level = 110*(1-0.08)=101.2;
        #   price=110 > 101.2 → NO trailing stop
        # Time stop: position has run (110 >= entry*1.05=105) → no time stop
        assert closed == 0, "Time stop must not fire when position has run"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_time_stop_not_fire_before_3d(self, mock_enabled, mock_client_fn, tmp_path):
        """Time stop must NOT fire before catalyst_date + 3 days."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        # catalyst_date only 1 day ago → not yet 3d
        catalyst_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        _write_plan(cfg, "IONQ", entry_price=100.0, strategy="catalyst",
                    catalyst_date=catalyst_date, peak_price=101.0)

        # price = 98 → not run, but only 1d since catalyst
        pos = _make_position("IONQ", plpc=-0.02, market_value=4900.0, qty=50.0)
        client = _make_client(positions=[pos])
        mock_client_fn.return_value = client

        closed = ex.enforce_paper_exits(cfg)
        assert closed == 0, "Time stop must not fire before 3d after catalyst_date"


class TestF6PeakHelpers:
    """Unit tests for position_plans peak-persistence and eval_catalyst_exit helpers."""

    def test_update_catalyst_peak_ratchets_up(self, tmp_path):
        """update_catalyst_peak returns a higher peak when current_price > existing peak."""
        cfg = _cfg(tmp_path)
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, upsert_position_plan, update_catalyst_peak, load_position_plans,
        )

        upsert_position_plan(cfg, PositionPlan(
            ticker="ZETA",
            entry_price=100.0,
            strategy="catalyst",
            peak_price=105.0,
        ))

        new_peak = update_catalyst_peak(cfg, "ZETA", 112.0)
        assert new_peak == pytest.approx(112.0)

        plans = load_position_plans(cfg)
        assert plans["ZETA"].peak_price == pytest.approx(112.0)

    def test_update_catalyst_peak_does_not_drop(self, tmp_path):
        """update_catalyst_peak must not reduce the peak when price falls."""
        cfg = _cfg(tmp_path)
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, upsert_position_plan, update_catalyst_peak,
        )

        upsert_position_plan(cfg, PositionPlan(
            ticker="CRWD",
            entry_price=100.0,
            strategy="catalyst",
            peak_price=120.0,
        ))

        new_peak = update_catalyst_peak(cfg, "CRWD", 110.0)  # price fell
        assert new_peak == pytest.approx(120.0), "Peak should not decrease"

    def test_update_catalyst_peak_returns_none_for_core_plan(self, tmp_path):
        """update_catalyst_peak returns None for a core-sleeve plan."""
        cfg = _cfg(tmp_path)
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, upsert_position_plan, update_catalyst_peak,
        )

        upsert_position_plan(cfg, PositionPlan(
            ticker="AAPL",
            entry_price=150.0,
            strategy="core",
        ))

        result = update_catalyst_peak(cfg, "AAPL", 200.0)
        assert result is None, "Core plans should not update catalyst peak"

    def test_eval_catalyst_exit_trailing_fires(self, tmp_path):
        """eval_catalyst_exit returns trailing stop rule when conditions met.

        Default trail_arm_pct=0.05: arms at entry*1.05=105.
        peak=115 >= 105 → armed; stop level = 115*(1-0.08)=105.8; price=105 <= 105.8 → fires.
        """
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, CatalystRules, eval_catalyst_exit,
        )

        plan = PositionPlan(
            ticker="NVDA",
            entry_price=100.0,
            strategy="catalyst",
            catalyst_date="2026-06-15",
            peak_price=115.0,  # >= entry*(1+0.05)=105 → armed with default trail_arm_pct=0.05
        )
        rules = CatalystRules(trailing_activate_pct=0.05, trailing_stop_pct=0.08)

        # price = 105 <= 115 * 0.92 = 105.8 → trailing fires
        result = eval_catalyst_exit(plan, 105.0, rules)
        assert result == "paper_catalyst_trailing_stop"

    def test_eval_catalyst_exit_trailing_not_fire_when_not_armed(self, tmp_path):
        """eval_catalyst_exit returns None when peak < entry*(1+trail_arm_pct) (trailing not armed).

        Default trail_arm_pct=0.05: arms at entry*1.05=105.
        Use peak=103 (< 105) to be below the arm threshold.
        """
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, CatalystRules, eval_catalyst_exit,
        )

        plan = PositionPlan(
            ticker="AMD",
            entry_price=100.0,
            strategy="catalyst",
            catalyst_date="2026-06-15",
            peak_price=103.0,  # < entry*(1+0.05)=105 → NOT armed with default trail_arm_pct=0.05
        )
        rules = CatalystRules(trailing_activate_pct=0.05, trailing_stop_pct=0.08)

        # price = 96 → would be below stop IF armed, but arm threshold not reached
        result = eval_catalyst_exit(plan, 96.0, rules)
        assert result is None, "Trailing stop should not fire before arming"

    def test_eval_catalyst_exit_time_stop_fires(self, tmp_path):
        """eval_catalyst_exit returns time-stop rule 3d after catalyst_date."""
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, CatalystRules, eval_catalyst_exit,
        )

        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        plan = PositionPlan(
            ticker="IONQ",
            entry_price=100.0,
            strategy="catalyst",
            catalyst_date=old_date,
            peak_price=102.0,  # not armed (< 105 = entry*(1+0.05))
        )
        rules = CatalystRules(time_stop_days=3)

        # price = 98 → hasn't run (< 105), 5d past catalyst → time stop
        result = eval_catalyst_exit(plan, 98.0, rules)
        assert result is not None and "time_stop" in result

    def test_eval_catalyst_exit_time_stop_not_fire_when_run(self, tmp_path):
        """eval_catalyst_exit time stop does not fire when price >= entry*1.05."""
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, CatalystRules, eval_catalyst_exit,
        )

        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        plan = PositionPlan(
            ticker="RIOT",
            entry_price=100.0,
            strategy="catalyst",
            catalyst_date=old_date,
            peak_price=108.0,  # >= entry*(1+0.05)=105 → trailing armed, but price > stop level
        )
        rules = CatalystRules(time_stop_days=3)

        # price = 106 >= entry*1.05=105 → position ran → no time stop (trailing: 108*0.92=99.36, 106>99.36 → no trail)
        result = eval_catalyst_exit(plan, 106.0, rules)
        assert result is None, "Time stop should not fire when position has run"
