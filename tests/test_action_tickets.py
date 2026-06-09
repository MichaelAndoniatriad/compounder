"""Order-ticket rendering + 'only ping on a real/changed action' gating.

The advisor must message only when there's a concrete, executable move, and
each message must say what + how much, at what price, and when to exit.
"""
from tradingagents.portfolio_advisor import proposals as pr


def _p(**kw):
    base = dict(ticker="X", action="buy", shares=0, approx_usd=0, target_price=0, sleeve=None, reason="")
    base.update(kw)
    return base


# --- format_action_ticket ---------------------------------------------------

def test_catalyst_buy_ticket_has_size_entry_and_stop():
    t = pr.format_action_ticket({}, _p(ticker="DKNG", action="buy", shares=10, approx_usd=275,
                                       target_price=27.46, sleeve="catalyst", reason="World Cup"))
    assert "🟢 BUY DKNG" in t
    assert "10 sh" in t and "$275" in t and "$27.46" in t and "catalyst" in t
    assert "Buy: today" in t                          # entry timing
    assert "Sell: short hold" in t and "$25.26" in t  # exit timing + -8% stop (27.46*0.92)
    assert "Why: World Cup" in t


def test_core_buy_ticket_shows_horizon_not_a_stop():
    t = pr.format_action_ticket({}, _p(ticker="VEEV", action="buy", approx_usd=500, sleeve="core"))
    assert "Buy: this week" in t
    assert "Sell: 3–5 yr hold" in t and "thesis-break" in t
    assert "−8% stop" not in t


def test_sell_ticket_says_act_today_with_reason():
    t = pr.format_action_ticket({}, _p(ticker="NFLX", action="sell", shares=2.14, approx_usd=177,
                                       reason="thesis broken"))
    assert "🔴 SELL NFLX" in t and "2.14 sh" in t and "$177" in t
    assert "When: today" in t
    assert "Why: thesis broken" in t


# --- _proposal_is_new_or_changed --------------------------------------------

def test_new_proposal_notifies():
    assert pr._proposal_is_new_or_changed(None, _p(approx_usd=275)) is True


def test_minor_drift_stays_silent():
    prior = _p(action="buy", approx_usd=275)
    assert pr._proposal_is_new_or_changed(prior, _p(action="buy", approx_usd=290)) is False  # ~5%


def test_side_flip_and_material_resize_notify():
    prior = _p(action="buy", approx_usd=275)
    assert pr._proposal_is_new_or_changed(prior, _p(action="sell", approx_usd=275)) is True
    assert pr._proposal_is_new_or_changed(prior, _p(action="buy", approx_usd=400)) is True   # +45%


# --- add() end-to-end gating ------------------------------------------------

def test_add_pings_on_new_then_silent_on_drift_then_pings_on_resize(monkeypatch):
    store: list = []
    monkeypatch.setattr(pr, "load_all", lambda cfg: list(store))
    monkeypatch.setattr(pr, "save_all", lambda cfg, rows: (store.clear(), store.extend(rows)))
    sent: list = []
    monkeypatch.setattr(pr, "_send_action_ticket", lambda cfg, e: sent.append(e))
    cfg: dict = {}

    pr.add(cfg, ticker="DKNG", action="buy", shares=10, approx_usd=275, sleeve="catalyst", reason="catalyst")
    assert len(sent) == 1                     # new -> ticket
    pr.add(cfg, ticker="DKNG", action="buy", shares=10, approx_usd=285, sleeve="catalyst", reason="catalyst")
    assert len(sent) == 1                     # ~4% drift -> silent
    pr.add(cfg, ticker="DKNG", action="buy", shares=15, approx_usd=410, sleeve="catalyst", reason="upsized")
    assert len(sent) == 2                     # +49% -> ticket


def test_kill_switch_silences_tickets(monkeypatch):
    store: list = []
    monkeypatch.setattr(pr, "load_all", lambda cfg: list(store))
    monkeypatch.setattr(pr, "save_all", lambda cfg, rows: (store.clear(), store.extend(rows)))
    sent: list = []
    monkeypatch.setattr(pr, "_send_action_ticket", lambda cfg, e: sent.append(e))
    pr.add({"portfolio_advisor_action_tickets": False}, ticker="DKNG", action="buy", approx_usd=275)
    assert sent == []
