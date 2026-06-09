"""Quality-on-a-dip watch: classification + latch/hand-off behaviour.

The dip-watch is a price-only sweep over CORE watchlist names. It must (1) only
flag a SHALLOW pullback, never a falling knife, (2) wake the PM exactly once when a
name first enters the buy zone (not every scan), (3) re-arm after the name leaves
the zone, and (4) never surface a name already held.
"""
import tradingagents.portfolio_advisor.advisor_pm as advisor_pm
from tradingagents.portfolio_advisor import dip_watch as dw


def _sig(below, off_high, rsi=40.0, price=100.0):
    return {"price": price, "ma": price * (1 + below / 100.0),
            "below_ma_pct": below, "off_high_pct": off_high, "rsi": rsi}


# --- classify_dip ----------------------------------------------------------

def test_classify_shallow_dip_is_buy_zone():
    v, _ = dw.classify_dip(_sig(below=5, off_high=15, rsi=40), {})
    assert v == "buy_zone"


def test_classify_deep_below_ma_is_falling_knife():
    v, why = dw.classify_dip(_sig(below=30, off_high=20), {})
    assert v == "falling_knife" and "too deep" in why


def test_classify_collapsed_off_high_is_falling_knife():
    v, _ = dw.classify_dip(_sig(below=5, off_high=40), {})
    assert v == "falling_knife"


def test_classify_capitulation_rsi_is_falling_knife():
    v, _ = dw.classify_dip(_sig(below=5, off_high=15, rsi=20), {})
    assert v == "falling_knife"


def test_classify_above_trend_is_neutral():
    v, _ = dw.classify_dip(_sig(below=-3, off_high=2), {})
    assert v == "neutral"


def test_classify_not_off_high_enough_is_neutral():
    # below the MA but only 3% off the high — not a real pullback yet
    v, _ = dw.classify_dip(_sig(below=5, off_high=3), {})
    assert v == "neutral"


def test_classify_thresholds_are_config_driven():
    # widen the knife threshold so a 30%-below name now reads as buy zone
    cfg = {"portfolio_advisor_dip_watch_falling_knife_below_ma_pct": 40.0,
           "portfolio_advisor_dip_watch_max_below_ma_pct": 35.0}
    v, _ = dw.classify_dip(_sig(below=30, off_high=20), cfg)
    assert v == "buy_zone"


# --- run_dip_watch end-to-end ---------------------------------------------

class _Env:
    def __init__(self, monkeypatch, watchlist, signals, held=()):
        self.signals = dict(signals)
        self.calls = []          # (trigger, extra_context) per PM hand-off
        self.store = {}          # in-memory advisor state
        monkeypatch.setattr(dw.watchlist_mod, "load_watchlist", lambda cfg: list(watchlist))
        monkeypatch.setattr(dw.price_util, "dip_signal_yfinance",
                            lambda tk, ma_window=50: self.signals.get(tk))
        monkeypatch.setattr(dw, "_held_tickers", lambda cfg: set(held))
        monkeypatch.setattr(dw.pa_state, "load_state", lambda cfg: self.store)
        monkeypatch.setattr(dw.pa_state, "save_state", lambda cfg, st: None)
        monkeypatch.setattr(advisor_pm, "run_pm_cycle",
                            lambda cfg, trigger=None, extra_context=None: self.calls.append((trigger, extra_context)))

    def run(self):
        return dw.run_dip_watch({}, ignore_market_hours=True)


def test_fresh_dip_hands_off_to_pm_once_then_stays_silent(monkeypatch):
    env = _Env(
        monkeypatch,
        watchlist=[{"ticker": "VEEV", "strategy": "core", "thesis": "vertical SaaS"},
                   {"ticker": "INCY", "strategy": "core"},          # falling knife
                   {"ticker": "DKNG", "strategy": "catalyst"}],      # ignored (catalyst)
        signals={"VEEV": _sig(below=5, off_high=15, rsi=40),
                 "INCY": _sig(below=30, off_high=20),
                 "DKNG": _sig(below=5, off_high=15)},
    )
    assert env.run() == 1                       # only VEEV
    assert len(env.calls) == 1
    trigger, extra = env.calls[0]
    assert trigger == "dip_watch_triggered"
    assert "VEEV" in extra and "INCY" not in extra and "DKNG" not in extra
    assert "value trap" in extra and "propose_trade" in extra   # directive to the PM

    assert env.run() == 0                       # still in zone -> no re-ping
    assert len(env.calls) == 1


def test_reentry_fires_again_after_leaving_zone(monkeypatch):
    env = _Env(monkeypatch,
               watchlist=[{"ticker": "VEEV", "strategy": "core"}],
               signals={"VEEV": _sig(below=5, off_high=15)})
    assert env.run() == 1 and len(env.calls) == 1
    env.signals["VEEV"] = _sig(below=-2, off_high=3)    # recovered above trend
    assert env.run() == 0 and len(env.calls) == 1       # re-armed, silent
    env.signals["VEEV"] = _sig(below=6, off_high=14)    # dipped again
    assert env.run() == 1 and len(env.calls) == 2       # fires on the new dip


def test_held_name_is_never_surfaced(monkeypatch):
    env = _Env(monkeypatch,
               watchlist=[{"ticker": "VEEV", "strategy": "core"}],
               signals={"VEEV": _sig(below=5, off_high=15)},
               held={"VEEV"})
    assert env.run() == 0 and env.calls == []


def test_pm_failure_leaves_handoff_pending_for_retry(monkeypatch):
    env = _Env(monkeypatch,
               watchlist=[{"ticker": "VEEV", "strategy": "core"}],
               signals={"VEEV": _sig(below=5, off_high=15)})

    def boom(cfg, trigger=None, extra_context=None):
        raise RuntimeError("PM down")

    monkeypatch.setattr(advisor_pm, "run_pm_cycle", boom)
    assert env.run() == 1                                  # counted as a new dip
    assert env.store["dip_watch_triggers"]["VEEV"]["handoff_pending"] is True

    # PM recovers — next scan retries the still-pending hand-off
    monkeypatch.setattr(advisor_pm, "run_pm_cycle",
                        lambda cfg, trigger=None, extra_context=None: env.calls.append((trigger, extra_context)))
    assert env.run() == 1
    assert len(env.calls) == 1
    assert env.store["dip_watch_triggers"]["VEEV"]["handoff_pending"] is False


def test_disabled_kill_switch(monkeypatch):
    env = _Env(monkeypatch,
               watchlist=[{"ticker": "VEEV", "strategy": "core"}],
               signals={"VEEV": _sig(below=5, off_high=15)})
    assert dw.run_dip_watch({"portfolio_advisor_dip_watch_enabled": False},
                            ignore_market_hours=True) == 0
    assert env.calls == []
