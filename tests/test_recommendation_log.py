"""Tests for recommendation_log.py — extended schema (plan section 6).

Covers:
- Backward compatible append (existing callers in messaging.py)
- New attribution fields (confidence, thesis_break_metrics, exit_horizon_days, peer_holdings)
- Consensus factor tagging fields (consensus_rank, consensus_age_days, consensus_score, deepseek_aligned_with_consensus)
- Field validation and normalisation
- load_due_for_measurement() horizon logic
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from tradingagents.portfolio_advisor import recommendation_log


@pytest.fixture
def tmp_advisor_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect the recommendation log to a temp directory."""
    def fake_advisor_dir(_cfg: Dict[str, Any]) -> Path:
        return tmp_path

    monkeypatch.setattr(
        "tradingagents.portfolio_advisor.state.advisor_dir",
        fake_advisor_dir,
    )
    return tmp_path


def _read_log(advisor_dir: Path):
    p = advisor_dir / "recommendation_log.jsonl"
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_backward_compatible_minimal_call(tmp_advisor_dir: Path):
    """Existing callers (messaging.py) only pass the original fields."""
    cfg: Dict[str, Any] = {}
    rec_id = recommendation_log.log_recommendation(
        cfg,
        trigger="action_check",
        type="macro_alert",
        action="reduce_entries_50pct",
        rationale="Macro risk 6/10",
        ticker="AAPL",
    )
    assert rec_id is not None
    rows = _read_log(tmp_advisor_dir)
    assert len(rows) == 1
    entry = rows[0]
    assert entry["ticker"] == "AAPL"
    assert entry["trigger"] == "action_check"
    # New optional fields default to None
    assert entry["confidence"] is None
    assert entry["thesis_break_metrics"] is None
    assert entry["exit_horizon_days"] is None
    assert entry["peer_holdings"] is None
    assert entry["consensus_rank"] is None
    assert entry["consensus_score"] is None


def test_full_field_extension(tmp_advisor_dir: Path):
    """New callers can populate all extension fields."""
    cfg: Dict[str, Any] = {}
    rec_id = recommendation_log.log_recommendation(
        cfg,
        trigger="full_graph",
        type="ep_entry",
        action="buy_100_shares",
        rationale="Earnings catalyst with strong fundamentals",
        ticker="NVDA",
        entry_price=950.25,
        confidence=0.72,
        thesis_break_metrics=[
            "data center revenue growth decelerates below 30% YoY",
            "gross margin falls below 70%",
        ],
        exit_horizon_days=30,
        peer_holdings={"MSFT": 0.12, "AVGO": 0.08},
        consensus_rank=1,
        consensus_age_days=187,
        consensus_score={"composite": -0.35, "entry": -0.6, "divergence": -0.4, "flow": -0.05},
        deepseek_aligned_with_consensus=True,
    )
    assert rec_id is not None
    rows = _read_log(tmp_advisor_dir)
    assert len(rows) == 1
    e = rows[0]
    assert e["confidence"] == 0.72
    assert len(e["thesis_break_metrics"]) == 2
    assert e["exit_horizon_days"] == 30
    assert e["peer_holdings"] == {"MSFT": 0.12, "AVGO": 0.08}
    assert e["consensus_rank"] == 1
    assert e["consensus_age_days"] == 187
    assert e["consensus_score"]["composite"] == -0.35
    assert e["deepseek_aligned_with_consensus"] is True


def test_confidence_normalisation(tmp_advisor_dir: Path):
    """Confidence is clipped to [0.0, 1.0] and out of range becomes None."""
    cfg: Dict[str, Any] = {}
    recommendation_log.log_recommendation(
        cfg, trigger="t", type="sizing", action="a", confidence=1.5
    )
    recommendation_log.log_recommendation(
        cfg, trigger="t", type="sizing", action="a", confidence=-0.1
    )
    recommendation_log.log_recommendation(
        cfg, trigger="t", type="sizing", action="a", confidence="not a number"
    )
    rows = _read_log(tmp_advisor_dir)
    assert all(r["confidence"] is None for r in rows)


def test_thesis_break_metrics_normalisation(tmp_advisor_dir: Path):
    """Empty strings filtered, max 5 metrics, trimmed to 200 chars each."""
    cfg: Dict[str, Any] = {}
    long_str = "x" * 500
    recommendation_log.log_recommendation(
        cfg,
        trigger="t",
        type="sizing",
        action="a",
        thesis_break_metrics=["valid", "", "  ", long_str, "ok1", "ok2", "ok3", "ok4"],
    )
    rows = _read_log(tmp_advisor_dir)
    metrics = rows[0]["thesis_break_metrics"]
    assert len(metrics) == 5
    assert all(len(m) <= 200 for m in metrics)
    assert "" not in metrics
    assert "  " not in metrics


