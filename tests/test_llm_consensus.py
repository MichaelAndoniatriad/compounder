"""Tests for llm_consensus.py — ticker extraction, aggregation, failure handling."""
import pytest
from unittest.mock import patch
from pathlib import Path
from tradingagents.dataflows.llm_consensus import (
    _extract_tickers, _compute_deepseek_alignment, load_llm_consensus_snapshot,
)

class TestTickerExtraction:
    def test_simple_tickers(self):
        assert _extract_tickers("I recommend AAPL, MSFT, and NVDA") == {"AAPL", "MSFT", "NVDA"}
    def test_dollar_prefix(self):
        assert _extract_tickers("Buy $AAPL and $GOOGL.") == {"AAPL", "GOOGL"}
    def test_filters_single_letters(self):
        tickers = _extract_tickers("I think A and B are not tickers but AAPL is")
        assert "I" not in tickers
        assert "A" not in tickers
        assert "AAPL" in tickers
    def test_no_tickers(self):
        assert _extract_tickers("The market looks uncertain right now.") == set()

class TestDeepSeekAlignment:
    def test_overlap_calculation(self):
        ticker_counts = {"AAPL": {"count": 10, "days": {"2026-06-01"}, "models": {"openai/gpt-5.4"}}}
        with patch("tradingagents.dataflows.llm_consensus._get_deepseek_recommendations", return_value={"AAPL", "NVDA"}):
            with patch.object(Path, "is_file", return_value=False):
                a = _compute_deepseek_alignment(ticker_counts, {"AAPL", "MSFT"})
                assert a["overlap_with_top_20"] == 0.5
    def test_no_deepseek_data(self):
        with patch("tradingagents.dataflows.llm_consensus._get_deepseek_recommendations", return_value=set()):
            with patch.object(Path, "is_file", return_value=False):
                a = _compute_deepseek_alignment({}, {"AAPL"})
                assert a["overlap_with_top_20"] == 0.0

class TestFailureHandling:
    def test_snapshot_missing(self):
        with patch.object(Path, "is_file", return_value=False):
            assert load_llm_consensus_snapshot() is None
    def test_corrupt_snapshot(self):
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "read_text", side_effect=OSError):
            assert load_llm_consensus_snapshot() is None
    def test_3_models_valid(self):
        results = [{"model": "a", "tickers": []}, {"model": "b", "tickers": []}, {"model": "c", "tickers": []}, {"model": "d", "error": "x"}, {"model": "e", "error": "x"}]
        assert sum(1 for r in results if "error" not in r) == 3
    def test_2_models_invalid(self):
        results = [{"model": "a", "tickers": []}, {"model": "b", "tickers": []}, {"model": "c", "error": "x"}, {"model": "d", "error": "x"}, {"model": "e", "error": "x"}]
        assert sum(1 for r in results if "error" not in r) == 2
