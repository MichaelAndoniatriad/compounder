# Compounder 2.0 — Vision & Execution Plan

**Date:** 2026-06-09
**Status:** Agreed direction. Supersedes the priority of `core_discovery_v2_plan.md` (deferred, see §8).
**One-line mission:** Turn Compounder from a system that *looks* intelligent into one that *cannot avoid being measured* — and therefore can actually become intelligent.

---

## 1. Why 2.0 — honest diagnosis of 1.0

A critical audit (2026-06-09) of the codebase and runtime state found:

| Claim | Reality |
|---|---|
| "Learns from outcomes" | `reflection.py` writes LLM prose about P&L; `memory.py` re-injects the prose. Nothing changes behavior: no rule is down-weighted, no confidence recalibrated. It is a **journaling system, not a learning loop**. |
| "Accountable" | `recommendation_log.jsonl` had **5 entries, all screening events, zero measured outcomes**. `was_correct` is absolute ±5%, not relative to a benchmark — a +6% pick while QQQ does +12% scores "good". |
| "Multi-perspective debate" | Bull/bear and risk debaters receive **the same four analyst reports** and re-process them through personas. No new tools, no new data. One opinion expensively rephrased. |
| "Picks and sizes positions" | Exit rules are deterministic and good. **Entry sizing is an LLM heuristic** ("1/3 tranche") — no volatility scaling, no conviction weighting. Logged `confidence` affects nothing. |
| "Tests itself" | `paper_portfolio.py` exists (next-open fills, 0.1% friction, SPY benchmark) but **nothing calls `execute_recommendation()` automatically**. It is forensic, not a feedback mechanism. |

Root cause: the feedback plumbing exists in pieces but was never wired into a closed loop, and outcome data only accumulates when the human acts on advice — which mostly doesn't happen. The flywheel has never completed one rotation.

**What 1.0 got right (keep, do not rebuild):** deterministic exit rules with sleeve lock (`position_plans.py`), hard candidate gates (`candidates.py`), the recommendation-log schema incl. human-override analysis (`recommendation_log.py`), cheap DeepSeek model routing, watchdog price triggers, Telegram messaging with dedupe/quiet-hours.

## 2. Definition of "intelligent" (the bar for 2.0)

> A system is intelligent iff its **behavior changes in response to measured outcomes**, and its claims survive comparison against a benchmark it did not choose after the fact.

Everything in this plan is plumbing toward that definition. No new discovery features, no new agents, no new data sources until the measurement loop is live.

## 3. Architecture principle

**Keep the skeleton, rewire the feedback.** 1.0's pipeline (discovery → gates → research → PM → advice) stays. 2.0 adds a closed measurement loop around it:

```
                ┌──────────────────────────────────────────────┐
                │                                              ▼
discovery → gates → PM advice → Telegram (human)        PAPER EXECUTION
                ▲                                       (advisor book +
                │                                        shadow book)
                │                                              │
        scoreboard injected                              outcomes scored
        into PM prompt each cycle                        vs QQQ (alpha)
                │                                              │
                └────────── outcome_tracker stats ◄────────────┘
```

## 4. Phase 0 — Backfill (one session; verdict before further investment)

Replay the **17 existing proposals** in `~/.tradingagents/portfolio_advisor/proposed_trades.jsonl` against actual prices from proposal timestamp to today, vs QQQ over the same window.

- Input: ticker, ts, target_price/approx_usd from each proposal; yfinance daily closes; QQQ closes.
- Output: per-proposal return, alpha vs QQQ, and an aggregate "if followed blindly" figure. Write to `docs/backfill_2026-06.md` + seed `outcomes.jsonl`.
- Purpose: cheapest possible sign-of-life check. Uniformly terrible → early warning before any clock starts. Mixed-to-decent → justifies the test.
- Caveats: tiny n, no stops simulated, proposals were repeated (dedupe by ticker+thesis); treat as smoke test, not statistics.

## 5. Phase 1 — Pre-test upgrades (2–3 sessions)

These must land **before** the live test starts, otherwise the test measures known bugs instead of the idea.

### 5.1 Wire the paper portfolio (the virtual account)
- Call `PaperPortfolio.execute_recommendation()` automatically from the PM cycle whenever a trade is proposed (hook in `advisor_pm.py`, which currently only reads `build_paper_portfolio_block`).
- Fills at next open with existing 0.1% friction. Seed with a chosen cash balance via `paper-init` (or eToro clone).
- **Every paper trade sends a Telegram message** via `messaging.py`: side, ticker, shares, fill, sleeve, stop, one-line reason. Human watches; never has to act.

### 5.2 Alpha-relative outcome scoring
- Change `was_correct` / outcome classification from absolute ±5% to **return minus QQQ over the same holding window** in `outcome_tracker.py` + `recommendation_log.py`.
- Keep the absolute number alongside; alpha is the headline.

### 5.3 Scoreboard injection (the cheap learning loop)
- Each PM cycle, inject a compact live scoreboard into the PM prompt:
  - per-rule hit rates from `outcome_tracker` (n ≥ threshold),
  - per-source track record (news-funnel vs idea_generator vs core_discovery picks),
  - calibration line ("your conf ≥ 0.8 calls: 54% hit rate, n=13"),
  - paper book vs QQQ since start.
- Deterministic teeth: any rule/source with hit rate < 45% at n ≥ 10 gets auto-flagged in the prompt and surfaced to the human; at n ≥ 20 it is disabled pending review. (Thresholds in config, not code.)

