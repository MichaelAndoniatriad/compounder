"""Tests for T2 — R-based catalyst sizing + PM veto gate.

Covers:
- R-sizing math: equity 100k, 1% risk, 8% stop → $12,500 notional
- Regime multiplier applies AFTER R-sizing
- Max-position cap binds before regime
- HC denied for catalyst sleeve; HC still works for core sleeve
- Scanner-sourced proposal gets veto window + NOT auto-executed immediately
- PM-originated proposal (propose_trade) still immediate-executes
- veto_candidate cancels proposal + writes shadow row with status=pm_vetoed
- execute_unvetoed_candidates executes after veto window expires
- execute_unvetoed_candidates skips proposals within window
- execute_unvetoed_candidates re-checks cooldown guards (skip if in cooldown)
- veto_scorecard math on fixture rows
- sizing_method="r_based_1pct" in ledger for catalyst; "confidence" for core
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


def _cfg(tmp_path: Path, **overrides) -> Dict[str, Any]:
    """Minimal executor config — no live APIs, no cooldowns by default."""
    cfg: Dict[str, Any] = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_alpaca_paper": True,
        "portfolio_advisor_alpaca_max_position_pct": 0.10,  # 10% cap = $10k on $100k
        "portfolio_advisor_add_cooldown_days": 0,
        "portfolio_advisor_reentry_cooldown_days": 0,
        "portfolio_advisor_max_spread_bps": 100,
        "portfolio_advisor_limit_slip_bps": 10,
        "portfolio_advisor_catalyst_hard_stop_pct": 0.08,
        "portfolio_advisor_catalyst_risk_pct": 0.01,
        "portfolio_advisor_pm_veto_minutes": 45,
        "portfolio_advisor_regime_enabled": False,   # off by default — test separately
        "portfolio_advisor_alpaca_high_conviction_enabled": True,
        "portfolio_advisor_alpaca_high_conviction_pct": 0.15,
        "portfolio_advisor_alpaca_high_conviction_min_confidence": 0.85,
        "portfolio_advisor_alpaca_high_conviction_max_positions": 2,
    }
    cfg.update(overrides)
    return cfg


def _make_client(equity: float = 100_000.0) -> MagicMock:
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


def _catalyst_proposal(ticker: str = "ACME", usd: float = 5_000.0,
                       entry_price: float = 50.0, **extra) -> Dict[str, Any]:
    p: Dict[str, Any] = {
        "ticker": ticker,
        "action": "buy",
        "approx_usd": usd,
        "sleeve": "catalyst",
        "target_price": entry_price,
        "catalyst_date": "2026-08-01",
        "confidence": 0.7,
    }
    p.update(extra)
    return p


def _core_proposal(ticker: str = "AAPL", usd: float = 5_000.0,
                   entry_price: float = 200.0, **extra) -> Dict[str, Any]:
    p: Dict[str, Any] = {
        "ticker": ticker,
        "action": "buy",
        "approx_usd": usd,
        "sleeve": "core",
        "target_price": entry_price,
        "confidence": 0.7,
    }
    p.update(extra)
    return p


# ---------------------------------------------------------------------------
# 1. R-based sizing math
# ---------------------------------------------------------------------------


class TestRBasedSizing:
    """R-based catalyst sizing: equity × risk_pct / hard_stop_pct."""

    def _run_paper_buy(self, tmp_path, equity, proposal, extra_cfg=None):
        """Exercise _paper_buy with mocked client + market clock open."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path, **(extra_cfg or {}))
        client = _make_client(equity)

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "_client", return_value=client),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "_latest_quote", return_value=None),  # market order path
            patch.object(ex, "_vol_dampener", return_value=1.0),
            patch.object(ex, "_auto_create_position_plan"),
            patch.object(ex, "_ensure_baseline"),
            # suppress threading fill-check
            patch("threading.Thread"),
        ):
            return ex._paper_buy(cfg, client, float(equity), proposal["ticker"], proposal)

    def test_r_sizing_base_math(self, tmp_path):
        """equity=100k, risk_pct=1%, hard_stop_pct=8% → notional=$12,500."""
        # risk_budget = 100_000 × 0.01 = $1,000
        # notional = 1_000 / 0.08 = $12,500
        # cap (10%) = $10,000 → min($12,500, $10,000) = $10,000
        # NOTE: max_position_pct cap BINDS here (12,500 > 10,000 = 10% of 100k)
        cfg = _cfg(tmp_path,
                   portfolio_advisor_catalyst_risk_pct=0.01,
                   portfolio_advisor_catalyst_hard_stop_pct=0.08,
                   portfolio_advisor_alpaca_max_position_pct=0.15)  # raise cap so math works
        equity = 100_000.0
        client = _make_client(equity)

        from tradingagents.integrations.alpaca import executor as ex

        captured_notional = []

        orig_log = ex._log_row

        def capture_log(c, row):
            if row.get("action") == "buy":
                captured_notional.append(row.get("notional_usd"))
            orig_log(c, row)

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "_client", return_value=client),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "_latest_quote", return_value=None),
            patch.object(ex, "_vol_dampener", return_value=1.0),
            patch.object(ex, "_auto_create_position_plan"),
            patch.object(ex, "_ensure_baseline"),
            patch.object(ex, "_log_row", side_effect=capture_log),
            patch("threading.Thread"),
        ):
            result = ex._paper_buy(cfg, client, equity, "ACME",
                                   _catalyst_proposal(entry_price=50.0))

        assert "skipped" not in result, f"unexpected skip: {result}"
        # Expected: 100k × 1% / 8% = $12,500; cap=15% of 100k=15k; min=12,500
        assert captured_notional, "no ledger row captured"
        assert abs(captured_notional[0] - 12_500.0) < 1.0, (
            f"expected $12,500 notional, got {captured_notional[0]}"
        )

    def test_max_position_cap_binds(self, tmp_path):
        """When R-notional > max_position_pct × equity, cap wins."""
        # equity=100k, risk_pct=5%, stop=8% → R-notional = 62,500 → cap 10% = 10k
        cfg = _cfg(tmp_path,
                   portfolio_advisor_catalyst_risk_pct=0.05,
                   portfolio_advisor_catalyst_hard_stop_pct=0.08,
                   portfolio_advisor_alpaca_max_position_pct=0.10)
        equity = 100_000.0
        client = _make_client(equity)
        from tradingagents.integrations.alpaca import executor as ex

        captured = []
        orig = ex._log_row
        def cap(c, row):
            if row.get("action") == "buy":
                captured.append(row.get("notional_usd"))
            orig(c, row)

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "_client", return_value=client),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "_latest_quote", return_value=None),
            patch.object(ex, "_vol_dampener", return_value=1.0),
            patch.object(ex, "_auto_create_position_plan"),
            patch.object(ex, "_ensure_baseline"),
            patch.object(ex, "_log_row", side_effect=cap),
            patch("threading.Thread"),
        ):
            ex._paper_buy(cfg, client, equity, "ACME",
                          _catalyst_proposal(entry_price=50.0))

        assert captured, "no ledger row"
        assert abs(captured[0] - 10_000.0) < 1.0, (
            f"expected cap-bound $10,000, got {captured[0]}"
        )

    def test_regime_multiplier_applied_after_r_sizing(self, tmp_path):
        """Regime multiplier (0.75 for caution) reduces notional AFTER R-sizing."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path,
                   portfolio_advisor_catalyst_risk_pct=0.01,
                   portfolio_advisor_catalyst_hard_stop_pct=0.08,
                   portfolio_advisor_alpaca_max_position_pct=0.15,
                   portfolio_advisor_regime_enabled=True)
        equity = 100_000.0
        client = _make_client(equity)

        captured = []
        orig = ex._log_row
        def cap(c, row):
            if row.get("action") == "buy":
                captured.append(row.get("notional_usd"))
            orig(c, row)

        mock_regime = {"regime": "caution", "cached": False}
        mock_breaker = {"level": "none", "drawdown_pct": 0.0}

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "_client", return_value=client),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "_latest_quote", return_value=None),
            patch.object(ex, "_vol_dampener", return_value=1.0),
            patch.object(ex, "_auto_create_position_plan"),
            patch.object(ex, "_ensure_baseline"),
            patch.object(ex, "_log_row", side_effect=cap),
            patch("tradingagents.portfolio_advisor.regime.compute_regime", return_value=mock_regime),
            patch("tradingagents.portfolio_advisor.regime.drawdown_breaker", return_value=mock_breaker),
            patch("tradingagents.portfolio_advisor.regime.new_buy_multiplier", return_value=0.75),
            patch("threading.Thread"),
        ):
            ex._paper_buy(cfg, client, equity, "ACME",
                          _catalyst_proposal(entry_price=50.0))

        assert captured, "no ledger row"
        # R-notional = 12,500; regime mult 0.75 → 9,375
        assert abs(captured[0] - 9_375.0) < 1.0, (
            f"expected $9,375 after regime mult, got {captured[0]}"
        )

    def test_sizing_method_in_ledger_catalyst(self, tmp_path):
        """Catalyst buy ledger row has sizing_method='r_based_1pct'."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path,
                   portfolio_advisor_catalyst_risk_pct=0.01,
                   portfolio_advisor_catalyst_hard_stop_pct=0.08,
                   portfolio_advisor_alpaca_max_position_pct=0.15)
        equity = 100_000.0
        client = _make_client(equity)

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "_client", return_value=client),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "_latest_quote", return_value=None),
            patch.object(ex, "_vol_dampener", return_value=1.0),
            patch.object(ex, "_auto_create_position_plan"),
            patch.object(ex, "_ensure_baseline"),
            patch("threading.Thread"),
        ):
            ex._paper_buy(cfg, client, equity, "ACME",
                          _catalyst_proposal(entry_price=50.0))

        ledger = Path(str(tmp_path / "pa")) / "alpaca_trades.jsonl"
        assert ledger.is_file()
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        buy_rows = [r for r in rows if r.get("action") == "buy" and r.get("status") == "submitted"]
        assert buy_rows, "no submitted buy row"
        assert buy_rows[0]["sizing_method"] == "r_based_1pct"

    def test_sizing_method_in_ledger_core(self, tmp_path):
        """Core buy ledger row has sizing_method='confidence'."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path, portfolio_advisor_alpaca_max_position_pct=0.10)
        equity = 100_000.0
        client = _make_client(equity)

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "_client", return_value=client),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "_latest_quote", return_value=None),
            patch.object(ex, "_vol_dampener", return_value=1.0),
            patch.object(ex, "_auto_create_position_plan"),
            patch.object(ex, "_ensure_baseline"),
            patch("threading.Thread"),
        ):
            ex._paper_buy(cfg, client, equity, "AAPL",
                          _core_proposal(usd=5_000.0))

        ledger = Path(str(tmp_path / "pa")) / "alpaca_trades.jsonl"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        buy_rows = [r for r in rows if r.get("action") == "buy" and r.get("status") == "submitted"]
        assert buy_rows, "no submitted buy row"
        assert buy_rows[0]["sizing_method"] == "confidence"


# ---------------------------------------------------------------------------
# 2. HC denied for catalyst
# ---------------------------------------------------------------------------


class TestHCDeniedForCatalyst:

    def test_hc_denied_for_catalyst_sleeve(self, tmp_path):
        """High-conviction grant returns (False, '...core-sleeve only...') for catalyst."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        client = _make_client()

        granted, note = ex._high_conviction_grant(cfg, client, "ACME", 0.95, sleeve="catalyst")
        assert granted is False
        assert "core-sleeve only" in note

    def test_hc_granted_for_core_sleeve(self, tmp_path):
        """HC grant returns True for core sleeve when confidence clears floor."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path,
                   portfolio_advisor_alpaca_high_conviction_enabled=True,
                   portfolio_advisor_alpaca_high_conviction_min_confidence=0.85,
                   portfolio_advisor_alpaca_high_conviction_max_positions=2)
        client = _make_client()
        client.get_all_positions.return_value = []

        with patch("tradingagents.portfolio_advisor.position_plans.load_position_plans", return_value={}):
            granted, note = ex._high_conviction_grant(cfg, client, "AAPL", 0.90, sleeve="core")

        assert granted is True, f"expected granted, got: {note}"

    def test_hc_flag_on_catalyst_proposal_still_executes_normally(self, tmp_path):
        """Catalyst proposal with high_conviction=True executes at R-size (HC denied silently)."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path,
                   portfolio_advisor_catalyst_risk_pct=0.01,
                   portfolio_advisor_catalyst_hard_stop_pct=0.08,
                   portfolio_advisor_alpaca_max_position_pct=0.15)
        equity = 100_000.0
        client = _make_client(equity)

        captured = []
        orig = ex._log_row
        def cap(c, row):
            if row.get("action") == "buy":
                captured.append(row)
            orig(c, row)

        proposal = _catalyst_proposal(entry_price=50.0)
        proposal["high_conviction"] = True
        proposal["confidence"] = 0.92

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "_client", return_value=client),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "_latest_quote", return_value=None),
            patch.object(ex, "_vol_dampener", return_value=1.0),
            patch.object(ex, "_auto_create_position_plan"),
            patch.object(ex, "_ensure_baseline"),
            patch.object(ex, "_log_row", side_effect=cap),
            patch("threading.Thread"),
        ):
            result = ex._paper_buy(cfg, client, equity, "ACME", proposal)

        assert "skipped" not in result
        assert captured
        # HC denied → notional should be R-based, not HC-cap-based
        assert abs(captured[0]["notional_usd"] - 12_500.0) < 1.0
        # high_conviction field in ledger should be False (denied)
        assert captured[0]["high_conviction"] is False


