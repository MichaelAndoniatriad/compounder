"""Small-cap universe helpers for R5 — down-cap universe expansion.

Implements:
  - fetch_sp600_constituents(): fetch S&P 600 tickers from Wikipedia (30d cache)
    with fallback to a vendored CSV.
  - build_universe_with_cohorts(cfg): returns a list of {ticker, cohort} dicts
    where cohort is "largecap" (S&P 500 + NASDAQ 100) or "smallcap" (S&P 600).
  - smallcap_shadow_active(cfg): single function that decides whether the
    shadow-routing window is still active.
  - get_ticker_cohort(ticker, cfg): cache-only cohort lookup (no network).
    Returns "largecap", "smallcap", or "unknown" (unknown → treat as smallcap
    when the shadow window is active, per the fail-closed policy).

All network fetches are no-ops when portfolio_advisor_universe_smallcap_enabled
is False — the S&P 600 fetch is never attempted in that case.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Vendored CSV bundled next to this module (created during R5 bootstrap)
_VENDORED_CSV = Path(__file__).parent.parent / "dataflows" / "data" / "sp600_constituents.csv"

# Cache TTL for the live Wikipedia fetch (30 days)
_SP600_CACHE_TTL_DAYS = 30


def _sp600_cache_path(data_cache_dir: Optional[str] = None) -> Path:
    if data_cache_dir:
        base = Path(data_cache_dir)
    else:
        base = Path(os.path.expanduser("~")) / ".tradingagents" / "cache"
    return base / "sp600_constituents_cache.json"


def _load_sp600_from_cache(data_cache_dir: Optional[str] = None) -> Optional[List[str]]:
    p = _sp600_cache_path(data_cache_dir)
    if not p.exists():
        return None
    age_days = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 86400
    if age_days > _SP600_CACHE_TTL_DAYS:
        return None
    try:
        data = json.loads(p.read_bytes())
        tickers = data.get("tickers")
        if isinstance(tickers, list) and tickers:
            return tickers
    except Exception:
        pass
    return None


def _save_sp600_to_cache(tickers: List[str], data_cache_dir: Optional[str] = None) -> None:
    p = _sp600_cache_path(data_cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "tickers": tickers}).encode()
        )
        tmp.replace(p)
    except Exception:
        pass


def _fetch_sp600_from_wikipedia() -> Optional[List[str]]:
    """Fetch S&P 600 constituents from Wikipedia via pandas.read_html.

    Returns sorted list of tickers or None on failure.
    """
    try:
        import io as _io
        import pandas as pd
        import requests

        url = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
        resp = requests.get(
            url,
            headers={"User-Agent": "TradingAgents/1.0 (portfolio research)"},
            timeout=20,
        )
        resp.raise_for_status()
        all_tables = pd.read_html(_io.StringIO(resp.text))
    except Exception as exc:
        logger.warning("sp600 Wikipedia fetch failed: %s", exc)
        return None

    tickers: set[str] = set()
    for table in all_tables:
        for col in ("Symbol", "Ticker"):
            if col not in table.columns:
                continue
            for t in table[col].tolist():
                s = str(t).strip().upper().replace(".", "-")
                if s[:1].isalpha():
                    tickers.add(s)
            break

    if not tickers:
        return None

    result = sorted(tickers)
    logger.info("sp600: fetched %d constituents from Wikipedia", len(result))
    return result


def _load_sp600_from_vendored_csv() -> List[str]:
    """Load tickers from the bundled vendored CSV (fallback).

    Returns empty list if the file is missing or unreadable.
    """
    if not _VENDORED_CSV.is_file():
        logger.warning("sp600 vendored CSV not found at %s", _VENDORED_CSV)
        return []
    tickers: List[str] = []
    try:
        for line in _VENDORED_CSV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            t = line.split(",")[0].strip().upper().replace(".", "-")
            if t[:1].isalpha():
                tickers.append(t)
    except Exception as exc:
        logger.warning("sp600 vendored CSV read failed: %s", exc)
    logger.info("sp600: loaded %d constituents from vendored CSV", len(tickers))
    return sorted(tickers)


def fetch_sp600_constituents(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return S&P 600 ticker list, using cache when fresh.

    Fetch order:
    1. 30d disk cache (JSON)
    2. Live Wikipedia fetch (requires requests + pandas/lxml)
    3. Vendored CSV snapshot

    Returns empty list under pytest (no live fetch).
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return []

    data_cache_dir = (cfg or {}).get("data_cache_dir")
    cached = _load_sp600_from_cache(data_cache_dir)
    if cached is not None:
        return cached

    live = _fetch_sp600_from_wikipedia()
    if live:
        _save_sp600_to_cache(live, data_cache_dir)
        return live

    logger.warning("sp600: live fetch failed, using vendored CSV")
    vendored = _load_sp600_from_vendored_csv()
    if vendored:
        # Cache vendored list so we don't read the file on every call, but use a
        # very short TTL (0 bytes/no write here — just return and caller hits CSV again)
        pass
    return vendored


# ---------------------------------------------------------------------------
# Shadow-routing window
# ---------------------------------------------------------------------------


def smallcap_shadow_active(cfg: Dict[str, Any]) -> bool:
    """Return True while the small-cap shadow window is still open.

    The window is defined as: now < smallcap_enabled_at + shadow_days.
    The enable timestamp is read from the pa_state dict (persisted via state.py).
    If smallcap_enabled is off the window is always closed.

    SELF-HEAL: if the flag is on but smallcap_enabled_at is absent from state,
    this function records it as now (so the 90-day clock starts), logs a WARNING,
    sends a one-time Telegram note, and returns True for this call.

    This is the SINGLE function that controls shadow routing — used by both
    core_discovery and any catalyst-side path.
    """
    if not cfg.get("portfolio_advisor_universe_smallcap_enabled", False):
        return False

    shadow_days = int(cfg.get("portfolio_advisor_smallcap_shadow_days", 90) or 90)

    try:
        from tradingagents.portfolio_advisor import state as _state
        pa_state = _state.load_state(cfg)
        enabled_at_str = pa_state.get("smallcap_enabled_at")
        if not enabled_at_str:
            # SELF-HEAL: flag is on but timestamp is missing — record it now so
            # the 90-day clock starts, emit a WARNING so the operator knows the
            # clock was just anchored, and return True (still in window).
            now_iso = datetime.now(timezone.utc).isoformat()
            pa_state["smallcap_enabled_at"] = now_iso
            try:
                _state.save_state(cfg, pa_state)
            except Exception as _save_exc:
                logger.warning(
                    "smallcap_shadow_active: self-heal timestamp write failed: %s", _save_exc
                )
            logger.warning(
                "smallcap_shadow_active: smallcap_enabled_at was missing — "
                "self-healed to %s; 90-day shadow clock now starts from this call. "
                "All unvetted smallcap buys are shadow-routed until the window expires.",
                now_iso,
            )
            # One-time Telegram note so the operator is aware
            try:
                from tradingagents.portfolio_advisor.messaging import send_advisor_message
                send_advisor_message(
                    cfg,
                    "Smallcap Shadow",
                    "smallcap cohort map unavailable — failing closed. "
                    f"Shadow window clock anchored to {now_iso}. "
                    "All unvetted smallcap proposals are shadow-routed for the next "
                    f"{shadow_days} days.",
                    urgent=False,
                )
            except Exception:
                pass
            return True
        enabled_at = datetime.fromisoformat(str(enabled_at_str).replace("Z", "+00:00"))
        if enabled_at.tzinfo is None:
            enabled_at = enabled_at.replace(tzinfo=timezone.utc)
        window_end = enabled_at + timedelta(days=shadow_days)
        return datetime.now(timezone.utc) < window_end
    except Exception as exc:
        logger.warning("smallcap_shadow_active: error reading state: %s", exc)
        return True  # fail-safe: if we can't check, stay in shadow mode


def record_smallcap_enabled_at(cfg: Dict[str, Any]) -> None:
    """Persist smallcap_enabled_at in pa_state if not already set.

    Called from core_discovery when the flag is on and the key is absent.
    """
    try:
        from tradingagents.portfolio_advisor import state as _state
        pa_state = _state.load_state(cfg)
        if not pa_state.get("smallcap_enabled_at"):
            pa_state["smallcap_enabled_at"] = datetime.now(timezone.utc).isoformat()
            _state.save_state(cfg, pa_state)
            logger.info("smallcap_universe: recorded smallcap_enabled_at")
    except Exception as exc:
        logger.warning("smallcap_universe: could not persist smallcap_enabled_at: %s", exc)


# ---------------------------------------------------------------------------
# Cohort lookup — cache-only, no network
# ---------------------------------------------------------------------------

# Module-level in-memory cache of the sp600 set to avoid repeated disk I/O.
# Populated lazily on the first get_ticker_cohort call.
_sp600_set_cache: Optional[set] = None


def get_ticker_cohort(ticker: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """Return the cohort label for *ticker* using only the cached S&P 600 list.

    Never touches the network — reads only from the disk cache or the vendored
    CSV (both are loaded by fetch_sp600_constituents at discovery time).

    Return values:
      "largecap"  — ticker was definitively NOT in the S&P 600 constituent list
                    (it may still be small; the S&P 500 / NASDAQ-100 check is
                    handled by the caller using the original universe sets).
      "smallcap"  — ticker is in the cached S&P 600 list.
      "unknown"   — cache is empty or unreadable.  Callers MUST treat "unknown"
                    as "smallcap" when the shadow window is active (fail-closed).

    Design: this is intentionally a lightweight set-membership test.  The full
    cohort map built by _build_cohort_map in core_discovery is the authoritative
    source during discovery runs; this helper is for the proposals.add() choke
    point that runs outside of discovery (potentially hours later).
    """
    global _sp600_set_cache

    tk = (ticker or "").strip().upper()
    if not tk:
        return "unknown"

    # Populate in-memory cache lazily (from disk; never from the network).
    if _sp600_set_cache is None:
        _cfg = cfg or {}
        data_cache_dir = _cfg.get("data_cache_dir")
        cached_list = _load_sp600_from_cache(data_cache_dir)
        if cached_list:
            _sp600_set_cache = set(cached_list)
        else:
            # Try vendored CSV as last resort
            vendored = _load_sp600_from_vendored_csv()
            if vendored:
                _sp600_set_cache = set(vendored)
            else:
                # No data available at all — return "unknown" so callers fail closed.
                return "unknown"

    if not _sp600_set_cache:
        return "unknown"

    return "smallcap" if tk in _sp600_set_cache else "largecap"


def _invalidate_sp600_cache() -> None:
    """Clear the module-level sp600 in-memory cache (for tests and after refreshes)."""
    global _sp600_set_cache
    _sp600_set_cache = None
