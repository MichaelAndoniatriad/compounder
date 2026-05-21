"""Run full LangGraph analysis for every current eToro holding (explicit, costly)."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tradingagents.agents.utils.event_log import append_event
from tradingagents.clerk.deep_runner import (
    has_clerk_report_for_trade_date,
    run_deep_research,
    save_deep_report,
)
from tradingagents.dataflows.config import set_config
from tradingagents.portfolio_advisor import etoro_scan, messaging, outcome_sync, state

logger = logging.getLogger(__name__)


def _optional_pm_cycle_after_bootstrap(cfg: Dict[str, Any]) -> None:
    """Run advisor-level PM once bootstrap state is saved (best-effort; never raises)."""
    if not bool(cfg.get("portfolio_advisor_pm_enabled", True)):
        return
    if not bool(cfg.get("portfolio_advisor_pm_cycle_after_bootstrap", True)):
        return
    try:
        from tradingagents.portfolio_advisor.advisor_pm import run_pm_cycle

        run_pm_cycle(cfg, trigger="after_bootstrap")
    except Exception:
        logger.exception("Post-bootstrap PM cycle failed (bootstrap still completed)")


def _book_fingerprint(portfolio_text: str) -> str:
    return hashlib.sha256((portfolio_text or "").encode("utf-8")).hexdigest()


def run_full_portfolio_bootstrap(
    cfg: Dict[str, Any],
    *,
    trade_date: Optional[str] = None,
    delay_seconds: float = 0.0,
    max_positions: Optional[int] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """Sequential deep research for each live ticker. Updates advisor state fingerprint.

    When ``resume`` is True, tickers that already have ``{trade_date}_clerk_triggered.md`` under
    ``results_dir/clerk_deep/<TICKER>/`` are skipped so an interrupted bootstrap can continue.

    Each full-graph run receives the latest on-disk clerk markdown in ``past_context`` (unless
    disabled via ``portfolio_advisor_inject_prior_clerk_report``) so the model can build on
    the prior automated snapshot.

    Emits ``portfolio_book_changed`` event when the eToro text snapshot hash changed
    since the last stored fingerprint. Appends ``full_graph_decision`` is handled
    inside ``TradingAgentsGraph``; this layer logs ``bootstrap_position_complete`` or
    ``bootstrap_position_failed`` per ticker.
    """
    set_config(cfg)
    td = trade_date or date.today().isoformat()
    _payload, portfolio_text, live, rows = etoro_scan.fetch_portfolio_rows()
    live_set = etoro_scan.current_ticker_set(live)
    try:
        outcome_sync.auto_close_outcomes(cfg, live_set, rows=rows)
    except Exception:
        logger.debug("bootstrap outcome_sync skipped", exc_info=True)
    live_list = sorted(live_set)
    if not live_list:
        raise RuntimeError("No tickers in eToro portfolio export.")

    st = state.load_state(cfg)
    new_fp = _book_fingerprint(portfolio_text)
    old_fp = st.get("last_portfolio_text_hash")
    if old_fp and old_fp != new_fp:
        append_event(
            cfg,
            {
                "ticker": "*",
                "event_type": "portfolio_book_changed",
                "key_data": {"old_hash_prefix": str(old_fp)[:12], "new_hash_prefix": str(new_fp)[:12]},
                "outcome": None,
            },
        )
        messaging.send_advisor_message(
            cfg,
            "[TradingAgents] Portfolio book changed since last bootstrap",
            "eToro snapshot fingerprint changed. Review adds/removes/ch sizes before relying on old schedules.",
        )
        prev_names = {str(x).strip().upper() for x in (st.get("last_portfolio_tickers") or []) if x}
        ta = sorted(live_set - prev_names)
        tr = sorted(prev_names - live_set)
        from tradingagents.portfolio_advisor.advisor_pm import optional_pm_cycle_on_portfolio_change

        optional_pm_cycle_on_portfolio_change(
            cfg,
            trigger="portfolio_book_changed",
            old_portfolio_text_hash=str(old_fp),
            new_portfolio_text_hash=new_fp,
            tickers_added=ta,
            tickers_removed=tr,
        )

    analysts = cfg.get("portfolio_advisor_deep_analysts") or ["news", "fundamentals", "market"]
    if not isinstance(analysts, list):
        analysts = ["news", "fundamentals", "market"]

    cap = max_positions if max_positions is not None and max_positions > 0 else len(live_list)
    todo: List[str] = live_list[:cap]
    rd = Path(str(cfg.get("results_dir", "."))).expanduser()
    if resume:
        n0 = len(todo)
        todo = [t for t in todo if not has_clerk_report_for_trade_date(rd, t, td)]
        skipped = n0 - len(todo)
        if skipped:
            logger.info(
                "bootstrap resume: skipping %d ticker(s) with existing %s clerk report",
                skipped,
                td,
            )
    if not todo:
        raise RuntimeError(
            f"No tickers left to bootstrap for {td} (resume skips names that already have "
            f"{td}_clerk_triggered.md under {rd / 'clerk_deep'}). Omit resume to re-run all."
        )

    results: Dict[str, Dict[str, Any]] = {}

    for i, tid in enumerate(todo):
        if i > 0 and delay_seconds > 0:
            time.sleep(float(delay_seconds))
        try:
            try:
                from tradingagents.portfolio_advisor.position_plans import strategy_for_ticker
                strategy = strategy_for_ticker(cfg, tid)
            except Exception:
                strategy = "core"
            final_state, _sig = run_deep_research(tid, td, analysts, cfg, strategy=strategy)
            rd = Path(str(cfg.get("results_dir", ".")))
            save_deep_report(results_dir=rd, ticker=tid, trade_date=td, final_state=final_state)
            decision_text = str(final_state.get("final_trade_decision") or "")
            rating = ""
            try:
                from tradingagents.agents.utils.rating import parse_rating

                rating = parse_rating(decision_text)
            except Exception:
                pass
            results[tid] = {
                "status": "ok",
                "rating": rating or "unknown",
                "excerpt": decision_text[:300],
            }
        except Exception as e:
            logger.exception("bootstrap failed for %s", tid)
            append_event(
                cfg,
                {"ticker": tid, "event_type": "bootstrap_position_failed", "key_data": {"error": str(e)}},
            )
            results[tid] = {
                "status": f"error: {e}",
                "rating": "",
                "excerpt": "",
            }

    rating_counts = Counter(
        v.get("rating", "unknown") for v in results.values() if v.get("status") == "ok"
    )
    ok_n = sum(1 for v in results.values() if v.get("status") == "ok")
    err_n = len(results) - ok_n

    st["last_portfolio_text_hash"] = new_fp
    st["last_bootstrap_iso"] = datetime.now(timezone.utc).isoformat()
    st["last_bootstrap_summary"] = {
        "trade_date": td,
        "tickers": [str(x).strip().upper() for x in todo],
        "ok": ok_n,
        "errors": err_n,
        "ratings": {str(k): int(v) for k, v in rating_counts.items()},
    }
    state.save_state(cfg, st)

    summary_lines: List[str] = [
        f"Portfolio bootstrap finished: {len(todo)} ticker(s) on {td}.",
        "",
        "Scheduled advisor jobs (separate from this bootstrap) run when you execute "
        "`python -m cli.main advisor portfolio run-due` on a timer (cron). "
        "Increase `portfolio_advisor_run_due_max` in config if each invocation should drain more queue.",
        "",
        "--- Rating distribution ---",
    ]
    if rating_counts:
        for r, count in sorted(rating_counts.items()):
            summary_lines.append(f"  {r}: {count}")
    else:
        summary_lines.append("  (none)")
    errors = [k for k, v in results.items() if v.get("status") != "ok"]
    if errors:
        summary_lines.append(f"  Errors: {len(errors)}")

    summary_lines.extend(["", "--- Per ticker ---"])
    for ticker, v in results.items():
        if v.get("status") == "ok":
            summary_lines.append(f"{ticker}: {v.get('rating', 'unknown')}")
            excerpt = v.get("excerpt") or ""
            if excerpt:
                summary_lines.append(f"  {excerpt[:200]}")
        else:
            summary_lines.append(f"{ticker}: ERROR, {v.get('status', '')}")
        summary_lines.append("")

    append_event(
        cfg,
        {
            "ticker": "*",
            "event_type": "portfolio_bootstrap_complete",
            "key_data": {
                "trade_date": td,
                "tickers": list(todo),
                "ok": ok_n,
                "errors": err_n,
            },
            "outcome": None,
        },
    )

    _optional_pm_cycle_after_bootstrap(cfg)

    messaging.send_advisor_message(
        cfg,
        f"[TradingAgents] Portfolio bootstrap complete ({len(todo)} names)",
        "\n".join(summary_lines)[:15000],
        urgent=True,
    )
    return {"trade_date": td, "results": results, "tickers": todo}
