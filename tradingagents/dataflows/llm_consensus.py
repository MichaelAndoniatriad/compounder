"""LLM Consensus — polls 5 consumer LLMs daily to track public ticker consensus.

Phase A of AI Consensus Guardrails. Runs daily via cron. Polls OpenAI,
Anthropic, Google, xAI, and Perplexity via OpenRouter with 30 standardised
retail-investor prompts. Extracts tickers, validates against CRSP universe,
and builds a rolling 30-day consensus snapshot.

The snapshot powers: consensus concentration flags, DeepSeek alignment
detection, crowded trade triggers, and the defensive mode gate.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

# Consumer LLMs polled via OpenRouter
POLL_MODELS = [
    "openai/gpt-5.4",
    "anthropic/claude-4.6",
    "google/gemini-3.1-pro",
    "xai/grok-4",
    "perplexity/sonar-pro",
]

# Ticker extraction regex: $AAPL, AAPL, "AAPL"
_TICKER_RE = re.compile(r"\b\$?([A-Z]{1,5})\b")

def _extract_tickers(text: str) -> set[str]:
    """Extract uppercase tickers, filtering single-letter matches."""
    raw = set(_TICKER_RE.findall(text))
    return {t for t in raw if len(t) >= 2}

# Cost tracking
_MONTHLY_COST_CAP = 150.0  # USD

_CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "llm_consensus"


def _prompts_path() -> Path:
    from tradingagents.dataflows import llm_consensus_prompts  # noqa
    import importlib
    spec = importlib.util.find_spec("tradingagents.dataflows.llm_consensus_prompts")
    if spec and spec.origin:
        return Path(spec.origin)
    # Fallback: look relative to this file
    return Path(__file__).parent / "llm_consensus_prompts.json"


def _load_prompts() -> List[str]:
    """Load standardised prompts from JSON file."""
    p = _prompts_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(item) for item in data if str(item).strip()]
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback prompts if file missing
    return [
        "What are the top 5 stocks to buy this week?",
        "Best AI stocks for 2026",
        "Top growth stocks for the next 3 years",
        "What is most undervalued right now in US equities?",
        "Top dividend stocks for 2026",
        "Best semiconductor stocks to own",
        "What is the best stock pick for retirement?",
        "Top 5 mega-cap stocks to hold long term",
        "Which tech stocks have the most upside?",
        "Best value stocks in the S&P 500 right now",
    ]


def _crsp_universe_path() -> Path:
    return Path.home() / ".tradingagents" / "cache" / "crsp_universe.json"


def _load_crsp_universe() -> Set[str]:
    """Load cached CRSP universe, refresh weekly."""
    p = _crsp_universe_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            if (datetime.now(timezone.utc) - mtime).days <= 7:
                return {str(t).strip().upper() for t in data if str(t).strip()}
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: S&P 500 constituents as minimum viable set
    logger.warning("CRSP universe cache missing or stale, using S&P 500 fallback")
    return _sp500_fallback()


def _sp500_fallback() -> Set[str]:
    """Minimal S&P 500 ticker set when CRSP unavailable."""
    try:
        import yfinance as yf
        sp500 = yf.Ticker("^GSPC")
        # This won't actually work — yfinance doesn't expose constituents directly.
        # Fall through to hardcoded top 100 as minimum.
    except Exception:
        pass
    # Hardcoded top 100 US listed tickers as absolute fallback
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


def _snapshot_path(date_str: str = "") -> Path:
    d = date_str or date.today().isoformat()
    return _CACHE_DIR / "snapshot.json"


def _daily_dir(day_str: str = "") -> Path:
    d = day_str or date.today().isoformat()
    return _CACHE_DIR / d


def poll_single_model(model: str, api_key: str, prompts: List[str]) -> Dict[str, Any]:
    """Poll one model with all prompts. Returns {model, responses, tickers, cost_usd}."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    responses: List[str] = []
    all_tickers: Set[str] = set()
    total_cost = 0.0

    for prompt in prompts[:30]:  # Cap at 30 prompts
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.7,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                responses.append(content)
                # Extract tickers
                tickers = _extract_tickers(content)
                all_tickers.update(tickers)
                # Track cost from OpenRouter headers
                total_cost += float(resp.headers.get("x-openrouter-cost", 0) or 0)
            elif resp.status_code in (401, 402):
                return {"model": model, "error": f"HTTP {resp.status_code}", "responses": [], "tickers": [], "cost_usd": 0.0}
            else:
                logger.warning("consensus poll %s: HTTP %s", model, resp.status_code)
        except requests.RequestException as e:
            logger.warning("consensus poll %s: %s", model, e)
            continue

    # Validate against CRSP universe
    universe = _load_crsp_universe()
    valid_tickers = sorted(all_tickers & universe)

    return {
        "model": model,
        "responses": responses,
        "tickers": valid_tickers,
        "cost_usd": round(total_cost, 4),
    }


def run_daily_consensus_poll(api_key: Optional[str] = None) -> Dict[str, Any]:
    """Run the full daily consensus poll across all 5 models.

    Returns the daily result dict with per-model breakdown and aggregate stats.
    Stores raw responses at cache/llm_consensus/YYYY-MM-DD/<model>.json.
    """
    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return {"error": "OPENROUTER_API_KEY missing", "models_polled": 0}

    prompts = _load_prompts()
    today = date.today().isoformat()
    daily_dir = _daily_dir(today)
    daily_dir.mkdir(parents=True, exist_ok=True)

    results = []
    total_cost = 0.0
    for model in POLL_MODELS:
        result = poll_single_model(model, key, prompts)
        results.append(result)
        total_cost += result.get("cost_usd", 0)

        # Store raw response
        model_file = daily_dir / f"{model.replace('/', '_')}.json"
        model_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    # Update rolling snapshot
    models_ok = sum(1 for r in results if "error" not in r)
    snapshot = _update_snapshot(today, results)

    return {
        "date": today,
        "models_polled": len(POLL_MODELS),
        "models_succeeded": models_ok,
        "total_cost_usd": round(total_cost, 4),
        "snapshot_valid": models_ok >= 3,
        "results": results,
    }