### 5.4 Confidence must cost something
- Paper position size = f(stated confidence) — e.g. linear 2%→6% of book between conf 0.5 and 0.9, vol-dampened (size ÷ 60-day realized vol percentile). Exact formula in config.
- Miscalibration then shows up directly as paper P&L and in the §5.3 calibration line.

### 5.5 Inject deterministic risk, cut LLM noise
- `portfolio_risk.py` (correlation matrix, risk contributions) is computed but **unread at decision time** — inject its output into the PM prompt.
- Drop bull/bear + risk-debate layers from **routine** runs (keep for full_graph deep dives on new names if desired). Fewer LLM calls, one genuinely new signal added.

## 6. Phase 2 — The live test

### 6.1 The eToro demo account
Demo (virtual) account credentials are stored in `.env` as `ETORO_DEMO_AUTH_TOKEN` and `ETORO_DEMO_USER_KEY` (added 2026-06-09; **never** commit values or copy them elsewhere).

- **First task: verify what they grant.** eToro has no retail order API, so assume read-only portfolio sync against the demo account until proven otherwise. If the existing eToro client authenticates with these, the demo account becomes the human-visible mirror of the paper book (user manually mirrors paper trades there if desired, or we just read it).
- **Ground truth stays the internal paper book** regardless — it fills deterministically and can't be polluted by manual fiddling.
- Execution remains human-only on the real account. Unchanged policy.

### 6.2 Two books
| Book | Trades | Tests |
|---|---|---|
| **Advisor book** | only what the PM actually advises | "is the advisor worth following?" |
| **Shadow book** | every candidate that clears hard gates, small equal-vol positions | "does the pipeline find alpha at all?" — 5–10× more resolved outcomes per month, faster learning |
Both share the same `paper_portfolio.py` plumbing (second state file). The divergence between them is itself a finding (PM adds value / PM subtracts value).

### 6.3 Notifications & cadence
- Per-trade Telegram ping (both books, tagged `[PAPER]` / `[SHADOW]`).
- **Weekly scoreboard message** (Sat, with existing weekly cron): both books vs QQQ since start, hit rate, alpha, open positions, calibration line. This is the human's entire time cost.

### 6.4 Kill criterion (written now, before the test — non-negotiable)
> **At 30 resolved recommendations or 6 months (whichever first): if mean per-recommendation call-alpha ≤ 0 (and/or <50% of calls alpha-positive), the discovery pipeline gets redesigned, not patched.** If shadow-book call-alpha > 0 but advisor call-alpha ≤ 0, the PM layer is the problem and gets redesigned instead.

No moving the goalposts after the fact. The criterion lives here so it can't be quietly forgotten.

### 6.5 Structural divergence between the books (known, accepted)
The paper book executes EVERY recommendation; the human executes some. The PM's
advice is conditioned on the ETORO book (its sleeves, cash, holdings) — so as the
books drift apart, paper-book *portfolio-level* metrics (sleeve mix, concentration,
equity curve) increasingly reflect eToro-conditioned advice applied to a different
portfolio. This is accepted, not fixed, because the metric hierarchy makes it harmless:

1. **Primary: per-recommendation call-alpha** (recommendation_log → outcome_tracker).
   Each call is measured independently — ticker return vs QQQ over the call's own
   horizon, sign-flipped for sell/trim calls. Immune to book divergence. This drives
   the kill criterion, the calibration block, and rule flagging.
2. **Secondary: Alpaca equity curve vs SPY.** Directional color on "advice followed
   literally," confounded by the conditioning mismatch — never the deciding metric.
3. **Tertiary: human_override_analysis.** eToro-vs-paper divergence is itself the
   measurement of whether the human's filter adds value.

Paper-only positions (advice the human didn't take) get hard-floor exits only
(catalyst −8%/max-hold, core −40% via enforce_paper_exits) — cruder than the
watchdog/PM management eToro names get. Acceptable for the same reason: those
positions inform metric 2, not metric 1.

Possible Phase-3 upgrade (NOT now): a separate autonomous PM pass that manages the
Alpaca book as a first-class portfolio (own sleeve targets, own cash logic). That
answers a different question — "can the system run a portfolio end-to-end?" — and
only becomes worth asking if metric 1 survives the kill criterion.

## 7. Success metrics

1. **Primary:** advisor-book alpha vs QQQ (after paper friction), at the kill-criterion checkpoint.
2. Hit rate (alpha-positive %) per book, per source, per rule.
3. Calibration: hit rate of conf ≥ 0.8 vs conf < 0.6 calls must be ordered correctly.
4. Loop liveness: every recommendation gets a resolved outcome with zero human action (the 1.0 failure mode).

## 8. Explicitly deferred (do NOT build until the scoreboard is live)

- Core Discovery v2 anti-herd tilts, consensus factor wiring (`core_discovery_v2_plan.md`)
- EP gate-audit branch merge as a feature effort
- Universe widening below current cap
- Any execution automation (real or demo)
- New data sources / paid feeds

Rationale: adding picker features to an unmeasured picker is how 1.0 spent its effort. Each deferred item gets re-evaluated **against scoreboard data** once it exists.

## 9. Sequencing

| Step | What | Effort |
|---|---|---|
| 0 | Backfill 17 proposals → `docs/backfill_2026-06.md` | 1 session |
| 1 | Wire paper execution + Telegram pings (§5.1) | 1 session |
| 2 | Alpha scoring + scoreboard injection (§5.2, §5.3) | 1 session |
| 3 | Confidence sizing + risk injection + debate cut (§5.4, §5.5) | 1 session |
| 4 | Demo-account auth probe + shadow book + weekly scoreboard (§6) | 1 session |
| 5 | Start the clock. Build nothing. Read weekly messages. | 6 months / 30 outcomes |
