"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        monkeypatch.setenv(env_var, os.environ.get(env_var, "placeholder"))


@pytest.fixture(autouse=True)
def _isolate_live_event_log(tmp_path, monkeypatch):
    """No test may write to the real ~/.tradingagents event log.

    append_event() falls back to ~/.tradingagents/memory/events.jsonl when a
    config carries no explicit path. A graph-path test (propagate) leaked
    phantom NVDA 2026-01-10 "Buy" decisions into production this way, faking a
    persistent PM conflict. Redirect ONLY that real-home fallback to a temp
    file; explicit event_log_path / memory_log_path configs are untouched.
    """
    from pathlib import Path

    import tradingagents.agents.utils.event_log as el

    real = Path.home() / ".tradingagents" / "memory" / "events.jsonl"
    safe = tmp_path / "events.jsonl"
    _orig = el._default_event_path

    def _guarded(cfg):
        resolved = _orig(cfg)
        return safe if Path(resolved) == real else resolved

    monkeypatch.setattr(el, "_default_event_path", _guarded)


@pytest.fixture(autouse=True)
def _isolate_live_message_log(tmp_path, monkeypatch):
    """No test may write to the real ~/.tradingagents message log.

    messaging.message_log_path() falls back to ~/.tradingagents/memory/messages.jsonl
    when cfg has no explicit path.  The PYTEST guard inside the function itself is
    the primary defence; this fixture is belt-and-braces: it patches the function so
    that the real-home fallback is replaced with a per-test tmp file even if the
    in-function guard were ever removed or circumvented.

    Tests that explicitly pass ``message_log_path`` in cfg still use their own path
    (the function returns before reaching the fallback, so the monkeypatch never fires).
    """
    from pathlib import Path

    import tradingagents.portfolio_advisor.messaging as msg

    real = Path.home() / ".tradingagents" / "memory" / "messages.jsonl"
    safe = tmp_path / "messages.jsonl"
    _orig = msg.message_log_path

    def _guarded(cfg):
        resolved = _orig(cfg)
        return safe if Path(resolved) == real else resolved

    monkeypatch.setattr(msg, "message_log_path", _guarded)


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