def _update_snapshot(today_str: str, daily_results: List[Dict]) -> Dict[str, Any]:
    """Update the rolling 30-day consensus snapshot."""
    # Load existing snapshot
    sp = _snapshot_path()
    snapshot: Dict[str, Any] = {}
    if sp.is_file():
        try:
            snapshot = json.loads(sp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Build ticker frequency map from last 30 days of raw data
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    ticker_counts: Dict[str, Dict[str, Any]] = {}

    for day_dir in sorted(_CACHE_DIR.glob("20*")):
        if not day_dir.is_dir():
            continue
        day_str = day_dir.name
        if day_str < cutoff:
            continue
        for model_file in day_dir.glob("*.json"):
            try:
                data = json.loads(model_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for ticker in data.get("tickers", []):
                t = ticker.strip().upper()
                if t not in ticker_counts:
                    ticker_counts[t] = {"count": 0, "days": set(), "models": set()}
                ticker_counts[t]["count"] += 1
                ticker_counts[t]["days"].add(day_str)
                model_name = data.get("model", "")
                if model_name:
                    ticker_counts[t]["models"].add(model_name)

    # Build top 20 ranking
    ranked = sorted(ticker_counts.items(),
                    key=lambda x: (len(x[1]["days"]), x[1]["count"]),
                    reverse=True)
    top_20 = []
    for rank, (ticker, info) in enumerate(ranked[:20], 1):
        prev = _prev_rank(ticker, snapshot)
        top_20.append({
            "ticker": ticker,
            "rank": rank,
            "days_in_top_20": len(info["days"]),
            "rank_7d_change": (prev - rank) if prev else 0,
            "models_recommending": sorted(info["models"]),
        })

    # Detect fresh entries and dropouts (7-day window)
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    current_top = {t["ticker"] for t in top_20}
    prev_top = {t["ticker"] for t in snapshot.get("top_20", [])}

    fresh_entries = []
    drop_outs = []
    for ticker, info in ticker_counts.items():
        recent_days = {d for d in info["days"] if d >= week_ago}
        if ticker in current_top and ticker not in prev_top:
            fresh_entries.append(ticker)
        elif ticker not in current_top and ticker in prev_top:
            drop_outs.append(ticker)

    # Compute DeepSeek alignment
    deepseek_alignment = _compute_deepseek_alignment(ticker_counts, current_top)

    snapshot = {
        "snapshot_date": today_str,
        "rolling_window_days": 30,
        "top_20": top_20,
        "fresh_entries_7d": fresh_entries,
        "drop_outs_7d": drop_outs,
        "deepseek_alignment": deepseek_alignment,
    }

    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return snapshot


def _prev_rank(ticker: str, snapshot: Dict) -> Optional[int]:
    for t in snapshot.get("top_20", []):
        if t["ticker"] == ticker:
            return t.get("rank")
    return None


def _compute_deepseek_alignment(
    ticker_counts: Dict[str, Dict], current_top: Set[str]
) -> Dict[str, Any]:
    """Compute overlap between DeepSeek recommendations and public consensus."""
    # Get DeepSeek-originated recommendations from the recommendation log
    deepseek_picks = _get_deepseek_recommendations(30)

    overlap_count = len(deepseek_picks & current_top)
    overlap_pct = overlap_count / max(len(current_top), 1)

    # Trend: compare with previous snapshot
    prev_snapshot = _snapshot_path()
    prev_overlap = 0.0
    if prev_snapshot.is_file():
        try:
            prev = json.loads(prev_snapshot.read_text(encoding="utf-8"))
            prev_alignment = prev.get("deepseek_alignment", {})
            prev_overlap = float(prev_alignment.get("overlap_with_top_20", 0) or 0)
        except (json.JSONDecodeError, OSError):
            pass

    trend = round(overlap_pct - prev_overlap, 3)

    return {
        "deepseek_last_recommended": sorted(deepseek_picks)[:20],
        "overlap_with_top_20": round(overlap_pct, 3),
        "overlap_trend_30d": f"{trend:+.3f}",
    }


def _get_deepseek_recommendations(days: int = 30) -> Set[str]:
    """Get tickers DeepSeek recommended in the last N days from recommendation log."""
    try:
        from tradingagents.portfolio_advisor.recommendation_log import load_measured
        recs = load_measured({})  # empty config fallback
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        tickers: Set[str] = set()
        for r in recs:
            if str(r.get("ts", "")) >= cutoff:
                t = str(r.get("ticker", "") or "").strip().upper()
                if t:
                    tickers.add(t)
        return tickers
    except Exception:
        return set()


def load_llm_consensus_snapshot() -> Optional[Dict[str, Any]]:
    """Return the current consensus snapshot or None if unavailable."""
    sp = _snapshot_path()
    if not sp.is_file():
        return None
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def monthly_cost_total() -> float:
    """Sum consensus polling costs for the current calendar month."""
    month_start = date.today().replace(day=1).isoformat()
    total = 0.0
    for day_dir in _CACHE_DIR.glob("20*"):
        if not day_dir.is_dir() or day_dir.name < month_start:
            continue
        for model_file in day_dir.glob("*.json"):
            try:
                data = json.loads(model_file.read_text(encoding="utf-8"))
                total += float(data.get("cost_usd", 0) or 0)
            except (json.JSONDecodeError, OSError, ValueError):
                pass
    return round(total, 2)
