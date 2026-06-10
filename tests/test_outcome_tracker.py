"""Tests for outcome_tracker.py — plan section 7.

Covers:
- classify_outcome thresholds
- compute_recommendation_outcomes: due detection, classification, log update
- rule_performance_summary: aggregation, top/bottom ranking
- format_rule_performance_for_pm_prompt: empty handling, content shape
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from tradingagents.portfolio_advisor import outcome_tracker, recommendation_log


@pytest.fixture
def tmp_advisor_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect both recommendation_log and outcome_tracker to a temp dir."""
    def fake_advisor_dir(_cfg: Dict[str, Any]) -> Path:
        return tmp_path

    monkeypatch.setattr(
        "tradingagents.portfolio_advisor.state.advisor_dir",
        fake_advisor_dir,
    )
    return tmp_path


def _seed_recommendations(advisor_dir: Path, entries: List[Dict[str, Any]]) -> None:
    p = advisor_dir / "recommendation_log.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _fake_price_fetcher(returns_by_ticker: Dict[str, float]):
    """Build a price fetcher that returns (1.0, 1.0 + returns_by_ticker[t]).

    Tickers not in the map return None (simulating failed fetch).
    """
    def fetch(ticker: str, start_date: str, end_date: str) -> Optional[Tuple[float, float]]:
        if ticker not in returns_by_ticker:
            return None
        return 1.0, 1.0 + returns_by_ticker[ticker]
    return fetch


def test_classify_outcome_thresholds():
    # Compounder 2.0 §5.2: input is ALPHA vs QQQ (not absolute return); ±2% thresholds.
    assert outcome_tracker.classify_outcome(0.10) == "good"
    assert outcome_tracker.classify_outcome(0.02) == "good"
    assert outcome_tracker.classify_outcome(0.019) == "neutral"
    assert outcome_tracker.classify_outcome(0.0) == "neutral"
    assert outcome_tracker.classify_outcome(-0.019) == "neutral"
    assert outcome_tracker.classify_outcome(-0.02) == "bad"
    assert outcome_tracker.classify_outcome(-0.30) == "bad"


