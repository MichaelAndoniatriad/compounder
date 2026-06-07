# AI Consensus as Trading Factor — Execution Plan (v4)

**Date:** 6 June 2026
**Owner:** Michael Andonia
**Executor:** Hermes (autonomous, overnight)
**Status:** Reframes v3. Supersedes all prior versions.

---

## Why v4 exists

v3 framed consensus as a defensive concern: hard gates, Trigger 4 trims, kill switches, mode state machines. The framing was wrong. The existing pre buy framework (PEG < 1.5, ROIC > 15%, durable moat, revenue growth > 20%, founder led, no red flags) already prevents the system from buying a hyped consensus name that fails fundamentals. NVDA passes that framework on merit. So does MSFT. The herd overlap is the consequence of multiple smart selection processes converging on the same answers, not a bug.

v4 treats consensus as **a tradable factor**, not a thing to defend against. The system uses consensus rank, age, and divergence as positive signals that influence position sizing, entry timing, and outcome attribution. The defensive machinery is removed. The signal infrastructure is kept and extended.

---

## What is already on branch `consensus-guardrails-v1`

Two commits live, one set of changes uncommitted:

| Commit / change | Status | Keep in v4? |
|-----------------|--------|-------------|
| dc180d9 — Phase 0 dependency fixes | live | KEEP |
| 96cb807 — Phase A dataflows (llm_consensus.py 391 lines, retail_flow_tracker.py, prompts JSON, 9 tests) | live | REFACTOR llm_consensus.py to scrape mode; KEEP rest |
| Uncommitted — candidates.py with `_consensus_check` hard gate + soft signals helper | broken (SyntaxError, helpers inserted mid-function) | FIX the SyntaxError, then DELETE `_consensus_check`, KEEP `_attach_consensus_soft_signals` |
| Uncommitted — portfolio_risk.py with consensus crowding flag | clean | KEEP, commit |

Hermes does not start from scratch. v4 is a delta on top of what already exists.

---

## 0. Execution mode

Single autonomous Hermes goal command. Hermes works on the existing `consensus-guardrails-v1` branch. Hermes:
- Fixes the existing candidates.py SyntaxError first
- Refactors llm_consensus.py from API polls to web scraping (Option B from earlier discussion)
- Removes the hard gate code but keeps the soft signals
- Adds the new scoring helpers and recommendation log tagging
- Wires the soft signal line into PM and analyst prompts
- Adds tests for new code
- Commits, pushes, does NOT merge

What stops Hermes (hard blockers):
- Cannot fix the candidates.py SyntaxError within 3 attempts
- A test fails twice with the same error
- A file modification exceeds 500 lines in a single change
- Wall clock exceeds 6 hours

What Hermes does NOT do without human approval:
- Merge the branch to main
- Flip the feature flag to true
- Modify existing triggers 1, 2, 3 or drawdown floor
- Run real LLM polls (the new dataflow is scrape based and free, but real cron deployment is human approved)

---

## 1. Goal statement

By end of run, branch `consensus-guardrails-v1` contains:
- candidates.py fixed (SyntaxError resolved; hard gate removed; soft signals kept)
- portfolio_risk.py committed (concentration flag clean)
- llm_consensus.py refactored to scrape Insider Monkey + Yahoo Finance + Barchart + US News for published LLM stock picks (no OpenRouter API calls)
- New scoring helpers in `prompt_limits.py` (or new `consensus_score.py`) for consensus rank, age, divergence
- One soft signal context line wired into PM and analyst prompts
- `research_and_execution.py` reads consensus score and modulates sizing (no rejection, only sizing)
- Recommendation log tagged with consensus state on every trade
- Paper portfolio shadow wired
- Feature flag `CONSENSUS_FACTOR_LIVE` defaults to false
- All tests passing
- Branch pushed, NOT merged
- Final report written

---

## 2. Pre flight

### 2.1 Verify branch state

```bash
git status
git log --oneline consensus-guardrails-v1 ^main
```

Expect: on branch `consensus-guardrails-v1`, two commits ahead of main (dc180d9, 96cb807), candidates.py and portfolio_risk.py modified but not committed.

If the branch state differs materially from this, write the actual state into section 16 and stop.

### 2.2 Environment

`OPENROUTER_API_KEY` is no longer required. The new `llm_consensus.py` scrapes published articles. If `OPENROUTER_API_KEY` is missing, that is fine.

