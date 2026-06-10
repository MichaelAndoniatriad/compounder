# Compounder 2.2 — Alpha Roadmap (work order)

**Date:** 2026-06-10. **Source:** 17-agent edge analysis (5 code readers, 8 edge analysts, 3 adversarial skeptics, 1 synthesis).
**Audience:** an AI or engineer with NO prior context. Everything needed is in this file + the repo.

## Context

Repo: `~/workspace/trading-agents`. Python 3.12 uv venv at `.venv` (use `.venv/bin/python`, never `python -m pip`).
Tests: `cd ~/workspace/trading-agents && set -a && source .env && set +a && .venv/bin/python -m pytest tests/ -q` → suite must stay green (604 tests + 93 subtests as of commit 1a3c090).

The system is an autonomous LLM portfolio manager trading an Alpaca PAPER account (~$100k) in `TRADINGAGENTS_ACCOUNT_MODE=alpaca`. Two sleeves: core (3–5yr compounders) and catalyst (short-term dated-event trades). The edge analysis concluded the system's ONE durable alpha source is **capacity-constrained event drift (PEAD / episodic pivots / revision drift) in small/mid caps ($2–50M daily dollar volume)** — worth an honest 50–150bps/yr net, currently captured at 0bps because the catalyst sleeve has never completed a trade and the universe stops at large caps. Discipline and a regime overlay are multipliers that protect that edge, not alpha. Everything else was adversarially killed.

**Safety invariants — never violate:**
- PAPER ONLY. `executor.py::_client` refuses non-`PK` Alpaca keys, hard-codes `paper=True`. Never add a real-money path. Never touch eToro execution.
- `PYTEST_CURRENT_TEST` guards (`executor.enabled()`, `etoro_scan.account_mode()`, `executor.market_clock()`) return safe defaults under pytest. Preserve; tests never hit live APIs.
- Executor entry points swallow their own errors (log + Telegram, never raise into PM cycle/watchdog).
- State in `~/.tradingagents/portfolio_advisor/`. Tests sandbox via cfg `portfolio_advisor_dir`.
- Do NOT git-add the 5 known-dirty test files: tests/test_advisor_pm.py, test_corporate_hierarchy.py, test_env_overrides.py, test_messaging.py, test_telegram_bot.py.
- Every fix lands with unit tests; commit per item with a WHY message ending `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`.

Implement in order — R1/R2 unblock everything else.

---

## R1 — Execution machinery ✅ aacec0b (BLOCKER: small-cap spreads eat the edge without this)

In `tradingagents/integrations/alpaca/executor.py`:
1. **Marketable-limit entries.** Before a buy, fetch the latest quote (alpaca-py `StockHistoricalDataClient` + `StockLatestQuoteRequest`; free IEX feed; keys already in env). If quoted spread > `portfolio_advisor_max_spread_bps` (new cfg, default 100): skip with a logged+ledgered reason. Otherwise submit a LIMIT DAY order at ask × (1 + `portfolio_advisor_limit_slip_bps`/10000, default 10) instead of a market order. Quantity-based (qty = floor(notional/limit_price × 1000)/1000 fractional ok) since bracket orders need qty. If the quote fetch fails → fall back to current market-order behavior (large caps stay fine).
2. **Broker-resident stop for catalyst buys.** When sleeve=="catalyst", submit as a bracket/OTO order with `stop_loss` at entry × (1 − catalyst hard stop pct, default 0.08) so the −8% stop lives at the broker, not in the 5-min watchdog poll. Keep `enforce_paper_exits` as the backstop (it must tolerate the broker stop having already closed the position — it already skips unheld names).
3. **No overnight queuing for catalyst buys.** If `market_clock()` says closed and sleeve=="catalyst": skip with reason "market closed — catalyst entries only execute live" (re-proposal next cycle is automatic). Core buys may still queue.
4. **Fill + slippage logging.** After submit, poll the order once after ~2s (and let the next watchdog tick reconcile): write a ledger row `{"action":"fill_check", order_id, status, filled_avg_price, intended_price, slippage_bps}` when filled. Add `reconcile_fills(cfg)` called from `enforce_paper_exits` top that back-fills any submitted-but-unchecked orders from the last 24h.

Accept: unit tests for spread-reject, limit-price math, bracket params for catalyst, closed-market catalyst skip, slippage row. Mock all Alpaca clients.

## R2 — Wire the measurement loop ✅ f7e8944 (the learning system is dead code in production)

