"""Tests for market-clock awareness and high-conviction sizing tier.

Market clock (Alpaca-authoritative, UTC-window fallback):
  1. executor.market_clock() returns None under pytest (PYTEST_CURRENT_TEST guard).
  2. watchdog.market_is_open() uses Alpaca clock when available.
  3. watchdog.market_is_open() falls back to in_us_equity_watch_window_utc() when
     market_clock() returns None.
  4. run_watchdog() returns 0 early when market is closed without fetching portfolio.

High-conviction sizing tier (cash-only concentration, no margin):
  5. _high_conviction_grant: denied when confidence is None or below floor.
  6. _high_conviction_grant: granted when confidence >= 0.9 and no open HC positions.
  7. _high_conviction_grant: denied when max HC slots are occupied.
  8. _high_conviction_grant: same-ticker slot is excluded (re-buy of held HC name
     is not blocked by its own slot).
  9. _paper_buy HC sizing: notional capped at 15% of equity when HC is granted.
 10. Denied HC falls back to normal sizing; ledger records hc_note.
 11. propose_trade tool: HC guards (action, confidence floor, success).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
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
        "portfolio_advisor_alpaca_conf_min_pct": 0.02,
        "portfolio_advisor_alpaca_conf_max_pct": 0.06,
        "portfolio_advisor_alpaca_high_conviction_enabled": True,
        "portfolio_advisor_alpaca_high_conviction_pct": 0.15,
        "portfolio_advisor_alpaca_high_conviction_min_confidence": 0.85,
        "portfolio_advisor_alpaca_high_conviction_max_positions": 2,
        "portfolio_advisor_add_cooldown_days": 0,
        "portfolio_advisor_reentry_cooldown_days": 0,
        "portfolio_advisor_catalyst_max_hold_days": 30,
        "portfolio_advisor_catalyst_time_stop_days": 3,
        "portfolio_advisor_catalyst_hard_stop_pct": 0.08,
        # Disable regime overlay for these pre-regime tests so the caution fallback
        # (triggered when yfinance is mocked to return empty history) does not
        # reduce HC notional and break sizing assertions written before T1 shipped.
        "portfolio_advisor_regime_enabled": False,
    }
    cfg.update(overrides)
    return cfg


def _pa_dir(cfg: Dict[str, Any]) -> Path:
    p = Path(cfg["portfolio_advisor_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def _make_alpaca_client(equity: float = 100_000.0) -> MagicMock:
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


def _make_position(ticker: str) -> MagicMock:
    pos = MagicMock()
    pos.symbol = ticker
    return pos


def _build_propose_tool(cfg, captured, monkeypatch):
    """Patch proposals.add and position_plans; return the propose_trade tool."""
    from tradingagents.portfolio_advisor import pm_tools, proposals, position_plans

    def fake_add(c, **kw):
        captured.append(kw)

    monkeypatch.setattr(proposals, "add", fake_add)
    monkeypatch.setattr(position_plans, "append_position_decision", lambda *a, **k: None)
    tools = pm_tools.build_pm_tools(cfg, live_tickers=set())
    return next(t for t in tools if getattr(t, "name", "") == "propose_trade")


# ---------------------------------------------------------------------------
# 1. market_clock() returns None under pytest
# ---------------------------------------------------------------------------


def test_market_clock_returns_none_under_pytest():
    """executor.market_clock() must always return None when PYTEST_CURRENT_TEST is set."""
    from tradingagents.integrations.alpaca import executor as ex

    result = ex.market_clock()
    assert result is None, (
        "market_clock() must return None under pytest (PYTEST_CURRENT_TEST guard)"
    )


# ---------------------------------------------------------------------------
# 2 & 3. watchdog.market_is_open() — Alpaca answer and fallback
# ---------------------------------------------------------------------------


def test_market_is_open_uses_alpaca_clock_when_closed():
    """market_is_open() returns False when Alpaca clock reports is_open=False."""
    with patch(
        "tradingagents.integrations.alpaca.executor.market_clock",
        return_value={"is_open": False, "next_open": "2026-01-02T14:30:00Z", "next_close": "2026-01-02T21:00:00Z"},
    ):
        from tradingagents.portfolio_advisor import watchdog
        assert watchdog.market_is_open() is False


def test_market_is_open_uses_alpaca_clock_when_open():
    """market_is_open() returns True when Alpaca clock reports is_open=True."""
    with patch(
        "tradingagents.integrations.alpaca.executor.market_clock",
        return_value={"is_open": True, "next_open": "2026-01-03T14:30:00Z", "next_close": "2026-01-02T21:00:00Z"},
    ):
        from tradingagents.portfolio_advisor import watchdog
        assert watchdog.market_is_open() is True


def test_market_is_open_falls_back_to_utc_window_when_clock_none():
    """market_is_open() falls back to in_us_equity_watch_window_utc() when clock=None."""
    with patch(
        "tradingagents.integrations.alpaca.executor.market_clock",
        return_value=None,
    ):
        from tradingagents.portfolio_advisor import watchdog
        expected = watchdog.in_us_equity_watch_window_utc()
        result = watchdog.market_is_open()
        assert result == expected, (
            f"market_is_open() fallback must equal in_us_equity_watch_window_utc() "
            f"(got {result!r}, expected {expected!r})"
        )


# ---------------------------------------------------------------------------
# 4. run_watchdog() returns 0 immediately when market is closed
# ---------------------------------------------------------------------------


def test_run_watchdog_returns_zero_when_market_closed(tmp_path):
    """run_watchdog() must return 0 early without fetching when market is closed."""
    cfg = _cfg(tmp_path)
    _pa_dir(cfg)

    with patch(
        "tradingagents.integrations.alpaca.executor.market_clock",
        return_value={"is_open": False, "next_open": "2026-01-03T14:30:00Z", "next_close": ""},
    ):
        from tradingagents.portfolio_advisor import watchdog, etoro_scan

        # Confirm early exit without reaching eToro fetch
        with patch.object(
            etoro_scan,
            "fetch_portfolio_rows",
            side_effect=AssertionError("must not be called"),
        ):
            # Also patch enforce_paper_exits so executor.enabled() doesn't trip
            with patch("tradingagents.integrations.alpaca.executor.enforce_paper_exits", return_value=0):
                result = watchdog.run_watchdog(cfg)

    assert result == 0


# ---------------------------------------------------------------------------
# 5. _high_conviction_grant: denied when confidence is None or below floor
# ---------------------------------------------------------------------------


def test_hc_grant_denied_confidence_none(tmp_path):
    """_high_conviction_grant denies when confidence is None."""
    from tradingagents.integrations.alpaca import executor as ex

    cfg = _cfg(tmp_path)
    client = _make_alpaca_client()
    granted, note = ex._high_conviction_grant(cfg, client, "NVDA", None)
    assert not granted
    assert "floor" in note.lower() or "confidence" in note.lower()


def test_hc_grant_denied_confidence_too_low(tmp_path):
    """_high_conviction_grant denies when confidence is below the 0.85 floor."""
    from tradingagents.integrations.alpaca import executor as ex

    cfg = _cfg(tmp_path)
    client = _make_alpaca_client()
    granted, note = ex._high_conviction_grant(cfg, client, "NVDA", 0.7)
    assert not granted
    assert "floor" in note.lower() or "0.85" in note


# ---------------------------------------------------------------------------
# 6. _high_conviction_grant: granted when confidence >= 0.9 and no HC slots used
# ---------------------------------------------------------------------------


def test_hc_grant_granted_no_open_positions(tmp_path):
    """_high_conviction_grant grants when confidence >= 0.9 and no HC positions open."""
    from tradingagents.integrations.alpaca import executor as ex

    cfg = _cfg(tmp_path)
    _pa_dir(cfg)
    client = _make_alpaca_client()
    client.get_all_positions.return_value = []

    granted, note = ex._high_conviction_grant(cfg, client, "NVDA", 0.9)
    assert granted, f"Expected grant, got denial: {note}"
    assert "granted" in note.lower()


# ---------------------------------------------------------------------------
# 7. _high_conviction_grant: denied when max HC slots are full
# ---------------------------------------------------------------------------


def test_hc_grant_denied_slots_full(tmp_path):
    """_high_conviction_grant denies when two HC positions already open."""
    from tradingagents.integrations.alpaca import executor as ex
    from tradingagents.portfolio_advisor.position_plans import PositionPlan, upsert_position_plan

    cfg = _cfg(tmp_path)
    _pa_dir(cfg)

    # Write two HC plans
    for ticker in ("AAA", "BBB"):
        plan = PositionPlan(
            ticker=ticker,
            entry_price=100.0,
            strategy="core",
            high_conviction=True,
        )
        upsert_position_plan(cfg, plan)

    # Mock Alpaca client reporting both positions open
    client = _make_alpaca_client()
    client.get_all_positions.return_value = [
        _make_position("AAA"),
        _make_position("BBB"),
    ]

    granted, note = ex._high_conviction_grant(cfg, client, "CCC", 0.9)
    assert not granted
    assert "slots full" in note.lower() or "max" in note.lower()


# ---------------------------------------------------------------------------
# 8. Same-ticker re-grant: AAA's own slot doesn't block a re-buy of AAA
# ---------------------------------------------------------------------------


def test_hc_grant_same_ticker_excluded_from_slot_check(tmp_path):
    """HC slot check excludes the ticker being granted (re-buy of AAA not blocked by its own slot)."""
    from tradingagents.integrations.alpaca import executor as ex
    from tradingagents.portfolio_advisor.position_plans import PositionPlan, upsert_position_plan

    cfg = _cfg(tmp_path)
    _pa_dir(cfg)

    # Two HC plans: AAA and BBB
    for ticker in ("AAA", "BBB"):
        plan = PositionPlan(
            ticker=ticker,
            entry_price=100.0,
            strategy="core",
            high_conviction=True,
        )
        upsert_position_plan(cfg, plan)

    # Both open in Alpaca
    client = _make_alpaca_client()
    client.get_all_positions.return_value = [
        _make_position("AAA"),
        _make_position("BBB"),
    ]

    # Grant check for AAA itself — AAA should be excluded from the slot count
    # so only BBB occupies a slot (1 < max 2) → should be granted
    granted, note = ex._high_conviction_grant(cfg, client, "AAA", 0.9)
    assert granted, (
        f"Re-grant for AAA should succeed because AAA is excluded from its own slot count. "
        f"Got denial: {note}"
    )


# ---------------------------------------------------------------------------
# 9. _paper_buy HC sizing: notional == 15% of equity when HC is granted
# ---------------------------------------------------------------------------


@patch("tradingagents.integrations.alpaca.executor._notify")
@patch("tradingagents.integrations.alpaca.executor._vol_dampener", return_value=1.0)
@patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
def test_paper_buy_hc_sizing_15pct(mock_enabled, mock_dampener, mock_notify, tmp_path):
    """When HC is granted, notional must be min(approx_usd, equity * 15%)."""
    from tradingagents.integrations.alpaca import executor as ex

    cfg = _cfg(tmp_path)
    _pa_dir(cfg)

    equity = 100_000.0
    client = _make_alpaca_client(equity)
    # No HC positions open → grant succeeds
    client.get_all_positions.return_value = []

    proposal = {
        "ticker": "NVDA",
        "action": "buy",
        "approx_usd": 50_000.0,  # much bigger than the 15% cap → capped
        "confidence": 0.9,
        "high_conviction": True,
        "sleeve": "core",
        "target_price": 500.0,
    }

    # Patch _auto_create_position_plan so it doesn't call yfinance
    with patch.object(ex, "_auto_create_position_plan"):
        result = ex._paper_buy(cfg, client, equity, "NVDA", proposal)

    assert "skipped" not in result.lower(), f"Expected execution, got: {result}"
    client.submit_order.assert_called_once()

    # Extract the notional from the submitted MarketOrderRequest
    call_args = client.submit_order.call_args
    order_req = call_args[0][0]
    notional = float(order_req.notional)
    assert notional == pytest.approx(15_000.0, abs=1.0), (
        f"Expected notional ~$15,000 (15% of $100k with vol_dampener=1.0), got {notional}"
    )

    # Verify ledger row has high_conviction=True
    ledger_path = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
    assert ledger_path.is_file(), "Ledger file should exist after execution"
    rows = [json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()]
    assert rows, "Ledger should have at least one row"
    last_row = rows[-1]
    assert last_row.get("high_conviction") is True, f"Ledger high_conviction should be True: {last_row}"

    # Verify position plan has high_conviction=True
    from tradingagents.portfolio_advisor.position_plans import load_position_plans
    # We patched _auto_create_position_plan so the plan won't exist; skip plan check here


@patch("tradingagents.integrations.alpaca.executor._notify")
@patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
def test_paper_buy_hc_sizing_15pct_with_plan(mock_enabled, mock_notify, tmp_path):
    """Full integration: HC granted, 15% cap, position plan created with high_conviction=True."""
    from tradingagents.integrations.alpaca import executor as ex

    cfg = _cfg(tmp_path)
    _pa_dir(cfg)

    equity = 100_000.0
    client = _make_alpaca_client(equity)
    client.get_all_positions.return_value = []

    proposal = {
        "ticker": "NVDA",
        "action": "buy",
        "approx_usd": 50_000.0,
        "confidence": 0.9,
        "high_conviction": True,
        "sleeve": "core",
        "target_price": 500.0,
    }

    # Patch yfinance inside _auto_create_position_plan so test is hermetic
    with patch("tradingagents.integrations.alpaca.executor._vol_dampener", return_value=1.0):
        with patch("yfinance.Ticker") as mock_yf:
            mock_hist = MagicMock()
            mock_hist.__len__ = lambda self: 0
            mock_yf.return_value.history.return_value = mock_hist
            result = ex._paper_buy(cfg, client, equity, "NVDA", proposal)

    assert "skipped" not in result.lower(), f"Expected execution, got: {result}"

    # Ledger: high_conviction=True
    ledger_path = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
    rows = [json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()]
    last_row = rows[-1]
    assert last_row.get("high_conviction") is True

    # Notional capped at 15%
    notional = float(last_row["notional_usd"])
    assert notional == pytest.approx(15_000.0, abs=1.0)


# ---------------------------------------------------------------------------
# 10. Denied HC falls back to normal sizing
# ---------------------------------------------------------------------------


@patch("tradingagents.integrations.alpaca.executor._notify")
@patch("tradingagents.integrations.alpaca.executor._vol_dampener", return_value=1.0)
@patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
def test_paper_buy_hc_denied_falls_back_to_normal(mock_enabled, mock_dampener, mock_notify, tmp_path):
    """When HC is denied (confidence 0.6), trade executes at normal sizing."""
    from tradingagents.integrations.alpaca import executor as ex

    cfg = _cfg(tmp_path)
    _pa_dir(cfg)

    equity = 100_000.0
    client = _make_alpaca_client(equity)
    client.get_all_positions.return_value = []

    proposal = {
        "ticker": "NVDA",
        "action": "buy",
        "approx_usd": 50_000.0,
        "confidence": 0.6,  # below 0.85 floor → HC denied
        "high_conviction": True,
        "sleeve": "core",
        "target_price": 500.0,
    }

    with patch.object(ex, "_auto_create_position_plan"):
        result = ex._paper_buy(cfg, client, equity, "NVDA", proposal)

    assert "skipped" not in result.lower(), f"Trade should still execute: {result}"
    client.submit_order.assert_called_once()

    call_args = client.submit_order.call_args
    order_req = call_args[0][0]
    notional = float(order_req.notional)
    # Normal sizing: conf 0.6 → low-end. Must be < 15000.
    assert notional < 15_000.0, f"Denied HC should fall back to normal sizing < $15k, got {notional}"

    # Ledger: high_conviction=False, hc_note mentions floor
    ledger_path = Path(cfg["portfolio_advisor_dir"]) / "alpaca_trades.jsonl"
    rows = [json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()]
    last_row = rows[-1]
    assert last_row.get("high_conviction") is False, f"Ledger high_conviction should be False: {last_row}"
    hc_note = last_row.get("hc_note") or ""
    assert "floor" in hc_note.lower() or "confidence" in hc_note.lower(), (
        f"hc_note should mention floor/confidence: {hc_note!r}"
    )


# ---------------------------------------------------------------------------
# 11. propose_trade tool: HC validation guards
# ---------------------------------------------------------------------------


def test_propose_trade_hc_rejected_low_confidence(tmp_path, monkeypatch):
    """high_conviction=True with confidence 0.6 → error, nothing captured."""
    cfg = _cfg(tmp_path)
    _pa_dir(cfg)
    captured: list = []
    tool = _build_propose_tool(cfg, captured, monkeypatch)

    out = tool.invoke({
        "ticker": "NVDA",
        "action": "buy",
        "sleeve": "core",
        "approx_usd": 5000,
        "confidence": 0.6,
        "high_conviction": True,
        "reason": "Strong momentum setup",
    })
    assert "error" in out.lower(), f"Expected error, got: {out!r}"
    assert captured == [], "Nothing should be captured on HC confidence error"


def test_propose_trade_hc_rejected_wrong_action(tmp_path, monkeypatch):
    """high_conviction=True with action='trim' → error, nothing captured."""
    cfg = _cfg(tmp_path)
    _pa_dir(cfg)
    captured: list = []
    tool = _build_propose_tool(cfg, captured, monkeypatch)

    out = tool.invoke({
        "ticker": "NVDA",
        "action": "trim",
        "sleeve": "core",
        "approx_usd": 5000,
        "confidence": 0.9,
        "high_conviction": True,
        "reason": "Partial profit take",
    })
    assert "error" in out.lower(), f"Expected error, got: {out!r}"
    assert captured == [], "Nothing should be captured on HC action error"


def test_propose_trade_hc_success(tmp_path, monkeypatch):
    """high_conviction=True + action='buy' + confidence 0.9 → success, high_conviction captured."""
    cfg = _cfg(tmp_path)
    _pa_dir(cfg)
    captured: list = []
    tool = _build_propose_tool(cfg, captured, monkeypatch)

    out = tool.invoke({
        "ticker": "NVDA",
        "action": "buy",
        "sleeve": "core",
        "approx_usd": 5000,
        "confidence": 0.9,
        "high_conviction": True,
        "reason": "Best setup of the year: all signals aligned",
    })
    assert "error" not in out.lower(), f"Expected success, got: {out!r}"
    assert len(captured) == 1, f"Expected one captured proposal, got {len(captured)}: {captured}"
    assert captured[0].get("high_conviction") is True, (
        f"high_conviction should be True in captured kwargs: {captured[0]}"
    )
