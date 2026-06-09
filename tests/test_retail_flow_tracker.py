"""Tests for retail_flow_tracker.py — parsing, fallback."""
import pytest
from tradingagents.dataflows.retail_flow_tracker import _parse_pct, get_retail_flow_share

class TestRetailFlowParsing:
    def test_parse_pct(self):
        assert _parse_pct("12.5%") == pytest.approx(0.125)
        assert _parse_pct("5%") == pytest.approx(0.05)
    def test_no_cache_returns_none(self):
        assert get_retail_flow_share("ZZZTICKER") is None
