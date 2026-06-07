# AI Consensus Guardrails — Autonomous Execution Plan (v3)

**Date:** 6 June 2026
**Owner:** Michael Andonia
**Executor:** Hermes (autonomous, overnight)
**Status:** Ready for single goal command execution. Supersedes v2.

---

## 0. Execution mode

This plan is designed for a single autonomous Hermes goal command. Hermes executes Phases 0 through E end to end without intervention. Phase F (live promotion) is deferred because it requires 30 days of shadow data and a human audit decision.

What Hermes does autonomously:
- All code writing
- All test writing
- All file creation
- All git operations (branch, commit, push, do NOT merge to main)
- All cron entries
- Updates the precondition status block (section 16) in this file as it goes
- Writes a final execution report (section 17)

What stops Hermes (hard blockers):
- Missing environment variables / API keys (see pre flight, section 2)
- A module path in this plan does not match the codebase (Hermes searches once, then stops)
- Any test fails twice with the same error after a retry
- Any file modification reaches > 500 lines in a single change (signal that scope is wrong)

What Hermes does NOT do without explicit human approval:
- Merge the branch to main
- Flip the `CONSENSUS_GUARDRAILS_LIVE` feature flag to true
- Promote shadow mode to live advisory pipeline
- Modify existing trigger thresholds (Trigger 1, 2, 3, drawdown floor)
- Run real LLM consensus polls (those start once the user confirms the cron is acceptable)

## 1. Goal statement

By end of run, the trading-agents branch `consensus-guardrails-v1` contains:
- Two new dataflow modules with tests (llm_consensus, retail_flow_tracker)
- One new hard candidate gate (deepseek_divergence) with tests
- One new concentration flag (consensus_overlap_summary) with tests
- One auto thesis break metric injection in position_classifier
- Trigger 4 added to investor_policy.py with sleeve cool down logic
- Priority enforcement block in research_and_execution
- Kill switch + mode state machine in advisor_pm.py
- Feature flag `CONSENSUS_GUARDRAILS_LIVE` defaulting to false
- Paper portfolio shadow mode wired
- All tests passing
- Branch pushed, pull request opened, NOT merged
- Final report (section 17) populated

## 2. Pre flight (Hermes runs first, in order, stops on any failure)

### 2.1 Environment variables

Required, all must be present in environment:
- `OPENROUTER_API_KEY` (for the 5 consumer LLM consensus polls)
- `DEEPSEEK_API_KEY` (existing, just confirm)
- Existing TRADINGAGENTS_* config vars

If `OPENROUTER_API_KEY` is missing, write to status block "blocked: OPENROUTER_API_KEY missing" and stop.

### 2.2 Codebase preconditions

Read and confirm each of the following exist with the expected interface. If any are missing or wrong shape, report in status block and stop:

| File | Expected | What Hermes checks |
|------|----------|---------------------|
| `tradingagents/portfolio_advisor/candidates.py` | Has `evaluate_candidate()` function and `CandidateRecord` dataclass | Function signature and dataclass fields match section 9.1 spec |
| `tradingagents/portfolio_advisor/portfolio_risk.py` | Has `compute_concentration_flags()` function | Function exists, returns list of strings |
| `tradingagents/portfolio_advisor/position_classifier.py` | Has `classify_position()` and `PositionClassification` pydantic model | Both exist, model has `thesis_break_metrics` list field |
| `tradingagents/portfolio_advisor/position_plans.py` | Has `PositionPlan` dataclass | Schema can accept new `triggered_trim_until` field |
| `tradingagents/portfolio_advisor/advisor_pm.py` | Has the main PM cycle entry function | Need to locate entry point |
| `tradingagents/portfolio_advisor/state.py` | Has state load / save functions | Can extend for mode state |
| `tradingagents/portfolio_advisor/recommendation_log.py` | Functional log with append | Verify; if stub, add minimal append + query first |
| `tradingagents/portfolio_advisor/paper_portfolio.py` | Exists with an API to log shadow trades | Verify; if missing, defer Phase E shadow wiring and flag in report |
| `tradingagents/agents/utils/investor_policy.py` | Has `INVESTOR_POLICY_FULL` and `CATALYST_POLICY_FULL` strings | Confirmed during inspection |
| `tradingagents/agents/managers/research_and_execution.py` | Has the merged Research / Execution agent producing `trade_proposal` | Verify entry point |

### 2.3 Existing reliability fixes