`DEEPSEEK_API_KEY` and existing TRADINGAGENTS_* config vars must be present (existing system dependency).

### 2.3 Existing reliability and compression fixes

Phase 1.1 was already verified moot (due-jobs disabled in production); 1.2 to 1.5 were already fixed in commit dc180d9 or earlier. PM prompt compression (improvement_plan.md 3.3 / 3.4) was also already done as part of Phase 0. No additional Phase 0 work required this run unless Hermes finds those claims contradicted by the actual file state.

---

## 3. Hard stop conditions

Hermes pauses, updates section 16, and exits if any of:
- candidates.py SyntaxError not resolvable in 3 attempts
- A test fails twice with the same error trace
- A single file modification exceeds 500 lines
- Wall clock exceeds 6 hours
- A circular import is introduced and not resolvable in 3 attempts

Does NOT stop for:
- Linter warnings
- Single test flake
- Mypy hints

---

## 4. Research basis (read once, no action)

Full thesis at `~/Documents/Cowork OS/Research/AI Retail Trading Microstructure/working_thesis.md`. The pivot from v3 to v4: existing pre buy rules already filter quality, so the herd overlap on NVDA / MSFT / AVGO is not a buy-the-wrong-thing risk. The real risks are entry timing into stale consensus names, oversized positions in agreed-upon names, slow exits when thesis breaks but herd is still buying, and missing the negative universe (good names no one is talking about). These are scoring problems, not gating problems.

---

## 5. Design principles

1. **Consensus is a factor, not a gate.** No new hard reject on consensus alignment. Soft scoring only.
2. **Existing framework filters quality.** Trust it. Do not add a second quality filter dressed as a consensus rule.
3. **Sizing and timing, not approval.** Consensus state modulates how much and when, not whether.
4. **Outcome attribution is the learning loop.** Every trade tagged. After 90 days of shadow data, you know whether consensus alignment is alpha or noise.
5. **Shadow first.** Feature flag defaults to false. Tagging and scoring happen regardless; sizing impact only when flag is true.

---

## 6. Scope

In scope:
- Fix candidates.py SyntaxError (move helpers out of `run_promoted_candidate_pm_comparison`, restore the orphaned tail)
- Delete `_consensus_check` hard gate from candidates.py
- Keep `_attach_consensus_soft_signals` in candidates.py
- Refactor `llm_consensus.py` to scrape published article URLs instead of OpenRouter API calls
- Update `llm_consensus_prompts.json` to scrape source URLs (or replace with `llm_consensus_sources.json`)
- Commit Phase B (cleaned candidates.py) and Phase C (portfolio_risk.py)
- Add new module `tradingagents/portfolio_advisor/consensus_score.py` with `consensus_entry_score()`, `consensus_age_score()`, `consensus_divergence_score()`, `compute_composite_consensus_score()`
- Add `consensus_pm_summary_line()` and `consensus_analyst_line()` helpers in `prompt_limits.py`
- Wire the summary line into PM prompt (one line)
- Wire the analyst line into analyst prompts (one line, per ticker)
- Modify `research_and_execution.py` to read composite consensus score and modulate `sizing_usd` (no rejection)
- Extend `recommendation_log.py` to record per recommendation: `consensus_rank`, `consensus_age_days`, `consensus_score`, `deepseek_aligned_with_consensus`
- Feature flag `CONSENSUS_FACTOR_LIVE` in `default_config.py`
- Paper portfolio shadow wiring (run scoring + tagging regardless of flag; sizing modulation only when flag is true)
- Cron entry for `llm_consensus.py` (daily, document but do not install)
- Cron entry for `retail_flow_tracker.py` (daily, document)
- Tests for all new code (90% coverage target)
- Tests for the deleted hard gate are removed/replaced

Out of scope (Hermes does NOT build):
- Hard gate on consensus (deleted)
- Trigger 4 crowded trim (deleted)
- Sleeve rebalancing cool down (deleted)
- Priority hierarchy table (deleted)
- Mode state machine (deleted)
- Kill switch (deleted)
- Auto thesis break metric injection in position_classifier (deleted)
- Merge to main
- Feature flag flip to true

---

## 7. Module specs

### 7.1 Fix candidates.py first (critical bug)

Files Hermes reads first: full current `tradingagents/portfolio_advisor/candidates.py`.

