"""Tests for notification-noise reduction policy.

Covers:
1. proposals.add in alpaca mode suppresses action ticket
2. proposals.add in etoro mode sends action ticket
3. proposals.add in alpaca mode with cfg override sends action ticket
4. _pm_note_throttled: first note (no prior) not throttled
5. _pm_note_throttled: note 5 min after last → throttled
6. _pm_note_throttled: note 90 min after last (gap=60) → not throttled
7. Note containing "?" never throttled even 5 min after last
8. Recent submitted ledger row (ts now) → not throttled even 5 min after last
9. PM prompt text contains "DEFAULT TO SILENCE"
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from tradingagents.portfolio_advisor import proposals as pr
from tradingagents.portfolio_advisor.advisor_pm import _pm_note_throttled
from tradingagents.portfolio_advisor import state as pa_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **overrides) -> Dict[str, Any]:
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_action_tickets": True,
    }
    cfg.update(overrides)
    return cfg


def _pa_dir(cfg: Dict[str, Any]) -> Path:
    p = Path(cfg["portfolio_advisor_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_alpaca_ledger_row(pa_dir: Path, status: str = "submitted", minutes_ago: float = 0.0) -> None:
    """Write one row to alpaca_trades.jsonl."""
    pa_dir.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    row = {"ts": ts, "ticker": "NU", "status": status, "order_id": "test-order"}
    ledger = pa_dir / "alpaca_trades.jsonl"
    with ledger.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Task 1 — action tickets in alpaca vs etoro mode
# ---------------------------------------------------------------------------


def test_action_ticket_suppressed_in_alpaca_mode(tmp_path, monkeypatch):
    """In alpaca mode, action tickets must NOT be sent (autonomous mode already announces trades)."""
    cfg = _cfg(tmp_path)
    _pa_dir(cfg)

    # Monkeypatch the mode-check helper to simulate alpaca
    monkeypatch.setattr(pr, "_should_send_action_ticket_for_mode", lambda c: False)

    store: list = []
    monkeypatch.setattr(pr, "load_all", lambda c: list(store))
    monkeypatch.setattr(pr, "save_all", lambda c, rows: (store.clear(), store.extend(rows)))
    sent: list = []
    monkeypatch.setattr(pr, "_send_action_ticket", lambda c, e: sent.append(e))

    # Disable alpaca executor
    alpaca_mock = MagicMock()
    alpaca_mock.execute_proposal.return_value = {"status": "disabled", "detail": ""}
    import tradingagents.integrations.alpaca.executor as _alpaca_mod
    monkeypatch.setattr(_alpaca_mod, "execute_proposal", alpaca_mock.execute_proposal)

    pr.add(cfg, ticker="NU", action="buy", approx_usd=300, reason="test")
    assert sent == [], "Action ticket must not be sent in alpaca mode"


def test_action_ticket_sent_in_etoro_mode(tmp_path, monkeypatch):
    """In etoro mode, action tickets MUST be sent (human executes them)."""
    cfg = _cfg(tmp_path)
    _pa_dir(cfg)

    # Monkeypatch the mode-check helper to simulate etoro
    monkeypatch.setattr(pr, "_should_send_action_ticket_for_mode", lambda c: True)

    store: list = []
    monkeypatch.setattr(pr, "load_all", lambda c: list(store))
    monkeypatch.setattr(pr, "save_all", lambda c, rows: (store.clear(), store.extend(rows)))
    sent: list = []
    monkeypatch.setattr(pr, "_send_action_ticket", lambda c, e: sent.append(e))

    # Disable alpaca executor
    alpaca_mock = MagicMock()
    alpaca_mock.execute_proposal.return_value = {"status": "disabled", "detail": ""}
    import tradingagents.integrations.alpaca.executor as _alpaca_mod
    monkeypatch.setattr(_alpaca_mod, "execute_proposal", alpaca_mock.execute_proposal)

    pr.add(cfg, ticker="NU", action="buy", approx_usd=300, reason="test")
    assert len(sent) == 1, "Action ticket must be sent in etoro mode"


def test_action_ticket_sent_in_alpaca_mode_with_override(tmp_path, monkeypatch):
    """In alpaca mode with portfolio_advisor_action_tickets_autonomous=True, ticket IS sent."""
    cfg = _cfg(tmp_path, portfolio_advisor_action_tickets_autonomous=True)
    _pa_dir(cfg)

    # _should_send_action_ticket_for_mode with alpaca + override=True should return True
    # We test the helper directly (bypassing PYTEST_CURRENT_TEST guard on account_mode)
    from tradingagents.portfolio_advisor import etoro_scan
    monkeypatch.setattr(etoro_scan, "account_mode", lambda: "alpaca")

    result = pr._should_send_action_ticket_for_mode(cfg)
    assert result is True, "With autonomous override=True, ticket should be sent"


def test_should_send_action_ticket_alpaca_no_override(monkeypatch):
    """_should_send_action_ticket_for_mode returns False in alpaca without override."""
    from tradingagents.portfolio_advisor import etoro_scan
    monkeypatch.setattr(etoro_scan, "account_mode", lambda: "alpaca")

    result = pr._should_send_action_ticket_for_mode({})
    assert result is False


def test_should_send_action_ticket_etoro(monkeypatch):
    """_should_send_action_ticket_for_mode returns True in etoro regardless of override."""
    from tradingagents.portfolio_advisor import etoro_scan
    monkeypatch.setattr(etoro_scan, "account_mode", lambda: "etoro")

    assert pr._should_send_action_ticket_for_mode({}) is True
    assert pr._should_send_action_ticket_for_mode({"portfolio_advisor_action_tickets_autonomous": False}) is True


# ---------------------------------------------------------------------------
# Task 2 — _pm_note_throttled
# ---------------------------------------------------------------------------


def test_pm_note_throttled_no_prior_note_not_throttled(tmp_path):
    """First note ever (no last_pm_note_iso) must NOT be throttled."""
    cfg = _cfg(tmp_path)
    _pa_dir(cfg)
    # No state file → no prior note
    result = _pm_note_throttled(cfg, "Market looks stable today.")
    assert result is False, "First note should never be throttled"


def test_pm_note_throttled_within_gap_suppressed(tmp_path):
    """Note sent 5 min after last, gap=60 → throttled."""
    cfg = _cfg(tmp_path, portfolio_advisor_pm_note_min_gap_minutes=60)
    _pa_dir(cfg)

    # Record a prior note 5 minutes ago
    st = pa_state.load_state(cfg)
    st["last_pm_note_iso"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    pa_state.save_state(cfg, st)

    result = _pm_note_throttled(cfg, "Nothing new here.")
    assert result is True, "Should be throttled: 5 min < 60 min gap"


def test_pm_note_throttled_outside_gap_not_suppressed(tmp_path):
    """Note sent 90 min after last, gap=60 → NOT throttled."""
    cfg = _cfg(tmp_path, portfolio_advisor_pm_note_min_gap_minutes=60)
    _pa_dir(cfg)

    st = pa_state.load_state(cfg)
    st["last_pm_note_iso"] = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    pa_state.save_state(cfg, st)

    result = _pm_note_throttled(cfg, "Nothing new here.")
    assert result is False, "Should NOT be throttled: 90 min > 60 min gap"


def test_pm_note_question_never_throttled(tmp_path):
    """Note containing '?' must never be throttled, even within gap."""
    cfg = _cfg(tmp_path, portfolio_advisor_pm_note_min_gap_minutes=60)
    _pa_dir(cfg)

    # Prior note just 1 minute ago
    st = pa_state.load_state(cfg)
    st["last_pm_note_iso"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    pa_state.save_state(cfg, st)

    result = _pm_note_throttled(cfg, "Should I buy more NU here?")
    assert result is False, "Notes with '?' must never be throttled"


def test_pm_note_recent_trade_not_throttled(tmp_path):
    """Recent submitted trade in ledger → NOT throttled even within gap."""
    cfg = _cfg(tmp_path, portfolio_advisor_pm_note_min_gap_minutes=60)
    pa_dir = _pa_dir(cfg)

    # Prior note just 5 minutes ago
    st = pa_state.load_state(cfg)
    st["last_pm_note_iso"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    pa_state.save_state(cfg, st)

    # Recent submitted trade (ts = now)
    _write_alpaca_ledger_row(pa_dir, status="submitted", minutes_ago=0)

    result = _pm_note_throttled(cfg, "NU order filled at $14.20.")
    assert result is False, "Recent trade should unblock the note"


def test_pm_note_old_trade_still_throttled(tmp_path):
    """Old trade (>15 min ago) does NOT unblock a throttled note."""
    cfg = _cfg(tmp_path, portfolio_advisor_pm_note_min_gap_minutes=60)
    pa_dir = _pa_dir(cfg)

    # Prior note 5 min ago → within gap
    st = pa_state.load_state(cfg)
    st["last_pm_note_iso"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    pa_state.save_state(cfg, st)

    # Old trade 30 minutes ago
    _write_alpaca_ledger_row(pa_dir, status="submitted", minutes_ago=30)

    result = _pm_note_throttled(cfg, "Nothing new here.")
    assert result is True, "Old trade should not unblock a within-gap note"


def test_pm_note_non_submitted_ledger_row_ignored(tmp_path):
    """A recent ledger row with status != 'submitted' does not unblock."""
    cfg = _cfg(tmp_path, portfolio_advisor_pm_note_min_gap_minutes=60)
    pa_dir = _pa_dir(cfg)

    # Prior note 5 min ago → within gap
    st = pa_state.load_state(cfg)
    st["last_pm_note_iso"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    pa_state.save_state(cfg, st)

    # Recent but non-submitted row
    _write_alpaca_ledger_row(pa_dir, status="skipped", minutes_ago=1)

    result = _pm_note_throttled(cfg, "Nothing new here.")
    assert result is True, "Non-submitted rows should not unblock the note"


# ---------------------------------------------------------------------------
# Task 3 — prompt text contains DEFAULT TO SILENCE
# ---------------------------------------------------------------------------


def test_pm_prompt_contains_default_to_silence():
    """The PM standing instructions must contain 'DEFAULT TO SILENCE'."""
    import tradingagents.portfolio_advisor.advisor_pm as apm
    prompt_text = apm._PM_CLAUDE_DEFAULT
    assert "DEFAULT TO SILENCE" in prompt_text, (
        "PM prompt must contain 'DEFAULT TO SILENCE' — update the _PM_CLAUDE_DEFAULT constant"
    )


def test_pm_prompt_no_restate_execution_notice():
    """Prompt must instruct PM not to restate trades the execution notice already announced."""
    import tradingagents.portfolio_advisor.advisor_pm as apm
    prompt_text = apm._PM_CLAUDE_DEFAULT
    assert "execution notice" in prompt_text.lower() or "already announced" in prompt_text.lower(), (
        "PM prompt must tell the PM not to restate trades already announced by execution notice"
    )


def test_pm_prompt_details_on_demand_contract():
    """Prompt must state the 'human can ask anytime on Telegram' contract."""
    import tradingagents.portfolio_advisor.advisor_pm as apm
    prompt_text = apm._PM_CLAUDE_DEFAULT
    assert "telegram" in prompt_text.lower() and "ask" in prompt_text.lower(), (
        "PM prompt must include the 'ask on Telegram' contract"
    )
