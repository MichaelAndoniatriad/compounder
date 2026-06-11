"""Regression tests: message log and event log must never fall back to real home paths.

Guards the fix that prevents tests from writing to
~/.tradingagents/memory/messages.jsonl and events.jsonl when cfg has no
explicit path override.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest


# ---------------------------------------------------------------------------
# message_log_path
# ---------------------------------------------------------------------------

def test_message_log_path_default_not_home_under_pytest():
    """With empty cfg, message_log_path() must NOT return a path under Path.home()."""
    from tradingagents.portfolio_advisor.messaging import message_log_path

    # We are running under pytest so PYTEST_CURRENT_TEST is set.
    assert "PYTEST_CURRENT_TEST" in os.environ, "This test must run under pytest"

    p = message_log_path({})
    assert Path.home() not in p.parents and p != Path.home() / ".tradingagents" / "memory" / "messages.jsonl", (
        f"message_log_path({{}}) resolved to the real home path {p} — isolation guard is broken"
    )


def test_message_log_path_explicit_cfg_respected(tmp_path):
    """Explicit cfg key must always win regardless of PYTEST_CURRENT_TEST."""
    from tradingagents.portfolio_advisor.messaging import message_log_path

    custom = str(tmp_path / "custom_messages.jsonl")
    cfg: Dict[str, Any] = {"message_log_path": custom}
    assert message_log_path(cfg) == Path(custom)


def test_message_log_path_memory_log_parent_respected(tmp_path):
    """memory_log_path sibling override must still work."""
    from tradingagents.portfolio_advisor.messaging import message_log_path

    mem = str(tmp_path / "mem" / "state.jsonl")
    cfg = {"memory_log_path": mem}
    expected = tmp_path / "mem" / "messages.jsonl"
    assert message_log_path(cfg) == expected


# ---------------------------------------------------------------------------
# event_log._default_event_path
# ---------------------------------------------------------------------------

def test_default_event_path_not_home_under_pytest():
    """With empty cfg, _default_event_path() must NOT return a path under Path.home()."""
    import tradingagents.agents.utils.event_log as el

    assert "PYTEST_CURRENT_TEST" in os.environ

    p = el._default_event_path({})
    assert p != Path.home() / ".tradingagents" / "memory" / "events.jsonl", (
        f"_default_event_path({{}}) resolved to the real home path {p} — isolation guard is broken"
    )


def test_default_event_path_explicit_cfg_respected(tmp_path):
    """Explicit cfg event_log_path must always win."""
    import tradingagents.agents.utils.event_log as el

    custom = str(tmp_path / "custom_events.jsonl")
    p = el._default_event_path({"event_log_path": custom})
    assert p == Path(custom)


# ---------------------------------------------------------------------------
# send_advisor_message with empty cfg must not touch the real file
# ---------------------------------------------------------------------------

def test_send_advisor_message_empty_cfg_does_not_write_real_file(monkeypatch):
    """Calling send_advisor_message with an empty cfg must leave the real
    messages.jsonl completely untouched (neither created nor appended to)."""
    from tradingagents.portfolio_advisor import messaging

    real_path = Path.home() / ".tradingagents" / "memory" / "messages.jsonl"

    # Record initial state of the real file (may or may not exist)
    existed_before = real_path.exists()
    mtime_before = real_path.stat().st_mtime if existed_before else None
    size_before  = real_path.stat().st_size  if existed_before else None

    # Call with empty cfg — no Telegram/webhook/SMTP tokens so nothing is sent,
    # but previously the record was still appended to the real file.
    messaging.send_advisor_message({}, "isolation-test", "this must not reach the real log")

    if existed_before:
        # File existed: must not have changed
        st = real_path.stat()
        assert st.st_mtime == mtime_before, (
            "Real messages.jsonl was modified by send_advisor_message with empty cfg"
        )
        assert st.st_size == size_before
    else:
        # File did not exist: must still not exist
        assert not real_path.exists(), (
            "Real messages.jsonl was created by send_advisor_message with empty cfg"
        )


# ---------------------------------------------------------------------------
# Temp-dir default path is inside tempdir (not home)
# ---------------------------------------------------------------------------

def test_message_log_default_path_is_under_tempdir():
    """Default message log path under pytest should be inside tempfile.gettempdir()."""
    from tradingagents.portfolio_advisor.messaging import message_log_path

    p = message_log_path({})
    td = Path(tempfile.gettempdir())
    assert td in p.parents, (
        f"Expected default test path under {td}, got {p}"
    )


def test_event_log_default_path_is_under_tempdir():
    """Default event log path under pytest should be inside tempfile.gettempdir()."""
    import tradingagents.agents.utils.event_log as el

    p = el._default_event_path({})
    td = Path(tempfile.gettempdir())
    assert td in p.parents, (
        f"Expected default test path under {td}, got {p}"
    )
