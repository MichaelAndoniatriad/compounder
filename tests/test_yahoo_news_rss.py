"""Tests for yahoo_news_rss adapter.

Covers ticker extraction, RSS parsing, date filtering, and the get_market_news
public entry. HTTP is mocked; no real network calls.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import List
from unittest.mock import patch

import pytest

from tradingagents.dataflows import yahoo_news_rss as ynr


def test_extract_tickers_basic():
    assert ynr._extract_tickers("Apple ($AAPL) and Microsoft (MSFT) report") == ["AAPL", "MSFT"]


def test_extract_tickers_filters_noise():
    text = "USD strength impacts CEO decisions; AAPL beats EPS"
    tickers = ynr._extract_tickers(text)
    assert "USD" not in tickers
    assert "CEO" not in tickers
    assert "EPS" not in tickers
    # AAPL not in this text (no $ or parens)


def test_extract_tickers_deduplicates():
    tickers = ynr._extract_tickers("$NVDA up, $NVDA earnings, (NVDA) results")
    assert tickers == ["NVDA"]


def test_extract_tickers_length_limit():
    tickers = ynr._extract_tickers("$ABCDEFGH should not match (six+ chars)")
    assert "ABCDEFGH" not in tickers


def _make_rss(items_xml: List[str]) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        '<title>Test feed</title>'
        + "".join(items_xml)
        + '</channel></rss>'
    ).encode("utf-8")


def test_parse_rss_extracts_items():
    now = datetime.now(timezone.utc)
    pub = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    xml = _make_rss([
        f"<item><title>NVDA ($NVDA) beats EPS estimates</title>"
        f"<description>The chip maker reported record revenue.</description>"
        f"<link>https://example.com/nvda-beats</link>"
        f"<pubDate>{pub}</pubDate></item>"
    ])
    items = ynr._parse_rss(xml)
    assert len(items) == 1
    item = items[0]
    assert "NVDA" in item["title"]
    assert item["url"] == "https://example.com/nvda-beats"
    assert item["source"] == "Yahoo Finance"
    assert any(t["ticker"] == "NVDA" for t in item["ticker_sentiment"])
    # Relevance is the placeholder 0.5
    assert item["ticker_sentiment"][0]["relevance_score"] == "0.5"


def test_parse_rss_handles_missing_pub_date():
    xml = _make_rss([
        "<item><title>$MSFT news</title><description>Story</description><link>http://x</link></item>"
    ])
    items = ynr._parse_rss(xml)
    assert len(items) == 1
    # time_published defaults to "now" formatted
    assert len(items[0]["time_published"]) == 15


def test_parse_rss_handles_malformed():
    items = ynr._parse_rss(b"<not valid xml>")
    assert items == []


def test_get_market_news_filters_by_lookback():
    now = datetime.now(timezone.utc)
    old_pub = (now - timedelta(days=10)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    fresh_pub = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    xml = _make_rss([
        f"<item><title>Old $A news</title><description>x</description><link>http://old</link><pubDate>{old_pub}</pubDate></item>",
        f"<item><title>Fresh $B news</title><description>x</description><link>http://fresh</link><pubDate>{fresh_pub}</pubDate></item>",
    ])
    with patch.object(ynr, "_http_get", return_value=xml):
        items = ynr.get_market_news(today=date.today(), look_back_days=2)
    urls = {it["url"] for it in items}
    assert "http://fresh" in urls
    assert "http://old" not in urls


def test_get_market_news_returns_empty_on_fetch_failure():
    with patch.object(ynr, "_http_get", return_value=None):
        items = ynr.get_market_news(today=date.today())
    assert items == []


def test_get_market_news_respects_limit():
    now = datetime.now(timezone.utc)
    pub = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    items_xml = [
        f"<item><title>News {i} $X</title><description>x</description>"
        f"<link>http://x{i}</link><pubDate>{pub}</pubDate></item>"
        for i in range(20)
    ]
    xml = _make_rss(items_xml)
    with patch.object(ynr, "_http_get", return_value=xml):
        items = ynr.get_market_news(today=date.today(), limit=5)
    assert len(items) == 5


def test_av_shape_compatibility():
    """Items must have all keys the EP scanner expects from Alpha Vantage."""
    now = datetime.now(timezone.utc)
    pub = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    xml = _make_rss([
        f"<item><title>$NVDA beats EPS</title><description>x</description>"
        f"<link>http://x</link><pubDate>{pub}</pubDate></item>"
    ])
    with patch.object(ynr, "_http_get", return_value=xml):
        items = ynr.get_market_news(today=date.today())
    assert items
    item = items[0]
    # Required keys for EP scanner _bucket_by_ticker
    for key in ("title", "summary", "source", "url", "time_published", "ticker_sentiment"):
        assert key in item
    # ticker_sentiment items must have the AV fields
    for ts in item["ticker_sentiment"]:
        assert "ticker" in ts
        assert "relevance_score" in ts
        assert "ticker_sentiment_label" in ts
