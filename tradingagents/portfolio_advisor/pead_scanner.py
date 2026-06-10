"""Post-Earnings Announcement Drift (PEAD) candidate scanner.

Two entry points:
  - refresh_calendar(cfg)   — nightly pull of Alpha Vantage EARNINGS_CALENDAR.
  - scan_post_reports(cfg)  — morning check of yesterday's reporters for drift.

Free-tier budget
  calendar: 1 AV call per run (cached 3 days).
  post-report: ≤1 AV EARNINGS call per reporter, capped by
               cfg['portfolio_advisor_pead_max_names'] (default 15).

Gates (all must pass for a candidate to survive):
  1. SUE ≥ +10%  (actual EPS − estimate) / |estimate|
  2. Gap direction: today open > prior close
  3. RVOL ≥ 1.5 vs 20-day average volume
  4. Dollar volume ≥ $5M (price × today volume)

Survivors → candidates.evaluate_candidate(sleeve="catalyst") +
             candidates.shadow_book_add() for gate-passers that don't become proposals.

Pytest guard: both entry points short-circuit when PYTEST_CURRENT_TEST is set.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

_DEFAULT_MAX_NAMES = 15
_SUE_MIN_PCT = 10.0      # +10 % surprise required
_RVOL_MIN = 1.5          # 1.5× 20-day average volume
_DOLLAR_VOL_MIN = 5_000_000.0  # $5 M daily dollar volume


def _advisor_dir(cfg: Dict[str, Any]) -> Path:
    from tradingagents.portfolio_advisor import state as _state
    return Path(_state.advisor_dir(cfg))


def _calendar_path(cfg: Dict[str, Any]) -> Path:
    return _advisor_dir(cfg) / "earnings_calendar.json"


def _max_names(cfg: Dict[str, Any]) -> int:
    try:
        return int(cfg.get("portfolio_advisor_pead_max_names", _DEFAULT_MAX_NAMES) or _DEFAULT_MAX_NAMES)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_NAMES


# ---------------------------------------------------------------------------
# Alpha Vantage helpers
# ---------------------------------------------------------------------------

def _av_api_key() -> str:
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    if not key:
        raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is not set")
    return key


def _fetch_earnings_calendar() -> str:
    """Pull EARNINGS_CALENDAR CSV from AV (1 call). Returns raw CSV text."""
    import requests  # type: ignore
    url = "https://www.alphavantage.co/query"
    resp = requests.get(url, params={
        "function": "EARNINGS_CALENDAR",
        "horizon": "3month",
        "apikey": _av_api_key(),
    }, timeout=30)
    resp.raise_for_status()
    return resp.text


def _fetch_earnings_eps(symbol: str) -> Optional[Dict[str, Any]]:
    """Pull EARNINGS JSON for one symbol. Returns the most-recent quarterly row or None."""
    import requests  # type: ignore
    url = "https://www.alphavantage.co/query"
    resp = requests.get(url, params={
        "function": "EARNINGS",
        "symbol": symbol,
        "apikey": _av_api_key(),
    }, timeout=20)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        return None
    quarterly = data.get("quarterlyEarnings") or []
    if not quarterly:
        return None
    # Most-recent row first
    return quarterly[0] if isinstance(quarterly[0], dict) else None


# ---------------------------------------------------------------------------
# Calendar parsing
# ---------------------------------------------------------------------------

def _parse_calendar_csv(csv_text: str) -> List[Dict[str, Any]]:
    """Parse EARNINGS_CALENDAR CSV into a list of dicts.

    Expected columns (AV free tier):
      symbol, name, reportDate, fiscalDateEnding, estimate, currency
    """
    rows: List[Dict[str, Any]] = []
    if not csv_text or not csv_text.strip():
        return rows
    reader = csv.DictReader(StringIO(csv_text))
    for row in reader:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "name": (row.get("name") or "").strip(),
            "report_date": (row.get("reportDate") or "").strip(),
            "fiscal_date_ending": (row.get("fiscalDateEnding") or "").strip(),
            "estimate": (row.get("estimate") or "").strip(),
            "currency": (row.get("currency") or "USD").strip(),
        })
    return rows


# ---------------------------------------------------------------------------
# ENTRY POINT 1: refresh_calendar(cfg)
# ---------------------------------------------------------------------------

def refresh_calendar(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Nightly: pull EARNINGS_CALENDAR from Alpha Vantage and cache.

    Returns {'symbols': int, 'cached': bool, 'path': str}.
    Short-circuits under pytest without hitting any live API.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        logger.debug("pead_scanner.refresh_calendar: skipping under pytest")
        return {"symbols": 0, "cached": False, "path": "", "skipped_pytest": True}

    cache_path = _calendar_path(cfg)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Reuse if <3 days old
    if cache_path.is_file():
        try:
            existing = json.loads(cache_path.read_text(encoding="utf-8"))
            fetched_at_str = existing.get("fetched_at", "")
            if fetched_at_str:
                fetched_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00"))
                age = datetime.now(timezone.utc) - fetched_at
                if age < timedelta(days=3):
                    logger.info("pead_scanner.refresh_calendar: cache is %.1fh old, reusing", age.total_seconds() / 3600)
                    return {
                        "symbols": len(existing.get("rows", [])),
                        "cached": True,
                        "path": str(cache_path),
                    }
        except Exception as exc:
            logger.warning("pead_scanner.refresh_calendar: cache read failed (%s), re-fetching", exc)

    try:
        csv_text = _fetch_earnings_calendar()
    except Exception as exc:
        logger.error("pead_scanner.refresh_calendar: AV fetch failed: %s", exc)
        return {"symbols": 0, "cached": False, "path": str(cache_path), "error": str(exc)}

    rows = _parse_calendar_csv(csv_text)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }
    try:
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as exc:
        logger.error("pead_scanner.refresh_calendar: write failed: %s", exc)
        return {"symbols": len(rows), "cached": False, "path": str(cache_path), "error": str(exc)}

    logger.info("pead_scanner.refresh_calendar: cached %d symbols to %s", len(rows), cache_path)
    return {"symbols": len(rows), "cached": False, "path": str(cache_path)}


# ---------------------------------------------------------------------------
# yfinance helpers (all mockable)
# ---------------------------------------------------------------------------

def _yf_price_volume(ticker: str, lookback_days: int = 25) -> Optional[Dict[str, Any]]:
    """Return today's open, prior close, today's volume, and 20d avg volume.

    Returns None on failure. Uses yfinance daily bars (no prepost).
    """
    try:
        import yfinance as yf  # type: ignore
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{lookback_days}d", interval="1d", auto_adjust=False, prepost=False)
        if hist is None or hist.empty or len(hist) < 2:
            return None
        last = hist.iloc[-1]
        prev = hist.iloc[-2]
        vols = hist["Volume"].dropna()
        avg_vol_20d = float(vols.iloc[-min(21, len(vols)):-1].mean()) if len(vols) >= 2 else 0.0
        today_vol = float(last["Volume"])
        today_open = float(last["Open"])
        today_close = float(last["Close"])
        prior_close = float(prev["Close"])
        price = today_close if today_close > 0 else today_open
        return {
            "open": today_open,
            "close": today_close,
            "prior_close": prior_close,
            "today_volume": today_vol,
            "avg_vol_20d": avg_vol_20d,
            "price": price,
        }
    except Exception as exc:
        logger.debug("pead_scanner._yf_price_volume(%s) failed: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# SUE computation
# ---------------------------------------------------------------------------

def _compute_sue(reported_eps: str, estimated_eps: str) -> Tuple[Optional[float], str]:
    """Compute Standardised Unexpected Earnings percentage.

    SUE = (actual - estimate) / |estimate| × 100

    Edge cases:
      - estimate == 0 → skip (division undefined; return None, reason)
      - estimate missing/non-numeric → skip
      - actual missing/non-numeric → skip
      - negative estimate → valid; |estimate| used as denominator
    Returns (sue_pct, reason_if_skipped).
    """
    try:
        actual = float(reported_eps)
    except (ValueError, TypeError):
        return None, f"non-numeric reportedEPS '{reported_eps}'"

    try:
        estimate = float(estimated_eps)
    except (ValueError, TypeError):
        return None, f"non-numeric estimatedEPS '{estimated_eps}'"

    if estimate == 0.0:
        return None, "estimatedEPS is zero — skipping (undefined SUE)"

    sue_pct = (actual - estimate) / abs(estimate) * 100.0
    return sue_pct, ""


# ---------------------------------------------------------------------------
# ENTRY POINT 2: scan_post_reports(cfg)
# ---------------------------------------------------------------------------

def scan_post_reports(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Morning scan: check yesterday's reporters for PEAD setup.

    Loads the cached EARNINGS_CALENDAR, identifies reporters from yesterday,
    fetches EPS surprise + price/volume gates, routes survivors to
    candidates.evaluate_candidate and candidates.shadow_book_add.

    Returns a summary dict suitable for logging/PM context.
    Short-circuits under pytest without hitting any live API.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        logger.debug("pead_scanner.scan_post_reports: skipping under pytest")
        return {"candidates": [], "skipped": [], "gate_log": [], "skipped_pytest": True}

    today = date.today()
    yesterday = today - timedelta(days=1)
    # Also look back 2 extra days to catch weekend reporters
    lookback_dates = {(yesterday - timedelta(days=d)).isoformat() for d in range(3)}

    # Load calendar
    cache_path = _calendar_path(cfg)
    if not cache_path.is_file():
        logger.warning("pead_scanner.scan_post_reports: no earnings_calendar.json; run refresh_calendar first")
        return {"candidates": [], "skipped": [], "gate_log": [], "error": "no_calendar"}

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        cal_rows = payload.get("rows", [])
    except Exception as exc:
        logger.error("pead_scanner.scan_post_reports: calendar load failed: %s", exc)
        return {"candidates": [], "skipped": [], "gate_log": [], "error": str(exc)}

    # Reporters from lookback window
    reporters = [r for r in cal_rows if r.get("report_date", "") in lookback_dates]
    if not reporters:
        logger.info("pead_scanner.scan_post_reports: no reporters found for %s", yesterday.isoformat())
        return {"candidates": [], "skipped": [], "gate_log": [], "reporters_found": 0}

    max_names = _max_names(cfg)
    reporters = reporters[:max_names]  # cap AV calls

    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    gate_log: List[Dict[str, Any]] = []
    proposals: List[str] = []

    for row in reporters:
        ticker = row["symbol"]
        report_date_str = row.get("report_date", yesterday.isoformat())
        gate: Dict[str, Any] = {"ticker": ticker, "report_date": report_date_str}

        # --- Gate 0: Fetch AV EPS surprise ---
        eps_row = None
        try:
            eps_row = _fetch_earnings_eps(ticker)
        except Exception as exc:
            gate["failed_at"] = "eps_fetch_error"
            gate["error"] = str(exc)
            gate_log.append(gate)
            skipped.append({"ticker": ticker, "reason": f"EPS fetch error: {exc}"})
            continue

        if eps_row is None:
            gate["failed_at"] = "no_eps_data"
            gate_log.append(gate)
            skipped.append({"ticker": ticker, "reason": "no quarterly EPS data from AV"})
            continue

        reported_eps = str(eps_row.get("reportedEPS") or "")
        estimated_eps = str(eps_row.get("estimatedEPS") or eps_row.get("surprisePercentage") and "" or "")
        # AV returns surprisePercentage directly — use it when numeric
        surprise_pct_raw = str(eps_row.get("surprisePercentage") or "")
        sue_pct: Optional[float] = None
        sue_skip_reason = ""

        if surprise_pct_raw:
            try:
                sue_pct = float(surprise_pct_raw)
            except (ValueError, TypeError):
                pass

        if sue_pct is None:
            # Fall back to manual SUE computation
            estimated_eps_val = str(eps_row.get("estimatedEPS") or "")
            sue_pct, sue_skip_reason = _compute_sue(reported_eps, estimated_eps_val)

        gate["reported_eps"] = reported_eps
        gate["estimated_eps"] = str(eps_row.get("estimatedEPS") or "")
        gate["surprise_pct"] = sue_pct

        # --- Gate 1: SUE ≥ +10% ---
        if sue_pct is None:
            gate["failed_at"] = "sue_undefined"
            gate["sue_skip_reason"] = sue_skip_reason
            gate_log.append(gate)
            skipped.append({"ticker": ticker, "reason": f"SUE undefined: {sue_skip_reason}"})
            continue

        if sue_pct < _SUE_MIN_PCT:
            gate["failed_at"] = "sue_below_threshold"
            gate["sue_pct"] = round(sue_pct, 2)
            gate["threshold"] = _SUE_MIN_PCT
            gate_log.append(gate)
            skipped.append({"ticker": ticker, "reason": f"SUE {sue_pct:.1f}% < {_SUE_MIN_PCT}%"})
            continue

        gate["sue_pass"] = True

        # --- Fetch price/volume data ---
        pv = _yf_price_volume(ticker)
        if pv is None:
            gate["failed_at"] = "no_price_data"
            gate_log.append(gate)
            skipped.append({"ticker": ticker, "reason": "no price/volume data from yfinance"})
            continue

        gate.update({
            "open": round(pv["open"], 2),
            "prior_close": round(pv["prior_close"], 2),
            "today_volume": int(pv["today_volume"]),
            "avg_vol_20d": round(pv["avg_vol_20d"], 0),
            "price": round(pv["price"], 2),
        })

        # --- Gate 2: Gap agrees (open > prior close for longs) ---
        if pv["open"] <= pv["prior_close"]:
            gate["failed_at"] = "gap_direction"
            gate["gap_pct"] = round((pv["open"] / pv["prior_close"] - 1.0) * 100.0 if pv["prior_close"] else 0.0, 2)
            gate_log.append(gate)
            skipped.append({
                "ticker": ticker,
                "reason": f"no positive gap: open={pv['open']:.2f} ≤ prior_close={pv['prior_close']:.2f}",
            })
            continue

        gap_pct = (pv["open"] / pv["prior_close"] - 1.0) * 100.0
        gate["gap_pct"] = round(gap_pct, 2)
        gate["gap_pass"] = True

        # --- Gate 3: RVOL ≥ 1.5 ---
        avg_vol = pv["avg_vol_20d"]
        today_vol = pv["today_volume"]
        rvol = (today_vol / avg_vol) if avg_vol > 0 else 0.0
        gate["rvol"] = round(rvol, 2)

        if rvol < _RVOL_MIN:
            gate["failed_at"] = "rvol_below_threshold"
            gate["threshold"] = _RVOL_MIN
            gate_log.append(gate)
            skipped.append({
                "ticker": ticker,
                "reason": f"RVOL {rvol:.2f} < {_RVOL_MIN} (vol={today_vol:.0f}, avg={avg_vol:.0f})",
            })
            continue

        gate["rvol_pass"] = True

        # --- Gate 4: Dollar volume ≥ $5M ---
        dollar_vol = pv["price"] * today_vol
        gate["dollar_vol"] = round(dollar_vol, 0)

        if dollar_vol < _DOLLAR_VOL_MIN:
            gate["failed_at"] = "dollar_vol_below_threshold"
            gate["threshold"] = _DOLLAR_VOL_MIN
            gate_log.append(gate)
            skipped.append({
                "ticker": ticker,
                "reason": f"dollar vol ${dollar_vol:,.0f} < ${_DOLLAR_VOL_MIN:,.0f}",
            })
            continue

        gate["dollar_vol_pass"] = True
        gate["passed"] = True
        gate_log.append(gate)

        # --- All gates passed ---
        candidates.append({
            "ticker": ticker,
            "report_date": report_date_str,
            "sue_pct": round(sue_pct, 2),
            "gap_pct": round(gap_pct, 2),
            "rvol": round(rvol, 2),
            "dollar_vol": round(dollar_vol, 0),
            "open": round(pv["open"], 2),
            "prior_close": round(pv["prior_close"], 2),
            "price": round(pv["price"], 2),
        })

    # --- Route survivors through evaluate_candidate ---
    scanned_at = datetime.now(timezone.utc).isoformat()
    if candidates:
        try:
            from tradingagents.portfolio_advisor import candidates as _cands
            for c in candidates:
                ticker = c["ticker"]
                raw = {
                    "ticker": ticker,
                    "strategy": "catalyst",
                    "catalyst": f"earnings beat {c['sue_pct']:+.1f}% surprise",
                    "catalyst_date": c["report_date"],
                    "reason": (
                        f"PEAD: EPS surprise {c['sue_pct']:+.1f}%, "
                        f"gap {c['gap_pct']:+.1f}%, RVOL {c['rvol']:.1f}x, "
                        f"dollar vol ${c['dollar_vol']:,.0f}"
                    ),
                    "liquidity_ok": True,
                    "source": "pead_scanner",
                    "priority": 2,
                }
                try:
                    rec = _cands.evaluate_candidate(raw, cfg=cfg, default_source="pead_scanner")
                    _cands.append_candidate_records(cfg, [rec])
                    proposals.append(ticker)

                    # Gate-passers that did NOT become proposals → shadow book
                    if rec.status != "promoted":
                        gates_passed = [
                            g for g in ["sue", "gap", "rvol", "dollar_vol"] if True
                        ]
                        _cands.shadow_book_add(
                            cfg,
                            ticker=ticker,
                            source="pead_scanner",
                            reason=raw["reason"],
                            strategy="catalyst",
                            catalyst_date=c["report_date"],
                            gates_passed=["sue", "gap", "rvol", "dollar_vol"],
                            entry_price=c["price"],
                        )
                except Exception as exc:
                    logger.error("pead_scanner: evaluate_candidate(%s) failed: %s", ticker, exc)
        except Exception as exc:
            logger.error("pead_scanner: candidate routing failed: %s", exc)

    result = {
        "scanned_at": scanned_at,
        "reporters_found": len(reporters),
        "candidates": candidates,
        "skipped": skipped,
        "gate_log": gate_log,
        "proposals_routed": proposals,
    }
    logger.info(
        "pead_scanner.scan_post_reports: %d reporters → %d candidates, %d skipped",
        len(reporters), len(candidates), len(skipped),
    )
    return result
