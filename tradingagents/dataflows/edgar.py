"""SEC EDGAR data client for TradingAgents.

Provides free, fair-use access to SEC EDGAR APIs. Follows SEC fair-access
policy: User-Agent header identifies this application and the responsible
party, request rate is capped at ≤10 req/s, and 429/403 responses trigger
a single exponential-backoff retry.

API routes chosen
-----------------
We use two EDGAR endpoints:

1. **Ticker→CIK map**: https://www.sec.gov/files/company_tickers.json
   A single JSON download (≈10K entries) that maps CIK integer → {ticker,
   title}. Cached locally for 7 days. Fast and reliable — one request loads
   the whole universe.

2. **Per-company submissions**: https://data.sec.gov/submissions/CIK<10d>.json
   Each company's full filing history as a JSON object. The ``filings.recent``
   sub-object contains parallel arrays: ``form``, ``filingDate``, ``items``,
   ``accessionNumber``, ``primaryDocument``, etc. The ``items`` field is a
   comma-separated string of 8-K item numbers, e.g. ``"2.02,9.01"``.

Why not the EDGAR full-text search (efts.sec.gov) or daily index?
  - EDGAR full-text search (EFTS) is powerful but requires form-type filtering
    across the whole corpus and returns paginated results — more requests than
    submissions JSON, harder to map back to tickers.
  - Daily index (https://www.sec.gov/Archives/edgar/daily-index/) is a text
    directory listing that requires parsing a .idx file. More fragile and still
    requires per-CIK lookups to get filing details.
  - Submissions JSON is the cleanest: one request per company, structured JSON,
    includes item numbers and filing dates directly. Given we only care about
    active tickers in our watchlist (typically <600), the per-company approach
    is efficient and straightforward.

PYTEST guard
------------
All network calls return [] when ``PYTEST_CURRENT_TEST`` is in the environment,
UNLESS the caller passes ``_allow_in_test=True`` (used by unit tests that inject
fixture data via the parse-only helpers).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# SEC fair-access User-Agent: application name + contact email per
# https://www.sec.gov/developer
_USER_AGENT = "TradingAgents research mikelhazboun@gmail.com"

# Rate-limit: ≤10 req/s. We sleep this many seconds between requests.
_MIN_REQUEST_INTERVAL_S = 0.11  # ~9 req/s, safely under 10

# Ticker→CIK cache TTL: 7 days (the file is static for weeks at a time).
_CIK_MAP_TTL_DAYS = 7

# companyfacts cache TTL: 7 days (XBRL facts update at most quarterly).
_COMPANYFACTS_TTL_DAYS = 7

# Backoff on 429/403: wait this many seconds before one retry.
_BACKOFF_S = 6.0

# The 8-K item numbers we care about by default.
_DEFAULT_8K_ITEMS: Tuple[str, ...] = ("2.02", "8.01", "5.02")

_last_request_time: float = 0.0


def _throttle() -> None:
    """Sleep if needed to stay under the 10 req/s limit."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL_S:
        time.sleep(_MIN_REQUEST_INTERVAL_S - elapsed)
    _last_request_time = time.monotonic()


def _fetch_url(url: str, *, timeout: int = 15) -> bytes:
    """Fetch ``url`` with the required User-Agent header.

    On 429 or 403, waits ``_BACKOFF_S`` seconds and retries once.
    Raises ``urllib.error.URLError`` / ``urllib.error.HTTPError`` on failure.
    """
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 403):
            logger.debug("EDGAR %s %s — backing off %.1fs", exc.code, url, _BACKOFF_S)
            time.sleep(_BACKOFF_S)
            _throttle()
            req2 = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req2, timeout=timeout) as resp:
                return resp.read()
        raise


def _cache_dir() -> Path:
    """Return the data_cache_dir path used by other dataflows.

    Reads TRADINGAGENTS_CACHE_DIR if set, otherwise uses
    ``~/.tradingagents/cache``.
    """
    base = os.environ.get("TRADINGAGENTS_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".tradingagents", "cache"
    )
    p = Path(base) / "edgar"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Ticker → CIK map
# ---------------------------------------------------------------------------


def _cik_map_path() -> Path:
    return _cache_dir() / "company_tickers.json"


def _load_cik_map_from_cache() -> Optional[Dict[str, Any]]:
    """Return cached CIK map if it exists and is fresh. None otherwise."""
    p = _cik_map_path()
    if not p.exists():
        return None
    age_days = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 86400
    if age_days > _CIK_MAP_TTL_DAYS:
        return None
    try:
        return json.loads(p.read_bytes())
    except Exception:
        return None


