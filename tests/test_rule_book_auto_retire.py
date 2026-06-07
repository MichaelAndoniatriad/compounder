"""Tests for rule_book.py auto retirement (plan section 4)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

import pytest

from tradingagents.portfolio_advisor import rule_book


@pytest.fixture
def tmp_advisor_dir(monkeypatch, tmp_path: Path) -> Path:
    def fake_advisor_dir(_cfg: Dict[str, Any]) -> Path:
        return tmp_path
    monkeypatch.setattr(
        "tradingagents.portfolio_advisor.state.advisor_dir",
        fake_advisor_dir,
    )
    return tmp_path


def _seed_rule(cfg, name: str, confirmed: int, violated: int, confidence: str = "emerging"):
    """Add a rule, then surgically set its counters by re running update_rule."""
    rule_book.add_rule(
        cfg, name=name, pattern="p", rule_text="r", confidence=confidence
    )
    for _ in range(confirmed):
        rule_book.update_rule(cfg, name=name, action="confirm")
    for _ in range(violated):
        rule_book.update_rule(cfg, name=name, action="violate")


def test_auto_retire_skips_rules_below_threshold(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    _seed_rule(cfg, "low_violations", confirmed=2, violated=2)
    retired = rule_book.auto_retire_failed_rules(cfg, min_violations=3)
    assert retired == []
    rules = rule_book._parse_rules(rule_book.load_rule_book_text(cfg))
    assert rules["low_violations"]["confidence"] != "retired"


def test_auto_retire_retires_violated_dominated(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    _seed_rule(cfg, "bad_rule", confirmed=1, violated=4)
    retired = rule_book.auto_retire_failed_rules(
        cfg, min_violations=3, violation_to_confirmation_ratio=2.0
    )
    assert retired == ["bad_rule"]
    rules = rule_book._parse_rules(rule_book.load_rule_book_text(cfg))
    assert rules["bad_rule"]["confidence"] == "retired"


def test_auto_retire_does_not_touch_mixed_rules(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    _seed_rule(cfg, "mixed_rule", confirmed=3, violated=3)
    retired = rule_book.auto_retire_failed_rules(
        cfg, min_violations=3, violation_to_confirmation_ratio=2.0
    )
    assert retired == []
    rules = rule_book._parse_rules(rule_book.load_rule_book_text(cfg))
    assert rules["mixed_rule"]["confidence"] != "retired"


def test_auto_retire_skips_already_retired(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    _seed_rule(cfg, "already_dead", confirmed=0, violated=5)
    # Retire once
    first = rule_book.auto_retire_failed_rules(cfg, min_violations=3)
    assert first == ["already_dead"]
    # Second pass should be empty (already retired)
    second = rule_book.auto_retire_failed_rules(cfg, min_violations=3)
    assert second == []


def test_recently_retired_block_empty_when_none(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    _seed_rule(cfg, "active_rule", confirmed=5, violated=0)
    assert rule_book.recently_retired_block(cfg) == ""


def test_recently_retired_block_lists_recent(tmp_advisor_dir: Path):
    cfg: Dict[str, Any] = {}
    _seed_rule(cfg, "fresh_kill", confirmed=0, violated=4)
    rule_book.auto_retire_failed_rules(cfg, min_violations=3)
    block = rule_book.recently_retired_block(cfg, lookback_days=14)
    assert "Recently retired" in block
    assert "fresh_kill" in block


def test_auto_retire_idempotent(tmp_advisor_dir: Path):
    """Running twice gives the same end state, no thrash."""
    cfg: Dict[str, Any] = {}
    _seed_rule(cfg, "rule1", confirmed=0, violated=3)
    _seed_rule(cfg, "rule2", confirmed=2, violated=2)
    rule_book.auto_retire_failed_rules(cfg)
    state_after_first = rule_book.load_rule_book_text(cfg)
    rule_book.auto_retire_failed_rules(cfg)
    state_after_second = rule_book.load_rule_book_text(cfg)
    # The only acceptable diff is the timestamp on the retire note — not the
    # confidence assignments. Confidences should be stable.
    rules1 = rule_book._parse_rules(state_after_first)
    rules2 = rule_book._parse_rules(state_after_second)
    assert rules1["rule1"]["confidence"] == rules2["rule1"]["confidence"] == "retired"
    assert rules1["rule2"]["confidence"] == rules2["rule2"]["confidence"]
    assert rules2["rule2"]["confidence"] != "retired"
