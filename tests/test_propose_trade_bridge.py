"""Unit tests for the propose_trade → recommendation_log bridge."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the project root to the path so imports work from the test directory.
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


class TestProposeTradeBridge:
    """Verify that propose_trade calls log_recommendation with the expected args."""

    @pytest.fixture
    def cfg(self):
        return {"portfolio_advisor_dir": "/tmp/test_tradingagents"}

    @pytest.fixture
    def mock_proposals_add(self):
        with patch(
            "tradingagents.portfolio_advisor.proposals.add", return_value={}
        ) as mock:
            yield mock

    @pytest.fixture
    def mock_position_plans(self):
        with patch(
            "tradingagents.portfolio_advisor.position_plans.append_position_decision"
        ) as mock:
            yield mock

    @pytest.fixture
    def mock_log_recommendation(self):
        with patch(
            "tradingagents.portfolio_advisor.recommendation_log.log_recommendation",
            return_value="test-rec-id",
        ) as mock:
            yield mock

    @pytest.fixture
    def propose_trade_tool(self, cfg, mock_proposals_add, mock_position_plans):
        """Build the full tool list and extract propose_trade."""
        from tradingagents.portfolio_advisor.pm_tools import build_pm_tools

        tools = build_pm_tools(cfg, live_tickers=set())
        for t in tools:
            if t.name == "propose_trade":
                return t
        raise RuntimeError("propose_trade tool not found")

    def test_propose_trade_calls_log_recommendation(
        self,
        cfg,
        propose_trade_tool,
        mock_log_recommendation,
    ):
        """Happy path: propose_trade with all fields should bridge to rec log."""
        result = propose_trade_tool.invoke({
            "ticker": "NVDA",
            "action": "buy",
            "shares": 10.0,
            "approx_usd": 2000.0,
            "target_price": 200.0,
            "sleeve": "core",
            "reason": "Strong earnings beat, raising guidance",
        })

        assert "PROPOSAL recorded" in result

        mock_log_recommendation.assert_called_once()
        call_kwargs = mock_log_recommendation.call_args.kwargs

        assert call_kwargs["trigger"] == "action_check"
        assert call_kwargs["type"] == "trade_proposal"
        assert call_kwargs["ticker"] == "NVDA"
        assert call_kwargs["action"] == "buy"
        assert call_kwargs["shares"] == 10.0
        assert call_kwargs["entry_price"] == 200.0
        assert call_kwargs["rationale"] == "Strong earnings beat, raising guidance"
        assert call_kwargs["rule_ref"] == "core"

    def test_propose_trade_with_catalyst_sleeve(
        self,
        cfg,
        propose_trade_tool,
        mock_log_recommendation,
    ):
        """Catalyst sleeve should map to rule_ref='catalyst'."""
        result = propose_trade_tool.invoke({
            "ticker": "ANET",
            "action": "buy",
            "shares": 5.0,
            "approx_usd": 750.0,
            "target_price": 150.0,
            "sleeve": "catalyst",
            "reason": "Earnings gap play",
        })
        assert "PROPOSAL recorded" in result

        call_kwargs = mock_log_recommendation.call_args.kwargs
        assert call_kwargs["rule_ref"] == "catalyst"

    def test_propose_trade_unknown_sleeve_maps_to_none(
        self,
        cfg,
        propose_trade_tool,
        mock_log_recommendation,
    ):
        """An unrecognised sleeve should set rule_ref to None."""
        result = propose_trade_tool.invoke({
            "ticker": "TEAM",
            "action": "sell",
            "shares": 10.0,
            "approx_usd": 0.0,
            "target_price": 0.0,
            "sleeve": "growth",
            "reason": "Position review",
        })
        assert "PROPOSAL recorded" in result

        call_kwargs = mock_log_recommendation.call_args.kwargs
        assert call_kwargs["rule_ref"] is None

    def test_propose_trade_handles_log_failure_gracefully(
        self,
        cfg,
        propose_trade_tool,
        mock_proposals_add,
    ):
        """If log_recommendation fails, propose_trade must still succeed."""
        with patch(
            "tradingagents.portfolio_advisor.recommendation_log.log_recommendation",
            side_effect=OSError("disk full"),
        ):
            result = propose_trade_tool.invoke({
                "ticker": "NVDA",
                "action": "sell",
                "approx_usd": 3000.0,
                "reason": "Risk reduction",
            })
        assert "PROPOSAL recorded" in result
        mock_proposals_add.assert_called_once()

    def test_propose_trade_zero_shares_and_price(
        self,
        cfg,
        propose_trade_tool,
        mock_log_recommendation,
    ):
        """Zero shares and zero target_price should map to None, not 0.0."""
        result = propose_trade_tool.invoke({
            "ticker": "MNDY",
            "action": "trim",
            "approx_usd": 500.0,
            "target_price": 0.0,
            "shares": 0.0,
            "sleeve": "core",
            "reason": "Reduce exposure",
        })
        assert "PROPOSAL recorded" in result

        call_kwargs = mock_log_recommendation.call_args.kwargs
        assert call_kwargs["shares"] is None
        assert call_kwargs["entry_price"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
