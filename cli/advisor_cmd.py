# cli/advisor_cmd.py — live position advisor CLI

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.advisor.runner import run_advisor_loop, run_advisor_once
from tradingagents.portfolio_advisor import messaging as portfolio_messaging
from tradingagents.portfolio_advisor import service as portfolio_advisor_service

console = Console()
advisor_app = typer.Typer(
    help=(
        "Optional position rule alerts (e.g. drawdown / earnings trims). "
        "For scheduled headline + calendar-driven scans without price triggers, use `tradingagents clerk`."
    ),
    no_args_is_help=True,
)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


@advisor_app.command("run")
def advisor_run(
    positions: Path = typer.Option(
        ...,
        "--positions",
        "-p",
        exists=True,
        dir_okay=False,
        readable=True,
        help="JSON file with a top-level 'positions' array (see cli/static/advisor_positions.example.json).",
    ),
    webhook: Optional[str] = typer.Option(
        None,
        "--webhook",
        "-w",
        help="Override TRADINGAGENTS_ADVISOR_WEBHOOK_URL for this run.",
    ),
    interval: int = typer.Option(
        0,
        "--interval",
        "-i",
        help="If >0, re-run every N seconds (daemon-style loop on this machine).",
    ),
    llm_digest: bool = typer.Option(
        False,
        "--llm-digest",
        help="Append a one-shot quick-model narrative using your configured LLM provider.",
    ),
    no_dedupe: bool = typer.Option(
        False,
        "--no-dedupe",
        help="Send a webhook on every tick even if the same alert was already sent today.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
):
    """Evaluate positions, print a digest, and POST alerts to a webhook when configured."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()

    if interval and interval > 0:
        console.print(
            f"[cyan]Advisor loop[/cyan] every {interval}s — Ctrl+C to stop. "
            f"Positions: {positions.resolve()}"
        )
        run_advisor_loop(
            positions,
            interval_seconds=interval,
            webhook_url=webhook,
            use_dedupe=not no_dedupe,
            with_llm_digest=llm_digest,
            config=cfg,
        )
        return

    text, alerts = run_advisor_once(
        positions,
        webhook_url=webhook,
        use_dedupe=not no_dedupe,
        with_llm_digest=llm_digest,
        config=cfg,
    )
    console.print(text)
    if alerts:
        console.print(f"\n[bold]{len(alerts)} alert(s)[/bold] evaluated this tick.")
    else:
        console.print("\n[dim]No rule-based alerts this tick.[/dim]")


@advisor_app.command("example-path")
def advisor_example_path():
    """Print the path to the bundled example positions JSON."""
    here = Path(__file__).resolve().parent / "static" / "advisor_positions.example.json"
    console.print(str(here))


portfolio_app = typer.Typer(
    help=(
        "Autonomous eToro portfolio advisor: init/replan builds an LLM schedule; weekly is a "
        "light portfolio check; run-due executes scheduled deep research; catalogue exports jobs/timestamps. "
        "All advisory only."
    ),
    no_args_is_help=True,
)
advisor_app.add_typer(portfolio_app, name="portfolio")


@portfolio_app.command("init")
def portfolio_advisor_init(
    force: bool = typer.Option(
        False,
        "--force",
        help="Reset advisor state and rebuild the schedule from scratch.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """First deployment: full eToro scan + LLM schedule + notifications."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        portfolio_advisor_service.run_init(cfg, force=force)
        console.print("[green]Portfolio advisor init complete.[/green]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("weekly")
def portfolio_advisor_weekly(
    force: bool = typer.Option(
        False,
        "--force",
        help="Run even if today is not the configured weekday (for testing).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Light weekly check: positions vs snapshot, overdue jobs, orphan job cleanup (not a full replan)."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        outcome = portfolio_advisor_service.run_weekly(cfg, ignore_weekday=force)
        if outcome == "skipped_weekday":
            console.print(
                "[dim]Weekly check skipped (not your configured weekday; "
                "use --force to run anyway).[/dim]"
            )
        else:
            console.print("[green]Weekly portfolio check complete.[/green]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("replan")
def portfolio_advisor_replan(
    force: bool = typer.Option(
        False,
        "--force",
        help="Run even if today is not the configured weekday (for testing).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Full LLM reschedule (replaces pending jobs). Same weekday gate as weekly by default."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        outcome = portfolio_advisor_service.run_replan(cfg, ignore_weekday=force)
        if outcome == "skipped_weekday":
            console.print(
                "[dim]Replan skipped (weekday gate). Use --force to run anyway.[/dim]"
            )
        else:
            console.print("[green]Portfolio advisor replan complete.[/green]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("alert")
def portfolio_advisor_alert(
    subject: str = typer.Option(..., "--subject", "-s", help="Email / webhook subject line."),
    body: str = typer.Option(..., "--body", "-b", help="Message body (advisory text)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Send an ad-hoc advisor message via the same webhook + SMTP as other portfolio notices."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    # Manual `portfolio alert` is user-initiated; always deliver regardless of window.
    ok = portfolio_messaging.send_advisor_message(cfg, subject, body, urgent=True)
    if ok:
        console.print("[green]Message sent (at least one channel).[/green]")
    else:
        console.print("[yellow]No channel accepted the message; check webhook + SMTP env.[/yellow]")


@portfolio_app.command("run-due")
def portfolio_advisor_run_due(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Execute pending scheduled deep runs (cron every 10–30 minutes)."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        n = portfolio_advisor_service.run_due_jobs(cfg)
        console.print(f"[cyan]Advisor run-due:[/cyan] processed {n} job(s).")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("morning-digest")
def portfolio_morning_digest(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Send morning digest of open action items via ntfy."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor.action_log import run_morning_digest
        sent = run_morning_digest(cfg)
        if sent:
            console.print("[green]Morning digest sent.[/green]")
        else:
            console.print("[yellow]No open action items — nothing sent.[/yellow]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("evening-digest")
def portfolio_evening_digest(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Send evening digest of open action items via ntfy."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor.action_log import run_evening_digest
        sent = run_evening_digest(cfg)
        if sent:
            console.print("[green]Evening digest sent.[/green]")
        else:
            console.print("[yellow]No open action items — nothing sent.[/yellow]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("watchdog")
def portfolio_advisor_watchdog(
    force: bool = typer.Option(
        False,
        "--force",
        help="Run even outside the default 13:30 to 20:00 UTC weekday window (for tests).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Price only rule sweep: no LLM. Use a 5 to 10 minute cron during US equity hours."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        n = portfolio_advisor_service.run_watchdog(cfg, ignore_market_hours=force)
        console.print(f"[cyan]Advisor watchdog:[/cyan] critical alert count {n}.")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("bootstrap")
def portfolio_advisor_bootstrap(
    delay: float = typer.Option(
        45.0,
        "--delay",
        "-d",
        help="Seconds to sleep between tickers (rate limits / provider throttling).",
    ),
    max_positions: Optional[int] = typer.Option(
        None,
        "--max",
        "-m",
        help="Optional cap on how many holdings to analyze (default: all).",
    ),
    trade_date: Optional[str] = typer.Option(
        None,
        "--date",
        help="YYYY-MM-DD as-of for saved reports (default: local today's date).",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Skip tickers that already have clerk_deep/<SYM>/<date>_clerk_triggered.md for that date.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run full LangGraph analysis for each live eToro holding (explicit, costly)."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        out = portfolio_advisor_service.run_bootstrap(
            cfg,
            delay_seconds=delay,
            max_positions=max_positions,
            trade_date=trade_date,
            resume=resume,
        )
        console.print(f"[green]Bootstrap finished:[/green] {out.get('results')}")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("memory-review")
def portfolio_advisor_memory_review(
    days: int = typer.Option(120, "--days", help="Event log lookback window."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Summarize JSONL event log + optional reasoning narrative (emailed)."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        text = portfolio_advisor_service.run_memory_review(cfg, lookback_days=days)
        console.print(text[:4000])
        console.print("[green]Memory review sent.[/green]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("post-earnings")
def portfolio_advisor_post_earnings(
    ticker: str = typer.Option(..., "--ticker", "-t", help="Symbol in your current eToro export."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """One-shot post-earnings verdict email (reasoning model; advisory only)."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        text = portfolio_advisor_service.run_post_earnings(cfg, ticker)
        console.print("[green]Post-earnings verdict sent.[/green]")
        console.print(text[:2000])
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("status")
def portfolio_advisor_status():
    """Show persisted advisor jobs and last scan timestamps."""
    cfg = DEFAULT_CONFIG.copy()
    console.print(portfolio_advisor_service.status_text(cfg))


@portfolio_app.command("catalogue")
def portfolio_advisor_catalogue(
    out: Optional[str] = typer.Option(
        None,
        "--out",
        "-o",
        help="Markdown output path (default: ~/.tradingagents/portfolio_advisor/advisor_jobs_catalogue.md).",
    ),
    json: bool = typer.Option(
        False,
        "--json",
        help="Also write a machine-readable JSON snapshot (same stem as the Markdown file).",
    ),
):
    """Write a Markdown catalogue of advisor timestamps and all jobs (plus optional JSON)."""
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor.catalogue import write_advisor_catalogue

        paths = write_advisor_catalogue(
            cfg,
            markdown_path=Path(out).expanduser() if out else None,
            write_json=json,
        )
        console.print("[green]Catalogue written:[/green]")
        for k, v in paths.items():
            console.print(f"  {k}: {v}")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("pm-cycle")
def portfolio_advisor_pm_cycle(
    trigger: str = typer.Option(
        "manual",
        "--trigger",
        "-t",
        help="Label stored in logs (e.g. manual, after_bootstrap, cron).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run one advisor-level portfolio manager cycle (structured memo + stances + tasks + memory)."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor.advisor_pm import run_pm_cycle

        out = run_pm_cycle(cfg, trigger=trigger)
        console.print("[green]PM cycle complete.[/green]")
        console.print(out.executive_summary[:2000])
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("action-check")
def portfolio_advisor_action_check(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Proactive action pass (3x/day cron). Refreshes price latches, runs one PM
    cycle, and messages the human ONLY when there is an action to take."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor.advisor_pm import run_action_check

        out = run_action_check(cfg)
        pushed = bool((out.push_note or "").strip())
        console.print(f"[cyan]Advisor action-check:[/cyan] action message sent={pushed}.")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@portfolio_app.command("cost-report")
def portfolio_cost_report(
    days: int = typer.Option(7, "--days", help="Lookback window in days (default 7)."),
):
    """Show LLM spend: daily totals, top models, top individual calls."""
    from rich.table import Table
    from rich import box
    from tradingagents.llm_clients.cost_report import (
        load_records,
        daily_spend,
        model_spend,
        top_calls,
        null_cost_count,
        total_spend,
    )

    records = load_records(days=days)
    if not records:
        console.print(f"[yellow]No cost records found for the last {days} day(s).[/yellow]")
        return

    null_count = null_cost_count(records)
    grand_total = total_spend(records)

    console.print(f"\n[bold]LLM Cost Report[/bold] — last {days} day(s)\n")
    if null_count:
        console.print(
            f"[yellow]Warning:[/yellow] {null_count} call(s) have null cost_usd "
            "(model not in pricing table).\n"
        )

    # Total and per-day breakdown
    console.print(f"[bold]Total spend:[/bold] ${grand_total:.4f}")
    day_totals = daily_spend(records)
    if day_totals:
        day_table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
        day_table.add_column("Date", style="cyan")
        day_table.add_column("Spend (USD)", justify="right")
        for day, cost in day_totals.items():
            day_table.add_row(day, f"${cost:.4f}")
        console.print(day_table)

    # Top 5 models
    console.print("\n[bold]Top 5 models by cost:[/bold]")
    model_table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
    model_table.add_column("Model", style="cyan")
    model_table.add_column("Calls", justify="right")
    model_table.add_column("Total (USD)", justify="right")
    for model, count, cost in model_spend(records)[:5]:
        model_table.add_row(model, str(count), f"${cost:.4f}")
    console.print(model_table)

    # Top 5 individual calls
    console.print("\n[bold]Top 5 most expensive calls:[/bold]")
    call_table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
    call_table.add_column("Timestamp", style="cyan")
    call_table.add_column("Model")
    call_table.add_column("Tokens", justify="right")
    call_table.add_column("Cost (USD)", justify="right")
    for rec in top_calls(records, 5):
        cost_str = f"${rec['cost_usd']:.4f}" if rec.get("cost_usd") is not None else "null"
        call_table.add_row(
            str(rec.get("ts", ""))[:19],
            str(rec.get("model", "")),
            str(rec.get("total_tokens", 0)),
            cost_str,
        )
    console.print(call_table)
    console.print(f"\n[dim]Calls with unknown pricing: {null_count}[/dim]")


@portfolio_app.command("heartbeat")
def portfolio_advisor_heartbeat():
    """Send a system-alive Telegram ping: positions tracked, cron/watchdog status, last alert age."""
    from datetime import datetime, timezone
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor import etoro_scan, messaging, state as pa_state
        from tradingagents.portfolio_advisor.position_plans import load_position_plans
        from tradingagents.portfolio_advisor.messaging import load_recent_messages

        _payload, _text, _tickers, rows = etoro_scan.fetch_portfolio_rows()
        n_positions = len({str(r.get("symbolFull") or "").strip().upper() for r in rows if r.get("symbolFull")})

        plans = load_position_plans(cfg)
        n_plans = len(plans)

        recent = load_recent_messages(cfg, limit=50)
        last_alert = None
        for m in recent:
            if not m.get("suppressed_duplicate") and m.get("delivered"):
                last_alert = m.get("timestamp")
                break
        if last_alert:
            try:
                ts = datetime.fromisoformat(last_alert.replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                last_alert_str = f"{age_h:.0f}h ago"
            except Exception:
                last_alert_str = last_alert[:16]
        else:
            last_alert_str = "none on record"

        st = pa_state.load_state(cfg)
        pending = len([j for j in (st.get("jobs") or []) if j.get("status") == "pending"])

        body = (
            f"Positions tracked: {n_positions}\n"
            f"Position plans: {n_plans}\n"
            f"Pending jobs: {pending}\n"
            f"Last delivered alert: {last_alert_str}\n"
            f"eToro fetch: OK"
        )
        ok = messaging.send_advisor_message(cfg, "Advisor: system alive", body, urgent=True)
        if ok:
            console.print("[green]Heartbeat sent.[/green]")
        else:
            console.print("[yellow]Heartbeat logged (no Telegram configured or quiet hours).[/yellow]")
        console.print(body)
    except Exception as e:
        console.print(f"[red]Heartbeat failed: {e}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Position plan commands
# ---------------------------------------------------------------------------

position_plan_app = typer.Typer(
    help="Per-position exit plans: entry price, thesis-break metrics, rule trigger tracking.",
    no_args_is_help=True,
)
portfolio_app.add_typer(position_plan_app, name="position-plan")


@position_plan_app.command("set")
def position_plan_set(
    ticker: str = typer.Argument(..., help="Ticker symbol, e.g. NVDA"),
    entry: float = typer.Option(..., "--entry", "-e", help="Entry price (your average cost basis)."),
    strategy: str = typer.Option(
        "core",
        "--strategy",
        "-s",
        help="Sleeve: 'core' (long-term/growth) or 'catalyst' (short-term event trade).",
    ),
    entry_date: str = typer.Option("", "--date", "-d", help="Entry date YYYY-MM-DD."),
    thesis: str = typer.Option(
        "",
        "--thesis",
        "-t",
        help="(core) Thesis-break metrics, semicolon-separated, e.g. 'rev growth <20%;NRR drops below 110%'",
    ),
    catalyst_date: str = typer.Option("", "--catalyst-date", help="(catalyst) Expected event date YYYY-MM-DD."),
    catalyst_desc: str = typer.Option("", "--catalyst-desc", help="(catalyst) What event is this trade betting on?"),
    horizon: str = typer.Option("3-5 years", "--horizon", "-h", help="(core) Target hold horizon."),
    notes: str = typer.Option("", "--notes", "-n", help="Free-form notes."),
):
    """Write or update the exit plan for a position (core or catalyst sleeve)."""
    from tradingagents.portfolio_advisor.position_plans import (
        PositionPlan,
        upsert_position_plan,
        catalyst_rules_from_cfg,
    )

    cfg = DEFAULT_CONFIG.copy()
    strategy = strategy.strip().lower()
    if strategy not in ("core", "catalyst"):
        console.print(f"[red]Invalid strategy '{strategy}'. Use 'core' or 'catalyst'.[/red]")
        raise typer.Exit(1)
    metrics = [m.strip() for m in thesis.split(";") if m.strip()] if thesis else []
    plan = PositionPlan(
        ticker=ticker.strip().upper(),
        entry_price=entry,
        strategy=strategy,
        entry_date=entry_date,
        thesis_break_metrics=metrics,
        target_horizon=horizon,
        notes=notes,
        catalyst_date=catalyst_date,
        catalyst_description=catalyst_desc,
    )
    upsert_position_plan(cfg, plan)
    console.print(f"[green]Plan saved for {plan.ticker} ({strategy}):[/green]")
    if strategy == "core":
        console.print(f"  entry=${plan.entry_price:.4f}, trim_at=${plan.trim_at:.2f}, "
                      f"double_at=${plan.double_at:.2f}, soft_stop=${plan.soft_stop:.2f}, "
                      f"hard_stop=${plan.hard_stop_core:.2f}")
        if metrics:
            console.print(f"  thesis_break_metrics: {'; '.join(metrics)}")
        else:
            console.print("[yellow]  Warning: no thesis-break metrics set — add them before the next earnings print[/yellow]")
    else:
        r = catalyst_rules_from_cfg(cfg)
        hard = plan.entry_price * (1 - r.hard_stop_pct)
        arm = plan.entry_price * (1 + r.trailing_activate_pct)
        console.print(f"  entry=${plan.entry_price:.4f}, hard_stop=${hard:.2f} (-{r.hard_stop_pct*100:.0f}%), "
                      f"trailing arms at ${arm:.2f} (+{r.trailing_activate_pct*100:.0f}%, then -{r.trailing_stop_pct*100:.0f}% from peak)")
        if catalyst_date:
            console.print(f"  catalyst: {catalyst_desc or '(undescribed)'} on {catalyst_date} "
                          f"(time-stop {r.time_stop_days}d after if no move)")
        else:
            console.print(f"[yellow]  No catalyst date — max hold {r.max_hold_days}d. Set --catalyst-date for a tighter time-stop.[/yellow]")
        if not catalyst_desc:
            console.print("[yellow]  Warning: catalyst not described — what event is this trade betting on?[/yellow]")


@position_plan_app.command("list")
def position_plan_list():
    """Show all position plans and their current trigger status (fetches live prices)."""
    from tradingagents.portfolio_advisor.position_plans import (
        load_position_plans,
        compute_trigger_status,
        catalyst_rules_from_cfg,
        _fetch_prices,
        _fetch_earnings_dates_for_trim,
        _earnings_window_days,
    )

    cfg = DEFAULT_CONFIG.copy()
    plans = load_position_plans(cfg)
    if not plans:
        console.print("[yellow]No position plans on file. Use `position-plan set` to add one.[/yellow]")
        return

    live = set(plans.keys())  # treat all plans as live for a manual listing
    current_prices = _fetch_prices(plans, live)
    earnings_dates = _fetch_earnings_dates_for_trim(plans, current_prices)
    statuses = compute_trigger_status(
        plans, current_prices,
        catalyst_rules=catalyst_rules_from_cfg(cfg),
        earnings_dates=earnings_dates,
        earnings_window_days=_earnings_window_days(cfg),
    )

    from rich.text import Text

    for ticker in sorted(statuses):
        s = statuses[ticker]
        if s.triggered:
            style = "bold red"
        elif s.watch:
            style = "yellow"
        else:
            style = "green"
        console.print(Text("\n" + s.summary_line(), style=style))


@position_plan_app.command("backfill")
def position_plan_backfill(
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace existing plans too (default: skip them)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Create core plans for live holdings using their eToro weighted-avg cost basis.

    Defaults every plan to the 'core' sleeve. After this, reclassify catalyst names with
    `position-plan set TICKER --strategy catalyst ...` and add thesis-break metrics to core names.
    """
    _configure_logging(verbose)
    from tradingagents.portfolio_advisor.position_plans import backfill_plans_from_portfolio
    from tradingagents.portfolio_advisor import etoro_scan

    cfg = DEFAULT_CONFIG.copy()
    try:
        _, _, _, rows = etoro_scan.fetch_portfolio_rows()
    except Exception as e:
        console.print(f"[red]Could not fetch eToro portfolio: {e}[/red]")
        raise typer.Exit(1) from e
    result = backfill_plans_from_portfolio(cfg, rows, overwrite=overwrite)
    if result["created"]:
        console.print(f"[green]Created plans (core):[/green] {', '.join(result['created'])}")
    if result["skipped_existing"]:
        console.print(f"[dim]Kept existing plans: {', '.join(result['skipped_existing'])}[/dim]")
    if result["skipped_no_data"]:
        console.print(f"[yellow]Skipped (no usable cost basis): {', '.join(result['skipped_no_data'])}[/yellow]")
    if not any(result.values()):
        console.print("[yellow]No holdings found to backfill.[/yellow]")
    console.print("\nNext: reclassify any catalyst trades and add thesis-break metrics, e.g.")
    console.print("  advisor portfolio position-plan set DKNG --entry <p> --strategy catalyst --catalyst-date YYYY-MM-DD --catalyst-desc '...'")


@position_plan_app.command("classify")
def position_plan_classify(
    only_missing: bool = typer.Option(
        False, "--only-missing", help="Only classify plans that have no thesis-break metrics yet."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Let the system decide each holding's sleeve + thesis-break metrics + keep/sell.

    Reads each position's research (memory, events, latest decision) and price action, applies the
    investor policy, and writes the classification back into the plan. Sell recommendations are
    surfaced, never auto-executed.
    """
    _configure_logging(verbose)
    from tradingagents.portfolio_advisor.position_classifier import classify_all_holdings

    cfg = DEFAULT_CONFIG.copy()
    result = classify_all_holdings(cfg, only_missing=only_missing)
    if not result.get("classified"):
        console.print(f"[yellow]Nothing classified:[/yellow] {result.get('reason', 'no holdings')}")
        return
    for c in result["classified"]:
        tag = "bold red" if c["recommendation"] == "sell" else ("yellow" if c["strategy"] == "catalyst" else "green")
        from rich.text import Text
        header = f"{c['ticker']} -> {c['strategy'].upper()}  (rec: {c['recommendation']})"
        console.print(Text("\n" + header, style=tag))
        for m in c["thesis_break_metrics"]:
            console.print(Text(f"    break: {m}"), markup=False)
        if c["rationale"]:
            console.print(Text(f"    {c['rationale']}", style="dim"))
    if result["sell_flags"]:
        console.print(f"\n[bold red]SELL recommended:[/bold red] {', '.join(result['sell_flags'])}")
    if result["skipped"]:
        console.print(f"[dim]Skipped: {', '.join(result['skipped'])}[/dim]")


@position_plan_app.command("remove")
def position_plan_remove(
    ticker: str = typer.Argument(..., help="Ticker symbol to remove."),
):
    """Delete a position plan."""
    from tradingagents.portfolio_advisor.position_plans import remove_position_plan

    cfg = DEFAULT_CONFIG.copy()
    removed = remove_position_plan(cfg, ticker.strip().upper())
    if removed:
        console.print(f"[green]Plan removed for {ticker.upper()}.[/green]")
    else:
        console.print(f"[yellow]No plan found for {ticker.upper()}.[/yellow]")


# ---------------------------------------------------------------------------
# Watchlist commands
# ---------------------------------------------------------------------------

watchlist_app = typer.Typer(
    help="Candidate watchlist: tickers you want scanned and gated before entering the portfolio.",
    no_args_is_help=True,
)
portfolio_app.add_typer(watchlist_app, name="watchlist")


@watchlist_app.command("add")
def watchlist_add(
    ticker: str = typer.Argument(..., help="Ticker to add to the watchlist."),
    thesis: str = typer.Option("", "--thesis", "-t", help="Short investment thesis."),
    strategy: str = typer.Option(
        "core",
        "--strategy",
        "-s",
        help="Sleeve: 'core' (long-term/growth) or 'catalyst' (short-term event trade).",
    ),
    catalyst_date: str = typer.Option("", "--catalyst-date", help="(catalyst) Expected event date YYYY-MM-DD."),
):
    """Add a ticker to the candidate watchlist."""
    from tradingagents.portfolio_advisor.watchlist import add_to_watchlist

    cfg = DEFAULT_CONFIG.copy()
    strategy = strategy.strip().lower()
    if strategy not in ("core", "catalyst"):
        console.print(f"[red]Invalid strategy '{strategy}'. Use 'core' or 'catalyst'.[/red]")
        raise typer.Exit(1)
    added = add_to_watchlist(cfg, ticker, thesis=thesis, strategy=strategy, catalyst_date=catalyst_date)
    if added:
        console.print(f"[green]{ticker.upper()} added to watchlist ({strategy}).[/green]")
    else:
        console.print(f"[yellow]{ticker.upper()} is already on the watchlist.[/yellow]")


@watchlist_app.command("remove")
def watchlist_remove(
    ticker: str = typer.Argument(..., help="Ticker to remove."),
):
    """Remove a ticker from the candidate watchlist."""
    from tradingagents.portfolio_advisor.watchlist import remove_from_watchlist

    cfg = DEFAULT_CONFIG.copy()
    removed = remove_from_watchlist(cfg, ticker)
    if removed:
        console.print(f"[green]{ticker.upper()} removed.[/green]")
    else:
        console.print(f"[yellow]{ticker.upper()} not found on watchlist.[/yellow]")


@watchlist_app.command("list")
def watchlist_list():
    """Show all tickers on the watchlist."""
    from tradingagents.portfolio_advisor.watchlist import load_watchlist

    cfg = DEFAULT_CONFIG.copy()
    entries = load_watchlist(cfg)
    if not entries:
        console.print("[yellow]Watchlist is empty. Use `watchlist add TICKER` to add ideas.[/yellow]")
        return
    for e in entries:
        if isinstance(e, str):
            console.print(f"  {e.upper()} (core): (no thesis)")
        else:
            ticker = str(e.get("ticker", "")).upper()
            thesis = str(e.get("thesis", ""))
            strategy = str(e.get("strategy", "core")).lower()
            cat = str(e.get("catalyst_date", ""))
            cat_s = f" [catalyst {cat}]" if cat else ""
            console.print(f"  {ticker} ({strategy}){cat_s}: {thesis or '(no thesis)'}", markup=False)


@watchlist_app.command("scan")
def watchlist_scan(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    no_jobs: bool = typer.Option(False, "--no-jobs", help="Gate entries but don't queue research jobs."),
):
    """Run all watchlist entries through the candidate gates. Queues research for those that pass."""
    _configure_logging(verbose)
    from tradingagents.portfolio_advisor.watchlist import scan_watchlist
    from tradingagents.portfolio_advisor import etoro_scan

    cfg = DEFAULT_CONFIG.copy()
    try:
        _, _, tickers, _ = etoro_scan.fetch_portfolio_rows()
        live = etoro_scan.current_ticker_set(tickers)
    except Exception:
        live = set()
    result = scan_watchlist(cfg, live_tickers=live, queue_jobs=not no_jobs)
    console.print(f"[green]Scan complete:[/green] {result['scanned']} entries evaluated, "
                  f"{result['queued']} research jobs queued")
    if result["promoted"]:
        console.print(f"  Passed gates / queued for research: {', '.join(result['promoted'])}")
    if result["watch"]:
        console.print(f"  Watch (weak thesis or missing data): {', '.join(result['watch'])}")
    if result["rejected"]:
        console.print(f"  Rejected (hard gate failure): {', '.join(result['rejected'])}")


@watchlist_app.command("scan-deep")
def watchlist_scan_deep(
    no_generate: bool = typer.Option(
        False, "--no-generate", help="Skip idea discovery; only gate/pick from the existing watchlist."
    ),
    generate_n: int = typer.Option(8, "--generate-n", help="How many new ideas to discover."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Biweekly: discover new growth ideas, then send the single best to full_graph.

    Generates fresh candidates that fit your mandate, adds them to the watchlist, gates everything,
    picks one name, and queues a full_graph job. The next run-due tick runs the deep research; the
    candidate pipeline then runs the PM comparison to decide. This is what the biweekly cron calls.
    """
    _configure_logging(verbose)
    from tradingagents.portfolio_advisor.watchlist import scan_and_queue_one_deep
    from tradingagents.portfolio_advisor import etoro_scan

    cfg = DEFAULT_CONFIG.copy()
    try:
        _, _, tickers, _ = etoro_scan.fetch_portfolio_rows()
        live = etoro_scan.current_ticker_set(tickers)
    except Exception:
        live = set()
    result = scan_and_queue_one_deep(cfg, live_tickers=live, generate=not no_generate, generate_n=generate_n)
    gen = result.get("generated") or {}
    if gen.get("added"):
        console.print(f"[cyan]Discovered & added to watchlist:[/cyan] {', '.join(gen['added'])}")
    picked = result.get("picked")
    if not picked:
        console.print(f"[yellow]Nothing queued:[/yellow] {result.get('reason', 'no eligible candidate')}")
        if result.get("rejected"):
            console.print(f"  Rejected by gates: {', '.join(result['rejected'])}")
        return
    console.print(
        f"[green]Queued full_graph deep dive:[/green] {picked} ({result.get('strategy')}, "
        f"priority {result.get('priority')}, status {result.get('status')})"
    )
    console.print("  It will run on the next run-due tick (every 15 min) and the PM will decide on it.")


@watchlist_app.command("research")
def watchlist_research(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Weekly research funnel (Stage 1): discover candidates from market news, gate them, queue screens.

    News-driven discovery (with memory-generator top-up if news is thin) -> hard gates ->
    cheap single_model thesis screens. Names that clear the screen auto-promote to full_graph
    (capped per week); the PM then decides. This is what the weekly cron calls.
    """
    _configure_logging(verbose)
    from tradingagents.portfolio_advisor.news_researcher import run_weekly_discovery
    from tradingagents.portfolio_advisor import etoro_scan

    cfg = DEFAULT_CONFIG.copy()
    try:
        _, _, tickers, _ = etoro_scan.fetch_portfolio_rows()
        live = etoro_scan.current_ticker_set(tickers)
    except Exception:
        live = set()
    result = run_weekly_discovery(cfg, live_tickers=live)
    if result.get("discovered"):
        console.print(f"[cyan]Discovered from news:[/cyan] {', '.join(result['discovered'])}")
    if result.get("topped_up"):
        console.print(f"[dim]Topped up from generator: {', '.join(result['topped_up'])}[/dim]")
    if not result.get("added") and not result.get("screened"):
        console.print(f"[yellow]Nothing queued:[/yellow] {result.get('reason', 'no candidates passed the rules')}")
        return
    console.print(f"[green]Queued {result.get('screened', 0)} single_model screen(s).[/green]")
    if result.get("rejected"):
        console.print(f"  Rejected by gates: {', '.join(result['rejected'])}")
    console.print(
        f"  Screens run on the next run-due tick; winners promote to full_graph "
        f"(budget left this week: {result.get('full_graph_budget_left', '?')}). The PM then decides."
    )


@watchlist_app.command("generate")
def watchlist_generate(
    n: int = typer.Option(8, "--n", help="How many ideas to discover."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Discover new growth-stock ideas that fit your mandate and add them to the watchlist (no research)."""
    _configure_logging(verbose)
    from tradingagents.portfolio_advisor.idea_generator import discover_ideas_to_watchlist
    from tradingagents.portfolio_advisor import etoro_scan

    cfg = DEFAULT_CONFIG.copy()
    try:
        _, _, tickers, _ = etoro_scan.fetch_portfolio_rows()
        live = etoro_scan.current_ticker_set(tickers)
    except Exception:
        live = set()
    result = discover_ideas_to_watchlist(cfg, live_tickers=live, n=n)
    if not result.get("ideas"):
        console.print("[yellow]No ideas generated.[/yellow]")
        return
    from rich.text import Text
    for idea in result["ideas"]:
        added = "added" if idea["ticker"] in result["added"] else "dup/skip"
        console.print(Text(f"\n{idea['ticker']} ({idea['strategy']}) [{idea['theme']}] — {added}"), markup=False)
        console.print(Text(f"    {idea['thesis']}", style="dim"))
    console.print(f"\n[green]Added {len(result['added'])} new name(s) to the watchlist.[/green]")


proposals_app = typer.Typer(
    help="PM trade proposals (dry-run). Review, approve, reject — nothing is executed automatically.",
    no_args_is_help=True,
)
portfolio_app.add_typer(proposals_app, name="proposals")


@proposals_app.command("list")
def proposals_list(
    all_statuses: bool = typer.Option(
        False, "--all", help="Show every status (default: only pending proposals)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """List proposed trades from the PM. Default shows only pending (status=proposed)."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    from tradingagents.portfolio_advisor import proposals as ptp
    rows = ptp.load_all(cfg) if all_statuses else ptp.list_pending(cfg)
    if not rows:
        console.print("[yellow]No proposals." + (" (use --all to see archived statuses)" if not all_statuses else "") + "[/yellow]")
        return
    for r in rows:
        console.print(ptp.format_one_line(r))


@proposals_app.command("approve")
def proposals_approve(
    id_: str = typer.Argument(..., metavar="ID", help="Proposal short id (first 8+ hex chars)."),
    note: str = typer.Option("", "--note", help="Optional note recorded with the approval."),
):
    """Mark a proposal as approved. Does NOT execute — sets state for a future executor."""
    cfg = DEFAULT_CONFIG.copy()
    from tradingagents.portfolio_advisor import proposals as ptp
    # Allow partial id prefixes (first 8 chars).
    target = id_.strip().lower()
    rows = ptp.load_all(cfg)
    matches = [r for r in rows if (r.get("id") or "").startswith(target)]
    if not matches:
        console.print(f"[red]No proposal matching id prefix {target!r}.[/red]")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print(f"[red]Ambiguous id {target!r} ({len(matches)} matches). Use more chars.[/red]")
        raise typer.Exit(1)
    ok, row = ptp.approve(cfg, matches[0]["id"], note=note)
    if ok and row is not None:
        console.print(f"[green]Approved:[/green] {ptp.format_one_line(row)}")
    else:
        console.print("[red]Approval failed.[/red]"); raise typer.Exit(1)


@proposals_app.command("reject")
def proposals_reject(
    id_: str = typer.Argument(..., metavar="ID"),
    note: str = typer.Option("", "--note", help="Optional reason recorded with the rejection."),
):
    """Mark a proposal as rejected."""
    cfg = DEFAULT_CONFIG.copy()
    from tradingagents.portfolio_advisor import proposals as ptp
    target = id_.strip().lower()
    rows = ptp.load_all(cfg)
    matches = [r for r in rows if (r.get("id") or "").startswith(target)]
    if not matches:
        console.print(f"[red]No proposal matching id prefix {target!r}.[/red]")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print(f"[red]Ambiguous id {target!r}.[/red]")
        raise typer.Exit(1)
    ok, row = ptp.reject(cfg, matches[0]["id"], note=note)
    if ok and row is not None:
        console.print(f"[yellow]Rejected:[/yellow] {ptp.format_one_line(row)}")
    else:
        console.print("[red]Rejection failed.[/red]"); raise typer.Exit(1)


@proposals_app.command("clear")
def proposals_clear():
    """Remove archived proposals (rejected / cancelled / executed). Keeps pending + approved."""
    cfg = DEFAULT_CONFIG.copy()
    from tradingagents.portfolio_advisor import proposals as ptp
    n = ptp.clear(cfg)
    console.print(f"[cyan]Removed {n} archived proposal(s).[/cyan]")


executor_app = typer.Typer(
    help=(
        "Real-execution scaffold (Phase 1: login + session). No trades are "
        "placed automatically — execution still requires explicit approval + "
        "REAL_EXECUTION_ENABLED=yes in env."
    ),
    no_args_is_help=True,
)
portfolio_app.add_typer(executor_app, name="executor")


@executor_app.command("login")
def executor_login():
    """One-time interactive eToro login. Prompts for the SMS code on stdin,
    saves the session cookies to ~/.tradingagents/portfolio_advisor/etoro_session.json.
    Requires ETORO_LOGIN_USERNAME and ETORO_LOGIN_PASSWORD in the environment.
    """
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor.etoro_browser import login_interactive
        msg = login_interactive(cfg)
        console.print(msg)
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@executor_app.command("verify")
def executor_verify():
    """Load the saved eToro session and confirm we land on the portfolio page
    without being redirected to login. Run this after `executor login` and
    occasionally to detect session expiry."""
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor.etoro_browser import verify_session
        console.print(verify_session(cfg))
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@executor_app.command("status")
def executor_status():
    """Show today's execution audit log + current safety limits."""
    cfg = DEFAULT_CONFIG.copy()
    from tradingagents.portfolio_advisor.executor import limits_from_cfg, read_audit_today
    lim = limits_from_cfg(cfg)
    console.print(f"limits: max {lim.max_trades_per_day} trades/day, "
                  f"max ${lim.max_position_usd:.0f}/position, "
                  f"actions={sorted(lim.allowed_actions)}, "
                  f"allowlist={'(any)' if lim.allowed_tickers is None else sorted(lim.allowed_tickers)}")
    import os
    real_on = os.environ.get("REAL_EXECUTION_ENABLED", "").strip().lower() == "yes"
    console.print(f"REAL_EXECUTION_ENABLED: {'YES (live)' if real_on else 'no (dry-run only)'}")
    rows = read_audit_today(cfg)
    console.print(f"audit rows today: {len(rows)}")
    for r in rows[-8:]:
        console.print(f"  {r.get('ts','')[:19]}  {r.get('ticker'):6} "
                      f"{r.get('action'):6} ~${float(r.get('approx_usd') or 0):.0f}  "
                      f"-> {r.get('outcome')}" + (f"  err: {r.get('error')}" if r.get('error') else ""))


@portfolio_app.command("telegram-listen")
def portfolio_advisor_telegram_listen(
    once: bool = typer.Option(False, "--once", help="Poll once and exit."),
    interval: float = typer.Option(2.0, "--interval", help="Sleep seconds between long-poll requests."),
    timeout: int = typer.Option(25, "--timeout", help="Telegram long-poll timeout seconds."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Listen for Telegram bot messages and answer with the advisor PM."""
    _configure_logging(verbose)
    cfg = DEFAULT_CONFIG.copy()
    try:
        from tradingagents.portfolio_advisor.telegram_bot import poll_once, run_poll_loop

        if once:
            n = poll_once(cfg, timeout=timeout)
            console.print(f"[cyan]Telegram listener:[/cyan] processed {n} message(s).")
        else:
            console.print("[cyan]Telegram listener running.[/cyan] Ctrl+C to stop.")
            run_poll_loop(cfg, interval_seconds=interval, timeout=timeout)
    except KeyboardInterrupt:
        console.print("[yellow]Telegram listener stopped.[/yellow]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