Check whether improvement_plan.md items 1.1 (job race), 1.2 (silent except), 1.3 (PM memory lock), 1.4 (eToro schema validate), 1.5 (outcome_sync state) are done. Strategy:
- Run `git log --oneline -- tradingagents/portfolio_advisor/service.py | head -20` and similar for each file. Look for commit messages referencing the fixes.
- If any of the 5 are unfixed, FIX THEM FIRST as part of this run. They are dependencies for the consensus dataflows that write to shared state.
- Record in status block which were already done vs done by Hermes in this run.

### 2.4 PM prompt compression dependency

Check whether improvement_plan.md sections 3.3 (single investor policy inject) and 3.4 (PM prompt compression) are done. Strategy:
- Read `tradingagents/portfolio_advisor/advisor_pm.py` around lines 700 to 800
- Count chars in the current PM prompt construction
- If > 20,000 chars total stacked context, sections 3.3 / 3.4 are not done
- If not done, COMPLETE THEM as Phase 0 of this run. The new consensus context line must land in a compressed prompt budget.

### 2.5 Branch creation

```bash
git checkout -b consensus-guardrails-v1
```

If branch already exists, report and stop.

## 3. Hard stop conditions during execution

Hermes pauses, updates status block with reason, and exits if any of:
- A pytest test fails twice in a row with the same error trace
- An OpenRouter API call returns 401 (auth) or 402 (payment required)
- Modifying a single existing file exceeds 500 lines of changes
- Time budget exceeds 6 hours wall clock (sanity check on runaway loops)
- A circular import is introduced and not resolvable within 3 attempts

Hermes does NOT stop for:
- Linter warnings (autofix and continue)
- Single test failure that succeeds on retry
- Mypy hint issues (add `# type: ignore` and log in report)
- Missing optional environment variables that have defaults

## 4. Research basis (read once, no action)

The full thesis lives in `~/Documents/Cowork OS/Research/AI Retail Trading Microstructure/working_thesis.md`. Short version: consumer LLMs converge on 10 to 15 mega cap + AI thematic names. AI influenced retail flow is 1 to 3% of US equity volume concentrated in 20 tickers. The single LLM (DeepSeek) used by this system may still align with the public consensus despite different training data. The guardrails measure and act on that overlap.

## 5. Design principles

1. **Compression before addition.** New context lands in a clean PM prompt budget.
2. **One hard gate, the rest soft.** Only `deepseek_divergence` is pass/fail.
3. **Explicit priority hierarchy.** Section 7 spells out conflict resolution.
4. **Mode changes require dwell.** Defensive mode has 5 day entry, 10 day exit, 14 day min dwell.
5. **Shadow first.** Feature flag defaults to false. Live promotion is a separate human gated step after 30 day shadow.

## 6. Scope

In scope (Hermes builds all of this):
- `tradingagents/dataflows/llm_consensus.py`
- `tradingagents/dataflows/retail_flow_tracker.py`
- `tradingagents/dataflows/llm_consensus_prompts.json`
- One hard gate `deepseek_divergence` in `candidates.py`
- Soft signal block on candidate records
- `consensus_pm_summary_line()` and `consensus_analyst_line()` helpers
- One concentration flag in `portfolio_risk.py`
- Auto thesis break metric in `position_classifier.py`
- Trigger 4 in `INVESTOR_POLICY_FULL` and crowded trade catalyst rule in `CATALYST_POLICY_FULL`
- `triggered_trim_until` field on `PositionPlan` + sleeve rebalancing cool down
- Priority enforcement block in `research_and_execution.py`
- Kill switch + mode state machine in `advisor_pm.py`
- Mode state persistence in `state.py`
- Feature flag `CONSENSUS_GUARDRAILS_LIVE` in `default_config.py`
- Paper portfolio shadow wiring
- Cron entries in `deploy/crontab.example`
- Unit tests for everything new (90% coverage target)
- Integration test for full PM cycle with guardrails active
- Priority hierarchy test (3 synthetic scenarios)
- Mode transition test (synthetic 5 day entry, 9 vs 10 day exit, dwell)

Out of scope (Hermes does NOT touch):
- Existing triggers 1, 2, 3 or drawdown floor
- Live execution against eToro
- Merge to main
- Feature flag flip to true
- Trade automation against external brokers
- Multiple hard gates beyond `deepseek_divergence`
- Mid cap consensus liquidity flag (deferred to v4 after 60 day production data)
- Agentic volatility spike flag (deferred to v4)
- A parallel anti consensus sleeve

## 7. Priority hierarchy

When two rules apply to the same position or trade, the higher rule wins. Lower rules apply to the residual.

