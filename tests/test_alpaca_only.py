"""Tests for the Alpaca-only / eToro hard-disable gate.

Coverage:
1. _etoro_enabled() — returns False by default (flag=False, not pytest) and True under pytest.
2. account_mode() — returns "alpaca" when flag=False, even if TRADINGAGENTS_ACCOUNT_MODE="etoro".
3. fetch_portfolio_rows() — eToro network path is never called when flag=False.
4. fetch_current_portfolio_headlines() — same gate.
5. fetch_clerk_watchlist_from_etoro() — same gate.
6. Even with flag=True and env=etoro, account_mode() stays "alpaca" and the fetch
   routes to Alpaca (eToro is hard-severed; the eToro network path is unreachable).
7. format_action_ticket() in alpaca mode — footer must not contain "you execute on eToro".
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

import tradingagents.portfolio_advisor.etoro_scan as etoro_scan_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_alpaca_rows():
    """Minimal fake return value from fetch_portfolio_rows_alpaca."""
    return ({}, "Alpaca snapshot", ["DKNG"], [{"symbolFull": "DKNG", "unitsBaseValueDollars": 500}])


# ---------------------------------------------------------------------------
# Fixture: force "not pytest" mode by monkeypatching the seam
# ---------------------------------------------------------------------------

@pytest.fixture
def simulate_production(monkeypatch):
    """Monkeypatch _is_pytest() to return False, simulating a production run."""
    monkeypatch.setattr(etoro_scan_mod, "_is_pytest", lambda: False)
    # Reset the one-shot log guard between tests
    monkeypatch.setattr(etoro_scan_mod, "_etoro_disabled_logged", False)
    yield


# ---------------------------------------------------------------------------
# 1. _etoro_enabled() default behaviour
# ---------------------------------------------------------------------------

class TestEtoroEnabledFlag:
    def test_under_pytest_always_enabled(self, monkeypatch):
        """Under real pytest, _etoro_enabled() must return True (existing tests are mocked)."""
        # We're running under pytest right now — _is_pytest() is real → True
        assert etoro_scan_mod._is_pytest() is True
        assert etoro_scan_mod._etoro_enabled() is True

    def test_production_flag_false_returns_disabled(self, simulate_production, monkeypatch):
        """In production with flag=False, _etoro_enabled() returns False."""
        monkeypatch.setattr(
            "tradingagents.portfolio_advisor.etoro_scan.DEFAULT_CONFIG",
            {"portfolio_advisor_etoro_enabled": False},
            raising=False,
        )
        # Patch the import inside _etoro_enabled to use our mock config
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {"portfolio_advisor_etoro_enabled": False})
        assert etoro_scan_mod._etoro_enabled() is False

    def test_production_flag_true_returns_enabled(self, simulate_production, monkeypatch):
        """In production with flag=True, _etoro_enabled() returns True."""
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {"portfolio_advisor_etoro_enabled": True})
        assert etoro_scan_mod._etoro_enabled() is True


# ---------------------------------------------------------------------------
# 2. account_mode() — alpaca is forced when flag=False
# ---------------------------------------------------------------------------

class TestAccountMode:
    def test_production_flag_false_always_returns_alpaca(self, simulate_production, monkeypatch):
        """account_mode() returns 'alpaca' even when TRADINGAGENTS_ACCOUNT_MODE=etoro."""
        monkeypatch.setenv("TRADINGAGENTS_ACCOUNT_MODE", "etoro")
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {
            "portfolio_advisor_etoro_enabled": False,
            "account_mode": "etoro",
        })
        result = etoro_scan_mod.account_mode()
        assert result == "alpaca", f"Expected 'alpaca', got {result!r}"

    def test_under_pytest_returns_etoro_regardless_of_env(self, monkeypatch):
        """Under pytest, account_mode() always returns 'etoro' (mocked test safety)."""
        monkeypatch.setenv("TRADINGAGENTS_ACCOUNT_MODE", "alpaca")
        # _is_pytest() is real here → True
        result = etoro_scan_mod.account_mode()
        assert result == "etoro", f"Expected 'etoro', got {result!r}"

    def test_production_flag_true_respects_env(self, simulate_production, monkeypatch):
        """account_mode() honours TRADINGAGENTS_ACCOUNT_MODE when flag=True."""
        monkeypatch.setenv("TRADINGAGENTS_ACCOUNT_MODE", "alpaca")
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {
            "portfolio_advisor_etoro_enabled": True,
            "account_mode": "etoro",
        })
        result = etoro_scan_mod.account_mode()
        assert result == "alpaca"


# ---------------------------------------------------------------------------
# 3. fetch_portfolio_rows() — gate blocks eToro HTTP when flag=False
# ---------------------------------------------------------------------------

class TestFetchPortfolioRowsGate:
    def test_flag_false_no_etoro_network_call(self, simulate_production, monkeypatch):
        """When flag=False, fetch_portfolio_rows() routes to Alpaca without touching eToro HTTP."""
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {
            "portfolio_advisor_etoro_enabled": False,
            "account_mode": "etoro",  # would be eToro if flag were ignored
        })

        # Mock the Alpaca adapter so no real Alpaca call either
        alpaca_called = []
        def fake_fetch_alpaca():
            alpaca_called.append(True)
            return _make_fake_alpaca_rows()

        monkeypatch.setattr(
            "tradingagents.integrations.alpaca.account_adapter.fetch_portfolio_rows_alpaca",
            fake_fetch_alpaca,
        )

        # Mock requests.get at the integration layer — should never be called
        import tradingagents.integrations.etoro.client as etoro_client_mod
        etoro_http_calls = []
        def fail_on_etoro_http(*args, **kwargs):
            etoro_http_calls.append(args)
            raise AssertionError("eToro HTTP should not be called when flag=False")
        monkeypatch.setattr(etoro_client_mod.requests, "get", fail_on_etoro_http)

        # Call should succeed via Alpaca path
        result = etoro_scan_mod.fetch_portfolio_rows()
        assert result[2] == ["DKNG"]
        assert alpaca_called, "Alpaca adapter was not called"
        assert not etoro_http_calls, "eToro HTTP was called despite flag=False"

    def test_etoro_severed_routes_to_alpaca_even_with_flag_and_env(self, simulate_production, monkeypatch):
        """eToro is hard-severed: even with flag=True AND TRADINGAGENTS_ACCOUNT_MODE=etoro,
        account_mode() stays 'alpaca' and fetch_portfolio_rows() never touches eToro HTTP.

        account_mode() is hardwired to 'alpaca' (see etoro_scan.account_mode), so the
        eToro network path is unreachable in production. This is the intended behaviour
        after the eToro-sever change — the prior "flag=True reaches eToro" expectation
        no longer holds.
        """
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {
            "portfolio_advisor_etoro_enabled": True,
            "account_mode": "etoro",
        })
        monkeypatch.setenv("TRADINGAGENTS_ACCOUNT_MODE", "etoro")
        monkeypatch.setenv("ETORO_API_KEY", "test-key")
        monkeypatch.setenv("ETORO_USER_KEY", "test-user")

        # Alpaca adapter is where the call must land.
        alpaca_called = []
        def fake_fetch_alpaca():
            alpaca_called.append(True)
            return _make_fake_alpaca_rows()
        monkeypatch.setattr(
            "tradingagents.integrations.alpaca.account_adapter.fetch_portfolio_rows_alpaca",
            fake_fetch_alpaca,
        )

        # eToro HTTP must never be hit, flag or no flag.
        import tradingagents.integrations.etoro.client as etoro_client_mod
        etoro_http_calls = []
        def fail_on_etoro_http(*args, **kwargs):
            etoro_http_calls.append(args)
            raise AssertionError("eToro HTTP must not be called — eToro is severed")
        monkeypatch.setattr(etoro_client_mod.requests, "get", fail_on_etoro_http)

        assert etoro_scan_mod.account_mode() == "alpaca"
        result = etoro_scan_mod.fetch_portfolio_rows()
        assert result[2] == ["DKNG"]
        assert alpaca_called, "Alpaca adapter was not called"
        assert not etoro_http_calls, "eToro HTTP was called despite the sever"


# ---------------------------------------------------------------------------
# 4. fetch_current_portfolio_headlines() gate
# ---------------------------------------------------------------------------

class TestPortfolioDigestGate:
    def test_flag_false_raises_without_network(self, simulate_production, monkeypatch):
        """portfolio_digest.fetch_current_portfolio_headlines raises when flag=False."""
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {
            "portfolio_advisor_etoro_enabled": False,
        })

        import tradingagents.integrations.etoro.client as etoro_client_mod
        http_calls = []
        def fail_on_http(*args, **kwargs):
            http_calls.append(args)
            raise AssertionError("eToro HTTP called despite flag=False")
        monkeypatch.setattr(etoro_client_mod.requests, "get", fail_on_http)

        from tradingagents.clerk.portfolio_digest import fetch_current_portfolio_headlines
        with pytest.raises(RuntimeError, match="etoro_enabled"):
            fetch_current_portfolio_headlines()
        assert not http_calls, "eToro HTTP was called"


# ---------------------------------------------------------------------------
# 5. fetch_clerk_watchlist_from_etoro() gate
# ---------------------------------------------------------------------------

class TestClerkBridgeGate:
    def test_flag_false_raises_without_network(self, simulate_production, monkeypatch):
        """clerk_bridge.fetch_clerk_watchlist_from_etoro raises when flag=False."""
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {
            "portfolio_advisor_etoro_enabled": False,
        })

        import tradingagents.integrations.etoro.client as etoro_client_mod
        http_calls = []
        def fail_on_http(*args, **kwargs):
            http_calls.append(args)
            raise AssertionError("eToro HTTP called despite flag=False")
        monkeypatch.setattr(etoro_client_mod.requests, "get", fail_on_http)

        from tradingagents.integrations.etoro.clerk_bridge import fetch_clerk_watchlist_from_etoro
        with pytest.raises(RuntimeError, match="etoro_enabled"):
            fetch_clerk_watchlist_from_etoro()
        assert not http_calls, "eToro HTTP was called"


# ---------------------------------------------------------------------------
# 6. format_action_ticket() footer — alpaca mode must not say "you execute on eToro"
# ---------------------------------------------------------------------------

class TestActionTicketFooter:
    def _proposal(self, **kw):
        base = dict(ticker="DKNG", action="buy", shares=10, approx_usd=275,
                    target_price=27.46, sleeve="catalyst", reason="Test")
        base.update(kw)
        return base

    def test_alpaca_footer_has_no_etoro_text(self, simulate_production, monkeypatch):
        """In alpaca mode, format_action_ticket must not produce 'you execute on eToro'."""
        import tradingagents.default_config as dc_mod
        monkeypatch.setattr(dc_mod, "DEFAULT_CONFIG", {
            "portfolio_advisor_etoro_enabled": False,  # → account_mode returns "alpaca"
            "account_mode": "etoro",
        })

        from tradingagents.portfolio_advisor import proposals as pr
        ticket = pr.format_action_ticket({}, self._proposal())
        assert "you execute on eToro" not in ticket, (
            f"eToro advisory footer found in alpaca-mode ticket:\n{ticket}"
        )
        assert "Alpaca paper book" in ticket, (
            f"Expected Alpaca footer in autonomous ticket:\n{ticket}"
        )

    def test_etoro_mode_footer_no_longer_mentions_etoro(self, monkeypatch):
        """Even in eToro mode, the fallback footer text must not say 'you execute on eToro'
        (the new neutral text is 'advisory — awaiting manual execution')."""
        # Under pytest account_mode() returns "etoro"
        from tradingagents.portfolio_advisor import proposals as pr
        ticket = pr.format_action_ticket({}, self._proposal())
        assert "you execute on eToro" not in ticket, (
            f"Old eToro footer text still present:\n{ticket}"
        )