def _download_cik_map() -> Dict[str, Any]:
    """Download company_tickers.json from SEC and cache it. Returns the raw dict."""
    url = "https://www.sec.gov/files/company_tickers.json"
    raw = _fetch_url(url)
    data = json.loads(raw)
    try:
        _cik_map_path().write_bytes(raw)
    except Exception:
        pass
    return data


def build_cik_to_ticker(raw_map: Dict[str, Any]) -> Dict[int, str]:
    """Convert the SEC company_tickers JSON to {cik_int: ticker_upper}.

    The SEC JSON has string keys mapping to {cik_str, ticker, title}.
    """
    out: Dict[int, str] = {}
    for entry in raw_map.values():
        cik = entry.get("cik_str")
        tk = (entry.get("ticker") or "").strip().upper()
        if cik and tk:
            out[int(cik)] = tk
    return out


def build_ticker_to_cik(raw_map: Dict[str, Any]) -> Dict[str, int]:
    """Convert the SEC company_tickers JSON to {ticker_upper: cik_int}."""
    out: Dict[str, int] = {}
    for entry in raw_map.values():
        cik = entry.get("cik_str")
        tk = (entry.get("ticker") or "").strip().upper()
        if cik and tk:
            out[tk] = int(cik)
    return out


def get_cik_maps(
    *, _allow_in_test: bool = False
) -> Tuple[Dict[int, str], Dict[str, int]]:
    """Return (cik→ticker, ticker→cik) maps, using cache when fresh.

    Returns empty dicts under pytest unless ``_allow_in_test=True``.
    """
    if "PYTEST_CURRENT_TEST" in os.environ and not _allow_in_test:
        return {}, {}
    raw = _load_cik_map_from_cache()
    if raw is None:
        raw = _download_cik_map()
    cik_to_tk = build_cik_to_ticker(raw)
    tk_to_cik = build_ticker_to_cik(raw)
    return cik_to_tk, tk_to_cik


# ---------------------------------------------------------------------------
# Submissions JSON parser (testable without network)
# ---------------------------------------------------------------------------


