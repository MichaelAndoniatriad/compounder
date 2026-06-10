import os
import json

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_DEBATE_ENABLED":       "debate_enabled",
    "TRADINGAGENTS_RISK_DEBATE_ENABLED":  "risk_debate_enabled",
    "TRADINGAGENTS_ACCOUNT_MODE":         "account_mode",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_ANALYST_LLM":          "analyst_llm",
    "TRADINGAGENTS_DEBATE_LLM":           "debate_llm",
    "TRADINGAGENTS_EXECUTION_LLM":        "execution_llm",
    "TRADINGAGENTS_LEARNED_RULES_ENABLED": "learned_rules_enabled",
    "TRADINGAGENTS_LEARNED_RULES_PATH":   "learned_rules_path",
    "TRADINGAGENTS_ANALYSIS_WEBHOOK_URL": "analysis_webhook_url",
    "TRADINGAGENTS_ANALYSIS_TELEGRAM_BOT_TOKEN": "analysis_telegram_bot_token",
    "TRADINGAGENTS_ANALYSIS_TELEGRAM_CHAT_ID": "analysis_telegram_chat_id",
    "TRADINGAGENTS_ANALYSIS_TELEGRAM_THREAD_ID": "analysis_telegram_thread_id",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_DIR": "portfolio_advisor_dir",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_STATE_PATH": "portfolio_advisor_state_path",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_WEEKDAY": "portfolio_advisor_weekly_weekday",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_MAX_JOBS": "portfolio_advisor_max_jobs_per_plan",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_RUN_DUE_MAX": "portfolio_advisor_run_due_max",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_WEEKLY_LLM": "portfolio_advisor_weekly_llm",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_WEEKLY_ALWAYS_EMAIL": "portfolio_advisor_weekly_always_email",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PLANNER_MODEL": "portfolio_advisor_planner_model",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_REASONING_MODEL": "portfolio_advisor_reasoning_model",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_SKIP_REPLAN_UNCHANGED": "portfolio_advisor_skip_replan_llm_when_unchanged",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_EARNINGS_RAMP_DAYS": "portfolio_advisor_earnings_ramp_days",
    "TRADINGAGENTS_ANALYSIS_NOTIFY_SUPPRESS_HOLD": "analysis_notify_suppress_hold",
    "TRADINGAGENTS_EVENT_LOG_PATH": "event_log_path",
    "TRADINGAGENTS_MEMORY_CONTEXT_LOOKBACK_DAYS": "memory_context_lookback_days",
    "TRADINGAGENTS_MEMORY_CONTEXT_MAX_SAME": "memory_context_max_same_ticker",
    "TRADINGAGENTS_MEMORY_CONTEXT_MAX_CROSS": "memory_context_max_cross_ticker",
    "TRADINGAGENTS_MEMORY_EVENT_LOG_PROMPT_DAYS": "memory_event_log_prompt_days",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_BOOTSTRAP_ON_INIT": "portfolio_advisor_bootstrap_on_init",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_BOOTSTRAP_DELAY": "portfolio_advisor_bootstrap_delay_seconds",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_BOOTSTRAP_MAX_POSITIONS": "portfolio_advisor_bootstrap_max_positions",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_INJECT_PRIOR_CLERK": "portfolio_advisor_inject_prior_clerk_report",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PRIOR_CLERK_MAX_CHARS": "portfolio_advisor_prior_clerk_report_max_chars",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_MODEL": "portfolio_advisor_pm_model",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_PROVIDER": "portfolio_advisor_pm_provider",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_LOG_PATH": "portfolio_advisor_pm_log_path",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_AFTER_BOOTSTRAP": "portfolio_advisor_pm_cycle_after_bootstrap",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_ENABLED": "portfolio_advisor_pm_enabled",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_AFTER_EACH_LANGGRAPH": "portfolio_advisor_pm_after_each_langgraph",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_APPLY_ACTIONS": "portfolio_advisor_pm_apply_actions",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_REPLAN_IGNORE_WEEKDAY": "portfolio_advisor_pm_replan_ignore_weekday",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_ON_PORTFOLIO_CHANGE": "portfolio_advisor_pm_cycle_on_portfolio_change",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_UNIFIED_MEMORY": "portfolio_advisor_pm_unified_memory",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_TRADING_MEMORY_PROMPT_CHARS": "portfolio_advisor_pm_trading_memory_prompt_chars",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_SNAPSHOT_CHARS": "portfolio_advisor_pm_portfolio_snapshot_chars",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_COMPACT_JSON": "portfolio_advisor_pm_compact_prompt_json",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_EVIDENCE_STALE_DAYS": "portfolio_advisor_pm_evidence_stale_days",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_CANDIDATE_COMPARISON": "portfolio_advisor_pm_candidate_comparison",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_CANDIDATE_MARKET_DATA": "portfolio_advisor_candidate_market_data_enabled",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PLANNER_PORTFOLIO_CHARS": "portfolio_advisor_planner_portfolio_chars",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_PLANNER_CATALYST_CHARS": "portfolio_advisor_planner_catalyst_chars",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_POST_VERDICT_PORTFOLIO_CHARS": "portfolio_advisor_post_verdict_portfolio_chars",
    "TRADINGAGENTS_PORTFOLIO_ADVISOR_WEEKLY_LLM_DIGEST_CHARS": "portfolio_advisor_weekly_llm_digest_chars",
    "TRADINGAGENTS_MIN_REDDIT_UPVOTES": "min_reddit_upvotes",
    "TRADINGAGENTS_ANALYSIS_EMAIL_TO": "analysis_email_to",
    "TRADINGAGENTS_ANALYSIS_EMAIL_FROM": "analysis_email_from",
    "TRADINGAGENTS_SMTP_HOST": "smtp_host",
    "TRADINGAGENTS_SMTP_PORT": "smtp_port",
    "TRADINGAGENTS_SMTP_USER": "smtp_user",
    "TRADINGAGENTS_SMTP_PASSWORD": "smtp_password",
    "TRADINGAGENTS_SMTP_USE_TLS": "smtp_use_tls",
    "TRADINGAGENTS_MODEL_PRICING_JSON": "model_pricing",
    "TRADINGAGENTS_OPENAI_VAULT_MAX_COMPLETION_TOKENS": "openai_vault_max_completion_tokens",
}


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value."""
    if isinstance(reference, bool):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    if isinstance(reference, (dict, list)):
        try:
            return json.loads(value)
        except Exception:
            return reference
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        config[key] = _coerce(raw, config.get(key))
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Append-only JSONL for alerts, bootstrap, post-earnings, etc. (default: sibling of memory log).
    "event_log_path": None,
    # Append-only JSONL of outbound advisor messages (webhook/SMTP). UI reads this.
    "message_log_path": None,
    # Suppress near-identical advisor pushes inside this rolling window. Corrections are never suppressed.
    "portfolio_advisor_message_dedupe_minutes": 180,
    # Injected into PM ``past_context`` with markdown memory: rolling calendar window.
    "memory_context_lookback_days": 60,
    "memory_context_max_same_ticker": 3,
    "memory_context_max_cross_ticker": 2,
    # Recent JSONL lines per ticker appended to ``past_context`` for the graph PM.
    "memory_event_log_prompt_days": 21,
    # Latest ``clerk_deep/<TICKER>/*_clerk_triggered.md`` prepended to full-graph ``past_context``.
    "portfolio_advisor_inject_prior_clerk_report": True,
    "portfolio_advisor_prior_clerk_report_max_chars": 8000,
    # Advisor-level PM council (separate from LangGraph's Portfolio Manager node).
    "portfolio_advisor_pm_model": "deepseek-v4-pro",
    # Route the PM at native DeepSeek (not OpenRouter). Set to None to fall
    # back to slug-based routing (a model with "/" implies openrouter).
    "portfolio_advisor_pm_provider": "deepseek",
    "portfolio_advisor_pm_log_path": None,
    # Master switch for advisor PM (set False only to pause all PM automation, e.g. tests or cost cap).
    "portfolio_advisor_pm_enabled": True,
    # After each autonomous full-graph deep run (bootstrap, run-due full_graph, clerk queues), run advisor PM.
    # Default False to save a GPT-5.5 pass per full_graph job; a post-batch PM brief still runs
    # in run_due_jobs. Set to True only if you want a per-ticker PM after every deep run.
    "portfolio_advisor_pm_after_each_langgraph": False,
    # When True, run one advisor PM cycle immediately after portfolio bootstrap completes.
    "portfolio_advisor_pm_cycle_after_bootstrap": True,
    # When True, PM structured flags may trigger replan and/or append extra pending jobs.
    "portfolio_advisor_pm_apply_actions": True,
    # When PM calls replan, ignore the planner weekday gate by default so the request always runs.
    "portfolio_advisor_pm_replan_ignore_weekday": True,
    # When True, run an advisor PM cycle when the weekly check or bootstrap detects a book/fingerprint change.
    "portfolio_advisor_pm_cycle_on_portfolio_change": True,
    # When True, advisor PM markdown appends to memory_log_path (same file as LangGraph trading_memory.md),
    # in delimiter-separated blocks ignored by TradingMemoryLog.parse; PM prompt includes a tail of that file.
    "portfolio_advisor_pm_unified_memory": True,
    # Max characters of trading_memory tail injected into the advisor PM prompt (unified mode only).
    "portfolio_advisor_pm_trading_memory_prompt_chars": 3500,
    # PM prompt size limits (lower = fewer tokens; raise if the model misses context).
    "portfolio_advisor_pm_portfolio_snapshot_chars": 4500,
    "portfolio_advisor_pm_bootstrap_summary_chars": 2500,
    "portfolio_advisor_pm_pending_jobs_cap": 12,
    "portfolio_advisor_pm_prior_cycles": 2,
    "portfolio_advisor_pm_prior_executive_chars": 450,
    "portfolio_advisor_pm_prior_memory_note_chars": 700,
    "portfolio_advisor_pm_prior_context_total_chars": 2600,
    "portfolio_advisor_pm_compact_prompt_json": True,
    "portfolio_advisor_pm_extra_context_chars": 2000,
    # Skip the post-batch PM brief if every completed verdict was HOLD.
    # PM still runs on portfolio_change / replan / chat triggers.
    "portfolio_advisor_pm_skip_if_all_hold": True,
    # Research older than this is considered stale for PM action stances unless refreshed.
    "portfolio_advisor_pm_evidence_stale_days": 30,
    # In autonomous (alpaca) mode the executor already announces every trade; the full
    # action ticket is redundant. Set True to re-enable tickets in alpaca mode anyway.
    "portfolio_advisor_action_tickets_autonomous": False,
    # Minimum gap (minutes) between PM push_notes in autonomous mode. Notes are suppressed
    # when no trade was submitted recently, no "?" in the note, and last note was within
    # this window. Set 0 to disable throttling.
    "portfolio_advisor_pm_note_min_gap_minutes": 60,
    # When monthly lookout promotes a candidate, ask the PM council to compare it with current holdings.
    "portfolio_advisor_pm_candidate_comparison": True,
    # Best-effort yfinance enrichment for candidate liquidity gates when avg_daily_volume is not supplied.
    "portfolio_advisor_candidate_market_data_enabled": True,
    # --- Strategy sleeves (core long-term/growth vs catalyst tactical) ---
    # Target portfolio mix by sleeve; PM flags drift and steers new capital to rebalance.
    "portfolio_advisor_sleeve_targets": {"core": 0.50, "catalyst": 0.40, "cash": 0.10},
    # How far actual sleeve weight may drift from target before the PM flags it (fraction of total).
    "portfolio_advisor_sleeve_drift_tolerance": 0.07,
    # After a position is closed (watchdog_exit, sell, or trim in the ledger), a new BUY
    # for the same ticker within this many days is blocked (wash-loop prevention).
    # Applies to both 'buy' and 'add' actions; the existing add-cooldown covers only
    # sequential adds, not re-entry after a stop-out.
    "portfolio_advisor_reentry_cooldown_days": 5,
    # Catalyst-sleeve exit rules (fractions of entry/peak; days for time-based exits).
    "portfolio_advisor_catalyst_hard_stop_pct": 0.08,        # exit if down 8% from entry
    # Trailing stop arm threshold: trailing activates once the position is up this much from entry.
    # Arms at +5% (not +10%) so the +5..+10% "dead zone" (below the old arm, above time-stop) is covered.
    "portfolio_advisor_catalyst_trail_arm_pct": 0.05,        # trailing arms once up 5% from entry
    # Trailing stop distance: exit if price falls this far below the running peak.
    "portfolio_advisor_catalyst_trail_dist_pct": 0.08,       # then exit if down 8% from peak
    # Legacy keys kept for backward compatibility — prefer the new trail_arm/trail_dist keys above.
    "portfolio_advisor_catalyst_trailing_activate_pct": 0.10,  # (legacy) trailing arms once up 10% from entry
    "portfolio_advisor_catalyst_trailing_stop_pct": 0.08,    # (legacy) then exit if down 8% from peak
    "portfolio_advisor_catalyst_time_stop_days": 3,          # days after catalyst date to exit if move didn't happen
    "portfolio_advisor_catalyst_max_hold_days": 30,          # hard hold cap when no catalyst date is set
    "portfolio_advisor_catalyst_pre_event_days_before": 1,   # go flat this many days before catalyst event (Option B: avoid binary print)
    # --- High-conviction sizing tier (studied concentration, NOT margin) ---
    # The PM may flag a buy as high_conviction. If confidence clears the floor and a
    # slot is free, the position cap rises from max_position_pct to the pct below.
    # Stops are NEVER loosened — bigger size, same exit discipline. Cash only, no margin.
    "portfolio_advisor_alpaca_high_conviction_enabled": True,
    "portfolio_advisor_alpaca_high_conviction_pct": 0.15,             # per-position cap of book for granted HC buys
    "portfolio_advisor_alpaca_high_conviction_min_confidence": 0.85,  # stated confidence floor to qualify
    "portfolio_advisor_alpaca_high_conviction_max_positions": 2,      # concurrent HC slots across the whole book
    # --- Marketable-limit entry quality controls (R1) ---
    # Max bid-ask spread in basis-points to allow a limit order; wider spreads are skipped
    # (market order would give away too much to the spread). 100 bps = 1% of price.
    "portfolio_advisor_max_spread_bps": 100,
    # Extra slippage buffer added to the ask when computing the limit price, in bps.
    # Ensures the limit is high enough to fill immediately at the ask rather than missing.
    "portfolio_advisor_limit_slip_bps": 10,
    # Dip-watch (quality-on-a-dip): watch CORE watchlist names for a SHALLOW pullback
    # into a buy zone, then let the PM judge (value-trap check) + propose a staged entry.
    # Bounds are config because the research found exact thresholds are indicative, not
    # validated — tune to taste. below_ma_pct is positive when price sits BELOW the MA.
    "portfolio_advisor_dip_watch_enabled": True,
    "portfolio_advisor_dip_watch_ma_window": 50,                   # moving-average window (days)
    "portfolio_advisor_dip_watch_high_window_days": 30,           # "off-high" measured vs the 30-day (recent) high
    "portfolio_advisor_dip_watch_min_below_ma_pct": 0.0,          # buy zone starts at/just below the MA
    "portfolio_advisor_dip_watch_max_below_ma_pct": 20.0,        # ...up to 20% below the MA
    "portfolio_advisor_dip_watch_off_high_min_pct": 8.0,         # must be >=8% off the recent high (a real pullback)
    "portfolio_advisor_dip_watch_off_high_max_pct": 20.0,       # >=20% off the recent high = too far, reject
    "portfolio_advisor_dip_watch_falling_knife_below_ma_pct": 25.0,  # >=25% below the MA = knife, reject
    "portfolio_advisor_dip_watch_rsi_floor": 25.0,              # RSI < 25 = capitulation, reject
    # Core sleeve: a +15% position only fires PRE_EARNINGS_TRIM (red) when its next earnings
    # date is within this many days; otherwise it stays amber ("trim armed").
    "portfolio_advisor_pre_earnings_trim_window_days": 14,
    # After each full_graph deep run, auto-refresh that name's sleeve + thesis-break metrics
    # via the position classifier (one extra cheap LLM pass). Set False to disable.
    "portfolio_advisor_auto_classify_after_full_graph": True,
    # Weekly research funnel: max candidate full_graph deep runs per rolling 7 days (cost control).
    # Names that clear the screen beyond this are held for the next week, ranked by priority.
    "portfolio_advisor_weekly_full_graph_cap": 4,
    # News researcher (weekly discovery): how many candidates to target, and prompt sizing.
    "news_researcher_target_candidates": 8,
    "news_researcher_article_limit": 15,
    "news_researcher_news_chars": 6000,
    # Planner / replan LLM: portfolio export + catalyst digest size (characters).
    "portfolio_advisor_planner_portfolio_chars": 5500,
    "portfolio_advisor_planner_catalyst_chars": 3500,
    # Post-earnings verdict CLI: portfolio excerpt in the reasoning prompt.
    "portfolio_advisor_post_verdict_portfolio_chars": 3500,
    # Optional weekly LLM digest (when ``portfolio_advisor_weekly_llm`` is True).
    "portfolio_advisor_weekly_llm_digest_chars": 3500,
    # Memory review: last N events as compact JSON in the reasoning prompt.
    "portfolio_advisor_memory_review_sample_events": 24,
    "portfolio_advisor_memory_review_json_chars": 6000,
    # core_discovery_cadence: monthly (runs first Saturday of each month).
    # Core names change on quarterly fundamentals cadence; monthly is enough.
    # Cron: 0 10 1-7 * 6 (first Saturday, 10:00 UTC).
    "core_discovery_cadence": "monthly",
    # Consensus factor tilt for core discovery ranking (post-gate, ±0.10 max).
    # Uses only crowding components (entry + divergence), NOT retail flow.
    # Gated behind CONSENSUS_FACTOR_LIVE — keep False until snapshot is populated.
    "consensus_tilt_weight": 0.10,
    "CONSENSUS_FACTOR_LIVE": False,
    # Single-model advisor jobs: memory + JSONL tail injected into the reasoning prompt.
    "portfolio_advisor_single_model_memory_chars": 3500,
    "portfolio_advisor_single_model_events_chars": 2500,
    # After a pending decision is resolved with returns, the quick LLM may append
    # short rules to learned_rules.md (default: sibling of trading_memory.md).
    "learned_rules_enabled": True,
    "learned_rules_path": None,
    # Optional Slack-style webhook (JSON `{"text":...}`) after each successful
    # full-graph run — posts the advisory PM memo (truncated), not an order.
    "analysis_webhook_url": None,
    # Optional Telegram delivery for advisor messages. Create a bot with BotFather,
    # message it/group it, then set token + chat id in env/.env.
    "analysis_telegram_bot_token": None,
    "analysis_telegram_chat_id": None,
    "analysis_telegram_thread_id": None,
    # Autonomous portfolio advisor (eToro positions → LLM schedule → due deep runs).
    # portfolio_advisor_weekly_weekday: 0=Mon … 5=Sat, 6=Sun (default Saturday).
    "portfolio_advisor_dir": None,
    "portfolio_advisor_state_path": None,
    "portfolio_advisor_weekly_weekday": 5,
    "portfolio_advisor_max_jobs_per_plan": 15,
    "portfolio_advisor_run_due_max": 3,
    "portfolio_advisor_deep_analysts": ["news", "fundamentals", "market"],
    # Weekly ``advisor portfolio weekly`` = lightweight check only (no full replan).
    "portfolio_advisor_weekly_llm": False,
    "portfolio_advisor_weekly_always_email": False,
    # Send an ntfy notification after every single-model analysis job (thesis_check etc.).
    # False = results are written to the message log and dashboard only; no ntfy push.
    "portfolio_advisor_single_model_notify": False,
    # Portfolio advisor planner (scheduling) uses ``llm_provider`` + this model.
    # None = fall back to ``quick_think_llm`` (cheap path for routine scheduling).
    "portfolio_advisor_planner_model": None,
    # Stronger model for: post-earnings verdict CLI, and optional advisor digest when
    # a CRITICAL rule fires. OpenRouter slug recommended.
    # V4 Pro for PM/memory-review (synthesis, not fresh reasoning); R1 kept in
    # single_model_analysis via portfolio_advisor_single_model_reasoning_model below.
    "portfolio_advisor_reasoning_model": "deepseek-v4-pro",
    # Model used specifically for single_model_analysis jobs (fresh per-ticker reasoning
    # where R1's chain-of-thought earns its cost). Falls back to reasoning_model if unset.
    "portfolio_advisor_single_model_reasoning_model": "deepseek-reasoner",
    # When True, ``advisor portfolio replan`` skips the planner LLM if live tickers and
    # the catalyst digest match the last successful plan (saves cost; pending jobs unchanged).
    "portfolio_advisor_skip_replan_llm_when_unchanged": False,
    # Injected into the planner prompt: highlight names with earnings within N days.
    "portfolio_advisor_earnings_ramp_days": 7,
    # Optional map tickerUpper -> list of thesis break metric strings for plan validation.
    "portfolio_advisor_thesis_metrics": {},
    # After a full graph run: skip webhook + advisory email when parsed rating is Hold.
    "analysis_notify_suppress_hold": False,
    # After ``advisor portfolio init``: optionally run full graph on every holding (expensive).
    "portfolio_advisor_bootstrap_on_init": False,
    "portfolio_advisor_bootstrap_delay_seconds": 45.0,
    "portfolio_advisor_bootstrap_max_positions": None,
    # Optional: email advisory memo after each successful full-graph run (SMTP).
    "analysis_email_to": None,
    "analysis_email_from": None,
    "smtp_host": None,
    "smtp_port": 587,
    "smtp_user": None,
    "smtp_password": None,
    "smtp_use_tls": True,
    # Optional per-model price table used by CLI runtime cost estimates.
    # Format:
    # {
    #   "model-id": {"input_per_1m": 0.15, "output_per_1m": 0.60}
    # }
    # You can also provide this as TRADINGAGENTS_MODEL_PRICING_JSON.
    "model_pricing": {},
    # Corporate hierarchy: per-agent routing through OpenRouter only (see
    # model_catalog.DEFAULT_CORPORATE_AGENT_ROUTING). When True, the main graph
    # ignores ``llm_provider`` / ``quick_think_llm`` / ``deep_think_llm``.
    # Tuned only via this dict or Streamlit settings — not TRADINGAGENTS_* env.
    "corporate_hierarchy_enabled": True,
    # Provider backing every corporate-hierarchy role. "openrouter" routes
    # ``upstream/model`` slugs; "deepseek" (etc.) calls the provider's own API
    # directly and strips the slug prefix — valid only when every routed model
    # belongs to that provider (the routing table is all-DeepSeek by default).
    "corporate_llm_provider": "deepseek",
    # Same-provider rate-limit fallback model for native (non-OpenRouter) mode.
    # OpenRouter mode uses ``llm_fallback_openrouter_model`` instead. Empty
    # disables the fallback (single-provider has no cross-provider safety net).
    "corporate_llm_fallback_model": "deepseek-chat",
    # Fixed temperature on graph LLMs for repeatable verdicts. 0.0 = maximum
    # determinism (cuts the model's intermittent empty-reply behavior in chat
    # mode). Set None to use provider defaults.
    "graph_temperature": 0.0,
    # Per-ticker rolling cap on full_graph runs (14d window). Stops retry
    # storms (e.g. NVDA had 11 runs/14d before this). On exceeding, jobs are
    # downgraded to single_model so the work still happens cheaply.
    "portfolio_advisor_full_graph_per_ticker_14d_cap": 2,
    # Reflection at the start of every full_graph: gate so it only runs on
    # full_graph (not single_model) and only once per (ticker, day). The
    # learned-rules pipeline is unaffected — outcomes still resolve in batch.
    # Set False to disable per-run reflection entirely (a weekly batch CLI is
    # the recommended replacement).
    "reflection_per_run_enabled": True,
    # Daily $ spend cap across all advisor LLM calls. When today's logged
    # spend exceeds this, run_due_jobs claims no new jobs and queues a single
    # urgent alert. Watchdog (price triggers) + chat replies still run.
    # 0 or None disables the cap. Reads cost.jsonl (TRADINGAGENTS_COST_LOG).
    "portfolio_advisor_daily_cost_cap_usd": 5.0,
    # Throttle for the "budget cap hit" alert so it does not page every tick.
    "portfolio_advisor_daily_cost_alert_throttle_hours": 6,
    # Routine-monitoring jobs: combine all live tickers into one single_model
    # call per day instead of one job per ticker. Massive prompt-token saving.
    "portfolio_advisor_combined_daily_monitoring": True,
    # Optional partial overrides: logical agent key -> {model, extra_body?}
    # (``provider`` is always openrouter; any ``provider`` key in overrides is ignored.)
    "agent_llm_routing": {},
    # OpenRouter API base URL for corporate mode only (None = library default).
    "corporate_openrouter_base_url": None,
    "llm_rate_limit_fallback_enabled": True,
    "llm_fallback_openrouter_model": "openai/gpt-4o-mini",
    "openai_vault_max_completion_tokens": 16000,
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # Tiered model routing (used when corporate_hierarchy_enabled is False).
    # When all three keys are set (non-None), each role group gets its own
    # OpenRouter model instead of the coarse quick/deep split.
    #   analyst_llm    -> market, social, news, fundamentals analysts (Tier 2)
    #   debate_llm     -> bull, bear, trader, research_manager,
    #                     risk_aggressive, risk_neutral, risk_conservative (Tier 3 debate)
    #   execution_llm  -> portfolio_manager only (Tier 3 arbiter)
    # Set any of them to None to fall back to the quick/deep pair below.
    "analyst_llm": None,
    "debate_llm": None,
    "execution_llm": None,
    # LLM settings (legacy single-provider graph when corporate_hierarchy_enabled is False)
    "llm_provider": "deepseek",
    "deep_think_llm": "deepseek-v4-pro",
    "quick_think_llm": "deepseek-v4-flash",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # When False, skip the bull/bear researcher debate entirely and send analysts
    # directly to the Research And Execution node. Saves 2+ LLM calls per run.
    "debate_enabled": True,
    # When False, skip the aggressive/conservative risk debate after R&E and go
    # directly to the Portfolio Manager. Saves 2 more LLM calls per run.
    # Set both debate_enabled=False and risk_debate_enabled=False to run the
    # minimum viable graph (analysts → R&E → PM) once scoreboard data shows
    # debates don't improve alpha.
    "risk_debate_enabled": True,
    # Which account the advisor manages. "etoro": advisory-only — human executes
    # on eToro, paper book mirrors advice. "alpaca": AUTONOMOUS — the PM manages
    # the Alpaca PAPER book as the first-class portfolio (snapshot, watchdog,
    # sleeves all read Alpaca; proposals auto-execute there; Telegram messages
    # are decision notices, not action requests). Real-money execution does not
    # exist in either mode. Override with TRADINGAGENTS_ACCOUNT_MODE.
    "account_mode": "etoro",
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 12,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 6,       # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Search queries used by get_global_news for macro headlines. Extend or
    # replace to broaden geographic / sector coverage.
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Reddit signal quality filter: posts below this upvote threshold are dropped.
    "min_reddit_upvotes": 20,
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS":  "^NSEI",    # NSE India (Nifty 50)
        ".BO":  "^BSESN",   # BSE India (Sensex)
        ".T":   "^N225",    # Tokyo (Nikkei 225)
        ".HK":  "^HSI",     # Hong Kong (Hang Seng)
        ".L":   "^FTSE",    # London (FTSE 100)
        ".TO":  "^GSPTSE",  # Toronto (TSX Composite)
        ".AX":  "^AXJO",    # Australia (ASX 200)
        "":     "SPY",      # default for US-listed tickers (no suffix)
    },
    # EP scanner news sources. Yahoo Finance RSS is now enabled alongside Alpha
    # Vantage per the Option B fix in docs/ep_gate_audit.md. The scanner deduplicates
    # by URL (preferred) or (source, title) fallback, so overlap is harmless.
    # Add future sources here without changing any other code.
    "ep_scanner_news_sources": ["alpha_vantage", "yahoo_finance_rss"],
    # How far out a catalyst date may be for an EP candidate or catalyst-sleeve proposal
    # to be accepted. Beyond this window the thesis is speculative and the dated-event
    # constraint loses meaning. Default 90 days. Must be a positive integer.
    "portfolio_advisor_catalyst_max_days_out": 90,
    # PEAD scanner settings (R3)
    # Max Alpha Vantage EARNINGS calls per scan_post_reports run (free-tier budget).
    "portfolio_advisor_pead_max_names": 15,
})
