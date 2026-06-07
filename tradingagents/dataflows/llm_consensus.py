"""LLM Consensus Scraper — pulls published AI stock picks from financial media.

v4 refactor: scrapes published articles instead of polling LLM APIs.
Sources: Insider Monkey, Yahoo Finance, Barchart, US News.
Cost: zero. Failure mode: keep prior snapshot if all sources fail.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "llm_consensus"
_SOURCES_PATH = Path(__file__).parent / "llm_consensus_sources.json"

# Ticker extraction
_TICKER_RE = re.compile(r"\b\$?([A-Z]{1,5})\b")

def _extract_tickers(text: str) -> set[str]:
    raw = set(_TICKER_RE.findall(text))
    return {t for t in raw if len(t) >= 2}


def _load_sources() -> List[Dict]:
    if _SOURCES_PATH.is_file():
        try:
            data = json.loads(_SOURCES_PATH.read_text(encoding="utf-8"))
            return data.get("sources", [])
        except (json.JSONDecodeError, OSError):
            pass
    return [
        {"name": "insider_monkey", "url": "https://www.insidermonkey.com/?s=AI+stock+portfolio", "parser": "insider_monkey"},
        {"name": "yahoo_ai", "url": "https://finance.yahoo.com/topic/ai-stocks/", "parser": "yahoo"},
        {"name": "us_news", "url": "https://money.usnews.com/investing/articles/ai-stocks-to-buy", "parser": "us_news"},
    ]


def _crsp_universe() -> Set[str]:
    p = Path.home() / ".tradingagents" / "cache" / "crsp_universe.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {str(t).strip().upper() for t in data if str(t).strip()}
        except (json.JSONDecodeError, OSError):
            pass
    return _sp500_fallback()


def _sp500_fallback() -> Set[str]:
    return {
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK.B", "JPM",
        "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD", "DIS", "BAC", "XOM", "CVX",
        "NFLX", "ADBE", "CRM", "ORCL", "CSCO", "INTC", "AMD", "QCOM", "TXN", "AVGO",
        "TSM", "ASML", "NOW", "INTU", "IBM", "AMAT", "LRCX", "MU", "PLTR", "SNOW",
        "DDOG", "MNDY", "NET", "CRWD", "ZS", "PANW", "FTNT", "SQ", "PYPL", "COIN",
        "MELI", "SHOP", "UBER", "LYFT", "ABNB", "SNAP", "PINS", "RBLX", "DKNG",
        "CVLT", "ANET", "TEAM", "VRTX", "REGN", "ISRG", "GILD", "AMGN", "BMY",
        "LLY", "NVO", "ABBV", "MRK", "PFE", "TMO", "DHR", "ABT", "SYK", "BSX",
        "COST", "TGT", "LOW", "HD", "MCD", "SBUX", "NKE", "TJX", "CMG", "DASH",
        "CAT", "DE", "GE", "HON", "LMT", "RTX", "BA", "UNP", "UPS", "FDX",
    }


def _daily_dir(day_str: str = "") -> Path:
    return _CACHE_DIR / (day_str or date.today().isoformat())


def _snapshot_path() -> Path:
    return _CACHE_DIR / "snapshot.json"


def scrape_source(source: Dict) -> Dict[str, Any]:
    """Scrape one source for ticker mentions. Returns {name, tickers, success, error}."""
    try:
        resp = requests.get(
            source["url"],
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TradingAgents/1.0)"},
        )
        if resp.status_code != 200:
            return {"name": source["name"], "tickers": [], "success": False,
                    "error": f"HTTP {resp.status_code}"}

        html = resp.text
        tickers = _extract_tickers(html)
        universe = _crsp_universe()
        valid = sorted(tickers & universe)[:30]

        return {"name": source["name"], "tickers": valid, "success": True, "error": None}
    except requests.RequestException as e:
        return {"name": source["name"], "tickers": [], "success": False, "error": str(e)[:100]}


def run_daily_scrape() -> Dict[str, Any]:
    """Run the daily scrape across all configured sources.

    Returns {date, sources_succeeded, sources_total, results, snapshot_valid}.
    Stores raw results at cache/llm_consensus/YYYY-MM-DD/<source>.json.
    """
    today = date.today().isoformat()
    sources = _load_sources()
    daily_dir = _daily_dir(today)
    daily_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for source in sources:
        result = scrape_source(source)
        results.append(result)
        src_file = daily_dir / f"{source['name']}.json"
        src_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    succeeded = sum(1 for r in results if r["success"])
    snapshot_valid = succeeded >= 2  # At least 2 of N sources

    if snapshot_valid or _snapshot_path().is_file():
        _update_snapshot(today, results)
    else:
        logger.warning("consensus scrape: all sources failed, no prior snapshot")

    return {
        "date": today,
        "sources_succeeded": succeeded,
        "sources_total": len(sources),
        "snapshot_valid": snapshot_valid,
        "last_successful_scrape": today if snapshot_valid else None,
        "results": results,
    }


def _update_snapshot(today_str: str, daily_results: List[Dict]) -> Dict[str, Any]:
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    ticker_counts: Dict[str, Dict] = {}

    for day_dir in sorted(_CACHE_DIR.glob("20*")):
        if not day_dir.is_dir() or day_dir.name < cutoff:
            continue
        for src_file in day_dir.glob("*.json"):
            try:
                data = json.loads(src_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            src_name = data.get("name", src_file.stem)
            for ticker in data.get("tickers", []):
                t = ticker.strip().upper()
                if t not in ticker_counts:
                    ticker_counts[t] = {"count": 0, "days": set(), "models": set()}
                ticker_counts[t]["count"] += 1
                ticker_counts[t]["days"].add(day_dir.name)
                ticker_counts[t]["models"].add(src_name)

    ranked = sorted(ticker_counts.items(),
                    key=lambda x: (len(x[1]["days"]), x[1]["count"]), reverse=True)
    top_20 = []
    for rank, (ticker, info) in enumerate(ranked[:20], 1):
        top_20.append({
            "ticker": ticker,
            "rank": rank,
            "days_in_top_20": len(info["days"]),
            "rank_7d_change": 0,
            "models_recommending": sorted(info["models"]),
        })

    week_ago = (date.today() - timedelta(days=7)).isoformat()
    current_top = {t["ticker"] for t in top_20}
    fresh = [t for t, info in ticker_counts.items()
             if t in current_top and any(d >= week_ago for d in info["days"]) and not _was_in_top(t, None)]
    drops = [t for t in _prev_top_tickers() if t not in current_top]

    deepseek = _compute_deepseek_alignment(ticker_counts, current_top)

    snapshot = {
        "snapshot_date": today_str,
        "rolling_window_days": 30,
        "top_20": top_20,
        "fresh_entries_7d": fresh,
        "drop_outs_7d": drops,
        "deepseek_alignment": deepseek,
        "last_successful_scrape": today_str,
    }

    _snapshot_path().parent.mkdir(parents=True, exist_ok=True)
    _snapshot_path().write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return snapshot


def _was_in_top(ticker: str, prev: Optional[Dict]) -> bool:
    return False


def _prev_top_tickers() -> Set[str]:
    sp = _snapshot_path()
    if sp.is_file():
        try:
            prev = json.loads(sp.read_text(encoding="utf-8"))
            return {t["ticker"] for t in prev.get("top_20", [])}
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _compute_deepseek_alignment(
    ticker_counts: Dict, current_top: Set[str]
) -> Dict[str, Any]:
    deepseek_picks = _get_deepseek_recommendations(30)
    overlap_count = len(deepseek_picks & current_top)
    overlap_pct = overlap_count / max(len(current_top), 1)

    prev_overlap = 0.0
    sp = _snapshot_path()
    if sp.is_file():
        try:
            prev = json.loads(sp.read_text(encoding="utf-8"))
            prev_overlap = float(prev.get("deepseek_alignment", {}).get("overlap_with_top_20", 0) or 0)
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "deepseek_last_recommended": sorted(deepseek_picks)[:20],
        "overlap_with_top_20": round(overlap_pct, 3),
        "overlap_trend_30d": f"{overlap_pct - prev_overlap:+.3f}",
    }


def _get_deepseek_recommendations(days: int = 30) -> Set[str]:
    try:
        from tradingagents.portfolio_advisor.recommendation_log import load_measured
        recs = load_measured({})
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return {str(r.get("ticker", "")).strip().upper()
                for r in recs if str(r.get("ts", "")) >= cutoff}
    except Exception:
        return set()


def load_llm_consensus_snapshot() -> Optional[Dict[str, Any]]:
    sp = _snapshot_path()
    if not sp.is_file():
        return None
    try:
        snap = json.loads(sp.read_text(encoding="utf-8"))
        last = snap.get("last_successful_scrape", "")
        if last:
            try:
                last_date = date.fromisoformat(last[:10])
                if (date.today() - last_date).days > 5:
                    logger.warning("consensus snapshot stale: %s days old", (date.today() - last_date).days)
            except (ValueError, TypeError):
                pass
        return snap
    except (json.JSONDecodeError, OSError):
        return None