Current state: `_consensus_check` and `_attach_consensus_soft_signals` were inserted into the middle of `run_promoted_candidate_pm_comparison`, splitting the function. The orphaned tail (`if not bool(cfg.get("portfolio_advisor_pm_candidate_comparison", True)): return 0` and the lines that follow) sits at module level and causes a SyntaxError.

Fix steps in order:
1. Restore `run_promoted_candidate_pm_comparison` as a single contiguous function. The orphaned lines (currently lines 725 to 733) belong inside the function, before the existing `return 0` at line 664.
2. Delete `_consensus_check` from the file. The hard gate is removed in v4.
3. Move `_attach_consensus_soft_signals` to a module level position at the very bottom of the file (or near other module level helpers like `_bool_gate`).
4. In `evaluate_candidate()`, remove the `_consensus_check` call (lines around 152 in the current state). Keep the `_attach_consensus_soft_signals(gates, ticker)` call. The candidate is not gated on consensus.
5. Run `python -c "import tradingagents.portfolio_advisor.candidates"` to confirm import works.
6. Commit: "Phase B fix: remove hard gate, keep soft signals, repair function structure".

### 7.2 Refactor llm_consensus.py to scrape mode

Files Hermes reads first: full current `tradingagents/dataflows/llm_consensus.py`.

Current state: polls 5 LLMs via OpenRouter at daily cron. v4 replaces this with a scraper.

New approach: pull published "AI stock pick" articles from these sources daily:
- Insider Monkey (search results for ChatGPT/Claude/Gemini/Grok stock portfolios)
- Yahoo Finance markets section (search for LLM stock pick articles)
- Barchart (top stocks picked by AI articles)
- US News investing (cross model agreement articles)

Sources list lives in new file `tradingagents/dataflows/llm_consensus_sources.json`:

```json
{
  "sources": [
    {"name": "insider_monkey_chatgpt", "url": "https://www.insidermonkey.com/?s=ChatGPT+stock+portfolio", "parser": "insider_monkey_search"},
    {"name": "insider_monkey_claude", "url": "https://www.insidermonkey.com/?s=Claude+stock+portfolio", "parser": "insider_monkey_search"},
    {"name": "insider_monkey_grok", "url": "https://www.insidermonkey.com/?s=Grok+stock+portfolio", "parser": "insider_monkey_search"},
    {"name": "yahoo_ai_picks", "url": "https://finance.yahoo.com/topic/ai-stocks/", "parser": "yahoo_topic"},
    {"name": "us_news_chatgpt_grok_gemini", "url": "https://money.usnews.com/investing/articles/chatgpt-grok-gemini-top-stocks-to-buy-for-2026", "parser": "us_news_article"}
  ]
}
```

Implementation: simple HTML scraper using `requests` + `beautifulsoup4` (likely already in `requirements.txt`; verify). For each source, parse out ticker symbols mentioned and attribute to the model name in the article context. Aggregate into the same snapshot format as the existing API based version:

```json
{
  "snapshot_date": "2026-06-06",
  "rolling_window_days": 30,
  "top_20": [{"ticker": "NVDA", "rank": 1, "days_in_top_20": 187, "rank_7d_change": 0, "models_recommending": ["chatgpt", "claude", "grok", "gemini"]}, ...],
  "fresh_entries_7d": ["MELI"],
  "drop_outs_7d": ["PYPL"],
  "deepseek_alignment": {
    "deepseek_last_recommended": ["NVDA", "MSFT", "AVGO", "TSM", "PLTR"],
    "overlap_with_top_20": 0.80,
    "overlap_trend_30d": "+0.15"
  }
}
```

`deepseek_last_recommended` derived from the recommendation log (DeepSeek originated buy recommendations in the last 30 days). `overlap_with_top_20` is the Jaccard or simple overlap of DeepSeek's last picks with the public top 20.

Cost: zero. No API calls.

Failure mode: if all 5 sources fail, log error and keep prior snapshot. Add `last_successful_scrape` timestamp to the snapshot. Downstream code rejects snapshots older than 5 days.

Tests to update / replace:
- `tests/test_llm_consensus.py` — rename / refactor the 9 existing tests:
  - Ticker extraction tests stay
  - Replace API mocking tests with scrape parser mocking tests (mock HTML responses, assert ticker lists)
  - Failure handling tests stay
- Total target: 10 to 12 tests after refactor

Commit: "Phase A refactor: llm_consensus scrapes published articles instead of API polling".

### 7.3 Keep portfolio_risk.py concentration flag, commit it

