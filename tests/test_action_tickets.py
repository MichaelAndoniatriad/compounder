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

def test_catalyst_buy_full_size_with_stop_and_cap():
    t = pr.format_action_ticket({}, _p(ticker="DKNG", action="buy", shares=10, approx_usd=275,
                                       target_price=27.46, sleeve="catalyst", reason="World Cup"))
    assert "🟢 BUY DKNG" in t and "catalyst" in t
    assert "Buy: full size today" in t                 # full size for catalyst
    assert "$25.26" in t                               # -8% stop (27.46*0.92)
    assert "30-day" in t                               # max-hold cap, no catalyst date
    assert "Why: World Cup" in t


def test_catalyst_buy_with_date_prints_concrete_exit_day():
    t = pr.format_action_ticket({}, _p(ticker="DKNG", action="buy", target_price=27.46,
                                       sleeve="catalyst", catalyst_date="2026-06-11", reason="WC"))
    assert "Sell by Jun 14" in t                        # 06-11 + 3-day time-stop


def test_core_buy_scales_in_with_concrete_levels():
    t = pr.format_action_ticket({}, _p(ticker="VEEV", action="buy", approx_usd=600,
                                       target_price=200, sleeve="core"))
    assert "~$200" in t and "~1/3 of a ~$600 target" in t   # first buy = a third of the total
    assert "2–4 wks" in t and "not all at once" in t        # cadence is explicit, not a fixed calendar
    assert "+100% ($400.00" in t and "−40% ($120.00" in t   # 200*2 and 200*0.6
    assert "thesis-break" in t


def test_core_buy_without_price_shows_pct_rules():
    t = pr.format_action_ticket({}, _p(ticker="VEEV", action="buy", approx_usd=600, sleeve="core"))
    assert "~1/3 of a ~$600 target" in t and "+100% trim half" in t and "−40% exit" in t
    assert "$" not in t.split("Sell:")[1].split("\n")[0]    # no fabricated price levels


def test_catalyst_exit_by_helper():
    assert pr._catalyst_exit_by({}, "2026-06-11") == "Jun 14"            # +3 default
    assert pr._catalyst_exit_by({"portfolio_advisor_catalyst_time_stop_days": 5}, "2026-06-11") == "Jun 16"
    assert pr._catalyst_exit_by({}, "") == ""
    assert pr._catalyst_exit_by({}, "not-a-date") == ""


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


def test_reconcile_cancels_sells_for_unheld_but_never_buys(monkeypatch):
    """A BUY is FOR a name you don't hold yet — reconcile must not kill it.

    Regression: every catalyst entry (CRDO, DKNG) was auto-cancelled 16s after
    proposal because the name wasn't in the book. Also verifies stand-down fires
    only for the cancelled sell, not for surviving proposals.
    """
    store = [
        {"ticker": "CRDO", "action": "buy", "status": "proposed"},   # new position
        {"ticker": "DKNG", "action": "buy", "status": "proposed"},   # new position
        {"ticker": "NFLX", "action": "sell", "status": "proposed"},  # exit a held name
        {"ticker": "GONE", "action": "sell", "status": "proposed"},  # exit a name now closed
    ]
    monkeypatch.setattr(pr, "load_all", lambda cfg: list(store))
    monkeypatch.setattr(pr, "save_all", lambda cfg, rows: (store.clear(), store.extend(rows)))
    stood_down: list = []
    monkeypatch.setattr(pr, "_send_stand_down", lambda cfg, e, reason: stood_down.append(e["ticker"]))

    n = pr.reconcile_with_portfolio({}, held_tickers=["NFLX", "AAPL"])

    by_t = {r["ticker"]: r["status"] for r in store}
    assert by_t["CRDO"] == "proposed"   # buy survives
    assert by_t["DKNG"] == "proposed"   # buy survives
    assert by_t["NFLX"] == "proposed"   # sell of a held name survives
    assert by_t["GONE"] == "cancelled"  # sell of a closed name is cleared
    assert n == 1
    assert stood_down == ["GONE"]       # stand-down fires exactly once, for the cancelled sell


def test_kill_switch_silences_tickets(monkeypatch):
    store: list = []
    monkeypatch.setattr(pr, "load_all", lambda cfg: list(store))
    monkeypatch.setattr(pr, "save_all", lambda cfg, rows: (store.clear(), store.extend(rows)))
    sent: list = []
    monkeypatch.setattr(pr, "_send_action_ticket", lambda cfg, e: sent.append(e))
    pr.add({"portfolio_advisor_action_tickets": False}, ticker="DKNG", action="buy", approx_usd=275)
    assert sent == []
