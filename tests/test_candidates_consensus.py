"""Tests for candidates.py consensus gate."""
import pytest
from unittest.mock import patch
from tradingagents.portfolio_advisor.candidates import _consensus_check

class TestConsensusCheck:
    def test_deepseek_aligned_fails(self):
        snap = {"deepseek_alignment": {"deepseek_last_recommended": ["AAPL"], "overlap_with_top_20": 0.80}}
        with patch("tradingagents.dataflows.llm_consensus.load_llm_consensus_snapshot", return_value=snap):
            result, tags = _consensus_check("AAPL")
            assert result == "fail_aligned"
    def test_deepseek_divergent_passes(self):
        snap = {"deepseek_alignment": {"deepseek_last_recommended": ["AAPL"], "overlap_with_top_20": 0.20}}
        with patch("tradingagents.dataflows.llm_consensus.load_llm_consensus_snapshot", return_value=snap):
            result, _ = _consensus_check("AAPL")
            assert result == "pass_divergent"
    def test_not_in_deepseek_passes(self):
        snap = {"deepseek_alignment": {"deepseek_last_recommended": [], "overlap_with_top_20": 0.80}}
        with patch("tradingagents.dataflows.llm_consensus.load_llm_consensus_snapshot", return_value=snap):
            result, _ = _consensus_check("MSFT")
            assert result == "pass"
    def test_snapshot_missing_returns_unknown(self):
        with patch("tradingagents.dataflows.llm_consensus.load_llm_consensus_snapshot", return_value=None):
            result, _ = _consensus_check("AAPL")
            assert result == "unknown"
