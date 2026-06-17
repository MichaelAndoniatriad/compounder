"""Unit tests for corporate hierarchy routing and rate-limit fallback helpers."""

from __future__ import annotations

import unittest

from tradingagents.llm_clients.fallback_chat_model import is_rate_limit_error
from tradingagents.llm_clients.corporate_llm_factory import _effective_routing_table
from tradingagents.llm_clients.model_catalog import DEFAULT_CORPORATE_AGENT_ROUTING


class TestRateLimitDetection(unittest.TestCase):
    def test_429_in_message(self):
        self.assertTrue(is_rate_limit_error(RuntimeError("Error 429: Too Many Requests")))

    def test_rate_limit_word(self):
        self.assertTrue(is_rate_limit_error(ValueError("You exceeded your rate limit.")))

    def test_non_rate_limit(self):
        self.assertFalse(is_rate_limit_error(ValueError("invalid json")))


class TestRoutingMerge(unittest.TestCase):
    def test_default_has_all_graph_logical_keys(self):
        keys = set(DEFAULT_CORPORATE_AGENT_ROUTING)
        for required in (
            "market_analyst",
            "sentiment_analyst",
            "news_analyst",
            "fundamentals_analyst",
            "bull_researcher",
            "bear_researcher",
            "trader",
            "risk_aggressive",
            "risk_neutral",
            "risk_conservative",
            "research_manager",
            "portfolio_manager",
            "reflection",
        ):
            with self.subTest(key=required):
                self.assertIn(required, keys)
        for spec in DEFAULT_CORPORATE_AGENT_ROUTING.values():
            self.assertEqual(spec.get("provider"), "openrouter")

    def test_override_merges_model_only(self):
        cfg = {
            "agent_llm_routing": {
                "news_analyst": {"model": "google/gemini-2.5-flash"},
            }
        }
        table = _effective_routing_table(cfg)
        self.assertEqual(table["news_analyst"]["model"], "google/gemini-2.5-flash")
        self.assertEqual(table["news_analyst"]["provider"], "openrouter")
        # A non-overridden key keeps its built-in default model. Assert against the
        # default table itself so this survives model-routing migrations.
        self.assertEqual(
            table["research_manager"]["model"],
            DEFAULT_CORPORATE_AGENT_ROUTING["research_manager"]["model"],
        )


if __name__ == "__main__":
    unittest.main()
