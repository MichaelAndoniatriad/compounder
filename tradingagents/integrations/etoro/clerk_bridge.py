# tradingagents/integrations/etoro/clerk_bridge.py

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from tradingagents.clerk.watchlist import ClerkTriggers, ClerkWatchlist
from tradingagents.integrations.etoro.client import EtoroClient
from tradingagents.integrations.etoro.portfolio import (
    dedupe_positions,
    instrument_id_from_position,
    iter_positions,
    summarize_portfolio,
)


def _normalize_ticker(symbol_full: str) -> str:
    s = (symbol_full or "").strip().upper()
    if not s:
        return ""
    # eToro often uses "NVDA" or exchange-qualified forms — yfinance usually accepts the stem
    if "-" in s and not s.endswith((".L", ".TO", ".HK", ".T")):
        return s.split("-")[0]
    return s


def fetch_clerk_watchlist_from_etoro(
    template_path: Optional[Path] = None,
) -> ClerkWatchlist:
    """Build a ``ClerkWatchlist`` from live open positions + optional JSON template for triggers."""
    from tradingagents.portfolio_advisor.etoro_scan import (
        _etoro_enabled,
        _log_etoro_disabled_once,
    )
    if not _etoro_enabled():
        _log_etoro_disabled_once("fetch_clerk_watchlist_from_etoro")
        raise RuntimeError(
            "eToro reads are disabled (portfolio_advisor_etoro_enabled=False). "
            "Use a watchlist file or the Alpaca adapter instead."
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
    _summary, rows = summarize_portfolio(payload, meta)

    tickers = sorted(
        {
            _normalize_ticker(str(r.get("symbolFull") or ""))
            for r in rows
            if _normalize_ticker(str(r.get("symbolFull") or ""))
        }
    )

    if not tickers:
        tickers = ["SPY"]  # fallback so clerk doesn't crash empty

    if template_path and template_path.exists():
        base = json.loads(template_path.read_text(encoding="utf-8"))
        triggers_raw = base.get("triggers") or {}
        tr = ClerkTriggers(
            deep_research_on_new_headlines=bool(
                triggers_raw.get("deep_research_on_new_headlines", True)
            ),
            deep_research_earnings_within_days=_opt_int(
                triggers_raw.get("deep_research_earnings_within_days")
            ),
            deep_research_keyword_hits=[
                str(x) for x in (triggers_raw.get("deep_research_keyword_hits") or []) if str(x).strip()
            ],
            use_llm_materiality_gate=bool(triggers_raw.get("use_llm_materiality_gate", False)),
        )
        analysts = base.get("deep_research_analysts") or ["news", "fundamentals"]
        if not isinstance(analysts, list):
            analysts = ["news", "fundamentals"]
        analysts = [str(a).strip().lower() for a in analysts]
        lang = str(base.get("output_language") or "English")
        return ClerkWatchlist(
            tickers=tickers,
            triggers=tr,
            deep_research_analysts=analysts,
            output_language=lang,
        )

    return ClerkWatchlist.default_for_tickers(tickers)


def _opt_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    return int(v)
