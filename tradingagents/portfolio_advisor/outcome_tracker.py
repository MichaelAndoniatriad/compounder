"""Outcome tracker — weekly job that measures recommendation outcomes.

Phase 2 closing the loop. For each recommendation whose exit horizon has
elapsed, fetch the realised return and classify as good / bad / neutral.
Writes to outcomes.jsonl and updates the recommendation log entry's
was_correct + pnl_impact_est fields.

Also computes per rule performance so the PM can prefer rules that have
worked and discount rules that have not.

Designed to run weekly via cron. Idempotent: re-running does not double
count because already-measured entries are skipped by
recommendation_log.load_due_for_measurement().

Pluggable price fetcher: tests inject a mock. Production uses yfinance.

Compounder 2.0 (§5.2): classification is ALPHA-RELATIVE — return minus QQQ
over the same holding window. A pick that gains +6% while QQQ gains +8% is
"bad" (-2% alpha), not "good". Absolute return is stored alongside but the
headline metric and was_correct are driven by alpha. This is the only metric
that matters for "does the system beat the alternative of buying QQQ?"
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Alpha-relative classification thresholds (alpha = return minus QQQ, decimal)
# e.g. GOOD_THRESHOLD = 0.02 means the pick must beat QQQ by ≥2% to be "good"
GOOD_THRESHOLD = 0.02   # +2% alpha → good
BAD_THRESHOLD = -0.02   # -2% alpha → bad


def _outcomes_path(cfg: Dict[str, Any]) -> Path:
    """Path to outcomes.jsonl under the advisor directory."""
    from tradingagents.portfolio_advisor import state as pa_state
    return pa_state.advisor_dir(cfg) / "outcomes.jsonl"


def _fetch_qqq_return(start_date: str, end_date: str) -> Optional[float]:
    """Return QQQ fractional return over [start_date, end_date]. None on failure."""
    try:
        import yfinance as yf
        hist = yf.Ticker("QQQ").history(start=start_date, end=end_date,
                                         interval="1d", auto_adjust=False)
        if hist is None or len(hist) < 2:
            return None
        return float(hist["Close"].iloc[-1]) / float(hist["Close"].iloc[0]) - 1
    except Exception as e:
        logger.debug("QQQ benchmark fetch failed: %s", e)
        return None


def classify_outcome(alpha_pct: float) -> str:
    """Return one of 'good', 'bad', 'neutral' from an alpha (return minus QQQ) as decimal."""
    if alpha_pct >= GOOD_THRESHOLD:
        return "good"
    if alpha_pct <= BAD_THRESHOLD:
        return "bad"
    return "neutral"


def _default_price_fetcher(ticker: str, start_date: str, end_date: str) -> Optional[Tuple[float, float]]:
    """Production price fetcher using yfinance.

    Returns (start_close, end_close) or None on failure. start_date and
    end_date are ISO date strings. The fetcher returns the closing price
    on or after start_date, and on or before end_date.
    """
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        logger.error("yfinance not installed; cannot fetch outcomes")
        return None

    try:
        hist = yf.Ticker(ticker).history(
            start=start_date,
            end=end_date,
            interval="1d",
            auto_adjust=False,
        )
        if hist is None or len(hist) < 2:
            return None
        start_close = float(hist["Close"].iloc[0])
        end_close = float(hist["Close"].iloc[-1])
        return start_close, end_close
    except Exception as e:  # noqa: BLE001 - upstream failures are varied
        logger.debug("yfinance fetch failed for %s: %s", ticker, e)
        return None


def compute_recommendation_outcomes(
    cfg: Dict[str, Any],
    *,
    price_fetcher: Optional[Callable[[str, str, str], Optional[Tuple[float, float]]]] = None,
    default_horizon_days: int = 30,
) -> Dict[str, Any]:
    """Measure outcomes for every recommendation past its exit horizon.

    For each due recommendation:
    - Fetch realised return using the price fetcher
    - Classify as good / bad / neutral
    - Append an outcome record to outcomes.jsonl
    - Update the recommendation log entry with was_correct + pnl_impact_est

    Returns a summary dict with counts.
    """
    from tradingagents.portfolio_advisor import recommendation_log

    if price_fetcher is None:
        price_fetcher = _default_price_fetcher

    due = recommendation_log.load_due_for_measurement(
        cfg, default_horizon_days=default_horizon_days
    )

    summary = {
        "due": len(due),
        "measured": 0,
        "skipped_no_ticker": 0,
        "skipped_no_price": 0,
        "good": 0,
        "bad": 0,
        "neutral": 0,
    }

    if not due:
        logger.info("outcome tracker: no recommendations due for measurement")
        return summary

    outcomes_file = _outcomes_path(cfg)
    outcomes_file.parent.mkdir(parents=True, exist_ok=True)

    with open(outcomes_file, "a", encoding="utf-8") as out_f:
        for rec in due:
            ticker = rec.get("ticker")
            if not ticker:
                summary["skipped_no_ticker"] += 1
                continue

            ts_str = rec.get("ts", "")
            try:
                start_dt = datetime.fromisoformat(ts_str)
            except (TypeError, ValueError):
                summary["skipped_no_ticker"] += 1
                continue

            horizon = rec.get("exit_horizon_days") or default_horizon_days
            try:
                horizon = int(horizon)
            except (TypeError, ValueError):
                horizon = default_horizon_days

            # Use a small window around start and end so weekends/holidays
            # do not break the fetch. yfinance returns the closest trading
            # day inside the window.
            start_date = start_dt.date().isoformat()
            from datetime import timedelta
            end_date = (start_dt + timedelta(days=horizon + 2)).date().isoformat()

            prices = price_fetcher(ticker, start_date, end_date)
            if prices is None:
                summary["skipped_no_price"] += 1
                logger.debug("outcome tracker: no price for %s in [%s, %s]", ticker, start_date, end_date)
                continue

            start_price, end_price = prices
            if start_price <= 0:
                summary["skipped_no_price"] += 1
                continue

            return_pct = (end_price - start_price) / start_price

            # Direction: a SELL/TRIM call is correct when the stock subsequently
            # UNDERPERFORMS the benchmark — score the CALL, not the stock.
            action = str(rec.get("action") or "").lower()
            is_reduce = any(k in action for k in ("sell", "trim", "exit", "close", "reduce"))

            # §5.2 Compounder 2.0: alpha-relative classification
            qqq_return = _fetch_qqq_return(start_date, end_date)
            if qqq_return is not None:
                alpha = return_pct - qqq_return
                call_alpha = -alpha if is_reduce else alpha
                classification = classify_outcome(call_alpha)
            else:
                # Fallback to absolute if QQQ fetch fails — log it so we notice
                logger.warning("outcome tracker: QQQ benchmark unavailable for %s [%s, %s]; "
                               "falling back to absolute return", ticker, start_date, end_date)
                alpha = None
                call_alpha = None
                signed = -return_pct if is_reduce else return_pct
                # Absolute fallback: ±5% (old thresholds)
                if signed >= 0.05:
                    classification = "good"
                elif signed <= -0.05:
                    classification = "bad"
                else:
                    classification = "neutral"

            outcome_record: Dict[str, Any] = {
                "recommendation_id": rec.get("id"),
                "ticker": ticker,
                "recommendation_date": start_date,
                "exit_date": end_date,
                "horizon_days": horizon,
                "start_price": round(start_price, 4),
                "end_price": round(end_price, 4),
                "realised_return": round(return_pct, 4),
                "qqq_return": round(qqq_return, 4) if qqq_return is not None else None,
                "alpha_vs_qqq": round(alpha, 4) if alpha is not None else None,
                "direction": "reduce" if is_reduce else "increase",
                # call_alpha = direction-adjusted alpha of the CALL (what gets classified)
                "call_alpha": round(call_alpha, 4) if call_alpha is not None else None,
                "classification": classification,
                "measured_at": datetime.now(timezone.utc).isoformat(),
                "rule_ref": rec.get("rule_ref"),
                "consensus_score": rec.get("consensus_score"),
            }
            out_f.write(json.dumps(outcome_record, ensure_ascii=False) + "\n")

            was_correct: Optional[bool]
            if classification == "good":
                was_correct = True
            elif classification == "bad":
                was_correct = False
            else:
                was_correct = None

            # Best effort pnl estimate: per share return if shares set, else
            # the percentage as a stand in.
            shares = rec.get("shares")
            pnl_impact_est = None
            try:
                if shares:
                    pnl_impact_est = round(float(shares) * (end_price - start_price), 2)
                else:
                    pnl_impact_est = round(return_pct * 100, 2)  # percentage proxy
            except (TypeError, ValueError):
                pnl_impact_est = None

            alpha_str = f", call-alpha {call_alpha:+.2%}" if call_alpha is not None else ""
            recommendation_log.update_outcome(
                cfg,
                rec.get("id"),
                was_correct=was_correct,
                pnl_impact_est=pnl_impact_est,
                note=f"horizon {horizon}d return {return_pct:+.2%}{alpha_str} ({'reduce' if is_reduce else 'increase'}) -> {classification}",
            )

            summary["measured"] += 1
            summary[classification] += 1

    logger.info("outcome tracker: %s", json.dumps(summary))
    return summary


def _nav_history_path(cfg: Dict[str, Any]) -> "Path":
    from tradingagents.portfolio_advisor import state as pa_state
    return pa_state.advisor_dir(cfg) / "nav_history.jsonl"


def record_daily_nav(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Append one NAV snapshot per calendar day to nav_history.jsonl.

    Reads equity/cash/positions from the Alpaca executor's client (only when
    executor is enabled). Fetches SPY close via yfinance. If today already has
    a row, or if the executor is disabled, or on any error: silently returns None.

    Returns the row dict written, or None if skipped.
    """
    import json as _json
    from pathlib import Path as _Path
    from datetime import date as _date, datetime as _dt, timezone as _tz

    today_str = _date.today().isoformat()
    nav_path = _nav_history_path(cfg)

    try:
        # Check if today already has a row.
        if nav_path.is_file():
            for line in nav_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing = _json.loads(line)
                    if str(existing.get("date", ""))[:10] == today_str:
                        return None  # already recorded today
                except (ValueError, _json.JSONDecodeError):
                    continue
    except Exception:
        return None

    try:
        from tradingagents.integrations.alpaca import executor as _alpaca
        if not _alpaca.enabled(cfg):
            return None

        client = _alpaca._client()
        account = client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        positions_count = len(client.get_all_positions())
    except Exception:
        return None

    spy_close: Optional[float] = None
    try:
        import yfinance as yf  # type: ignore
        hist = yf.Ticker("SPY").history(period="5d", interval="1d", auto_adjust=False)
        if hist is not None and len(hist) > 0:
            spy_close = float(hist["Close"].iloc[-1])
    except Exception:
        pass

    row: Dict[str, Any] = {
        "date": today_str,
        "ts": _dt.now(_tz.utc).isoformat(),
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "positions_count": positions_count,
        "spy_close": round(spy_close, 4) if spy_close is not None else None,
    }
    try:
        nav_path.parent.mkdir(parents=True, exist_ok=True)
        with open(nav_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return None
    return row


def rule_performance_summary(
    cfg: Dict[str, Any],
    *,
    top_n: int = 5,
) -> Dict[str, Any]:
    """Aggregate outcomes by rule_ref. Top N best, bottom N worst.

    A rule's score is the mean of its outcome classifications mapped to
    {good: 1, neutral: 0, bad: -1}. Ties broken by sample size.
    """
    outcomes_file = _outcomes_path(cfg)
    if not outcomes_file.is_file():
        return {"top": [], "bottom": [], "total_rules": 0, "total_outcomes": 0}

    score_map = {"good": 1, "neutral": 0, "bad": -1}
    per_rule: Dict[str, Dict[str, Any]] = {}

    total_outcomes = 0
    for line in outcomes_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        total_outcomes += 1
        rule_ref = row.get("rule_ref")
        if not rule_ref:
            continue
        rec = per_rule.setdefault(
            rule_ref,
            {"rule_ref": rule_ref, "uses": 0, "good": 0, "bad": 0, "neutral": 0,
             "sum_score": 0, "sum_return": 0.0, "sum_alpha": 0.0},
        )
        rec["uses"] += 1
        cls = row.get("classification") or "neutral"
        rec[cls] = rec.get(cls, 0) + 1
        rec["sum_score"] += score_map.get(cls, 0)
        try:
            rec["sum_return"] += float(row.get("realised_return") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            # Prefer direction-adjusted call_alpha; fall back for pre-fix rows.
            alpha = row.get("call_alpha", row.get("alpha_vs_qqq"))
            if alpha is not None:
                rec["sum_alpha"] += float(alpha)
        except (TypeError, ValueError):
            pass

    rules = []
    for rec in per_rule.values():
        uses = rec["uses"]
        rec["mean_score"] = round(rec["sum_score"] / uses, 3) if uses else 0.0
        rec["mean_return"] = round(rec["sum_return"] / uses, 4) if uses else 0.0
        rec["mean_alpha"] = round(rec.get("sum_alpha", 0.0) / uses, 4) if uses else 0.0
        rec["pct_good"] = round(rec["good"] / uses * 100, 1) if uses else 0.0
        rec["pct_bad"] = round(rec["bad"] / uses * 100, 1) if uses else 0.0
        rec["hit_rate"] = rec["pct_good"] / 100.0  # fraction alpha-positive
        rules.append(rec)

    rules.sort(key=lambda r: (r["mean_score"], r["uses"]), reverse=True)
    top = rules[:top_n]
    bottom = list(reversed(rules[-top_n:])) if len(rules) > top_n else []

    return {
        "top": top,
        "bottom": bottom,
        "total_rules": len(rules),
        "total_outcomes": total_outcomes,
    }


def veto_scorecard(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Compare PM-vetoed candidate outcomes vs executed candidate outcomes.

    Sources:
    - shadow_outcomes.jsonl: rows with source containing "pm_vetoed" or
      status field "pm_vetoed" → the vetoed cohort.
    - alpaca_trades.jsonl: submitted buy rows for catalyst sleeve → executed cohort.
      30d forward return is approximated from outcome_tracker outcomes.jsonl where
      available, or from yfinance for recent trades.

    Returns a dict::

        {
          "vetoed":   {"count": int, "avg_30d_return": float|None, "avg_alpha_vs_spy": float|None},
          "executed": {"count": int, "avg_30d_return": float|None, "avg_alpha_vs_spy": float|None},
          "pm_veto_lift": float|None,  # executed_avg - vetoed_avg (positive = PM adds value by blocking)
          "note": str,
        }

    Callable from the ``measure-outcomes`` CLI.
    """
    from tradingagents.portfolio_advisor import state as pa_state

    advisor_dir = pa_state.advisor_dir(cfg)
    shadow_outcomes_path = advisor_dir / "shadow_outcomes.jsonl"
    ledger_path = advisor_dir / "alpaca_trades.jsonl"

    # --- Vetoed cohort: shadow_outcomes.jsonl rows with pm_vetoed source/status ---
    vetoed_returns: List[float] = []
    vetoed_alphas: List[float] = []
    if shadow_outcomes_path.is_file():
        for line in shadow_outcomes_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            src = str(row.get("source") or "")
            if "pm_vetoed" not in src:
                continue
            raw_ret = row.get("raw_return")
            alpha = row.get("alpha_vs_spy")
            if raw_ret is not None:
                try:
                    vetoed_returns.append(float(raw_ret))
                except (TypeError, ValueError):
                    pass
            if alpha is not None:
                try:
                    vetoed_alphas.append(float(alpha))
                except (TypeError, ValueError):
                    pass

    # --- Executed cohort: outcomes.jsonl rows for catalyst-sleeve buys ---
    outcomes_path = _outcomes_path(cfg)
    executed_returns: List[float] = []
    executed_alphas: List[float] = []

    # Build a set of catalyst-sleeve tickers from the ledger for filtering.
    catalyst_tickers: set = set()
    if ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if (
                row.get("status") == "submitted"
                and str(row.get("sleeve") or "").lower() == "catalyst"
                and str(row.get("action") or "").lower() in ("buy", "add")
            ):
                tk = str(row.get("ticker") or "").upper()
                if tk:
                    catalyst_tickers.add(tk)

    if outcomes_path.is_file() and catalyst_tickers:
        for line in outcomes_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            tk = str(row.get("ticker") or "").upper()
            if tk not in catalyst_tickers:
                continue
            raw_ret = row.get("realised_return")
            # outcomes.jsonl uses alpha_vs_qqq; shadow_outcomes uses alpha_vs_spy.
            alpha = row.get("alpha_vs_qqq") or row.get("alpha_vs_spy")
            if raw_ret is not None:
                try:
                    executed_returns.append(float(raw_ret))
                except (TypeError, ValueError):
                    pass
            if alpha is not None:
                try:
                    executed_alphas.append(float(alpha))
                except (TypeError, ValueError):
                    pass

    def _avg(lst: List[float]) -> Optional[float]:
        return round(sum(lst) / len(lst), 4) if lst else None

    vetoed_avg = _avg(vetoed_returns)
    executed_avg = _avg(executed_returns)
    veto_lift: Optional[float] = None
    if vetoed_avg is not None and executed_avg is not None:
        veto_lift = round(executed_avg - vetoed_avg, 4)

    note_parts = []
    if not vetoed_returns:
        note_parts.append("no scored vetoed candidates yet")
    if not executed_returns:
        note_parts.append("no scored executed catalyst trades yet")
    if veto_lift is not None:
        if veto_lift > 0:
            note_parts.append(
                f"PM vetoes ADD value: executed cohort outperformed vetoed by {veto_lift:+.2%}"
            )
        elif veto_lift < 0:
            note_parts.append(
                f"PM vetoes SUBTRACT value: vetoed cohort outperformed executed by {-veto_lift:+.2%} "
                "(consider further demoting PM to advisory-only for catalyst)"
            )
        else:
            note_parts.append("PM vetoes have zero net impact so far")

    return {
        "vetoed": {
            "count": len(vetoed_returns),
            "avg_30d_return": vetoed_avg,
            "avg_alpha_vs_spy": _avg(vetoed_alphas),
        },
        "executed": {
            "count": len(executed_returns),
            "avg_30d_return": executed_avg,
            "avg_alpha_vs_spy": _avg(executed_alphas),
        },
        "pm_veto_lift": veto_lift,
        "note": "; ".join(note_parts) if note_parts else "scorecard computed",
    }


def format_rule_performance_for_pm_prompt(
    cfg: Dict[str, Any],
    *,
    top_n: int = 5,
) -> str:
    """One block of compact text injectable into the PM prompt context.

    Empty string if no outcomes have been measured yet.
    """
    summary = rule_performance_summary(cfg, top_n=top_n)
    if summary["total_outcomes"] == 0:
        return ""

    lines = [f"[RULE PERFORMANCE — alpha-relative] {summary['total_outcomes']} outcomes across {summary['total_rules']} rules."]
    if summary["top"]:
        lines.append(f"Best alpha ({top_n}):")
        for r in summary["top"]:
            flag = " ⚠ FLAGGED" if r.get("flagged") else ""
            lines.append(
                f"  {r['rule_ref']}: {r['uses']} uses, {r['pct_good']:.0f}% alpha-positive, "
                f"avg alpha {r['mean_alpha']:+.2%}{flag}"
            )
    if summary["bottom"]:
        lines.append(f"Worst alpha ({top_n}):")
        for r in summary["bottom"]:
            flag = " ⚠ FLAGGED" if r.get("flagged") else ""
            lines.append(
                f"  {r['rule_ref']}: {r['uses']} uses, {r['pct_bad']:.0f}% alpha-negative, "
                f"avg alpha {r['mean_alpha']:+.2%}{flag}"
            )
    return "\n".join(lines)