def test_compute_outcomes_classifies_and_writes_outcomes_file(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    _seed_recommendations(
        tmp_advisor_dir,
        [
            {
                "id": "rec_good",
                "ts": (now - timedelta(days=35)).isoformat(),
                "exit_horizon_days": 30,
                "trigger": "ep_scan", "type": "ep_entry", "action": "buy",
                "ticker": "NVDA", "rule_ref": "rule_nvda_growth",
                "shares": 10,
                "was_correct": None,
            },
            {
                "id": "rec_bad",
                "ts": (now - timedelta(days=40)).isoformat(),
                "exit_horizon_days": 30,
                "trigger": "ep_scan", "type": "ep_entry", "action": "buy",
                "ticker": "BAD", "rule_ref": "rule_bad",
                "shares": 5,
                "was_correct": None,
            },
            {
                "id": "rec_neutral",
                "ts": (now - timedelta(days=33)).isoformat(),
                "exit_horizon_days": 30,
                "trigger": "ep_scan", "type": "ep_entry", "action": "buy",
                "ticker": "FLAT", "rule_ref": "rule_flat",
                "shares": 100,
                "was_correct": None,
            },
            {
                "id": "rec_not_due",
                "ts": (now - timedelta(days=10)).isoformat(),
                "exit_horizon_days": 30,
                "trigger": "ep_scan", "type": "ep_entry", "action": "buy",
                "ticker": "NVDA",
                "was_correct": None,
            },
        ],
    )

    fetcher = _fake_price_fetcher(
        {"NVDA": 0.15, "BAD": -0.20, "FLAT": 0.01}
    )

    summary = outcome_tracker.compute_recommendation_outcomes(
        cfg, price_fetcher=fetcher, default_horizon_days=30
    )

    assert summary["due"] == 3
    assert summary["measured"] == 3
    assert summary["good"] == 1
    assert summary["bad"] == 1
    assert summary["neutral"] == 1

    outcomes_path = tmp_advisor_dir / "outcomes.jsonl"
    rows = [json.loads(l) for l in outcomes_path.read_text().splitlines() if l.strip()]
    assert len(rows) == 3
    classes = {r["recommendation_id"]: r["classification"] for r in rows}
    assert classes["rec_good"] == "good"
    assert classes["rec_bad"] == "bad"
    assert classes["rec_neutral"] == "neutral"


def test_compute_outcomes_updates_recommendation_log(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    _seed_recommendations(
        tmp_advisor_dir,
        [
            {
                "id": "rec_a",
                "ts": (now - timedelta(days=35)).isoformat(),
                "exit_horizon_days": 30,
                "trigger": "ep_scan", "type": "ep_entry", "action": "buy",
                "ticker": "NVDA", "rule_ref": "r1", "shares": 10,
                "was_correct": None,
            }
        ],
    )

    fetcher = _fake_price_fetcher({"NVDA": 0.20})
    outcome_tracker.compute_recommendation_outcomes(
        cfg, price_fetcher=fetcher, default_horizon_days=30
    )

    measured = recommendation_log.load_measured(cfg)
    assert len(measured) == 1
    assert measured[0]["was_correct"] is True
    # 10 shares × $0.20 gain = $2.00 (start=1.0, end=1.20)
    assert measured[0]["pnl_impact_est"] == 2.0


def test_skipped_when_price_fetch_fails(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    _seed_recommendations(
        tmp_advisor_dir,
        [
            {
                "id": "rec_unknown",
                "ts": (now - timedelta(days=35)).isoformat(),
                "exit_horizon_days": 30,
                "trigger": "ep_scan", "type": "ep_entry", "action": "buy",
                "ticker": "UNKNOWN_TICKER",
                "was_correct": None,
            }
        ],
    )

    fetcher = _fake_price_fetcher({})  # empty, all fetches fail
    summary = outcome_tracker.compute_recommendation_outcomes(
        cfg, price_fetcher=fetcher
    )
    assert summary["measured"] == 0
    assert summary["skipped_no_price"] == 1

    # Recommendation log unchanged
    assert recommendation_log.load_measured(cfg) == []


def test_idempotent_rerun(tmp_advisor_dir: Path):
    """Re-running after measurement does not double count."""
    cfg: Dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    _seed_recommendations(
        tmp_advisor_dir,
        [
            {
                "id": "rec_idem",
                "ts": (now - timedelta(days=35)).isoformat(),
                "exit_horizon_days": 30,
                "trigger": "ep_scan", "type": "ep_entry", "action": "buy",
                "ticker": "NVDA", "rule_ref": "r1",
                "was_correct": None,
            }
        ],
    )

    fetcher = _fake_price_fetcher({"NVDA": 0.10})

    s1 = outcome_tracker.compute_recommendation_outcomes(cfg, price_fetcher=fetcher)
    s2 = outcome_tracker.compute_recommendation_outcomes(cfg, price_fetcher=fetcher)

    assert s1["measured"] == 1
    assert s2["measured"] == 0  # already measured, no longer "due"


def test_rule_performance_summary_ranking(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    outcomes_path = tmp_advisor_dir / "outcomes.jsonl"
    outcomes = [
        # rule_good: 3 good outcomes
        {"recommendation_id": "1", "rule_ref": "rule_good", "classification": "good", "realised_return": 0.10},
        {"recommendation_id": "2", "rule_ref": "rule_good", "classification": "good", "realised_return": 0.12},
        {"recommendation_id": "3", "rule_ref": "rule_good", "classification": "good", "realised_return": 0.08},
        # rule_bad: 2 bad
        {"recommendation_id": "4", "rule_ref": "rule_bad", "classification": "bad", "realised_return": -0.10},
        {"recommendation_id": "5", "rule_ref": "rule_bad", "classification": "bad", "realised_return": -0.07},
        # rule_mixed: 1 good 1 bad
        {"recommendation_id": "6", "rule_ref": "rule_mixed", "classification": "good", "realised_return": 0.06},
        {"recommendation_id": "7", "rule_ref": "rule_mixed", "classification": "bad", "realised_return": -0.05},
        # rule_none: outcome with no rule ref (ignored)
        {"recommendation_id": "8", "rule_ref": None, "classification": "good", "realised_return": 0.05},
    ]
    outcomes_path.write_text("\n".join(json.dumps(o) for o in outcomes) + "\n")

    summary = outcome_tracker.rule_performance_summary(cfg, top_n=5)
    assert summary["total_outcomes"] == 8
    assert summary["total_rules"] == 3  # rule_none excluded

    top = summary["top"]
    assert top[0]["rule_ref"] == "rule_good"
    assert top[0]["mean_score"] == 1.0
    assert top[0]["uses"] == 3
    assert top[-1]["rule_ref"] == "rule_bad"
    assert top[-1]["mean_score"] == -1.0


def test_format_pm_prompt_block_empty_when_no_outcomes(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    assert outcome_tracker.format_rule_performance_for_pm_prompt(cfg) == ""


def test_format_pm_prompt_block_renders_when_outcomes_exist(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    outcomes_path = tmp_advisor_dir / "outcomes.jsonl"
    outcomes_path.write_text(json.dumps({
        "recommendation_id": "1",
        "rule_ref": "rule_a",
        "classification": "good",
        "realised_return": 0.10,
    }) + "\n")

    block = outcome_tracker.format_rule_performance_for_pm_prompt(cfg, top_n=3)
    assert "[RULE PERFORMANCE — alpha-relative]" in block
    assert "rule_a" in block
