"""Tests for portfolio_risk.py consensus crowding flag."""
import pytest
from unittest.mock import patch

class TestConsensusCrowding:
    def make_positions(self, tickers_and_vals):
        return [{"ticker": t, "symbolFull": t, "invested_usd": v} for t, v in tickers_and_vals]

    def test_no_consensus_no_flag(self):
        from tradingagents.portfolio_advisor.portfolio_risk import compute_concentration_flags
        pos = self.make_positions([("AAPL", 1000), ("MSFT", 500)])
        with patch("tradingagents.dataflows.llm_consensus.load_llm_consensus_snapshot", return_value=None):
            flags = compute_concentration_flags(pos, {}, {})
            assert not any("Consensus crowding" in f for f in flags)

    def test_above_40_warning(self):
        from tradingagents.portfolio_advisor.portfolio_risk import compute_concentration_flags
        pos = self.make_positions([("AAPL", 5000), ("TSLA", 5000)])
        snap = {"top_20": [{"ticker": "AAPL"}]}
        with patch("tradingagents.dataflows.llm_consensus.load_llm_consensus_snapshot", return_value=snap):
            flags = compute_concentration_flags(pos, {}, {})
            consensus_flags = [f for f in flags if "Consensus crowding" in f]
            assert len(consensus_flags) == 1

    def test_above_60_severe(self):
        from tradingagents.portfolio_advisor.portfolio_risk import compute_concentration_flags
        pos = self.make_positions([("AAPL", 7000), ("TSLA", 3000)])
        snap = {"top_20": [{"ticker": "AAPL"}, {"ticker": "TSLA"}]}
        with patch("tradingagents.dataflows.llm_consensus.load_llm_consensus_snapshot", return_value=snap):
            flags = compute_concentration_flags(pos, {}, {})
            assert any("SEVERE" in f for f in flags)
