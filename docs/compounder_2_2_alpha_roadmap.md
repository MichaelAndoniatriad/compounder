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

## R5 — Down-cap universe expansion ✅ d2c288a (gated behind R1–R4; ship code now, flag off)

1. `core_discovery._build_universe`: behind cfg `portfolio_advisor_universe_smallcap_enabled` (default False until R1–R4 are verified live), add S&P 600 constituents (fetch + cache; fall back to a vendored CSV).
2. `mechanical_filter.py`: replace flat share-count/size gates with dollar-ADV gates — tradable iff $2M ≤ 20d avg dollar volume (≤$50M = "edge zone" tag).
3. Fundamentals at scale: prefer free SEC XBRL `companyfacts` (via `dataflows/edgar.py`) for revenue growth/margins where Alpha Vantage OVERVIEW would blow the free tier; AV stays for the current 516.
4. Small-cap cohort routing: while `portfolio_advisor_smallcap_shadow_days` (default 90) hasn't elapsed since the flag was enabled (persist enable date in state), ALL small-cap candidates route to the shadow book only — never live proposals.

Accept: unit tests for ADV gates, shadow routing window, universe flag off-by-default.

---

---

## Status 2026-06-10

All five Compounder 2.2 alpha roadmap items are complete and merged to main.

| Item | Hash | Notes |
|------|------|-------|
| R1 — Execution machinery | aacec0b | Marketable-limit entries, broker-resident stop, closed-market catalyst skip, fill logging |
| R2 — Measurement loop | f7e8944 | outcome tracker wired, shadow book activated, NAV history, recommendation log |
| R3 — PEAD scanner | b4b2141 | Nightly earnings calendar + morning drift scan feeding catalyst sleeve |
| R4 — EP scanner fixes + EDGAR 8-K feed | 0012974 | RVOL gate, pre-market honesty, edgar.py with 8-K feed |
| R5 — Down-cap universe expansion | d2c288a | S&P 600 cohort, dollar-ADV gates, XBRL companyfacts, 90d shadow window; flag off until R1–R4 live validation |

Suite: 714 tests + 93 subtests, all green.

### Post-review hardening (adversarial review wbx14udym) — 2026-06-11

Five fail-open holes found and closed across the R5 shadow window and catalyst exit logic:

| Finding | Hash | Fix |
|---------|------|-----|
| Phase 5b fail-open (empty cohort map bypassed shadow routing) | 29f216d / d4aa92e | Drop `and _cohort_map` gate; empty map shadows all picks + Telegram alert |
| proposals.add() shadow bypass (watchlist/catalyst paths skipped choke) | d4aa92e | Central shadow choke in proposals.add(): every buy/add during window, unknown cohort → shadow_book, status=shadowed |
| `get_ticker_cohort` missing (no cache-only lookup existed) | d4aa92e | New cache-only helper; "unknown" return = callers fail closed |
| Smallcap quant screen: $1B floor killed smallcap candidates | d4aa92e | MIN_MARKET_CAP_SMALLCAP=$100M for smallcap cohort; shares_outstanding×price cap fallback |
| enabled_at missing = silent debug log, no self-heal | d4aa92e | Self-heal: record now, WARNING log, Telegram, return True |
| Time-stop insta-close on PEAD entries (past catalyst_date) | d4aa92e | Anchor to max(catalyst_date, entry_date) in eval_catalyst_exit and executor no-plan fallback |

Also fixed a pre-existing test isolation leak in test_pm_catalyst_guard.py (direct `proposals.add = lambda` was never restored, contaminating subsequent tests).

Suite after hardening: **784 tests + 93 subtests, all green** (19 new tests added).

### T1 ✅ 2715496 — Regime overlay + circuit breaker — 2026-06-11

Deterministic sizing overlay enforced in code (never via LLM judgment). Two failure modes from the 17-agent edge analysis closed:

| Component | Detail |
|-----------|--------|
| `regime.py` | `compute_regime()` (SPY 200DMA + 20d vol, 6h cache, caution fallback), `drawdown_breaker()` (HWM ratchet, −10% halve / −15% halt), one-shot Telegram on transitions |
| `executor._paper_buy` | Regime multiplier (1.0 / 0.75 / 0.5) + breaker applied post-sizing; halt skips buy; all failures degrade to 1.0; ledger rows gain regime + breaker_level fields |
| `advisor_pm.py` | "MUST deploy" → neutral framing; all-cash path no longer demands re-entry; regime block injected into PM prompt (informational) |
| `default_config.py` | 4 new keys: regime_enabled, regime_vol_threshold, breaker_halve_pct, breaker_halt_pct |
| Tests | 32 new tests in `test_regime_overlay.py`; pre-existing HC test updated with `regime_enabled=False` |

Suite: **816 tests + 93 subtests, all green**.

### T2 ✅ 70130de — R-based catalyst sizing + PM veto gate — 2026-06-11

Catalyst sleeve's alpha bounded by mechanical components; LLM judgment can only subtract via familiarity bias. Closed two systematic failure modes:

| Component | Detail |
|-----------|--------|
| `executor._paper_buy` | Catalyst sleeve now R-based sizing: notional = equity × risk_pct / hard_stop_pct (1%/8% = 12.5× risk budget). Confidence and HC no longer affect size. Ledger rows gain `sizing_method: "r_based_1pct"` |
| `_high_conviction_grant` | Denies with "core-sleeve only" when sleeve=="catalyst". HC tier has no meaning with a hard stop — size up, same stop = catalyst R-math already does that |
| `proposals.add` | Scanner-sourced catalyst proposals (ep_scanner, pead_scanner) filed with `pm_veto_window_until = now + 45m`. NOT auto-executed. PM-originated proposals keep immediate path |
| `execute_unvetoed_candidates` | New function in executor, called from enforce_paper_exits, fires proposals whose veto window expired un-vetoed (all guards re-checked live) |
| `pm_tools.veto_candidate` | New PM tool: blocks scanner candidate within window. Writes shadow row (source=`pm_vetoed_<orig>`) for forward-return scoring |
| `outcome_tracker.veto_scorecard` | New helper: vetoed vs executed cohort avg 30d returns + pm_veto_lift metric; wired into measure-outcomes CLI |
| PM prompt | Veto contract stated: "execute automatically after N minutes unless you veto_candidate(ticker, reason). Veto only on disqualifying evidence — vetoes are scored" |
| Tests | 26 new tests in `test_catalyst_r_sizing_and_veto.py`; 1 pre-existing test updated for R-sizing |

Suite: **842 tests + 93 subtests, all green**.

### T4 ✅ 81972a9 — Multi-horizon outcome scoring — 2026-06-11

Single 30-day window was scoring 3–5yr core theses against noise. Catalyst trades need R-multiple scoring at their natural horizon.

| Component | Detail |
|-----------|--------|
| `outcome_tracker.py` | `compute_core_multihorizon_outcomes()`: idempotent 30/90/365d checkpoints → `multihorizon_outcomes.jsonl`; `CORE_HORIZONS=(30,90,365)` module constant |
| `outcome_tracker.py` | `compute_catalyst_r_outcomes()`: R = (exit−entry)/(entry×hard_stop_pct) at catalyst_date+5d; ledger close used when position exits early → `catalyst_r_outcomes.jsonl` |
| `outcome_tracker.py` | `multihorizon_aggregates()`: per-horizon count/avg_ret/avg_alpha for core; count/avg_R for catalyst |
| `veto_scorecard` | Gains `core_horizons` + `catalyst_r` aggregates in return dict |
| `recommendation_log.py` | `log_recommendation()` gains `sleeve` + `catalyst_date` params |
| `proposals.py` | `proposals.add()` passes sleeve/catalyst_date to recommendation log |
| `cli/advisor_cmd.py` | `measure-outcomes` calls both new scorers; prints per-horizon alpha/R summary |
| Tests | 8 new tests in `TestMultiHorizonCoreScoring` + `TestCatalystRMultipleScoring` |

Suite: **870 tests + 93 subtests, all green**.

### T3 ✅ 2bbb8c7 — Re-underwrite flow + hard book-loss cap + counterfactual ledger — 2026-06-11

Converted the core sleeve's mandatory -40% full-exit stop into a forced re-underwrite with
a deterministic deadline and a separate hard book-loss cap — so the Bessembinder tail can
survive temporary drawdowns while accounts are still protected from concentrated losses.

| Component | Detail |
|-----------|--------|
| `position_plans.py` | `PositionPlan` gains `reunderwrite_triggered_at`, `reunderwrite_deadline`, `reunderwrite_verdict`, `reunderwrite_last_cleared_at`; `build_trigger_block` surfaces PENDING RE-UNDERWRITES section with deadline |
| `executor.py` | Core -40% now arms re-underwrite + queues `full_graph` job (idempotent; dedup); deadline expiry → `paper_core_reunderwrite_expired`; `reunderwrite_verdict="broken"` → `paper_core_thesis_broken`; account equity cached once per tick; book-loss cap (5% equity) closes immediately regardless of re-underwrite state; `close_for_watchdog` appends to `counterfactual_ledger.jsonl` on every rule close |
| `pm_tools.py` | New `record_reunderwrite_verdict(ticker, verdict, reason)` tool; "reconfirmed" clears trigger + 30d cooldown; "broken" flags for immediate close |
| `outcome_tracker.py` | New `score_counterfactuals(cfg)`: scores ledger rows ≥30d/≥180d vs SPY forward return, writes `counterfactual_scores.jsonl`; idempotent |
| `default_config.py` | 3 new keys: `portfolio_advisor_reunderwrite_days=5`, `portfolio_advisor_reunderwrite_cooldown_days=30`, `portfolio_advisor_max_position_book_loss_pct=0.05` |
| `cli/advisor_cmd.py` | `measure-outcomes` now calls `score_counterfactuals` |
| Tests | 18 new tests in `test_reunderwrite_and_book_loss.py` |

Suite: **860 tests + 93 subtests, all green**.

---

## Explicitly rejected (do not build)
Thematic anticipation trades as catalyst entries (wrong-signed; DKNG class → shadow book only — covered by validate gates in pm_tools); macro pattern library as a trading signal; more LLM context/authority; LLM self-grading loops; naive small-cap tilt without the event trigger.

## Done criteria
Suite green after every item; one commit per item; this doc updated with a ✅ + commit hash per item as it lands.
