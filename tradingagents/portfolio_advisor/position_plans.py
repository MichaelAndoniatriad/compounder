"""Per-position exit plans and deterministic rule-trigger checks.

Each position is tagged with a strategy sleeve that determines which exit rules apply:

  core (long-term hold + growth, 3-5yr):
    - trim_at: entry * 1.15   (pre-earnings trim trigger)
    - double_at: entry * 2.0  (double-from-entry trigger: sell half)
    - soft_stop: entry * 0.70 (-30%: review at next earnings)
    - hard_stop: entry * 0.60 (-40%: mandatory full exit)
    - exit on thesis-break metric firing on earnings/material news

  catalyst (short-term, event-driven):
    - hard_stop: entry * (1 - catalyst_hard_stop_pct)   (default -8%)
    - trailing_stop: once up trailing_activate_pct (default +10%), exit if down
      trailing_stop_pct (default 8%) from the tracked peak
    - time_stop: exit time_stop_days after catalyst_date if the move didn't happen
    - max_hold: hard cap of max_hold_days when no catalyst_date is set

The sleeve is locked at entry. A catalyst trade that is underwater cannot be
relabeled "core" to dodge its tight stop, and vice versa.

compute_trigger_status() is called deterministically each PM cycle and returns
structured TRIGGERED/WATCH/OK flags per position — the PM reads these, not computes them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tradingagents.portfolio_advisor import state

logger = logging.getLogger(__name__)

STRATEGIES = ("core", "catalyst")
_DEFAULT_STRATEGY = "core"


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class CatalystRules:
    hard_stop_pct: float = 0.08
    trailing_activate_pct: float = 0.10
    trailing_stop_pct: float = 0.08
    time_stop_days: int = 3
    max_hold_days: int = 30


def catalyst_rules_from_cfg(cfg: Dict[str, Any]) -> CatalystRules:
    def _f(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return CatalystRules(
        hard_stop_pct=_f("portfolio_advisor_catalyst_hard_stop_pct", 0.08),
        trailing_activate_pct=_f("portfolio_advisor_catalyst_trailing_activate_pct", 0.10),
        trailing_stop_pct=_f("portfolio_advisor_catalyst_trailing_stop_pct", 0.08),
        time_stop_days=_i("portfolio_advisor_catalyst_time_stop_days", 3),
        max_hold_days=_i("portfolio_advisor_catalyst_max_hold_days", 30),
    )


@dataclass
class PositionPlan:
    ticker: str
    entry_price: float
    strategy: str = _DEFAULT_STRATEGY
    entry_date: str = ""
    thesis_break_metrics: List[str] = field(default_factory=list)
    target_horizon: str = "3-5 years"
    notes: str = ""
    # Catalyst-only fields:
    catalyst_date: str = ""
    catalyst_description: str = ""
    peak_price: Optional[float] = None
    # Append-only log of each PM decision on this position (the WHAT + the WHY):
    #   {ts, action (buy/sell/trim/add/hold/watch/…), rationale, source, price?}
    decision_history: List[Dict[str, Any]] = field(default_factory=list)
    triggered_trim_until: Optional[str] = None  # ISO date; sleeve rebalancing skips this position until then
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        self.strategy = (self.strategy or _DEFAULT_STRATEGY).strip().lower()
        if self.strategy not in STRATEGIES:
            self.strategy = _DEFAULT_STRATEGY

    @property
    def is_catalyst(self) -> bool:
        return self.strategy == "catalyst"

    # --- core sleeve thresholds ---
    @property
    def trim_at(self) -> float:
        return round(self.entry_price * 1.15, 4)

    @property
    def double_at(self) -> float:
        return round(self.entry_price * 2.0, 4)

    @property
    def soft_stop(self) -> float:
        return round(self.entry_price * 0.70, 4)

    @property
    def hard_stop_core(self) -> float:
        return round(self.entry_price * 0.60, 4)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.strategy == "core":
            d["trim_at"] = self.trim_at
            d["double_at"] = self.double_at
            d["soft_stop"] = self.soft_stop
            d["hard_stop"] = self.hard_stop_core
        return d


def _plans_path(cfg: Dict[str, Any]) -> Path:
    return state.advisor_dir(cfg) / "position_plans.json"


def load_position_plans(cfg: Dict[str, Any]) -> Dict[str, PositionPlan]:
    path = _plans_path(cfg)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, PositionPlan] = {}
    for ticker, data in raw.items():
        if not isinstance(data, dict):
            continue
        try:
            ep = float(data.get("entry_price") or 0)
            if ep <= 0:
                continue
            peak = data.get("peak_price")
            out[ticker.upper()] = PositionPlan(
                ticker=ticker.upper(),
                entry_price=ep,
                strategy=str(data.get("strategy") or _DEFAULT_STRATEGY),
                entry_date=str(data.get("entry_date") or ""),
                thesis_break_metrics=list(data.get("thesis_break_metrics") or []),
                target_horizon=str(data.get("target_horizon") or "3-5 years"),
                notes=str(data.get("notes") or ""),
                catalyst_date=str(data.get("catalyst_date") or ""),
                catalyst_description=str(data.get("catalyst_description") or ""),
                peak_price=float(peak) if peak not in (None, "") else None,
                decision_history=[d for d in (data.get("decision_history") or []) if isinstance(d, dict)],
                last_updated=str(data.get("last_updated") or ""),
            )
        except (TypeError, ValueError):
            continue
    return out


def save_position_plans(cfg: Dict[str, Any], plans: Dict[str, PositionPlan]) -> None:
    path = _plans_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {ticker: plan.to_dict() for ticker, plan in plans.items()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def upsert_position_plan(cfg: Dict[str, Any], plan: PositionPlan) -> None:
    plans = load_position_plans(cfg)
    plan.last_updated = datetime.now(timezone.utc).isoformat()
    plans[plan.ticker.upper()] = plan
    save_position_plans(cfg, plans)


_DECISION_HISTORY_MAX = 20


def append_position_decision(
    cfg: Dict[str, Any],
    ticker: str,
    action: str,
    rationale: str,
    *,
    source: str = "pm",
    price: Optional[float] = None,
    max_entries: int = _DECISION_HISTORY_MAX,
) -> bool:
    """Record one dated decision (the action AND the WHY) on a held position's plan.

    Returns True if a new entry was written. No-op (returns False) when:
      - the ticker has no plan on file yet (brand-new buys live in
        proposed_trades.jsonl until the position is opened and a plan exists), or
      - the decision is identical in action and rationale to the most recent
        entry (so repeated "hold, thesis intact" cycles don't balloon the log).
    """
    tk = (ticker or "").strip().upper()
    act = (action or "").strip().lower()
    why = (rationale or "").strip()
    if not tk or not act or not why:
        return False
    plans = load_position_plans(cfg)
    plan = plans.get(tk)
    if plan is None:
        return False
    history = [d for d in (plan.decision_history or []) if isinstance(d, dict)]
    if history:
        last = history[-1]
        if (
            str(last.get("action") or "").strip().lower() == act
            and str(last.get("rationale") or "").strip()[:200] == why[:200]
        ):
            return False
    entry: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": act,
        "rationale": why[:600],
        "source": (source or "pm").strip()[:40],
    }
    px = _to_float(price)
    if px is not None:
        entry["price"] = round(px, 4)
    history.append(entry)
    if max_entries > 0 and len(history) > max_entries:
        history = history[-max_entries:]
    plan.decision_history = history
    plans[tk] = plan
    save_position_plans(cfg, plans)
    return True


def backfill_plans_from_portfolio(
    cfg: Dict[str, Any],
    rows: List[Dict[str, Any]],
    *,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Create core position plans for live holdings using their eToro weighted-avg cost basis.

    Entry price per ticker = sum(capital) / sum(units) across that ticker's lots, i.e. the
    weighted-average open rate. Defaults every backfilled plan to the 'core' sleeve; the user
    reclassifies catalyst names with `position-plan set ... --strategy catalyst`. Existing plans
    are left untouched unless overwrite=True. Returns a summary dict.
    """
    from tradingagents.integrations.etoro.clerk_bridge import _normalize_ticker

    existing = load_position_plans(cfg)
    by_ticker: Dict[str, Dict[str, float]] = {}
    for r in rows or []:
        tk = _normalize_ticker(str(r.get("symbolFull") or ""))
        if not tk:
            continue
        units = _to_float(r.get("units"))
        capital = _to_float(r.get("unitsBaseValueDollars"))
        if units is None or capital is None or units <= 0:
            continue
        agg = by_ticker.setdefault(tk, {"units": 0.0, "capital": 0.0})
        agg["units"] += units
        agg["capital"] += capital

    created: List[str] = []
    skipped_existing: List[str] = []
    skipped_no_data: List[str] = []
    for tk, agg in sorted(by_ticker.items()):
        if tk in existing and not overwrite:
            skipped_existing.append(tk)
            continue
        if agg["units"] <= 0:
            skipped_no_data.append(tk)
            continue
        entry = round(agg["capital"] / agg["units"], 6)
        if entry <= 0:
            skipped_no_data.append(tk)
            continue
        upsert_position_plan(
            cfg,
            PositionPlan(
                ticker=tk,
                entry_price=entry,
                strategy="core",
                notes="Backfilled from eToro weighted-avg open rate; reclassify sleeve / add thesis-break metrics.",
            ),
        )
        created.append(tk)

    return {
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_no_data": skipped_no_data,
    }


def strategy_for_ticker(
    cfg: Dict[str, Any],
    ticker: str,
    job: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve the strategy sleeve for a ticker about to be researched.

    Order of precedence: explicit job strategy → existing position plan →
    candidate state record → 'core' default.
    """
    tk = str(ticker or "").strip().upper()
    if job:
        js = str(job.get("strategy") or "").strip().lower()
        if js in STRATEGIES:
            return js
    plans = load_position_plans(cfg)
    if tk in plans:
        return plans[tk].strategy
    try:
        from tradingagents.portfolio_advisor.candidates import load_candidate_state

        rec = (load_candidate_state(cfg).get("candidates") or {}).get(tk)
        if rec:
            cs = str(rec.get("strategy") or "").strip().lower()
            if cs in STRATEGIES:
                return cs
    except Exception:
        pass
    return _DEFAULT_STRATEGY


def remove_position_plan(cfg: Dict[str, Any], ticker: str) -> bool:
    plans = load_position_plans(cfg)
    key = ticker.strip().upper()
    if key not in plans:
        return False
    del plans[key]
    save_position_plans(cfg, plans)
    return True


def _days_since(date_str: str) -> Optional[int]:
    """Whole days from the YYYY-MM-DD prefix of date_str until now (UTC)."""
    s = (date_str or "").strip()[:10]
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


@dataclass
class TriggerStatus:
    ticker: str
    strategy: str
    entry_price: float
    current_price: Optional[float]
    pct_from_entry: Optional[float]
    thresholds: Dict[str, float]
    thesis_break_metrics: List[str]
    target_horizon: str
    triggered: List[str]
    watch: List[str]
    catalyst_date: str = ""
    catalyst_description: str = ""
    peak_price: Optional[float] = None
    decision_history: List[Dict[str, Any]] = field(default_factory=list)

    def is_clean(self) -> bool:
        return not self.triggered and not self.watch

    def summary_line(self) -> str:
        px = f"${self.current_price:.2f}" if self.current_price else "price unknown"
        pct = f"{self.pct_from_entry:+.1f}%" if self.pct_from_entry is not None else ""
        base = f"{self.ticker} [{self.strategy}]: entry=${self.entry_price:.2f}, now={px} {pct}".strip()
        thr = "  thresholds: " + ", ".join(f"{k}=${v:.2f}" for k, v in self.thresholds.items())
        lines = [base, thr]
        if self.is_catalyst_strategy:
            cat = f"  catalyst: {self.catalyst_description or '(none)'}"
            if self.catalyst_date:
                cat += f" (date: {self.catalyst_date})"
            if self.peak_price:
                cat += f"; peak=${self.peak_price:.2f}"
            lines.append(cat)
        else:
            if self.thesis_break_metrics:
                lines.append(f"  thesis_break_metrics: {'; '.join(self.thesis_break_metrics)}")
            else:
                lines.append("  thesis_break_metrics: NOT SET — write them before the next earnings print")
            lines.append(f"  target_horizon: {self.target_horizon}")
        if self.triggered:
            lines.append("  TRIGGERED: " + ", ".join(self.triggered))
        elif self.watch:
            lines.append("  WATCH: " + ", ".join(self.watch))
        else:
            lines.append("  status: OK")
        if self.decision_history:
            recent = [d for d in self.decision_history if isinstance(d, dict)][-2:]
            notes = "; ".join(
                f"{str(d.get('ts') or '')[:10]} {str(d.get('action') or '').lower()}: "
                f"{str(d.get('rationale') or '').strip()[:140]}"
                for d in recent
            )
            if notes:
                lines.append(f"  recent decisions (why): {notes}")
        return "\n".join(lines)

    @property
    def is_catalyst_strategy(self) -> bool:
        return self.strategy == "catalyst"


def _days_until(date_str: str) -> Optional[int]:
    """Whole days from now (UTC) until the YYYY-MM-DD prefix of date_str (negative = past)."""
    since = _days_since(date_str)
    return None if since is None else -since


def _core_triggers(
    plan: PositionPlan,
    px: Optional[float],
    next_earnings_date: Optional[str] = None,
    earnings_window_days: int = 14,
) -> tuple[List[str], List[str], Dict[str, float]]:
    thresholds = {
        "trim_at": plan.trim_at,
        "double_at": plan.double_at,
        "soft_stop": plan.soft_stop,
        "hard_stop": plan.hard_stop_core,
    }
    triggered: List[str] = []
    watch: List[str] = []
    if px is not None:
        if px >= plan.double_at:
            triggered.append("DOUBLE_FROM_ENTRY: sell half, remainder is house money")
        elif px >= plan.trim_at:
            # Up 15%+: the trim only executes going INTO an earnings event.
            days_to = _days_until(next_earnings_date) if next_earnings_date else None
            pct_up = (px / plan.entry_price - 1) * 100 if plan.entry_price else 0
            if days_to is not None and 0 <= days_to <= earnings_window_days:
                triggered.append(
                    f"PRE_EARNINGS_TRIM: up {pct_up:.0f}% and earnings in {days_to}d "
                    f"({next_earnings_date}) — sell half before the print"
                )
            elif days_to is not None and days_to > earnings_window_days:
                watch.append(
                    f"pre-earnings trim armed (up {pct_up:.0f}%); earnings {next_earnings_date} "
                    f"({days_to}d out) — trim half as it approaches"
                )
            else:
                # No usable future earnings date (unknown, or the last print just passed).
                watch.append(
                    f"pre-earnings trim armed (up {pct_up:.0f}%); next earnings date not confirmed — "
                    "verify and trim half before the next print"
                )
        elif px <= plan.hard_stop_core:
            triggered.append("HARD_STOP_40PCT: exit full position, no exceptions")
        elif px <= plan.soft_stop:
            triggered.append("SOFT_STOP_30PCT: thesis intact review — action window = next earnings")
        if not triggered and not watch:
            if px >= plan.trim_at * 0.90:
                watch.append(f"approaching pre-earnings trim (trim_at=${plan.trim_at:.2f})")
            if px <= plan.soft_stop * 1.10:
                watch.append(f"approaching soft stop (soft_stop=${plan.soft_stop:.2f})")
    if not plan.thesis_break_metrics:
        watch.append("thesis_break_metrics not set")
    return triggered, watch, thresholds


def _catalyst_triggers(
    plan: PositionPlan,
    px: Optional[float],
    rules: CatalystRules,
) -> tuple[List[str], List[str], Dict[str, float], Optional[float]]:
    hard_stop = round(plan.entry_price * (1 - rules.hard_stop_pct), 4)
    trailing_activate = round(plan.entry_price * (1 + rules.trailing_activate_pct), 4)
    thresholds: Dict[str, float] = {
        "hard_stop": hard_stop,
        "trailing_arms_at": trailing_activate,
    }
    triggered: List[str] = []
    watch: List[str] = []
    new_peak = plan.peak_price

    if px is not None:
        # Track peak for trailing stop.
        new_peak = px if (plan.peak_price is None or px > plan.peak_price) else plan.peak_price
        trailing_armed = new_peak is not None and new_peak >= trailing_activate
        trailing_stop_level = (
            round(new_peak * (1 - rules.trailing_stop_pct), 4) if trailing_armed and new_peak else None
        )
        if trailing_stop_level is not None:
            thresholds["trailing_stop"] = trailing_stop_level

        if px <= hard_stop:
            triggered.append(
                f"CATALYST_HARD_STOP: down {rules.hard_stop_pct*100:.0f}%+ from entry — exit now"
            )
        elif trailing_armed and trailing_stop_level is not None and px <= trailing_stop_level:
            triggered.append(
                f"CATALYST_TRAILING_STOP: off peak ${new_peak:.2f} by {rules.trailing_stop_pct*100:.0f}%+ — exit, lock the gain"
            )
        else:
            if not trailing_armed and px >= trailing_activate * 0.97:
                watch.append(f"trailing stop about to arm (arms at ${trailing_activate:.2f})")
            if px <= hard_stop * 1.03:
                watch.append(f"approaching hard stop (${hard_stop:.2f})")

    # Time-based exits.
    if not triggered:
        if plan.catalyst_date:
            days_after = _days_since(plan.catalyst_date)
            if days_after is not None and days_after >= rules.time_stop_days:
                moved_up = px is not None and px >= plan.entry_price * 1.05
                if not moved_up:
                    triggered.append(
                        f"CATALYST_TIME_STOP: {days_after}d past catalyst ({plan.catalyst_date}), "
                        "expected move didn't happen — exit"
                    )
                else:
                    watch.append(
                        f"catalyst passed {days_after}d ago and position is up — trailing stop now governs"
                    )
            elif days_after is not None and days_after >= 0:
                watch.append(f"catalyst date reached ({plan.catalyst_date}) — watch the reaction")
        else:
            held = _days_since(plan.entry_date)
            if held is not None and held >= rules.max_hold_days:
                triggered.append(
                    f"CATALYST_MAX_HOLD: held {held}d with no catalyst date — close the tactical trade"
                )
            elif held is not None and held >= rules.max_hold_days * 0.8:
                watch.append(f"approaching max hold ({rules.max_hold_days}d cap; held {held}d)")
            if not plan.catalyst_description:
                watch.append("catalyst not described — what event is this trade betting on?")

    return triggered, watch, thresholds, new_peak


def compute_trigger_status(
    plans: Dict[str, PositionPlan],
    current_prices: Dict[str, Optional[float]],
    *,
    catalyst_rules: Optional[CatalystRules] = None,
    earnings_dates: Optional[Dict[str, Optional[str]]] = None,
    earnings_window_days: int = 14,
) -> Dict[str, TriggerStatus]:
    rules = catalyst_rules or CatalystRules()
    earnings_dates = earnings_dates or {}
    out: Dict[str, TriggerStatus] = {}
    for ticker, plan in plans.items():
        px = current_prices.get(ticker)
        pct = None
        if px is not None and plan.entry_price > 0:
            pct = (px - plan.entry_price) / plan.entry_price * 100.0

        peak = plan.peak_price
        if plan.is_catalyst:
            triggered, watch, thresholds, peak = _catalyst_triggers(plan, px, rules)
        else:
            triggered, watch, thresholds = _core_triggers(
                plan, px,
                next_earnings_date=earnings_dates.get(ticker),
                earnings_window_days=earnings_window_days,
            )

        out[ticker] = TriggerStatus(
            ticker=ticker,
            strategy=plan.strategy,
            entry_price=plan.entry_price,
            current_price=px,
            pct_from_entry=pct,
            thresholds=thresholds,
            thesis_break_metrics=plan.thesis_break_metrics,
            target_horizon=plan.target_horizon,
            triggered=triggered,
            watch=watch,
            catalyst_date=plan.catalyst_date,
            catalyst_description=plan.catalyst_description,
            peak_price=peak,
            decision_history=plan.decision_history,
        )
    return out


def _earnings_window_days(cfg: Dict[str, Any]) -> int:
    try:
        return int(cfg.get("portfolio_advisor_pre_earnings_trim_window_days", 14))
    except (TypeError, ValueError):
        return 14


def _fetch_prices(
    plans: Dict[str, PositionPlan],
    live: set,
) -> Dict[str, Optional[float]]:
    from tradingagents.portfolio_advisor.price_util import last_close_yfinance

    prices: Dict[str, Optional[float]] = {}
    for ticker in plans:
        if ticker in live:
            try:
                prices[ticker] = last_close_yfinance(ticker)
            except Exception:
                prices[ticker] = None
        else:
            prices[ticker] = None
    return prices


def _fetch_earnings_dates_for_trim(
    plans: Dict[str, PositionPlan],
    current_prices: Dict[str, Optional[float]],
) -> Dict[str, Optional[str]]:
    """Fetch next earnings date only for core positions at/above ~90% of their trim line.

    Limiting the lookups keeps the yfinance load down — a position far from the trim
    threshold does not need an earnings date to be correctly classified.
    """
    from tradingagents.portfolio_advisor.price_util import next_earnings_date_yfinance

    out: Dict[str, Optional[str]] = {}
    for ticker, plan in plans.items():
        if plan.is_catalyst:
            continue
        px = current_prices.get(ticker)
        if px is None or px < plan.trim_at * 0.90:
            continue
        try:
            out[ticker] = next_earnings_date_yfinance(ticker)
        except Exception:
            out[ticker] = None
    return out


def build_trigger_block(
    cfg: Dict[str, Any],
    live_tickers: Any,
) -> str:
    """Build the position plans + rule trigger block for the PM prompt.

    Fetches current prices from yfinance for plans on live tickers, computes
    deterministic trigger status, and persists any updated catalyst peak prices.
    Returns an empty string if no plans exist yet.
    """
    plans = load_position_plans(cfg)
    if not plans:
        return (
            "Position plans: none on file. "
            "To track exit rules, set entry prices via: "
            "`advisor portfolio position-plan set TICKER --entry PRICE --strategy core|catalyst`\n"
        )

    live = {str(t).strip().upper() for t in (live_tickers or [])}
    current_prices = _fetch_prices(plans, live)
    earnings_dates = _fetch_earnings_dates_for_trim(plans, current_prices)
    rules = catalyst_rules_from_cfg(cfg)
    window = _earnings_window_days(cfg)
    statuses = compute_trigger_status(
        plans, current_prices,
        catalyst_rules=rules,
        earnings_dates=earnings_dates,
        earnings_window_days=window,
    )

    # Persist updated catalyst peak prices so trailing stops ratchet across cycles.
    peaks_changed = False
    for ticker, s in statuses.items():
        if s.strategy == "catalyst" and s.peak_price is not None:
            if plans[ticker].peak_price != s.peak_price:
                plans[ticker].peak_price = s.peak_price
                peaks_changed = True
    if peaks_changed:
        try:
            save_position_plans(cfg, plans)
        except Exception as e:
            logger.debug("failed to persist catalyst peaks: %s", e)

    triggered_tickers = [tk for tk, s in statuses.items() if s.triggered]
    watch_tickers = [tk for tk, s in statuses.items() if not s.triggered and s.watch]
    ok_tickers = [tk for tk, s in statuses.items() if s.is_clean()]

    lines = ["=== POSITION RULE STATUS (deterministic — act on TRIGGERED before anything else) ==="]

    if triggered_tickers:
        lines.append("\nTRIGGERED (rule fires now — advisory action required):")
        for tk in sorted(triggered_tickers):
            lines.append(statuses[tk].summary_line())

    if watch_tickers:
        lines.append("\nWATCH (approaching threshold or missing plan data):")
        for tk in sorted(watch_tickers):
            lines.append(statuses[tk].summary_line())

    if ok_tickers:
        lines.append("\nOK:")
        for tk in sorted(ok_tickers):
            lines.append(statuses[tk].summary_line())

    tickers_without_plans = sorted(live - set(plans.keys()))
    if tickers_without_plans:
        lines.append(
            f"\nPositions with no plan on file (entry price / sleeve unknown): {', '.join(tickers_without_plans)}"
        )


def check_crowded_trade_trim(plan: PositionPlan, current_price: float) -> Optional[dict]:
    """Check Trigger 4 conditions for a position. Returns trim dict or None."""
    try:
        from tradingagents.dataflows.llm_consensus import load_llm_consensus_snapshot
        consensus = load_llm_consensus_snapshot()
    except Exception:
        return None
    if consensus is None:
        return None

    top_10 = {t["ticker"] for t in consensus.get("top_20", [])[:10]}
    if plan.ticker not in top_10:
        return None

    gain_pct = (current_price / plan.entry_price - 1) * 100 if plan.entry_price > 0 else 0

    # Check system mode for threshold
    try:
        from tradingagents.portfolio_advisor.state import load_system_mode
        mode = load_system_mode()
    except Exception:
        mode = "normal"

    threshold = 15.0 if mode == "consensus_defensive" else 30.0
    if gain_pct < threshold:
        return None

    try:
        from tradingagents.dataflows.retail_flow_tracker import get_retail_flow_share
        flow = get_retail_flow_share(plan.ticker)
    except Exception:
        flow = None

    if not flow or float(flow.get("share_30d", 0) or 0) <= 0.25:
        return None

    from datetime import date as _date, timedelta as _td
    return {
        "action": "trim_25_pct",
        "reason": f"Trigger 4: +{gain_pct:.0f}% in consensus top 10 with retail flow {float(flow['share_30d'])*100:.0f}%",
        "cool_down_until": (_date.today() + _td(days=30)).isoformat(),
    }

    return "\n".join(lines) + "\n"