def parse_recent_8k_filings(
    submissions_json: Dict[str, Any],
    cik: int,
    cik_to_ticker: Dict[int, str],
    *,
    hours: int = 24,
    items: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Parse a submissions JSON dict and return matching 8-K filings.

    Parameters
    ----------
    submissions_json:
        Parsed JSON from ``https://data.sec.gov/submissions/CIK########.json``.
    cik:
        The CIK integer for this company (used for URL construction).
    cik_to_ticker:
        Lookup from CIK → ticker string. Filings whose CIK has no ticker are
        dropped.
    hours:
        How far back to look (``filingDate`` / ``acceptanceDateTime`` compared
        to now).
    items:
        Set of 8-K item numbers to include, e.g. ``{"2.02", "8.01"}``.
        None means include any 8-K.

    Returns
    -------
    List of dicts: [{ticker, item, title, filed_at, url}].
    """
    if items is None:
        items = set(_DEFAULT_8K_ITEMS)

    ticker = cik_to_ticker.get(int(cik))
    if not ticker:
        return []

    recent = (submissions_json.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    acceptance_times = recent.get("acceptanceDateTime") or []
    item_lists = recent.get("items") or []
    accession_numbers = recent.get("accessionNumber") or []
    primary_docs = recent.get("primaryDocument") or []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    results: List[Dict[str, Any]] = []

    for i, form in enumerate(forms):
        if form != "8-K":
            continue

        # Parse filing date/time.
        filed_at: Optional[datetime] = None
        acc_ts = acceptance_times[i] if i < len(acceptance_times) else ""
        fd_str = filing_dates[i] if i < len(filing_dates) else ""
        if acc_ts:
            try:
                # acceptanceDateTime format: "2026-05-20T12:34:56.789Z"
                ts = acc_ts.replace("Z", "+00:00")
                filed_at = datetime.fromisoformat(ts)
                if filed_at.tzinfo is None:
                    filed_at = filed_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        if filed_at is None and fd_str:
            try:
                filed_at = datetime.fromisoformat(fd_str).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        if filed_at is None or filed_at < cutoff:
            continue

        # Check item numbers.
        raw_items_str = item_lists[i] if i < len(item_lists) else ""
        filing_items = {s.strip() for s in str(raw_items_str).split(",")} if raw_items_str else set()
        matched_items = filing_items & items
        if not matched_items:
            continue

        # Build URL from accession number.
        acc = accession_numbers[i] if i < len(accession_numbers) else ""
        doc = primary_docs[i] if i < len(primary_docs) else ""
        cik_padded = str(int(cik)).zfill(10)
        if acc and doc:
            acc_clean = acc.replace("-", "")
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{acc_clean}/{doc}"
            )
        elif acc:
            acc_clean = acc.replace("-", "")
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/"
            )
        else:
            url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_padded}&type=8-K"

        # Title from entity name in submissions JSON.
        entity_name = (submissions_json.get("name") or ticker).strip()

        for item_num in sorted(matched_items):
            results.append({
                "ticker": ticker,
                "item": item_num,
                "title": entity_name,
                "filed_at": filed_at.isoformat(),
                "url": url,
            })

    return results


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------


def fetch_recent_8k_items(
    *,
    hours: int = 24,
    items: Optional[Set[str]] = None,
    tickers: Optional[List[str]] = None,
    _allow_in_test: bool = False,
) -> List[Dict[str, Any]]:
    """Fetch 8-K filings across SEC EDGAR for the given items and time window.

    Parameters
    ----------
    hours:
        How far back to look (filing acceptance time vs now UTC).
    items:
        Set of 8-K item numbers to include.
        Default: ``{"2.02", "8.01", "5.02"}`` (earnings, other events, director changes).
    tickers:
        Restrict to these ticker symbols (upper-case). None means all tickers
        in the CIK map (expensive — use with a small list in production).
    _allow_in_test:
        Set True in pytest fixtures to bypass the PYTEST guard.

    Returns
    -------
    List of dicts sorted by filed_at descending:
        [{ticker, item, title, filed_at, url}, ...]

    Returns [] under pytest unless ``_allow_in_test=True``.
    """
    if "PYTEST_CURRENT_TEST" in os.environ and not _allow_in_test:
        return []

    if items is None:
        items = set(_DEFAULT_8K_ITEMS)

    cik_to_tk, tk_to_cik = get_cik_maps(_allow_in_test=_allow_in_test)
    if not cik_to_tk:
        logger.warning("EDGAR: CIK map empty — cannot fetch 8-K items")
        return []

    # Determine which CIKs to query.
    if tickers:
        upper_tickers = [t.upper() for t in tickers]
        target_ciks = {tk_to_cik[tk]: tk for tk in upper_tickers if tk in tk_to_cik}
    else:
        target_ciks = {cik: tk for cik, tk in cik_to_tk.items()}

    if not target_ciks:
        return []

    # Cutoff time for filtering.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    all_results: List[Dict[str, Any]] = []

    for cik in target_ciks:
        cik_padded = str(int(cik)).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        try:
            raw = _fetch_url(url)
            sub_json = json.loads(raw)
        except Exception as exc:
            logger.debug("EDGAR submissions fetch failed for CIK %s: %s", cik, exc)
            continue

        filings = parse_recent_8k_filings(
            sub_json, cik, cik_to_tk, hours=hours, items=items
        )
        all_results.extend(filings)

    # Sort by filed_at descending.
    all_results.sort(key=lambda x: x.get("filed_at") or "", reverse=True)
    return all_results


# ---------------------------------------------------------------------------
# XBRL companyfacts — revenue growth + gross margin for small-cap R5 funnel
# ---------------------------------------------------------------------------


def _companyfacts_cache_path(cik: int) -> Path:
    return _cache_dir() / "companyfacts" / f"cik{str(cik).zfill(10)}.json"


def _load_companyfacts_from_cache(cik: int) -> Optional[Dict[str, Any]]:
    p = _companyfacts_cache_path(cik)
    if not p.exists():
        return None
    age_days = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 86400
    if age_days > _COMPANYFACTS_TTL_DAYS:
        return None
    try:
        return json.loads(p.read_bytes())
    except Exception:
        return None


def _save_companyfacts_to_cache(cik: int, data: bytes) -> None:
    p = _companyfacts_cache_path(cik)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_bytes(data)
    except Exception:
        pass


def _extract_annual_revenue(facts: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Extract (latest_annual_revenue, prior_year_revenue) from XBRL facts.

    Returns None when insufficient data is present. Tries us-gaap
    RevenueFromContractWithCustomerExcludingAssessedTax first, then Revenues.
    Looks only at 10-K frames (annual period, no instantaneous facts).
    """
    us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    candidates = [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ]
    for concept in candidates:
        concept_data = us_gaap.get(concept) or {}
        units = concept_data.get("units") or {}
        usd_units = units.get("USD") or []
        # Filter for annual 10-K facts (form field present and duration of ~365d)
        annual_entries = []
        for entry in usd_units:
            form = str(entry.get("form") or "").strip().upper()
            start = str(entry.get("start") or "").strip()
            end = str(entry.get("end") or "").strip()
            if form != "10-K":
                continue
            if not start or not end:
                continue
            try:
                from datetime import date as _date
                s = _date.fromisoformat(start)
                e = _date.fromisoformat(end)
                days = (e - s).days
                # Accept 300-400 day windows as annual (accommodates fiscal year boundaries)
                if 300 <= days <= 400:
                    annual_entries.append((e, int(entry.get("val") or 0)))
            except (ValueError, TypeError):
                continue
        if len(annual_entries) >= 2:
            annual_entries.sort(key=lambda x: x[0], reverse=True)
            latest_rev = float(annual_entries[0][1])
            prior_rev = float(annual_entries[1][1])
            return latest_rev, prior_rev
    return None


def _extract_gross_profit(facts: Dict[str, Any]) -> Optional[float]:
    """Extract latest annual gross profit from XBRL facts. Returns None on miss."""
    us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    concept_data = us_gaap.get("GrossProfit") or {}
    units = concept_data.get("units") or {}
    usd_units = units.get("USD") or []
    annual_entries = []
    for entry in usd_units:
        form = str(entry.get("form") or "").strip().upper()
        start = str(entry.get("start") or "").strip()
        end = str(entry.get("end") or "").strip()
        if form != "10-K" or not start or not end:
            continue
        try:
            from datetime import date as _date
            s = _date.fromisoformat(start)
            e = _date.fromisoformat(end)
            if 300 <= (e - s).days <= 400:
                annual_entries.append((e, int(entry.get("val") or 0)))
        except (ValueError, TypeError):
            continue
    if annual_entries:
        annual_entries.sort(key=lambda x: x[0], reverse=True)
        return float(annual_entries[0][1])
    return None


def _parse_companyfacts(raw_facts: Dict[str, Any]) -> Dict[str, Any]:
    """Parse XBRL companyfacts JSON into a fundamentals dict.

    Returns a dict with keys: rev_latest, rev_prior, rev_growth (fraction),
    gross_profit_latest, gross_margin (fraction or None), ticker, cik_str.
    Missing/insufficient data → the corresponding key is None.
    """
    entity_name = raw_facts.get("entityName") or ""
    cik_str = str(raw_facts.get("cik") or "").strip()

    rev_data = _extract_annual_revenue(raw_facts)
    gross_profit = _extract_gross_profit(raw_facts)

    rev_latest = rev_prior = rev_growth = None
    if rev_data is not None:
        rev_latest, rev_prior = rev_data
        if rev_prior and rev_prior != 0:
            rev_growth = (rev_latest - rev_prior) / abs(rev_prior)

    gross_margin = None
    if gross_profit is not None and rev_latest and rev_latest != 0:
        gross_margin = gross_profit / rev_latest

    return {
        "entity_name": entity_name,
        "cik_str": cik_str,
        "rev_latest": rev_latest,
        "rev_prior": rev_prior,
        "rev_growth": rev_growth,
        "gross_profit_latest": gross_profit,
        "gross_margin": gross_margin,
    }


def companyfacts(ticker: str, *, _allow_in_test: bool = False) -> Optional[Dict[str, Any]]:
    """Fetch and parse SEC XBRL companyfacts for a ticker.

    Uses https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
    with the same throttle/UA/caching infrastructure from the 8-K feed.
    Cache TTL: 7 days.

    Returns a dict with {entity_name, cik_str, rev_latest, rev_prior,
    rev_growth, gross_profit_latest, gross_margin} or None when the
    ticker has no CIK mapping or the fetch fails.

    Returns None under pytest unless ``_allow_in_test=True``.
    """
    if "PYTEST_CURRENT_TEST" in os.environ and not _allow_in_test:
        return None

    tk = (ticker or "").strip().upper()
    if not tk:
        return None

    _, tk_to_cik = get_cik_maps(_allow_in_test=_allow_in_test)
    cik = tk_to_cik.get(tk)
    if not cik:
        logger.debug("companyfacts: no CIK for ticker %s", tk)
        return None

    cached = _load_companyfacts_from_cache(cik)
    if cached is not None:
        return _parse_companyfacts(cached)

    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    try:
        raw_bytes = _fetch_url(url)
    except Exception as exc:
        logger.debug("companyfacts fetch failed for %s (CIK %s): %s", tk, cik, exc)
        return None

    try:
        raw_facts = json.loads(raw_bytes)
    except Exception:
        return None

    _save_companyfacts_to_cache(cik, raw_bytes)
    return _parse_companyfacts(raw_facts)
