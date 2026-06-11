"""Read live eToro positions for the portfolio advisor."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Set, Tuple

from tradingagents.integrations.etoro.clerk_bridge import _normalize_ticker
from tradingagents.integrations.etoro.client import EtoroClient
from tradingagents.integrations.etoro.portfolio import (
    dedupe_positions,
    instrument_id_from_position,
    iter_positions,
    portfolio_headlines,
    summarize_portfolio,
)

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Testable seam: tests monkeypatch _is_pytest() to control the gate.
# Production code never sets this — it reads the real PYTEST_CURRENT_TEST.
# -----------------------------------------------------------------------
def _is_pytest() -> bool:
    """Return True when running under pytest. Monkeypatched in tests."""
    return "PYTEST_CURRENT_TEST" in os.environ


def _etoro_enabled() -> bool:
    """Return True when eToro reads are enabled.

    Rules (evaluated in this order):
    1. Under pytest — always True (preserves existing eToro-mock tests).
    2. ``portfolio_advisor_etoro_enabled`` in DEFAULT_CONFIG (default: False) — controls
       production behaviour.  When False, every eToro network fetch is short-circuited.
    """
    if _is_pytest():
        return True
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        return bool(DEFAULT_CONFIG.get("portfolio_advisor_etoro_enabled", False))
    except Exception:
        return False


# One-shot log guard so we don't spam the log on every watchdog tick.
_etoro_disabled_logged: bool = False


def _log_etoro_disabled_once(context: str = "") -> None:
    global _etoro_disabled_logged
    if not _etoro_disabled_logged:
        msg = "eToro reads are disabled (portfolio_advisor_etoro_enabled=False). Returning empty/unavailable result."
        if context:
            msg = f"{msg} context={context}"
        logger.warning(msg)
        _etoro_disabled_logged = True


def etoro_keys_configured() -> bool:
    return bool(
        (os.environ.get("ETORO_API_KEY") or "").strip()
        and (os.environ.get("ETORO_USER_KEY") or "").strip()
    )


def account_mode() -> str:
    """Which account the advisor manages: "etoro" (advisory, human executes)
    or "alpaca" (autonomous, PM-managed paper book).

    Priority order:
    1. Under pytest: always "etoro" (fully mocked, preserves existing tests).
    2. When eToro reads are disabled (portfolio_advisor_etoro_enabled=False):
       always "alpaca" — a LaunchAgent without TRADINGAGENTS_ACCOUNT_MODE must
       never fall back to eToro mode.
    3. Env TRADINGAGENTS_ACCOUNT_MODE wins over DEFAULT_CONFIG.
    4. DEFAULT_CONFIG "account_mode" second; falls back to "etoro".

    This is THE switch for autonomous mode — every portfolio consumer reads
    through fetch_portfolio_rows(), so flipping it repoints the PM cycle,
    watchdog, weekly check, sleeves, and risk at the chosen book.
    """
    # Tests always run in etoro mode (fully mocked) — never let a dev shell's
    # .env leak autonomous mode into pytest, where the Alpaca adapter would
    # make live API calls.
    if _is_pytest():
        return "etoro"
    # When eToro reads are hard-disabled, always run in alpaca mode regardless
    # of env — prevents a LaunchAgent from silently falling back to eToro.
    if not _etoro_enabled():
        return "alpaca"
    mode = (os.environ.get("TRADINGAGENTS_ACCOUNT_MODE") or "").strip().lower()
    if not mode:
        try:
            from tradingagents.default_config import DEFAULT_CONFIG

            mode = str(DEFAULT_CONFIG.get("account_mode") or "").strip().lower()
        except Exception:
            mode = ""
    return "alpaca" if mode == "alpaca" else "etoro"


def fetch_portfolio_rows() -> Tuple[Dict[str, Any], str, List[str], List[Dict[str, Any]]]:
    """Same as ``fetch_portfolio_bundle`` but also returns ``summarize_portfolio`` rows."""
    if account_mode() == "alpaca":
        from tradingagents.integrations.alpaca.account_adapter import fetch_portfolio_rows_alpaca

        return fetch_portfolio_rows_alpaca()
    # --- eToro gate ---
    # This path is only reached when account_mode() == "etoro", which only happens
    # when _etoro_enabled() is True (or under pytest).  Belt-and-suspenders: gate
    # again here at the lowest network-call level.
    if not _etoro_enabled():
        _log_etoro_disabled_once("fetch_portfolio_rows")
        raise RuntimeError(
            "eToro reads are disabled (portfolio_advisor_etoro_enabled=False). "
            "Set account_mode='alpaca' or enable eToro reads explicitly."
        )
    if not etoro_keys_configured():
        raise RuntimeError(
            "eToro keys missing: set ETORO_API_KEY and ETORO_USER_KEY in the environment."
        )
    client = EtoroClient()
    payload = client.get_portfolio_pnl()
    cp = payload.get("clientPortfolio") or {}
    positions = dedupe_positions(iter_positions(cp))
    ids: List[int] = []
    for p in positions:
        iid = instrument_id_from_position(p)
        if iid is not None:
            ids.append(iid)
    meta = client.get_instruments_metadata(ids) if ids else {}
    text, rows = summarize_portfolio(payload, meta)
    tickers = sorted(
        {
            _normalize_ticker(str(r.get("symbolFull") or ""))
            for r in rows
            if _normalize_ticker(str(r.get("symbolFull") or ""))
        }
    )
    hl = portfolio_headlines(payload)
    head = (
        f"Headlines: available_balance={hl.get('credit')!r}, "
        f"unrealized_pnl={hl.get('unrealized_pnl')!r}, "
        f"open_positions={hl.get('open_positions')!r}\n"
    )
    return payload, head + text, tickers, rows


def fetch_portfolio_bundle() -> Tuple[Dict[str, Any], str, List[str]]:
    """Return (raw_pnl_payload, summary_text, tickers_upper).

    Raises RuntimeError if keys missing or API fails.
    """
    payload, text, tickers, _rows = fetch_portfolio_rows()
    return payload, text, tickers


def current_ticker_set(tickers: List[str]) -> Set[str]:
    return {t.upper().strip() for t in tickers if t and str(t).strip()}