Audit-confirmed: `outcomes.jsonl` does not exist; recommendation_log has ~5 rows ever; the shadow book referenced in `candidates.py` has never been written.
1. Find the LaunchAgent/cron that should measure outcomes (`grep -r measure-outcomes ~/Library/LaunchAgents scripts/ cli/`). Point it at `outcome_tracker.compute_recommendation_outcomes` (currently test-only); replace any legacy logic in `cli/advisor_cmd.py` (~line 485).
2. In `proposals.add()`: after an executed proposal, write a recommendation_log row (trigger="autonomous_execution", action, ticker, rationale=reason) so every trade is scoreable.
3. Activate the shadow book: every candidate that passes all mechanical gates but is NOT traded gets a `shadow_book.jsonl` row (ticker, ts, gates passed, hypothetical entry = last close, catalyst_date, source). Add `shadow_outcomes(cfg)` scoring 30d forward returns vs SPY, callable from the same outcome cron.
4. Daily NAV snapshot: append `{ts, equity, spy_close, positions_count, cash}` to `nav_history.jsonl` once per day (guard by date) from the watchdog tick. The "are we beating the S&P" question must be answerable from a persisted series.

Accept: unit tests with sandboxed dirs for each writer; outcome cron smoke-callable via CLI.

## R3 — PEAD scanner ✅ b4b2141 (the best-documented anomaly at this scale is untargeted)

New `tradingagents/portfolio_advisor/pead_scanner.py`:
- Nightly: pull Alpha Vantage `EARNINGS_CALENDAR` (3-month horizon, CSV endpoint, free tier) → persist a forward calendar `earnings_calendar.json`.
- Post-report (run next morning): for yesterday's reporters — SUE proxy = (actual EPS − estimate)/|estimate| from AV `EARNINGS`; require surprise ≥ +10%, next-day gap direction agreeing (open > prior close for longs), RVOL ≥ 1.5 (vs 20d avg volume), dollar volume ≥ $5M.
- Survivors → `candidates.evaluate_candidate` with `sleeve="catalyst"`, `catalyst_date` = report date (drift window: time-stop logic already handles exit), source="pead_scanner".
- Schedule: extend `scripts/run-advisor.sh` routing + a `com.compounder.pead-scan.plist` LaunchAgent (weekdays 14:45 local — after US open), modeled on existing plists. Respect AV free-tier limits (≤25 req/day): batch, cache, and cap names checked per run (cfg `portfolio_advisor_pead_max_names`, default 15).

Accept: unit tests for SUE math, gate logic, calendar parsing (fixture CSV), proposal routing; plist loads.

## R4 — EP scanner fixes + EDGAR 8-K feed ✅ 0012974 (funnel starvation)

1. In `ep_scanner.py`: add the RVOL gate its own SOP mandates — day volume ≥ 2× 20d average AND dollar volume ≥ $5M — logged to the existing gate log with pass/fail detail. (docs/strategies/episodic_pivot.md §volume; gap-without-volume is where reversals live.)
2. Pre-market scan honesty: the 08:30 scan currently uses yfinance daily bars (no prepost) — fake. Use Alpaca latest quote/trade (free IEX) for pre-market price when in the pre-market window; if unavailable, SKIP the pre-market scan with a log line rather than scanning stale closes.
3. New `tradingagents/dataflows/edgar.py`: free SEC APIs (https://data.sec.gov, proper User-Agent header per SEC fair-access policy, ≤10 req/s, cache the ticker→CIK map). `fetch_recent_8k_items(items={"2.02","8.01","5.02"}, hours=24)` → list of {ticker, item, title, filed_at, url}. Register as a third source in `ep_scanner_news_sources` ("edgar") so 8-K filings feed the same Tier1/Tier2 hint pipeline. 8-Ks cover the small caps where free news feeds don't.

Accept: unit tests with fixture JSON for EDGAR parsing; RVOL gate tests; pre-market path test (mocked).

## R5 — Down-cap universe expansion (gated behind R1–R4; ship code now, flag off)

1. `core_discovery._build_universe`: behind cfg `portfolio_advisor_universe_smallcap_enabled` (default False until R1–R4 are verified live), add S&P 600 constituents (fetch + cache; fall back to a vendored CSV).
2. `mechanical_filter.py`: replace flat share-count/size gates with dollar-ADV gates — tradable iff $2M ≤ 20d avg dollar volume (≤$50M = "edge zone" tag).
3. Fundamentals at scale: prefer free SEC XBRL `companyfacts` (via `dataflows/edgar.py`) for revenue growth/margins where Alpha Vantage OVERVIEW would blow the free tier; AV stays for the current 516.
4. Small-cap cohort routing: while `portfolio_advisor_smallcap_shadow_days` (default 90) hasn't elapsed since the flag was enabled (persist enable date in state), ALL small-cap candidates route to the shadow book only — never live proposals.

Accept: unit tests for ADV gates, shadow routing window, universe flag off-by-default.

---

## Explicitly rejected (do not build)
Thematic anticipation trades as catalyst entries (wrong-signed; DKNG class → shadow book only — covered by validate gates in pm_tools); macro pattern library as a trading signal; more LLM context/authority; LLM self-grading loops; naive small-cap tilt without the event trigger.

## Done criteria
Suite green after every item; one commit per item; this doc updated with a ✅ + commit hash per item as it lands.