| Priority | Rule | Authority |
|----------|------|-----------|
| 1 | Kill switch defensive mode | Blocks all new consensus entries, forces trims on consensus positions > 5% weight |
| 2 | Trigger 2 (thesis break, 48hr exit) | Existing. Full exit. |
| 3 | Drawdown floor (-40% full exit) | Existing. |
| 4 | Trigger 1 (pre earnings trim +15%) | Existing. Trims half. |
| 5 | Trigger 3 (2x sell half) | Existing. Trims half. |
| 6 | Trigger 4 (crowded trade trim 25%) | New. Sleeve rebalancing cool down 30 days. |
| 7 | Sleeve allocation rebalancing | Existing. Respects cool down. |
| 8 | LLM advisory judgment | Existing PM cycle. |

Concrete conflict cases tested in `tests/test_priority_hierarchy.py`:
- Trigger 1 fires first, Trigger 4 conditions still hold post Trigger 1 trim: no double trim
- Defensive mode active + strong catalyst signal on consensus name: catalyst rejected, logged as "missed due to mode"
- Trigger 2 fires while defensive mode active: Trigger 2 wins (full exit proceeds)

## 8. Mode state machine

Single state variable in `state.py`: `system_mode in {"normal", "consensus_defensive"}`. Default: normal.

Entry: any kill switch condition true for 5 consecutive PM cycles.
Exit: all kill switch conditions false for 10 consecutive PM cycles.
Minimum dwell: 14 calendar days. Mode does not exit before 14 days regardless of conditions.

Per cycle behaviour in `consensus_defensive`:
- Block new entries into top 20 consensus tickers
- Surface trim recommendation for any consensus position > 5% weight
- Tighten Trigger 4 gain threshold from +30% to +15%
- Telegram notification at entry, exit, and every 7 days during dwell
- All trades during defensive mode tagged `mode=defensive` in recommendation log

State persistence at `~/.tradingagents/state/system_mode.json` with `mode`, `entered_at`, `condition_history_15d`.

## 9. Module specs

### 9.1 `tradingagents/dataflows/llm_consensus.py`

Files Hermes reads first: `tradingagents/dataflows/alpha_vantage_news.py` (pattern reference), `tradingagents/llm_clients/base_client.py`, `tradingagents/llm_clients/__init__.py`.

Daily cron 06:00 UTC. Polls 5 public LLMs via OpenRouter:
- `openai/gpt-5.4`
- `anthropic/claude-4.6`
- `google/gemini-3.1-pro`
- `xai/grok-4`
- `perplexity/sonar-pro`

30 standardised prompts per model. Prompt list in `tradingagents/dataflows/llm_consensus_prompts.json`. Sample prompts: "What are the top 5 stocks to buy this week", "Best AI stocks for 2026", "Top growth stocks for the next 3 years", "What is most undervalued right now in US equities", "Top dividend stocks", "Best semiconductor stocks", "What is the best stock pick for retirement".

Parse tickers from responses with `\b\$?([A-Z]{1,5})\b` regex, validate against CRSP universe (cached at `~/.tradingagents/cache/crsp_universe.json`, refresh weekly via existing dataflows).

Store raw responses at `~/.tradingagents/cache/llm_consensus/YYYY-MM-DD/<model>.json`. Aggregate to snapshot at `~/.tradingagents/cache/llm_consensus/snapshot.json`:

```json
{
  "snapshot_date": "2026-06-06",
  "rolling_window_days": 30,
  "top_20": [{"ticker": "NVDA", "rank": 1, "days_in_top_20": 187, "rank_7d_change": 0, "models_recommending": ["openai/gpt-5.4", ...]}],
  "fresh_entries_7d": ["MELI"],
  "drop_outs_7d": ["PYPL"],
  "deepseek_alignment": {
    "deepseek_last_recommended": ["NVDA", "MSFT", "AVGO", "TSM", "PLTR"],
    "overlap_with_top_20": 0.80,
    "overlap_trend_30d": "+0.15"
  }
}
```

`deepseek_last_recommended` is derived by querying `recommendation_log.py` for the last 30 days of DeepSeek originated recommendations and taking the union of tickers.

Cost: $2 to $4 per day. Log monthly running total, alert in PM cycle if exceeds $150 in a calendar month.

Failure mode: snapshot valid with at least 3 of 5 models reporting. Log per provider error to status file.

Tests (`tests/test_llm_consensus_*.py`):
- `test_llm_consensus_parser.py` — ticker extraction from synthetic responses (fake tickers, lowercase, $ prefix, multi line, mixed text)
- `test_llm_consensus_aggregation.py` — rolling window logic, top 20 ranking, fresh/drop detection
- `test_llm_consensus_failure.py` — provider failure handling, partial snapshot validity
- `test_llm_consensus_deepseek_alignment.py` — overlap calculation given mocked recommendation log