# ---------------------------------------------------------------------------
# 3. Scanner proposal gets veto window + not auto-executed immediately
# ---------------------------------------------------------------------------


class TestScannerProposalVetoWindow:

    def test_scanner_catalyst_proposal_gets_veto_window(self, tmp_path):
        """ep_scanner catalyst buy is filed with pm_veto_window_until, NOT executed."""
        from tradingagents.portfolio_advisor import proposals

        cfg = _cfg(tmp_path, portfolio_advisor_pm_veto_minutes=45)

        # Mock executor to ensure it never gets called
        with patch("tradingagents.integrations.alpaca.executor.execute_proposal") as mock_exec:
            before = datetime.now(timezone.utc)
            entry = proposals.add(
                cfg,
                ticker="ACME",
                action="buy",
                approx_usd=5000.0,
                sleeve="catalyst",
                reason="PEAD drift play",
                catalyst_date="2026-08-01",
                source="ep_scanner",
            )
            after = datetime.now(timezone.utc)

        # execute_proposal should NOT have been called
        mock_exec.assert_not_called()
        # entry should have pm_veto_window_until set
        assert entry.get("pm_veto_window_until") is not None
        veto_until = datetime.fromisoformat(entry["pm_veto_window_until"])
        assert veto_until > before
        # Window should be ~45 minutes from now
        expected_min = before + timedelta(minutes=44)
        expected_max = after + timedelta(minutes=46)
        assert expected_min <= veto_until <= expected_max
        # Status should remain proposed
        assert entry["status"] == "proposed"

    def test_pead_scanner_catalyst_proposal_gets_veto_window(self, tmp_path):
        """pead_scanner catalyst buy also gets veto window."""
        from tradingagents.portfolio_advisor import proposals

        cfg = _cfg(tmp_path, portfolio_advisor_pm_veto_minutes=30)

        with patch("tradingagents.integrations.alpaca.executor.execute_proposal") as mock_exec:
            entry = proposals.add(
                cfg,
                ticker="PEAD1",
                action="buy",
                approx_usd=3000.0,
                sleeve="catalyst",
                reason="earnings drift",
                catalyst_date="2026-07-15",
                source="pead_scanner",
            )

        mock_exec.assert_not_called()
        assert entry.get("pm_veto_window_until") is not None
        assert entry["status"] == "proposed"

    def test_non_scanner_proposal_executes_immediately(self, tmp_path):
        """PM-originated proposal (no source / propose_trade) executes immediately."""
        from tradingagents.portfolio_advisor import proposals

        cfg = _cfg(tmp_path)

        mock_result = {"status": "executed", "detail": "executed ok"}
        with patch("tradingagents.integrations.alpaca.executor.execute_proposal",
                   return_value=mock_result) as mock_exec:
            entry = proposals.add(
                cfg,
                ticker="AAPL",
                action="buy",
                approx_usd=5000.0,
                sleeve="core",
                reason="thesis intact",
                source="",  # PM-originated
            )

        mock_exec.assert_called_once()
        assert entry.get("pm_veto_window_until") is None

    def test_scanner_core_proposal_executes_immediately(self, tmp_path):
        """Scanner source with core sleeve is NOT veto-gated (only catalyst)."""
        from tradingagents.portfolio_advisor import proposals

        cfg = _cfg(tmp_path)

        mock_result = {"status": "executed", "detail": "ok"}
        with patch("tradingagents.integrations.alpaca.executor.execute_proposal",
                   return_value=mock_result) as mock_exec:
            entry = proposals.add(
                cfg,
                ticker="XYZ",
                action="buy",
                approx_usd=2000.0,
                sleeve="core",
                reason="watchlist core add",
                source="ep_scanner",  # scanner source but core sleeve
            )

        mock_exec.assert_called_once()
        assert entry.get("pm_veto_window_until") is None


