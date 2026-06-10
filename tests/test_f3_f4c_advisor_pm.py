"""F3 + F4C regression tests for advisor_pm.py.

F3: run_pm_cycle must not deadlock (raise RuntimeError) when the Alpaca book
    is 100% cash (empty positions list). The prompt-assembly path must
    complete with a graceful all-cash snapshot and empty live_tickers.

F4C: _apply_candidate_comparisons must force sleeve='core' and append
     "(forced core: no dated catalyst)" to the reason when the LLM returns
     target_sleeve='catalyst', because the CandidateComparison model carries
     no catalyst_date field — passing through a catalyst sleeve here would
     bypass the dated-event guard and slap a −8% hard stop on what might be a
     secular compounder.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.portfolio_advisor import advisor_pm, etoro_scan
from tradingagents.portfolio_advisor.models import (
    AdvisorPMCandidateComparison,
    AdvisorPMCycleResult,
)


# ---------------------------------------------------------------------------
# Helpers shared by F3 tests
# ---------------------------------------------------------------------------

def _make_fake_llm_result(summary: str = "all-cash: re-entry cycle") -> AdvisorPMCycleResult:
    return AdvisorPMCycleResult(executive_summary=summary)


def _alpaca_empty_portfolio_return():
    """Mimic fetch_portfolio_rows_alpaca() when the book is all cash (no positions)."""
    cash = 100_000.0
    portfolio_text = (
        f"Headlines: available_balance={cash!r}, unrealized_pnl=0, open_positions=0\n"
        "Account snapshot (Alpaca PAPER — autonomous mode, PM-managed)\n"
        f"Available balance (cash): {cash}\n"
        "Equity: 100000.0\n"
        "Unrealized P&L (aggregate): 0.0\n"
        "\n"
        "Open positions:\n"
        "  (none)"
    )
    tickers: List[str] = []   # <-- the key: empty list triggers F3
    rows: List[Dict[str, Any]] = []
    payload: Dict[str, Any] = {
        "clientPortfolio": {
            "credit": cash,
            "unrealizedPnL": 0.0,
            "positions": [],
            "mirrors": [],
        }
    }
    return payload, portfolio_text, tickers, rows


# ---------------------------------------------------------------------------
# F3 tests
# ---------------------------------------------------------------------------

class TestF3EmptyAlpacaBook:
    """run_pm_cycle must not raise when Alpaca book is all-cash."""

    def _run_with_empty_book(self, tmp_path):
        cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
        fake_result = _make_fake_llm_result()
        m_llm = MagicMock()
        m_client = MagicMock()
        m_client.get_llm.return_value = m_llm

        # Monkeypatch account_mode so the F3 guard sees "alpaca" (PYTEST_CURRENT_TEST
        # normally forces "etoro" — we need to override the guard inside the function).
        with patch.object(etoro_scan, "fetch_portfolio_rows",
                          return_value=_alpaca_empty_portfolio_return()):
            with patch(
                "tradingagents.portfolio_advisor.advisor_pm.etoro_scan.account_mode",
                return_value="alpaca",
            ):
                with patch(
                    "tradingagents.portfolio_advisor.advisor_pm.create_llm_client",
                    return_value=m_client,
                ):
                    with patch(
                        "tradingagents.portfolio_advisor.advisor_pm._coerce_pm_result_from_text",
                        return_value=fake_result,
                    ):
                        with patch("tradingagents.portfolio_advisor.advisor_pm.append_event"):
                            result = advisor_pm.run_pm_cycle(cfg, trigger="test_f3")
        return result, m_llm

    def test_does_not_raise(self, tmp_path):
        """run_pm_cycle must not raise RuntimeError on an empty Alpaca book."""
        result, _ = self._run_with_empty_book(tmp_path)
        assert result is not None
        assert isinstance(result, AdvisorPMCycleResult)

    def test_prompt_contains_all_cash_message(self, tmp_path):
        """The prompt sent to the LLM must describe the all-cash state."""
        _, m_llm = self._run_with_empty_book(tmp_path)
        # The LLM is called via bind_tools().invoke() or plain invoke().
        # Capture the prompt regardless of which path was taken.
        prompt_text = ""
        if m_llm.bind_tools.return_value.invoke.called:
            args = m_llm.bind_tools.return_value.invoke.call_args[0]
            if args:
                messages = args[0]
                # messages[0].content is the full prompt string
                prompt_text = messages[0].content if messages else ""
        elif m_llm.invoke.called:
            args = m_llm.invoke.call_args[0]
            if args:
                messages = args[0]
                prompt_text = messages[0].content if messages else ""

        # The all-cash text is embedded inside the portfolio snapshot block which
        # appears after the standing instructions — search the full prompt string.
        assert "100% cash" in prompt_text or "no open positions" in prompt_text, (
            f"Expected all-cash message somewhere in prompt "
            f"(length {len(prompt_text)}). "
            f"Last 500 chars: {prompt_text[-500:]!r}"
        )

    def test_live_tickers_empty_no_crash(self, tmp_path):
        """Empty live_tickers must not cause any downstream block to crash."""
        # This is implicitly tested by test_does_not_raise — if any block
        # crashed we would see an exception propagate.  We add an explicit
        # assertion that the result has a valid executive_summary so we know
        # the full pipeline ran.
        result, _ = self._run_with_empty_book(tmp_path)
        assert isinstance(result.executive_summary, str)

    def test_etoro_mode_still_raises(self, tmp_path):
        """In eToro mode, empty tickers must still raise RuntimeError."""
        cfg = {"portfolio_advisor_dir": str(tmp_path / "pa")}
        fake_result = _make_fake_llm_result()
        m_llm = MagicMock()
        m_client = MagicMock()
        m_client.get_llm.return_value = m_llm

        with patch.object(etoro_scan, "fetch_portfolio_rows",
                          return_value=_alpaca_empty_portfolio_return()):
            # account_mode returns "etoro" — the default in tests (PYTEST_CURRENT_TEST)
            # No need to patch; the guard inside run_pm_cycle calls etoro_scan.account_mode
            # which already returns "etoro" under pytest.
            with patch(
                "tradingagents.portfolio_advisor.advisor_pm.create_llm_client",
                return_value=m_client,
            ):
                with pytest.raises(RuntimeError, match="No tickers in eToro portfolio export"):
                    advisor_pm.run_pm_cycle(cfg, trigger="test_f3_etoro")

    def test_cash_amount_appears_in_prompt(self, tmp_path):
        """The prompt should name the actual cash balance so the PM knows deployment size."""
        _, m_llm = self._run_with_empty_book(tmp_path)
        prompt_text = ""
        if m_llm.bind_tools.return_value.invoke.called:
            args = m_llm.bind_tools.return_value.invoke.call_args[0]
            if args:
                messages = args[0]
                prompt_text = messages[0].content if messages else ""
        elif m_llm.invoke.called:
            args = m_llm.invoke.call_args[0]
            if args:
                messages = args[0]
                prompt_text = messages[0].content if messages else ""
        # $100,000.00 is the cash amount in our mock
        assert "100,000" in prompt_text or "100000" in prompt_text, (
            f"Expected cash amount in prompt. Got start: {prompt_text[:500]!r}"
        )


# ---------------------------------------------------------------------------
# F4C tests
# ---------------------------------------------------------------------------

def _result_with_catalyst_sleeve(
    candidate: str = "CRDO",
    sleeve: str = "catalyst",
    conviction: str = "high",
    rationale: str = "AI buildout — secular tailwind",
    **kw,
) -> AdvisorPMCycleResult:
    return AdvisorPMCycleResult(
        executive_summary="test",
        candidate_comparisons=[
            AdvisorPMCandidateComparison(
                candidate_ticker=candidate,
                replace_or_add="add",
                conviction=conviction,
                target_sleeve=sleeve,
                rationale=rationale,
                **kw,
            )
        ],
    )


@pytest.fixture
def captured_proposals(monkeypatch):
    """Capture proposals.add() calls without touching disk."""
    calls: List[Dict[str, Any]] = []

    import tradingagents.portfolio_advisor.proposals as proposals_mod
    import tradingagents.portfolio_advisor.messaging as messaging_mod

    def fake_add(cfg, **kw):
        calls.append(kw)
        return {"ok": True, **kw}

    def fake_send(cfg, source, body, **kw):
        return True

    monkeypatch.setattr(proposals_mod, "add", fake_add)
    monkeypatch.setattr(messaging_mod, "send_advisor_message", fake_send)
    return calls


class TestF4CCatalystSleeveForcing:
    """_apply_candidate_comparisons must force sleeve=core when LLM says catalyst."""

    def test_catalyst_sleeve_forced_to_core(self, captured_proposals):
        """catalyst target_sleeve → proposal recorded as core."""
        res = _result_with_catalyst_sleeve(sleeve="catalyst")
        applied = advisor_pm._apply_candidate_comparisons({}, res, live_tickers=set())

        assert len(captured_proposals) == 1
        buy = captured_proposals[0]
        assert buy["sleeve"] == "core", (
            f"Expected sleeve='core' after forcing; got {buy['sleeve']!r}"
        )

    def test_reason_gets_forced_core_note(self, captured_proposals):
        """The reason must contain the explanatory note when sleeve is forced."""
        res = _result_with_catalyst_sleeve(
            sleeve="catalyst",
            rationale="AI buildout — secular tailwind",
        )
        advisor_pm._apply_candidate_comparisons({}, res, live_tickers=set())
        buy = captured_proposals[0]
        assert "(forced core: no dated catalyst)" in buy["reason"], (
            f"Expected forcing note in reason; got {buy['reason']!r}"
        )

    def test_original_rationale_preserved(self, captured_proposals):
        """The original rationale must still appear in the reason after forcing."""
        original = "AI buildout — secular tailwind"
        res = _result_with_catalyst_sleeve(sleeve="catalyst", rationale=original)
        advisor_pm._apply_candidate_comparisons({}, res, live_tickers=set())
        buy = captured_proposals[0]
        assert original in buy["reason"], (
            f"Original rationale missing from reason; got {buy['reason']!r}"
        )

    def test_core_sleeve_unchanged(self, captured_proposals):
        """A core sleeve from the LLM must pass through unchanged."""
        res = _result_with_catalyst_sleeve(sleeve="core", rationale="compounder")
        advisor_pm._apply_candidate_comparisons({}, res, live_tickers=set())
        buy = captured_proposals[0]
        assert buy["sleeve"] == "core"
        assert "(forced core: no dated catalyst)" not in buy["reason"]

    def test_empty_sleeve_unchanged(self, captured_proposals):
        """An empty sleeve must pass through unchanged (not forced to core for a different reason)."""
        res = _result_with_catalyst_sleeve(sleeve="", rationale="compounder")
        advisor_pm._apply_candidate_comparisons({}, res, live_tickers=set())
        buy = captured_proposals[0]
        assert buy["sleeve"] == ""
        assert "(forced core: no dated catalyst)" not in buy["reason"]

    def test_applied_record_reflects_forced_sleeve(self, captured_proposals):
        """The applied list returned must also carry the forced sleeve."""
        res = _result_with_catalyst_sleeve(sleeve="catalyst")
        applied = advisor_pm._apply_candidate_comparisons({}, res, live_tickers=set())
        assert len(applied) == 1
        assert applied[0]["sleeve"] == "core", (
            f"applied entry sleeve should be 'core', got {applied[0]['sleeve']!r}"
        )

    def test_low_conviction_catalyst_not_recorded(self, captured_proposals):
        """Low-conviction catalyst comparisons must not reach proposals.add at all."""
        res = _result_with_catalyst_sleeve(sleeve="catalyst", conviction="low")
        applied = advisor_pm._apply_candidate_comparisons({}, res, live_tickers=set())
        assert captured_proposals == []
        assert applied == []
