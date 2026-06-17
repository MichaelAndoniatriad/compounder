"""Tests for advisor outbound message hygiene."""

from __future__ import annotations

from tradingagents.portfolio_advisor import messaging


def test_send_advisor_message_suppresses_near_duplicate(tmp_path):
    cfg = {
        "message_log_path": str(tmp_path / "messages.jsonl"),
        "portfolio_advisor_message_dedupe_minutes": 180,
    }

    messaging.send_advisor_message(cfg, "PM", "TEAM must exit today. Close 5 lots.")
    sent = messaging.send_advisor_message(cfg, "PM", "TEAM must exit today. Close 5 lots.")

    rows = messaging.load_recent_messages(cfg, limit=5)
    assert sent is False
    assert rows[0]["suppressed_duplicate"] is True
    assert rows[1]["suppressed_duplicate"] is False


def test_send_advisor_message_never_suppresses_correction(tmp_path):
    cfg = {
        "message_log_path": str(tmp_path / "messages.jsonl"),
        "portfolio_advisor_message_dedupe_minutes": 180,
    }

    messaging.send_advisor_message(cfg, "PM", "TEAM must exit today. Close 5 lots.")
    messaging.send_advisor_message(cfg, "PM", "Correction: TEAM date was wrong.")

    rows = messaging.load_recent_messages(cfg, limit=5)
    assert rows[0]["suppressed_duplicate"] is False
    assert rows[0]["body"].startswith("Correction:")


def test_send_advisor_message_sends_telegram(tmp_path, monkeypatch):
    cfg = {
        "message_log_path": str(tmp_path / "messages.jsonl"),
        "analysis_telegram_bot_token": "123:test-token",
        "analysis_telegram_chat_id": "456",
        # Non-urgent sends are gated to quiet-hours windows; disable so this test
        # exercises delivery deterministically regardless of wall-clock time.
        "portfolio_advisor_quiet_hours_enabled": False,
    }
    calls = []

    class Resp:
        status_code = 200
        text = '{"ok": true}'

        @staticmethod
        def json():
            return {"ok": True}

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return Resp()

    monkeypatch.setattr(messaging.requests, "post", fake_post)

    sent = messaging.send_advisor_message(cfg, "Action required", "Close TEAM lot 123.")

    assert sent is True
    assert calls[0][0] == "https://api.telegram.org/bot123:test-token/sendMessage"
    assert calls[0][1]["chat_id"] == "456"
    assert "Action required" in calls[0][1]["text"]
    rows = messaging.load_recent_messages(cfg, limit=1)
    assert rows[0]["telegram_ok"] is True
    assert rows[0]["channels_attempted"] == ["telegram"]
