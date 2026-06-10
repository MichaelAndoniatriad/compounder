"""Tests for tradingagents.portfolio_advisor.pead_scanner (R3).

All live-API calls are mocked. Tests never touch ~/.tradingagents — every
file write is sandboxed via the cfg['portfolio_advisor_dir'] fixture.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.portfolio_advisor.pead_scanner import (
    _compute_sue,
    _parse_calendar_csv,
    refresh_calendar,
    scan_post_reports,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_CALENDAR_CSV = """\
symbol,name,reportDate,fiscalDateEnding,estimate,currency
AAPL,Apple Inc,{yesterday},2024-09-30,1.50,USD
MSFT,Microsoft Corp,{yesterday},2024-09-30,3.10,USD
TSLA,Tesla Inc,{in_two_weeks},2024-09-30,0.50,USD
"""

FIXTURE_EPS_RESPONSE_POSITIVE = {
    "symbol": "AAPL",
    "quarterlyEarnings": [
        {
            "fiscalDateEnding": "2024-09-30",
            "reportedDate": "2024-10-31",
            "reportedEPS": "1.80",
            "estimatedEPS": "1.50",
            "surprise": "0.30",
            "surprisePercentage": "20.0",
        }
    ],
}

FIXTURE_EPS_RESPONSE_NEGATIVE_SURPRISE = {
    "symbol": "MSFT",
    "quarterlyEarnings": [
        {
            "fiscalDateEnding": "2024-09-30",
            "reportedDate": "2024-10-31",
            "reportedEPS": "2.90",
            "estimatedEPS": "3.10",
            "surprise": "-0.20",
            "surprisePercentage": "-6.45",
        }
    ],
}

FIXTURE_EPS_RESPONSE_ZERO_ESTIMATE = {
    "symbol": "ZEROEST",
    "quarterlyEarnings": [
        {
            "fiscalDateEnding": "2024-09-30",
            "reportedDate": "2024-10-31",
            "reportedEPS": "0.50",
            "estimatedEPS": "0.00",
            "surprise": "",
            "surprisePercentage": "",
        }
    ],
}

FIXTURE_EPS_RESPONSE_NEGATIVE_ESTIMATE = {
    "symbol": "NEGEST",
    "quarterlyEarnings": [
        {
            "fiscalDateEnding": "2024-09-30",
            "reportedDate": "2024-10-31",
            "reportedEPS": "-0.10",
            "estimatedEPS": "-0.40",
            "surprise": "0.30",
            "surprisePercentage": "75.0",
        }
    ],
}


def _make_cfg(tmp_path: Path) -> Dict[str, Any]:
    return {"portfolio_advisor_dir": str(tmp_path / "pa")}


def _yf_pv_pass(ticker: str, lookback_days: int = 25) -> Dict[str, Any]:
    """Mocked yfinance price/volume — all gates pass."""
    return {
        "open": 152.0,
        "close": 153.0,
        "prior_close": 140.0,   # gap ~8.6% open vs prior_close
        "today_volume": 50_000_000,
        "avg_vol_20d": 25_000_000,
        "price": 153.0,
    }


def _yf_pv_no_gap(ticker: str, lookback_days: int = 25) -> Dict[str, Any]:
    """Mocked — open ≤ prior close (gate 2 fails)."""
    return {
        "open": 139.0,
        "close": 140.0,
        "prior_close": 140.0,
        "today_volume": 50_000_000,
        "avg_vol_20d": 25_000_000,
        "price": 140.0,
    }


def _yf_pv_low_rvol(ticker: str, lookback_days: int = 25) -> Dict[str, Any]:
    """Mocked — RVOL < 1.5 (gate 3 fails)."""
    return {
        "open": 152.0,
        "close": 153.0,
        "prior_close": 140.0,
        "today_volume": 10_000_000,
        "avg_vol_20d": 25_000_000,  # rvol = 0.4
        "price": 153.0,
    }


def _yf_pv_low_dollar_vol(ticker: str, lookback_days: int = 25) -> Dict[str, Any]:
    """Mocked — dollar vol < $5M (gate 4 fails): price $153 × vol 10k = $1.53M."""
    return {
        "open": 152.0,
        "close": 153.0,
        "prior_close": 140.0,
        "today_volume": 10_000,
        "avg_vol_20d": 5_000,
        "price": 153.0,
    }


# ---------------------------------------------------------------------------
# Calendar CSV parsing
# ---------------------------------------------------------------------------

class TestParseCalendarCsv:
    def test_parses_symbols(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        in_two_weeks = (date.today() + timedelta(days=14)).isoformat()
        csv_text = FIXTURE_CALENDAR_CSV.format(yesterday=yesterday, in_two_weeks=in_two_weeks)
        rows = _parse_calendar_csv(csv_text)
        assert len(rows) == 3
        symbols = [r["symbol"] for r in rows]
        assert "AAPL" in symbols
        assert "TSLA" in symbols

    def test_empty_csv_returns_empty_list(self):
        assert _parse_calendar_csv("") == []
        assert _parse_calendar_csv("   \n  ") == []

    def test_header_only_returns_empty_list(self):
        assert _parse_calendar_csv("symbol,name,reportDate,fiscalDateEnding,estimate,currency\n") == []

    def test_symbol_uppercased(self):
        csv_text = "symbol,name,reportDate,fiscalDateEnding,estimate,currency\naapl,Apple,2024-10-31,,1.5,USD\n"
        rows = _parse_calendar_csv(csv_text)
        assert rows[0]["symbol"] == "AAPL"

    def test_report_date_preserved(self):
        csv_text = "symbol,name,reportDate,fiscalDateEnding,estimate,currency\nAAPL,Apple,2024-10-31,,1.5,USD\n"
        rows = _parse_calendar_csv(csv_text)
        assert rows[0]["report_date"] == "2024-10-31"


# ---------------------------------------------------------------------------
# SUE computation edge cases
# ---------------------------------------------------------------------------

class TestComputeSue:
    def test_positive_surprise(self):
        sue, reason = _compute_sue("1.80", "1.50")
        assert reason == ""
        assert abs(sue - 20.0) < 0.01

    def test_negative_surprise(self):
        sue, reason = _compute_sue("1.20", "1.50")
        assert reason == ""
        assert sue < 0

    def test_zero_estimate_returns_none(self):
        sue, reason = _compute_sue("0.50", "0.00")
        assert sue is None
        assert "zero" in reason.lower()

    def test_zero_estimate_float(self):
        sue, reason = _compute_sue("0.50", "0.0")
        assert sue is None

    def test_negative_estimate_valid(self):
        # estimate=-0.40, actual=-0.10 → surprise = (-0.10 - (-0.40)) / |-0.40| × 100 = 75%
        sue, reason = _compute_sue("-0.10", "-0.40")
        assert reason == ""
        assert abs(sue - 75.0) < 0.01

    def test_non_numeric_actual(self):
        sue, reason = _compute_sue("N/A", "1.50")
        assert sue is None
        assert "non-numeric" in reason.lower()

    def test_non_numeric_estimate(self):
        sue, reason = _compute_sue("1.50", "N/A")
        assert sue is None
        assert "non-numeric" in reason.lower()

    def test_exact_match_zero_surprise(self):
        sue, reason = _compute_sue("1.50", "1.50")
        assert reason == ""
        assert sue == 0.0

    def test_large_positive_surprise(self):
        sue, reason = _compute_sue("3.00", "1.00")
        assert reason == ""
        assert sue == 200.0


# ---------------------------------------------------------------------------
# Gate tests (mocked yfinance + AV)
# ---------------------------------------------------------------------------

def _run_scan_unguarded(cfg, eps_responses, yf_pv_fn):
    """Run scan_post_reports with PYTEST_CURRENT_TEST cleared so the entry-point
    guard is bypassed (all live-API calls are replaced by the provided mocks)."""
    env_copy = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}

    with (
        patch.dict(os.environ, env_copy, clear=True),
        patch(
            "tradingagents.portfolio_advisor.pead_scanner._fetch_earnings_eps",
            side_effect=lambda sym: eps_responses.get(sym),
        ),
        patch(
            "tradingagents.portfolio_advisor.pead_scanner._yf_price_volume",
            side_effect=yf_pv_fn,
        ),
        patch("tradingagents.portfolio_advisor.candidates.evaluate_candidate",
              return_value=MagicMock(status="watch", ticker=list(eps_responses.keys())[0])),
        patch("tradingagents.portfolio_advisor.candidates.append_candidate_records"),
        patch("tradingagents.portfolio_advisor.candidates.shadow_book_add"),
    ):
        return scan_post_reports(cfg)


class TestGates:
    """Test each gate individually via scan_post_reports."""

    def _run_scan(self, tmp_path, eps_responses, yf_pv_fn, report_date=None):
        """Helper: write a calendar with one reporter and run scan_post_reports."""
        if report_date is None:
            report_date = (date.today() - timedelta(days=1)).isoformat()
        cfg = _make_cfg(tmp_path)
        # Write a minimal calendar cache
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        calendar_payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "symbol": sym,
                    "name": sym,
                    "report_date": report_date,
                    "fiscal_date_ending": "2024-09-30",
                    "estimate": "1.50",
                    "currency": "USD",
                }
                for sym in eps_responses
            ],
        }
        (pa_dir / "earnings_calendar.json").write_text(
            json.dumps(calendar_payload), encoding="utf-8"
        )
        return _run_scan_unguarded(cfg, eps_responses, yf_pv_fn)

    def test_sue_gate_passes_at_exactly_10pct(self, tmp_path):
        eps = {
            "AAPL": {
                "reportedEPS": "1.65",
                "estimatedEPS": "1.50",
                "surprisePercentage": "10.0",
            }
        }
        result = self._run_scan(tmp_path, eps, _yf_pv_pass)
        assert any(c["ticker"] == "AAPL" for c in result["candidates"])

    def test_sue_gate_fails_below_10pct(self, tmp_path):
        eps = {
            "AAPL": {
                "reportedEPS": "1.54",
                "estimatedEPS": "1.50",
                "surprisePercentage": "2.67",
            }
        }
        result = self._run_scan(tmp_path, eps, _yf_pv_pass)
        assert not any(c["ticker"] == "AAPL" for c in result["candidates"])
        assert any(s["ticker"] == "AAPL" for s in result["skipped"])
        gate = next((g for g in result["gate_log"] if g["ticker"] == "AAPL"), {})
        assert gate.get("failed_at") == "sue_below_threshold"

    def test_gap_gate_fails_when_open_below_prior_close(self, tmp_path):
        eps = {
            "AAPL": {"reportedEPS": "1.80", "estimatedEPS": "1.50", "surprisePercentage": "20.0"}
        }
        result = self._run_scan(tmp_path, eps, _yf_pv_no_gap)
        assert not any(c["ticker"] == "AAPL" for c in result["candidates"])
        gate = next((g for g in result["gate_log"] if g["ticker"] == "AAPL"), {})
        assert gate.get("failed_at") == "gap_direction"

    def test_rvol_gate_fails_when_below_1_5(self, tmp_path):
        eps = {
            "AAPL": {"reportedEPS": "1.80", "estimatedEPS": "1.50", "surprisePercentage": "20.0"}
        }
        result = self._run_scan(tmp_path, eps, _yf_pv_low_rvol)
        assert not any(c["ticker"] == "AAPL" for c in result["candidates"])
        gate = next((g for g in result["gate_log"] if g["ticker"] == "AAPL"), {})
        assert gate.get("failed_at") == "rvol_below_threshold"

    def test_dollar_vol_gate_fails_when_below_5m(self, tmp_path):
        eps = {
            "AAPL": {"reportedEPS": "1.80", "estimatedEPS": "1.50", "surprisePercentage": "20.0"}
        }
        result = self._run_scan(tmp_path, eps, _yf_pv_low_dollar_vol)
        assert not any(c["ticker"] == "AAPL" for c in result["candidates"])
        gate = next((g for g in result["gate_log"] if g["ticker"] == "AAPL"), {})
        assert gate.get("failed_at") == "dollar_vol_below_threshold"

    def test_all_gates_pass_produces_candidate(self, tmp_path):
        eps = {
            "AAPL": {"reportedEPS": "1.80", "estimatedEPS": "1.50", "surprisePercentage": "20.0"}
        }
        result = self._run_scan(tmp_path, eps, _yf_pv_pass)
        assert any(c["ticker"] == "AAPL" for c in result["candidates"])
        cand = next(c for c in result["candidates"] if c["ticker"] == "AAPL")
        assert cand["sue_pct"] == pytest.approx(20.0, abs=0.01)

    def test_zero_estimate_skipped(self, tmp_path):
        """Zero estimate → SUE undefined → skip."""
        eps = {
            "ZEROEST": {
                "reportedEPS": "0.50",
                "estimatedEPS": "0.00",
                "surprisePercentage": "",
            }
        }
        result = self._run_scan(tmp_path, eps, _yf_pv_pass)
        assert not any(c["ticker"] == "ZEROEST" for c in result["candidates"])
        assert any(s["ticker"] == "ZEROEST" for s in result["skipped"])

    def test_no_eps_data_skipped(self, tmp_path):
        eps = {"AAPL": None}
        result = self._run_scan(tmp_path, eps, _yf_pv_pass)
        assert not any(c["ticker"] == "AAPL" for c in result["candidates"])
        gate = next((g for g in result["gate_log"] if g["ticker"] == "AAPL"), {})
        assert gate.get("failed_at") == "no_eps_data"


# ---------------------------------------------------------------------------
# Routing: evaluate_candidate is called with correct args
# ---------------------------------------------------------------------------

class TestRouting:
    def _write_calendar(self, pa_dir: Path, symbols: List[str], report_date: str) -> None:
        rows = [
            {"symbol": s, "name": s, "report_date": report_date,
             "fiscal_date_ending": "2024-09-30", "estimate": "1.50", "currency": "USD"}
            for s in symbols
        ]
        payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
        (pa_dir / "earnings_calendar.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_evaluate_candidate_called_with_catalyst_sleeve(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self._write_calendar(pa_dir, ["AAPL"], yesterday)

        captured_calls = []

        def fake_evaluate(raw, **kwargs):
            captured_calls.append(raw)
            return MagicMock(status="watch", ticker="AAPL")

        eps_data = {"AAPL": {"reportedEPS": "1.80", "estimatedEPS": "1.50", "surprisePercentage": "20.0"}}
        env_copy = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}

        with (
            patch.dict(os.environ, env_copy, clear=True),
            patch("tradingagents.portfolio_advisor.pead_scanner._fetch_earnings_eps", side_effect=lambda s: eps_data[s]),
            patch("tradingagents.portfolio_advisor.pead_scanner._yf_price_volume", side_effect=_yf_pv_pass),
            patch("tradingagents.portfolio_advisor.candidates.evaluate_candidate", side_effect=fake_evaluate),
            patch("tradingagents.portfolio_advisor.candidates.append_candidate_records"),
            patch("tradingagents.portfolio_advisor.candidates.shadow_book_add"),
        ):
            scan_post_reports(cfg)

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["ticker"] == "AAPL"
        assert call["strategy"] == "catalyst"
        assert call["source"] == "pead_scanner"
        assert call["catalyst_date"] == yesterday

    def test_shadow_book_add_called_for_non_proposal(self, tmp_path):
        """Gate-passers that don't become proposals → shadow_book_add called."""
        cfg = _make_cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self._write_calendar(pa_dir, ["AAPL"], yesterday)

        shadow_calls = []
        eps_data = {"AAPL": {"reportedEPS": "1.80", "estimatedEPS": "1.50", "surprisePercentage": "20.0"}}
        env_copy = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}

        with (
            patch.dict(os.environ, env_copy, clear=True),
            patch("tradingagents.portfolio_advisor.pead_scanner._fetch_earnings_eps", side_effect=lambda s: eps_data[s]),
            patch("tradingagents.portfolio_advisor.pead_scanner._yf_price_volume", side_effect=_yf_pv_pass),
            patch("tradingagents.portfolio_advisor.candidates.evaluate_candidate", return_value=MagicMock(status="watch", ticker="AAPL")),
            patch("tradingagents.portfolio_advisor.candidates.append_candidate_records"),
            patch("tradingagents.portfolio_advisor.candidates.shadow_book_add", side_effect=lambda cfg, **kw: shadow_calls.append(kw)),
        ):
            scan_post_reports(cfg)

        assert any(kw.get("ticker") == "AAPL" for kw in shadow_calls)
        call = next(kw for kw in shadow_calls if kw.get("ticker") == "AAPL")
        assert call["source"] == "pead_scanner"
        assert call["strategy"] == "catalyst"