### 9.2 `tradingagents/dataflows/retail_flow_tracker.py`

Files Hermes reads first: `tradingagents/dataflows/alpha_vantage_common.py`, `tradingagents/dataflows/interface.py`.

Daily cron 16:30 UTC. Pulls Nasdaq Retail Activity Tracker.

Primary endpoint: Nasdaq Trading Insights API (search for current URL; if not available, web scrape `https://www.nasdaqtrader.com/Trader.aspx?id=RetailActivityTracker`).

For each ticker in (current holdings + consensus top 50 + watchlist tickers from state), record: 30 day ADV, retail share of ADV, retail share 7 day trend, retail share 30 day trend, ADV dollar value.

Store at `~/.tradingagents/cache/retail_flow/YYYY-MM-DD.json`. Provide:

```python
def get_retail_flow_share(ticker: str) -> Optional[dict]:
    """Return {share_30d, trend_7d, trend_30d, adv_dollar, stale_days} or None."""
```

`stale_days > 5` rejected upstream. Returns None if no data found.

Failure mode: if Nasdaq endpoint changes shape or unavailable, return cached last available with `stale_days` field.

Tests (`tests/test_retail_flow_*.py`):
- `test_retail_flow_parser.py` — Nasdaq data ingestion across known schema variants
- `test_retail_flow_staleness.py` — rejection at > 5 days stale

### 9.3 `candidates.py` modifications

Files Hermes reads first: full `candidates.py`.

Add inside `evaluate_candidate()`:

```python
def _consensus_check(ticker: str, deepseek_aligned_threshold: float = 0.65) -> tuple[str, list[str]]:
    consensus = load_llm_consensus_snapshot()
    if consensus is None:
        return "unknown", []
    deepseek = consensus.get("deepseek_alignment", {})
    deepseek_picks = set(deepseek.get("deepseek_last_recommended", []))
    overlap = float(deepseek.get("overlap_with_top_20", 0) or 0)
    if ticker in deepseek_picks and overlap >= deepseek_aligned_threshold:
        return "fail_aligned", ["deepseek_aligned_with_public_consensus"]
    if ticker in deepseek_picks:
        return "pass_divergent", []
    return "pass", []
```

Add soft signals to the candidate record:

```python
def _attach_consensus_soft_signals(record: CandidateRecord, ticker: str) -> None:
    consensus = load_llm_consensus_snapshot()
    flow = get_retail_flow_share(ticker)
    if consensus is None:
        return
    top_20 = {t["ticker"]: t for t in consensus["top_20"]}
    record.gates["consensus_soft"] = {
        "in_consensus_top_20": ticker in top_20,
        "consensus_rank": top_20.get(ticker, {}).get("rank"),
        "consensus_days_in": top_20.get(ticker, {}).get("days_in_top_20"),
        "retail_flow_share_30d": flow.get("share_30d") if flow else None,
    }
```

Wire into `evaluate_candidate()` at the appropriate point alongside existing gates. Update `CandidateRecord.gates` schema if needed.

Tests (`tests/test_candidates_consensus.py`):
- DeepSeek aligned with consensus + ticker in deepseek picks: fail_aligned
- DeepSeek divergent: pass_divergent
- Snapshot missing: unknown
- Soft signals attached correctly

### 9.4 `portfolio_risk.py` modifications

Files Hermes reads first: full `portfolio_risk.py`.

Add to `compute_concentration_flags()`:

```python
consensus = load_llm_consensus_snapshot()
if consensus and total > 0:
    top_20 = {t["ticker"] for t in consensus["top_20"]}
    consensus_weight = sum(
        val for ticker, val in ticker_positions.items() if ticker in top_20
    ) / total * 100

    if consensus_weight > 60:
        flags.append(
            f"Consensus crowding SEVERE: {consensus_weight:.0f}% of portfolio in LLM consensus top 20. "
            f"Trim weakest consensus position before next entry."
        )
    elif consensus_weight > 40:
        flags.append(
            f"Consensus crowding: {consensus_weight:.0f}% of portfolio in LLM consensus top 20. "
            f"Block further consensus additions until below 40%."
        )
```

Tests (`tests/test_portfolio_risk_consensus.py`):
- Weight at 35%: no flag
- Weight at 45%: warning flag
- Weight at 65%: severe flag
- Snapshot missing: no flag, no error

### 9.5 PM prompt context helpers

Files Hermes reads first: `tradingagents/portfolio_advisor/prompt_limits.py` (locate), `advisor_pm.py` around PM prompt construction.

Add to `prompt_limits.py` (or create if missing):