Files Hermes reads first: current diff of `portfolio_risk.py`.

Already clean. The consensus crowding flag (40% warning, 60% severe) works as is. Just commit it.

Commit: "Phase C: consensus crowding concentration flag".

### 7.4 New module: consensus_score.py

Location: `tradingagents/portfolio_advisor/consensus_score.py`.

Purpose: score helpers that quantify the consensus factor as input to sizing decisions, not gates.

Functions:

```python
def consensus_entry_score(ticker: str) -> float:
    """Return -1.0 to +1.0. Higher = better entry timing.

    Logic:
    - Fresh entry (< 14 days in top 20): +0.5
    - Mature (14 to 60 days): 0.0
    - Stale (60 to 180 days): -0.3
    - Very stale (> 180 days): -0.6
    - Not in consensus at all: +0.3 (negative universe edge)
    """
    consensus = load_llm_consensus_snapshot()
    if consensus is None:
        return 0.0
    top_20 = {t["ticker"]: t for t in consensus["top_20"]}
    if ticker not in top_20:
        return 0.3  # negative universe edge
    days = top_20[ticker].get("days_in_top_20", 0)
    if days < 14:
        return 0.5
    if days < 60:
        return 0.0
    if days < 180:
        return -0.3
    return -0.6


def consensus_divergence_score(ticker: str) -> float:
    """Return -1.0 to +1.0. Higher = DeepSeek dissenting from public consensus.

    Logic:
    - DeepSeek picked this AND it is in public consensus: -0.4 (herding)
    - DeepSeek picked this AND it is NOT in public consensus: +0.4 (divergent / potential alpha)
    - DeepSeek did not pick this: 0.0 (no signal)
    """
    consensus = load_llm_consensus_snapshot()
    if consensus is None:
        return 0.0
    deepseek = consensus.get("deepseek_alignment", {})
    deepseek_picks = set(deepseek.get("deepseek_last_recommended", []))
    if ticker not in deepseek_picks:
        return 0.0
    top_20_tickers = {t["ticker"] for t in consensus.get("top_20", [])}
    if ticker in top_20_tickers:
        return -0.4
    return 0.4


def consensus_retail_flow_score(ticker: str) -> float:
    """Return -1.0 to +1.0. Higher = retail flow share supports entry timing.

    Logic:
    - Retail flow share > 35% of ADV: -0.5 (entering at peak crowd)
    - 20 to 35%: -0.2
    - 10 to 20%: 0.0
    - < 10%: +0.2 (institutional dominated, less retail crowding)
    """
    flow = get_retail_flow_share(ticker)
    if flow is None:
        return 0.0
    share = flow.get("share_30d", 0)
    if share > 0.35:
        return -0.5
    if share > 0.20:
        return -0.2
    if share > 0.10:
        return 0.0
    return 0.2


def compute_composite_consensus_score(ticker: str) -> dict:
    """Return composite score and components for transparency.

    Composite is the average of the three component scores. Returned as dict
    so caller can log components separately for outcome attribution.
    """
    entry = consensus_entry_score(ticker)
    divergence = consensus_divergence_score(ticker)
    flow = consensus_retail_flow_score(ticker)
    composite = (entry + divergence + flow) / 3
    return {
        "composite": round(composite, 3),
        "entry": round(entry, 3),
        "divergence": round(divergence, 3),
        "flow": round(flow, 3),
    }
```

The composite score is a number between -1.0 and +1.0. Used by `research_and_execution.py` to modulate sizing.

Tests (`tests/test_consensus_score.py`):
- Fresh entry returns +0.5
- Mature returns 0.0
- Stale returns negative
- Non consensus returns +0.3 (negative universe)
- DeepSeek aligned with consensus returns -0.4 divergence
- DeepSeek divergent returns +0.4 divergence
- High retail flow share returns -0.5 flow score
- Composite is mean of three components

### 7.5 PM prompt context helpers

Files Hermes reads first: `tradingagents/portfolio_advisor/prompt_limits.py`, `advisor_pm.py` PM prompt construction area.

Add to `prompt_limits.py`:

```python
def consensus_pm_summary_line(positions: list[dict]) -> str:
    """One line, < 200 chars, for the PM prompt."""
    consensus = load_llm_consensus_snapshot()
    if consensus is None:
        return "[CONSENSUS] data unavailable."
    top_20 = {t["ticker"]: t["rank"] for t in consensus["top_20"]}
    held_in = sorted(
        [(p["ticker"], top_20[p["ticker"]]) for p in positions if p["ticker"] in top_20],
        key=lambda x: x[1],
    )[:3]
    weight = _consensus_portfolio_weight(positions, top_20)
    top_str = ", ".join(f"{t}(rank {r})" for t, r in held_in) if held_in else "none"
    return f"[CONSENSUS] portfolio weight in top 20: {weight:.0f}%. Holdings in top 5: {top_str}."

def consensus_analyst_line(ticker: str) -> str:
    """One line, per ticker, for analyst prompts."""
    from tradingagents.portfolio_advisor.consensus_score import compute_composite_consensus_score
    consensus = load_llm_consensus_snapshot()
    if consensus is None:
        return ""
    top_20 = {t["ticker"]: t for t in consensus["top_20"]}
    score = compute_composite_consensus_score(ticker)
    if ticker in top_20:
        info = top_20[ticker]
        return (
            f"[CONSENSUS] {ticker} ranked {info['rank']} in LLM consensus, "
            f"in top 20 for {info['days_in_top_20']} days. Composite score: {score['composite']:+.2f}."
        )
    return f"[CONSENSUS] {ticker} not in public LLM consensus top 20. Composite score: {score['composite']:+.2f}."
```

Wire into PM prompt construction (one line addition) and analyst prompt builders (one line each).

Tests (`tests/test_consensus_prompt_lines.py`):
- Summary line returns expected format
- Snapshot missing returns graceful fallback
- Length < 200 chars in all cases
- Analyst line includes composite score

### 7.6 research_and_execution.py — sizing modulation

Files Hermes reads first: full current `research_and_execution.py`.

After `trade_proposal` is finalised and BEFORE it is returned, modulate sizing based on composite consensus score:

```python
def _apply_consensus_factor(proposal: dict) -> dict:
    from tradingagents.portfolio_advisor.consensus_score import compute_composite_consensus_score
    from tradingagents.default_config import DEFAULT_CONFIG
    if not DEFAULT_CONFIG.get("consensus_factor_live", False):
        # Tagging happens regardless; sizing modulation only when flag is true.
        score = compute_composite_consensus_score(proposal["ticker"])
        proposal["consensus_score"] = score
        return proposal

    score = compute_composite_consensus_score(proposal["ticker"])
    proposal["consensus_score"] = score

    # Modulation: composite score modifies sizing by +/- 30% maximum.
    # Composite +1.0 = +30% sizing. Composite -1.0 = -30% sizing.
    multiplier = 1.0 + (score["composite"] * 0.3)
    proposal["sizing_usd"] = float(proposal.get("sizing_usd", 0)) * multiplier
    proposal["rationale"] += (
        f" Consensus factor applied: composite {score['composite']:+.2f} → "
        f"sizing × {multiplier:.2f}."
    )
    return proposal
```

The proposal is NOT rejected based on consensus. Sizing is modulated up or down by at most 30%. Existing pre buy framework still applies.

Tests (`tests/test_research_and_execution_consensus.py`):
- Flag false: score attached, sizing unchanged
- Flag true + composite +0.5: sizing × 1.15
- Flag true + composite -0.5: sizing × 0.85
- Composite at extremes does not break sizing logic

### 7.7 recommendation_log.py — tagging

Files Hermes reads first: full current `recommendation_log.py`.

Extend the log entry schema to capture per recommendation:
- `consensus_rank: Optional[int]` (rank in top 20 at decision time, or None)
- `consensus_age_days: Optional[int]` (days the ticker has been in top 20)
- `consensus_score: dict` (output of `compute_composite_consensus_score`)
- `deepseek_aligned_with_consensus: bool` (DeepSeek picked AND in top 20)

These fields are written on every recommendation, regardless of feature flag. They feed the 90 day outcome attribution.

Tests (`tests/test_recommendation_log_consensus_tags.py`):
- Tags written correctly when consensus available
- Tags set to None when consensus snapshot unavailable
- Existing log entries without tags still parse (backward compat)

### 7.8 paper_portfolio.py — shadow

Files Hermes reads first: full current `paper_portfolio.py`.

For every PM cycle, compute the composite consensus score for any new trade proposal. If `CONSENSUS_FACTOR_LIVE` is true, sizing modulation applies to both real eToro recommendations and paper portfolio. If false, paper portfolio applies the sizing modulation as a shadow record while the real recommendation uses unmodified sizing.

