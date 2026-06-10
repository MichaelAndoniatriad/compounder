"""Tests for the Telegram markdown sanitiser and its integration into _telegram_text."""

from tradingagents.portfolio_advisor.messaging import _strip_markdown_for_telegram, _telegram_text

# ---------------------------------------------------------------------------
# The real-world sample that triggered this fix
# ---------------------------------------------------------------------------
_REAL_WORLD_SAMPLE = """\
---

**Stances**

| Ticker | Stance | Why |
|--------|--------|-----|
| TEAM | Hold | Human closed both TEAM positions June 9; 11 lots still showing is eToro API lag. Do not re-alert. |
| CVLT | Hold | +29%, pre-earnings trim armed but 48 days out. Full_graph June 9 Hold. No change. |
| ANET | Hold | Momentum intact, no new catalyst. Hold. |
_Evidence refs: ANET full_graph_decision 2026-06-09, ..._"""


def test_sanitize_real_world_sample_no_table_rows():
    out = _strip_markdown_for_telegram(_REAL_WORLD_SAMPLE)
    lines = out.splitlines()
    for line in lines:
        stripped = line.strip()
        assert not (stripped.startswith("|") and stripped.endswith("|")), (
            f"Table row survived sanitisation: {line!r}"
        )


def test_sanitize_real_world_sample_no_bold():
    out = _strip_markdown_for_telegram(_REAL_WORLD_SAMPLE)
    assert "**" not in out, "Bold markers (**) survived sanitisation"


def test_sanitize_real_world_sample_no_standalone_hr():
    out = _strip_markdown_for_telegram(_REAL_WORLD_SAMPLE)
    for line in out.splitlines():
        stripped = line.strip()
        # A line of only dashes (with optional spaces) is a horizontal rule
        assert not (stripped and all(c in "-_ *" for c in stripped) and len(stripped) >= 3 and set(stripped) != {" "}), (
            f"Horizontal rule survived sanitisation: {line!r}"
        )


def test_sanitize_real_world_sample_team_hold_rendered():
    out = _strip_markdown_for_telegram(_REAL_WORLD_SAMPLE)
    assert "TEAM: Hold — Human closed both TEAM positions" in out, (
        f"Expected TEAM table row to be rendered as prose. Got:\n{out}"
    )


def test_sanitize_real_world_sample_evidence_refs_no_underscores():
    out = _strip_markdown_for_telegram(_REAL_WORLD_SAMPLE)
    # The _Evidence refs: ..._ line should have outer underscores stripped
    assert "Evidence refs:" in out, "Evidence refs line disappeared"
    # The leading/trailing underscores of the line-level italic should be gone
    assert not any(line.strip().startswith("_Evidence") for line in out.splitlines()), (
        "Outer leading underscore survived on Evidence refs line"
    )


def test_sanitize_real_world_sample_identifier_intact():
    out = _strip_markdown_for_telegram(_REAL_WORLD_SAMPLE)
    assert "full_graph_decision" in out, (
        "Identifier full_graph_decision was mangled by the sanitiser"
    )


# ---------------------------------------------------------------------------
# Prose passthrough — normal conversational messages must be untouched
# ---------------------------------------------------------------------------
_PROSE_SAMPLE = (
    "ISRG filled 6 sh @ $421 today 🎉 — up +2.9% on the week. "
    "Still watching Technology | rev_growth 20.9% | ROIC 30.8% for the next entry."
)


def test_prose_passthrough_unchanged():
    out = _strip_markdown_for_telegram(_PROSE_SAMPLE)
    assert out == _PROSE_SAMPLE, (
        f"Prose passthrough was unexpectedly modified.\nBefore: {_PROSE_SAMPLE!r}\nAfter: {out!r}"
    )


# ---------------------------------------------------------------------------
# Integration: _telegram_text wraps the sanitiser
# ---------------------------------------------------------------------------
def test_telegram_text_strips_table():
    out = _telegram_text("PM", _REAL_WORLD_SAMPLE)
    lines = out.splitlines()
    for line in lines:
        stripped = line.strip()
        assert not (stripped.startswith("|") and stripped.endswith("|")), (
            f"Table row survived _telegram_text: {line!r}"
        )


# ---------------------------------------------------------------------------
# Code fence: content kept, fence lines dropped
# ---------------------------------------------------------------------------
def test_code_fence_content_kept():
    src = "```\nfoo\n```"
    out = _strip_markdown_for_telegram(src)
    assert "foo" in out
    assert "```" not in out


# ---------------------------------------------------------------------------
# ATX header stripping
# ---------------------------------------------------------------------------
def test_atx_header_stripped():
    src = "## Plan"
    out = _strip_markdown_for_telegram(src)
    assert out.strip() == "Plan"
    assert "#" not in out
