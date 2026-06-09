"""The post-tool structured JSON pass that revives stances/candidate_comparisons.

deepseek-v4-pro answers in prose during the tool loop, so the final structured result
came back empty. _request_structured_result asks once more (no tools) for a JSON object
and coerces it. These tests cover JSON / fenced-JSON / prose / error inputs without a
real model.
"""
import json
from types import SimpleNamespace

from tradingagents.portfolio_advisor import advisor_pm


class _LLM:
    """Minimal stand-in: .invoke(messages) returns a message with .content."""

    def __init__(self, content):
        self._content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if isinstance(self._content, Exception):
            raise self._content
        return SimpleNamespace(content=self._content)


_GOOD = {
    "executive_summary": "Book is tech-heavy; catalyst sleeve empty.",
    "stances": [{"ticker": "NVDA", "stance": "hold", "rationale": "core"}],
    "candidate_comparisons": [{
        "candidate_ticker": "VEEV", "replace_or_add": "replace",
        "replace_ticker": "ADBE", "proposed_size_usd": 500,
        "target_sleeve": "core", "conviction": "high",
    }],
}


def test_plain_json_object_parses_into_structured_result():
    res = advisor_pm._request_structured_result(_LLM(json.dumps(_GOOD)), [])
    assert res is not None
    assert res.stances[0].ticker == "NVDA"
    assert res.candidate_comparisons[0].candidate_ticker == "VEEV"
    assert res.candidate_comparisons[0].replace_ticker == "ADBE"
    assert res.candidate_comparisons[0].conviction == "high"


def test_fenced_json_parses():
    fenced = "Here you go:\n```json\n" + json.dumps(_GOOD) + "\n```"
    res = advisor_pm._request_structured_result(_LLM(fenced), [])
    assert res is not None and res.candidate_comparisons[0].candidate_ticker == "VEEV"


def test_prose_returns_none():
    res = advisor_pm._request_structured_result(
        _LLM("Alright, here's the deal. DKNG is the clear winner."), []
    )
    assert res is None


def test_model_error_returns_none():
    res = advisor_pm._request_structured_result(_LLM(RuntimeError("boom")), [])
    assert res is None


def test_appends_one_instruction_message():
    llm = _LLM(json.dumps(_GOOD))
    advisor_pm._request_structured_result(llm, [SimpleNamespace(content="prior")])
    assert llm.calls == 1  # exactly one extra non-tool pass