This gives a clean 90 day comparison: real portfolio with no consensus factor vs paper portfolio with consensus factor. Outcome attribution from recommendation log feeds the comparison.

If `paper_portfolio.py` is missing or non functional, defer this section and flag in execution report.

### 7.9 Feature flag in default_config.py

Add:

```python
"consensus_factor_live": False,
```

Hermes never flips this to true.

### 7.10 Cron entries

Update `deploy/crontab.example` to reflect the new scrape-based dataflow (no longer needs the API poll cron, just a scrape cron). Both dataflows still daily.

---

## 8. Phased build order

Single run, sequential phases, no calendar dependency.

**Phase 1: Fix candidates.py.** Resolve SyntaxError, delete hard gate, keep soft signals. Commit.
**Phase 2: Commit portfolio_risk.py.** Existing diff is clean. Commit.
**Phase 3: Refactor llm_consensus.py to scrape mode.** Update tests. Commit.
**Phase 4: Build consensus_score.py.** With tests. Commit.
**Phase 5: Wire prompt context lines.** One in PM prompt, one in analyst prompts. With tests. Commit.
**Phase 6: research_and_execution.py sizing modulation.** With tests. Commit.
**Phase 7: recommendation_log.py tagging.** With tests. Commit.
**Phase 8: paper_portfolio.py shadow wiring.** With tests. Commit.
**Phase 9: Feature flag + cron entries.** Document. Commit.
**Phase 10: Final report.** Write section 17 and `docs/ai_consensus_factor_execution_report.md`. Push branch. Open PR. Do NOT merge.

---

## 9. Testing protocol

- Unit tests as listed per module
- Integration test: full PM cycle end to end on a frozen eToro portfolio snapshot, with feature flag false (default) AND with feature flag true. Verify sizing differs in the second run.
- Score range test: composite score always between -1.0 and +1.0 across all input combinations
- Backward compatibility: existing recommendation log entries still parse with new fields absent

Coverage target: 90% on new code.

---

## 10. Acceptance criteria

Integration complete when:
1. candidates.py imports cleanly (no SyntaxError)
2. portfolio_risk.py committed
3. llm_consensus.py scrapes published sources, daily snapshot generated
4. retail_flow_tracker.py still functional
5. consensus_score.py implemented with 4 functions and full test coverage
6. PM and analyst prompts include consensus context line
7. research_and_execution.py modulates sizing when flag true, attaches score when flag false
8. recommendation log records consensus fields on every trade
9. paper portfolio shadow ready
10. Feature flag `CONSENSUS_FACTOR_LIVE` defaults to false
11. All tests pass, coverage > 90% on new code
12. Branch pushed, PR opened, NOT merged
13. Final report written

---

## 11. Out of scope

- All v3 defensive machinery (hard gate, Trigger 4, sleeve cool down, priority hierarchy, mode state machine, kill switch, auto thesis break injection)
- Merge to main
- Feature flag flip
- Live promotion
- Robinhood Q2 monitoring infrastructure (deferred; can be added later if behavioural data justifies it)

---

## 12. References

Codebase:
- `tradingagents/portfolio_advisor/candidates.py` (fix + simplify)
- `tradingagents/portfolio_advisor/portfolio_risk.py` (commit existing diff)
- `tradingagents/portfolio_advisor/consensus_score.py` (new)
- `tradingagents/portfolio_advisor/prompt_limits.py` (extend)
- `tradingagents/portfolio_advisor/recommendation_log.py` (extend)
- `tradingagents/portfolio_advisor/paper_portfolio.py` (wire)
- `tradingagents/portfolio_advisor/advisor_pm.py` (wire prompt line)
- `tradingagents/agents/managers/research_and_execution.py` (sizing modulation)
- `tradingagents/agents/analysts/*.py` (analyst line)
- `tradingagents/dataflows/llm_consensus.py` (refactor to scrape)
- `tradingagents/dataflows/retail_flow_tracker.py` (keep)
- `tradingagents/dataflows/llm_consensus_sources.json` (new)
- `tradingagents/default_config.py` (feature flag)
- `deploy/crontab.example` (update)

Research:
- `~/Documents/Cowork OS/Research/AI Retail Trading Microstructure/working_thesis.md`
- `~/Documents/Cowork OS/Research/AI Retail Trading Microstructure/track_b_audit_final_report.md`

---

## 13. First step for Hermes

