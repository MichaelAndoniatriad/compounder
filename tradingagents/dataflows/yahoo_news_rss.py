"""Yahoo Finance news RSS adapter.

Implements the Option B structural fix from docs/ep_gate_audit.md. The EP scanner
needs a second news feed to complement Alpha Vantage; this module produces news
items in the same shape so the existing classifier and gates work unchanged.

Output schema (matches Alpha Vantage):
{
  "title": str,
  "summary": str,
  "source": str,
  "url": str,
  "time_published": "YYYYMMDDTHHMMSS",
  "ticker_sentiment": [
    {"ticker": "AAPL", "relevance_score": "0.5", "ticker_sentiment_label": ""}
  ]
}

Yahoo Finance RSS does not provide ticker_sentiment or relevance scores natively;
this module assigns a default relevance of 0.5 to extracted tickers. The EP
scanner's existing low_relevance threshold (0.3) still applies.

The default endpoint is the Yahoo Finance topic RSS for market headlines. It is
configurable via env so Hermes or future users can swap to a different feed
without code changes.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.request
from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# Default Yahoo Finance RSS endpoint. Override via env if needed.
DEFAULT_RSS_URL = os.environ.get(
    "YAHOO_FINANCE_RSS_URL",
    "https://finance.yahoo.com/news/rssindex",
)

# Ticker pattern. Matches $TICKER or (TICKER) or "Apple (AAPL)" style.
_TICKER_PATTERN = re.compile(r"\$([A-Z]{1,5})\b|\(([A-Z]{1,5})\)")

# Common false positives to skip when extracting tickers.
_TICKER_NOISE = {
    "USD", "GDP", "CEO", "CFO", "AI", "IPO", "ETF", "PCE", "CPI", "FED", "FOMC",
    "EU", "US", "UK", "Q1", "Q2", "Q3", "Q4", "GAAP", "EPS", "FDA", "SEC", "DOJ",
    "EV", "ESG", "API", "URL", "NYC", "LA", "DC", "NY",
}


def _http_get(url: str, timeout: float = 10.0) -> Optional[bytes]:
    """Fetch URL contents with a sensible User-Agent. Returns None on failure."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "compounder/1.0 (+https://github.com/MichaelAndoniatriad/compounder)",
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.warning("yahoo_news_rss: HTTP fetch failed for %s: %s", url, e)
        return None


def _parse_rfc822_date(value: str) -> Optional[datetime]:
    """Parse RSS pubDate (RFC 822) into a UTC datetime."""
    if not value:
        return None
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _extract_tickers(text: str) -> List[str]:
    """Extract probable ticker symbols from a title + summary blob."""
    out: List[str] = []
    seen: set = set()
    for match in _TICKER_PATTERN.findall(text or ""):
        for group in match:
            if not group:
                continue
            symbol = group.strip().upper()
            if not symbol or len(symbol) > 5:
                continue
            if symbol in _TICKER_NOISE:
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            out.append(symbol)
    return out


def _format_av_time(dt: datetime) -> str:
    """Convert a datetime to Alpha Vantage time_published format."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _parse_rss(xml_bytes: bytes, default_source: str = "Yahoo Finance") -> List[Dict[str, Any]]:
    """Parse an RSS XML document into a list of items in Alpha Vantage shape."""
    items: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("yahoo_news_rss: XML parse failed: %s", e)
        return items

    # RSS 2.0 puts items under <channel><item>; Atom uses <entry>. Support both
    # by finding any element ending in 'item' or 'entry'.
    for item in root.iter():
        tag_local = item.tag.split("}")[-1] if "}" in item.tag else item.tag
        if tag_local not in ("item", "entry"):
            continue

        title = ""
        summary = ""
        url = ""
        pub_date_str = ""
        for child in item:
            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            text = (child.text or "").strip()
            if ctag == "title":
                title = text
            elif ctag in ("description", "summary", "content"):
                summary = text
            elif ctag == "link":
                url = text or child.attrib.get("href", "")
            elif ctag in ("pubDate", "published", "updated"):
                pub_date_str = text

        if not title:
            continue

        dt = _parse_rfc822_date(pub_date_str)
        time_published = _format_av_time(dt) if dt else _format_av_time(datetime.now(timezone.utc))

        blob = f"{title} {summary}"
        tickers = _extract_tickers(blob)
        ticker_sentiment = [
            {
                "ticker": tk,
                "relevance_score": "0.5",
                "ticker_sentiment_label": "",
            }
            for tk in tickers
        ]

        items.append({
            "title": title[:300],
            "summary": summary[:1000],
            "source": default_source,
            "url": url,
            "time_published": time_published,
            "ticker_sentiment": ticker_sentiment,
        })

    return items


def get_market_news(
    today: date,
    look_back_days: int = 2,
    limit: int = 200,
    rss_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Pull recent market news from Yahoo Finance RSS, return in Alpha Vantage shape.

    Filters items to the look_back_days window. Caps return at `limit`. Items
    without extractable tickers are still returned (the classifier inspects
    title+summary regardless).
    """
    url = rss_url or DEFAULT_RSS_URL
    body = _http_get(url)
    if body is None:
        return []

    items = _parse_rss(body, default_source="Yahoo Finance")

    if look_back_days >= 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=look_back_days + 1)
        filtered: List[Dict[str, Any]] = []
        for it in items:
            ts = it.get("time_published", "")
            try:
                dt = datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                filtered.append(it)
                continue
            if dt >= cutoff:
                filtered.append(it)
        items = filtered

    return items[:limit]