# ---------------------------------------------------------------------------
# 4. veto_candidate tool cancels + shadow row
# ---------------------------------------------------------------------------


class TestVetoCandidate:

    def _build_pm_tools(self, cfg):
        from tradingagents.portfolio_advisor.pm_tools import build_pm_tools
        tools = build_pm_tools(cfg, live_tickers=set())
        return {t.name: t for t in tools}

    def test_veto_cancels_pending_proposal(self, tmp_path):
        """veto_candidate marks the pending proposal as pm_vetoed."""
        from tradingagents.portfolio_advisor import proposals

        cfg = _cfg(tmp_path, portfolio_advisor_pm_veto_minutes=60)

        # File a scanner-sourced proposal
        with patch("tradingagents.integrations.alpaca.executor.execute_proposal") as _:
            proposals.add(
                cfg,
                ticker="VETO1",
                action="buy",
                approx_usd=3000.0,
                sleeve="catalyst",
                reason="PEAD setup",
                catalyst_date="2026-08-10",
                source="pead_scanner",
            )

        tools = self._build_pm_tools(cfg)
        veto_tool = tools["veto_candidate"]

        with patch("tradingagents.portfolio_advisor.candidates.shadow_book_add", return_value={"ts": "now"}):
            result = veto_tool.invoke({"ticker": "VETO1", "reason": "bad data — wrong earnings date"})

        assert "VETOED" in result or "vetoed" in result.lower(), f"unexpected result: {result}"

        # Proposal row should now be pm_vetoed
        rows = proposals.load_all(cfg)
        veto_rows = [r for r in rows if r.get("ticker") == "VETO1" and r.get("status") == "pm_vetoed"]
        assert veto_rows, "expected pm_vetoed row"

    def test_veto_writes_shadow_row(self, tmp_path):
        """veto_candidate writes a shadow_book row with status=pm_vetoed."""
        from tradingagents.portfolio_advisor import proposals
        from tradingagents.portfolio_advisor.candidates import shadow_book_path

        cfg = _cfg(tmp_path, portfolio_advisor_pm_veto_minutes=60)

        with patch("tradingagents.integrations.alpaca.executor.execute_proposal") as _:
            proposals.add(
                cfg,
                ticker="VETO2",
                action="buy",
                approx_usd=3000.0,
                sleeve="catalyst",
                reason="PEAD setup",
                catalyst_date="2026-08-15",
                source="ep_scanner",
                target_price=45.0,
            )

        tools = self._build_pm_tools(cfg)
        veto_tool = tools["veto_candidate"]

        # Use real shadow_book_add but mock yfinance price fetch
        import yfinance as yf
        import pandas as pd
        mock_hist = pd.DataFrame({"Close": [45.0], "Volume": [1_000_000]})
        with patch.object(yf.Ticker, "history", return_value=mock_hist):
            result = veto_tool.invoke({"ticker": "VETO2", "reason": "corporate action — merger announced"})

        sb_path = shadow_book_path(cfg)
        assert sb_path.is_file(), "shadow_book.jsonl not created"
        rows = [json.loads(l) for l in sb_path.read_text().splitlines() if l.strip()]
        vetoed_rows = [r for r in rows if r.get("ticker") == "VETO2" and "pm_vetoed" in str(r.get("source") or "")]
        assert vetoed_rows, "expected pm_vetoed shadow row"
        assert vetoed_rows[0]["status"] == "pm_vetoed"

    def test_veto_without_reason_rejected(self, tmp_path):
        """veto_candidate with empty reason returns error."""
        cfg = _cfg(tmp_path)
        tools = self._build_pm_tools(cfg)
        veto_tool = tools["veto_candidate"]
        result = veto_tool.invoke({"ticker": "AAPL", "reason": ""})
        assert "error" in result.lower()

    def test_veto_no_matching_proposal_returns_error(self, tmp_path):
        """veto_candidate on a ticker with no pending scanner proposal returns error."""
        cfg = _cfg(tmp_path)
        tools = self._build_pm_tools(cfg)
        veto_tool = tools["veto_candidate"]
        result = veto_tool.invoke({"ticker": "NOMATCH", "reason": "bad data"})
        assert "no pending" in result.lower() or "not found" in result.lower() or "no pending" in result.lower()

    def test_veto_not_allowed_on_pm_originated_proposal(self, tmp_path):
        """veto_candidate only works on scanner-sourced proposals (has pm_veto_window_until)."""
        from tradingagents.portfolio_advisor import proposals

        cfg = _cfg(tmp_path)

        # PM-originated proposal (no pm_veto_window_until)
        mock_result = {"status": "executed", "detail": "ok"}
        with patch("tradingagents.integrations.alpaca.executor.execute_proposal",
                   return_value=mock_result):
            proposals.add(
                cfg,
                ticker="PMTRADE",
                action="buy",
                approx_usd=2000.0,
                sleeve="catalyst",
                reason="PM direct trade",
                catalyst_date="2026-08-01",
                source="",
            )

        tools = self._build_pm_tools(cfg)
        veto_tool = tools["veto_candidate"]
        result = veto_tool.invoke({"ticker": "PMTRADE", "reason": "want to block it"})
        # Should fail — no pm_veto_window_until on a PM-originated proposal
        assert "no pending" in result.lower() or "not found" in result.lower()


