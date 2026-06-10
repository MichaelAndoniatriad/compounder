"""propose_trade guard: a catalyst buy must name a dated event.

Regression: the PM filed CRDO (a secular AI compounder, no dated event) in the
catalyst sleeve just to fill it, which slapped a -8% hard stop on a name whose
own thesis said to ADD on a dip to ~$205 — the stop would fire at the buy price.
A catalyst buy with no catalyst_date must be rejected and pushed to core.
"""
from tradingagents.portfolio_advisor import pm_tools, proposals, position_plans


def _propose_tool(cfg, captured, monkeypatch=None):
    def fake_add(cfg, **kw):
        captured.append(kw)
        return {"id": "x", **kw}

    # Use monkeypatch when available so the patch is automatically reversed after
    # the test — direct assignment leaks into subsequent tests.
    if monkeypatch is not None:
        monkeypatch.setattr(proposals, "add", fake_add)
        monkeypatch.setattr(position_plans, "append_position_decision", lambda *a, **k: None)
    else:
        proposals.add = fake_add  # type: ignore[assignment]
        position_plans.append_position_decision = lambda *a, **k: None  # type: ignore[assignment]
    tools = pm_tools.build_pm_tools(cfg, live_tickers=set())
    return next(t for t in tools if getattr(t, "name", "") == "propose_trade")


def test_catalyst_buy_without_date_is_rejected(monkeypatch):
    captured: list = []
    monkeypatch.setattr(proposals, "add", lambda *a, **k: captured.append(k))
    tool = _propose_tool({}, captured, monkeypatch)
    out = tool.invoke({"ticker": "CRDO", "action": "buy", "sleeve": "catalyst",
                       "approx_usd": 222, "reason": "AI buildout — secular tailwind"})
    assert "catalyst_date" in out and "core" in out      # tells the PM how to fix it
    assert captured == []                                # nothing written


def test_catalyst_buy_with_date_proceeds(monkeypatch):
    captured: list = []
    tool = _propose_tool({}, captured, monkeypatch)
    out = tool.invoke({"ticker": "DKNG", "action": "buy", "sleeve": "catalyst",
                       "catalyst_date": "2026-06-11", "approx_usd": 275, "reason": "World Cup draw"})
    assert "error" not in out.lower()
    assert captured and captured[0]["ticker"] == "DKNG"


def test_core_buy_without_date_is_fine(monkeypatch):
    captured: list = []
    tool = _propose_tool({}, captured, monkeypatch)
    out = tool.invoke({"ticker": "VEEV", "action": "buy", "sleeve": "core",
                       "approx_usd": 400, "reason": "compounder — growth-adjusted value"})
    assert "error" not in out.lower()
    assert captured and captured[0]["sleeve"] == "core"


def test_catalyst_sell_is_unaffected(monkeypatch):
    """The guard only gates entries — exiting a catalyst name needs no date."""
    captured: list = []
    tool = _propose_tool({}, captured, monkeypatch)
    out = tool.invoke({"ticker": "DKNG", "action": "sell", "sleeve": "catalyst",
                       "shares": 10, "reason": "time-stop hit"})
    assert "error" not in out.lower()
    assert captured and captured[0]["action"] == "sell"