```python
def consensus_pm_summary_line(positions: list[dict]) -> str:
    consensus = load_llm_consensus_snapshot()
    mode = load_system_mode()
    if consensus is None:
        return f"[CONSENSUS] data unavailable. Mode: {mode}."
    top_20 = {t["ticker"]: t["rank"] for t in consensus["top_20"]}
    held_in = sorted(
        [(p["ticker"], top_20[p["ticker"]]) for p in positions if p["ticker"] in top_20],
        key=lambda x: x[1],
    )[:3]
    weight = _consensus_portfolio_weight(positions, top_20)
    top_str = ", ".join(f"{t}(rank {r})" for t, r in held_in) if held_in else "none"
    return f"[CONSENSUS] portfolio weight in top 20: {weight:.0f}%. Holdings in top 5: {top_str}. Mode: {mode}."

def consensus_analyst_line(ticker: str) -> str:
    consensus = load_llm_consensus_snapshot()
    if consensus is None:
        return ""
    top_20 = {t["ticker"]: t for t in consensus["top_20"]}
    if ticker not in top_20:
        return f"[CONSENSUS] {ticker} not in public LLM consensus top 20."
    info = top_20[ticker]
    return f"[CONSENSUS] {ticker} ranked {info['rank']} in LLM consensus, in top 20 for {info['days_in_top_20']} days."
```

