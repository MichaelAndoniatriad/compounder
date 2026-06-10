# Compounder 2.0 — Phase 0 Backfill
**Date:** 2026-06-10
**Purpose:** Sign-of-life check on existing proposals before committing to the live test. Per the 2.0 vision: "Uniformly terrible → early warning. Mixed-to-decent → justifies the test."

---

## Finding: No meaningful historical track record exists

The Hetzner server (sole production system) was abandoned 2026-06-08 after an SSH brute-force takedown. Its decision history, proposal logs, and outcomes did not survive. The Mac's `proposed_trades.jsonl` was only populated during the 2026-06-09 rebuild session — **all 17 proposals are from a single day**.

This is not a failure of the backfill; it's the correct answer to "what historical signal exists?" — none. The track record starts from today.

---

## What the 1-day window shows

All 7 unique ticker/direction pairs proposed on 2026-06-09, measured from day-open to 2026-06-10 close vs QQQ (-2.09% same window):

| Ticker | Direction | Entry | Close | Raw ret | Alpha vs QQQ | Status |
|--------|-----------|-------|-------|---------|--------------|--------|
| ISRG   | BUY       | 421.03 | 426.61 | +1.3%  | **+3.4%** | cancelled |
| NFLX   | SELL      | 82.03  | 81.41  | +0.8%  | -1.3% | cancelled |
| DKNG   | BUY       | 25.15  | 27.59  | +9.7%  | **+11.8%** | cancelled |
| TEAM   | SELL      | 95.31  | 95.61  | -0.3%  | -2.4% | executed |
| AMZN   | BUY       | 247.70 | 244.19 | -1.4%  | +0.7% | cancelled |
| VEEV   | BUY       | 165.51 | 167.68 | +1.3%  | **+3.4%** | cancelled |
| CRDO   | BUY       | 226.50 | 234.32 | +3.5%  | **+5.5%** | cancelled |

**Summary (1-day, n=7):**
- 5/7 alpha-positive (71%) — vs QQQ over the same window
- Mean alpha: +3.0% (heavily influenced by DKNG's World Cup catalyst call)
- The one executed trade (TEAM sell) was alpha-negative (-2.4%)
- 5/6 non-TEAM proposals were cancelled — the system advised but nothing happened

**Caveats rendering this unactionable:**
1. N=7 over 1 day is pure noise. A coin flip produces similar variance.
2. QQQ fell -2.09% that day — any long portfolio would look alpha-positive.
3. DKNG's +11.8% alpha dominates; strip it and mean alpha is +1.5%.
4. "Executed" status on TEAM means the proposal was logged; the human may or may not have acted on eToro.

---

## Verdict

> Insufficient data to confirm or deny edge. Proceed with the live test.

The 1-day sample is neither "uniformly terrible" (which would warrant stopping) nor "statistically significant." The structural improvements in Phase 1 are worth building regardless of this noise. The real test starts from today with the Alpaca paper book.

---

## What changes as a result

- `outcomes.jsonl` is NOT seeded from these proposals — 1-day data would pollute the outcome tracker's hit-rate calculations with noise that looks like signal. The file stays empty; the tracker bootstraps from real live outcomes.
- The kill criterion stands: **at 30 resolved recommendations or 6 months, advisor-book alpha ≤ 0 vs QQQ → redesign the discovery pipeline.** Clock starts 2026-06-10.
