"""Tests for Telegram inbound PM bridge."""

from __future__ import annotations

from unittest.mock import patch

from tradingagents.portfolio_advisor import telegram_bot
from tradingagents.portfolio_advisor.models import AdvisorPMCycleResult, AdvisorPMTickerStance


def test_process_update_answers_allowed_chat(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "analysis_telegram_bot_token": "123:test",
        "analysis_telegram_chat_id": "42",
    }
    update = {
        "update_id": 10,
        "message": {"chat": {"id": 42}, "text": "what should I do?"},
    }
    # answer_text retries run_pm_cycle while the reply is empty/short (< 30 chars),
    # so the canned answer must be a realistic substantive reply to break after one call.
    result = AdvisorPMCycleResult(
        executive_summary="Do nothing broad right now — the book looks balanced after the latest check.",
        stances=[AdvisorPMTickerStance(ticker="NVDA", stance="hold", rationale="Latest evidence supports hold.")],
    )

    with patch("tradingagents.portfolio_advisor.telegram_bot.run_pm_cycle", return_value=result) as pm:
        with patch("tradingagents.portfolio_advisor.messaging.send_telegram_message", return_value=True) as send:
            reply = telegram_bot.process_update(cfg, update)

    assert reply is not None
    assert "Do nothing broad" in reply
    pm.assert_called_once()
    assert pm.call_args.kwargs["trigger"] == "ntfy_question"
    send.assert_called_once()


def test_process_update_ignores_other_chat(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "analysis_telegram_bot_token": "123:test",
        "analysis_telegram_chat_id": "42",
    }
    update = {
        "update_id": 10,
        "message": {"chat": {"id": 99}, "text": "what should I do?"},
    }

    with patch("tradingagents.portfolio_advisor.telegram_bot.run_pm_cycle") as pm:
        with patch("tradingagents.portfolio_advisor.messaging.send_telegram_message") as send:
            reply = telegram_bot.process_update(cfg, update)

    assert reply is None
    pm.assert_not_called()
    send.assert_not_called()


def test_poll_once_advances_offset(tmp_path):
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "analysis_telegram_bot_token": "123:test",
        "analysis_telegram_chat_id": "42",
    }
    updates = [
        {"update_id": 7, "message": {"chat": {"id": 42}, "text": "hello"}},
        {"update_id": 8, "message": {"chat": {"id": 42}, "text": "what should I do?"}},
    ]

    with patch("tradingagents.portfolio_advisor.telegram_bot.fetch_updates", return_value=updates):
        with patch("tradingagents.portfolio_advisor.messaging.send_telegram_message", return_value=True):
            with patch(
                "tradingagents.portfolio_advisor.telegram_bot.run_pm_cycle",
                return_value=AdvisorPMCycleResult(executive_summary="Answer."),
            ):
                n = telegram_bot.poll_once(cfg, timeout=1)

    assert n == 2
    st = telegram_bot._load_state(cfg)
    assert st["last_update_id"] == 8