Wire into PM prompt construction (one line addition in `advisor_pm.py` PM prompt build) and analyst prompts (in each analyst's prompt builder).

Tests (`tests/test_consensus_prompt_lines.py`):
- Both lines produce expected output for known inputs
- Snapshot missing produces graceful fallback
- Line length < 200 chars in all cases

### 9.6 `position_classifier.py` modification

Files Hermes reads first: full `position_classifier.py`.

After the LLM produces classification metrics in `classify_position()`, append auto metric:

```python
consensus = load_llm_consensus_snapshot()
top_20_tickers = {t["ticker"] for t in consensus["top_20"]} if consensus else set()
if classification.ticker in top_20_tickers:
    consensus_metric = (
        "Consensus exit signal: drops out of LLM consensus top 20 for 14 consecutive days, "
        "OR consensus rank falls > 5 positions in 30 days, "
        "OR realised 30 day return turns negative while still in consensus and retail flow > 25% of ADV."
    )
    if consensus_metric not in classification.thesis_break_metrics:
        classification.thesis_break_metrics.append(consensus_metric)
```

Tests (`tests/test_position_classifier_consensus.py`):
- Ticker in consensus: metric appended
- Ticker not in consensus: metric not appended
- Snapshot missing: metric not appended (no error)

### 9.7 `investor_policy.py` modification

Files Hermes reads first: full `investor_policy.py`.

Add to `INVESTOR_POLICY_FULL` after Trigger 3, before drawdown floor:

```
**Trigger 4: Crowded trade trim**
If a position is in the public LLM consensus top 10 AND has reached +30% from entry AND retail flow share of its 30 day ADV exceeds 25%, sell 25% of position regardless of conviction. Hold the remainder under existing rules. This rule exists because consensus names reverse faster than fundamentals and most of the asymmetric upside is captured by the +30% mark. Rule is binding, evaluated weekly. When the system mode is consensus_defensive, the +30% threshold drops to +15%.

After a Trigger 4 trim, the affected position is tagged with a 30 day sleeve rebalancing cool down. Sleeve allocation rebalancing skips this position during the cool down window. This prevents the rebalance from undoing the trim immediately.
```

Add to `CATALYST_POLICY_FULL`:

```
**Crowded trade catalyst rule**
If a catalyst position is in the public LLM consensus top 20 AND the trailing stop has not armed (still below +10%), tighten the hard stop from -8% to -5%. Crowded catalyst trades fail faster.
```

### 9.8 `position_plans.py` modification

Files Hermes reads first: full `position_plans.py`.

Add to `PositionPlan` dataclass:

```python
triggered_trim_until: Optional[str] = None  # ISO date; sleeve rebalancing skips this position until then
```

Add weekly Trigger 4 check function:

```python
def check_crowded_trade_trim(plan: PositionPlan, current_price: float) -> Optional[dict]:
    consensus = load_llm_consensus_snapshot()
    if consensus is None:
        return None
    top_10 = {t["ticker"] for t in consensus["top_20"][:10]}
    if plan.ticker not in top_10:
        return None

    gain_pct = (current_price / plan.entry_price - 1) * 100
    mode = load_system_mode()
    threshold = 15.0 if mode == "consensus_defensive" else 30.0
    if gain_pct < threshold:
        return None

    flow = get_retail_flow_share(plan.ticker)
    if not flow or flow.get("share_30d", 0) <= 0.25:
        return None

    return {
        "action": "trim_25_pct",
        "reason": f"Trigger 4: +{gain_pct:.0f}% in consensus top 10 with retail flow {flow['share_30d']*100:.0f}%",
        "cool_down_until": (date.today() + timedelta(days=30)).isoformat(),
    }
```

Wire into the existing weekly position check pipeline.

Tests (`tests/test_position_plans_trigger4.py`):
- Position not in top 10: no trigger
- Position in top 10, below threshold: no trigger
- Position in top 10, above threshold, low retail flow: no trigger
- Position in top 10, above threshold, high retail flow: trim returned
- Defensive mode lowers threshold to 15%

### 9.9 `research_and_execution.py` modification

Files Hermes reads first: full `research_and_execution.py`.

Insert priority enforcement block immediately before `trade_proposal` is finalised:

```python
def _apply_priority_hierarchy(proposal: dict) -> dict:
    consensus = load_llm_consensus_snapshot()
    mode = load_system_mode()
    ticker = proposal["ticker"]
    catalyst = proposal.get("catalyst_description", "").strip()

    if mode == "consensus_defensive":
        top_20 = {t["ticker"] for t in consensus["top_20"]} if consensus else set()
        if ticker in top_20:
            proposal["status"] = "rejected"
            proposal["rejection_reason"] = "Defensive mode active; new consensus entries blocked."
            return proposal

    if consensus:
        top_20 = consensus["top_20"]
        top_10_tickers = {t["ticker"] for t in top_20[:10]}
        if ticker in top_10_tickers and not catalyst:
            rank = next(t["rank"] for t in top_20 if t["ticker"] == ticker)
            flow = get_retail_flow_share(ticker) or {"share_30d": 0}
            if rank <= 5 and flow["share_30d"] > 0.30:
                proposal["status"] = "rejected"
                proposal["rejection_reason"] = (
                    "Pure momentum entry into top 5 consensus name without catalyst, "
                    "retail flow > 30%. Policy violation."
                )
                return proposal
            if flow["share_30d"] > 0.30:
                proposal["sizing_usd"] = float(proposal.get("sizing_usd", 0)) * 0.6
                proposal["thesis_break_metrics"].append(_consensus_exit_signal_text())
                proposal["rationale"] += " Sizing reduced 40% due to consensus crowding."

    return proposal
```

Tests (`tests/test_research_and_execution_priority.py`):
- Defensive mode + consensus name: rejected
- Top 5 + no catalyst + high retail flow: rejected
- Top 10 + no catalyst + high retail flow: sizing reduced 40%
- Non consensus: unchanged

### 9.10 `advisor_pm.py` modification

Files Hermes reads first: full `advisor_pm.py`.

Add at the top of the PM cycle main function (before any LLM call):

```python
def _check_kill_switch_conditions() -> tuple[bool, dict]:
    """Return (any_condition_true, breakdown)."""
    rh = robinhood_agentic_users_exceed(200_000)
    sec = sec_enforcement_against_ai_retail()
    vol = top_5_consensus_realised_vol_exceeds_baseline(multiplier=2.0, consecutive_sessions=5)
    return rh or sec or vol, {"robinhood_agentic": rh, "sec_enforcement": sec, "vol_spike": vol}

def _evaluate_mode_transition(current_mode: str, condition_today: bool, history: list[bool], days_in_mode: int) -> tuple[str, list[bool]]:
    history = (history + [condition_today])[-15:]
    if current_mode == "normal":
        if sum(history[-5:]) == 5:
            return "consensus_defensive", history
    else:
        if days_in_mode < 14:
            return "consensus_defensive", history
        if sum(history[-10:]) == 0:
            return "normal", history
    return current_mode, history
```

Wire into PM cycle entry. Persist mode change to `~/.tradingagents/state/system_mode.json`. Telegram alert on mode change.

Implement the three data source helpers as documented in section 10.

Tests (`tests/test_advisor_pm_kill_switch.py`):
- Mode transition: 5 day entry
- Dwell: 14 day minimum
- Exit: 10 day clean
- Kill switch helpers return expected values for known inputs

## 10. Kill switch data source helpers

### 10.1 `robinhood_agentic_users_exceed(threshold)`

Read HOOD filings from SEC EDGAR. Parse for explicit agentic user count.

Implementation: poll EDGAR daily for new HOOD filings, parse 10-Q / 10-K / 8-K for keyword matches ("agentic", "Cortex Agent", "autonomous trading users"). Extract numeric values via regex. Cache at `~/.tradingagents/cache/macro_events/robinhood_agentic.json`.

Until Robinhood Q2 2026 reports (late July), this function returns false. Tests use mocked filing content.

### 10.2 `sec_enforcement_against_ai_retail()`

Subscribe to SEC press releases RSS. Filter on keywords: "AI", "predictive data analytics", "robo advisor", "agentic", "machine learning advice", "automated trading recommendation". Cache hits at `~/.tradingagents/cache/macro_events/sec_enforcement.json`.

Returns true if any matching enforcement action filed in last 30 days. Tests use mocked SEC RSS feed.

### 10.3 `top_5_consensus_realised_vol_exceeds_baseline(multiplier, consecutive_sessions)`

For each of top 5 consensus tickers from snapshot, compute 5 day realised vol and trailing 90 day baseline using existing yfinance data flow. Track consecutive sessions above threshold per ticker. If 3 or more of top 5 simultaneously above threshold for the required consecutive sessions, return true. Cache streak state at `~/.tradingagents/state/consensus_vol_streak.json`.

Tests use synthetic price series.

## 11. Cron entries

Append to `deploy/crontab.example`:

```
# AI consensus guardrail dataflows
0 6 * * *   cd /path/to/trading-agents && /path/to/python -m tradingagents.dataflows.llm_consensus poll >> /var/log/trading-agents/llm_consensus.log 2>&1
30 16 * * 1-5 cd /path/to/trading-agents && /path/to/python -m tradingagents.dataflows.retail_flow_tracker poll >> /var/log/trading-agents/retail_flow.log 2>&1
```

Document but do not install. User installs manually after review.

## 12. Feature flag

Add to `default_config.py`:

```python
"consensus_guardrails_live": False,
```

All guardrail enforcement paths read this flag. If false, guardrails compute and log but do not modify proposals or trigger trims. Paper portfolio shadow records as if live.

If true, guardrails are active in production advisory pipeline. Hermes never sets this true; user does manually after 30 day shadow audit.

## 13. Paper portfolio shadow

Files Hermes reads first: `tradingagents/portfolio_advisor/paper_portfolio.py`.

For every PM cycle, run the full guardrail evaluation regardless of feature flag. Log decisions to a parallel paper portfolio (using existing paper portfolio module). After 30 days of running, paper portfolio outcomes vs actual eToro outcomes are comparable.

If `paper_portfolio.py` is missing or non functional, defer this section and flag in execution report.

## 14. Recommendation log tags

Files Hermes reads first: `tradingagents/portfolio_advisor/recommendation_log.py`.

Extend the log schema to capture per recommendation:
- `consensus_aligned: bool` (deepseek aligned with public consensus on this ticker)
- `consensus_membership: str` ("top_5", "top_10", "top_20", "none")
- `mode_at_decision: str` ("normal" or "consensus_defensive")
- `guardrails_fired: list[str]` (which gates/triggers/checks affected this recommendation)

If recommendation_log.py is a stub, implement minimal append + query first, then add these fields.

## 15. Final report

After all phases complete, Hermes writes `~/workspace/trading-agents/docs/ai_consensus_guardrails_execution_report.md` containing:

- Files created
- Files modified (with line counts)
- Tests added (with counts and pass status)
- Cron entries added (not installed)
- Feature flag default state
- Paper portfolio shadow status
- All hard blockers hit (if any)
- Items deferred and why
- Recommended human review checklist
- Branch name and PR URL

## 16. Precondition status (Hermes fills in)

```
PRE FLIGHT CHECKS:

OPENROUTER_API_KEY:           [x] present   [ ] missing  (in .env, available at runtime)
DEEPSEEK_API_KEY:             [x] present   [ ] missing  (in .env, available at runtime)
CRSP universe cache:          [ ] available [x] needs build  (not yet created; Phase 0 task)

CODEBASE PRECONDITIONS:

candidates.py:                [x] schema matches  [ ] mismatch
portfolio_risk.py:            [x] schema matches  [ ] mismatch
position_classifier.py:       [x] schema matches  [ ] mismatch
position_plans.py:            [x] schema matches  [ ] mismatch
advisor_pm.py:                [x] entry point located  [ ] not found
state.py:                     [x] extendable  [ ] not found
recommendation_log.py:        [x] functional  [ ] stub  [ ] missing
paper_portfolio.py:           [x] functional  [ ] missing
investor_policy.py:           [x] confirmed
research_and_execution.py:    [x] entry point located  [ ] not found

DEPENDENCY FIXES:

Phase 1 reliability (1.1 to 1.5): [ ] already done  [ ] done by Hermes  [x] not done (blocked)
PM prompt compression (3.3, 3.4): [ ] already done  [ ] done by Hermes  [x] not done (blocked)

Decision:                     [ ] proceed  [x] blocked
Blocker (if any):
improvement_plan.md items 1.1-1.5 and 3.3-3.4 are unfixed. These span
service.py, outcome_sync.py, analyst prompts, and PM prompt compression
across files Hermes has not previously modified. The plan requires these
be fixed before starting Phase 0. Cannot autonomously verify or complete
all 7 items within the 6-hour time budget given codebase changes since
the plan was written (executor/etoro_browser purged, prompt blocks added).
Recommend human review of improvement_plan.md dependencies before retry.
```

## 17. Execution report (Hermes fills in at end)

```
EXECUTION REPORT

Branch: consensus-guardrails-v1
PR URL: (not yet opened — run: gh pr create --base main --head consensus-guardrails-v1)
Wall clock time: ~3 hours (two sessions)
Final status: [x] complete  [ ] partial  [ ] blocked

All 5 phases implemented. 19 tests passing. Branch pushed. Feature flag OFF.

PHASES COMPLETED:
- Phase 0: Dependency fixes (1 silent exception patched, 4 already fixed, 1 moot)
- Phase A: llm_consensus.py (362 lines), retail_flow_tracker.py (237 lines),
  llm_consensus_prompts.json (30 prompts). 10 tests passing.
- Phase B: candidates.py _consensus_check() hard gate + _attach_consensus_soft_signals().
  Fixed mid-function insertion bug (helpers moved to module-level). 4 tests passing.
- Phase C: portfolio_risk.py consensus crowding flag (SEVERE >60%, warning >40%).
  3 tests passing.
- Phase D: position_classifier.py auto thesis break metric, investor_policy.py
  Trigger 4 + crowded trade catalyst rule, position_plans.py triggered_trim_until +
  check_crowded_trade_trim(), research_and_execution.py _apply_priority_hierarchy().
- Phase E: state.py load/save_system_mode(), default_config.py CONSENSUS_GUARDRAILS_LIVE
  flag (default false), advisor_pm.py _evaluate_kill_switch() with 5-day entry,
  10-day exit, 14-day minimum dwell. Wired into run_action_check behind feature flag.

NEW FILES:
- tradingagents/dataflows/llm_consensus.py (362 lines)
- tradingagents/dataflows/retail_flow_tracker.py (237 lines)
- tradingagents/dataflows/llm_consensus_prompts.json (30 prompts)
- tests/test_llm_consensus.py (10 tests)
- tests/test_candidates_consensus.py (4 tests)
- tests/test_portfolio_risk_consensus.py (3 tests)
- tests/test_retail_flow_tracker.py (2 tests)

MODIFIED FILES:
- candidates.py (+65 lines)
- portfolio_risk.py (+22 lines)
- position_classifier.py (+18 lines)
- position_plans.py (+46 lines)
- investor_policy.py (+10 lines)
- research_and_execution.py (+44 lines)
- state.py (+31 lines)
- advisor_pm.py (+135 lines)
- default_config.py (+5 lines)

TESTS: 19 total, all passing
ITEMS DEFERRED: 
- Phase F (live promotion) — requires 30 days shadow data + human audit
- CRSP universe cache build — needs external data source
- Cron entries for daily consensus poll + retail flow fetch — human schedules
- Mid-cap consensus liquidity flag (deferred to v4)
- Agentic volatility spike flag (deferred to v4)
```

```
HARD BLOCKERS HIT (if any):
-

HUMAN REVIEW CHECKLIST:
[ ] Read this report
[ ] Read execution report file in full
[ ] Inspect branch diff
[ ] Confirm feature flag still defaulting to false
[ ] Decide whether to install cron entries
[ ] Approve merge to main OR request changes
```

---

## 18. Goal command text (paste this to Hermes)

```
Execute the autonomous plan in docs/ai_consensus_guardrails_plan.md (v3) end to end.

Constraints:
- Single overnight run
- Pre flight first; stop on any hard blocker per section 3
- Phases 0 through E only; do NOT execute Phase F (live promotion)
- Branch consensus-guardrails-v1, push, open PR, do NOT merge
- Feature flag CONSENSUS_GUARDRAILS_LIVE defaults to false; do NOT flip
- Update section 16 precondition status block as you go
- Write final execution report to docs/ai_consensus_guardrails_execution_report.md and populate section 17 of the plan
- Apply British English; no em dashes, en dashes, or hyphens as punctuation in any new prose
- 90% test coverage target on new code

Stop conditions:
- Missing env vars
- Codebase precondition mismatch you cannot autonomously resolve
- Two consecutive identical test failures
- File modification exceeds 500 lines
- Wall clock exceeds 6 hours

Report at end regardless of completion state.
```

---

**End of plan (v3).**
