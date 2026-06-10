"""R2 — Measurement loop tests.

Covers:
- executed proposal → recommendation_log row written
- skipped/cancelled proposal → no recommendation_log row
- shadow_book_add writes correct-shaped row
- shadow_outcomes scores a ≥30d-old row and skips a <30d-old row
- record_daily_nav writes once per day (second call same day = no-op)
- CLI measure-outcomes smoke test (monkeypatched)
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
    return {"portfolio_advisor_dir": str(tmp_path)}


def _advisor_dir(tmp_path: Path) -> Path:
    return tmp_path


def _rec_log_path(tmp_path: Path) -> Path:
    return tmp_path / "recommendation_log.jsonl"


# ---------------------------------------------------------------------------
# 1. proposals.add() — executed → recommendation_log row
# ---------------------------------------------------------------------------

class TestProposalRecommendationLog:
    def test_executed_proposal_writes_rec_log(self, tmp_path):
        """When executor returns status=executed, a recommendation_log row is appended."""
        cfg = _cfg(tmp_path)

        # Patch the alpaca executor to return "executed"
        mock_result = {"status": "executed", "detail": "filled 10 sh @ 100.00"}
        with patch(
            "tradingagents.integrations.alpaca.executor.execute_proposal",
            return_value=mock_result,
        ):
            from tradingagents.portfolio_advisor import proposals

            proposals.add(
                cfg,
                ticker="AAPL",
                action="buy",
                shares=10,
                approx_usd=1000,
                reason="test reason",
                sleeve="core",
            )

        log_path = _rec_log_path(tmp_path)
        assert log_path.is_file(), "recommendation_log.jsonl should be created"
        rows = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        # At least one row must be for this ticker with trigger=autonomous_execution
        executed_rows = [r for r in rows if r.get("trigger") == "autonomous_execution"]
        assert len(executed_rows) >= 1
        row = executed_rows[0]
        assert row["ticker"] == "AAPL"
        assert row["action"] == "buy"

    def test_skipped_proposal_no_rec_log(self, tmp_path):
        """When executor returns status=skipped, NO recommendation_log row is written."""
        cfg = _cfg(tmp_path)

        mock_result = {"status": "skipped", "detail": "market closed"}
        with patch(
            "tradingagents.integrations.alpaca.executor.execute_proposal",
            return_value=mock_result,
        ):
            from tradingagents.portfolio_advisor import proposals

            proposals.add(
                cfg,
                ticker="TSLA",
                action="buy",
                shares=5,
                sleeve="catalyst",
            )

        log_path = _rec_log_path(tmp_path)
        if log_path.is_file():
            rows = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            executed_rows = [r for r in rows if r.get("trigger") == "autonomous_execution"]
            assert len(executed_rows) == 0, "skipped proposal must not write a rec_log row"

    def test_disabled_executor_no_rec_log(self, tmp_path):
        """When executor returns status=disabled (advisory mode), no rec_log row."""
        cfg = _cfg(tmp_path)

        mock_result = {"status": "disabled", "detail": "alpaca disabled"}
        with patch(
            "tradingagents.integrations.alpaca.executor.execute_proposal",
            return_value=mock_result,
        ):
            from tradingagents.portfolio_advisor import proposals

            proposals.add(
                cfg,
                ticker="MSFT",
                action="sell",
                shares=3,
            )

        log_path = _rec_log_path(tmp_path)
        if log_path.is_file():
            rows = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            executed_rows = [r for r in rows if r.get("trigger") == "autonomous_execution"]
            assert len(executed_rows) == 0


# ---------------------------------------------------------------------------
# 2. shadow_book_add — correct shape
# ---------------------------------------------------------------------------

class TestShadowBookAdd:
    def test_shadow_book_add_writes_correct_shape(self, tmp_path):
        """shadow_book_add writes a correctly-shaped open row to shadow_book.jsonl."""
        cfg = _cfg(tmp_path)

        mock_hist = MagicMock()
        mock_hist.__len__ = lambda self: 3
        mock_hist.__getitem__ = lambda self, key: MagicMock(iloc=MagicMock(__getitem__=lambda s, i: 150.0))

        with patch("yfinance.Ticker") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.history.return_value = mock_hist
            mock_yf.return_value = mock_ticker

            from tradingagents.portfolio_advisor.candidates import shadow_book_add, shadow_book_path

            result = shadow_book_add(
                cfg,
                ticker="NVDA",
                source="ep_scanner",
                reason="gap+volume",
                strategy="catalyst",
                catalyst_date="2026-07-01",
                gates_passed=["liquidity", "thesis"],
            )

        assert result is not None, "shadow_book_add should return the written row"
        path = shadow_book_path(cfg)
        assert path.is_file()
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        assert len(rows) == 1
        row = rows[0]
        assert row["ticker"] == "NVDA"
        assert row["side"] == "open"
        assert row["source"] == "ep_scanner"
        assert row["strategy"] == "catalyst"
        assert row["gates_passed"] == ["liquidity", "thesis"]
        assert "entry_price" in row
        assert "ts" in row

    def test_shadow_book_add_no_duplicate(self, tmp_path):
        """Second call for the same ticker returns None (already tracked)."""
        cfg = _cfg(tmp_path)

        mock_hist = MagicMock()
        mock_hist.__len__ = lambda self: 3
        mock_hist.__getitem__ = lambda self, key: MagicMock(iloc=MagicMock(__getitem__=lambda s, i: 100.0))

        with patch("yfinance.Ticker") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.history.return_value = mock_hist
            mock_yf.return_value = mock_ticker

            from tradingagents.portfolio_advisor.candidates import shadow_book_add

            first = shadow_book_add(cfg, ticker="AMD", source="test", entry_price=100.0)
            second = shadow_book_add(cfg, ticker="AMD", source="test", entry_price=100.0)

        assert first is not None
        assert second is None, "duplicate open position must be skipped"

    def test_shadow_book_add_with_entry_price(self, tmp_path):
        """When entry_price is provided directly, yfinance is not called."""
        cfg = _cfg(tmp_path)

        with patch("yfinance.Ticker") as mock_yf:
            from tradingagents.portfolio_advisor.candidates import shadow_book_add

            result = shadow_book_add(
                cfg,
                ticker="GOOG",
                source="pead_scanner",
                entry_price=175.0,
                gates_passed=["rvol", "dollar_adv"],
            )
            # yfinance should NOT be called since entry_price was supplied
            mock_yf.assert_not_called()

        assert result is not None
        assert result["entry_price"] == 175.0


# ---------------------------------------------------------------------------
# 3. shadow_outcomes — score ≥30d rows, skip <30d rows
# ---------------------------------------------------------------------------

class TestShadowOutcomes:
    def _write_shadow_open(self, path: Path, ticker: str, ts: datetime) -> None:
        row = {
            "ts": ts.isoformat(),
            "ticker": ticker,
            "side": "open",
            "entry_price": 100.0,
            "shares": 5.0,
            "notional": 500.0,
            "status": "watch",
            "strategy": "core",
            "source": "test",
            "reason": "test",
            "gates_passed": ["liquidity"],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")

    def test_scores_31d_old_row(self, tmp_path):
        """A shadow-book open row that is 31 days old gets scored."""
        cfg = _cfg(tmp_path)
        shadow_path = tmp_path / "shadow_book.jsonl"
        old_ts = datetime.now(timezone.utc) - timedelta(days=31)
        self._write_shadow_open(shadow_path, "AAPL", old_ts)

        # Mock yfinance to return 2 data points so the scoring logic works.
        import pandas as pd
        idx = pd.date_range("2026-01-01", periods=2, freq="D")
        mock_hist = pd.DataFrame({"Close": [100.0, 110.0]}, index=idx)

        spy_hist = pd.DataFrame({"Close": [400.0, 404.0]}, index=idx)

        call_count = [0]
        def fake_history(*a, ticker_name="", **kw):
            call_count[0] += 1
            if ticker_name == "SPY":
                return spy_hist
            return mock_hist

        with patch("yfinance.Ticker") as mock_yf:
            def ticker_side_effect(sym):
                t = MagicMock()
                if sym == "SPY":
                    t.history.return_value = spy_hist
                else:
                    t.history.return_value = mock_hist
                return t
            mock_yf.side_effect = ticker_side_effect

            from tradingagents.portfolio_advisor.candidates import shadow_outcomes

            summary = shadow_outcomes(cfg)

        assert summary["scored"] == 1, f"Expected 1 scored, got {summary}"
        outcomes_path = tmp_path / "shadow_outcomes.jsonl"
        assert outcomes_path.is_file()
        rows = [json.loads(l) for l in outcomes_path.read_text().splitlines() if l.strip()]
        assert len(rows) == 1
        row = rows[0]
        assert row["ticker"] == "AAPL"
        assert "raw_return" in row
        assert "spy_return" in row
        assert "alpha_vs_spy" in row

    def test_skips_5d_old_row(self, tmp_path):
        """A shadow-book open row that is only 5 days old is NOT scored."""
        cfg = _cfg(tmp_path)
        shadow_path = tmp_path / "shadow_book.jsonl"
        young_ts = datetime.now(timezone.utc) - timedelta(days=5)
        self._write_shadow_open(shadow_path, "TSLA", young_ts)

        import pandas as pd
        idx = pd.date_range("2026-01-01", periods=2, freq="D")
        mock_hist = pd.DataFrame({"Close": [200.0, 210.0]}, index=idx)

        with patch("yfinance.Ticker") as mock_yf:
            t = MagicMock()
            t.history.return_value = mock_hist
            mock_yf.return_value = t

            from tradingagents.portfolio_advisor.candidates import shadow_outcomes
            summary = shadow_outcomes(cfg)

        assert summary["skipped_too_young"] == 1
        assert summary["scored"] == 0

    def test_shadow_outcomes_idempotent(self, tmp_path):
        """Running shadow_outcomes twice does not double-score the same row."""
        cfg = _cfg(tmp_path)
        shadow_path = tmp_path / "shadow_book.jsonl"
        old_ts = datetime.now(timezone.utc) - timedelta(days=35)
        self._write_shadow_open(shadow_path, "NVDA", old_ts)

        import pandas as pd
        idx = pd.date_range("2026-01-01", periods=2, freq="D")
        mock_hist = pd.DataFrame({"Close": [300.0, 315.0]}, index=idx)
        spy_hist = pd.DataFrame({"Close": [450.0, 454.0]}, index=idx)

        with patch("yfinance.Ticker") as mock_yf:
            def ticker_se(sym):
                t = MagicMock()
                t.history.return_value = spy_hist if sym == "SPY" else mock_hist
                return t
            mock_yf.side_effect = ticker_se

            from tradingagents.portfolio_advisor.candidates import shadow_outcomes
            s1 = shadow_outcomes(cfg)
            s2 = shadow_outcomes(cfg)

        assert s1["scored"] == 1
        assert s2["scored"] == 0, "Second run must skip already-scored row"


# ---------------------------------------------------------------------------
# 4. record_daily_nav — once per day, second call is no-op
# ---------------------------------------------------------------------------

class TestRecordDailyNav:
    def _make_mock_alpaca(self, equity=100000.0, cash=20000.0, n_positions=3):
        mock_account = MagicMock()
        mock_account.equity = equity
        mock_account.cash = cash

        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        mock_client.get_all_positions.return_value = [MagicMock()] * n_positions
        return mock_client

    def test_writes_nav_once(self, tmp_path):
        """record_daily_nav writes one row to nav_history.jsonl."""
        cfg = _cfg(tmp_path)
        mock_client = self._make_mock_alpaca()

        import pandas as pd
        idx = pd.date_range("2026-01-01", periods=1, freq="D")
        spy_hist = pd.DataFrame({"Close": [540.0]}, index=idx)

        with patch(
            "tradingagents.integrations.alpaca.executor.enabled",
            return_value=True,
        ), patch(
            "tradingagents.integrations.alpaca.executor._client",
            return_value=mock_client,
        ), patch("yfinance.Ticker") as mock_yf:
            t = MagicMock()
            t.history.return_value = spy_hist
            mock_yf.return_value = t

            from tradingagents.portfolio_advisor.outcome_tracker import record_daily_nav
            result = record_daily_nav(cfg)

        assert result is not None
        nav_path = tmp_path / "nav_history.jsonl"
        assert nav_path.is_file()
        rows = [json.loads(l) for l in nav_path.read_text().splitlines() if l.strip()]
        assert len(rows) == 1
        row = rows[0]
        assert row["equity"] == 100000.0
        assert row["cash"] == 20000.0
        assert row["positions_count"] == 3
        assert "date" in row
        assert "spy_close" in row

    def test_second_call_same_day_noop(self, tmp_path):
        """Second call on the same calendar day returns None (no duplicate row)."""
        cfg = _cfg(tmp_path)
        mock_client = self._make_mock_alpaca()

        import pandas as pd
        idx = pd.date_range("2026-01-01", periods=1, freq="D")
        spy_hist = pd.DataFrame({"Close": [540.0]}, index=idx)

        with patch(
            "tradingagents.integrations.alpaca.executor.enabled",
            return_value=True,
        ), patch(
            "tradingagents.integrations.alpaca.executor._client",
            return_value=mock_client,
        ), patch("yfinance.Ticker") as mock_yf:
            t = MagicMock()
            t.history.return_value = spy_hist
            mock_yf.return_value = t

            from tradingagents.portfolio_advisor.outcome_tracker import record_daily_nav
            first = record_daily_nav(cfg)
            second = record_daily_nav(cfg)

        assert first is not None
        assert second is None, "Second call same day must be a no-op"

        nav_path = tmp_path / "nav_history.jsonl"
        rows = [json.loads(l) for l in nav_path.read_text().splitlines() if l.strip()]
        assert len(rows) == 1, "Only one row should be written per day"

    def test_disabled_executor_skips(self, tmp_path):
        """When executor is disabled, record_daily_nav returns None without writing."""
        cfg = _cfg(tmp_path)

        with patch(
            "tradingagents.integrations.alpaca.executor.enabled",
            return_value=False,
        ):
            from tradingagents.portfolio_advisor.outcome_tracker import record_daily_nav
            result = record_daily_nav(cfg)

        assert result is None
        nav_path = tmp_path / "nav_history.jsonl"
        assert not nav_path.is_file()


# ---------------------------------------------------------------------------
# 5. CLI measure-outcomes smoke test
# ---------------------------------------------------------------------------

class TestMeasureOutcomesCLI:
    def test_cli_measure_outcomes_smoke(self, tmp_path):
        """CLI measure-outcomes command completes without error when there are no rows."""
        from typer.testing import CliRunner
        from cli.main import app

        cfg_patch = {"portfolio_advisor_dir": str(tmp_path)}

        with patch(
            "cli.advisor_cmd.DEFAULT_CONFIG",
            cfg_patch,
        ), patch(
            "tradingagents.portfolio_advisor.outcome_tracker.compute_recommendation_outcomes",
            return_value={
                "due": 0, "measured": 0, "skipped_no_ticker": 0,
                "skipped_no_price": 0, "good": 0, "bad": 0, "neutral": 0,
            },
        ), patch(
            "tradingagents.portfolio_advisor.candidates.shadow_outcomes",
            return_value={
                "total_open": 0, "scored": 0,
                "skipped_too_young": 0, "skipped_no_price": 0,
            },
        ), patch(
            "tradingagents.portfolio_advisor.recommendation_log.human_override_analysis",
            return_value={
                "followed_count": 0, "followed_avg_pnl": None,
                "overrode_count": 0, "overrode_avg_pnl": None,
                "total_measured": 0,
            },
        ):
            runner = CliRunner()
            result = runner.invoke(app, ["advisor", "portfolio", "measure-outcomes"])

        assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"
        assert "measure-outcomes" in result.output