# ---------------------------------------------------------------------------
# 5. execute_unvetoed_candidates
# ---------------------------------------------------------------------------


class TestExecuteUnvetoed:

    def _file_scanner_proposal(self, cfg, ticker: str,
                                veto_minutes_offset: int = -60) -> Dict:
        """File a scanner-sourced proposal and manually set veto window to the past/future."""
        from tradingagents.portfolio_advisor import proposals

        with patch("tradingagents.integrations.alpaca.executor.execute_proposal") as _:
            entry = proposals.add(
                cfg,
                ticker=ticker,
                action="buy",
                approx_usd=3000.0,
                sleeve="catalyst",
                reason="PEAD drift",
                catalyst_date="2026-08-01",
                source="pead_scanner",
            )

        # Manually set veto window to past (expired) or future (active)
        rows = proposals.load_all(cfg)
        for r in rows:
            if r.get("ticker") == ticker and r.get("status") == "proposed":
                new_until = (datetime.now(timezone.utc) +
                             timedelta(minutes=veto_minutes_offset)).isoformat()
                r["pm_veto_window_until"] = new_until
                break
        proposals.save_all(cfg, rows)
        return entry

    def test_expired_window_executes(self, tmp_path):
        """execute_unvetoed_candidates fires proposals whose window has expired."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        self._file_scanner_proposal(cfg, "EXP1", veto_minutes_offset=-60)

        mock_result = {"status": "executed", "detail": "done"}
        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "execute_proposal", return_value=mock_result),
        ):
            count = ex.execute_unvetoed_candidates(cfg)

        assert count == 1

        # Check the row was marked executed
        from tradingagents.portfolio_advisor import proposals
        rows = proposals.load_all(cfg)
        exp_rows = [r for r in rows if r.get("ticker") == "EXP1" and r.get("status") == "executed"]
        assert exp_rows, "row not marked executed"

    def test_active_window_not_executed(self, tmp_path):
        """execute_unvetoed_candidates skips proposals still within veto window."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        self._file_scanner_proposal(cfg, "ACT1", veto_minutes_offset=+30)  # still active

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "execute_proposal") as mock_exec,
        ):
            count = ex.execute_unvetoed_candidates(cfg)

        mock_exec.assert_not_called()
        assert count == 0

    def test_cooldown_guard_skips_if_in_cooldown(self, tmp_path):
        """execute_unvetoed_candidates re-checks cooldown via execute_proposal (skipped result)."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        self._file_scanner_proposal(cfg, "COOL1", veto_minutes_offset=-60)

        # execute_proposal returns "skipped" (cooldown fired inside)
        mock_result = {"status": "skipped", "detail": "cooldown: buy within last 5d"}
        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "market_clock", return_value={"is_open": True}),
            patch.object(ex, "execute_proposal", return_value=mock_result),
        ):
            count = ex.execute_unvetoed_candidates(cfg)

        assert count == 1  # attempted (returned 1 even though it was skipped)

        from tradingagents.portfolio_advisor import proposals
        rows = proposals.load_all(cfg)
        cancelled = [r for r in rows if r.get("ticker") == "COOL1" and r.get("status") == "cancelled"]
        assert cancelled, "skipped result should mark proposal as cancelled"

    def test_market_closed_skips_all(self, tmp_path):
        """execute_unvetoed_candidates returns 0 immediately when market is closed."""
        from tradingagents.integrations.alpaca import executor as ex

        cfg = _cfg(tmp_path)
        self._file_scanner_proposal(cfg, "CLOSED1", veto_minutes_offset=-60)

        with (
            patch.object(ex, "enabled", return_value=True),
            patch.object(ex, "market_clock", return_value={"is_open": False}),
            patch.object(ex, "execute_proposal") as mock_exec,
        ):
            count = ex.execute_unvetoed_candidates(cfg)

        mock_exec.assert_not_called()
        assert count == 0


# ---------------------------------------------------------------------------
# 6. veto_scorecard math
# ---------------------------------------------------------------------------


class TestVetoScorecard:

    def _write_shadow_outcomes(self, path: Path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def _write_outcomes(self, path: Path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def _write_ledger(self, path: Path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_empty_returns_zero_counts(self, tmp_path):
        """veto_scorecard with no data returns zero counts and None lifts."""
        from tradingagents.portfolio_advisor.outcome_tracker import veto_scorecard

        cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
        result = veto_scorecard(cfg)
        assert result["vetoed"]["count"] == 0
        assert result["executed"]["count"] == 0
        assert result["pm_veto_lift"] is None

    def test_scoreboard_math(self, tmp_path):
        """veto_scorecard computes avg returns correctly from fixture rows."""
        from tradingagents.portfolio_advisor.outcome_tracker import veto_scorecard
        from tradingagents.portfolio_advisor import state as pa_state

        cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
        adv_dir = pa_state.advisor_dir(cfg)
        adv_dir.mkdir(parents=True, exist_ok=True)

        # Three vetoed outcomes in shadow_outcomes.jsonl
        shadow_rows = [
            {"ticker": "A", "source": "pm_vetoed_pead_scanner",
             "raw_return": 0.10, "alpha_vs_spy": 0.05},
            {"ticker": "B", "source": "proposals_choke_pm_vetoed",
             "raw_return": 0.20, "alpha_vs_spy": 0.12},
            {"ticker": "C", "source": "pm_vetoed",
             "raw_return": -0.05, "alpha_vs_spy": -0.08},
        ]
        self._write_shadow_outcomes(adv_dir / "shadow_outcomes.jsonl", shadow_rows)

        # Two executed catalyst outcomes in outcomes.jsonl + ledger
        ledger_rows = [
            {"ticker": "D", "action": "buy", "sleeve": "catalyst", "status": "submitted"},
            {"ticker": "E", "action": "buy", "sleeve": "catalyst", "status": "submitted"},
        ]
        self._write_ledger(adv_dir / "alpaca_trades.jsonl", ledger_rows)

        outcomes_rows = [
            {"ticker": "D", "realised_return": 0.15, "alpha_vs_qqq": 0.08},
            {"ticker": "E", "realised_return": 0.05, "alpha_vs_qqq": 0.01},
        ]
        self._write_outcomes(adv_dir / "outcomes.jsonl", outcomes_rows)

        result = veto_scorecard(cfg)

        # Vetoed avg = (0.10 + 0.20 - 0.05) / 3 = 0.0833...
        expected_vetoed_avg = round((0.10 + 0.20 - 0.05) / 3, 4)
        # Executed avg = (0.15 + 0.05) / 2 = 0.10
        expected_executed_avg = round((0.15 + 0.05) / 2, 4)

        assert result["vetoed"]["count"] == 3
        assert result["executed"]["count"] == 2
        assert abs(result["vetoed"]["avg_30d_return"] - expected_vetoed_avg) < 1e-4
        assert abs(result["executed"]["avg_30d_return"] - expected_executed_avg) < 1e-4

        # veto_lift = executed_avg - vetoed_avg
        expected_lift = round(expected_executed_avg - expected_vetoed_avg, 4)
        assert abs(result["pm_veto_lift"] - expected_lift) < 1e-4

    def test_positive_lift_note(self, tmp_path):
        """Positive veto_lift note says PM adds value."""
        from tradingagents.portfolio_advisor.outcome_tracker import veto_scorecard
        from tradingagents.portfolio_advisor import state as pa_state

        cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
        adv_dir = pa_state.advisor_dir(cfg)
        adv_dir.mkdir(parents=True, exist_ok=True)

        # Executed returns better than vetoed
        self._write_shadow_outcomes(adv_dir / "shadow_outcomes.jsonl", [
            {"ticker": "X", "source": "pm_vetoed", "raw_return": -0.10, "alpha_vs_spy": -0.15},
        ])
        self._write_ledger(adv_dir / "alpaca_trades.jsonl", [
            {"ticker": "Y", "action": "buy", "sleeve": "catalyst", "status": "submitted"},
        ])
        self._write_outcomes(adv_dir / "outcomes.jsonl", [
            {"ticker": "Y", "realised_return": 0.20, "alpha_vs_qqq": 0.15},
        ])

        result = veto_scorecard(cfg)
        assert result["pm_veto_lift"] is not None and result["pm_veto_lift"] > 0
        assert "add value" in result["note"].lower() or "ADD" in result["note"]

    def test_negative_lift_note(self, tmp_path):
        """Negative veto_lift note suggests demoting PM."""
        from tradingagents.portfolio_advisor.outcome_tracker import veto_scorecard
        from tradingagents.portfolio_advisor import state as pa_state

        cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
        adv_dir = pa_state.advisor_dir(cfg)
        adv_dir.mkdir(parents=True, exist_ok=True)

        # Vetoed returns better than executed — PM is adding noise
        self._write_shadow_outcomes(adv_dir / "shadow_outcomes.jsonl", [
            {"ticker": "X", "source": "pm_vetoed", "raw_return": 0.30, "alpha_vs_spy": 0.25},
        ])
        self._write_ledger(adv_dir / "alpaca_trades.jsonl", [
            {"ticker": "Y", "action": "buy", "sleeve": "catalyst", "status": "submitted"},
        ])
        self._write_outcomes(adv_dir / "outcomes.jsonl", [
            {"ticker": "Y", "realised_return": -0.10, "alpha_vs_qqq": -0.15},
        ])

        result = veto_scorecard(cfg)
        assert result["pm_veto_lift"] is not None and result["pm_veto_lift"] < 0
        assert "subtract" in result["note"].lower() or "SUBTRACT" in result["note"]


# ---------------------------------------------------------------------------
# 7. CLI measure-outcomes smoke test (veto_scorecard wired in)
# ---------------------------------------------------------------------------


class TestMeasureOutcomesCLI:

    def test_measure_outcomes_includes_veto_scorecard(self, tmp_path, monkeypatch):
        """measure-outcomes CLI output includes veto-scorecard line."""
        from typer.testing import CliRunner
        from cli.main import app
        from tradingagents.default_config import DEFAULT_CONFIG

        monkeypatch.setattr(
            "cli.advisor_cmd.DEFAULT_CONFIG",
            {**DEFAULT_CONFIG, "portfolio_advisor_dir": str(tmp_path / "pa")},
        )
        runner = CliRunner()
        result = runner.invoke(app, ["advisor", "portfolio", "measure-outcomes"])
        assert result.exit_code == 0, result.output
        assert "veto-scorecard" in result.output
