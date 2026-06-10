"""Tests for R4: RVOL gate, pre-market honesty, and EDGAR 8-K feed.

Scope
-----
1. RVOL gate pass / fail / dollar-floor cases (mocked volume data).
2. Pre-market path:
   - market closed + no Alpaca quote → ticker is skipped (per-ticker path).
   - market closed + Alpaca quote available → proceeds with live price.
   - market open → unchanged (yfinance) path.
3. EDGAR module:
   - parse_recent_8k_filings: item filtering, CIK→ticker mapping, hours window.
   - fetch_recent_8k_items: returns [] under pytest by default.
   - build_cik_to_ticker / build_ticker_to_cik round-trip.
4. Fetcher registration: 'edgar' source is in _NEWS_SOURCE_REGISTRY.
5. _pull_news_edgar: returns feed-compatible dicts when edgar module returns filings.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Dict[str, Any]:
    return {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "ep_scanner_news_sources": ["alpha_vantage"],
    }


def _future_dt(hours: int = 2) -> str:
    """Return a UTC ISO datetime string in the future (for next_open)."""
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


def _past_dt(hours: int = 2) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.isoformat()


def _make_yf_quote(
    *,
    open_: float = 55.0,
    close: float = 55.0,
    prev_close: float = 50.0,
    vol_today: float = 4_000_000,
    avg_vol_20d: float = 1_000_000,
) -> Dict[str, Any]:
    """Build a synthetic _yf_quote return value."""
    return {
        "open": open_,
        "close": close,
        "prev_close": prev_close,
        "high_1mo": close * 1.05,
        "close_10d_ago": prev_close * 0.95,
        "above_50dma": True,
        "volume_today": vol_today,
        "avg_volume_20d": avg_vol_20d,
    }


def _make_hit(ticker: str = "NVDA") -> Dict[str, Any]:
    """Build a synthetic _bucket_by_ticker entry (hit)."""
    return {
        "ticker": ticker,
        "hint": "tier1",
        "relevance": 0.9,
        "ticker_sentiment": "Bullish",
        "title": f"Company {ticker} beats EPS estimates",
        "summary": "Strong quarter",
        "source": "alpha_vantage",
        "url": f"https://example.com/{ticker}",
        "time_published": "20260610T120000",
    }


# ---------------------------------------------------------------------------
# 1. RVOL gate: pass/fail/dollar-floor
# ---------------------------------------------------------------------------


class TestRvolGate:
    """Tests for Gate 4.5 (RVOL gate) inside scan_for_ep_candidates."""

    def _run_scan(self, yf_quote: Dict[str, Any], tmp_path: Path) -> Dict[str, Any]:
        """Run scan with a single mocked ticker NVDA and the given quote data."""
        from tradingagents.portfolio_advisor import ep_scanner, etoro_scan

        hit = _make_hit("NVDA")
        with (
            patch.object(ep_scanner, "_pull_news", return_value=[]),
            patch.object(ep_scanner, "_bucket_by_ticker", return_value=({
                "NVDA": hit,
            }, {})),
            patch.object(ep_scanner, "_yf_quote", return_value=yf_quote),
            patch.object(ep_scanner, "_market_disqualifiers", return_value={
                "spy_pct": 0.5, "vix": 18.0, "blocked": False, "reason": ""
            }),
            patch.object(ep_scanner, "_alpaca_live_price", return_value=None),
            # Mock holdings so NVDA is NOT treated as already held.
            patch.object(etoro_scan, "fetch_portfolio_rows",
                         return_value=({}, "", [], [])),
            patch.object(etoro_scan, "current_ticker_set", return_value=set()),
        ):
            return ep_scanner.scan_for_ep_candidates(
                _cfg(tmp_path),
                # Not in pre-market: clock override = open
                _market_clock_override={"is_open": True},
            )

    def test_rvol_pass(self, tmp_path):
        """vol 4M, avg 1M → RVOL=4x, dollar_vol=4M*55=$220M → pass."""
        q = _make_yf_quote(vol_today=4_000_000, avg_vol_20d=1_000_000, close=55.0)
        result = self._run_scan(q, tmp_path)
        assert result["candidates"], f"Expected candidate to pass, got skipped: {result['skipped']}"
        cand = result["candidates"][0]
        assert cand["ticker"] == "NVDA"
        assert cand["rvol"] >= 2.0
        assert cand["dollar_vol_m"] >= 5.0

    def test_rvol_fail_low_multiple(self, tmp_path):
        """vol 1.5M, avg 1M → RVOL=1.5x (< 2x) → fail."""
        q = _make_yf_quote(vol_today=1_500_000, avg_vol_20d=1_000_000, close=55.0)
        result = self._run_scan(q, tmp_path)
        assert not result["candidates"], "Expected candidate to be rejected by RVOL gate"
        skipped = result["skipped"]
        assert any("RVOL" in s["reason"] or "rvol" in s["reason"].lower() for s in skipped), (
            f"Expected RVOL skip reason, got: {skipped}"
        )
        gate_entries = [g for g in result["gate_log"] if g.get("t") == "NVDA"]
        assert gate_entries, "Expected gate_log entry for NVDA"
        gate = gate_entries[0]
        assert gate.get("failed_at") == "rvol"
        assert "rvol" in gate
        assert "dollar_vol" in gate

    def test_rvol_fail_dollar_floor(self, tmp_path):
        """vol 3M, avg 1M (RVOL=3x ok), but close=$1.50 → dollar_vol=$4.5M < $5M → fail."""
        q = _make_yf_quote(
            vol_today=3_000_000, avg_vol_20d=1_000_000,
            close=1.50, prev_close=1.35,  # price low → dollar vol too small
        )
        # Note: close < _MIN_PRICE=5.0 so it would fail min_price first.
        # To isolate dollar-floor, use a price just above $5 but small.
        q2 = _make_yf_quote(
            open_=5.50, close=5.50, prev_close=4.90,
            vol_today=500_000, avg_vol_20d=200_000,
        )
        # RVOL = 500k / 200k = 2.5x ✓; dollar_vol = 500k * 5.50 = $2.75M < $5M ✗
        result = self._run_scan(q2, tmp_path)
        assert not result["candidates"], "Expected rejection due to dollar volume floor"
        skipped = result["skipped"]
        assert any("dollar vol" in s["reason"] or "rvol" in s["reason"].lower() for s in skipped)
        gate_entries = [g for g in result["gate_log"] if g.get("t") == "NVDA"]
        gate = gate_entries[0]
        assert gate.get("failed_at") == "rvol"
        assert gate.get("dollar_vol") == pytest.approx(2_750_000.0, rel=0.01)

    def test_rvol_pass_logs_values(self, tmp_path):
        """When RVOL passes, gate_log includes rvol and dollar_vol values."""
        q = _make_yf_quote(vol_today=4_000_000, avg_vol_20d=1_000_000, close=55.0)
        result = self._run_scan(q, tmp_path)
        gate_entries = [g for g in result["gate_log"] if g.get("t") == "NVDA"]
        assert gate_entries
        gate = gate_entries[0]
        assert "rvol" in gate
        assert gate["rvol"] == pytest.approx(4.0, rel=0.01)
        assert "dollar_vol" in gate
        assert gate["dollar_vol"] > 0


# ---------------------------------------------------------------------------
# 2. Pre-market path tests
# ---------------------------------------------------------------------------


class TestPreMarketPath:
    """Tests for pre-market price honesty in scan_for_ep_candidates."""

    def _run_scan_with_clock(
        self,
        clock: Dict[str, Any],
        alpaca_price: Optional[float],
        tmp_path: Path,
        *,
        yf_quote: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from tradingagents.portfolio_advisor import ep_scanner, etoro_scan

        if yf_quote is None:
            yf_quote = _make_yf_quote(vol_today=4_000_000, avg_vol_20d=1_000_000, close=55.0)

        hit = _make_hit("AAPL")
        with (
            patch.object(ep_scanner, "_pull_news", return_value=[]),
            patch.object(ep_scanner, "_bucket_by_ticker", return_value=({"AAPL": hit}, {})),
            patch.object(ep_scanner, "_yf_quote", return_value=yf_quote),
            patch.object(ep_scanner, "_market_disqualifiers", return_value={
                "spy_pct": 0.5, "vix": 18.0, "blocked": False, "reason": ""
            }),
            patch.object(ep_scanner, "_alpaca_live_price", return_value=alpaca_price),
            patch.object(etoro_scan, "fetch_portfolio_rows",
                         return_value=({}, "", [], [])),
            patch.object(etoro_scan, "current_ticker_set", return_value=set()),
        ):
            return ep_scanner.scan_for_ep_candidates(
                _cfg(tmp_path),
                _market_clock_override=clock,
            )

    def test_market_closed_no_quote_ticker_skipped(self, tmp_path):
        """Market closed + Alpaca returns None → ticker skipped with pre-market reason."""
        clock = {"is_open": False, "next_open": _future_dt(hours=2)}
        result = self._run_scan_with_clock(clock, alpaca_price=None, tmp_path=tmp_path)
        assert not result["candidates"], "Expected no candidates — no live price"
        skipped_reasons = [s["reason"] for s in result["skipped"]]
        assert any("pre-market" in r or "pre_market" in r for r in skipped_reasons), (
            f"Expected pre-market skip reason, got: {skipped_reasons}"
        )

    def test_market_closed_with_quote_uses_live_price(self, tmp_path):
        """Market closed + Alpaca live price=60.0 (≥10% above prev_close=50) → candidate passes."""
        clock = {"is_open": False, "next_open": _future_dt(hours=2)}
        # yfinance has prev_close=50 (stale). Alpaca gives live 60 → 20% gap.
        yf_q = _make_yf_quote(
            open_=50.0, close=50.0, prev_close=50.0,
            vol_today=4_000_000, avg_vol_20d=1_000_000,
        )
        result = self._run_scan_with_clock(
            clock, alpaca_price=60.0, tmp_path=tmp_path, yf_quote=yf_q
        )
        assert result["candidates"], f"Expected candidate to pass, skipped: {result['skipped']}"
        cand = result["candidates"][0]
        assert cand["ticker"] == "AAPL"
        # The gap should be computed using live price (60) vs prev_close (50) → 20%.
        assert cand["gap_pct"] == pytest.approx(20.0, abs=0.5)
        # Gate log should record pre_market_live_px.
        gate_entries = [g for g in result["gate_log"] if g.get("t") == "AAPL"]
        assert gate_entries
        gate = gate_entries[0]
        assert "pre_market_live_px" in gate
        assert gate["pre_market_live_px"] == pytest.approx(60.0, abs=0.01)

    def test_market_open_uses_yfinance(self, tmp_path):
        """Market open → in_pre_market=False → yfinance prices used, no Alpaca call."""
        clock = {"is_open": True}
        yf_q = _make_yf_quote(
            open_=55.0, close=55.0, prev_close=50.0,
            vol_today=4_000_000, avg_vol_20d=1_000_000,
        )
        from tradingagents.portfolio_advisor import ep_scanner, etoro_scan
        alpaca_spy = MagicMock(return_value=999.0)  # should NOT be called

        hit = _make_hit("AAPL")
        with (
            patch.object(ep_scanner, "_pull_news", return_value=[]),
            patch.object(ep_scanner, "_bucket_by_ticker", return_value=({"AAPL": hit}, {})),
            patch.object(ep_scanner, "_yf_quote", return_value=yf_q),
            patch.object(ep_scanner, "_market_disqualifiers", return_value={
                "spy_pct": 0.5, "vix": 18.0, "blocked": False, "reason": ""
            }),
            patch.object(ep_scanner, "_alpaca_live_price", alpaca_spy),
            patch.object(etoro_scan, "fetch_portfolio_rows",
                         return_value=({}, "", [], [])),
            patch.object(etoro_scan, "current_ticker_set", return_value=set()),
        ):
            result = ep_scanner.scan_for_ep_candidates(
                _cfg(tmp_path),
                _market_clock_override={"is_open": True},
            )

        # Alpaca should NOT have been called since market is open.
        alpaca_spy.assert_not_called()
        # Candidate should pass using yfinance prices.
        assert result["candidates"]
        gate_entries = [g for g in result["gate_log"] if g.get("t") == "AAPL"]
        assert gate_entries
        gate = gate_entries[0]
        assert "pre_market_live_px" not in gate


# ---------------------------------------------------------------------------
# 3. EDGAR module: parsing and API
# ---------------------------------------------------------------------------


# Fixture: a minimal submissions JSON for CIK 1045810 (NVDA).
FIXTURE_SUBMISSIONS_JSON = {
    "cik": "1045810",
    "name": "NVIDIA CORP",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0001045810-26-000051",
                "0001045810-26-000042",
                "0001045810-26-000010",
            ],
            "filingDate": [
                # Recent (within 24h)
                (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d"),
                # Recent but wrong item
                (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d"),
                # Too old (48h ago)
                (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%d"),
            ],
            "reportDate": ["2026-05-20", "2026-05-18", "2026-05-01"],
            "acceptanceDateTime": [
                (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                (datetime.now(timezone.utc) - timedelta(hours=5)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
            ],
            "form": ["8-K", "8-K", "8-K"],
            "items": ["2.02,9.01", "5.02", "2.02"],  # [match, match, too old]
            "primaryDocument": ["nvda-20260520.htm", "nvda-20260518.htm", "nvda-20260501.htm"],
        }
    },
}


class TestEdgarParsing:
    """Unit tests for EDGAR parsing helpers — no network calls."""

    def test_build_cik_to_ticker(self):
        from tradingagents.dataflows.edgar import build_cik_to_ticker
        raw = {
            "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
            "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        }
        cik_to_tk = build_cik_to_ticker(raw)
        assert cik_to_tk[1045810] == "NVDA"
        assert cik_to_tk[320193] == "AAPL"

    def test_build_ticker_to_cik(self):
        from tradingagents.dataflows.edgar import build_ticker_to_cik
        raw = {
            "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
            "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        }
        tk_to_cik = build_ticker_to_cik(raw)
        assert tk_to_cik["NVDA"] == 1045810
        assert tk_to_cik["AAPL"] == 320193

    def test_parse_recent_8k_item_filtering(self):
        """Only 8-K entries with matching items within hours window are returned."""
        from tradingagents.dataflows.edgar import parse_recent_8k_filings

        cik_to_ticker = {1045810: "NVDA"}
        filings = parse_recent_8k_filings(
            FIXTURE_SUBMISSIONS_JSON,
            cik=1045810,
            cik_to_ticker=cik_to_ticker,
            hours=24,
            items={"2.02", "8.01", "5.02"},
        )
        # Filing 0 has items 2.02,9.01 — 2.02 matches → included.
        # Filing 1 has item 5.02 — matches → included.
        # Filing 2 is >24h old → excluded.
        tickers = [f["ticker"] for f in filings]
        items = [f["item"] for f in filings]
        assert all(t == "NVDA" for t in tickers)
        assert "2.02" in items
        assert "5.02" in items
        # All returned filings are within the 24h window.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for f in filings:
            filed = datetime.fromisoformat(f["filed_at"])
            if filed.tzinfo is None:
                filed = filed.replace(tzinfo=timezone.utc)
            assert filed >= cutoff, f"Filing {f} is outside the 24h window"

    def test_parse_hours_window_excludes_old(self):
        """With hours=1, only filings accepted within the last hour are returned."""
        from tradingagents.dataflows.edgar import parse_recent_8k_filings

        cik_to_ticker = {1045810: "NVDA"}
        filings = parse_recent_8k_filings(
            FIXTURE_SUBMISSIONS_JSON,
            cik=1045810,
            cik_to_ticker=cik_to_ticker,
            hours=1,
            items={"2.02"},
        )
        # Filing 0 is 2h old → excluded; filing 2 is 48h old → excluded.
        assert not filings or all(
            (datetime.now(timezone.utc) - datetime.fromisoformat(
                f["filed_at"].replace("Z", "+00:00")
                if not datetime.fromisoformat(f["filed_at"]).tzinfo
                else f["filed_at"]
            )).total_seconds() <= 3600 + 60  # 1h + 60s tolerance
            for f in filings
        )

    def test_parse_unknown_cik_returns_empty(self):
        """A CIK not in the map is dropped → empty result."""
        from tradingagents.dataflows.edgar import parse_recent_8k_filings

        cik_to_ticker = {999999: "FAKE"}  # 1045810 not present
        filings = parse_recent_8k_filings(
            FIXTURE_SUBMISSIONS_JSON,
            cik=1045810,
            cik_to_ticker=cik_to_ticker,
            hours=24,
            items={"2.02"},
        )
        assert filings == []

    def test_parse_url_construction(self):
        """URL is constructed from accession number and primary document."""
        from tradingagents.dataflows.edgar import parse_recent_8k_filings

        cik_to_ticker = {1045810: "NVDA"}
        filings = parse_recent_8k_filings(
            FIXTURE_SUBMISSIONS_JSON,
            cik=1045810,
            cik_to_ticker=cik_to_ticker,
            hours=24,
            items={"2.02"},
        )
        nvda_2_02 = [f for f in filings if f["item"] == "2.02"]
        assert nvda_2_02
        url = nvda_2_02[0]["url"]
        assert "sec.gov" in url
        assert "nvda" in url.lower() or "1045810" in url


class TestEdgarFetchPytestGuard:
    """fetch_recent_8k_items returns [] under pytest by default."""

    def test_returns_empty_by_default(self):
        from tradingagents.dataflows.edgar import fetch_recent_8k_items
        # PYTEST_CURRENT_TEST is set in the test environment.
        result = fetch_recent_8k_items(hours=24)
        assert result == []

    def test_allow_in_test_bypasses_guard_with_mocked_network(self):
        """With _allow_in_test=True, the guard is bypassed but we mock the network."""
        from tradingagents.dataflows.edgar import fetch_recent_8k_items, build_cik_to_ticker

        raw_map = {
            "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        }
        cik_to_tk = {1045810: "NVDA"}
        tk_to_cik = {"NVDA": 1045810}

        sub_json = FIXTURE_SUBMISSIONS_JSON

        with (
            patch(
                "tradingagents.dataflows.edgar.get_cik_maps",
                return_value=(cik_to_tk, tk_to_cik),
            ),
            patch(
                "tradingagents.dataflows.edgar._fetch_url",
                return_value=json.dumps(sub_json).encode(),
            ),
        ):
            results = fetch_recent_8k_items(
                hours=24,
                items={"2.02", "5.02"},
                tickers=["NVDA"],
                _allow_in_test=True,
            )

        # Should include filings within 24h with matching items.
        assert isinstance(results, list)
        tickers_found = [r["ticker"] for r in results]
        assert all(t == "NVDA" for t in tickers_found)
        items_found = [r["item"] for r in results]
        assert "2.02" in items_found or "5.02" in items_found


# ---------------------------------------------------------------------------
# 4. Fetcher registration: 'edgar' in _NEWS_SOURCE_REGISTRY
# ---------------------------------------------------------------------------


class TestEdgarFetcherRegistration:
    def test_edgar_in_registry(self):
        from tradingagents.portfolio_advisor.ep_scanner import _NEWS_SOURCE_REGISTRY
        assert "edgar" in _NEWS_SOURCE_REGISTRY, (
            "'edgar' must be registered in _NEWS_SOURCE_REGISTRY"
        )

    def test_edgar_fetcher_callable(self):
        from tradingagents.portfolio_advisor.ep_scanner import _NEWS_SOURCE_REGISTRY
        from datetime import date
        fetcher = _NEWS_SOURCE_REGISTRY["edgar"]
        # Under pytest, _pull_news_edgar calls edgar.fetch_recent_8k_items which returns [].
        result = fetcher(date.today(), 2, 200)
        assert isinstance(result, list)

    def test_edgar_not_in_default_config_sources(self):
        """'edgar' must NOT be in the default ep_scanner_news_sources list."""
        from tradingagents.default_config import DEFAULT_CONFIG
        sources = DEFAULT_CONFIG.get("ep_scanner_news_sources", [])
        assert "edgar" not in sources, (
            "'edgar' must not be in default ep_scanner_news_sources; "
            "users must opt in by adding it to their config"
        )


# ---------------------------------------------------------------------------
# 5. _pull_news_edgar: returns feed-compatible dicts
# ---------------------------------------------------------------------------


class TestPullNewsEdgar:
    def test_returns_feed_compatible_dicts_from_filings(self, tmp_path):
        """_pull_news_edgar returns dicts with title, source, ticker_sentiment."""
        from tradingagents.portfolio_advisor.ep_scanner import _pull_news_edgar
        from datetime import date

        mock_filings = [
            {
                "ticker": "NVDA",
                "item": "2.02",
                "title": "NVIDIA CORP",
                "filed_at": datetime.now(timezone.utc).isoformat(),
                "url": "https://www.sec.gov/archives/edgar/data/1045810/abc.htm",
            }
        ]

        with patch(
            "tradingagents.dataflows.edgar.fetch_recent_8k_items",
            return_value=mock_filings,
        ):
            result = _pull_news_edgar(date.today(), look_back_days=2, limit=50)

        assert len(result) == 1
        item = result[0]
        # Title should include the 8-K item number.
        assert "8-K item 2.02" in item["title"]
        assert "NVIDIA CORP" in item["title"]
        # Source identifies the feed.
        assert item["source"] == "edgar_8k"
        # ticker_sentiment must be a list with NVDA at relevance 1.0.
        ts = item["ticker_sentiment"]
        assert isinstance(ts, list)
        assert len(ts) == 1
        assert ts[0]["ticker"] == "NVDA"
        assert float(ts[0]["relevance_score"]) == pytest.approx(1.0)

    def test_empty_on_edgar_failure(self, tmp_path):
        """_pull_news_edgar returns [] if edgar module raises."""
        from tradingagents.portfolio_advisor.ep_scanner import _pull_news_edgar
        from datetime import date

        with patch(
            "tradingagents.dataflows.edgar.fetch_recent_8k_items",
            side_effect=RuntimeError("EDGAR unavailable"),
        ):
            result = _pull_news_edgar(date.today(), look_back_days=2, limit=50)

        assert result == []
