"""Tests for consensus_score.py — scoring helpers."""
import pytest
from unittest.mock import patch

class TestConsensusEntryScore:
    def test_fresh_entry(self):
        from tradingagents.portfolio_advisor.consensus_score import consensus_entry_score
        snap = {"top_20": [{"ticker": "AAPL", "days_in_top_20": 5}]}
        with patch("tradingagents.portfolio_advisor.consensus_score._load", return_value=snap):
            assert consensus_entry_score("AAPL") == 0.5
    def test_mature(self):
        from tradingagents.portfolio_advisor.consensus_score import consensus_entry_score
        snap = {"top_20": [{"ticker": "AAPL", "days_in_top_20": 30}]}
        with patch("tradingagents.portfolio_advisor.consensus_score._load", return_value=snap):
            assert consensus_entry_score("AAPL") == 0.0
    def test_stale(self):
        from tradingagents.portfolio_advisor.consensus_score import consensus_entry_score
        snap = {"top_20": [{"ticker": "AAPL", "days_in_top_20": 100}]}
        with patch("tradingagents.portfolio_advisor.consensus_score._load", return_value=snap):
            assert consensus_entry_score("AAPL") == -0.3
    def test_negative_universe(self):
        from tradingagents.portfolio_advisor.consensus_score import consensus_entry_score
        with patch("tradingagents.portfolio_advisor.consensus_score._load", return_value={"top_20": []}):
            assert consensus_entry_score("MSFT") == 0.3

class TestConsensusDivergence:
    def test_herding(self):
        from tradingagents.portfolio_advisor.consensus_score import consensus_divergence_score
        snap = {"deepseek_alignment": {"deepseek_last_recommended": ["AAPL"]}, "top_20": [{"ticker": "AAPL"}]}
        with patch("tradingagents.portfolio_advisor.consensus_score._load", return_value=snap):
            assert consensus_divergence_score("AAPL") == -0.4
    def test_divergent(self):
        from tradingagents.portfolio_advisor.consensus_score import consensus_divergence_score
        snap = {"deepseek_alignment": {"deepseek_last_recommended": ["MSFT"]}, "top_20": [{"ticker": "AAPL"}]}
        with patch("tradingagents.portfolio_advisor.consensus_score._load", return_value=snap):
            assert consensus_divergence_score("MSFT") == 0.4

class TestCompositeScore:
    def test_mean(self):
        from tradingagents.portfolio_advisor.consensus_score import compute_composite_consensus_score
        snap = {"top_20": [{"ticker": "AAPL", "days_in_top_20": 5}],
                "deepseek_alignment": {"deepseek_last_recommended": ["AAPL"]}}
        with patch("tradingagents.portfolio_advisor.consensus_score._load", return_value=snap), \
             patch("tradingagents.portfolio_advisor.consensus_score.consensus_retail_flow_score", return_value=0.2):
            score = compute_composite_consensus_score("AAPL")
            # entry=0.5, divergence=-0.4, flow=0.2 → composite = 0.1
            assert score["composite"] == pytest.approx(0.1, abs=0.01)