# ---------------------------------------------------------------------------
# Calendar cache tests
# ---------------------------------------------------------------------------

class TestCalendarCache:
    def _env_no_pytest(self):
        return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}

    def test_refresh_calendar_writes_cache(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        csv_text = f"symbol,name,reportDate,fiscalDateEnding,estimate,currency\nAAPL,Apple,{yesterday},,1.5,USD\n"

        with (
            patch.dict(os.environ, self._env_no_pytest(), clear=True),
            patch("tradingagents.portfolio_advisor.pead_scanner._fetch_earnings_calendar", return_value=csv_text),
        ):
            result = refresh_calendar(cfg)

        assert result["symbols"] == 1
        assert result["cached"] is False
        cache_path = Path(cfg["portfolio_advisor_dir"]) / "earnings_calendar.json"
        assert cache_path.is_file()
        payload = json.loads(cache_path.read_text())
        assert len(payload["rows"]) == 1
        assert payload["rows"][0]["symbol"] == "AAPL"

    def test_refresh_calendar_reuses_fresh_cache(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        fresh = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "rows": [{"symbol": "AAPL", "name": "Apple", "report_date": "2024-10-31", "fiscal_date_ending": "", "estimate": "1.5", "currency": "USD"}],
        }
        (pa_dir / "earnings_calendar.json").write_text(json.dumps(fresh), encoding="utf-8")

        with (
            patch.dict(os.environ, self._env_no_pytest(), clear=True),
            patch("tradingagents.portfolio_advisor.pead_scanner._fetch_earnings_calendar") as mock_fetch,
        ):
            result = refresh_calendar(cfg)

        assert mock_fetch.call_count == 0
        assert result["cached"] is True
        assert result["symbols"] == 1

    def test_refresh_calendar_re_fetches_stale_cache(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        stale = {"fetched_at": stale_ts, "rows": []}
        (pa_dir / "earnings_calendar.json").write_text(json.dumps(stale), encoding="utf-8")

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        csv_text = f"symbol,name,reportDate,fiscalDateEnding,estimate,currency\nMSFT,Microsoft,{yesterday},,3.1,USD\n"

        with (
            patch.dict(os.environ, self._env_no_pytest(), clear=True),
            patch("tradingagents.portfolio_advisor.pead_scanner._fetch_earnings_calendar", return_value=csv_text) as mock_fetch,
        ):
            result = refresh_calendar(cfg)

        assert mock_fetch.call_count == 1
        assert result["cached"] is False
        assert result["symbols"] == 1


# ---------------------------------------------------------------------------
# max_names cap test
# ---------------------------------------------------------------------------

class TestMaxNamesCap:
    def test_max_names_limits_eps_calls(self, tmp_path):
        cfg = {**_make_cfg(tmp_path), "portfolio_advisor_pead_max_names": 2}
        pa_dir = Path(cfg["portfolio_advisor_dir"])
        pa_dir.mkdir(parents=True, exist_ok=True)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # Calendar has 5 reporters; cap=2 means at most 2 EPS calls
        rows = [
            {"symbol": s, "name": s, "report_date": yesterday,
             "fiscal_date_ending": "2024-09-30", "estimate": "1.0", "currency": "USD"}
            for s in ["AAPL", "MSFT", "TSLA", "NVDA", "AMZN"]
        ]
        calendar_payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
        (pa_dir / "earnings_calendar.json").write_text(json.dumps(calendar_payload), encoding="utf-8")

        eps_calls = []

        def fake_eps(sym):
            eps_calls.append(sym)
            return None  # no data → skip

        env_copy = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}

        with (
            patch.dict(os.environ, env_copy, clear=True),
            patch("tradingagents.portfolio_advisor.pead_scanner._fetch_earnings_eps", side_effect=fake_eps),
            patch("tradingagents.portfolio_advisor.pead_scanner._yf_price_volume", side_effect=_yf_pv_pass),
            patch("tradingagents.portfolio_advisor.candidates.evaluate_candidate"),
            patch("tradingagents.portfolio_advisor.candidates.append_candidate_records"),
            patch("tradingagents.portfolio_advisor.candidates.shadow_book_add"),
        ):
            scan_post_reports(cfg)

        assert len(eps_calls) == 2


# ---------------------------------------------------------------------------
# Pytest guard
# ---------------------------------------------------------------------------

class TestPytestGuard:
    """Entry points must short-circuit when PYTEST_CURRENT_TEST is set."""

    def test_refresh_calendar_skips_under_pytest(self, tmp_path):
        """PYTEST_CURRENT_TEST is already set (we're running under pytest)."""
        cfg = _make_cfg(tmp_path)
        # Should not raise and should not hit any live API
        result = refresh_calendar(cfg)
        assert result.get("skipped_pytest") is True

    def test_scan_post_reports_skips_under_pytest(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        result = scan_post_reports(cfg)
        assert result.get("skipped_pytest") is True