def test_exit_horizon_invalid_returns_none(tmp_advisor_dir: Path):
    """Negative or non int horizon becomes None."""
    cfg: Dict[str, Any] = {}
    recommendation_log.log_recommendation(
        cfg, trigger="t", type="sizing", action="a", exit_horizon_days=-5
    )
    recommendation_log.log_recommendation(
        cfg, trigger="t", type="sizing", action="a", exit_horizon_days="not int"
    )
    rows = _read_log(tmp_advisor_dir)
    assert all(r["exit_horizon_days"] is None for r in rows)


def test_load_due_for_measurement_horizon_logic(tmp_advisor_dir: Path):
    """Recommendations past their exit horizon are returned, others not."""
    cfg: Dict[str, Any] = {}

    # Write entries directly to bypass current timestamps
    p = tmp_advisor_dir / "recommendation_log.jsonl"
    now = datetime.now(timezone.utc)
    entries = [
        {
            "id": "due_explicit_horizon",
            "ts": (now - timedelta(days=40)).isoformat(),
            "exit_horizon_days": 30,
            "trigger": "t", "type": "sizing", "action": "a",
            "ticker": "A", "was_correct": None,
        },
        {
            "id": "not_due_explicit_horizon",
            "ts": (now - timedelta(days=10)).isoformat(),
            "exit_horizon_days": 30,
            "trigger": "t", "type": "sizing", "action": "a",
            "ticker": "B", "was_correct": None,
        },
        {
            "id": "due_default_horizon",
            "ts": (now - timedelta(days=35)).isoformat(),
            "exit_horizon_days": None,
            "trigger": "t", "type": "sizing", "action": "a",
            "ticker": "C", "was_correct": None,
        },
        {
            "id": "already_measured",
            "ts": (now - timedelta(days=60)).isoformat(),
            "exit_horizon_days": 30,
            "trigger": "t", "type": "sizing", "action": "a",
            "ticker": "D", "was_correct": True,
        },
    ]
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    due = recommendation_log.load_due_for_measurement(cfg, default_horizon_days=30)
    due_ids = {r["id"] for r in due}
    assert "due_explicit_horizon" in due_ids
    assert "due_default_horizon" in due_ids
    assert "not_due_explicit_horizon" not in due_ids
    assert "already_measured" not in due_ids


def test_old_entries_without_new_fields_still_parse(tmp_advisor_dir: Path):
    """Backward compat: old entries missing the new fields don't crash loaders."""
    p = tmp_advisor_dir / "recommendation_log.jsonl"
    old_entry = {
        "id": "old1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "trigger": "action_check",
        "type": "macro_alert",
        "ticker": "AAPL",
        "action": "hold",
        "rationale": "",
        "rule_ref": None,
        "entry_price": None,
        "stop_price": None,
        "shares": None,
        "status": "pending",
        "human_response": None,
        "outcome_measured_at": None,
        "was_correct": None,
        "pnl_impact_est": None,
        "outcome_note": None,
    }
    p.write_text(json.dumps(old_entry) + "\n")
    cfg: Dict[str, Any] = {}
    pending = recommendation_log.load_pending(cfg)
    assert len(pending) == 1
    assert pending[0]["id"] == "old1"
    # Should be returned by load_due_for_measurement with default horizon
    # (but only if ts is old enough; this one is fresh so not due)
    due = recommendation_log.load_due_for_measurement(cfg, default_horizon_days=30)
    assert not any(r["id"] == "old1" for r in due)


def test_outcome_update_preserves_new_fields(tmp_advisor_dir: Path):
    """Updating an outcome must not drop the extension fields."""
    cfg: Dict[str, Any] = {}
    rec_id = recommendation_log.log_recommendation(
        cfg,
        trigger="action_check",
        type="ep_entry",
        action="buy",
        ticker="NVDA",
        confidence=0.8,
        exit_horizon_days=30,
        thesis_break_metrics=["m1"],
    )
    assert rec_id is not None
    ok = recommendation_log.update_outcome(
        cfg, rec_id, was_correct=True, pnl_impact_est=152.50, note="Played out as expected"
    )
    assert ok
    rows = _read_log(tmp_advisor_dir)
    assert len(rows) == 1
    e = rows[0]
    assert e["was_correct"] is True
    assert e["pnl_impact_est"] == 152.50
    # New fields intact
    assert e["confidence"] == 0.8
    assert e["exit_horizon_days"] == 30
    assert e["thesis_break_metrics"] == ["m1"]