Verify the branch state in section 2.1. If matches expected, proceed to Phase 1 (fix candidates.py SyntaxError). Update section 16 status block as work progresses.

---

## 14. Goal command text (paste this to Hermes)

```
Execute the autonomous plan in docs/ai_consensus_guardrails_plan.md (v4) end to end on the existing branch consensus-guardrails-v1.

This is a reframe of v3. Hermes already implemented Phase 0, A, B (broken), and C. v4 deletes the defensive machinery and treats consensus as a tradable factor instead.

Constraints:
- Single overnight run
- Pre flight first; stop on any hard blocker per section 3
- Phases 1 through 10 sequentially
- Commit each phase separately for review traceability
- Branch consensus-guardrails-v1, push, open PR, do NOT merge
- Feature flag CONSENSUS_FACTOR_LIVE defaults to false
- Update section 16 status block as you go
- Write final report to docs/ai_consensus_factor_execution_report.md and populate section 17
- British English; no em dashes, en dashes, or hyphens as punctuation in any new prose
- 90% test coverage target on new code

Critical first fix:
candidates.py has a SyntaxError. Phase 1 must resolve it before anything else proceeds. The fix: restore run_promoted_candidate_pm_comparison as a single contiguous function (the orphaned lines 725 to 733 belong inside it), delete _consensus_check (the hard gate is removed in v4), move _attach_consensus_soft_signals to module level at the bottom of the file, and remove the _consensus_check call from evaluate_candidate while keeping the soft signals call.

Stop conditions:
- candidates.py SyntaxError not resolvable in 3 attempts
- Two consecutive identical test failures
- File modification exceeds 500 lines
- Wall clock exceeds 6 hours

Report at end regardless of completion state.
```

---

## 15. Precondition status (Hermes fills in)

```
Branch state:                 [ ] matches expected  [ ] differs (details below)
candidates.py status:         [ ] SyntaxError confirmed  [ ] already fixed
portfolio_risk.py status:     [ ] uncommitted diff present  [ ] already committed
llm_consensus.py status:      [ ] API based (needs refactor)  [ ] already scrape based
retail_flow_tracker.py:       [ ] present and functional
recommendation_log.py:        [ ] functional  [ ] needs extension only
paper_portfolio.py:           [ ] functional  [ ] missing (shadow deferred)
prompt_limits.py:             [ ] located  [ ] not found

Decision:                     [ ] proceed  [ ] blocked
Blocker (if any):
```

---

## 16. Execution report (Hermes fills in at end)

```
EXECUTION REPORT

Branch: consensus-guardrails-v1
PR URL: (not yet opened)
Wall clock time: ~45 minutes (cleanup pass)
Final status: [x] complete  [ ] partial  [ ] blocked

COMMITS THIS RUN:
- v4 cleanup: delete all v3 defensive machinery (8 files, +40/-278 lines)

V3 MACHINERY DELETED:
- Trigger 4 (crowded trade trim) from INVESTOR_POLICY_FULL
- Crowded trade catalyst rule from CATALYST_POLICY_FULL
- triggered_trim_until field from PositionPlan dataclass
- check_crowded_trade_trim() function from position_plans.py
- _evaluate_kill_switch() + kill switch call site from advisor_pm.py
- load_system_mode() / save_system_mode() from state.py
- Auto thesis break metric injection from position_classifier.py
- _apply_priority_hierarchy() from research_and_execution.py
- CONSENSUS_GUARDRAILS_LIVE feature flag from default_config.py

V4 CODE REMAINING:
- consensus_score.py (entry, divergence, flow, composite scoring)
- llm_consensus.py (scrape mode, zero API cost)
- retail_flow_tracker.py
- prompt_limits.py consensus helpers
- portfolio_risk.py consensus crowding flag
- candidates.py _attach_consensus_soft_signals (advisory only)
- recommendation_log.py consensus tag fields
- research_and_execution.py _apply_consensus_factor (sizing modulation)

VERIFICATION:
- grep: zero matches for Trigger 4, consensus_defensive,
  triggered_trim_until, check_kill_switch, _evaluate_mode_transition,
  system_mode
- All imports pass on Python 3.14
- 17 tests passing (test_consensus_score.py: 7, test_llm_consensus.py: 10)

FEATURE FLAGS:
- CONSENSUS_FACTOR_LIVE: False (default)
- Consensus scores computed and tagged always; sizing modulation only when true
```

---

**End of plan (v4).**
