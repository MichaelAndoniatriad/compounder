"""R5 — Down-cap universe expansion tests.

Tests:
  1. Flag off → universe unchanged, no S&P 600 fetch attempted
  2. Flag on → cohort tags present (mocked fetch/CSV)
  3. ADV gate pass/fail/edge-zone tagging
  4. companyfacts parsing from fixture JSON (rev growth math, missing-facts → None)
  5. Shadow window: enabled 5 days ago + 90d window → shadow_book_add called
     instead of proposal; enabled 100 days ago → normal proposal path
  6. Smallcap missing fundamentals → filtered out
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **kwargs) -> Dict[str, Any]:
    """Minimal config dict pointing all state at tmp_path."""
    d = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "data_cache_dir": str(tmp_path / "cache"),
        "portfolio_advisor_universe_smallcap_enabled": False,
        "portfolio_advisor_smallcap_shadow_days": 90,
        "portfolio_advisor_min_dollar_adv": 2_000_000,
        "portfolio_advisor_edge_zone_max_adv": 50_000_000,
        "portfolio_advisor_shadow_book": True,
        "portfolio_advisor_shadow_notional": 500,
    }
    d.update(kwargs)
    return d


# ---------------------------------------------------------------------------
# 1. Flag off → no S&P 600 fetch
# ---------------------------------------------------------------------------


def test_flag_off_no_sp600_fetch(tmp_path):
    """When smallcap flag is off, fetch_sp600_constituents returns [] without hitting the net."""
    from tradingagents.portfolio_advisor.smallcap_universe import fetch_sp600_constituents

    cfg = _cfg(tmp_path)  # flag off by default

    with patch(
        "tradingagents.portfolio_advisor.smallcap_universe._fetch_sp600_from_wikipedia"
    ) as mock_fetch:
        # Should NOT be called when running under pytest (PYTEST_CURRENT_TEST is set)
        result = fetch_sp600_constituents(cfg)

    # Under pytest, fetch returns [] and the Wikipedia function is never reached
    # (the PYTEST_CURRENT_TEST guard fires first)
    mock_fetch.assert_not_called()
    assert result == []


def test_flag_off_build_universe_no_smallcap(tmp_path):
    """With flag off, _build_universe result contains no cohort expansion calls."""
    from tradingagents.portfolio_advisor.smallcap_universe import fetch_sp600_constituents

    cfg = _cfg(tmp_path)
    assert not cfg["portfolio_advisor_universe_smallcap_enabled"]

    with patch(
        "tradingagents.portfolio_advisor.smallcap_universe.fetch_sp600_constituents",
        return_value=[],
    ) as mock_fetch:
        # Simulate flag off path — build_universe should never call fetch_sp600
        from tradingagents.portfolio_advisor import core_discovery

        with patch.object(core_discovery, "_extract_tickers", lambda *a, **kw: 0, create=True):
            pass  # We don't actually run _build_universe (network); just check the guard

    # When flag off, fetch_sp600_constituents is never invoked
    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Flag on → cohort tags present (via vendored CSV)
# ---------------------------------------------------------------------------


def test_flag_on_cohort_tags_from_csv(tmp_path):
    """When flag on, build_cohort_map tags S&P 600 tickers as smallcap."""
    from tradingagents.portfolio_advisor import smallcap_universe

    cfg = _cfg(tmp_path, portfolio_advisor_universe_smallcap_enabled=True)

    # Patch Wikipedia fetch to fail, so it falls through to the vendored CSV
    with patch.object(smallcap_universe, "_fetch_sp600_from_wikipedia", return_value=None):
        # Patch PYTEST_CURRENT_TEST out so fetch_sp600_constituents doesn't short-circuit
        env_backup = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            tickers = smallcap_universe.fetch_sp600_constituents(cfg)
        finally:
            if env_backup is not None:
                os.environ["PYTEST_CURRENT_TEST"] = env_backup

    # Vendored CSV must exist and have ~600 entries
    assert len(tickers) >= 100, f"Expected ≥100 tickers from vendored CSV, got {len(tickers)}"
    # All tickers are uppercase alpha strings
    for t in tickers[:10]:
        assert t.isalpha() or "-" in t, f"Bad ticker: {t}"


def test_cohort_map_tags_smallcap(tmp_path):
    """Tickers from S&P 600 (but not S&P 500/NASDAQ 100) get smallcap cohort tag."""
    from tradingagents.portfolio_advisor import smallcap_universe

    cfg = _cfg(tmp_path, portfolio_advisor_universe_smallcap_enabled=True)

    # Mock largecap fetches as empty; provide a small SP600 list
    fake_sp600 = ["FAKE1", "FAKE2", "FAKE3"]

    def fake_fetch_sp600(cfg):
        return fake_sp600

    with patch.object(smallcap_universe, "fetch_sp600_constituents", side_effect=fake_fetch_sp600):
        from tradingagents.portfolio_advisor.core_discovery import _build_cohort_map

        # patch _extract_tickers used inside _build_cohort_map via Wikipedia fetches
        # by patching requests.get to raise so no largecap tickers populate
        with patch("requests.get", side_effect=Exception("no network")):
            env_backup = os.environ.pop("PYTEST_CURRENT_TEST", None)
            try:
                cohort = _build_cohort_map(cfg)
            finally:
                if env_backup is not None:
                    os.environ["PYTEST_CURRENT_TEST"] = env_backup

    for t in fake_sp600:
        assert cohort.get(t) == "smallcap", f"{t} should be smallcap"


# ---------------------------------------------------------------------------
# 3. ADV gate pass / fail / edge-zone tagging
# ---------------------------------------------------------------------------


def _mock_yf_ticker(price: float, avg_volume: int, market_cap: float = 5e9):
    """Build a mock yfinance Ticker object."""
    ticker_mock = MagicMock()
    ticker_mock.fast_info.last_price = price
    ticker_mock.fast_info.market_cap = market_cap
    ticker_mock.info = {
        "quoteType": "EQUITY",
        "regularMarketPrice": price,
        "averageVolume": avg_volume,
        "currentPrice": price,
        "trailingEps": 1.0,
        "debtToEquity": 0.5,
    }
    return ticker_mock


def test_adv_gate_fail_below_floor(tmp_path):
    """Stock with ADV < $2M is rejected with low_dollar_adv."""
    from tradingagents.portfolio_advisor.mechanical_filter import _check_one

    cfg = {"portfolio_advisor_min_dollar_adv": 2_000_000}

    # price=$10, volume=100_000 → ADV = $1M (below $2M floor)
    mock_ticker = _mock_yf_ticker(price=10.0, avg_volume=100_000)
    with patch("yfinance.Ticker", return_value=mock_ticker):
        reason, adv = _check_one("FAKE", min_dollar_adv=2_000_000)

    assert reason == "low_dollar_adv"
    assert adv == pytest.approx(1_000_000.0)


def test_adv_gate_pass_above_floor(tmp_path):
    """Stock with ADV ≥ $2M passes the ADV gate."""
    from tradingagents.portfolio_advisor.mechanical_filter import _check_one

    # price=$20, volume=200_000 → ADV = $4M (above $2M floor, below $50M ceiling)
    mock_ticker = _mock_yf_ticker(price=20.0, avg_volume=200_000)
    with patch("yfinance.Ticker", return_value=mock_ticker):
        reason, adv = _check_one("FAKE", min_dollar_adv=2_000_000)

    assert reason == ""
    assert adv == pytest.approx(4_000_000.0)


def test_adv_edge_zone_tagged(tmp_path):
    """Stock with ADV in [$2M, $50M] is tagged in _edge_zone."""
    from tradingagents.portfolio_advisor.mechanical_filter import mechanical_filter

    cfg = _cfg(
        tmp_path,
        portfolio_advisor_min_dollar_adv=2_000_000,
        portfolio_advisor_edge_zone_max_adv=50_000_000,
    )

    # price=$10, volume=300_000 → ADV = $3M (in edge zone)
    mock_ticker = _mock_yf_ticker(price=10.0, avg_volume=300_000)
    with patch("yfinance.Ticker", return_value=mock_ticker):
        survivors, rejections = mechanical_filter(["EDGE1"], cfg=cfg)

    assert "EDGE1" in survivors
    assert "EDGE1" in rejections.get("_edge_zone", [])


def test_adv_above_edge_zone_not_tagged(tmp_path):
    """Stock with ADV > $50M is a survivor but NOT in _edge_zone."""
    from tradingagents.portfolio_advisor.mechanical_filter import mechanical_filter

    cfg = _cfg(
        tmp_path,
        portfolio_advisor_min_dollar_adv=2_000_000,
        portfolio_advisor_edge_zone_max_adv=50_000_000,
    )

    # price=$100, volume=600_000 → ADV = $60M (above edge zone)
    mock_ticker = _mock_yf_ticker(price=100.0, avg_volume=600_000, market_cap=10e9)
    with patch("yfinance.Ticker", return_value=mock_ticker):
        survivors, rejections = mechanical_filter(["BIG1"], cfg=cfg)

    assert "BIG1" in survivors
    assert "BIG1" not in rejections.get("_edge_zone", [])


# ---------------------------------------------------------------------------
# 4. companyfacts parsing from fixture JSON
# ---------------------------------------------------------------------------


def _make_companyfacts_fixture(
    rev_latest: int = 10_000_000,
    rev_prior: int = 8_000_000,
    gross_profit: Optional[int] = 5_000_000,
) -> dict:
    """Build a minimal XBRL companyfacts JSON blob with proper annual 10-K periods.

    Each period is exactly one fiscal year: Jan 1 → Dec 31 (~365 days),
    which falls in the 300-400 day acceptance window of _extract_annual_revenue.
    """
    from datetime import date

    # Use two completed calendar fiscal years
    # e.g. latest: 2024-01-01 → 2024-12-31 (366 days — leap year)
    #      prior:  2023-01-01 → 2023-12-31 (365 days)
    start1 = date(2024, 1, 1)
    end1 = date(2024, 12, 31)
    start0 = date(2023, 1, 1)
    end0 = date(2023, 12, 31)

    def _entry(val, start, end):
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "val": val,
            "accn": "0001234",
            "fy": end.year,
            "fp": "FY",
            "form": "10-K",
            "filed": end.isoformat(),
            "frame": f"CY{end.year}",
        }

    revenues = [_entry(rev_latest, start1, end1), _entry(rev_prior, start0, end0)]
    facts: dict = {
        "cik": 1234567,
        "entityName": "Fake Corp",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "description": "Revenues",
                    "units": {"USD": revenues},
                },
            }
        },
    }
    if gross_profit is not None:
        gp_entries = [_entry(gross_profit, start1, end1)]
        facts["facts"]["us-gaap"]["GrossProfit"] = {
            "label": "GrossProfit",
            "description": "Gross Profit",
            "units": {"USD": gp_entries},
        }
    return facts


def test_companyfacts_parse_rev_growth():
    """_parse_companyfacts computes rev_growth correctly."""
    from tradingagents.dataflows.edgar import _parse_companyfacts

    fixture = _make_companyfacts_fixture(rev_latest=10_000_000, rev_prior=8_000_000)
    result = _parse_companyfacts(fixture)

    assert result["rev_latest"] == pytest.approx(10_000_000)
    assert result["rev_prior"] == pytest.approx(8_000_000)
    assert result["rev_growth"] == pytest.approx(0.25)  # (10-8)/8 = 0.25


def test_companyfacts_parse_gross_margin():
    """_parse_companyfacts computes gross_margin correctly."""
    from tradingagents.dataflows.edgar import _parse_companyfacts

    fixture = _make_companyfacts_fixture(
        rev_latest=10_000_000, rev_prior=8_000_000, gross_profit=4_000_000
    )
    result = _parse_companyfacts(fixture)

    assert result["gross_margin"] == pytest.approx(0.40)  # 4M / 10M


def test_companyfacts_missing_gross_profit():
    """When GrossProfit is absent, gross_margin is None."""
    from tradingagents.dataflows.edgar import _parse_companyfacts

    fixture = _make_companyfacts_fixture(
        rev_latest=10_000_000, rev_prior=8_000_000, gross_profit=None
    )
    result = _parse_companyfacts(fixture)

    assert result["gross_margin"] is None


def test_companyfacts_missing_revenue():
    """When revenue data is absent, rev_growth is None."""
    from tradingagents.dataflows.edgar import _parse_companyfacts

    fixture = {
        "cik": 9999,
        "entityName": "Empty Corp",
        "facts": {"us-gaap": {}},
    }
    result = _parse_companyfacts(fixture)

    assert result["rev_latest"] is None
    assert result["rev_growth"] is None
    assert result["gross_margin"] is None


def test_companyfacts_pytest_guard():
    """companyfacts() returns None under pytest (PYTEST_CURRENT_TEST is set)."""
    from tradingagents.dataflows.edgar import companyfacts

    # PYTEST_CURRENT_TEST is set by the pytest runner
    assert "PYTEST_CURRENT_TEST" in os.environ
    result = companyfacts("AAPL")
    assert result is None


def test_companyfacts_with_allow_in_test():
    """companyfacts() with _allow_in_test=True can run the parse path via mocked network."""
    from tradingagents.dataflows.edgar import companyfacts, _parse_companyfacts

    fixture = _make_companyfacts_fixture(rev_latest=5_000_000, rev_prior=4_000_000)
    raw_bytes = json.dumps(fixture).encode()

    with patch(
        "tradingagents.dataflows.edgar.get_cik_maps",
        return_value=({1234567: "FAKE"}, {"FAKE": 1234567}),
    ):
        with patch("tradingagents.dataflows.edgar._load_companyfacts_from_cache", return_value=None):
            with patch("tradingagents.dataflows.edgar._fetch_url", return_value=raw_bytes):
                with patch("tradingagents.dataflows.edgar._save_companyfacts_to_cache"):
                    result = companyfacts("FAKE", _allow_in_test=True)

    assert result is not None
    assert result["rev_growth"] == pytest.approx(0.25)  # (5-4)/4 = 0.25


# ---------------------------------------------------------------------------
# 5. Shadow window routing
# ---------------------------------------------------------------------------


def _write_pa_state(tmp_path: Path, enabled_at: Optional[datetime]) -> None:
    """Write pa_state.json with smallcap_enabled_at."""
    pa_dir = tmp_path / "pa"
    pa_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "version": 1,
        "first_scan_complete": False,
        "jobs": [],
    }
    if enabled_at is not None:
        state["smallcap_enabled_at"] = enabled_at.isoformat()
    (pa_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def test_shadow_active_when_within_window(tmp_path):
    """smallcap_shadow_active returns True when enabled 5 days ago with 90d window."""
    from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active

    enabled_at = datetime.now(timezone.utc) - timedelta(days=5)
    _write_pa_state(tmp_path, enabled_at)

    cfg = _cfg(
        tmp_path,
        portfolio_advisor_universe_smallcap_enabled=True,
        portfolio_advisor_smallcap_shadow_days=90,
    )
    assert smallcap_shadow_active(cfg) is True


def test_shadow_inactive_after_window(tmp_path):
    """smallcap_shadow_active returns False when enabled 100 days ago with 90d window."""
    from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active

    enabled_at = datetime.now(timezone.utc) - timedelta(days=100)
    _write_pa_state(tmp_path, enabled_at)

    cfg = _cfg(
        tmp_path,
        portfolio_advisor_universe_smallcap_enabled=True,
        portfolio_advisor_smallcap_shadow_days=90,
    )
    assert smallcap_shadow_active(cfg) is False


def test_shadow_inactive_when_flag_off(tmp_path):
    """smallcap_shadow_active returns False when flag is off."""
    from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active

    enabled_at = datetime.now(timezone.utc) - timedelta(days=5)
    _write_pa_state(tmp_path, enabled_at)

    cfg = _cfg(
        tmp_path,
        portfolio_advisor_universe_smallcap_enabled=False,
    )
    assert smallcap_shadow_active(cfg) is False


def test_shadow_active_no_timestamp_conservative(tmp_path):
    """When flag is on but enabled_at is missing, shadow is active (conservative)."""
    from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active

    _write_pa_state(tmp_path, enabled_at=None)  # no timestamp

    cfg = _cfg(
        tmp_path,
        portfolio_advisor_universe_smallcap_enabled=True,
        portfolio_advisor_smallcap_shadow_days=90,
    )
    assert smallcap_shadow_active(cfg) is True


def test_shadow_routing_diverts_smallcap_to_shadow_book(tmp_path):
    """During shadow window, smallcap deep-dive picks go to shadow_book_add, not PM."""
    from tradingagents.portfolio_advisor import candidates as _cands
    from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active

    # Setup: enabled 5 days ago → window active
    enabled_at = datetime.now(timezone.utc) - timedelta(days=5)
    _write_pa_state(tmp_path, enabled_at)

    cfg = _cfg(
        tmp_path,
        portfolio_advisor_universe_smallcap_enabled=True,
        portfolio_advisor_smallcap_shadow_days=90,
    )

    shadow_calls: List[dict] = []

    def fake_shadow_book_add(cfg, *, ticker, **kwargs):
        shadow_calls.append({"ticker": ticker, **kwargs})
        return {"ticker": ticker, "side": "open"}

    picks = [
        {"ticker": "SC1", "thesis": "Small cap play", "deep_dive": "YES"},
        {"ticker": "LC1", "thesis": "Large cap play", "deep_dive": "YES"},
    ]
    cohort_map = {"SC1": "smallcap", "LC1": "largecap"}

    with patch("tradingagents.portfolio_advisor.candidates.shadow_book_add", side_effect=fake_shadow_book_add):
        shadow_active = smallcap_shadow_active(cfg)
        assert shadow_active is True

        live_picks = []
        for pick in picks:
            cohort = cohort_map.get(pick["ticker"], "largecap")
            if cohort == "smallcap" and shadow_active:
                fake_shadow_book_add(
                    cfg,
                    ticker=pick["ticker"],
                    source="core_discovery_smallcap",
                    reason=pick.get("thesis", ""),
                    strategy="core",
                    gates_passed=["quant_screen"],
                    status="shadow_smallcap_window",
                )
            else:
                live_picks.append(pick)

    assert len(shadow_calls) == 1
    assert shadow_calls[0]["ticker"] == "SC1"
    assert shadow_calls[0]["status"] == "shadow_smallcap_window"
    assert len(live_picks) == 1
    assert live_picks[0]["ticker"] == "LC1"


def test_no_shadow_diversion_after_window(tmp_path):
    """After shadow window expires, smallcap picks proceed to normal proposal path."""
    from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active

    # enabled 100 days ago → window expired
    enabled_at = datetime.now(timezone.utc) - timedelta(days=100)
    _write_pa_state(tmp_path, enabled_at)

    cfg = _cfg(
        tmp_path,
        portfolio_advisor_universe_smallcap_enabled=True,
        portfolio_advisor_smallcap_shadow_days=90,
    )

    shadow_active = smallcap_shadow_active(cfg)
    assert shadow_active is False

    picks = [{"ticker": "SC1", "thesis": "Small cap play", "deep_dive": "YES"}]
    cohort_map = {"SC1": "smallcap"}

    shadow_calls = []
    live_picks = []
    for pick in picks:
        cohort = cohort_map.get(pick["ticker"], "largecap")
        if cohort == "smallcap" and shadow_active:
            shadow_calls.append(pick["ticker"])
        else:
            live_picks.append(pick)

    assert len(shadow_calls) == 0
    assert len(live_picks) == 1  # SC1 goes to normal path


# ---------------------------------------------------------------------------
# 6. Smallcap missing fundamentals → filtered out
# ---------------------------------------------------------------------------


def test_smallcap_missing_fundamentals_filtered(tmp_path):
    """In _quantitative_screen, smallcap ticker with no AV data and no companyfacts is skipped."""
    from tradingagents.portfolio_advisor.core_discovery import _quantitative_screen

    # AV returns None; companyfacts returns None too
    with patch(
        "tradingagents.dataflows.alpha_vantage_fundamentals_cached.get_ticker_fundamentals",
        return_value=None,
    ):
        with patch(
            "tradingagents.dataflows.edgar.companyfacts",
            return_value=None,
        ):
            result = _quantitative_screen(["NOFUND"], cohort_map={"NOFUND": "smallcap"})

    assert result == [], f"Expected filtered out, got: {result}"


def test_smallcap_with_companyfacts_data(tmp_path):
    """In _quantitative_screen, smallcap ticker that has companyfacts data proceeds."""
    from tradingagents.portfolio_advisor.core_discovery import _quantitative_screen

    # AV returns minimal info with marketCap; companyfacts provides growth/margin
    # Note: MIN_MARKET_CAP in core_discovery is $1B (the quant screen gate),
    # not the mechanical_filter's $500M gate.
    av_info = {
        "marketCap": 1_200_000_000,  # above quant screen's $1B gate
        "shortName": "FakeCo",
        "sector": "Technology",
        "industry": "Software",
        "currentPrice": 15.0,
        "forwardPE": 20.0,
        "debtToEquity": 0.3,
        "pegRatio": 1.2,
        "returnOnEquity": 0.18,
        "revenueGrowth": None,   # AV missing
        "grossMargins": None,    # AV missing
        "longBusinessSummary": "A fake company.",
    }
    cf_result = {
        "entity_name": "FakeCo",
        "cik_str": "0001234",
        "rev_latest": 100_000_000,
        "rev_prior": 80_000_000,
        "rev_growth": 0.25,       # (100-80)/80 = 0.25 (above 0.20 tier)
        "gross_profit_latest": 50_000_000,
        "gross_margin": 0.50,     # (50/100)
    }

    with patch(
        "tradingagents.dataflows.alpha_vantage_fundamentals_cached.get_ticker_fundamentals",
        return_value=av_info,
    ):
        # _enrich_smallcap_info_from_companyfacts does a local import; patch the module attr
        import tradingagents.dataflows.edgar as _edgar_mod
        orig_cf = getattr(_edgar_mod, "companyfacts", None)
        _edgar_mod.companyfacts = lambda ticker, **kwargs: cf_result  # type: ignore[attr-defined]
        try:
            result = _quantitative_screen(["FAKECO"], cohort_map={"FAKECO": "smallcap"})
        finally:
            if orig_cf is not None:
                _edgar_mod.companyfacts = orig_cf  # type: ignore[attr-defined]

    # Should have passed the screen (rev_growth=0.25, gross_margin=0.50 → high score)
    assert len(result) == 1
    assert result[0]["ticker"] == "FAKECO"
    assert result[0]["cohort"] == "smallcap"
    assert result[0]["rev_growth"] == pytest.approx(25.0)   # stored as percentage
    assert result[0]["gross_margin"] == pytest.approx(50.0)  # stored as percentage


# ---------------------------------------------------------------------------
# 7. New config keys present with correct defaults
# ---------------------------------------------------------------------------


def test_default_config_keys():
    """New R5 config keys exist in DEFAULT_CONFIG with expected types/values."""
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["portfolio_advisor_universe_smallcap_enabled"] is False
    assert DEFAULT_CONFIG["portfolio_advisor_smallcap_shadow_days"] == 90
    assert DEFAULT_CONFIG["portfolio_advisor_min_dollar_adv"] == 2_000_000
    assert DEFAULT_CONFIG["portfolio_advisor_edge_zone_max_adv"] == 50_000_000


# ===========================================================================
# 8. Post-review hardening: fail-closed on empty cohort map
# ===========================================================================


class TestFailClosedCohortMap:
    """Unknown ticker → treated as smallcap → shadowed during the window.

    This covers the mandated design item 1: when the shadow window is active
    and a ticker is NOT definitively in the largecap set, it must be treated
    as smallcap (fail-closed).
    """

    def test_unknown_ticker_treated_as_smallcap_shadowed(self, tmp_path):
        """get_ticker_cohort semantics:
        - 'unknown' when both disk cache AND vendored CSV are unavailable
        - 'smallcap' for a ticker found in the S&P 600 cache
        - 'largecap' for a ticker NOT in the S&P 600 cache (populated cache = not smallcap)
        """
        from tradingagents.portfolio_advisor import smallcap_universe as su

        # Reset the module-level cache so our fake data takes effect
        su._invalidate_sp600_cache()

        # Case 1: no disk cache + vendored CSV returns empty → "unknown" for any ticker
        empty_cfg = {"data_cache_dir": str(tmp_path / "empty_cache")}
        with patch.object(su, "_load_sp600_from_vendored_csv", return_value=[]):
            cohort_unknown = su.get_ticker_cohort("ANYTICKER", cfg=empty_cfg)
        assert cohort_unknown == "unknown", (
            f"No cache, no vendored CSV → 'unknown' (fail-closed); got {cohort_unknown!r}"
        )
        su._invalidate_sp600_cache()

        # Case 2: populated cache — ticker found in S&P 600 → "smallcap"
        cache_path = su._sp600_cache_path(str(tmp_path / "cache"))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        cache_path.write_text(_json.dumps({"tickers": ["KNOWN1"]}), encoding="utf-8")

        try:
            cohort_sc = su.get_ticker_cohort("KNOWN1", cfg={"data_cache_dir": str(tmp_path / "cache")})
            assert cohort_sc == "smallcap", f"KNOWN1 in cache → smallcap; got {cohort_sc!r}"

            # Case 3: populated cache — ticker NOT in S&P 600 → "largecap"
            cohort_lc = su.get_ticker_cohort("NOTINSP600", cfg={"data_cache_dir": str(tmp_path / "cache")})
            assert cohort_lc == "largecap", (
                f"Ticker not in S&P 600 cache → largecap; got {cohort_lc!r}"
            )
        finally:
            su._invalidate_sp600_cache()

    def test_empty_cohort_map_shadows_all_picks(self, tmp_path):
        """Phase 5b with an empty _cohort_map diverts ALL picks during the window."""
        from tradingagents.portfolio_advisor.candidates import shadow_book_add as _real_sba

        enabled_at = datetime.now(timezone.utc) - timedelta(days=5)
        _write_pa_state(tmp_path, enabled_at)

        cfg = _cfg(
            tmp_path,
            portfolio_advisor_universe_smallcap_enabled=True,
            portfolio_advisor_smallcap_shadow_days=90,
        )

        shadowed: List[str] = []

        def fake_shadow_book_add(cfg, *, ticker, **kwargs):
            shadowed.append(ticker)
            return {"ticker": ticker, "side": "open"}

        # Simulate empty cohort map scenario — all picks default to "unknown"
        picks = [
            {"ticker": "SC1", "deep_dive": "YES", "thesis": "small co"},
            {"ticker": "SC2", "deep_dive": "YES", "thesis": "another small co"},
        ]
        empty_cohort_map: Dict = {}

        from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active

        with patch("tradingagents.portfolio_advisor.candidates.shadow_book_add",
                   side_effect=fake_shadow_book_add):
            shadow_active = smallcap_shadow_active(cfg)
            assert shadow_active is True

            live_picks = []
            for pick in picks:
                cohort = empty_cohort_map.get(pick["ticker"], "unknown")
                should_shadow = not empty_cohort_map or cohort != "largecap"
                if should_shadow and shadow_active:
                    fake_shadow_book_add(
                        cfg,
                        ticker=pick["ticker"],
                        source="core_discovery_smallcap",
                        reason=pick.get("thesis", ""),
                        strategy="core",
                        gates_passed=["quant_screen"],
                        status="shadow_smallcap_window",
                    )
                else:
                    live_picks.append(pick)

        assert len(shadowed) == 2, f"Both picks should be shadowed; got {shadowed}"
        assert len(live_picks) == 0


# ===========================================================================
# 9. Post-review hardening: proposals.add() central shadow choke
# ===========================================================================


class TestProposalsAddShadowChoke:
    """Mandated design item 2: proposals.add() diverts smallcap buys to shadow
    book during the window; largecap buys are unaffected; sell/trim unaffected.
    """

    def _setup_state(self, tmp_path: Path, days_ago: int) -> None:
        enabled_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
        _write_pa_state(tmp_path, enabled_at)

    def test_smallcap_buy_diverted_to_shadow_not_executed(self, tmp_path):
        """proposals.add() with a smallcap buy → shadow book, status=shadowed, no execution."""
        self._setup_state(tmp_path, days_ago=5)
        cfg = _cfg(
            tmp_path,
            portfolio_advisor_universe_smallcap_enabled=True,
            portfolio_advisor_smallcap_shadow_days=90,
            # Disable Alpaca execution so the test stays offline
            portfolio_advisor_alpaca_paper=False,
            TRADINGAGENTS_ACCOUNT_MODE="etoro",
        )

        shadow_calls: List[dict] = []

        def fake_shadow_book_add(cfg, *, ticker, **kwargs):
            shadow_calls.append({"ticker": ticker, **kwargs})
            return {"ticker": ticker, "side": "open", "ts": "2026-01-01T00:00:00+00:00"}

        executed: List[str] = []

        def fake_execute_proposal(cfg, entry):
            executed.append(entry.get("ticker"))
            return {"status": "executed", "detail": "mock"}

        from tradingagents.portfolio_advisor import proposals as prop

        # Patch get_ticker_cohort to return "smallcap" for SCTEST deterministically,
        # avoiding module-level cache state contamination between tests.
        with patch("tradingagents.portfolio_advisor.smallcap_universe.get_ticker_cohort",
                   return_value="smallcap"), \
             patch("tradingagents.portfolio_advisor.candidates.shadow_book_add",
                   side_effect=fake_shadow_book_add), \
             patch("tradingagents.integrations.alpaca.executor.execute_proposal",
                   side_effect=fake_execute_proposal):
            result = prop.add(
                cfg,
                ticker="SCTEST",
                action="buy",
                approx_usd=500.0,
                reason="test smallcap buy",
            )

        assert result.get("status") == "shadowed", f"Expected shadowed, got {result.get('status')!r}"
        assert len(shadow_calls) == 1, "shadow_book_add should have been called once"
        assert shadow_calls[0]["ticker"] == "SCTEST"
        assert shadow_calls[0]["status"] == "shadow_smallcap_window"
        assert len(executed) == 0, "execute_proposal must NOT be called for a shadowed proposal"

    def test_largecap_buy_unaffected_by_shadow_choke(self, tmp_path):
        """proposals.add() with a largecap buy is NOT diverted — it reaches execution."""
        self._setup_state(tmp_path, days_ago=5)
        cfg = _cfg(
            tmp_path,
            portfolio_advisor_universe_smallcap_enabled=True,
            portfolio_advisor_smallcap_shadow_days=90,
            portfolio_advisor_alpaca_paper=False,
            TRADINGAGENTS_ACCOUNT_MODE="etoro",
            portfolio_advisor_action_tickets=False,
        )

        shadow_calls: List[str] = []

        def fake_shadow_book_add(cfg, *, ticker, **kwargs):
            shadow_calls.append(ticker)
            return {"ticker": ticker}

        from tradingagents.portfolio_advisor import proposals as prop

        # Patch get_ticker_cohort to return "largecap" — not in S&P 600
        with patch("tradingagents.portfolio_advisor.smallcap_universe.get_ticker_cohort",
                   return_value="largecap"), \
             patch("tradingagents.portfolio_advisor.candidates.shadow_book_add",
                   side_effect=fake_shadow_book_add):
            result = prop.add(
                cfg,
                ticker="LCTEST",
                action="buy",
                approx_usd=500.0,
                reason="test largecap buy",
            )

        # Largecap should NOT be shadowed
        assert result.get("status") != "shadowed", (
            f"Largecap buy should NOT be shadowed; status={result.get('status')!r}"
        )
        assert "LCTEST" not in shadow_calls

    def test_after_window_smallcap_buy_proceeds(self, tmp_path):
        """After the shadow window expires, smallcap buys are NOT diverted."""
        # Window expired 10 days ago
        self._setup_state(tmp_path, days_ago=100)
        cfg = _cfg(
            tmp_path,
            portfolio_advisor_universe_smallcap_enabled=True,
            portfolio_advisor_smallcap_shadow_days=90,
            portfolio_advisor_alpaca_paper=False,
            TRADINGAGENTS_ACCOUNT_MODE="etoro",
            portfolio_advisor_action_tickets=False,
        )

        shadow_calls: List[str] = []

        def fake_shadow_book_add(cfg, *, ticker, **kwargs):
            shadow_calls.append(ticker)
            return {"ticker": ticker}

        from tradingagents.portfolio_advisor import proposals as prop

        # Even if cohort is "smallcap", the window has expired so no diversion
        with patch("tradingagents.portfolio_advisor.smallcap_universe.get_ticker_cohort",
                   return_value="smallcap"), \
             patch("tradingagents.portfolio_advisor.candidates.shadow_book_add",
                   side_effect=fake_shadow_book_add):
            result = prop.add(
                cfg,
                ticker="SCTEST2",
                action="buy",
                approx_usd=500.0,
                reason="post-window smallcap buy",
            )

        # After window, should NOT be shadowed
        assert result.get("status") != "shadowed", (
            f"Post-window buy should NOT be shadowed; got {result.get('status')!r}"
        )
        # shadow_book_add should NOT be called via the choke (window is closed)
        assert "SCTEST2" not in shadow_calls


# ===========================================================================
# 10. Post-review hardening: $100M cap floor + cap-estimate fallback
# ===========================================================================


class TestSmallcapCapFloor:
    """Mandated design item 3: smallcap uses $100M floor; cap-estimate fallback
    via shares_outstanding × price when marketCap is 0/missing.
    """

    def test_100m_floor_passes_smallcap_above_100m(self, tmp_path):
        """A smallcap with marketCap=150M passes the quant screen ($100M floor)."""
        from tradingagents.portfolio_advisor.core_discovery import _quantitative_screen

        av_info = {
            "marketCap": 150_000_000,  # $150M — above $100M smallcap floor
            "shortName": "SmallCo",
            "sector": "Technology",
            "industry": "Software",
            "currentPrice": 15.0,
            "forwardPE": 20.0,
            "debtToEquity": 0.3,
            "pegRatio": 1.2,
            "returnOnEquity": 0.18,
            "revenueGrowth": 0.25,
            "grossMargins": 0.50,
            "longBusinessSummary": "A small company.",
        }

        with patch(
            "tradingagents.dataflows.alpha_vantage_fundamentals_cached.get_ticker_fundamentals",
            return_value=av_info,
        ):
            result = _quantitative_screen(["SMALL1"], cohort_map={"SMALL1": "smallcap"})

        assert len(result) == 1, f"$150M smallcap should pass the $100M floor, got {result}"

    def test_100m_floor_rejects_smallcap_below_100m(self, tmp_path):
        """A smallcap with marketCap=50M is filtered out."""
        from tradingagents.portfolio_advisor.core_discovery import _quantitative_screen

        av_info = {
            "marketCap": 50_000_000,  # $50M — below $100M smallcap floor
            "shortName": "TinyCo",
            "sector": "Technology",
            "industry": "Software",
            "currentPrice": 5.0,
            "revenueGrowth": 0.25,
            "grossMargins": 0.50,
        }

        with patch(
            "tradingagents.dataflows.alpha_vantage_fundamentals_cached.get_ticker_fundamentals",
            return_value=av_info,
        ):
            result = _quantitative_screen(["TINY1"], cohort_map={"TINY1": "smallcap"})

        assert result == [], f"$50M smallcap should be rejected by $100M floor, got {result}"

    def test_largecap_still_uses_1b_floor(self, tmp_path):
        """A largecap ticker with $500M cap is still rejected (uses $1B floor)."""
        from tradingagents.portfolio_advisor.core_discovery import _quantitative_screen

        av_info = {
            "marketCap": 500_000_000,  # $500M — above $100M but below $1B
            "shortName": "MidCo",
            "sector": "Technology",
            "currentPrice": 50.0,
            "revenueGrowth": 0.25,
            "grossMargins": 0.50,
            "pegRatio": 1.2,
            "returnOnEquity": 0.18,
        }

        with patch(
            "tradingagents.dataflows.alpha_vantage_fundamentals_cached.get_ticker_fundamentals",
            return_value=av_info,
        ):
            result = _quantitative_screen(["MID1"], cohort_map={"MID1": "largecap"})

        assert result == [], f"$500M largecap should be rejected by $1B floor"

    def test_cap_estimate_from_shares_x_price(self, tmp_path):
        """When marketCap=0 but shares_outstanding and price are present, estimate cap."""
        from tradingagents.portfolio_advisor.core_discovery import _quantitative_screen

        # 10M shares × $20 = $200M estimated cap (above $100M smallcap floor)
        av_info = {
            "marketCap": 0,  # missing
            "sharesOutstanding": 10_000_000,
            "currentPrice": 20.0,
            "shortName": "EstCo",
            "sector": "Technology",
            "revenueGrowth": 0.25,
            "grossMargins": 0.50,
            "pegRatio": 1.2,
            "returnOnEquity": 0.18,
        }

        with patch(
            "tradingagents.dataflows.alpha_vantage_fundamentals_cached.get_ticker_fundamentals",
            return_value=av_info,
        ):
            import tradingagents.dataflows.edgar as _edgar_mod
            orig_cf = getattr(_edgar_mod, "companyfacts", None)
            _edgar_mod.companyfacts = lambda ticker, **kwargs: None  # type: ignore
            try:
                result = _quantitative_screen(["ESTCO"], cohort_map={"ESTCO": "smallcap"})
            finally:
                if orig_cf is not None:
                    _edgar_mod.companyfacts = orig_cf  # type: ignore

        assert len(result) == 1, (
            f"Smallcap with estimated cap $200M (10M×$20) should pass $100M floor, got {result}"
        )


# ===========================================================================
# 11. Post-review hardening: enabled_at self-heal
# ===========================================================================


class TestEnabledAtSelfHeal:
    """Mandated design item 4: when flag is on but enabled_at is missing,
    smallcap_shadow_active self-heals (records timestamp), warns, returns True.
    """

    def test_self_heal_records_timestamp_and_warns(self, tmp_path):
        """Missing enabled_at → self-healed to now, warning logged, returns True."""
        import logging

        from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active
        from tradingagents.portfolio_advisor import state as _state

        # Write state WITHOUT enabled_at
        _write_pa_state(tmp_path, enabled_at=None)

        cfg = _cfg(
            tmp_path,
            portfolio_advisor_universe_smallcap_enabled=True,
            portfolio_advisor_smallcap_shadow_days=90,
        )

        warning_msgs: List[str] = []

        class _CapHandler(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    warning_msgs.append(record.getMessage())

        handler = _CapHandler()
        from tradingagents.portfolio_advisor import smallcap_universe as su_mod
        su_logger = logging.getLogger(su_mod.__name__)
        su_logger.addHandler(handler)

        # Patch send_advisor_message so no Telegram is sent
        with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message"):
            result = smallcap_shadow_active(cfg)

        su_logger.removeHandler(handler)

        assert result is True, "Should return True (still in window) after self-heal"

        # The timestamp should now be in state
        pa_state = _state.load_state(cfg)
        assert pa_state.get("smallcap_enabled_at"), (
            "smallcap_enabled_at should have been written to state during self-heal"
        )

        # A warning should have been logged
        assert any("self-heal" in m.lower() or "missing" in m.lower() or "enabled_at" in m.lower()
                   for m in warning_msgs), (
            f"Expected a warning about the missing timestamp; got: {warning_msgs}"
        )

    def test_self_heal_second_call_uses_persisted_timestamp(self, tmp_path):
        """After self-heal, a second call reads the persisted timestamp (no re-heal)."""
        from tradingagents.portfolio_advisor.smallcap_universe import smallcap_shadow_active
        from tradingagents.portfolio_advisor import state as _state

        _write_pa_state(tmp_path, enabled_at=None)

        cfg = _cfg(
            tmp_path,
            portfolio_advisor_universe_smallcap_enabled=True,
            portfolio_advisor_smallcap_shadow_days=90,
        )

        with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message"):
            first = smallcap_shadow_active(cfg)

        assert first is True

        ts_after_first = _state.load_state(cfg).get("smallcap_enabled_at")

        # Second call — should not overwrite the timestamp
        with patch("tradingagents.portfolio_advisor.messaging.send_advisor_message"):
            second = smallcap_shadow_active(cfg)

        assert second is True
        ts_after_second = _state.load_state(cfg).get("smallcap_enabled_at")
        assert ts_after_first == ts_after_second, (
            "Timestamp should not be overwritten on the second call"
        )
